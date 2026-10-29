"""上下文压缩算法：Micro、共享摘要、Blocking 和 Reactive。"""

import json
import re
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage

from ..prompts import load_prompt
from ..responses import response_problem
from ..session import Session
from ..tools.files import read_file
from ..tools.permissions import classify
from .policy import ContextPolicy

INTERNAL_CONTEXT_PREFIXES = (
    "[Runtime context]",
    "[Internal background task event]",
    "[Conversation continuation summary]",
    "[Re-read after compaction:",
    "[Context overflow recovery]",
)


def conversation_text(messages: list[AnyMessage]) -> str:
    """序列化文字与工具关系；图片保留路径说明，不把 Base64 送入文字摘要。"""
    items = []
    for message in messages:
        content = message.content
        if isinstance(content, list):
            content = [block for block in content
                       if not (isinstance(block, dict) and block.get("type") == "image")]
        item = {"role": message.type, "content": content}
        if isinstance(message, AIMessage) and message.tool_calls:
            item["tool_calls"] = message.tool_calls
        if isinstance(message, AIMessage) and message.invalid_tool_calls:
            item["invalid_tool_calls"] = message.invalid_tool_calls
        if isinstance(message, ToolMessage):
            item["tool_call_id"] = message.tool_call_id
        items.append(item)
    return json.dumps(items, ensure_ascii=False)


def estimate_tokens(messages: list[AnyMessage]) -> int:
    """文字按 UTF-8 粗估，图片暂按每张 4096；主请求再用 API usage 校准。"""
    images = sum(
        isinstance(block, dict) and block.get("type") == "image"
        for message in messages if isinstance(message.content, list)
        for block in message.content
    )
    return (len(conversation_text(messages).encode()) + 2) // 3 + images * 4096


def group_messages(messages: list[AnyMessage]) -> list[list[AnyMessage]]:
    """按完整消息组切分，保证 assistant 与其全部工具结果不被拆开。"""
    groups = []
    for message in messages:
        if not groups or isinstance(message, (HumanMessage, AIMessage)):
            groups.append([])
        groups[-1].append(message)
    return groups


def flatten_groups(groups: list[list[AnyMessage]]) -> list[AnyMessage]:
    return [message for group in groups for message in group]


def has_skill_instructions(group: list[AnyMessage]) -> bool:
    """成功加载的技能是工作指令；保护整组以保持工具调用配对。"""
    return any(
        isinstance(message, ToolMessage)
        and message.name == "load_skill" and message.status == "success"
        for message in group
    )


def micro_compact(
    messages: list[AnyMessage], session: Session, policy: ContextPolicy, *, idle: bool = False
) -> list[AnyMessage]:
    """Micro：保留近期输出，裁剪旧输出；idle=True 时降低批量裁剪门槛。"""
    minimum_savings = 0 if idle else policy.micro_min_savings
    candidates = {}
    output_tokens = 0
    savings = 0
    tool_rounds = 0
    for group in reversed(group_messages(messages)):
        if has_skill_instructions(group):
            continue
        outputs = [m for m in group if isinstance(m, ToolMessage)]
        if not outputs:
            continue
        tool_rounds += 1
        # 两种保护覆盖同一批近期输出；边界处保留整组并行工具结果。
        protected = (
            tool_rounds <= policy.micro_keep_rounds or output_tokens < policy.micro_keep_tokens
        )
        for message in outputs:
            if str(message.content).startswith("[Old tool result content cleared"):
                continue
            size = estimate_tokens([message])
            output_tokens += size
            if protected:
                continue
            if isinstance(message.content, list) and any(
                isinstance(block, dict) and block.get("type") == "image"
                for block in message.content
            ):
                # 原图已由读取工具保存；保留文字路径，不再把编码写成另一份文本文件。
                text = None
                content = (
                    "[Old tool result content cleared; image removed. "
                    "Use read_image with the saved path to view again.]\n" + message.text
                )
            else:
                text = (message.content if isinstance(message.content, str)
                        else json.dumps(message.content, ensure_ascii=False))
                content = (
                    f"[Old tool result content cleared; full output: {session.artifact_path(text)}]"
                )
            replacement = message.model_copy(update={"content": content})
            saved = size - estimate_tokens([replacement])
            if saved > 0:
                candidates[message.id] = (replacement, text)
                savings += saved
    if savings <= minimum_savings:
        return messages
    # 只有真正裁剪时才写磁盘；后续请求和恢复会话复用相同的引用文本。
    for _, text in candidates.values():
        if text is not None:
            session.save_artifact(text)
    return [candidates[m.id][0] if m.id in candidates else m for m in messages]


async def summarize(
    messages: list[AnyMessage],
    model: BaseChatModel,
    policy: ContextPolicy,
    instructions: str = "",
) -> str:
    """生成一次文本摘要，供 Auto 与手动压缩共用；失败直接交给调用方。"""
    if not messages:
        raise ValueError("No messages to summarize")
    request = [
        SystemMessage(content=load_prompt("summary") + "\n" + instructions),
        HumanMessage(content=conversation_text(messages)),
    ]
    if estimate_tokens(request) + policy.reserve >= policy.window:
        raise ValueError("Summary input exceeds budget; original context retained")
    response = await model.ainvoke(
        request, max_tokens=policy.reserve, config={"tags": ["nostream"]}
    )
    if problem := response_problem(response):
        raise ValueError(f"Summarizer returned {problem}")
    if response.tool_calls:
        raise ValueError("Summarizer returned tool calls instead of text")
    text = response.text.strip()
    match = re.search(r"<summary>(.*?)</summary>", text, re.S)
    if match:
        text = match.group(1).strip()
    if not text:
        raise ValueError("Summarizer returned an empty summary")
    return text


def split_for_summary(
    messages: list[AnyMessage],
    keep_rounds: int,
    start_group: int | None = None,
    end_group: int | None = None,
) -> tuple[list[AnyMessage], list[AnyMessage], list[AnyMessage]]:
    """返回前段、摘要段、保留段（含技能组）；组索引左闭右开。"""
    groups = group_messages(messages)
    if start_group is not None or end_group is not None:
        first = start_group or 0
        last = len(groups) if end_group is None else end_group
        if not 0 <= first < last <= len(groups):
            raise ValueError(f"Expected round range 0 <= start < end <= {len(groups)}")
    else:
        keep = min(keep_rounds, max(0, len(groups) - 1))
        first, last = 0, len(groups) - keep
    selected = groups[first:last]
    protected = [group for group in selected if has_skill_instructions(group)]
    summarized = [group for group in selected if not has_skill_instructions(group)]
    return (
        flatten_groups(groups[:first]),
        flatten_groups(summarized),
        flatten_groups([*protected, *groups[last:]]),
    )


def restore_recent_files(
    messages: list[AnyMessage], session: Session, token_budget: int
) -> list[AnyMessage]:
    """摘要后重读近期文件；token_budget 已扣除系统提示与工具定义的用量。"""
    result = list(messages)
    for item in reversed(session.get("recent_files", [])):
        # 自动恢复不能复用一次性审批；目录外文件须由模型重新申请读取。
        if classify({"name": "read_file", "args": {"path": item["path"]}}, session)[0] != "allow":
            continue
        budget = min(5000, token_budget - estimate_tokens(result) - 256)
        if budget < 128:
            break
        try:
            # 附件可能因预算被丢弃；自动读取不能推进智能体确认过的文件版本。
            text = read_file(
                session, item["path"], item["offset"], item["limit"], record_version=False,
            )["output"]
        except (OSError, UnicodeError, ValueError):
            continue
        text = text.encode()[: budget * 3].decode("utf-8", errors="ignore")
        attachment = HumanMessage(
            content=f"[Re-read after compaction: {item['path']}]\n{text}", id=str(uuid4())
        )
        if estimate_tokens([*result, attachment]) < token_budget:
            result.append(attachment)
    return result


def check_input_budget(input_tokens: int, max_input_tokens: int) -> None:
    """Blocking：请求仍超出可用输入预算时停止本轮，避免发送必然过大的请求。"""
    if input_tokens >= max_input_tokens:
        raise ValueError("Context exceeds request budget; use /compact or a larger context window")


def is_context_overflow(error: Exception) -> bool:
    """只识别明确的上下文超限错误，其他请求错误原样上抛。"""
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


def reactive_compact(
    messages: list[AnyMessage], session: Session, policy: ContextPolicy, overhead: int = 0
) -> list[AnyMessage]:
    """Reactive：实际超限后裁掉最老的完整组，以减半为目标，不调用摘要模型。"""
    groups = group_messages(messages)
    tail = next(
        (i for i in range(len(groups) - 1, -1, -1) if isinstance(groups[i][0], AIMessage)),
        len(groups) - 1,
    )
    # 最新 assistant/工具组及其后的用户输入、内部通知一起保留。
    protected = {m.id for m in flatten_groups(groups[tail:])}
    protected.update(
        m.id for m in messages
        if isinstance(m, HumanMessage)
        and not (m.id or "").startswith("user-")
        and str(m.content).startswith("[Conversation continuation summary]")
    )
    for message in reversed(messages):
        if isinstance(message, HumanMessage) and (
            (message.id or "").startswith("user-")
            # 兼容尚未使用 user- ID 的既有会话。
            or not str(message.content).startswith(INTERNAL_CONTEXT_PREFIXES)
        ):
            protected.add(message.id)
            break
    for message in reversed(messages):
        if (message.id or "").startswith("runtime-"):
            protected.add(message.id)
            break

    notice = HumanMessage(
        content=(
            "[Context overflow recovery]\nOlder conversation groups were omitted to fit "
            f"the model. Full original transcript: {session.transcript}"
        ),
        id=str(uuid4()),
    )
    before = estimate_tokens(messages) + overhead
    target = min(policy.block_limit, before // 2)
    remaining = before + estimate_tokens([notice])
    kept = []
    for group in groups:
        is_protected = has_skill_instructions(group) or any(m.id in protected for m in group)
        if remaining > target and not is_protected:
            remaining -= estimate_tokens(group)
        else:
            kept.extend(group)
    result = [notice, *kept]
    if estimate_tokens(result) + overhead >= min(before, policy.block_limit):
        raise ValueError(
            "Context cannot be reduced further while preserving the latest request "
            "and protected context; inspect transcript or adjust the model window"
        )
    return result
