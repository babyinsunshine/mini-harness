"""Permission rules inspect requests; dangerous examples are never executed."""

import json

import pytest

from mini_harness.session import Session
from mini_harness.tools.permissions import Permissions, classify


@pytest.fixture
def session(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    with Session(tmp_path / "session", project) as value:
        yield value


def decision(session, name, **args):
    return classify({"name": name, "args": args, "id": "permission-test"}, session)[0]


@pytest.mark.parametrize("tool", ["read_file", "write_file", "edit_file"])
def test_file_scope(session, tmp_path, tool):
    assert decision(session, tool, path="new.py") == "allow"
    assert decision(session, tool, path=str(session.artifacts / "result.txt")) == "allow"
    assert decision(session, tool, path=str(tmp_path / "outside.txt")) == "ask"


@pytest.mark.parametrize("path", ["/etc/hosts", "/boot/config", "/proc/x", "/sys/x", "/dev/sda"])
def test_system_writes_are_denied(session, path):
    assert decision(session, "write_file", path=path) == "deny"
    assert decision(session, "edit_file", path=path) == "deny"


@pytest.mark.parametrize(
    "filename", ["state.sqlite", "runtime.sqlite", "transcript.jsonl", "state.sqlite-wal"]
)
def test_session_records_cannot_be_overwritten(session, filename):
    path = str(session.directory / filename)
    assert decision(session, "write_file", path=path) == "deny"
    assert decision(session, "read_file", path=path) == "allow"


def test_environment_script_requires_approval(session):
    path = str(session.directory / "env.sh")
    assert decision(session, "read_file", path=path) == "allow"
    assert decision(session, "write_file", path=path) == "ask"
    assert decision(session, "edit_file", path=path) == "ask"


def test_environment_symlink_target_still_requires_approval(session):
    target = session.cwd / "shared.sh"
    target.write_text("export EXAMPLE=1\n")
    (session.directory / "env.sh").symlink_to(target)
    assert decision(session, "write_file", path=str(target)) == "ask"


def test_resolved_paths_and_cwd_do_not_expand_scope(session, tmp_path):
    (session.cwd / "outside-link").symlink_to(tmp_path)
    assert decision(session, "write_file", path="../outside.txt") == "ask"
    assert decision(session, "write_file", path="outside-link/outside.txt") == "ask"
    session.cwd = tmp_path
    assert decision(session, "write_file", path="outside.txt") == "ask"
    assert decision(session, "write_file", path="project/inside.txt") == "allow"


@pytest.mark.parametrize(
    "command, expected",
    [
        ("pwd", "allow"),
        ("ls -la .", "allow"),
        ("cat new.py", "allow"),
        ("head -n 10 new.py", "allow"),
        ("tail -n 20 new.py", "allow"),
        ("rg -n 'hello world' .", "allow"),
        ("rg --files", "allow"),
        ("rg 'rm -rf' .", "allow"),
        ("git status --short", "allow"),
        ("git log --oneline -n 5", "allow"),
        ("git diff --no-ext-diff --no-textconv", "allow"),
        ("git show --no-ext-diff --no-textconv HEAD", "allow"),
        ("rm -rf build", "deny"),
        ("rm --recursive --force build", "deny"),
        ("rm -r -f build", "deny"),
        ("mkfs.ext4 /dev/example", "deny"),
        ("dd if=image of=/dev/example", "deny"),
        ("shutdown now", "deny"),
        ("reboot", "deny"),
        ("git reset --hard", "deny"),
        ("git clean -fd", "deny"),
        ("git push --force", "deny"),
        ("git push --force-with-lease=main", "deny"),
        ("pwd; rm -rf build", "deny"),
        ("pwd#not-a-comment; rm -rf build", "deny"),
        ("printf x > /etc/example", "deny"),
        ("rm file.txt", "ask"),
        ("python app.py", "ask"),
        ("uv sync", "ask"),
        ("curl https://example.com", "ask"),
        ("sudo pwd", "ask"),
        ("cat /etc/hosts", "ask"),
        ("rg --pre=python hello .", "ask"),
        ("ls --unexpected-option .", "ask"),
        ("cat new.py > copy.py", "ask"),
        ("cat new.py | head", "ask"),
        ("cat $(pwd)/new.py", "ask"),
        ("cat `pwd`/new.py", "ask"),
        ("cat *.py", "ask"),
        ("cat 'unterminated", "ask"),
        ("git -c alias.x=anything status", "ask"),
    ],
)
def test_shell_decisions(session, command, expected):
    assert decision(session, "bash", command=command) == expected


@pytest.mark.parametrize("operation", ["diff", "show"])
@pytest.mark.parametrize("flags", ["--no-ext-diff --no-textconv", "--no-textconv"])
def test_git_protection_flags_must_be_options(session, operation, flags):
    command = f"git {operation} --no-ext-diff -- file.txt {flags}"
    assert decision(session, "bash", command=command) == "ask"
    command = f"git {operation} --no-ext-diff --no-textconv -- file.txt"
    assert decision(session, "bash", command=command) == "allow"


def test_control_and_unknown_tools(session):
    assert decision(session, "stop_task", task_id="task") == "allow"
    assert decision(session, "send_input", task_id="task", text="anything") == "ask"
    assert decision(session, "future_tool") == "ask"


def test_internal_git_changes_need_approval(session):
    assert decision(session, "write_file", path=".git/config") == "ask"


def test_missing_arguments_are_not_automatically_allowed(session):
    assert decision(session, "bash") == "ask"
    assert decision(session, "write_file") == "ask"


@pytest.mark.parametrize("name, args", [
    ("write_file", {"path": "../outside.txt", "content": "allowed"}),
    ("write_file", {"path": "/etc/example", "content": "allowed"}),
    ("bash", {"command": "rm -rf build"}),
])
async def test_full_access_allows_ask_and_deny_without_approval(session, name, args):
    async def approve(tool_call, reason):
        pytest.fail("Full access must not request approval")

    permissions = Permissions(session, approve=approve, full_access=True)
    # 仅检查权限，不实际执行这些调用。
    assert await permissions.check({"name": name, "args": args, "id": "full-access"}) is None
    record = json.loads(session.transcript.read_text().splitlines()[-1])
    assert record["kind"] == "permission" and record["allowed"] is True
