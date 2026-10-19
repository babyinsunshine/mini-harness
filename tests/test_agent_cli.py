import asyncio
import json
import shlex
import subprocess
import sys

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, message_to_dict
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from mini_harness.agent import Harness
from mini_harness.cli.config import build_parser, resolve_settings
from mini_harness.cli.main import main
from mini_harness.context.policy import ContextPolicy
from mini_harness.session import Session


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


async def allow_test_request(tool_call, reason):
    """Explicit approval for this suite's harmless environment and process fixtures."""
    return True


def test_cli_error_includes_underlying_connection_cause(tmp_path, monkeypatch, capsys):
    async def fail(args, directory):
        try:
            raise OSError("proxy cannot resolve host")
        except OSError as cause:
            raise ConnectionError("Connection error.") from cause

    monkeypatch.setattr("mini_harness.cli.main.run_cli", fail)
    monkeypatch.setattr(sys, "argv", [
        "mini-harness", "--model", "openai:unused", "--session-dir", str(tmp_path), "-p", "Hi"
    ])
    with pytest.raises(SystemExit) as stopped:
        main()
    assert stopped.value.code == 1
    error = capsys.readouterr().err
    assert "proxy cannot resolve host" in error and "Traceback" not in error


@pytest.mark.parametrize("approved", [True, False])
async def test_approval_precedes_execution(tmp_path, approved):
    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "outside.txt"
    model = ScriptedModel(responses=[
        call("write_file", {"path": str(target), "content": "approved"}, "outside-write"),
        AIMessage(content="Checked result"),
    ])
    decisions = []

    async def approve(tool_call, reason):
        assert harness.session.get_execution(tool_call["id"]) is None
        assert not target.exists()
        decisions.append(tool_call["id"])
        return approved

    async with Harness.open(project, tmp_path / "session", model, approve=approve) as harness:
        await consume(harness, "Write the specified file")
        result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
        assert decisions == ["outside-write"]
        assert result.tool_call_id == "outside-write"
        assert target.exists() == approved
        if approved:
            assert target.read_text() == "approved"
            assert harness.session.get_execution("outside-write")[0] == "done"
        else:
            assert result.status == "error"
            assert harness.session.get_execution("outside-write") is None
        assert '"kind": "permission"' in harness.session.transcript.read_text()


async def test_denied_write_has_no_execution(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "outside.txt"
    model = ScriptedModel(responses=[
        call("write_file", {"path": str(target), "content": "unapproved"}, "denied-write"),
        AIMessage(content="Permission was denied"),
    ])
    async with Harness.open(project, tmp_path / "session", model) as harness:
        await consume(harness, "Try writing outside")
        assert not target.exists()
        assert harness.session.get_execution("denied-write") is None
        result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
        assert result.tool_call_id == "denied-write"
        assert result.status == "error"
        assert "not executed" in result.content.lower()


async def test_hard_denial_cannot_be_approved(tmp_path):
    directory = tmp_path / "session"
    model = ScriptedModel(responses=[
        call(
            "write_file", {"path": str(directory / "transcript.jsonl"), "content": "corrupt"}, "p"
        ),
        AIMessage(content="Kept session records"),
    ])

    async def approve(tool_call, reason):
        raise AssertionError("A forbidden action must not be offered for approval")

    async with Harness.open(tmp_path, directory, model, approve=approve) as harness:
        await consume(harness, "Keep the original user request")
        assert harness.session.get_execution("p") is None
        assert "Keep the original user request" in harness.session.transcript.read_text()
        assert next(m for m in model.seen[-1] if isinstance(m, ToolMessage)).status == "error"


async def test_compaction_does_not_reread_external_file_without_new_approval(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("approved original content")
    model = ScriptedModel(responses=[
        call("read_file", {"path": str(target)}, "outside-read"), AIMessage(content="Read it"),
    ])
    async with Harness.open(
        project, tmp_path / "session", model, approve=allow_test_request
    ) as harness:
        await consume(harness, "Read the external file once")
        target.write_text("changed after approval")
        messages = await harness.compact()
        assert all("changed after approval" not in str(message.content) for message in messages)


async def test_real_graph_runs_four_tools_and_reopens_compressed_session(tmp_path):
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


async def test_model_can_edit_advertised_environment_file(tmp_path):
    directory = tmp_path / "session space 中文"
    env_file = directory / "env.sh"
    model = ScriptedModel(
        responses=[
            call("read_file", {"path": str(env_file)}, "env-read"),
            call(
                "write_file",
                {"path": str(env_file), "content": "export HARNESS_MODEL_ENV=first\n"},
                "env-write",
            ),
            call("bash", {"command": 'printf %s "$HARNESS_MODEL_ENV"'}, "env-bash-1"),
            call(
                "edit_file",
                {"path": str(env_file), "old_text": "first", "new_text": "second"},
                "env-edit",
            ),
            call("bash", {"command": 'printf %s "$HARNESS_MODEL_ENV"'}, "env-bash-2"),
            AIMessage(content="Environment updated"),
        ]
    )
    async with Harness.open(tmp_path, directory, model, approve=allow_test_request) as harness:
        assert await consume(harness, "Configure a reusable environment variable") == (
            "Environment updated"
        )
        state = await harness.graph.aget_state(harness.config)
        results = {
            m.tool_call_id: m for m in state.values["messages"] if isinstance(m, ToolMessage)
        }
        assert all(m.status == "success" for m in results.values())
        assert json.loads(results["env-bash-1"].content)["preview"] == "first"
        assert json.loads(results["env-bash-2"].content)["preview"] == "second"
    for request in model.seen:
        assert str(env_file) in request[0].content
        assert "sourced" in request[0].content
        assert sum(str(m.content).startswith("[Runtime context]") for m in request) == 1


async def test_runtime_changes_and_resume_preserve_system_and_history_prefix(tmp_path):
    project = tmp_path / "project"
    target = project / "nested directory"
    target.mkdir(parents=True)
    instructions = project / "AGENTS.md"
    instructions.write_text("Run pytest before finishing.\n")
    directory = tmp_path / "session"
    model = ScriptedModel(
        responses=[
            call("bash", {"command": f"cd {shlex.quote(str(target))}"}, "change-cwd"),
            call(
                "write_file",
                {"path": str(instructions), "content": "Run pytest -q before finishing.\n"},
                "change-instructions",
            ),
            AIMessage(content="Updated"),
        ]
    )
    async with Harness.open(project, directory, model, approve=allow_test_request) as harness:
        assert await consume(harness, "Update the project") == "Updated"
    first, second, third = model.seen
    assert first[0].content == second[0].content == third[0].content
    assert "Run pytest" not in first[0].content
    assert f"Current directory: {target}" in second[-1].content
    assert "Run pytest -q" in third[-1].content
    for previous, current in zip(model.seen, model.seen[1:]):
        assert [m.model_dump() for m in current[: len(previous)]] == [
            m.model_dump() for m in previous
        ]
    resumed = ScriptedModel(responses=[AIMessage(content="Continuing")])
    async with Harness.open(project, directory, resumed) as harness:
        assert await consume(harness, "continue") == "Continuing"
    request = resumed.seen[0]
    assert request[0].content == first[0].content
    assert any(
        f"Current directory: {target}" in m.content and "Run pytest -q" in m.content
        for m in request
        if isinstance(m.content, str)
    )


async def test_full_compaction_restores_runtime_context_without_changing_system(tmp_path):
    instructions = tmp_path / "AGENTS.md"
    instructions.write_text("Project rule: preserve the public API.\n")
    model = ScriptedModel(responses=[AIMessage(content="Ready"), AIMessage(content="Continuing")])
    async with Harness.open(
        tmp_path, tmp_path / "session", model, ContextPolicy(keep_rounds=0)
    ) as harness:
        await consume(harness, "Keep JWT support")
        await harness.compact()
        await consume(harness, "continue")
    ordinary = [m for m in model.seen if "Summarize the supplied" not in m[0].content]
    assert ordinary[0][0].content == ordinary[1][0].content
    runtime = [m for m in ordinary[1] if str(m.content).startswith("[Runtime context]")]
    assert len(runtime) == 1
    assert f"Current directory: {tmp_path}" in runtime[0].content
    assert "preserve the public API" in runtime[0].content


async def test_recovery_repairs_pending_calls_without_replaying_tools(tmp_path):
    directory = tmp_path / "session"
    async with Harness.open(tmp_path, directory, ScriptedModel()) as harness:
        await harness.graph.aupdate_state(
            harness.config,
            {
                "messages": [
                    HumanMessage(content="Run the commands", id="user"),
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "bash",
                                "args": {"command": "touch repeated-done"},
                                "id": "old-done",
                            },
                            {
                                "name": "bash",
                                "args": {"command": "touch repeated-error"},
                                "id": "old-error",
                            },
                            {
                                "name": "bash",
                                "args": {"command": "touch repeated-interrupted"},
                                "id": "old-started",
                            },
                        ],
                    ),
                ]
            },
        )
        harness.session.start_execution("old-done")
        harness.session.finish_execution(
            "old-done",
            message_to_dict(
                ToolMessage(
                    content="previous command completed", tool_call_id="old-done", id="old-result"
                )
            ),
        )
        harness.session.start_execution("old-error")
        harness.session.finish_execution(
            "old-error",
            message_to_dict(
                ToolMessage(
                    content="previous command failed", tool_call_id="old-error", status="error"
                )
            ),
        )
        harness.session.start_execution("old-started")
    model = ScriptedModel(responses=[AIMessage(content="Inspect previous results")])

    async def forbid_reapproval(tool_call, reason):
        raise AssertionError("Recovered calls must not request approval or run again")

    async with Harness.open(tmp_path, directory, model, approve=forbid_reapproval) as harness:
        assert harness.session.get_execution("old-started")[0] == "error"
        assert harness.session.get_execution("old-error")[0] == "error"
        await consume(harness, "continue")
        state = await harness.graph.aget_state(harness.config)
        results = {
            m.tool_call_id: m for m in state.values["messages"] if isinstance(m, ToolMessage)
        }
        assert results["old-done"].content == "previous command completed"
        assert results["old-error"].content == "previous command failed"
        assert results["old-error"].status == "error"
        assert results["old-started"].status == "error"
        assert "interrupted" in results["old-started"].content.lower()
    assert not list(tmp_path.glob("repeated-*"))


async def test_cancelled_tool_is_recorded_as_error_and_recovered(tmp_path):
    marker = tmp_path / "started"
    model = ScriptedModel(
        responses=[
            call("bash", {"command": "touch started; sleep 1000"}, "cancelled"),
            AIMessage(content="Inspect interrupted work"),
        ]
    )
    async with Harness.open(
        tmp_path, tmp_path / "s", model, approve=allow_test_request
    ) as harness:
        running = asyncio.create_task(consume(harness, "Start the command"))
        try:
            async with asyncio.timeout(5):
                while not marker.exists():
                    await asyncio.sleep(0.01)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        saved = harness.session.get_execution("cancelled")
        assert saved[0] == "error"
        assert saved[1]["data"]["status"] == "error"
        assert "interrupted" in saved[1]["data"]["content"].lower()
        await consume(harness, "Check the interrupted command")
    assert any(
        isinstance(m, ToolMessage) and m.tool_call_id == "cancelled" and m.status == "error"
        for m in model.seen[-1]
    )


async def test_pending_call_repaired_without_replaying_side_effects(tmp_path):
    model = ScriptedModel(responses=[AIMessage(content="Will inspect logs")])
    async with Harness.open(
        tmp_path, tmp_path / "s", model, approve=allow_test_request
    ) as harness:
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
    (tmp_path / "data.txt").write_text("Original output " + "x" * 5000)
    model = ScriptedModel(
        responses=[
            *[call("read_file", {"path": "data.txt"}, f"read-{i}") for i in range(4)],
            AIMessage(content="Read files"),
            RuntimeError("prompt_too_long"),
            AIMessage(content="Recovered", usage_metadata={
                "input_tokens": 700, "output_tokens": 1, "total_tokens": 701,
            }),
        ]
    )
    (tmp_path / "AGENTS.md").write_text("Project rule: keep the API stable.\n")
    # A real user may quote an internal marker; its source must still be recognized.
    latest = "[Runtime context] is a string in my code. Keep JWT support."
    async with Harness.open(tmp_path, tmp_path / "s", model) as harness:
        await consume(harness, "Read data four times")
        assert await consume(harness, latest) == "Recovered"
        state = await harness.graph.aget_state(harness.config)
        messages = state.values["messages"]
        assert "omitted" in messages[0].content
        assert messages[-1].content == "Recovered"
        assert any(m.content == latest for m in messages)
        assert any(
            "[Runtime context]" in m.content and "keep the API stable" in m.content
            for m in messages
        )
        results = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
        calls = {call["id"] for m in messages if isinstance(m, AIMessage) for call in m.tool_calls}
        assert results == calls
        assert "read-0" not in results
        assert "read-3" in results
        assert len(model.seen) == 7  # Five initial requests, rejection, one shorter retry.
        assert harness.context.estimate_input_tokens(model.seen[-1][1:]) == 700
        assert "read-0" in harness.session.transcript.read_text()
    resumed = ScriptedModel(responses=[AIMessage(content="Continued")])
    async with Harness.open(tmp_path, tmp_path / "s", resumed) as harness:
        await consume(harness, "continue")
    assert resumed.seen[0][1:-1] == messages


async def test_background_completion_is_delivered_on_next_turn(tmp_path):
    model = ScriptedModel(responses=[AIMessage(content="Observed completion")])
    async with Harness.open(tmp_path, tmp_path / "s", model) as harness:
        result = await harness.processes.run("printf completed", run_in_background=True)
        await harness.processes.tasks[result["task_id"]].watch_task
        await consume(harness, "status?")
        assert any(
            "completed" in str(m.content) and "background" in str(m.content).lower()
            for m in model.seen[0]
        )


async def test_idle_micro_persists_real_tool_results_across_graph_reopens(tmp_path, monkeypatch):
    (tmp_path / "output.txt").write_text("Important output " + "x" * 4000)
    directory = tmp_path / "session"
    clock = [1000]
    monkeypatch.setattr("mini_harness.context.manager.time", lambda: clock[0], raising=False)
    policy = ContextPolicy(micro_keep_tokens=0, micro_min_savings=50000)
    model = ScriptedModel(
        responses=[
            *[call("read_file", {"path": "output.txt"}, f"read-{i}") for i in range(4)],
            AIMessage(content="Read all outputs"),
        ]
    )
    async with Harness.open(tmp_path, directory, model, policy) as harness:
        await consume(harness, "Read the file four times")
        state = await harness.graph.aget_state(harness.config)
        original = [m for m in state.values["messages"] if isinstance(m, ToolMessage)]
        assert all("Important output" in m.content for m in original)

    clock[0] = 1300
    resumed = ScriptedModel(responses=[AIMessage(content="Continued")])
    async with Harness.open(tmp_path, directory, resumed, policy) as harness:
        await consume(harness, "continue")
        state = await harness.graph.aget_state(harness.config)
        trimmed = [m for m in state.values["messages"] if isinstance(m, ToolMessage)]
        assert all("cleared" in m.content for m in trimmed[:2])
        assert trimmed[-2:] == original[-2:]
        assert [m.tool_call_id for m in trimmed] == [m.tool_call_id for m in original]
        assert harness.session.get("last_model_request_at") == 1300

    clock[0] = 1310
    again = ScriptedModel(responses=[AIMessage(content="Still continuing")])
    async with Harness.open(tmp_path, directory, again, policy) as harness:
        await consume(harness, "continue again")
    assert [m for m in again.seen[0] if isinstance(m, ToolMessage)] == trimmed
    assert model.seen[0][0].content == resumed.seen[0][0].content == again.seen[0][0].content


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


def test_resume_restores_model_window_with_explicit_overrides(tmp_path):
    directory = tmp_path / "s"
    with Session(directory, tmp_path) as session:
        session.set(
            "config",
            {
                "model": "openai:custom",
                "context_window": 64000,
                "notes": True,
                "micro_keep_tokens": 32000,
                "micro_idle_seconds": 600,
            },
        )
    args = build_parser().parse_args(
        ["--resume", "s", "--output-reserve", "4096", "--micro-idle-seconds", "300"]
    )
    resolve_settings(args, directory)
    assert args.model == "openai:custom"
    assert args.context_window == 64000
    assert args.output_reserve == 4096
    # Legacy session settings must not re-enable the removed background feature.
    assert not hasattr(args, "notes")
    assert args.micro_keep_tokens == 32000
    assert args.micro_idle_seconds == 300
    assert args.max_steps == 1000
    with Session(directory) as session:
        session.set("config", {"max_steps": 200})
    args = build_parser().parse_args(["--resume", "s"])
    resolve_settings(args, directory)
    assert args.max_steps == 200
    args = build_parser().parse_args(["--resume", "s", "--max-steps", "600"])
    resolve_settings(args, directory)
    assert args.max_steps == 600
