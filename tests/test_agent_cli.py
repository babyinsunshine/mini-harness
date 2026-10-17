import subprocess
import sys

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, message_to_dict
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field


class ScriptedModel(BaseChatModel):
    responses: list = Field(default_factory=list)
    seen: list = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "scripted-offline-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        if "Summarize the supplied" in str(messages[0].content):
            response = AIMessage(content="User asked to keep JWT and add OAuth. Tests pending.")
        else:
            response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return ChatResult(generations=[ChatGeneration(message=response)])


def call(name, args, call_id):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


async def consume(harness, text):
    return "".join([chunk async for chunk in harness.stream(text)])


async def test_real_graph_runs_four_tools_and_reopens_compressed_session(tmp_path):
    from mini_harness.agent import Harness
    from mini_harness.context import ContextPolicy

    directory = tmp_path / "session"
    model = ScriptedModel(
        responses=[
            call("write_file", {"path": "answer.txt", "content": "before\n"}, "w1"),
            call("read_file", {"path": "answer.txt"}, "r1"),
            call(
                "edit_file", {"path": "answer.txt", "old_text": "before", "new_text": "after"}, "e1"
            ),
            call("bash", {"command": "cat answer.txt"}, "b1"),
            AIMessage(content="Verified after"),
        ]
    )
    async with Harness.open(tmp_path, directory, model, ContextPolicy()) as harness:
        assert await consume(harness, "Keep JWT and add OAuth") == "Verified after"
        assert (tmp_path / "answer.txt").read_text() == "after\n"
        await harness.compact()
        snapshot = await harness.graph.aget_state(harness.config)
        assert any("continuation summary" in str(m.content) for m in snapshot.values["messages"])
    resumed = ScriptedModel(responses=[AIMessage(content="Continuing tests")])
    async with Harness.open(tmp_path, directory, resumed, ContextPolicy()) as harness:
        assert await consume(harness, "continue") == "Continuing tests"
    assert any("JWT" in str(m.content) for m in resumed.seen[0])
    assert "Keep JWT and add OAuth" in (directory / "transcript.jsonl").read_text()


async def test_completed_tool_result_is_reused_and_unknown_command_not_repeated(tmp_path):
    from mini_harness.agent import Harness

    directory = tmp_path / "session"
    model = ScriptedModel(
        responses=[
            call("bash", {"command": "echo wrong > should-not-exist"}, "old-done"),
            call("bash", {"command": "echo wrong > unknown-should-not-exist"}, "old-started"),
            AIMessage(content="Inspect previous results"),
        ]
    )
    async with Harness.open(tmp_path, directory, model) as harness:
        harness.session.start_execution("old-done")
        harness.session.finish_execution(
            "old-done",
            message_to_dict(
                ToolMessage(
                    content="previous command completed", tool_call_id="old-done", id="old-result"
                )
            ),
        )
        harness.session.start_execution("old-started")
        await consume(harness, "continue")
        state = await harness.graph.aget_state(harness.config)
        results = [m.content for m in state.values["messages"] if isinstance(m, ToolMessage)]
        assert "previous command completed" in results
        assert any("unknown" in str(text).lower() for text in results)
    assert not (tmp_path / "should-not-exist").exists()
    assert not (tmp_path / "unknown-should-not-exist").exists()


async def test_pending_call_repaired_without_replaying_side_effects(tmp_path):
    from mini_harness.agent import Harness

    model = ScriptedModel(responses=[AIMessage(content="Will inspect logs")])
    async with Harness.open(tmp_path, tmp_path / "s", model) as harness:
        await harness.graph.aupdate_state(
            harness.config,
            {
                "messages": [
                    HumanMessage(content="run it", id="u"),
                    call("bash", {"command": "touch repeated"}, "pending"),
                ]
            },
        )
        harness.session.start_execution("pending")
        await consume(harness, "check what happened")
    assert not (tmp_path / "repeated").exists()
    assert any(isinstance(m, ToolMessage) for m in model.seen[0])


async def test_reactive_recovery_survives_actual_graph_checkpoint(tmp_path):
    from mini_harness.agent import Harness

    model = ScriptedModel(
        responses=[RuntimeError("prompt_too_long"), AIMessage(content="Recovered")]
    )
    async with Harness.open(tmp_path, tmp_path / "s", model) as harness:
        assert await consume(harness, "Keep JWT support") == "Recovered"
        state = await harness.graph.aget_state(harness.config)
        messages = state.values["messages"]
        assert "continuation summary" in messages[0].content
        assert messages[-1].content == "Recovered"


async def test_background_completion_is_delivered_on_next_turn(tmp_path):
    from mini_harness.agent import Harness

    model = ScriptedModel(responses=[AIMessage(content="Observed completion")])
    async with Harness.open(tmp_path, tmp_path / "s", model) as harness:
        result = await harness.processes.run("printf completed", run_in_background=True)
        await harness.processes.tasks[result["task_id"]].watcher
        await consume(harness, "status?")
        assert any(
            "completed" in str(m.content) and "background" in str(m.content).lower()
            for m in model.seen[0]
        )


def test_cli_help_and_missing_model_config(tmp_path):
    command = [sys.executable, "-m", "mini_harness"]
    result = subprocess.run([*command, "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "--resume" in result.stdout
    import os

    environment = dict(os.environ)
    environment.pop("HARNESS_MODEL", None)
    result = subprocess.run(
        [*command, "--session-dir", str(tmp_path), "-p", "hello"],
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 2
    assert "--model" in result.stderr


def test_resume_restores_model_window_and_env_file_with_explicit_overrides(tmp_path):
    from mini_harness.cli import parser, resolve_settings
    from mini_harness.session import Session

    directory = tmp_path / "s"
    with Session(directory, tmp_path) as session:
        session.set(
            "config",
            {
                "model": "openai:custom",
                "context_window": 64000,
                "env_file": "/tmp/saved-env.sh",
                "notes": True,
            },
        )
    args = parser().parse_args(["--resume", "s", "--output-reserve", "4096"])
    resolve_settings(args, directory)
    assert args.model == "openai:custom"
    assert args.context_window == 64000
    assert args.output_reserve == 4096
    assert str(args.env_file) == "/tmp/saved-env.sh"
    assert args.notes is True


def test_env_file_path_is_saved_independently_of_future_launch_directory(tmp_path, monkeypatch):
    from mini_harness.cli import parser, resolve_settings

    monkeypatch.chdir(tmp_path)
    args = parser().parse_args(["--env-file", "env.sh"])
    resolve_settings(args, tmp_path / "new-session")
    assert args.env_file == tmp_path / "env.sh"
