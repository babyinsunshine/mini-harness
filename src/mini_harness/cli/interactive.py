"""终端交互状态与调度；只有一个线程读取 stdin。"""

import asyncio
import json
import signal
import sys
import threading
from collections import deque
from time import monotonic

from ..responses import format_error
from .commands import command_name, handle_input
from .display import display_events


async def run_interactive(harness, sessions_dir):
    """保留简单入口，交互期间的状态由一个 CLI 实例持有。"""
    await InteractiveCLI(harness, sessions_dir).run()


class InteractiveCLI:
    """持有一次终端交互的状态；工具执行与会话存储仍交给 Harness。"""

    def __init__(self, harness, sessions_dir):
        self.harness, self.sessions_dir = harness, sessions_dir
        self.loop = None
        self.input_queue, self.approval_queue = asyncio.Queue(), asyncio.Queue()
        self.input_task = self.approval_task = self.turn_task = None
        self.active_approval = None
        self.queued_inputs = deque()
        self.paused = False
        self.exiting = False

    async def run(self):
        """启动监听，协调空闲与运行状态，退出时统一释放监听资源。"""
        self.loop = asyncio.get_running_loop()
        try:
            self.input_task = asyncio.create_task(self.input_queue.get())
            self.approval_task = asyncio.create_task(self.approval_queue.get())
            self.harness.permissions.approve = self.approve
            self.loop.add_signal_handler(signal.SIGINT, self.interrupt)
            threading.Thread(
                target=self._read_stdin, args=(self.loop, self.input_queue), daemon=True,
            ).start()
            while not self.exiting:
                kind, value = await self.wait_for_activity()
                if kind == "approval":
                    await self.handle_approval(value)
                    continue
                if kind == "input":
                    if value is None:
                        break
                    user_input = value.strip()
                    if user_input in {"/exit", "/quit"}:
                        break
                    if not user_input:
                        continue
                    if command_name(user_input) is None:
                        self.paused = False
                    turn = handle_input(self.harness, user_input, self.sessions_dir)
                else:
                    if self.paused:
                        continue
                    turn = display_events(self.harness, None)
                try:
                    await self.run_turn(turn)
                except asyncio.CancelledError:
                    self.paused = True
                    if not self.exiting:
                        print(
                            "\nInterrupted. Send a message to continue; background tasks: /tasks."
                        )
                except Exception as error:
                    print(f"Error: {format_error(error)}", file=sys.stderr)
        finally:
            await self.close()

    async def wait_for_activity(self):
        """返回 (input, 文本/EOF)、(approval, 审批请求) 或 (background, None)。"""
        if self.queued_inputs:
            return "input", self.queued_inputs.popleft()
        print("> ", end="", file=sys.stderr, flush=True)
        # 暂停时不监听完成通知，留到下一次聊天请求一起处理。
        completion_task = (
            None if self.paused
            else asyncio.create_task(self.harness.processes.completion_events.get())
        )
        waiters = [self.input_task, self.approval_task]
        if completion_task is not None:
            waiters.append(completion_task)
        try:
            done, _ = await asyncio.wait(
                waiters, return_when=asyncio.FIRST_COMPLETED,
            )
            if self.approval_task in done:
                return "approval", self._consume_approval()
            if self.input_task in done:
                return "input", self._consume_input()[1]
            return "background", None
        finally:
            if completion_task is not None:
                # 已完成的读取同步回队；多一次 await 就可能被取消，丢掉已取出的通知。
                if not completion_task.done():
                    completion_task.cancel()
                    await asyncio.gather(completion_task, return_exceptions=True)
                # 这里只唤醒主循环，通知内容仍由图收集。
                if not completion_task.cancelled():
                    self.harness.processes.completion_events.put_nowait(completion_task.result())

    async def run_turn(self, turn_coroutine):
        """执行一轮并响应审批；取消后等待图和子任务完成收尾。"""
        self.turn_task = asyncio.create_task(turn_coroutine)
        try:
            while not self.turn_task.done():
                done, _ = await asyncio.wait(
                    [self.turn_task, self.approval_task], return_when=asyncio.FIRST_COMPLETED,
                )
                if self.turn_task in done:
                    break
                await self.handle_approval(self._consume_approval())
                if self.exiting or self.turn_task.done():
                    break
            await self.turn_task
        except asyncio.CancelledError:
            # Task 可能尚未进入 Harness；只清理仍在运行的子任务，避免重复收尾。
            manager = self.harness.subagents
            if manager is not None and any(not task.done() for task in manager.tasks.values()):
                await manager.interrupt()
            raise
        finally:
            if not self.turn_task.done():
                self.turn_task.cancel()
            await asyncio.gather(self.turn_task, return_exceptions=True)
            self.turn_task = None

    async def approve(self, tool_call, reason):
        """供权限模块调用：将审批交给终端，等待一次允许或拒绝。"""
        approval_future = self.loop.create_future()
        await self.approval_queue.put((tool_call, reason, approval_future))
        return await approval_future

    async def handle_approval(self, request):
        """主轮运行和空闲时共用；输入按到达时间区分聊天与审批回答。"""
        tool_call, reason, approval_future = request
        if approval_future.done():
            return
        tool_args = tool_call["args"]
        target = tool_args.get(
            "command", tool_args.get("path", json.dumps(tool_args, ensure_ascii=False))
        )
        source = f" (subagent {tool_call['agent_id']})" if "agent_id" in tool_call else ""
        print(f"\nApproval{source}: {tool_call['name']}\n{target}\n{reason}", file=sys.stderr)
        sandbox_request = tool_call.get("sandbox_request", False)
        if sandbox_request:
            prompt = "Allow? [y=allow/N=deny] "
        else:
            print(
                "Session trust skips ordinary prompts; sandbox boundaries still apply."
                if self.harness.session.sandbox is not None else
                "Session trust includes future commands and outside-project access.",
                file=sys.stderr,
            )
            prompt = "Allow? [y=once/a=trust session/N=deny] "
        print(prompt, end="", file=sys.stderr, flush=True)
        approval_requested_at = monotonic()
        self.active_approval = approval_future
        try:
            while not approval_future.done():
                done, _ = await asyncio.wait(
                    [approval_future, self.input_task], return_when=asyncio.FIRST_COMPLETED,
                )
                if approval_future in done:
                    break
                received_at, user_input = self._consume_input()
                if user_input is None or user_input.strip() in {"/exit", "/quit"}:
                    self.exiting = True
                    approval_future.cancel()
                    if self.turn_task is not None:
                        self.turn_task.cancel()
                    break
                if received_at < approval_requested_at:
                    self.queued_inputs.append(user_input)
                    continue
                decision = user_input.strip().lower()
                if decision == "a" and not sandbox_request:
                    self.harness.permissions.session_trusted = True
                    print(
                        "Session trusted until exit; explicit deny rules still apply.",
                        file=sys.stderr,
                    )
                approval_future.set_result(
                    decision in {"y", "yes"} or (decision == "a" and not sandbox_request)
                )
        finally:
            self.active_approval = None

    def interrupt(self):
        """Ctrl-C 暂停自动继续；重复按键不取消正在进行的清理。"""
        self.paused = True
        if self.turn_task is not None and not self.turn_task.done():
            if not self.turn_task.cancelling():
                self.turn_task.cancel()
        elif self.active_approval is not None:
            self.active_approval.cancel()
        else:
            print("\nPaused. Send a message to continue; /exit to quit.", flush=True)

    async def close(self):
        """释放输入/审批监听并移除 CLI 设置的回调；图与进程由各自所有者清理。"""
        self.harness.permissions.approve = None
        self.harness.permissions.session_trusted = False
        if self.loop is not None:
            self.loop.remove_signal_handler(signal.SIGINT)
        tasks = [task for task in (self.input_task, self.approval_task) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _read_stdin(loop, input_queue):
        # 唯一 stdin 读取者；时间戳防止模型运行期间输入的聊天被当成未来审批。
        while True:
            try:
                user_input = input()
            except EOFError:
                user_input = None
            try:
                loop.call_soon_threadsafe(input_queue.put_nowait, (monotonic(), user_input))
            except RuntimeError:
                return
            if user_input is None or user_input.strip() in {"/exit", "/quit"}:
                return

    def _consume_input(self):
        item = self.input_task.result()
        self.input_task = asyncio.create_task(self.input_queue.get())
        return item

    def _consume_approval(self):
        request = self.approval_task.result()
        self.approval_task = asyncio.create_task(self.approval_queue.get())
        return request
