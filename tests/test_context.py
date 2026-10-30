from dataclasses import replace
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from mini_harness.context.compact import estimate_tokens, micro_compact, reactive_compact
from mini_harness.context.manager import ContextManager
from mini_harness.context.policy import ContextPolicy
from mini_harness.session import Session
from mini_harness.tools.files import read_file


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


@pytest.mark.parametrize("metadata", [{"finish_reason": "length"}, {"stop_reason": "max_tokens"}])
async def test_truncated_summary_does_not_replace_context(tmp_path, metadata):
    class TruncatedModel(SummaryModel):
        async def ainvoke(self, messages, **kwargs):
            return AIMessage(content="Partial summary", response_metadata=metadata)

    with Session(tmp_path / "s", tmp_path) as session:
        context = ContextManager(session, TruncatedModel(), policy())
        messages = history()
        with pytest.raises(ValueError, match="truncated"):
            await context.compact(messages)
        assert messages[0].content == "Do not remove JWT support"


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
    return replace(
        ContextPolicy(
            window=12000,
            reserve=1000,
            auto_buffer=1000,
            block_buffer=200,
            keep_rounds=2,
        ),
        **changes,
    )


@pytest.mark.parametrize("auto", [True, False])
async def test_auto_and_blocking_follow_provider_input_usage(tmp_path, auto):
    with Session(tmp_path / "s", tmp_path) as session:
        model = SummaryModel()
        context = ContextManager(session, model, policy(auto=auto))
        messages = context.with_runtime_context(history())
        actual = context.policy.block_limit + 1
        context.observe_usage(messages, AIMessage(content="Done", usage_metadata={
            "input_tokens": actual, "output_tokens": 1, "total_tokens": actual + 1,
        }))
        if auto:
            result = await context.prepare_messages(messages)
            assert model.inputs
            assert context.estimate_input_tokens(result) < context.policy.auto_limit
        else:
            with pytest.raises(ValueError, match="request budget"):
                await context.prepare_messages(messages)


def test_missing_usage_returns_to_local_estimation(tmp_path):
    with Session(tmp_path / "s", tmp_path) as session:
        context = ContextManager(session, SummaryModel(), policy())
        messages = history()
        context.observe_usage(messages, AIMessage(content="Done", usage_metadata={
            "input_tokens": 9000, "output_tokens": 1, "total_tokens": 9001,
        }))
        context.observe_usage(messages, AIMessage(content="No usage"))
        fresh = ContextManager(session, SummaryModel(), policy())
        assert context.estimate_input_tokens(messages) == fresh.estimate_input_tokens(messages)


def tool_history(counts=(1,) * 8, size=1800):
    messages = [HumanMessage(content="Fix the project", id="request")]
    for index, count in enumerate(counts):
        calls = [
            {"name": "bash", "args": {}, "id": f"call-{index}-{part}"}
            for part in range(count)
        ]
        messages.append(AIMessage(content="", id=f"assistant-{index}", tool_calls=calls))
        messages.extend(
            ToolMessage(
                content=f"output-{index}-{part}:" + "x" * size,
                tool_call_id=call["id"],
                id=f"result-{index}-{part}",
            )
            for part, call in enumerate(calls)
        )
    return messages


def test_reactive_keeps_only_latest_request_and_tool_group_with_large_fixed_overhead(tmp_path):
    messages = tool_history(counts=(1,) * 4, size=5000)
    messages.extend([
        AIMessage(content="Finished inspecting", id="final-reply"),
        HumanMessage(content="Now fix it", id="user-latest"),
    ])
    with Session(tmp_path / "s", tmp_path) as session:
        result = reactive_compact(messages, session, ContextPolicy(), overhead=10000)
    results = {m.tool_call_id for m in result if isinstance(m, ToolMessage)}
    assert "call-3-0" in results
    assert "call-0-0" not in results
    assert not any(m.id == "final-reply" for m in result)
    assert any(m.id == "user-latest" for m in result)


def test_reactive_can_drop_old_summaries_and_whole_skill_groups(tmp_path):
    recent = tool_history(counts=(2,), size=2000)
    messages = [
        HumanMessage(content="[Conversation continuation summary]\n" + "old " * 300,
                     id="summary-1"),
        HumanMessage(content="[Conversation continuation summary]\n" + "old " * 300,
                     id="summary-2"),
        AIMessage(content="", id="skill-call", tool_calls=[{
            "name": "load_skill", "args": {"name": "testing"}, "id": "skill-old",
        }]),
        ToolMessage(content="Testing conventions. " * 3000, name="load_skill",
                    tool_call_id="skill-old", id="skill-result"),
        *recent,
    ]
    with Session(tmp_path / "s", tmp_path) as session:
        result = reactive_compact(messages, session, ContextPolicy())
    assert all(message in result for message in recent)
    assert {"summary-1", "summary-2", "skill-call", "skill-result"}.isdisjoint(
        message.id for message in result
    )
    calls = {call["id"] for message in result if isinstance(message, AIMessage)
             for call in message.tool_calls}
    assert calls == {message.tool_call_id for message in result if isinstance(message, ToolMessage)}
    assert estimate_tokens(result) < estimate_tokens(messages) / 2


@pytest.mark.parametrize("latest_input", [
    "Preserve the authentication API. ", "[Response repair] is a literal in my code. ",
])
def test_reactive_preserves_real_user_instead_of_internal_response_repair(tmp_path, latest_input):
    user = HumanMessage(content=latest_input + "x" * 18000, id="user-latest")
    messages = [
        HumanMessage(content="Old context. " * 1000, id="user-old"),
        AIMessage(content="Previous answer", id="answer-old"),
        user,
        AIMessage(content="Partial answer", id="partial"),
        HumanMessage(content="[Response repair]\nPlease provide a complete reply.", id="repair"),
    ]
    with Session(tmp_path / "s", tmp_path) as session:
        result = reactive_compact(messages, session, ContextPolicy())
    assert user in result
    assert not any(message.id == "user-old" for message in result)


def test_micro_default_budget_keeps_recent_output_during_one_long_user_turn(tmp_path):
    with Session(tmp_path / "s", tmp_path) as session:
        messages = tool_history(size=30000)
        result = micro_compact(messages, session, ContextPolicy())
        outputs = [m for m in result if isinstance(m, ToolMessage)]
        assert all("cleared" in m.content for m in outputs[:4])
        assert all("cleared" not in m.content for m in outputs[4:])
        assert [m.id for m in result] == [m.id for m in messages]
        assert [m.tool_calls for m in result if isinstance(m, AIMessage)] == [
            m.tool_calls for m in messages if isinstance(m, AIMessage)
        ]


async def test_micro_preserves_originals_and_reuses_stable_file_references(tmp_path):
    with Session(tmp_path / "s", tmp_path) as session:
        model = SummaryModel()
        context = ContextManager(
            session, model, policy(micro_keep_tokens=0, micro_min_savings=500)
        )
        original = context.with_runtime_context(tool_history())
        context.observe_usage(original, AIMessage(content="Done", usage_metadata={
            "input_tokens": 9000, "output_tokens": 1, "total_tokens": 9001,
        }))
        result = await context.prepare_messages(original)
        assert context.estimate_input_tokens(result) == (
            estimate_tokens(result) + context.overhead_tokens
        )
        outputs = [m for m in result if isinstance(m, ToolMessage)]
        assert all("cleared" in m.content for m in outputs[:-2])
        assert outputs[-2:] == [m for m in original if isinstance(m, ToolMessage)][-2:]
        path = outputs[0].content.split("full output: ", 1)[1][:-1]
        assert Path(path).read_text() == original[2].content
        assert original[2].content in session.transcript.read_text()
        artifacts = sorted(session.artifacts.iterdir())
        assert await context.prepare_messages(result) == result
        assert sorted(session.artifacts.iterdir()) == artifacts
        assert not model.inputs


def test_micro_protects_all_parallel_results_in_the_two_recent_tool_groups(tmp_path):
    with Session(tmp_path / "s", tmp_path) as session:
        messages = tool_history(counts=(1, 1, 1, 2, 3))
        result = micro_compact(
            messages, session, policy(micro_keep_tokens=0, micro_min_savings=0)
        )
        outputs = [m for m in result if isinstance(m, ToolMessage)]
        assert all("cleared" in m.content for m in outputs[:3])
        assert outputs[3:] == [m for m in messages if isinstance(m, ToolMessage)][3:]


def test_micro_below_batch_threshold_does_not_rewrite_or_write_files(tmp_path):
    with Session(tmp_path / "s", tmp_path) as session:
        messages = tool_history(counts=(1,) * 4)
        assert micro_compact(
            messages, session, policy(micro_keep_tokens=0, micro_min_savings=5000)
        ) == messages
        assert not list(session.artifacts.iterdir())


@pytest.mark.parametrize("idle_seconds, clipped", [(299, False), (300, True), (301, True)])
async def test_micro_idle_boundary_survives_reopening_without_shrinking_protection(
    tmp_path, monkeypatch, idle_seconds, clipped
):
    directory = tmp_path / "s"
    with Session(directory, tmp_path) as session:
        session.set("last_model_request_at", 1000)
    monkeypatch.setattr(
        "mini_harness.context.manager.time", lambda: 1000 + idle_seconds, raising=False
    )
    with Session(directory) as session:
        context = ContextManager(
            session, SummaryModel(), policy(micro_keep_tokens=0, micro_min_savings=5000)
        )
        messages = tool_history(counts=(1,) * 4)
        outputs = [
            m for m in await context.prepare_messages(messages) if isinstance(m, ToolMessage)
        ]
        assert all(("cleared" in m.content) == clipped for m in outputs[:2])
        assert outputs[-2:] == [m for m in messages if isinstance(m, ToolMessage)][-2:]
        assert bool(list(session.artifacts.iterdir())) == clipped


def test_micro_does_not_replace_short_results_with_larger_file_pointers(tmp_path):
    with Session(tmp_path / "s", tmp_path) as session:
        messages = tool_history(counts=(1, 1), size=0)
        assert micro_compact(
            messages, session, policy(micro_keep_rounds=0, micro_keep_tokens=0, micro_min_savings=0)
        ) == messages
        assert not list(session.artifacts.iterdir())


async def test_only_main_model_requests_refresh_the_persisted_start_time(tmp_path, monkeypatch):
    clock = [1000]
    monkeypatch.setattr("mini_harness.context.manager.time", lambda: clock[0], raising=False)
    with Session(tmp_path / "s", tmp_path) as session:
        context = ContextManager(session, SummaryModel(), policy())
        messages = history()

        async def handler(req):
            clock[0] += 60
            return AIMessage(content="done")

        await context.request_model(messages, handler)
        assert session.get("last_model_request_at") == 1000
        await context.compact(history())
        assert session.get("last_model_request_at") == 1000
        await context.request_model(messages, handler)
        assert session.get("last_model_request_at") == 1060


async def test_compact_keeps_whole_tool_round_and_records_originals(tmp_path):
    with Session(tmp_path / "s", tmp_path) as session:
        context = ContextManager(session, SummaryModel(), policy(keep_rounds=3))
        session.set("compact_failures", 2)
        result = await context.compact(history())
        assert "summary:" in result[0].content
        assert [m.id for m in result[1:-1]] == ["a1", "r1", "r2", "a2", "u2"]
        assert result[-1].content.startswith("[Runtime context]")
        assert "Do not remove JWT support" in session.transcript.read_text()
        assert session.get("compact_failures") == 0


async def test_empty_summary_does_not_change_input(tmp_path):
    with Session(tmp_path / "s", tmp_path) as session:
        context = ContextManager(session, SummaryModel(response=""), policy())
        messages = history()
        with pytest.raises(ValueError, match="empty"):
            await context.compact(messages)
        assert messages[0].content == "Do not remove JWT support"


async def test_compact_budget_includes_restored_runtime_context(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Project rule. " * 230)
    with Session(tmp_path / "s", tmp_path) as session:
        model = SummaryModel()
        context = ContextManager(session, model, policy(keep_rounds=1))
        messages = [
            HumanMessage(content="Preserve the public API", id="request"),
            AIMessage(content="latest result " + "x" * 26000, id="latest"),
        ]
        result = await context.compact(messages)
        assert model.inputs
        assert "summary:" in result[0].content
        assert context.estimate_input_tokens(result) < context.policy.auto_limit
        assert "latest" in [m.id for m in result]
        assert any(
            "[Runtime context]" in m.content and "Project rule." in m.content
            for m in result
        )


async def test_partial_compact_preserves_outside_segment(tmp_path):
    with Session(tmp_path / "s", tmp_path) as session:
        context = ContextManager(session, SummaryModel(), policy())
        result = await context.compact(history(), start_group=1, end_group=2)
        assert result[0].id == "u1"
        assert "summary:" in result[1].content
        assert [m.id for m in result[2:-1]] == ["a2", "u2"]
        assert result[-1].content.startswith("[Runtime context]")


async def test_summary_request_is_bounded_and_failure_breaker_stops_calls(tmp_path):
    with Session(tmp_path / "s", tmp_path) as session:
        model = SummaryModel(error=RuntimeError("service unavailable"))
        context = ContextManager(session, model, policy(window=3000, auto_buffer=500))
        messages = [HumanMessage(content="x" * 9000, id="huge")]
        for _ in range(5):
            with pytest.raises(ValueError, match="budget"):
                await context.prepare_messages(messages)
        assert len(model.inputs) <= 3
        assert session.get("compact_failures") == 3


def reactive_history():
    messages = tool_history(counts=(1, 1, 2))
    messages[2].content = "Old large output " + "x" * 18000
    return [
        HumanMessage(content="[Conversation continuation summary]\nKeep JWT", id="summary"),
        *messages,
        HumanMessage(content="Now fix the login bug", id="latest-user"),
        HumanMessage(content="[Internal background task event]\nfinished", id="event"),
    ]


async def test_reactive_trims_old_groups_without_summarizing_and_returns_recovered_messages(
    tmp_path, monkeypatch
):
    clock = [1000]
    monkeypatch.setattr("mini_harness.context.manager.time", lambda: clock[0])
    with Session(tmp_path / "s", tmp_path) as session:
        model = SummaryModel(error=AssertionError("Reactive must not call the summarizer"))
        context = ContextManager(session, model, policy())
        messages = await context.prepare_messages(reactive_history())
        session.set("compact_failures", 2)
        attempts = []
        starts = []

        async def handler(req):
            attempts.append(req)
            starts.append(session.get("last_model_request_at"))
            if len(attempts) == 1:
                clock[0] = 1020
                raise RuntimeError("prompt_too_long")
            return AIMessage(content="recovered", id="done")

        result = await context.request_model(messages, handler)
        assert len(attempts) == 2
        assert starts == [1000, 1020]
        assert not model.inputs
        retried = attempts[1]
        ids = {m.id for m in retried}
        assert {"assistant-0", "result-0-0"}.isdisjoint(ids)
        assert {"assistant-1", "result-1-0", "assistant-2", "result-2-0", "result-2-1"} <= ids
        assert "summary" not in ids
        assert "latest-user" in ids
        assert any(m.content.startswith("[Runtime context]") for m in retried)
        assert str(session.transcript) in retried[0].content
        assert "omitted" in retried[0].content
        assert context.estimate_input_tokens(retried) < context.estimate_input_tokens(messages) / 2
        assert "Old large output " + "x" * 18000 in session.transcript.read_text()
        assert session.get("compact_failures") == 0
        reply, used = result
        assert used == retried
        assert reply.content == "recovered"


async def test_reactive_retry_failure_keeps_original_context(tmp_path):
    with Session(tmp_path / "s", tmp_path) as session:
        model = SummaryModel(error=AssertionError("Reactive must not call the summarizer"))
        context = ContextManager(session, model, policy())
        messages = await context.prepare_messages(reactive_history())
        session.set("compact_failures", 2)
        attempts = []

        async def handler(req):
            attempts.append(req)
            raise RuntimeError("prompt_too_long")

        with pytest.raises(RuntimeError, match="prompt_too_long"):
            await context.request_model(messages, handler)
        assert len(attempts) == 2
        assert "Old large output" in messages[3].content
        assert session.get("compact_failures") == 2
        assert not model.inputs


async def test_reactive_stops_if_only_latest_request_and_protected_context_remain(tmp_path):
    with Session(tmp_path / "s", tmp_path) as session:
        model = SummaryModel(error=AssertionError("Reactive must not call the summarizer"))
        context = ContextManager(session, model, policy())
        messages = await context.prepare_messages(
            [HumanMessage(content="Keep this latest request", id="latest-user")]
        )
        attempts = []

        async def handler(req):
            attempts.append(req)
            raise RuntimeError("prompt_too_long")

        with pytest.raises(ValueError, match="cannot be reduced"):
            await context.request_model(messages, handler)
        assert len(attempts) == 1
        assert not model.inputs


async def test_reactive_can_drop_old_user_text_quoting_the_summary_marker(tmp_path):
    with Session(tmp_path / "s", tmp_path) as session:
        context = ContextManager(session, SummaryModel(), policy())
        messages = await context.prepare_messages(
            [
                HumanMessage(
                    content=(
                        "[Conversation continuation summary] is a literal header " + "x" * 18000
                    ),
                    id="user-old",
                ),
                AIMessage(content="Noted", id="reply"),
                HumanMessage(content="Now fix login", id="user-latest"),
            ]
        )
        trimmed = reactive_compact(messages, session, context.policy, context.overhead_tokens)
        assert "user-old" not in {m.id for m in trimmed}
        assert "user-latest" in {m.id for m in trimmed}
        assert context.estimate_input_tokens(trimmed) < context.estimate_input_tokens(messages) / 2


async def test_ordinary_model_error_is_not_retried(tmp_path):
    with Session(tmp_path / "s", tmp_path) as session:
        model = SummaryModel()
        context = ContextManager(session, model, policy())
        messages = history()

        async def handler(req):
            raise RuntimeError("authentication failed")

        with pytest.raises(RuntimeError, match="authentication"):
            await context.request_model(messages, handler)
        assert not model.inputs


async def test_compaction_rereads_recent_file_and_respects_restoration_budget(tmp_path):
    target = tmp_path / "current.py"
    target.write_text("old version\n")
    with Session(tmp_path / "s", tmp_path) as session:
        read_file(session, "current.py")
        target.write_text("new version\n")
        context = ContextManager(session, SummaryModel(), policy())
        result = await context.compact(history())
        assert any(
            "current.py" in str(m.content) and "new version" in str(m.content) for m in result
        )
        assert context.estimate_input_tokens(result) < context.policy.auto_limit


async def test_disabling_auto_keeps_manual_compact_available(tmp_path):
    with Session(tmp_path / "s", tmp_path) as session:
        model = SummaryModel()
        context = ContextManager(session, model, policy(auto=False))
        messages = [HumanMessage(content="x" * 30000, id="u")]
        result = await context.prepare_messages(messages)
        assert result[:-1] == messages
        assert result[-1].content.startswith("[Runtime context]")
        assert not model.inputs
        await context.compact(messages)
        assert model.inputs


async def test_runtime_context_is_appended_only_when_changed_or_missing(tmp_path):
    instructions = tmp_path / "AGENTS.md"
    instructions.write_text("Run tests before finishing.\n")
    target = tmp_path / "nested"
    target.mkdir()
    with Session(tmp_path / "s", tmp_path) as session:
        context = ContextManager(session, SummaryModel(), policy())
        initial = await context.prepare_messages([HumanMessage(content="Inspect project", id="u")])
        assert "Run tests before finishing" in initial[-1].content
        assert await context.prepare_messages(initial) == initial
        session.cwd = target
        instructions.unlink()
        updated = await context.prepare_messages(initial)
        assert updated[:-1] == initial
        assert f"Current directory: {target}" in updated[-1].content
        assert "Project instructions:\n(none)" in updated[-1].content
        assert "Run tests before finishing" in initial[-1].content


@pytest.mark.parametrize("provider_overflow", [False, True])
async def test_summary_never_claims_coverage_of_omitted_requirement(tmp_path, provider_overflow):
    class LimitedModel(SummaryModel):
        async def ainvoke(self, messages, **kwargs):
            self.inputs.append(messages)
            if provider_overflow and "NEVER REMOVE AUTH" in str(messages):
                raise RuntimeError("prompt_too_long")
            return AIMessage(content="continue tests")

    with Session(tmp_path / "s", tmp_path) as session:
        model = LimitedModel()
        context = ContextManager(
            session,
            model,
            policy(
                window=3000,
                reserve=500,
                auto_buffer=500,
                block_buffer=100,
                keep_rounds=0,
            ),
        )
        messages = [
            HumanMessage(
                content="NEVER REMOVE AUTH\n" + "x" * (100 if provider_overflow else 9000), id="u1"
            ),
            AIMessage(content="Now add tests", id="a1"),
        ]
        with pytest.raises((ValueError, RuntimeError)):
            await context.compact(messages)
        assert "NEVER REMOVE AUTH" in messages[0].content


async def test_summary_overflow_fails_once_and_preserves_context(tmp_path):
    class SmallWindowModel(SummaryModel):
        async def ainvoke(self, messages, **kwargs):
            self.inputs.append(messages)
            text = str(messages[-1].content)
            if "first-request" in text and "second-request" in text:
                raise RuntimeError("prompt_too_long")
            return AIMessage(
                content="Preserved first requirement"
                if "first-request" in text
                else "Preserved both requirements"
            )

    with Session(tmp_path / "s", tmp_path) as session:
        model = SmallWindowModel()
        context = ContextManager(session, model, policy(keep_rounds=0))
        messages = [
            HumanMessage(content="first-request: preserve auth", id="u1"),
            HumanMessage(content="second-request: add tests", id="u2"),
        ]
        with pytest.raises(RuntimeError, match="prompt_too_long"):
            await context.compact(messages)
        assert len(model.inputs) == 1
        assert [m.id for m in messages] == ["u1", "u2"]
        assert "first-request: preserve auth" in session.transcript.read_text()
