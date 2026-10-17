"""Assembly and lifecycle. LangChain owns the model/tool loop."""

import json
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
    message_to_dict,
    messages_from_dict,
)
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from .context import ContextMiddleware, ContextPolicy, estimate_tokens
from .processes import ProcessManager
from .prompts import SYSTEM_PROMPT
from .session import Session
from .tools import create_tools


class ExecutionMiddleware(AgentMiddleware):
    """Durable tool results close the gap between OS side effects and checkpoints."""

    def __init__(self, session, processes, context, tool_tokens):
        self.session, self.processes, self.context = session, processes, context
        self.tool_tokens = tool_tokens

    def system_message(self):
        root = Path(self.session.get("project_root"))
        instructions = root / "AGENTS.md"
        text = SYSTEM_PROMPT + f"\nProject root: {root}\nCurrent directory: {self.session.cwd}\n"
        if instructions.is_file():
            text += "\nProject instructions:\n" + instructions.read_text(encoding="utf-8")
        return SystemMessage(content=text)

    async def abefore_model(self, state, runtime):
        self.context.overhead = estimate_tokens([self.system_message()]) + self.tool_tokens
        messages = []
        for event in self.processes.drain_events():
            message = HumanMessage(
                content="[Internal background task event]\n" + json.dumps(event), id=str(uuid4())
            )
            self.session.record_message(message)
            messages.append(message)
        return {"messages": messages} if messages else None

    async def awrap_model_call(self, request, handler):
        return await handler(request.override(system_message=self.system_message()))

    async def awrap_tool_call(self, request, handler):
        call_id = request.tool_call["id"]
        previous = self.session.execution(call_id)
        if previous:
            if previous[0] == "done":
                return messages_from_dict([previous[1]])[0]
            return ToolMessage(
                content="Previous execution result is unknown. Inspect files/logs; "
                "this command was not repeated.",
                tool_call_id=call_id,
                id=f"result-{call_id}",
                status="error",
            )
        self.session.start_execution(call_id)
        try:
            result = await handler(request)
        except Exception as error:
            result = ToolMessage(
                content=f"{type(error).__name__}: {error}", tool_call_id=call_id, status="error"
            )
        result.id = result.id or f"result-{call_id}"
        self.session.finish_execution(call_id, message_to_dict(result))
        self.session.record_message(result)
        return result


class Harness:
    def __init__(self, session, processes, model, policy, saver, max_steps):
        self.session, self.processes = session, processes
        self.context = ContextMiddleware(session, model, policy)
        tools = create_tools(session, processes)
        tool_tokens = sum(len(json.dumps(t.args).encode()) // 3 + 100 for t in tools)
        self.execution = ExecutionMiddleware(session, processes, self.context, tool_tokens)
        self.graph = create_agent(
            model=model, tools=tools, checkpointer=saver, middleware=[self.execution, self.context]
        )
        self.config = {"configurable": {"thread_id": session.id}, "recursion_limit": max_steps}

    @classmethod
    @asynccontextmanager
    async def open(
        cls, cwd, directory, model, policy=None, env_file=None, max_steps=200, process_timeout=3600
    ):
        with Session(Path(directory), Path(cwd)) as session:
            async with ProcessManager(session, env_file, process_timeout) as processes:
                async with AsyncSqliteSaver.from_conn_string(str(session.database)) as saver:
                    harness = cls(
                        session, processes, model, policy or ContextPolicy(), saver, max_steps
                    )
                    try:
                        yield harness
                    finally:
                        await harness.context.close()

    def pending_results(self, messages):
        completed = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
        results = []
        for message in messages:
            if not isinstance(message, AIMessage):
                continue
            for call in message.tool_calls:
                if call["id"] in completed:
                    continue
                saved = self.session.execution(call["id"])
                if saved and saved[0] == "done":
                    result = messages_from_dict([saved[1]])[0]
                else:
                    result = ToolMessage(
                        content="Execution interrupted; result unknown. Inspect state before retry."
                        if saved
                        else "Call canceled before execution; it was not run.",
                        tool_call_id=call["id"],
                        id=f"result-{call['id']}",
                        status="error",
                    )
                results.append(result)
        return results

    async def stream(self, text: str | None):
        snapshot = await self.graph.aget_state(self.config)
        messages = self.pending_results(snapshot.values.get("messages", []))
        if text is not None:
            messages.append(HumanMessage(content=text, id=str(uuid4())))
        async for chunk, metadata in self.graph.astream(
            {"messages": messages},
            self.config,
            stream_mode="messages",
            durability="sync",
        ):
            if (
                metadata.get("langgraph_node") == "model"
                and isinstance(chunk, AIMessage)
                and chunk.text
            ):
                yield chunk.text

    async def compact(self, instructions="", start=None, end=None):
        snapshot = await self.graph.aget_state(self.config)
        messages = snapshot.values.get("messages", [])
        if not messages:
            raise ValueError("There are no messages to compact")
        if self.pending_results(messages):
            raise ValueError("Finish or recover interrupted tool calls before compacting")
        self.context.overhead = (
            estimate_tokens([self.execution.system_message()]) + self.execution.tool_tokens
        )
        result = await self.context.compact(messages, instructions, start, end)
        await self.graph.aupdate_state(
            self.config,
            {
                "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *result],
            },
            as_node="ContextMiddleware.before_model",
        )
        return result
