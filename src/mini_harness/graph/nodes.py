"""六个执行阶段；具体的压缩、权限和进程机制由各自模块负责。"""

import json
from time import monotonic
from uuid import uuid4

from langchain_core.messages import HumanMessage, RemoveMessage, ToolMessage, message_to_dict
from langgraph.config import get_stream_writer
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime

from ..responses import request_model_reply, response_problem
from .types import Resources, State

INTERRUPTED_TOOL = "Execution interrupted; result not confirmed. Inspect files/logs before retry."


def collect_background_events(state: State, runtime: Runtime[Resources]):
    """在工具批次之间收集后台完成事件，作为下一次请求的上下文。"""
    resources = runtime.context
    messages = []
    for event in resources.processes.drain_events():
        message = HumanMessage(
            content="[Internal background task event]\n" + json.dumps(event), id=str(uuid4())
        )
        resources.session.record_message(message)
        messages.append(message)
        get_stream_writer()({"type": "background", "task": event})
    return {"messages": messages}


async def compact_context(state: State, runtime: Runtime[Resources]):
    """下一次主请求前做 Micro / Auto / Blocking，工具调用与结果已配对。"""
    context = runtime.context.context_manager
    tokens_before = context.estimate_input_tokens(state["messages"])
    if context.policy.auto and tokens_before >= context.policy.auto_limit:
        get_stream_writer()({"type": "compact_start"})
    messages = await context.prepare_messages(state["messages"])
    tokens_after = context.estimate_input_tokens(messages)
    if tokens_after < tokens_before:
        get_stream_writer()({"type": "compact_end", "before": tokens_before, "after": tokens_after})
    return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *messages]}


async def call_model(state: State, runtime: Runtime[Resources]):
    """调用已绑定工具的模型；超限恢复由普通上下文服务处理。"""
    resources = runtime.context
    tokens_before = resources.context_manager.estimate_input_tokens(state["messages"])

    async def invoke_model(messages):
        started_at = monotonic()
        get_stream_writer()({"type": "model_start"})
        reply = await request_model_reply(resources.model, [resources.system_message, *messages])
        request_stats = {
            "elapsed": monotonic() - started_at,
            "usage": reply.usage_metadata or {},
            "finish_reason": reply.response_metadata.get("finish_reason")
            or reply.response_metadata.get("stop_reason"),
        }
        resources.session.record("model", **request_stats)
        get_stream_writer()({"type": "model_end", **request_stats})
        return reply

    reply, input_messages = await resources.context_manager.request_model(
        state["messages"], invoke_model
    )
    resources.context_manager.observe_usage(input_messages, reply)
    resources.session.record_message(reply)
    if input_messages is not state["messages"]:
        get_stream_writer()(
            {
                "type": "compact_end",
                "before": tokens_before,
                "after": resources.context_manager.estimate_input_tokens(input_messages),
            }
        )
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *input_messages, reply]}
    return {"messages": [reply]}


async def validate_response(state: State, runtime: Runtime[Resources]):
    """检查回复并构造有限修复反馈；不完整参数始终不进入工具执行。"""
    reply = state["messages"][-1]
    if problem := response_problem(reply):
        repair_attempts = state.get("repair_attempts", 0)
        if repair_attempts >= 2:
            raise ValueError(f"Model response repair limit reached: {problem}")
        feedback = f"Tool was not executed: {problem}. Re-issue complete calls or a complete reply."
        tool_calls = [*reply.tool_calls, *reply.invalid_tool_calls]
        messages = [
            ToolMessage(
                content=feedback,
                tool_call_id=tool_call["id"],
                id=f"result-{tool_call['id']}",
                status="error",
            )
            for tool_call in tool_calls
        ]
        if not tool_calls:
            messages = [HumanMessage(content=f"[Response repair]\n{feedback}", id=str(uuid4()))]
        for message in messages:
            runtime.context.session.record_message(message)
        get_stream_writer()({"type": "repair", "reason": problem, "attempt": repair_attempts + 1})
        return {"messages": messages, "repair_attempts": repair_attempts + 1, "next": "collect"}
    return {
        "pending_calls": reply.tool_calls,
        "repair_attempts": 0,
        "next": "permission" if reply.tool_calls else "end",
    }


async def check_permission(state: State, runtime: Runtime[Resources]):
    """每次只审批当前调用，使下一个调用按执行后的 cwd 重新检查。"""
    original_call = state["pending_calls"][0]
    # 权限校验会规范化扩权路径；用副本，明确将获准参数交给后续执行。
    tool_call = {**original_call, "args": dict(original_call["args"])}
    denial_result = await runtime.context.permissions.check(tool_call)
    if denial_result is None:
        return {"approved": True, "pending_calls": [tool_call, *state["pending_calls"][1:]]}
    get_stream_writer()({
        "type": "tool_denied", "call_id": tool_call["id"],
        "name": tool_call["name"], "reason": denial_result.content,
    })
    return {
        "approved": False,
        "messages": [denial_result],
        "pending_calls": state["pending_calls"][1:],
    }


async def execute_tool(state: State, runtime: Runtime[Resources]):
    """执行一个已获准的调用，先落执行日志，再把结果交给图。"""
    tool_call = state["pending_calls"][0]
    resources = runtime.context
    emit_event = get_stream_writer()
    emit_event({
        "type": "tool_start", "call_id": tool_call["id"],
        "name": tool_call["name"], "args": tool_call["args"],
    })
    started_at = monotonic()
    resources.session.start_execution(tool_call["id"])
    result = ToolMessage(content=INTERRUPTED_TOOL, tool_call_id=tool_call["id"], status="error")
    exit_code = None
    try:
        tool = resources.tools[tool_call["name"]]
        result = await tool.ainvoke({**tool_call, "type": "tool_call"})
        if tool_call["name"] == "bash" and result.status == "success":
            exit_code = json.loads(result.text)["exit_code"]
            if exit_code not in (None, 0):
                result.status = "error"
    except Exception as error:
        result = ToolMessage(
            content=f"{type(error).__name__}: {error}", tool_call_id=tool_call["id"], status="error"
        )
    finally:
        # CancelledError 也必须保存不确定结果，恢复时不能重放这个副作用。
        result.id = result.id or f"result-{tool_call['id']}"
        resources.session.finish_execution(tool_call["id"], message_to_dict(result))
        resources.session.record_message(result)
    emit_event(
        {
            "type": "tool_end",
            "call_id": tool_call["id"],
            "name": tool_call["name"],
            "status": result.status,
            "exit_code": exit_code,
            "output": result.text,
            "elapsed": monotonic() - started_at,
        }
    )
    return {"messages": [result], "pending_calls": state["pending_calls"][1:], "approved": False}
