"""终端空闲时也处理子审批和子结果；复用真实图和 stdin 测试边界。"""

import asyncio
import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from test_agent_cli import call
from test_cli_permissions import cli_session
from test_subagents import AgentModel


async def test_idle_child_approval_uses_the_same_cli_and_identifies_source(
    tmp_path, monkeypatch, capsys,
):
    release = asyncio.Event()
    outside = tmp_path / "outside.txt"

    async def reply(child, messages):
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if child:
            if not results:
                await release.wait()
                return call("write_file", {"path": str(outside), "content": "approved"}, "write")
            return AIMessage(content="CHILD VERIFIED")
        if not results:
            return call("spawn_agent", {"task": "Write the requested file"}, "spawn")
        if any(isinstance(m, HumanMessage) and "CHILD VERIFIED" in m.content for m in messages):
            return AIMessage(content="Parent received child result")
        return AIMessage(content="Parent can work independently")

    async with cli_session(tmp_path, monkeypatch, AgentModel(handler=reply)) as (
        harness, terminal, running,
    ):
        terminal.answers.put("Delegate this work")
        assert await terminal.prompt() == "> "
        agent_id = next(iter(harness.subagents.agents))
        release.set()
        async with asyncio.timeout(5):
            assert "Allow?" in await terminal.prompt()
        assert not outside.exists()
        terminal.answers.put("y")
        assert await terminal.prompt() == "> "  # 审批结束，子任务继续运行
        assert await terminal.prompt() == "> "  # 子结果触发的父轮次已经完成
        assert outside.read_text() == "approved"
        output = capsys.readouterr()
        assert agent_id in output.err
        assert "CHILD VERIFIED" not in output.out
        assert "Parent received child result" in output.out
        terminal.answers.put("/exit")
        await asyncio.wait_for(running, 5)


async def test_timeout_cancels_idle_approval_and_cli_accepts_new_chat(tmp_path, monkeypatch):
    release = asyncio.Event()
    outside = tmp_path / "not-approved.txt"
    parent_inputs = []

    async def reply(child, messages):
        if child:
            await release.wait()
            return call("write_file", {"path": str(outside), "content": "no"}, "write")
        parent_inputs.append(messages)
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if not results:
            return call("spawn_agent", {"task": "Wait for approval", "timeout": 0.6}, "spawn")
        return AIMessage(content="Parent ready")

    async with cli_session(tmp_path, monkeypatch, AgentModel(handler=reply)) as (
        harness, terminal, running,
    ):
        terminal.answers.put("Start the child")
        assert await terminal.prompt() == "> "
        release.set()
        async with asyncio.timeout(5):
            assert "Allow?" in await terminal.prompt()
        assert await terminal.prompt() == "> "  # 超时先解除审批等待
        assert await terminal.prompt() == "> "  # 清理完成后通知父轮次
        assert not outside.exists()
        agent_id = next(iter(harness.subagents.agents))
        assert harness.subagents.agents[agent_id]["status"] == "timed_out"
        assert any(
            isinstance(m, HumanMessage) and '"status": "timed_out"' in m.content
            for m in parent_inputs[-1]
        )
        terminal.answers.put("Continue the parent conversation")
        assert await terminal.prompt() == "> "
        assert any(m.content == "Continue the parent conversation" for m in parent_inputs[-1])
        terminal.answers.put("/exit")
        await asyncio.wait_for(running, 5)


async def test_child_approval_during_parent_wait_can_trust_the_shared_session(
    tmp_path, monkeypatch,
):
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"

    async def reply(child, messages):
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if child:
            if not results:
                return call("write_file", {"path": str(first), "content": "child"}, "child-write")
            return AIMessage(content="child done")
        if not results:
            return call("spawn_agent", {"task": "Write a file"}, "spawn")
        agent_id = json.loads(results[0].content)["agent_id"]
        if len(results) == 1:
            return call("wait_agent", {"agent_id": agent_id}, "wait")
        if len(results) == 2:
            return call("write_file", {"path": str(second), "content": "parent"}, "parent-write")
        return AIMessage(content="done")

    async with cli_session(tmp_path, monkeypatch, AgentModel(handler=reply)) as (
        harness, terminal, running,
    ):
        terminal.answers.put("Delegate and then write")
        assert "Allow?" in await terminal.prompt()
        terminal.answers.put("a")
        assert await terminal.prompt() == "> "
        assert first.read_text() == "child" and second.read_text() == "parent"
        assert harness.permissions.session_trusted
        terminal.answers.put("/exit")
        await asyncio.wait_for(running, 5)
