import asyncio
from dataclasses import replace

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from mini_harness.session import Session


class SummaryModel:
    """Only the external model call is substituted; all context handling is real."""

    def __init__(self, response="summary: keep user intent and current task", error=None):
        self.response = response
        self.error = error
        self.inputs = []

    async def ainvoke(self, messages, **kwargs):
        self.inputs.append(messages)
        if self.error:
            raise self.error
        return AIMessage(content=self.response)


def history():
    return [
        HumanMessage(content="Do not remove JWT support", id="u1"),
        AIMessage(
            content="",
            id="a1",
            tool_calls=[
                {"name": "bash", "args": {}, "id": "t1"},
                {"name": "read_file", "args": {}, "id": "t2"},
            ],
        ),
        ToolMessage(content="old output " * 200, tool_call_id="t1", id="r1"),
        ToolMessage(content="other old output " * 200, tool_call_id="t2", id="r2"),
        AIMessage(content="Keep both auth methods", id="a2"),
        HumanMessage(content="Now add tests", id="u2"),
    ]


def policy(**changes):
    from mini_harness.context import ContextPolicy

    return replace(
        ContextPolicy(
            window=12000,
            reserve=1000,
            auto_buffer=1000,
            block_buffer=200,
            keep_rounds=2,
            micro_trigger=100000,
        ),
        **changes,
    )


async def test_micro_preserves_recent_result_and_original_transcript(tmp_path):
    from mini_harness.context import ContextMiddleware

    with Session(tmp_path / "s", tmp_path) as session:
        model = SummaryModel()
        context = ContextMiddleware(session, model, policy(micro_trigger=1, micro_keep=1))
        original = history()
        result = await context.prepare(original)
        assert result[2].tool_call_id == "t1"
        assert "cleared" in result[2].content
        assert "old output " * 200 == original[2].content
        assert result[3].content == original[3].content
        assert "old output old output" in session.transcript.read_text()
        assert not model.inputs


async def test_compact_keeps_whole_tool_round_and_records_originals(tmp_path):
    from mini_harness.context import ContextMiddleware

    with Session(tmp_path / "s", tmp_path) as session:
        context = ContextMiddleware(session, SummaryModel(), policy(keep_rounds=3))
        result = await context.compact(history())
        assert "summary:" in result[0].content
        assert [m.id for m in result[1:]] == ["a1", "r1", "r2", "a2", "u2"]
        assert "Do not remove JWT support" in session.transcript.read_text()


async def test_empty_summary_does_not_change_input_or_commit_boundary(tmp_path):
    from mini_harness.context import ContextMiddleware

    with Session(tmp_path / "s", tmp_path) as session:
        context = ContextMiddleware(session, SummaryModel(response=""), policy())
        messages = history()
        with pytest.raises(ValueError, match="empty"):
            await context.compact(messages)
        assert messages[0].content == "Do not remove JWT support"
        assert session.get("context_generation", 0) == 0


async def test_notes_reuse_preserves_every_uncovered_message(tmp_path):
    from mini_harness.context import ContextMiddleware

    with Session(tmp_path / "s", tmp_path) as session:
        model = SummaryModel()
        context = ContextMiddleware(session, model, policy(notes=True, keep_rounds=1))
        messages = history()
        session.set("notes", {"text": "Prior task decisions", "covered_until": "r2"})
        result = await context.compact(messages)
        assert "Prior task decisions" in result[0].content
        assert [m.id for m in result[1:]] == ["a2", "u2"]
        assert not model.inputs


async def test_background_notes_cannot_overwrite_newer_context(tmp_path):
    from mini_harness.context import ContextMiddleware

    entered, release = asyncio.Event(), asyncio.Event()

    class SlowModel(SummaryModel):
        async def ainvoke(self, messages, **kwargs):
            entered.set()
            await release.wait()
            return AIMessage(content="outdated notes")

    with Session(tmp_path / "s", tmp_path) as session:
        context = ContextMiddleware(session, SlowModel(), policy(notes=True))
        pending = asyncio.create_task(context.update_notes(history()))
        await entered.wait()
        session.set("context_generation", 1)
        release.set()
        await pending
        assert session.get("notes") is None


async def test_partial_compact_preserves_outside_segment(tmp_path):
    from mini_harness.context import ContextMiddleware

    with Session(tmp_path / "s", tmp_path) as session:
        context = ContextMiddleware(session, SummaryModel(), policy())
        result = await context.compact(history(), start=1, end=2)
        assert result[0].id == "u1"
        assert "summary:" in result[1].content
        assert [m.id for m in result[2:]] == ["a2", "u2"]


async def test_summary_request_is_bounded_and_failure_breaker_stops_calls(tmp_path):
    from mini_harness.context import ContextMiddleware

    with Session(tmp_path / "s", tmp_path) as session:
        model = SummaryModel(error=RuntimeError("service unavailable"))
        context = ContextMiddleware(session, model, policy(window=3000, auto_buffer=500))
        messages = [HumanMessage(content="x" * 9000, id="huge")]
        for _ in range(5):
            with pytest.raises(ValueError, match="budget"):
                await context.prepare(messages)
        assert len(model.inputs) <= 3
        assert session.get("compact_failures") == 3


async def test_reactive_recovery_returns_persistent_message_override(tmp_path):
    from mini_harness.context import ContextMiddleware

    with Session(tmp_path / "s", tmp_path) as session:
        context = ContextMiddleware(session, SummaryModel(), policy())
        request = ModelRequest(
            model=SummaryModel(), messages=history(), state={"messages": history()}, tools=[]
        )
        attempts = []

        async def handler(req):
            attempts.append(req.messages)
            if len(attempts) == 1:
                raise RuntimeError("prompt_too_long")
            return ModelResponse(result=[AIMessage(content="recovered", id="done")])

        result = await context.awrap_model_call(request, handler)
        assert len(attempts) == 2
        persisted = result.command.update["messages"]
        assert any("summary:" in str(m.content) for m in persisted)
        assert persisted[-1].content == "recovered"


async def test_ordinary_model_error_is_not_retried(tmp_path):
    from mini_harness.context import ContextMiddleware

    with Session(tmp_path / "s", tmp_path) as session:
        model = SummaryModel()
        context = ContextMiddleware(session, model, policy())
        request = ModelRequest(
            model=model, messages=history(), state={"messages": history()}, tools=[]
        )

        async def handler(req):
            raise RuntimeError("authentication failed")

        with pytest.raises(RuntimeError, match="authentication"):
            await context.awrap_model_call(request, handler)
        assert not model.inputs


async def test_compaction_rereads_recent_file_and_respects_restoration_budget(tmp_path):
    from mini_harness.context import ContextMiddleware
    from mini_harness.tools import read_file

    target = tmp_path / "current.py"
    target.write_text("old version\n")
    with Session(tmp_path / "s", tmp_path) as session:
        read_file(session, "current.py")
        target.write_text("new version\n")
        context = ContextMiddleware(session, SummaryModel(), policy())
        result = await context.compact(history())
        assert any(
            "current.py" in str(m.content) and "new version" in str(m.content) for m in result
        )
        assert context.tokens(result) < context.policy.auto_limit


async def test_disabling_auto_keeps_manual_compact_available(tmp_path):
    from mini_harness.context import ContextMiddleware

    with Session(tmp_path / "s", tmp_path) as session:
        model = SummaryModel()
        context = ContextMiddleware(session, model, policy(auto=False))
        messages = [HumanMessage(content="x" * 30000, id="u")]
        result = await context.prepare(messages)
        assert result == messages
        assert not model.inputs
        await context.compact(messages)
        assert model.inputs
