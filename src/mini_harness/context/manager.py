"""普通上下文服务：请求前压缩、手动压缩和一次超限恢复。"""

import json
from pathlib import Path
from time import time
from uuid import uuid4

from langchain_core.messages import HumanMessage

from ..session import Session
from .compact import (
    check_input_budget,
    estimate_tokens,
    is_context_overflow,
    micro_compact,
    reactive_compact,
    restore_recent_files,
    split_for_summary,
    summarize,
)
from .policy import ContextPolicy


class ContextManager:
    """组织上下文生命周期；各层压缩算法见 compact.py。"""

    def __init__(self, session: Session, model, policy: ContextPolicy):
        self.session, self.model, self.policy = session, model, policy
        self.overhead_tokens = 0
        self._usage_snapshot = None

    def estimate_input_tokens(self, messages) -> int:
        """前缀未变时，用上次实际输入量加新增内容估算；改写历史后退回粗估。"""
        estimated_tokens = estimate_tokens(messages)
        if self._usage_snapshot:
            previous_messages, actual_tokens, previous_estimate = self._usage_snapshot
            if messages[:len(previous_messages)] == previous_messages:
                return actual_tokens + estimated_tokens - previous_estimate
        return estimated_tokens + self.overhead_tokens

    def observe_usage(self, messages, reply):
        """messages 是本次实际输入历史；reply 提供 API usage，摘要请求不参与校准。"""
        usage = reply.usage_metadata or {}
        self._usage_snapshot = None
        if usage.get("input_tokens", 0) > 0:
            snapshot = [message.model_copy(deep=True) for message in messages]
            self._usage_snapshot = snapshot, usage["input_tokens"], estimate_tokens(messages)

    async def prepare_messages(self, messages):
        """请求前依次执行：运行时信息 → Micro → Auto → Blocking。"""
        messages = self.with_runtime_context(messages)
        for message in messages:
            self.session.record_message(message)
        last_request_at = self.session.get("last_model_request_at")
        idle = (
            last_request_at is not None
            and time() - last_request_at >= self.policy.micro_idle_seconds
        )
        result = micro_compact(messages, self.session, self.policy, idle=idle)
        compact_failures = self.session.get("compact_failures", 0)
        if (
            self.policy.auto
            and self.estimate_input_tokens(result) >= self.policy.auto_limit
            and compact_failures < 3
        ):
            try:
                result = await self.compact(result)
            except Exception as error:
                self.session.set("compact_failures", compact_failures + 1)
                self.session.record("compact_error", error=str(error))
        check_input_budget(self.estimate_input_tokens(result), self.policy.block_limit)
        return result

    async def request_model(self, messages, invoke_model):
        """invoke_model 接收消息列表并异步返回回复；返回 (回复, 实际使用的历史)。

        普通请求超限时裁剪历史，只重试一次。
        """
        self.session.set("last_model_request_at", time())
        try:
            return await invoke_model(messages), messages
        except Exception as error:
            if not is_context_overflow(error):
                raise
        recovered_messages = reactive_compact(
            messages, self.session, self.policy, self.overhead_tokens
        )
        self.session.set("last_model_request_at", time())
        reply = await invoke_model(recovered_messages)
        self.session.set("compact_failures", 0)
        retained_ids = {message.id for message in recovered_messages}
        self.session.record(
            "reactive_trim",
            before_tokens=self.estimate_input_tokens(messages),
            after_tokens=self.estimate_input_tokens(recovered_messages),
            dropped_ids=[message.id for message in messages if message.id not in retained_ids],
        )
        return reply, recovered_messages

    async def compact(self, messages, instructions="", start_group=None, end_group=None):
        """Auto 与 /compact 共用入口；组范围为 [start_group, end_group)，默认保留近期组。"""
        for message in messages:
            self.session.record_message(message)
        messages_before, summary_messages, messages_after = split_for_summary(
            messages, self.policy.keep_rounds, start_group, end_group
        )
        if not summary_messages:
            raise ValueError("No messages to summarize outside protected skill groups")
        partial = start_group is not None or end_group is not None
        summary = await summarize(summary_messages, self.model, self.policy, instructions)
        result = self.with_runtime_context(
            [*messages_before, self._summary_message(summary), *messages_after]
        )
        if not partial:
            result = restore_recent_files(
                result, self.session, self.policy.auto_limit - self.overhead_tokens
            )
        if self.estimate_input_tokens(result) >= self.policy.auto_limit:
            raise ValueError("Compacted context still exceeds budget; reduce retained rounds")
        self.session.set("compact_failures", 0)
        self.session.record(
            "compact",
            before_tokens=self.estimate_input_tokens(messages),
            after_tokens=self.estimate_input_tokens(result),
            covered_ids=[message.id for message in summary_messages],
            partial=partial,
        )
        return result

    def with_runtime_context(self, messages):
        """cwd、项目指令或当前计划变化时追加消息，复用未变的请求前缀。"""
        instructions_path = Path(self.session.get("project_root")) / "AGENTS.md"
        instructions = "(none)"
        try:
            if self.session.sandbox is not None:
                self.session.sandbox.check_path(instructions_path)
            if instructions_path.is_file():
                instructions = instructions_path.read_text(encoding="utf-8")
        except PermissionError:
            instructions = "(blocked by sandbox)"
        runtime_text = (
            f"[Runtime context]\nCurrent directory: {self.session.cwd}\nProject instructions:\n"
            + instructions
        )
        if self.session.sandbox is not None:
            runtime_text += "\nSandbox:\n" + json.dumps(self.session.sandbox.status())
        # 计划放在已有运行上下文中；摘要后可重建，Reactive 也会保留最新一条。
        if plan := self.session.get("plan"):
            runtime_text += "\nCurrent plan:\n" + json.dumps(plan, ensure_ascii=False)
        if agents := self.session.get("subagents"):
            active = [
                {key: agent[key] for key in ("agent_id", "status", "session_dir")}
                for agent in agents.values() if agent["status"] != "closed"
            ]
            runtime_text += "\nSubagents:\n" + json.dumps(active, ensure_ascii=False)
        previous_context = next(
            (
                message for message in reversed(messages)
                if isinstance(message, HumanMessage)
                and message.id and message.id.startswith("runtime-")
            ),
            None,
        )
        if previous_context is not None and previous_context.content == runtime_text:
            return messages
        return [*messages, HumanMessage(content=runtime_text, id=f"runtime-{uuid4()}")]

    def _summary_message(self, summary_text):
        """给摘要附上原始记录路径，便于模型按需查回细节。"""
        return HumanMessage(
            content=(
                "[Conversation continuation summary]\n"
                + summary_text
                + f"\nFull original transcript: {self.session.transcript}"
            ),
            id=str(uuid4()),
        )
