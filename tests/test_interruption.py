"""真实图、进程和 CLI 的打断边界；模型与终端输入使用可控替身。"""

import asyncio
import json
import os
import signal
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk
from test_agent_cli import ScriptedModel, call, consume
from test_cli_permissions import cli_session
from test_subagents import AgentModel

from mini_harness.agent import Harness


async def wait_until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_interruption_saves_completed_running_and_unstarted_calls(tmp_path):
    calls = [
        call("write_file", {"path": "finished.txt", "content": "kept"}, "finished"),
        call("bash", {"command": "printf before; touch started; sleep 60"}, "running"),
        call("write_file", {"path": "unstarted.txt", "content": "no"}, "unstarted"),
    ]
    model = ScriptedModel(responses=[AIMessage(
        content="", tool_calls=[message.tool_calls[0] for message in calls],
    ), AIMessage(content="Inspected existing work")])
    async with Harness.open(tmp_path, tmp_path / "session", model, full_access=True) as harness:
        running = asyncio.create_task(consume(harness, "Run these steps"))
        try:
            await wait_until(lambda: (tmp_path / "started").exists())
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        snapshot = await harness.graph.aget_state(harness.config)
        results = {m.tool_call_id: m for m in snapshot.values["messages"]
                   if isinstance(m, ToolMessage)}
        assert set(results) == {"finished", "running", "unstarted"}
        assert results["finished"].status == "success"
        assert results["running"].status == results["unstarted"].status == "error"
        process = json.loads(results["running"].content)
        assert process["reason"] == "interrupted" and process["status"] == "exited"
        assert process["exit_code"] is not None
        assert Path(process["output_path"]).read_text() == "before"
        assert harness.session.get("processes")[process["task_id"]] == process
        assert "not run" in results["unstarted"].content
        assert snapshot.next == () and snapshot.values["pending_calls"] == []
        assert (tmp_path / "finished.txt").read_text() == "kept"
        assert not (tmp_path / "unstarted.txt").exists()
        assert len(model.seen) == 1
        assert await consume(harness, "Inspect, do not repeat") == "Inspected existing work"
        assert not (tmp_path / "unstarted.txt").exists()


async def test_canceling_wait_preserves_background_process_and_saves_result(tmp_path):
    model = ScriptedModel()
    async with Harness.open(tmp_path, tmp_path / "session", model) as harness:
        process = await harness.processes.run("sleep 60", run_in_background=True)
        model.responses.append(call("wait_task", {"task_id": process["task_id"]}, "wait"))
        running = asyncio.create_task(consume(harness, "Wait for the background task"))
        try:
            await wait_until(lambda: harness.session.get_execution("wait") is not None)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        assert harness.processes.tasks[process["task_id"]].process.returncode is None
        snapshot = await harness.graph.aget_state(harness.config)
        result = next(m for m in snapshot.values["messages"] if isinstance(m, ToolMessage))
        assert result.tool_call_id == "wait" and result.status == "error"
        assert snapshot.next == ()


async def test_cli_pauses_background_requests_until_new_chat(tmp_path, monkeypatch):
    entered, resumed = asyncio.Event(), asyncio.Event()

    async def reply(child, messages):
        if not entered.is_set():
            entered.set()
            await asyncio.Event().wait()
        resumed.set()
        return AIMessage(content="Continued with new instructions")

    model = AgentModel(handler=reply)
    async with cli_session(tmp_path, monkeypatch, model) as (harness, terminal, running):
        terminal.answers.put("Start work")
        await asyncio.wait_for(entered.wait(), 5)
        background = await harness.processes.run("printf completed", run_in_background=True)
        await harness.processes.tasks[background["task_id"]].watch_task
        os.kill(os.getpid(), signal.SIGINT)
        assert await terminal.prompt() == "> "
        terminal.answers.put("/tasks")
        assert await terminal.prompt() == "> "
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(resumed.wait(), 0.15)
        assert not harness.processes.completion_events.empty()
        terminal.answers.put("Continue with another approach")
        assert await terminal.prompt() == "> "
        assert resumed.is_set()
        latest = model.seen[-1][1]
        assert any(m.content == "Continue with another approach" for m in latest)
        assert any(isinstance(m, HumanMessage) and background["task_id"] in str(m.content)
                   for m in latest)
        terminal.answers.put("/exit")
        await asyncio.wait_for(running, 5)


async def test_second_ctrl_c_waits_for_foreground_cleanup(tmp_path, monkeypatch):
    cleaning, release = asyncio.Event(), asyncio.Event()
    model = ScriptedModel(responses=[call("bash", {
        "command": "touch started; sleep 60",
    }, "foreground")])
    async with cli_session(tmp_path, monkeypatch, model) as (harness, terminal, running):
        harness.permissions.session_trusted = True
        original = harness.processes._kill_group

        async def delayed_cleanup(task):
            cleaning.set()
            await release.wait()
            await original(task)

        monkeypatch.setattr(harness.processes, "_kill_group", delayed_cleanup)
        try:
            terminal.answers.put("Run the command")
            await wait_until(lambda: (tmp_path / "project" / "started").exists())
            os.kill(os.getpid(), signal.SIGINT)
            await asyncio.wait_for(cleaning.wait(), 5)
            os.kill(os.getpid(), signal.SIGINT)
            await asyncio.sleep(0.1)
            assert terminal.prompts.empty(), "Prompt appeared before process cleanup finished"
        finally:
            release.set()
        assert await terminal.prompt() == "> "
        assert all(task.process.returncode is not None for task in harness.processes.tasks.values())
        assert (await harness.graph.aget_state(harness.config)).next == ()
        terminal.answers.put("/exit")
        await asyncio.wait_for(running, 5)


async def test_interrupted_model_stream_never_executes_partial_tool_call(tmp_path):
    entered = asyncio.Event()

    class PartialModel(ScriptedModel):
        async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
            yield ChatGenerationChunk(message=AIMessageChunk(
                content="Preparing", tool_call_chunks=[{
                    "name": "write_file", "args": '{"path":"partial.txt","content":',
                    "id": "partial", "index": 0,
                }],
            ))
            entered.set()
            await asyncio.Event().wait()

    async with Harness.open(tmp_path, tmp_path / "session", PartialModel()) as harness:
        running = asyncio.create_task(consume(harness, "Write a file"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        snapshot = await harness.graph.aget_state(harness.config)
        assert snapshot.next == () and snapshot.values["pending_calls"] == []
        assert harness.session.get_execution("partial") is None
        assert not any(isinstance(m, AIMessage) for m in snapshot.values["messages"])
        assert not (tmp_path / "partial.txt").exists()


async def test_ctrl_c_during_short_file_write_keeps_complete_content(tmp_path, monkeypatch):
    target = tmp_path / "project" / "written.txt"
    content = "Complete contents\n" * 100
    model = ScriptedModel(responses=[call("write_file", {
        "path": str(target), "content": content,
    }, "write")])
    original = Path.write_text

    def write_with_interrupt(path, *args, **kwargs):
        if path == target:
            os.kill(os.getpid(), signal.SIGINT)
        return original(path, *args, **kwargs)

    async with cli_session(tmp_path, monkeypatch, model) as (harness, terminal, running):
        monkeypatch.setattr(Path, "write_text", write_with_interrupt)
        terminal.answers.put("Write the file")
        assert await terminal.prompt() == "> "
        assert target.read_text() == content
        snapshot = await harness.graph.aget_state(harness.config)
        assert snapshot.next == ()
        assert any(isinstance(m, ToolMessage) and m.tool_call_id == "write"
                   for m in snapshot.values["messages"])
        terminal.answers.put("/exit")
        await asyncio.wait_for(running, 5)
