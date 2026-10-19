"""资源生命周期与会话接口；显式循环见 graph/builder.py。"""

import json
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
    messages_from_dict,
)
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from .context.compact import estimate_tokens
from .context.manager import ContextManager
from .context.policy import ContextPolicy
from .graph.builder import build_graph
from .graph.nodes import INTERRUPTED_TOOL
from .graph.types import Resources
from .prompts import load_prompt
from .session import Session
from .tools.factory import create_tools
from .tools.permissions import Permissions
from .tools.processes import ProcessManager


class Harness:
    def __init__(self, session, processes, model, policy, checkpointer, max_steps, approve=None):
        self.session, self.processes = session, processes
        self.context = ContextManager(session, model, policy)
        tools = create_tools(session, processes)
        tool_tokens = sum(len(json.dumps(tool.args).encode()) // 3 + 100 for tool in tools)
        system_message = SystemMessage(
            content=load_prompt("system")
            + (
                f"\nProject root: {session.get('project_root')}\n"
                f"Environment file: {processes.env_file}\n"
            )
        )
        self.context.overhead_tokens = estimate_tokens([system_message]) + tool_tokens
        self.permissions = Permissions(session, approve)
        self.resources = Resources(
            session=session,
            processes=processes,
            context_manager=self.context,
            permissions=self.permissions,
            model=model.bind_tools(tools),
            tools={tool.name: tool for tool in tools},
            system_message=system_message,
        )
        self.graph = build_graph(checkpointer)
        self.config = {"configurable": {"thread_id": session.id}, "recursion_limit": max_steps}

    @classmethod
    @asynccontextmanager
    async def open(
        cls, cwd, session_dir, model, policy=None,
        max_steps=1000, process_timeout=3600, approve=None,
    ):
        """打开会话；cwd 是首次工作目录，session_dir 保存会话数据。

        max_steps 按图节点计数；process_timeout 单位为秒；approve 是异步审批回调。
        """
        with Session(Path(session_dir), Path(cwd)) as session:
            async with ProcessManager(session, max_runtime_seconds=process_timeout) as processes:
                async with AsyncSqliteSaver.from_conn_string(str(session.database)) as checkpointer:
                    harness = cls(
                        session,
                        processes,
                        model,
                        policy or ContextPolicy(),
                        checkpointer,
                        max_steps,
                        approve,
                    )
                    yield harness

    def recover_missing_tool_results(self, messages):
        """补齐中断时缺失的工具结果，复用执行记录，不重新执行工具。"""
        completed_call_ids = {
            message.tool_call_id for message in messages if isinstance(message, ToolMessage)
        }
        recovered_results = []
        for message in messages:
            if not isinstance(message, AIMessage):
                continue
            for tool_call in [*message.tool_calls, *message.invalid_tool_calls]:
                if tool_call["id"] in completed_call_ids:
                    continue
                execution_record = self.session.get_execution(tool_call["id"])
                if execution_record and execution_record[1] is not None:
                    result = messages_from_dict([execution_record[1]])[0]
                else:
                    result = ToolMessage(
                        content=INTERRUPTED_TOOL
                        if execution_record
                        else "Call canceled before execution; it was not run.",
                        tool_call_id=tool_call["id"],
                        id=f"result-{tool_call['id']}",
                        status="error",
                    )
                recovered_results.append(result)
        return recovered_results

    async def events(self, user_input: str | None):
        """接收本次用户输入并产生事件；user_input=None 时仅处理已有会话。"""
        snapshot = await self.graph.aget_state(self.config)
        messages = self.recover_missing_tool_results(snapshot.values.get("messages", []))
        if user_input is not None:
            messages.append(HumanMessage(content=user_input, id=f"user-{uuid4()}"))
        async for stream_mode, stream_data in self.graph.astream(
            {
                "messages": messages,
                "pending_calls": [],
                "approved": False,
                "repair_attempts": 0,
                "next": "",
            },
            self.config,
            context=self.resources,
            stream_mode=["messages", "custom"],
            durability="sync",
        ):
            if stream_mode == "custom":
                yield stream_data
            else:
                chunk, metadata = stream_data
                if (
                    metadata.get("langgraph_node") == "model"
                    and isinstance(chunk, AIMessage)
                    and chunk.text
                ):
                    yield {"type": "text", "text": chunk.text}
        yield {"type": "done"}

    async def stream(self, user_input: str | None):
        """保留只读取模型正文的简单接口。"""
        async for event in self.events(user_input):
            if event["type"] == "text":
                yield event["text"]

    async def compact(self, instructions="", start_group=None, end_group=None):
        """手动压缩；instructions 是摘要侧重点，组索引从 0 开始，范围左闭右开。"""
        snapshot = await self.graph.aget_state(self.config)
        messages = snapshot.values.get("messages", [])
        if not messages:
            raise ValueError("There are no messages to compact")
        if self.recover_missing_tool_results(messages):
            raise ValueError("Finish or recover interrupted tool calls before compacting")
        result = await self.context.compact(messages, instructions, start_group, end_group)
        await self.graph.aupdate_state(
            self.config,
            {
                "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *result],
            },
            as_node="compact",
        )
        return result
