"""子智能体只管理委派与生命周期；每轮执行仍复用 Harness。"""

import asyncio
import math
from contextvars import Context
from uuid import uuid4


class SubagentManager:
    def __init__(self, parent, max_steps, process_timeout):
        self.parent = parent
        self.session = parent.session
        self.max_steps, self.process_timeout = max_steps, process_timeout
        self.agents = self.session.get("subagents", {})
        self.tasks: dict[str, asyncio.Task] = {}
        for agent in self.agents.values():
            if agent["status"] == "running":
                agent["status"] = "interrupted"
        self._save()

    def _save(self):
        self.session.set("subagents", self.agents)

    def _agent(self, agent_id):
        if agent_id not in self.agents:
            raise ValueError(f"Unknown subagent: {agent_id}")
        return self.agents[agent_id]

    @staticmethod
    def _describe(agent):
        """只返回身份、状态和结果，内部任务原文不反复占用父上下文。"""
        return {key: agent[key] for key in (
            "agent_id", "parent_session_id", "session_dir", "status", "timeout", "result",
        )}

    @staticmethod
    def _validate(message, timeout):
        if not message.strip():
            raise ValueError("A subagent requires a nonempty task or message")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")

    def spawn(self, task: str, timeout: float = 600) -> dict:
        """新建子会话并立即返回；timeout 限制这一轮的总运行秒数。"""
        self._validate(task, timeout)
        agent_id = str(uuid4())
        self.agents[agent_id] = {
            "agent_id": agent_id,
            "parent_session_id": self.session.id,
            "session_dir": str(self.session.directory / "subagents" / agent_id),
            "cwd": str(self.session.cwd),
            "timeout": timeout,
            "input": task,
            "input_id": f"user-{uuid4()}",
        }
        return self._start(self.agents[agent_id])

    def continue_agent(self, agent_id: str, message: str, timeout: float | None = None) -> dict:
        """继续原对话；重新计时，缺省沿用上一轮的超时设置。"""
        agent = self._agent(agent_id)
        if agent["status"] in {"running", "closed"}:
            raise ValueError(f"Subagent is {agent['status']}")
        timeout = agent["timeout"] if timeout is None else timeout
        self._validate(message, timeout)
        agent.update(input=message, input_id=f"user-{uuid4()}", timeout=timeout)
        return self._start(agent)

    def _start(self, agent):
        agent.update(status="running", result="")
        self._save()
        # 不继承父图的 writer/callback 上下文，子输出只进入自己的图和会话。
        self.tasks[agent["agent_id"]] = asyncio.create_task(self._run(agent), context=Context())
        return self._describe(agent)

    async def _run(self, agent):
        from .agent import Harness

        try:
            async with asyncio.timeout(agent["timeout"]):
                async with Harness.open(
                    agent["cwd"], agent["session_dir"], self.parent.context.model,
                    self.parent.context.policy, max_steps=self.max_steps,
                    process_timeout=self.process_timeout, parent_session=self.session,
                    parent_permissions=self.parent.permissions,
                ) as child:
                    async for _ in child.events(agent["input"], input_id=agent["input_id"]):
                        pass
                    snapshot = await child.graph.aget_state(child.config)
                    result = snapshot.values["messages"][-1].text
            status = "waiting"
        except TimeoutError:
            status, result = "timed_out", "Subagent timed out; its processes were cleaned up."
        except asyncio.CancelledError:
            status = "closed" if agent["status"] == "closed" else "interrupted"
            result = "Execution interrupted. Continue this session to resume the conversation."
        except Exception as error:
            status, result = "failed", f"{type(error).__name__}: {error}"
        output = self.session.prepare_tool_output(result)
        agent.update(
            status=status,
            result=output["output"] if "output" in output else (
                f"{output['preview']}\nFull result: {output['output_path']}"
            ),
        )
        self._save()
        self.session.record("subagent", **agent)
        if status not in {"interrupted", "closed"}:
            self.parent.processes.completion_events.put_nowait(self._describe(agent))

    async def wait(self, agent_id: str) -> dict:
        """等待当前轮结束；已结束时直接返回保存的结果。"""
        agent = self._agent(agent_id)
        if task := self.tasks.get(agent_id):
            await asyncio.shield(task)
        return self._describe(agent)

    async def close_agent(self, agent_id: str) -> dict:
        """停止执行并关闭逻辑子智能体；保留全部会话文件。"""
        agent = self._agent(agent_id)
        agent["status"] = "closed"
        self._save()
        if (task := self.tasks.get(agent_id)) and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return self._describe(agent)

    def resume_interrupted(self):
        """启动原子会话；由 Harness 的持久化输入 ID 判断指令是否已被接收。"""
        for agent in self.agents.values():
            if agent["status"] == "interrupted":
                self._start(agent)

    async def interrupt(self):
        """退出或取消父轮次时，等待所有子任务清理资源并保存中断状态。"""
        running = []
        for agent_id, task in self.tasks.items():
            if not task.done():
                self.agents[agent_id]["status"] = "interrupted"
                running.append(task)
        self._save()
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
