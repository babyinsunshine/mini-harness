import asyncio
import json
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from mini_harness.sandbox import Sandbox
from mini_harness.session import Session
from mini_harness.tools.processes import ProcessManager


@pytest.fixture
def bubblewrap():
    executable = shutil.which("bwrap")
    if executable is None:
        pytest.skip("Bubblewrap is not installed")
    probe = subprocess.run(
        [executable, "--unshare-all", "--share-net", "--ro-bind", "/", "/",
         "--proc", "/proc", "--", "/usr/bin/true"],
        capture_output=True, text=True, timeout=5,
    )
    if probe.returncode:
        pytest.skip(f"Bubblewrap namespaces unavailable: {probe.stderr.strip()}")


@pytest.fixture
async def processes(tmp_path, bubblewrap):
    project = tmp_path / "project"
    project.mkdir()
    with Session(tmp_path / "session ' $HOME ; 中文", project) as session:
        session.sandbox = Sandbox.load(project)
        async with ProcessManager(session) as manager:
            yield manager


async def test_bash_keeps_outside_reads_and_blocks_outside_writes(processes, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("original outside content")
    result = await processes.run(f"cat {shlex.quote(str(outside))}")
    assert result["preview"] == "original outside content"
    result = await processes.run(f"printf changed > {shlex.quote(str(outside))}")
    assert result["exit_code"] != 0
    assert outside.read_text() == "original outside content"
    result = await processes.run("printf allowed > allowed.txt")
    assert result["exit_code"] == 0
    assert (processes.session.cwd / "allowed.txt").read_text() == "allowed"


@pytest.mark.parametrize("deny_directory", [False, True])
async def test_bash_cannot_read_denied_file_or_directory(processes, tmp_path, deny_directory):
    private = processes.session.cwd / "private"
    private.mkdir()
    secret = private / "secret.txt"
    secret.write_text("PRIVATE FIXTURE CONTENT")
    processes.session.sandbox.deny_paths = (private if deny_directory else secret,)
    result = await processes.run(f"cat {shlex.quote(str(secret))}")
    assert result["exit_code"] != 0
    assert "PRIVATE FIXTURE CONTENT" not in result["preview"]
    assert secret.read_text() == "PRIVATE FIXTURE CONTENT"


async def test_bash_cwd_channel_handles_shell_characters_and_sources_env(processes):
    folder = processes.session.cwd / "space ' $HOME ; 中文"
    folder.mkdir()
    processes.env_file.write_text("export LESSON_VALUE=project-value\n")
    result = await processes.run(f"cd {shlex.quote(str(folder))}; printf %s \"$LESSON_VALUE\"")
    assert result["exit_code"] == 0
    assert result["preview"] == "project-value"
    assert processes.session.cwd == folder
    assert (await processes.run("pwd"))["preview"].strip() == str(folder)


@pytest.mark.parametrize("grant_session_directory", [False, True])
async def test_bash_private_temp_does_not_write_host_temp_files(processes, grant_session_directory):
    if grant_session_directory:
        processes.session.sandbox.grant([str(processes.session.directory)])
    code = "import tempfile; f=tempfile.NamedTemporaryFile(" \
           "prefix='mini-harness-sandbox-test-',delete=False); " \
           "f.write(b'temporary'); f.close(); print(f.name)"
    result = await processes.run(f"/usr/bin/python3 -c {shlex.quote(code)}")
    assert result["exit_code"] == 0
    path = Path(result["preview"].strip())
    try:
        assert path.is_relative_to(processes.session.directory)
        assert not path.exists()
    finally:
        if path.exists() and path.name.startswith("mini-harness-sandbox-test-"):
            path.unlink()


async def test_bash_uses_host_network(processes):
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        code = "import urllib.request; c=urllib.request.build_opener(" \
               "urllib.request.ProxyHandler({})); " \
               f"print(c.open('http://127.0.0.1:{port}', timeout=2).read().decode())"
        result = await processes.run(f"/usr/bin/python3 -c {shlex.quote(code)}")
    assert result["exit_code"] == 0
    assert result["preview"].strip() == "ok"


async def test_background_server_child_is_stopped_with_command(processes):
    code = "from http.server import HTTPServer,SimpleHTTPRequestHandler; " \
           "s=HTTPServer(('127.0.0.1',0),SimpleHTTPRequestHandler); " \
           "print(s.server_port,flush=True); s.serve_forever()"
    result = await processes.run(
        f"/usr/bin/python3 -u -c {shlex.quote(code)} & wait", run_in_background=True,
    )
    log = Path(result["output_path"])
    for _ in range(200):
        if log.read_text().strip():
            break
        await asyncio.sleep(0.01)
    assert log.read_text().strip().isdigit(), log.read_text()
    port = int(log.read_text().strip())
    _, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.close()
    await writer.wait_closed()
    await processes.stop_task(result["task_id"])
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)


async def test_sandbox_stdin_and_one_call_bypass(processes, tmp_path):
    result = await processes.run("cat", timeout=0.02)
    await processes.send_input(result["task_id"], "hello\n", eof=True)
    await processes.tasks[result["task_id"]].watch_task
    assert Path(result["output_path"]).read_text() == "hello\n"
    outside = tmp_path / "outside.txt"
    command = f"printf allowed > {shlex.quote(str(outside))}"
    result = await processes.run(command, bypass_sandbox=True)
    assert result["exit_code"] == 0 and outside.read_text() == "allowed"
    outside.unlink()
    assert (await processes.run(command))["exit_code"] != 0
    assert not outside.exists()


async def test_granted_parent_does_not_allow_overwriting_config(processes):
    config = processes.session.directory / "policy.json"
    config.write_text(json.dumps({"allow_bypass": True}))
    processes.session.sandbox = Sandbox.load(processes.session.cwd, config)
    processes.session.sandbox.grant([str(config.parent)])
    result = await processes.run(f"printf changed > {shlex.quote(str(config))}")
    assert result["exit_code"] != 0
    assert json.loads(config.read_text()) == {"allow_bypass": True}


def test_missing_runtime_does_not_fall_back_to_unsandboxed(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="[Bb]ubblewrap|bwrap"):
        Sandbox.load(tmp_path).require_runtime()


async def test_graph_approval_enables_real_sandbox_write_only_after_grant(tmp_path, bubblewrap):
    from langchain_core.messages import AIMessage, ToolMessage
    from test_agent_cli import ScriptedModel, call, consume

    from mini_harness.agent import Harness

    project, shared = tmp_path / "project", tmp_path / "shared"
    project.mkdir()
    shared.mkdir()
    target = shared / "output.txt"
    command = f"printf approved > {shlex.quote(str(target))}"
    approvals = []

    async def approve(tool_call, reason):
        assert not target.exists()
        approvals.append(tool_call["name"])
        return True

    model = ScriptedModel(responses=[
        call("bash", {"command": command}, "blocked"),
        call("request_sandbox_access", {"paths": [str(shared)], "reason": "Update shared code"},
             "grant"),
        call("bash", {"command": command}, "allowed"),
        AIMessage(content="Done"),
    ])
    async with Harness.open(project, tmp_path / "session", model, sandbox=True,
                            full_access=True, approve=approve) as harness:
        await consume(harness, "Update the external file")
    results = {m.tool_call_id: m for m in model.seen[-1] if isinstance(m, ToolMessage)}
    assert results["blocked"].status == "error"
    assert results["allowed"].status == "success"
    assert target.read_text() == "approved"
    assert approvals == ["request_sandbox_access"]


@pytest.mark.skipif(not Path("/mnt/c/Windows/System32/cmd.exe").is_file(),
                    reason="Windows interop probe requires WSL with Windows binaries")
async def test_wsl_windows_execution_bridge_is_blocked_inside_sandbox(processes):
    result = await processes.run(
        "/mnt/c/Windows/System32/cmd.exe /d /c echo HARNESS_INTEROP_MUST_BE_BLOCKED",
    )
    assert result["exit_code"] != 0
    assert "HARNESS_INTEROP_MUST_BE_BLOCKED" not in result["preview"]


async def test_renaming_denied_file_ancestor_cannot_expose_it_in_later_calls(processes):
    from mini_harness.tools.files import read_file

    parent = processes.session.cwd / "outer" / "private"
    parent.mkdir(parents=True)
    secret = parent / "secret.txt"
    secret.write_text("PRIVATE ANCESTOR FIXTURE")
    processes.session.sandbox.deny_paths = (secret,)
    result = await processes.run(
        "mv outer moved && mkdir -p outer/private && touch outer/private/secret.txt",
    )
    assert result["exit_code"] != 0
    assert secret.read_text() == "PRIVATE ANCESTOR FIXTURE"
    assert not (processes.session.cwd / "moved").exists()
    result = await processes.run("cat outer/private/secret.txt")
    assert result["exit_code"] != 0 and "PRIVATE ANCESTOR FIXTURE" not in result["preview"]
    with pytest.raises(PermissionError):
        read_file(processes.session, str(secret))
    assert (await processes.run("printf allowed > outer/private/allowed.txt"))["exit_code"] == 0


async def test_config_parent_rename_cannot_replace_original_policy(processes):
    parent = processes.session.cwd / "policies"
    parent.mkdir()
    config = parent / "sandbox.json"
    original = '{"allow_bypass": false}'
    config.write_text(original)
    processes.session.sandbox = Sandbox.load(processes.session.cwd, config)
    result = await processes.run(
        "mv policies saved && mkdir policies && printf '{}' > policies/sandbox.json",
    )
    assert result["exit_code"] != 0
    assert config.read_text() == original
    assert not (processes.session.cwd / "saved").exists()


async def test_session_metadata_is_protected_when_session_is_inside_project(tmp_path, bubblewrap):
    project = tmp_path / "project"
    project.mkdir()
    with Session(project / "session", project) as session:
        session.sandbox = Sandbox.load(project)
        session.set("sentinel", "original")
        query = "UPDATE metadata SET value='\"tampered\"' WHERE key='sentinel'"
        code = "import sqlite3; " \
               f"c=sqlite3.connect({str(session.runtime_database)!r}); " \
               f"c.execute({query!r}); " \
               "c.commit()"
        async with ProcessManager(session) as manager:
            result = await manager.run(f"/usr/bin/python3 -c {shlex.quote(code)}")
            assert result["exit_code"] != 0
            assert session.get("sentinel") == "original"
            result = await manager.run(
                f"printf 'export TEST_VALUE=ok' > {shlex.quote(str(manager.env_file))}",
            )
            assert result["exit_code"] == 0


async def test_runtime_binary_is_pinned_before_project_can_replace_path(processes, monkeypatch):
    import os

    original = Path(shutil.which("bwrap")).resolve()
    directory = processes.session.cwd / "bin"
    directory.mkdir()
    alias = directory / "bwrap"
    alias.symlink_to(original)
    monkeypatch.setenv("PATH", f"{directory}:{os.environ['PATH']}")
    processes.session.sandbox.require_runtime()
    alias.unlink()
    alias.write_text("#!/bin/sh\nprintf BYPASSED\n")
    alias.chmod(0o755)
    result = await processes.run("printf safe")
    assert result["exit_code"] == 0 and result["preview"] == "safe"


async def test_redundant_project_grant_does_not_block_directory_rename(processes):
    directory = processes.session.cwd / "source"
    directory.mkdir()
    processes.session.sandbox.grant([str(directory)])
    result = await processes.run("mv source renamed")
    assert result["exit_code"] == 0
    assert (processes.session.cwd / "renamed").is_dir()
