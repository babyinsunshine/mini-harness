"""真实 MCP 子进程与图；只替代外部模型。"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from test_agent_cli import ScriptedModel, call, consume
from test_subagents import AgentModel

from mini_harness.agent import Harness

pytest.importorskip("fastmcp.client")


@pytest.fixture
def mcp_project(tmp_path):
    server = tmp_path / "server.py"
    server.write_text('''
import json
import os
import base64
import sys
from pathlib import Path
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent

root = Path(os.environ["TEST_MCP_ROOT"])
(root / "server.pid").write_text(str(os.getpid()))
with (root / "starts.txt").open("a") as stream:
    stream.write("x")
server = MCPServer("fixture")
count = 0

@server.tool()
def increment(label: str) -> dict:
    """Increment a counter, retaining state between calls."""
    global count
    count += 1
    (root / "effect.json").write_text(json.dumps({"count": count}))
    return {"label": label, "count": count, "pid": os.getpid(),
            "has_model_key": "OPENAI_API_KEY" in os.environ}

@server.tool()
def fail() -> str:
    """Return a controlled error."""
    raise ToolError("MCP fixture failure")

@server.tool()
def mixed() -> CallToolResult:
    """Return text with additional structured data."""
    return CallToolResult(content=[TextContent(type="text", text="Saved successfully")],
                          structured_content={"record_id": 42})

@server.tool(structured_output=False)
def payload(kind: str) -> list:
    """Return a large result or image for artifact handling."""
    if kind == "long":
        return [TextContent(type="text", text="x" * 20000)]
    return [ImageContent(type="image", data=base64.b64encode(b"image-fixture").decode(),
                         mime_type="image/png")]

@server.tool()
async def slow() -> str:
    """Wait until canceled."""
    import asyncio
    (root / "slow.started").touch()
    await asyncio.sleep(60)
    return "finished"

if len(sys.argv) > 1:
    server.run("streamable-http", host="127.0.0.1", port=int(sys.argv[1]))
else:
    server.run()
''')
    config = {"mcpServers": {"local": {
        "command": sys.executable,
        "args": ["server.py"],
        "env": {"TEST_MCP_ROOT": str(tmp_path)},
    }}}
    (tmp_path / ".mcp.json").write_text(json.dumps(config))
    return tmp_path


def mcp_call(call_id="increment"):
    return call("call_mcp_tool", {
        "server": "local", "tool_name": "increment", "arguments": {"label": "test"},
    }, call_id)


@pytest.mark.parametrize("sandbox", [False, True])
async def test_mcp_discovers_schemas_keeps_session_and_cleans_up(mcp_project, monkeypatch, sandbox):
    if sandbox:
        monkeypatch.setattr("mini_harness.sandbox.Sandbox.require_runtime", lambda self: "bwrap")
    monkeypatch.setenv("OPENAI_API_KEY", "test-value-must-not-reach-mcp")
    model = ScriptedModel(responses=[
        call("list_mcp_tools", {}, "servers"),
        call("list_mcp_tools", {"server": "local", "tool_name": "increment"}, "catalog"),
        mcp_call("first"), mcp_call("second"), AIMessage(content="Done"),
    ])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=mcp_project / ".mcp.json",
                            full_access=True, sandbox=sandbox) as harness:
        assert await consume(harness, "Discover and call MCP") == "Done"
        messages = model.seen[-1]
        results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
        assert all(result.status == "success" for result in results.values())
        assert "local" in results["servers"].text
        catalog = json.loads(json.loads(results["catalog"].text)["output"])
        assert [tool["name"] for tool in catalog] == ["increment"]
        increment = next(tool for tool in catalog if tool["name"] == "increment")
        assert increment["input_schema"]["required"] == ["label"]
        first = json.loads(json.loads(results["first"].text)["output"])
        second = json.loads(json.loads(results["second"].text)["output"])
        assert first["count"] == 1 and second["count"] == 2
        assert first["pid"] == second["pid"]
        assert not first["has_model_key"]
        pid = first["pid"]
        assert os.kill(pid, 0) is None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("allowed", [False, True])
async def test_mcp_call_respects_existing_approval(mcp_project, allowed):
    async def approve(tool_call, reason):
        return allowed

    model = ScriptedModel(responses=[mcp_call(), AIMessage(content="Checked")])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=mcp_project / ".mcp.json",
                            approve=approve) as harness:
        await consume(harness, "Call MCP")
    assert (mcp_project / "effect.json").exists() == allowed
    result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
    assert result.status == ("success" if allowed else "error")


async def test_mcp_error_returns_to_model(mcp_project):
    model = ScriptedModel(responses=[
        call("call_mcp_tool", {
            "server": "local", "tool_name": "fail", "arguments": {},
        }, "fail"),
        AIMessage(content="Handled"),
    ])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=mcp_project / ".mcp.json",
                            full_access=True) as harness:
        assert await consume(harness, "Handle the failing tool") == "Handled"
    result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
    assert result.status == "error" and "MCP fixture failure" in result.text


async def test_incomplete_mcp_call_is_reported_without_crashing_cli(mcp_project, capfd):
    from mini_harness.cli.display import display_events

    model = ScriptedModel(responses=[
        call("call_mcp_tool", {"server": "local", "tool_name": "increment"}, "incomplete"),
        AIMessage(content="Handled"),
    ])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=mcp_project / ".mcp.json",
                            full_access=True) as harness:
        await display_events(harness, "Call with missing arguments")
    result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
    assert result.status == "error" and "arguments" in result.text
    assert "Handled" in capfd.readouterr().out
    assert not (mcp_project / "effect.json").exists()


async def test_mcp_preserves_text_and_structured_content(mcp_project):
    model = ScriptedModel(responses=[
        call("call_mcp_tool", {
            "server": "local", "tool_name": "mixed", "arguments": {},
        }, "mixed"),
        AIMessage(content="Read result"),
    ])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=mcp_project / ".mcp.json",
                            full_access=True) as harness:
        await consume(harness, "Read text and structured data")
    result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
    assert result.status == "success"
    output = json.loads(result.text)["output"]
    assert "Saved successfully" in output
    assert json.loads(output)["structured_content"] == {"record_id": 42}


async def test_mcp_catalog_does_not_require_approval(mcp_project):
    model = ScriptedModel(responses=[
        call("list_mcp_tools", {"server": "local"}, "catalog"), AIMessage(content="Found tools"),
    ])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=mcp_project / ".mcp.json") as harness:
        await consume(harness, "Inspect MCP")
    result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
    assert result.status == "success" and "increment" in result.text


async def test_child_reuses_parent_mcp_and_leaves_connection_open(mcp_project):
    def reply(child, messages):
        label = "child" if child else (
            "again" if any(m.content == "Again" for m in messages) else "parent"
        )
        if not any(isinstance(m, ToolMessage) and m.tool_call_id == label for m in messages):
            return mcp_call(label)
        return AIMessage(content="Done")

    model = AgentModel(handler=reply)
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=mcp_project / ".mcp.json",
                            full_access=True) as harness:
        await consume(harness, "Start")
        child = harness.subagents.spawn("Use MCP", timeout=10)
        result = await harness.subagents.wait(child["agent_id"])
        assert result["status"] == "waiting", result
        assert json.loads((mcp_project / "effect.json").read_text())["count"] == 2
        await consume(harness, "Again")
        assert json.loads((mcp_project / "effect.json").read_text())["count"] == 3
        assert (mcp_project / "starts.txt").read_text() == "x"


async def test_mcp_large_text_and_images_are_saved_as_artifacts(mcp_project):
    from pathlib import Path

    model = ScriptedModel(responses=[
        *[call("call_mcp_tool", {
            "server": "local", "tool_name": "payload", "arguments": {"kind": kind},
        }, kind) for kind in ("long", "image")],
        AIMessage(content="Saved"),
    ])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=mcp_project / ".mcp.json",
                            full_access=True) as harness:
        await consume(harness, "Read payloads")
        results = {m.tool_call_id: json.loads(m.text)
                   for m in model.seen[-1] if isinstance(m, ToolMessage)}
        assert Path(results["long"]["output_path"]).read_text() == "x" * 20000
        assert len(results["long"]["preview"]) < 20000
        image = next(harness.session.artifacts.glob("*.png"))
        assert image.read_bytes() == b"image-fixture"
        assert str(image) in results["image"]["output"]


async def test_streamable_http_uses_the_same_tool_interface(mcp_project):
    environment = dict(os.environ, TEST_MCP_ROOT=str(mcp_project))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with (mcp_project / "http.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, str(mcp_project / "server.py"), str(port)],
            env=environment, stdout=log, stderr=log,
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    assert process.poll() is None and time.monotonic() < deadline
                    time.sleep(0.05)
            (mcp_project / ".mcp.json").write_text(json.dumps({"mcpServers": {
                "local": {"url": f"http://127.0.0.1:{port}/mcp"},
            }}))
            model = ScriptedModel(responses=[mcp_call(), AIMessage(content="HTTP works")])
            async with Harness.open(mcp_project, mcp_project / "session", model,
                                    mcp_config=mcp_project / ".mcp.json",
                                    full_access=True) as harness:
                assert await consume(harness, "Call HTTP MCP") == "HTTP works"
            assert json.loads((mcp_project / "effect.json").read_text())["count"] == 1
            assert process.poll() is None  # HTTP 服务由外部启动，关闭客户端不关闭服务。
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


async def test_explicit_config_resolves_cwd_relative_to_its_directory(mcp_project):
    config_path = mcp_project / ".mcp.json"
    config = json.loads(config_path.read_text())
    config["mcpServers"]["local"]["cwd"] = "."
    config_path.write_text(json.dumps(config))
    model = ScriptedModel(responses=[mcp_call(), AIMessage(content="Done")])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=config_path, full_access=True) as harness:
        await consume(harness, "Use the explicit config")
    assert json.loads((mcp_project / "effect.json").read_text())["count"] == 1


async def test_canceling_mcp_call_records_interruption_and_cleans_server(mcp_project):
    model = ScriptedModel(responses=[call("call_mcp_tool", {
        "server": "local", "tool_name": "slow", "arguments": {},
    }, "slow")])
    async with asyncio.timeout(10):
        async with Harness.open(mcp_project, mcp_project / "session", model,
                                mcp_config=mcp_project / ".mcp.json",
                                full_access=True) as harness:
            running = asyncio.create_task(consume(harness, "Start slow MCP"))
            try:
                while not (mcp_project / "slow.started").exists():
                    await asyncio.sleep(0.01)
                running.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await running
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            saved = harness.session.get_execution("slow")
            assert saved[0] == "error" and "interrupted" in saved[1]["data"]["content"].lower()
            pid = int((mcp_project / "server.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_cli_mcp_config_is_restored_on_resume(mcp_project):
    from mini_harness.session import Session

    sessions = mcp_project / "sessions"
    config = mcp_project / ".mcp.json"
    environment = dict(os.environ, OPENAI_API_KEY="local-startup-test-not-a-secret",
                       LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")
    command = [sys.executable, "-m", "mini_harness", "--session-dir", str(sessions)]
    first = subprocess.run(
        [*command, "--cwd", str(mcp_project), "--mcp-config", str(config)],
        input="/stats\n/exit\n", env=environment, text=True, capture_output=True, timeout=15,
    )
    assert first.returncode == 0, first.stderr
    directory = next(sessions.iterdir())
    with Session(directory) as session:
        assert session.get("config")["mcp_config"] == str(config)
    pid = int((mcp_project / "server.pid").read_text())
    resumed = subprocess.run(
        [*command, "--resume", directory.name],
        input="/stats\n/exit\n", env=environment, text=True, capture_output=True, timeout=15,
    )
    assert resumed.returncode == 0, resumed.stderr
    assert (mcp_project / "starts.txt").read_text() == "xx"
    assert int((mcp_project / "server.pid").read_text()) != pid


async def test_mcp_server_only_starts_when_config_is_selected(mcp_project):
    model = ScriptedModel(responses=[AIMessage(content="Ready")])
    async with Harness.open(mcp_project, mcp_project / "session", model) as harness:
        assert await consume(harness, "Hello") == "Ready"
        assert not (mcp_project / "starts.txt").exists()
