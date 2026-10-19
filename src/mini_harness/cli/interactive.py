"""终端输入、会话命令、审批与中断；只有一个线程读取 stdin。"""

import argparse
import asyncio
import json
import shlex
import signal
import sys
import threading
from time import monotonic

from ..responses import format_error
from .config import list_sessions
from .display import display_events


async def handle_input(harness, user_input, sessions_dir):
    """处理斜杠命令或聊天输入；sessions_dir 是所有会话的存放目录。"""
    if user_input == "/tasks":
        for task in harness.processes.tasks.values():
            print(json.dumps(harness.processes.describe_task(task), ensure_ascii=False))
    elif user_input == "/sessions":
        print("\n".join(list_sessions(sessions_dir)))
    elif user_input == "/help":
        print("/compact [--from N --to M] [focus]  /tasks  /sessions  /exit")
    elif user_input == "/compact" or user_input.startswith("/compact "):
        options = argparse.ArgumentParser(add_help=False, exit_on_error=False)
        options.add_argument("--from", dest="start_group", type=int)
        options.add_argument("--to", dest="end_group", type=int)
        compact_args, summary_instructions = options.parse_known_args(shlex.split(user_input)[1:])
        await harness.compact(
            " ".join(summary_instructions), compact_args.start_group, compact_args.end_group
        )
        print("Context compacted.")
    else:
        await display_events(harness, user_input)


async def run_interactive(harness, sessions_dir):
    """一个输入线程；主循环协调聊天、审批、后台事件和当前 agent 轮次。"""
    loop = asyncio.get_running_loop()
    input_queue, approval_queue = asyncio.Queue(), asyncio.Queue()
    agent_task = None
    input_task = asyncio.create_task(input_queue.get())
    pending_inputs = []
    exiting = False

    def read_stdin():
        # 持续读取并记录到达时间，模型思考期间的输入仍然是聊天，不是未来的审批。
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

    def consume_input():
        nonlocal input_task
        item = input_task.result()
        input_task = asyncio.create_task(input_queue.get())
        return item

    def interrupt():
        if agent_task is not None and not agent_task.done():
            agent_task.cancel()
        else:
            print("\nUse /exit or Ctrl-D to exit.", flush=True)

    async def approve(tool_call, reason):
        """将审批交给终端主循环，等待一次允许或拒绝的布尔结果。"""
        approval_future = loop.create_future()
        await approval_queue.put((tool_call, reason, approval_future))
        return await approval_future

    async def run_turn(turn_coroutine):
        """执行一轮，同时处理审批；获准前工具仍等待在 permission 节点。"""
        nonlocal agent_task, exiting
        agent_task = asyncio.create_task(turn_coroutine)
        approval_task = asyncio.create_task(approval_queue.get())
        try:
            while not agent_task.done():
                done, _ = await asyncio.wait(
                    [agent_task, approval_task], return_when=asyncio.FIRST_COMPLETED
                )
                if agent_task in done:
                    break
                tool_call, reason, approval_future = approval_task.result()
                approval_task = asyncio.create_task(approval_queue.get())
                if approval_future.done():
                    continue
                tool_args = tool_call["args"]
                target = tool_args.get(
                    "command", tool_args.get("path", json.dumps(tool_args, ensure_ascii=False))
                )
                print(f"\nApproval: {tool_call['name']}\n{target}\n{reason}", file=sys.stderr)
                print(
                    "Session trust includes future commands and outside-project access.",
                    file=sys.stderr,
                )
                print(
                    "Allow? [y=once/a=trust session/N=deny] ",
                    end="", file=sys.stderr, flush=True,
                )
                approval_requested_at = monotonic()
                while not approval_future.done():
                    done, _ = await asyncio.wait(
                        [agent_task, input_task], return_when=asyncio.FIRST_COMPLETED
                    )
                    if agent_task in done:
                        approval_future.cancel()
                        break
                    received_at, user_input = consume_input()
                    if user_input is None or user_input.strip() in {"/exit", "/quit"}:
                        exiting = True
                        agent_task.cancel()
                        break
                    if received_at < approval_requested_at:
                        pending_inputs.append(user_input)
                        continue
                    decision = user_input.strip().lower()
                    if decision == "a":
                        harness.permissions.session_trusted = True
                        print(
                            "Session trusted until exit; explicit deny rules still apply.",
                            file=sys.stderr,
                        )
                    approval_future.set_result(decision in {"y", "yes", "a"})
                if exiting or agent_task.done():
                    break
            await agent_task
        finally:
            approval_task.cancel()
            if not agent_task.done():
                agent_task.cancel()
            await asyncio.gather(approval_task, agent_task, return_exceptions=True)
            agent_task = None

    harness.permissions.approve = approve
    threading.Thread(target=read_stdin, daemon=True).start()
    loop.add_signal_handler(signal.SIGINT, interrupt)
    try:
        while not exiting:
            background_trigger = False
            if pending_inputs:
                user_input = pending_inputs.pop(0)
            else:
                print("> ", end="", file=sys.stderr, flush=True)
                completion_task = asyncio.create_task(harness.processes.completion_events.get())
                done, _ = await asyncio.wait(
                    [input_task, completion_task], return_when=asyncio.FIRST_COMPLETED
                )
                if completion_task in done:
                    harness.processes.completion_events.put_nowait(completion_task.result())
                else:
                    completion_task.cancel()
                    await asyncio.gather(completion_task, return_exceptions=True)
                if input_task in done:
                    _, user_input = consume_input()
                else:
                    user_input = None  # 后台通知单独触发一轮，不构造用户消息。
                    background_trigger = True
            if user_input is None and not background_trigger:
                break
            if user_input is not None and user_input.strip() in {"/exit", "/quit"}:
                break
            if user_input is not None and not user_input.strip():
                continue
            turn_coroutine = (
                display_events(harness, None) if user_input is None
                else handle_input(harness, user_input.strip(), sessions_dir)
            )
            try:
                await run_turn(turn_coroutine)
            except asyncio.CancelledError:
                if not exiting:
                    print("\nInterrupted. Background tasks remain available in /tasks.")
            except Exception as error:
                print(f"Error: {format_error(error)}", file=sys.stderr)
    finally:
        harness.permissions.approve = None
        harness.permissions.session_trusted = False
        loop.remove_signal_handler(signal.SIGINT)
        input_task.cancel()
        await asyncio.gather(input_task, return_exceptions=True)
