import asyncio
import json
import shlex

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from test_agent_cli import ScriptedModel, call, consume
from test_cli_permissions import Terminal
from test_subagents import AgentModel

from mini_harness.agent import Harness
from mini_harness.cli.config import build_parser, resolve_settings
from mini_harness.cli.interactive import run_interactive
from mini_harness.sandbox import Sandbox
from mini_harness.session import Session
from mini_harness.tools.files import write_file


@pytest.fixture(autouse=True)
def policy_only_runtime(monkeypatch):
    # These tests exercise graph/approval behavior. Real bwrap is tested separately.
    monkeypatch.setattr(Sandbox, "require_runtime", lambda self: "bwrap")


@pytest.fixture
def paths(tmp_path):
    project, shared, config = tmp_path / "project", tmp_path / "shared", tmp_path / "policy.json"
    project.mkdir()
    shared.mkdir()
    config.write_text(json.dumps({"allow_bypass": True}))
    return project, shared, config


def request_access(shared, call_id="grant"):
    return call("request_sandbox_access", {"paths": [str(shared)], "reason": "Update shared code"},
                call_id)


@pytest.mark.parametrize("mode", ["standard", "full-access", "session-trust"])
async def test_grant_requires_explicit_approval_and_is_revoked_on_restart(paths, tmp_path, mode):
    project, shared, config = paths
    target = shared / "new.txt"
    approvals = []

    async def approve(tool_call, reason):
        assert tool_call["sandbox_request"] is True
        assert str(shared) in reason
        assert not target.exists()
        approvals.append(tool_call["name"])
        return True

    model = ScriptedModel(responses=[
        call("write_file", {"path": str(target), "content": "blocked"}, "before"),
        request_access(shared),
        call("write_file", {"path": str(target), "content": "approved"}, "after"),
        AIMessage(content="Done"),
    ])
    directory = tmp_path / "session"
    async with Harness.open(project, directory, model, sandbox=True, sandbox_config=config,
                            full_access=mode == "full-access", approve=approve) as harness:
        harness.permissions.session_trusted = mode == "session-trust"
        await consume(harness, "Update shared code")
        results = {m.tool_call_id: m for m in model.seen[-1] if isinstance(m, ToolMessage)}
        assert results["before"].status == "error"
        assert results["after"].status == "success"
        assert target.read_text() == "approved"
        assert approvals == ["request_sandbox_access"]

    resumed_target = shared / "after-restart.txt"
    resumed = ScriptedModel(responses=[
        call("write_file", {"path": str(resumed_target), "content": "blocked"}, "restart"),
        AIMessage(content="Need approval again"),
    ])
    async with Harness.open(project, directory, resumed, sandbox=True, full_access=True) as harness:
        await consume(harness, "Continue")
        assert harness.session.sandbox.extra_write_paths == set()
        assert not resumed_target.exists()


@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("full_access", [False, True])
async def test_bypass_is_explicit_and_only_affects_one_call(paths, tmp_path, approved, full_access):
    project, shared, config = paths
    target = shared / "bypass.txt"
    later = shared / "later.txt"
    approvals = []

    async def approve(tool_call, reason):
        approvals.append(tool_call["name"])
        assert tool_call["sandbox_request"] and "unsandboxed" in reason.lower()
        return approved

    model = ScriptedModel(responses=[
        call("bash", {"command": f"printf approved > {shlex.quote(str(target))}",
                      "bypass_sandbox": True, "reason": "One-time external update"}, "bypass"),
        call("write_file", {"path": str(later), "content": "blocked"}, "later"),
        AIMessage(content="Done"),
    ])
    async with Harness.open(project, tmp_path / "session", model, sandbox=True,
                            sandbox_config=config, full_access=full_access,
                            approve=approve) as harness:
        await consume(harness, "Run one command")
        assert target.exists() == approved
        assert not later.exists()
        assert harness.session.sandbox.extra_write_paths == set()
    assert approvals == ["bash"]


async def test_approved_bypass_can_read_a_denied_path_in_standard_mode(paths, tmp_path):
    project, shared, config = paths
    secret = shared / "private.txt"
    secret.write_text("PRIVATE TEST FIXTURE")
    config.write_text(json.dumps({"allow_bypass": True, "deny_paths": [str(secret)]}))

    async def approve(tool_call, reason):
        return True

    model = ScriptedModel(responses=[
        call("bash", {"command": f"cat {shlex.quote(str(secret))}", "bypass_sandbox": True,
                      "reason": "User-approved read"}, "bypass"),
        AIMessage(content="Done"),
    ])
    async with Harness.open(project, tmp_path / "session", model, sandbox=True,
                            sandbox_config=config, approve=approve) as harness:
        await consume(harness, "Read with explicit approval")
    result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
    assert result.status == "success" and "PRIVATE TEST FIXTURE" in result.text


@pytest.mark.parametrize("kind", [
    "missing-callback", "disabled-grant", "disabled-bypass", "reason",
])
async def test_invalid_or_unavailable_requests_never_run(paths, tmp_path, kind):
    project, shared, config = paths
    approvals = []

    async def approve(tool_call, reason):
        approvals.append(tool_call)
        return True

    request = request_access(shared)
    if kind == "disabled-grant":
        config.write_text(json.dumps({"allow_write_requests": False}))
    elif kind == "disabled-bypass":
        config.write_text("{}")
        request = call("bash", {"command": "true", "bypass_sandbox": True,
                                "reason": "Testing"}, "bypass")
    elif kind == "reason":
        request.tool_calls[0]["args"]["reason"] = ""
    model = ScriptedModel(responses=[request, AIMessage(content="Denied")])
    async with Harness.open(project, tmp_path / "session", model, sandbox=True,
                            sandbox_config=config, full_access=True,
                            approve=None if kind == "missing-callback" else approve) as harness:
        await consume(harness, "Request permission")
        result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
        assert result.status == "error"
        assert harness.session.sandbox.extra_write_paths == set()
    assert approvals == []


async def test_child_grants_are_shared_with_parent(paths, tmp_path):
    project, shared, config = paths
    approvals = []

    async def approve(tool_call, reason):
        approvals.append(tool_call)
        return True

    def reply(child, messages):
        if child and not any(isinstance(m, ToolMessage) for m in messages):
            return request_access(shared)
        return AIMessage(content="Child done")

    async with Harness.open(project, tmp_path / "parent", AgentModel(handler=reply), sandbox=True,
                            sandbox_config=config, full_access=True, approve=approve) as parent:
        agent = parent.subagents.spawn("Request access to shared code", timeout=10)
        result = await parent.subagents.wait(agent["agent_id"])
        assert result["status"] == "waiting"
        assert approvals[0]["agent_id"] == agent["agent_id"]
        write_file(parent.session, str(shared / "parent.txt"), "shared grant")
        assert (shared / "parent.txt").read_text() == "shared grant"


@pytest.mark.parametrize("bypass", [False, True])
async def test_saved_approved_call_is_not_replayed_after_restart(paths, tmp_path, bypass):
    project, shared, config = paths
    target = shared / "must-not-execute.txt"
    request = request_access(shared)
    if bypass:
        request = call("bash", {"command": f"touch {shlex.quote(str(target))}",
                                "bypass_sandbox": True, "reason": "Testing"}, "bypass")
    directory = tmp_path / "session"
    async with Harness.open(project, directory, ScriptedModel(), sandbox=True,
                            sandbox_config=config, full_access=True) as harness:
        await harness.graph.aupdate_state(harness.config, {
            "messages": [request], "pending_calls": request.tool_calls, "approved": True,
        }, as_node="permission")
    model = ScriptedModel(responses=[AIMessage(content="Resumed")])
    async with Harness.open(project, directory, model, sandbox=True, sandbox_config=config,
                            full_access=True) as harness:
        await consume(harness, "Continue")
        assert not target.exists()
        assert harness.session.sandbox.extra_write_paths == set()
        assert harness.session.get_execution(request.tool_calls[0]["id"]) is None
        assert any(isinstance(m, ToolMessage) and m.status == "error" for m in model.seen[0])


@pytest.mark.parametrize("answer, allowed", [("a", False), ("y", True)])
async def test_cli_special_approval_cannot_enable_general_session_trust(paths, tmp_path,
                                                                     monkeypatch, answer, allowed):
    project, shared, config = paths
    terminal = Terminal()
    monkeypatch.setattr("builtins.input", terminal.input)
    monkeypatch.setattr("sys.stderr", terminal)
    model = ScriptedModel(responses=[request_access(shared), AIMessage(content="Done")])
    async with Harness.open(project, tmp_path / "session", model, sandbox=True,
                            sandbox_config=config, full_access=True) as harness:
        running = asyncio.create_task(run_interactive(harness, tmp_path))
        try:
            assert await terminal.prompt() == "> "
            terminal.answers.put("Request access")
            prompt = await terminal.prompt()
            assert "Allow?" in prompt and "trust session" not in prompt
            terminal.answers.put(answer)
            assert await terminal.prompt() == "> "
            assert bool(harness.session.sandbox.extra_write_paths) == allowed
            assert harness.permissions.session_trusted is False
        finally:
            terminal.answers.put("/exit")
            await asyncio.wait_for(running, 5)


def test_cli_sandbox_settings_resume_and_explicit_disable(paths, tmp_path):
    project, _, config = paths
    parser = build_parser()
    args = parser.parse_args(["--sandbox", "--sandbox-config", str(config)])
    resolve_settings(args, tmp_path)
    assert args.sandbox is True and args.sandbox_config == str(config)
    directory = tmp_path / "saved"
    with Session(directory, project) as session:
        session.set("config", {"sandbox": True, "sandbox_config": str(config)})
    resumed = parser.parse_args(["--resume", "saved"])
    resolve_settings(resumed, directory)
    assert resumed.sandbox is True
    disabled = parser.parse_args(["--resume", "saved", "--no-sandbox"])
    resolve_settings(disabled, directory)
    assert disabled.sandbox is False


async def test_changing_symlink_during_approval_cannot_change_granted_directory(paths, tmp_path):
    project, shared, config = paths
    hidden = tmp_path / "not-shown"
    hidden.mkdir()
    alias = project / "link"
    alias.symlink_to(shared, target_is_directory=True)

    async def approve(tool_call, reason):
        assert str(shared) in reason and str(hidden) not in reason
        alias.unlink()
        alias.symlink_to(hidden, target_is_directory=True)
        return True

    model = ScriptedModel(responses=[request_access(alias), AIMessage(content="Done")])
    async with Harness.open(project, tmp_path / "session", model, sandbox=True,
                            sandbox_config=config, full_access=True, approve=approve) as harness:
        await consume(harness, "Request the linked directory")
        assert shared in harness.session.sandbox.extra_write_paths
        assert hidden not in harness.session.sandbox.extra_write_paths


async def test_approved_canonical_directory_replaced_with_symlink_is_rejected(paths, tmp_path):
    project, shared, config = paths
    hidden = tmp_path / "not-shown"
    hidden.mkdir()

    async def approve(tool_call, reason):
        shared.rename(tmp_path / "old-shared")
        shared.symlink_to(hidden, target_is_directory=True)
        return True

    model = ScriptedModel(responses=[request_access(shared), AIMessage(content="Done")])
    async with Harness.open(project, tmp_path / "session", model, sandbox=True,
                            sandbox_config=config, full_access=True, approve=approve) as harness:
        await consume(harness, "Request directory")
        assert harness.session.sandbox.extra_write_paths == set()
        result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
        assert result.status == "error"
