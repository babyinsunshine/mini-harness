"""资源生命周期与会话接口；显式循环见 graph/builder.py。"""

import asyncio
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
from .sandbox import Sandbox
from .session import Session
from .subagents import SubagentManager
from .tools.factory import create_tools
from .tools.mcp import connect_mcp
from .tools.permissions import Permissions
from .tools.processes import ProcessManager


class Harness:
    def __init__(
        self, session, processes, model, policy, checkpointer, max_steps,
        approve=None, process_timeout=3600, parent_permissions=None, full_access=False,
        mcp_clients=None,
    ):
        self.session, self.processes = session, processes
        self.mcp_clients = mcp_clients or {}
        self.context = ContextManager(session, model, policy)
        self.permissions = Permissions(
            session, approve, parent_permissions, full_access=full_access,
        )
        is_child = session.get("parent_session_id") is not None
        self.subagents = None if is_child else SubagentManager(self, max_steps, process_timeout)
        tools = create_tools(
            session, processes, self.subagents, include_plan=not is_child,
            mcp_clients=self.mcp_clients,
        )
        tool_tokens = sum(len(json.dumps(tool.args).encode()) // 3 + 100 for tool in tools)
        system_message = SystemMessage(
            content=load_prompt("system")
            + load_prompt("subagent" if is_child else "main")
            + (
                f"\nSession ID: {session.id}\nProject root: {session.get('project_root')}\n"
                f"Environment file: {processes.env_file}\n"
                f"Scratch directory: {session.scratch_directory}\n"
            )
        )
        if session.sandbox is not None:
            system_message.content += load_prompt("sandbox")
        if is_child:
            system_message.content += (
                f"Parent session ID: {session.get('parent_session_id')}\n"
                f"Parent session directory: {session.get('parent_session_dir')}\n"
            )
        self.context.overhead_tokens = estimate_tokens([system_message]) + tool_tokens
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
        parent_session=None, parent_permissions=None, full_access=False,
        mcp_config=None, parent_mcp=None,
        sandbox=False, sandbox_config=None, parent_sandbox=None,
    ):
        """打开会话；cwd 是首次工作目录，session_dir 保存会话数据。

        max_steps 按图节点计数；process_timeout 单位为秒；approve 是异步审批回调。
        parent_session / parent_permissions 仅供子智能体继承父身份与审批入口。
        full_access 跳过普通审批；沙箱边界及其明确审批仍有效，子智能体沿用父权限。
        mcp_config 显式启用配置中的 MCP 服务；parent_mcp 复用父连接。
        sandbox 开启文件沙箱；子智能体复用 parent_sandbox，不恢复额外授权。
        """
        with Session(Path(session_dir), Path(cwd)) as session:
            if parent_session is not None:
                session.set("parent_session_id", parent_session.id)
                session.set("parent_session_dir", str(parent_session.directory))
                session.set("project_root", parent_session.get("project_root"))
            session.sandbox = parent_sandbox
            if sandbox and session.sandbox is None:
                session.sandbox = Sandbox.load(Path(session.get("project_root")), sandbox_config)
            if session.sandbox is not None:
                session.sandbox.require_runtime()
            config_path = (
                Path(mcp_config).expanduser().resolve() if mcp_config is not None
                else None
            )
            async with (
                ProcessManager(session, max_runtime_seconds=process_timeout) as processes,
                AsyncSqliteSaver.from_conn_string(str(session.database)) as checkpointer,
                connect_mcp(config_path, parent_mcp) as mcp_clients,
            ):
                harness = cls(
                    session,
                    processes,
                    model,
                    policy or ContextPolicy(),
                    checkpointer,
                    max_steps,
                    approve=approve,
                    process_timeout=process_timeout,
                    parent_permissions=parent_permissions,
                    full_access=full_access,
                    mcp_clients=mcp_clients,
                )
                try:
                    yield harness
                finally:
                    if harness.subagents is not None:
                        await harness.subagents.interrupt()
                    # 取消图后仍可能写入取消记录；先等写入结束，再关闭 SQLite。
                    async with checkpointer.lock:
                        pass

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

    async def events(self, user_input: str | None, *, input_id: str | None = None):
        """接收本次用户输入并产生事件；user_input=None 时仅处理已有会话。

        input_id 用于子任务恢复去重；已接收的相同 ID 只继续上下文，不重发指令。
        """
        if self.subagents is not None:
            self.subagents.resume_interrupted()
        snapshot = await self.graph.aget_state(self.config)
        messages = self.recover_missing_tool_results(snapshot.values.get("messages", []))
        last_input_id = snapshot.values.get("last_user_input_id", "")
        # 消息可能已被摘要替换，是否接收过输入要看检查点字段，不能只查消息 ID。
        if user_input is not None and (input_id is None or input_id != last_input_id):
            last_input_id = input_id or f"user-{uuid4()}"
            messages.append(HumanMessage(content=user_input, id=last_input_id))
        try:
            async for stream_mode, stream_data in self.graph.astream(
                {
                    "messages": messages,
                    "pending_calls": [],
                    "approved": False,
                    "repair_attempts": 0,
                    "next": "",
                    "last_user_input_id": last_input_id,
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
        except asyncio.CancelledError:
            if self.subagents is not None:
                await self.subagents.interrupt()
            raise
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
