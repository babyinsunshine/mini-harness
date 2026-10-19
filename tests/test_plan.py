import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from test_agent_cli import ScriptedModel, call, consume
from test_context import SummaryModel, policy, tool_history

from mini_harness.agent import Harness
from mini_harness.cli.display import format_event
from mini_harness.context.manager import ContextManager
from mini_harness.session import Session
from mini_harness.tools.factory import create_tools


async def test_model_replaces_current_plan_without_approval_and_cli_displays_progress(tmp_path):
    initial = [{"step": "Inspect code", "status": "in_progress"}]
    updated = [
        {"step": "Inspect code", "status": "completed"},
        {"step": "Implement the change", "status": "in_progress"},
        {"step": "Run tests", "status": "pending"},
    ]
    model = ScriptedModel(responses=[
        call("update_plan", {"plan": initial}, "create"),
        call("update_plan", {"plan": updated, "explanation": "Add validation"}, "update"),
        AIMessage(content="Ready to implement"),
    ])
    async with Harness.open(tmp_path, tmp_path / "session", model) as harness:
        events = [event async for event in harness.events("Plan this change")]
        assert harness.session.get("plan") == {
            "plan": updated, "explanation": "Add validation"
        }
        updates = [
            json.loads(line) for line in harness.session.transcript.read_text().splitlines()
            if json.loads(line)["kind"] == "plan"
        ]
        assert [entry["plan"] for entry in updates] == [initial, updated]
        results = [event for event in events if event["type"] == "tool_end"]
        assert [event["status"] for event in results] == ["success", "success"]
        rendered = format_event(results[-1])
        assert "1/3" in rendered and "Add validation" in rendered
        assert "[x] Inspect code" in rendered
        assert "[>] Implement the change" in rendered
        assert "[ ] Run tests" in rendered
    for previous, current in zip(model.seen, model.seen[1:]):
        assert current[:len(previous)] == previous


@pytest.mark.parametrize("invalid", [
    [{"step": "Test", "status": "unknown"}],
    [{"step": "", "status": "pending"}],
    [{"step": "   ", "status": "pending"}],
    [
        {"step": "Implement", "status": "in_progress"},
        {"step": "Test", "status": "in_progress"},
    ],
])
async def test_invalid_update_returns_an_error_without_replacing_saved_plan(tmp_path, invalid):
    original = [{"step": "Inspect the API", "status": "pending"}]
    model = ScriptedModel(responses=[
        call("update_plan", {"plan": original}, "valid"),
        call("update_plan", {"plan": invalid}, "invalid"),
        AIMessage(content="The original plan is unchanged"),
    ])
    async with Harness.open(tmp_path, tmp_path / "session", model) as harness:
        await consume(harness, "Track this task")
        assert harness.session.get("plan")["plan"] == original
        result = next(
            message for message in model.seen[-1]
            if isinstance(message, ToolMessage) and message.tool_call_id == "invalid"
        )
        assert result.status == "error"


@pytest.mark.parametrize("mode", ["micro", "auto", "manual", "partial"])
async def test_compaction_restores_latest_plan_and_does_not_repeat_unchanged_context(
    tmp_path, mode
):
    with Session(tmp_path / "session", tmp_path) as session:
        tools = {tool.name: tool for tool in create_tools(session, None)}
        await tools["update_plan"].ainvoke({
            "plan": [{"step": "Obsolete step", "status": "pending"}]
        })
        settings = policy(
            keep_rounds=0, auto=mode == "auto", micro_keep_rounds=0, micro_keep_tokens=0,
            micro_min_savings=1000000 if mode == "auto" else 0,
        )
        model = SummaryModel()
        context = ContextManager(session, model, settings)
        messages = context.with_runtime_context(tool_history(counts=(1,) * 4, size=5000))
        await tools["update_plan"].ainvoke({
            "plan": [{"step": "Verify exported API", "status": "in_progress"}]
        })
        if mode == "auto":
            input_tokens = settings.auto_limit + 1
            context.observe_usage(messages, AIMessage(content="", usage_metadata={
                "input_tokens": input_tokens, "output_tokens": 1, "total_tokens": input_tokens + 1
            }))
        if mode in {"micro", "auto"}:
            result = await context.prepare_messages(messages)
        else:
            bounds = {"start_group": 0, "end_group": 2} if mode == "partial" else {}
            result = await context.compact(messages, **bounds)
        runtime = [m for m in result if str(m.content).startswith("[Runtime context]")]
        assert "Verify exported API" in runtime[-1].content
        assert "in_progress" in runtime[-1].content
        assert "Obsolete step" not in runtime[-1].content
        if mode != "micro":
            assert model.inputs
        else:
            assert any("cleared" in m.content for m in result if isinstance(m, ToolMessage))
        prepared = await context.prepare_messages(result)
        assert [m for m in prepared if m in runtime] == runtime
        assert sum(str(m.content).startswith("[Runtime context]") for m in prepared) == len(runtime)


async def test_overflow_retry_keeps_current_plan_visible_to_the_model(tmp_path):
    with Session(tmp_path / "session", tmp_path) as session:
        tools = {tool.name: tool for tool in create_tools(session, None)}
        await tools["update_plan"].ainvoke({
            "plan": [{"step": "Verify exported API", "status": "in_progress"}]
        })
        context = ContextManager(session, SummaryModel(), policy())
        messages = context.with_runtime_context(tool_history(counts=(1,) * 4, size=9000))
        attempts = []

        async def invoke_model(request):
            attempts.append(request)
            if len(attempts) == 1:
                raise RuntimeError("prompt_too_long")
            return AIMessage(content="Recovered")

        reply, retained = await context.request_model(messages, invoke_model)
        assert reply.content == "Recovered" and len(attempts) == 2
        assert len(retained) < len(messages)
        assert any("Verify exported API" in str(m.content) for m in attempts[-1])


async def test_plan_survives_reopening_and_empty_update_clears_it(tmp_path):
    directory = tmp_path / "session"
    model = ScriptedModel(responses=[
        call("update_plan", {
            "plan": [{"step": "Verify exported API", "status": "pending"}]
        }, "create"),
        AIMessage(content="Plan saved"),
    ])
    async with Harness.open(tmp_path, directory, model, policy(keep_rounds=0)) as harness:
        await consume(harness, "Track this task")
        await harness.compact()
    resumed = ScriptedModel(responses=[
        call("update_plan", {"plan": []}, "clear"), AIMessage(content="Plan cleared")
    ])
    async with Harness.open(tmp_path, directory, resumed, policy(keep_rounds=0)) as harness:
        await consume(harness, "Cancel this plan")
        assert any("Verify exported API" in str(m.content) for m in resumed.seen[0])
        assert harness.session.get("plan")["plan"] == []
        compacted = await harness.compact()
        runtime = [m for m in compacted if str(m.content).startswith("[Runtime context]")]
        assert "Verify exported API" not in runtime[-1].content
        assert '"plan": []' in runtime[-1].content
    other = ScriptedModel(responses=[AIMessage(content="New session")])
    async with Harness.open(tmp_path, tmp_path / "other", other) as harness:
        await consume(harness, "Start a different task")
        assert harness.session.get("plan") is None
        assert all("Verify exported API" not in str(m.content) for m in other.seen[0])
