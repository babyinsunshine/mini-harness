"""只负责连线；打开本文件就能看到循环与退出条件。"""

from langgraph.graph import END, START, StateGraph

from . import nodes
from .types import Resources, State


def route_after_permission(state: State):
    if state["approved"]:
        return "tools"
    return route_after_tool(state)


def route_after_tool(state: State):
    return "permission" if state["pending_calls"] else "collect"


def build_graph(checkpointer):
    builder = StateGraph(State, context_schema=Resources)
    builder.add_node("collect", nodes.collect_background_events)
    builder.add_node("compact", nodes.compact_context)
    builder.add_node("model", nodes.call_model)
    builder.add_node("response", nodes.validate_response)
    builder.add_node("permission", nodes.check_permission)
    builder.add_node("tools", nodes.execute_tool)

    builder.add_edge(START, "collect")
    builder.add_edge("collect", "compact")
    builder.add_edge("compact", "model")
    builder.add_edge("model", "response")
    builder.add_conditional_edges(
        "response",
        lambda state: state["next"],
        {"collect": "collect", "permission": "permission", "end": END},
    )
    builder.add_conditional_edges(
        "permission", route_after_permission, ["tools", "permission", "collect"]
    )
    builder.add_conditional_edges("tools", route_after_tool, ["permission", "collect"])
    return builder.compile(checkpointer=checkpointer)
