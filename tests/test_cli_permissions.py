"""Real graph and files; only terminal input and the external model are substituted."""


import asyncio
import os
import queue
import signal
import sys
import threading
from contextlib import asynccontextmanager

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from test_agent_cli import ScriptedModel, call

from mini_harness.agent import Harness
from mini_harness.cli.interactive import run_interactive


class Terminal:
    def __init__(self):
        self.prompts = queue.Queue()
        self.answers = queue.Queue()
        self.output = sys.stderr

    def write(self, text):
        self.output.write(text)
        if text == "> " or text.startswith("Allow?"):
            self.prompts.put(text)
        return len(text)

    def flush(self):
        self.output.flush()

    def input(self):
        line = self.answers.get(timeout=10)
        if line is None:
            raise EOFError
        return line

    async def prompt(self):
        return await asyncio.to_thread(self.prompts.get, True, 10)


@asynccontextmanager
async def cli_session(tmp_path, monkeypatch, model):
    project = tmp_path / "project"
    project.mkdir()
    terminal = Terminal()
    monkeypatch.setattr("builtins.input", terminal.input)
    monkeypatch.setattr("sys.stderr", terminal)
    async with Harness.open(project, tmp_path / "session", model) as harness:
        running = asyncio.create_task(run_interactive(harness, tmp_path))
        try:
            assert await terminal.prompt() == "> "
            yield harness, terminal, running
        finally:
            terminal.answers.put(None)
            running.cancel()
            await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 5)


def write_call(target, call_id="write"):
    return call("write_file", {"path": str(target), "content": "approved"}, call_id)


@pytest.mark.parametrize("completion_ready", [False, True])
async def test_canceling_idle_cli_leaves_background_notifications_available(
    tmp_path, monkeypatch, completion_ready,
):
    ready = asyncio.Event()
    original = asyncio.wait

    async def pause_after_completion(tasks, **kwargs):
        result = await original(tasks, **kwargs)
        if len(tasks) == 3:  # 暂停在真实队列已取出通知、主循环尚未处理的位置。
            ready.set()
            await asyncio.Event().wait()
        return result

    if completion_ready:
        monkeypatch.setattr(asyncio, "wait", pause_after_completion)
    async with cli_session(tmp_path, monkeypatch, ScriptedModel()) as (harness, _, running):
        if completion_ready:
            task = await harness.processes.run("printf completed", run_in_background=True)
            await harness.processes.tasks[task["task_id"]].watch_task
            await asyncio.wait_for(ready.wait(), 5)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        if not completion_ready:
            task = await harness.processes.run("printf completed", run_in_background=True)
            await harness.processes.tasks[task["task_id"]].watch_task
        event = await asyncio.wait_for(harness.processes.completion_events.get(), 0.5)
        assert event["task_id"] == task["task_id"]


@pytest.mark.parametrize("answer, exists", [("y", True), ("yes", True), ("n", False), ("", False)])
async def test_cli_approval(tmp_path, monkeypatch, answer, exists):
    target = tmp_path / "outside.txt"
    model = ScriptedModel(responses=[write_call(target), AIMessage(content="Reviewed")])
    async with cli_session(tmp_path, monkeypatch, model) as (harness, terminal, running):
        terminal.answers.put("Write the file")
        assert "Allow?" in await terminal.prompt()
        assert not target.exists()
        assert harness.session.get_execution("write") is None
        terminal.answers.put(answer)
        assert await terminal.prompt() == "> "
        assert target.exists() == exists
        terminal.answers.put("/exit")
        await asyncio.wait_for(running, 5)


async def test_sequential_approvals(tmp_path, monkeypatch):
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    calls = [write_call(first, "first").tool_calls[0], write_call(second, "second").tool_calls[0]]
    model = ScriptedModel(
        responses=[AIMessage(content="", tool_calls=calls), AIMessage(content="Done")]
    )
    async with cli_session(tmp_path, monkeypatch, model) as (_, terminal, running):
        terminal.answers.put("Write both")
        assert "Allow?" in await terminal.prompt()
        terminal.answers.put("y")
        assert "Allow?" in await terminal.prompt()
        terminal.answers.put("n")
        assert await terminal.prompt() == "> "
        assert sum(path.exists() for path in (first, second)) == 1
        terminal.answers.put("/exit")
        await asyncio.wait_for(running, 5)


async def test_cli_session_trust_is_transient_and_preserves_denials(tmp_path, monkeypatch):
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    calls = [
        write_call(first, "first").tool_calls[0],
        call("bash", {"command": "printf trusted"}, "run").tool_calls[0],
        call("bash", {"command": "rm -rf -- ''"}, "denied").tool_calls[0],
    ]
    model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=calls), AIMessage(content="Done"),
        write_call(second, "second"), AIMessage(content="Done again"),
    ])
    async with cli_session(tmp_path, monkeypatch, model) as (harness, terminal, running):
        terminal.answers.put("Do the work")
        assert "Allow" in await terminal.prompt()
        terminal.answers.put("a")
        assert await terminal.prompt() == "> "
        assert first.read_text() == "approved"
        assert harness.session.get_execution("run")[0] == "done"
        assert harness.session.get_execution("denied") is None
        terminal.answers.put("Continue")
        assert await terminal.prompt() == "> "
        assert second.read_text() == "approved"
        terminal.answers.put("/exit")
        await asyncio.wait_for(running, 5)

    outside = tmp_path / "after-reopen.txt"
    resumed = ScriptedModel(responses=[write_call(outside), AIMessage(content="Checked")])
    async with Harness.open(tmp_path / "project", tmp_path / "session", resumed) as harness:
        _ = [chunk async for chunk in harness.stream("Try the next file")]
        assert not outside.exists()
        assert harness.session.get_execution("write") is None


@pytest.mark.parametrize("answer", [None, "/exit", "/quit"])
async def test_exit_pending_approval(tmp_path, monkeypatch, answer):
    target = tmp_path / "outside.txt"
    model = ScriptedModel(responses=[write_call(target), AIMessage(content="Unused")])
    async with cli_session(tmp_path, monkeypatch, model) as (harness, terminal, running):
        terminal.answers.put("Write")
        assert "Allow?" in await terminal.prompt()
        terminal.answers.put(answer)
        await asyncio.wait_for(running, 5)
        assert not target.exists()
        assert harness.session.get_execution("write") is None
        assert harness.permissions.approve is None


async def test_cancel_pending_approval(tmp_path, monkeypatch, capsys):
    target = tmp_path / "outside.txt"
    model = ScriptedModel(responses=[write_call(target), AIMessage(content="Continued")])
    async with cli_session(tmp_path, monkeypatch, model) as (harness, terminal, running):
        terminal.answers.put("Write")
        assert "Allow?" in await terminal.prompt()
        os.kill(os.getpid(), signal.SIGINT)
        async with asyncio.timeout(5):
            while "Interrupted" not in capsys.readouterr().out:
                await asyncio.sleep(0.01)
        terminal.answers.put("Continue after cancellation")
        assert await terminal.prompt() == "> "
        async with asyncio.timeout(5):
            while len(model.seen) < 2:
                await asyncio.sleep(0.01)
        assert await terminal.prompt() == "> "
        assert not target.exists()
        assert harness.session.get_execution("write") is None
        assert any(isinstance(m, ToolMessage) and m.status == "error" for m in model.seen[-1])
        terminal.answers.put("/exit")
        await asyncio.wait_for(running, 5)


@pytest.mark.parametrize("queued_input", ["yes", "a"])
async def test_buffered_chat_is_not_approval(tmp_path, monkeypatch, queued_input):
    target = tmp_path / "outside.txt"
    entered, release = threading.Event(), threading.Event()
    original = ScriptedModel._generate

    def generate(self, *args, **kwargs):
        if len(self.seen) == 1:
            entered.set()
            assert release.wait(5)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ScriptedModel, "_generate", generate)
    model = ScriptedModel(responses=[
        AIMessage(content="Ready"), write_call(target),
        AIMessage(content="Denied"), AIMessage(content="Heard queued chat"),
    ])
    async with cli_session(tmp_path, monkeypatch, model) as (harness, terminal, running):
        try:
            terminal.answers.put("Hello")
            assert await terminal.prompt() == "> "
            task = await harness.processes.run("printf done", run_in_background=True)
            await harness.processes.tasks[task["task_id"]].watch_task
            assert await asyncio.to_thread(entered.wait, 5)
            terminal.answers.put(queued_input)  # Chat typed before any approval prompt.
            await asyncio.sleep(0.05)
            release.set()
            assert "Allow?" in await terminal.prompt()
            terminal.answers.put("n")
            assert await terminal.prompt() == "> "
            assert not target.exists()
            assert any(
                isinstance(m, HumanMessage) and m.content == queued_input for m in model.seen[-1]
            )
            terminal.answers.put("/exit")
            await asyncio.wait_for(running, 5)
        finally:
            release.set()


@pytest.mark.parametrize("queued_input", ["yes", "a"])
async def test_foreground_typeahead_is_not_approval(tmp_path, monkeypatch, queued_input):
    target = tmp_path / "outside.txt"
    entered, release = threading.Event(), threading.Event()
    original = ScriptedModel._generate

    def generate(self, *args, **kwargs):
        if not self.seen:
            entered.set()
            assert release.wait(5)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ScriptedModel, "_generate", generate)
    model = ScriptedModel(responses=[
        write_call(target), AIMessage(content="Denied"), AIMessage(content="Heard queued chat"),
    ])
    async with cli_session(tmp_path, monkeypatch, model) as (_, terminal, running):
        try:
            terminal.answers.put("Write")
            assert await asyncio.to_thread(entered.wait, 5)
            terminal.answers.put(queued_input)  # Chat typed while the model is still thinking.
            await asyncio.sleep(0.05)
            release.set()
            assert "Allow?" in await terminal.prompt()
            terminal.answers.put("n")
            assert await terminal.prompt() == "> "
            assert not target.exists()
            assert any(
                isinstance(m, HumanMessage) and m.content == queued_input for m in model.seen[-1]
            )
            terminal.answers.put("/exit")
            await asyncio.wait_for(running, 5)
        finally:
            release.set()
