"""A single context pipeline: clear old output, summarize, guard, recover."""

import asyncio
import json
import re
from dataclasses import dataclass
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command

from .prompts import NOTES_PROMPT, SUMMARY_PROMPT
from .session import Session


def conversation_text(messages) -> str:
    return json.dumps(
        [
            {
                "role": m.type,
                "content": m.content,
                **(
                    {"tool_calls": m.tool_calls}
                    if isinstance(m, AIMessage) and m.tool_calls
                    else {}
                ),
                **({"tool_call_id": m.tool_call_id} if isinstance(m, ToolMessage) else {}),
            }
            for m in messages
        ],
        ensure_ascii=False,
    )


def estimate_tokens(messages) -> int:
    # Conservative local estimate, including UTF-8 and tool arguments. No tokenizer download.
    return (len(conversation_text(messages).encode()) + 2) // 3


def rounds(messages) -> list[list]:
    """Keep an assistant's complete set of tool calls/results in the same group."""
    groups = []
    for message in messages:
        if not groups or isinstance(message, (HumanMessage, AIMessage)):
            groups.append([])
        groups[-1].append(message)
    return groups


def flatten(groups):
    return [message for group in groups for message in group]


def overflow_error(error: Exception) -> bool:
    text = str(error).lower()
    return any(
        marker in text
        for marker in (
            "prompt_too_long",
            "prompt is too long",
            "context_length_exceeded",
            "maximum context length",
            "input is too long",
            "context window exceeded",
        )
    )


@dataclass(frozen=True)
class ContextPolicy:
    window: int = 128000
    reserve: int = 8192
    auto_buffer: int = 13000
    block_buffer: int = 3000
    keep_rounds: int = 4
    micro_trigger: int = 64000
    micro_keep: int = 5
    notes: bool = False
    auto: bool = True
    notes_min_tokens: int = 10000
    notes_growth: int = 5000

    def __post_init__(self):
        if not 0 < self.reserve < self.window:
            raise ValueError("output reserve must be positive and smaller than the window")
        if not 0 <= self.block_buffer <= self.auto_buffer < self.window - self.reserve:
            raise ValueError("buffers must satisfy 0 <= block <= auto < window - reserve")
        if self.keep_rounds < 0 or self.micro_keep < 1 or self.micro_trigger < 0:
            raise ValueError("retention and Micro thresholds are invalid")

    @property
    def auto_limit(self):
        return self.window - self.reserve - self.auto_buffer

    @property
    def block_limit(self):
        return self.window - self.reserve - self.block_buffer


class ContextMiddleware(AgentMiddleware):
    def __init__(self, session: Session, model, policy: ContextPolicy):
        self.session, self.model, self.policy = session, model, policy
        self.overhead = 0
        self.notes_task = None
        self._notes_tokens = 0

    def tokens(self, messages):
        return estimate_tokens(messages) + self.overhead

    def micro(self, messages):
        if self.tokens(messages) < self.policy.micro_trigger:
            return messages
        results = [m.id for m in messages if isinstance(m, ToolMessage)]
        old = set(results[: -self.policy.micro_keep])
        output = []
        for message in messages:
            if isinstance(message, ToolMessage) and message.id in old:
                if str(message.content).startswith("[Old tool result content cleared"):
                    output.append(message)
                    continue
                text = (
                    message.content
                    if isinstance(message.content, str)
                    else json.dumps(message.content)
                )
                path = self.session.save_artifact(text)
                message = message.model_copy(
                    update={"content": f"[Old tool result content cleared; full output: {path}]"}
                )
            output.append(message)
        return output

    async def _summary(self, messages, instructions="", notes=False):
        groups = rounds(messages)
        prompt = (NOTES_PROMPT if notes else SUMMARY_PROMPT) + "\n" + instructions
        summary, batch_size, calls = "", len(groups), 0
        while groups:
            count = min(batch_size, len(groups))
            request = [SystemMessage(content=prompt)]
            if summary:
                request.append(
                    HumanMessage(
                        content="Previous summary covers earlier messages. Merge with the "
                        "following messages, preserving requirements and decisions:\n" + summary
                    )
                )
            request.append(HumanMessage(content=conversation_text(flatten(groups[:count]))))
            if estimate_tokens(request) + self.policy.reserve >= self.policy.window:
                if count == 1:
                    raise ValueError(
                        "Single round exceeds summary input budget; inspect transcript"
                    )
                batch_size = max(1, count // 2)
                continue
            calls += 1
            if calls > 32:
                raise ValueError("Summary call budget exhausted; original context retained")
            try:
                response = await self.model.ainvoke(
                    request,
                    max_tokens=self.policy.reserve,
                    config={"tags": ["nostream"]},
                )
            except Exception as error:
                if not overflow_error(error) or count == 1:
                    raise
                batch_size = max(1, count // 2)
                continue
            if response.tool_calls:
                raise ValueError("Summarizer returned tool calls instead of text")
            text = response.text.strip()
            match = re.search(r"<summary>(.*?)</summary>", text, re.S)
            if match:
                text = match.group(1).strip()
            if not text:
                raise ValueError("Summarizer returned an empty summary")
            summary = text
            groups = groups[count:]
        if not summary:
            raise ValueError("No messages to summarize")
        return summary

    async def compact(self, messages, instructions="", start=None, end=None, *, force=False):
        for message in messages:
            self.session.record_message(message)
        groups = rounds(messages)
        partial = start is not None or end is not None
        if partial:
            first, last = start or 0, len(groups) if end is None else end
            if not 0 <= first < last <= len(groups):
                raise ValueError(f"Expected round range 0 <= start < end <= {len(groups)}")
            before, selected, after = (
                flatten(groups[:first]),
                flatten(groups[first:last]),
                flatten(groups[last:]),
            )
        else:
            keep = 0 if force else min(self.policy.keep_rounds, max(0, len(groups) - 1))
            before = []
            selected = flatten(groups[:-keep] if keep else groups)
            after = flatten(groups[-keep:]) if keep else []

        summary = None
        notes = self.session.get("notes") if self.policy.notes else None
        if notes and not instructions and not partial and not force:
            ids = [m.id for m in messages]
            if notes["covered_until"] in ids:
                uncovered = ids.index(notes["covered_until"]) + 1
                recent = len(messages) - len(after)
                tail = messages[min(uncovered, recent) :]
                # Expand to the containing round; never orphan a tool result.
                if tail and isinstance(tail[0], ToolMessage):
                    for group in groups:
                        if tail[0] in group:
                            tail = messages[messages.index(group[0]) :]
                            break
                candidate = [self._summary_message(notes["text"]), *tail]
                if self.tokens(candidate) < self.policy.auto_limit:
                    summary, after = notes["text"], tail
        if summary is None:
            summary = await self._summary(selected, instructions)
        result = [*before, self._summary_message(summary), *after]
        if not partial:
            result = self.restore_files(result)
        if self.tokens(result) >= self.policy.auto_limit:
            raise ValueError("Compacted context still exceeds budget; reduce retained rounds")
        self.cleanup()
        self.session.record(
            "compact",
            source="notes" if notes and summary == notes["text"] else "model",
            before_tokens=self.tokens(messages),
            after_tokens=self.tokens(result),
            covered_ids=[m.id for m in selected],
            partial=partial,
        )
        return result

    def restore_files(self, messages):
        from .tools import read_file

        result = list(messages)
        remaining = 50000
        for item in reversed(self.session.get("recent_files", [])):
            budget = min(5000, remaining, self.policy.auto_limit - self.tokens(result) - 256)
            if budget < 128:
                break
            try:
                text = read_file(self.session, item["path"], item["offset"], item["limit"])
            except (OSError, UnicodeError):
                continue
            text = text.encode()[: budget * 3].decode("utf-8", errors="ignore")
            attachment = HumanMessage(
                content=f"[Re-read after compaction: {item['path']}]\n{text}",
                id=str(uuid4()),
            )
            if self.tokens([*result, attachment]) < self.policy.auto_limit:
                result.append(attachment)
                remaining -= estimate_tokens([attachment])
        return result

    def _summary_message(self, text):
        return HumanMessage(
            content=(
                "[Conversation continuation summary]\n"
                + text
                + f"\nFull original transcript: {self.session.transcript}"
            ),
            id=str(uuid4()),
        )

    def cleanup(self):
        self.session.set("context_generation", self.session.get("context_generation", 0) + 1)
        self.session.set("compact_failures", 0)
        self.session.set("notes", None)

    async def update_notes(self, messages):
        # Snapshot both content and boundary before the model call.
        snapshot = [m.model_copy(deep=True) for m in messages]
        if not snapshot or (isinstance(snapshot[-1], AIMessage) and snapshot[-1].tool_calls):
            return
        generation = self.session.get("context_generation", 0)
        text = await self._summary(snapshot, notes=True)
        if generation == self.session.get("context_generation", 0):
            self.session.set("notes", {"text": text, "covered_until": snapshot[-1].id})

    async def prepare(self, messages):
        for message in messages:
            self.session.record_message(message)
        result = self.micro(messages)
        failures = self.session.get("compact_failures", 0)
        if self.policy.auto and self.tokens(result) >= self.policy.auto_limit and failures < 3:
            try:
                result = await self.compact(result)
            except Exception as error:
                self.session.set("compact_failures", failures + 1)
                self.session.record("compact_error", error=str(error))
        if self.tokens(result) >= self.policy.block_limit:
            raise ValueError(
                "Context exceeds request budget; use /compact or a larger context window"
            )
        return result

    async def abefore_model(self, state, runtime):
        messages = state["messages"]
        prepared = await self.prepare(messages)
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *prepared]}

    async def aafter_model(self, state, runtime):
        for message in state["messages"]:
            self.session.record_message(message)
        count = self.tokens(state["messages"])
        last = state["messages"][-1]
        if (
            self.policy.notes
            and count >= self.policy.notes_min_tokens
            and count - self._notes_tokens >= self.policy.notes_growth
            and isinstance(last, AIMessage)
            and not last.tool_calls
            and (self.notes_task is None or self.notes_task.done())
        ):
            self._notes_tokens = count
            self.notes_task = asyncio.create_task(self._background_notes(state["messages"]))

    async def _background_notes(self, messages):
        try:
            await self.update_notes(messages)
        except Exception as error:
            self.session.record("notes_error", error=str(error))

    async def awrap_model_call(self, request, handler):
        try:
            return await handler(request)
        except Exception as error:
            if not overflow_error(error):
                raise
        recovered = await self.compact(request.messages, force=True)
        response = await handler(request.override(messages=recovered))
        return ExtendedModelResponse(
            model_response=response,
            command=Command(
                update={
                    "messages": [
                        RemoveMessage(id=REMOVE_ALL_MESSAGES),
                        *recovered,
                        *response.result,
                    ]
                }
            ),
        )

    async def close(self):
        if self.notes_task and not self.notes_task.done():
            self.notes_task.cancel()
            await asyncio.gather(self.notes_task, return_exceptions=True)
