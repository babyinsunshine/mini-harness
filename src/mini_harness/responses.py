"""模型请求、流式回复合并、完整性检查和简短错误说明。"""

import json

from langchain_core.messages import AIMessage, AIMessageChunk, message_chunk_to_message


def format_error(error: Exception) -> str:
    """保留底层连接等原因，不把完整 traceback 放进日常 CLI。"""
    parts = []
    while error is not None:
        parts.append(f"{type(error).__name__}: {error}")
        error = error.__cause__
    return " <- ".join(parts)


async def request_model_reply(model, messages) -> AIMessage:
    """发起模型请求，接收流式输出并合并成完整回复。

    messages 是本次请求的消息列表；原始工具参数保留下来供后续严格校验。
    """
    reply = None
    async for chunk in model.astream(messages):
        reply = chunk if reply is None else reply + chunk
    if isinstance(reply, AIMessageChunk):
        raw_arguments = [tool_call["args"] for tool_call in reply.tool_call_chunks]
        reply = message_chunk_to_message(reply)
        if raw_arguments:
            reply.response_metadata["raw_tool_arguments"] = raw_arguments
    return reply


def response_problem(message: AIMessage) -> str | None:
    """返回协议或结束状态的问题说明；有效回复返回 None，不判断代码是否正确。"""
    finish_reason = message.response_metadata.get("finish_reason") or message.response_metadata.get(
        "stop_reason"
    )
    if finish_reason in {"length", "max_tokens"}:
        return "truncated output: the output token limit was reached"
    malformed = "malformed tool arguments: provide complete, valid JSON"
    if message.invalid_tool_calls:
        return malformed
    # 流式适配器可能补全半截 JSON；有原始参数时再严格解析，不能直接信任补全结果。
    raw_arguments = [
        tool_call.get("function", {}).get("arguments")
        for tool_call in message.additional_kwargs.get("tool_calls", [])
    ]
    raw_arguments.extend(message.response_metadata.get("raw_tool_arguments", []))
    for arguments_json in raw_arguments:
        if isinstance(arguments_json, str):
            try:
                json.loads(arguments_json)
            except json.JSONDecodeError:
                return malformed
    if not message.text.strip() and not message.tool_calls:
        return "an empty response"
    return None
