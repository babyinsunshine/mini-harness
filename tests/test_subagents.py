"""真实图、会话和进程；仅把外部模型请求替换为可控异步回复。"""

import asyncio
import inspect
import json
import os
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
from test_agent_cli import ScriptedModel, call, consume

from mini_harness.agent import Harness
from mini_harness.context.policy import ContextPolicy
from mini_harness.session import Session


class AgentModel(ScriptedModel):
    handler: Any
    bindings: list = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        names = {tool.name for tool in tools}
        self.bindings.append(names)
        return self.bind(is_child="update_plan" not in names)

    async def _agenerate(self, messages, stop=None, run_manager=None, is_child=False, **kwargs):
        self.seen.append((is_child, list(messages)))
        reply = self.handler(is_child, messages)
        if inspect.isawaitable(reply):
            reply = await reply
        return ChatResult(generations=[ChatGeneration(message=reply)])


async def test_child_has_own_locked_session_and_parent_identity(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    async def reply(child, messages):
        if child:
            started.set()
            await release.wait()
        return AIMessage(content="child result" if child else "parent result")

    model = AgentModel(handler=reply)
    async with Harness.open(tmp_path, tmp_path / "parent", model) as parent:
        await consume(parent, "Private parent history")
        agent = parent.subagents.spawn("Read the tests", timeout=5)
        await asyncio.wait_for(started.wait(), 5)
        directory = parent.session.directory / "subagents" / agent["agent_id"]
        with pytest.raises(RuntimeError, match="already open"):
            Session(directory)
        with pytest.raises(ValueError, match="running"):
            parent.subagents.continue_agent(agent["agent_id"], "Overlapping turn")
        child_messages = next(messages for child, messages in model.seen if child)
        assert "Private parent history" not in str(child_messages)
        assert str(parent.session.directory) in child_messages[0].content
        assert "parent" in child_messages[0].content
        child_tools = model.bindings[-1]
        assert {"bash", "read_file", "write_file", "edit_file"} <= child_tools
        assert not child_tools & {
            "update_plan", "spawn_agent", "wait_agent", "continue_agent", "close_agent",
        }
        release.set()
        result = await parent.subagents.wait(agent["agent_id"])
        assert result["status"] == "waiting" and result["result"] == "child result"
        with Session(directory) as child_session:
            assert child_session.get("parent_session_id") == "parent"
            assert child_session.get("parent_session_dir") == str(parent.session.directory)
            assert child_session.get("plan") is None


async def test_continue_reuses_history_and_close_keeps_records(tmp_path):
    model = AgentModel(handler=lambda child, messages: AIMessage(content="child reply"))
    async with Harness.open(tmp_path, tmp_path / "parent", model) as parent:
        agent = parent.subagents.spawn("First request", timeout=5)
        await parent.subagents.wait(agent["agent_id"])
        parent.subagents.continue_agent(agent["agent_id"], "Follow-up request")
        result = await parent.subagents.wait(agent["agent_id"])
        history = [messages for child, messages in model.seen if child][-1]
        assert [m.content for m in history if isinstance(m, HumanMessage)
                and not m.id.startswith("runtime-")] == ["First request", "Follow-up request"]
        assert any(isinstance(m, AIMessage) and m.content == "child reply" for m in history)
        assert result["timeout"] == 5
        closed = await parent.subagents.close_agent(agent["agent_id"])
        assert closed["status"] == "closed"
        assert (parent.session.directory / "subagents" / agent["agent_id"]
                / "transcript.jsonl").is_file()
        with pytest.raises(ValueError, match="closed"):
            parent.subagents.continue_agent(agent["agent_id"], "Cannot continue")


@pytest.mark.parametrize("trusted", [False, True])
async def test_child_permissions_use_parent_approval_and_trust(tmp_path, trusted):
    project, outside = tmp_path / "project", tmp_path / "outside.txt"
    project.mkdir()
    decisions = []

    async def approve(tool_call, reason):
        decisions.append(tool_call)
        return True

    def reply(child, messages):
        if not any(isinstance(m, ToolMessage) for m in messages):
            return call("write_file", {"path": str(outside), "content": "approved"}, "write")
        return AIMessage(content="finished")

    async with Harness.open(project, tmp_path / "parent", AgentModel(handler=reply),
                            approve=approve) as parent:
        parent.permissions.session_trusted = trusted
        agent = parent.subagents.spawn("Write the requested file")
        result = await parent.subagents.wait(agent["agent_id"])
        assert result["status"] == "waiting" and outside.read_text() == "approved"
        assert len(decisions) == (0 if trusted else 1)
        if decisions:
            assert decisions[0]["agent_id"] == agent["agent_id"]


async def test_child_inherits_fixed_project_root_and_keeps_hard_denials(tmp_path):
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir()
    outside.mkdir()

    def reply(child, messages):
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if not results:
            return call("write_file", {"path": "unapproved.txt", "content": "no"}, "outside")
        if len(results) == 1:
            return call("bash", {"command": "rm -rf -- ''"}, "dangerous")
        return AIMessage(content="denied")

    model = AgentModel(handler=reply)
    async with Harness.open(project, tmp_path / "parent", model) as parent:
        parent.session.cwd = outside
        agent = parent.subagents.spawn("Respect permissions")
        await parent.subagents.wait(agent["agent_id"])
        assert not (outside / "unapproved.txt").exists()
        history = model.seen[-1][1]
        assert all(m.status == "error" for m in history if isinstance(m, ToolMessage))
        directory = parent.session.directory / "subagents" / agent["agent_id"]
        with Session(directory) as session:
            assert session.get("project_root") == str(project)
            assert session.get_execution("outside") is None
            assert session.get_execution("dangerous") is None


async def test_timeout_kills_child_processes_and_continue_gets_a_new_deadline(tmp_path):
    waiting = asyncio.Event()
    block = asyncio.Event()

    async def reply(child, messages):
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if any(isinstance(m, HumanMessage) and m.content == "Continue after timeout"
               for m in messages):
            await asyncio.sleep(0.1)
            return AIMessage(content="continued")
        if not results:
            return call("bash", {"command": "echo $$ > child.pid; sleep 60",
                                 "run_in_background": True}, "sleep")
        waiting.set()
        await block.wait()

    async def approve(tool_call, reason):
        return True

    async with Harness.open(tmp_path, tmp_path / "parent", AgentModel(handler=reply),
                            approve=approve) as parent:
        agent = parent.subagents.spawn("Run a long command", timeout=1)
        await asyncio.wait_for(waiting.wait(), 5)
        pid = int((tmp_path / "child.pid").read_text())
        result = await parent.subagents.wait(agent["agent_id"])
        assert result["status"] == "timed_out"
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert parent.processes.drain_events()[-1]["status"] == "timed_out"
        parent.subagents.continue_agent(agent["agent_id"], "Continue after timeout", timeout=2)
        continued = await parent.subagents.wait(agent["agent_id"])
        assert continued["status"] == "waiting" and continued["result"] == "continued"


async def test_parent_tools_deliver_only_final_child_result_without_stream_leak(tmp_path):
    child_started, parent_worked = asyncio.Event(), asyncio.Event()

    async def reply(child, messages):
        if child:
            child_started.set()
            await parent_worked.wait()
            return AIMessage(content="CHILD FINAL")
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if not results:
            return call("spawn_agent", {"task": "Do child work"}, "spawn")
        agent_id = json.loads(results[0].content)["agent_id"]
        if len(results) == 1:
            await child_started.wait()
            parent_worked.set()
            return call("wait_agent", {"agent_id": agent_id}, "wait")
        assert "CHILD FINAL" in results[-1].content
        if len(results) == 2:
            return call("close_agent", {"agent_id": agent_id}, "close")
        return AIMessage(content="PARENT FINAL")

    async with Harness.open(tmp_path, tmp_path / "parent", AgentModel(handler=reply)) as parent:
        events = [event async for event in parent.events("Delegate")]
        assert "".join(e["text"] for e in events if e["type"] == "text") == "PARENT FINAL"
        assert next(e for e in events if e["type"] == "background")["task"]["result"] == (
            "CHILD FINAL"
        )


async def test_restart_resumes_same_child_conversation_without_replaying_tools(tmp_path):
    reached = asyncio.Event()

    async def first_reply(child, messages):
        if not child:
            return AIMessage(content="parent")
        if not any(isinstance(m, ToolMessage) for m in messages):
            return call("bash", {"command": "printf x >> once.txt; sleep 60"}, "interrupted")
        reached.set()
        await asyncio.Event().wait()

    async def approve(tool_call, reason):
        reached.set()
        return True

    directory = tmp_path / "parent"
    async with Harness.open(tmp_path, directory, AgentModel(handler=first_reply),
                            approve=approve) as parent:
        agent = parent.subagents.spawn("Original child task", timeout=10)
        async with asyncio.timeout(5):
            while not (tmp_path / "once.txt").exists():
                await asyncio.sleep(0.01)
    model = AgentModel(handler=lambda child, messages: AIMessage(
        content="child resumed" if child else "parent resumed"
    ))
    async with Harness.open(tmp_path, directory, model) as parent:
        assert await consume(parent, "Continue the project") == "parent resumed"
        result = await parent.subagents.wait(agent["agent_id"])
        assert result["status"] == "waiting" and result["result"] == "child resumed"
        child_history = next(messages for child, messages in model.seen if child)
        assert sum(m.content == "Original child task" for m in child_history) == 1
        errors = [m for m in child_history if isinstance(m, ToolMessage)]
        assert len(errors) == 1 and errors[0].status == "error"
        assert "interrupted" in errors[0].content.lower()
        assert (tmp_path / "once.txt").read_text() == "x"


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
async def test_invalid_timeout_does_not_create_a_child(tmp_path, timeout):
    model = AgentModel(handler=lambda child, messages: AIMessage(content="unused"))
    async with Harness.open(tmp_path, tmp_path / "parent", model) as parent:
        with pytest.raises(ValueError, match="timeout"):
            parent.subagents.spawn("Work", timeout=timeout)
        assert not list((parent.session.directory / "subagents").glob("*/state.sqlite"))


async def test_close_and_shutdown_can_cancel_a_child_before_it_starts(tmp_path):
    model = AgentModel(handler=lambda child, messages: AIMessage(content="unused"))
    async with Harness.open(tmp_path, tmp_path / "parent", model) as parent:
        closed = parent.subagents.spawn("Close immediately")
        result = await parent.subagents.close_agent(closed["agent_id"])
        assert result["status"] == "closed"
        interrupted = parent.subagents.spawn("Interrupt immediately")
        await parent.subagents.interrupt()
        assert parent.session.get("subagents")[interrupted["agent_id"]]["status"] == "interrupted"


async def test_failure_and_closed_state_survive_reopen(tmp_path):
    def reply(child, messages):
        raise RuntimeError("fixture provider failed")

    async with Harness.open(tmp_path, tmp_path / "parent", AgentModel(handler=reply)) as parent:
        agent = parent.subagents.spawn("Report failure")
        result = await parent.subagents.wait(agent["agent_id"])
        assert result["status"] == "failed" and "fixture provider failed" in result["result"]
        assert parent.processes.drain_events()[0]["status"] == "failed"
        await parent.subagents.close_agent(agent["agent_id"])
    model = AgentModel(handler=lambda child, messages: AIMessage(content="only parent"))
    async with Harness.open(tmp_path, tmp_path / "parent", model) as parent:
        await consume(parent, "Continue parent")
        assert (await parent.subagents.wait(agent["agent_id"]))["status"] == "closed"
        assert all(not child for child, _ in model.seen)


async def test_parent_can_review_continue_then_close_the_same_child(tmp_path):
    def reply(child, messages):
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if child:
            if any(m.content == "Implement the suggested fix" for m in messages):
                if not results:
                    return call("write_file", {"path": "fix.txt", "content": "verified"}, "fix")
                return AIMessage(content="Fix implemented and verified")
            return AIMessage(content="A fix is needed")
        if not results:
            return call("spawn_agent", {"task": "Review the issue"}, "spawn")
        agent_id = json.loads(results[0].content)["agent_id"]
        if len(results) in {1, 3}:
            return call("wait_agent", {"agent_id": agent_id}, f"wait-{len(results)}")
        if len(results) == 2:
            assert json.loads(results[-1].content)["result"] == "A fix is needed"
            return call("continue_agent", {
                "agent_id": agent_id, "message": "Implement the suggested fix",
            }, "continue")
        if len(results) == 4:
            assert json.loads(results[-1].content)["result"] == "Fix implemented and verified"
            return call("close_agent", {"agent_id": agent_id}, "close")
        return AIMessage(content="Reviewed and finished")

    async with Harness.open(tmp_path, tmp_path / "parent", AgentModel(handler=reply)) as parent:
        assert await consume(parent, "Delegate review and implementation") == (
            "Reviewed and finished"
        )
        assert (tmp_path / "fix.txt").read_text() == "verified"
        assert len(parent.subagents.agents) == 1
        assert next(iter(parent.subagents.agents.values()))["status"] == "closed"


async def test_saved_child_results_remain_discoverable_after_compaction_and_reopen(tmp_path):
    directory = tmp_path / "parent"
    model = AgentModel(handler=lambda child, messages: AIMessage(content="Saved child finding"))
    async with Harness.open(tmp_path, directory, model, ContextPolicy(keep_rounds=0)) as parent:
        agent = parent.subagents.spawn("Inspect the project")
        await parent.subagents.wait(agent["agent_id"])
        await consume(parent, "Remember this delegated work")
        await parent.compact()
    resumed = AgentModel(handler=lambda child, messages: AIMessage(content="Parent continues"))
    async with Harness.open(tmp_path, directory, resumed) as parent:
        await consume(parent, "Continue")
        runtime = next(m for m in reversed(resumed.seen[-1][1])
                       if isinstance(m, HumanMessage) and m.id.startswith("runtime-"))
        assert agent["agent_id"] in runtime.content and "waiting" in runtime.content
        saved = await parent.subagents.wait(agent["agent_id"])
        assert saved["result"] == "Saved child finding"
        assert all(not child for child, _ in resumed.seen)


async def test_two_children_and_parent_can_wait_for_model_requests_independently(tmp_path):
    both_started, release = asyncio.Event(), asyncio.Event()
    children = []

    async def reply(child, messages):
        if child:
            children.append(messages)
            if len(children) == 2:
                both_started.set()
            await release.wait()
        return AIMessage(content="child done" if child else "parent still works")

    async with Harness.open(tmp_path, tmp_path / "parent", AgentModel(handler=reply)) as parent:
        first = parent.subagents.spawn("Inspect code")
        second = parent.subagents.spawn("Inspect tests")
        await asyncio.wait_for(both_started.wait(), 5)
        assert await consume(parent, "Continue independent work") == "parent still works"
        assert first["session_dir"] != second["session_dir"]
        release.set()
        results = await asyncio.gather(
            parent.subagents.wait(first["agent_id"]), parent.subagents.wait(second["agent_id"]),
        )
        assert all(result["status"] == "waiting" for result in results)
