"""斜杠命令与聊天入口分派；不参与输入监听或审批。"""

import argparse
import json
import shlex

from .config import list_sessions
from .display import display_events
from .usage import format_session_usage


def command_name(user_input):
    """只识别已有终端命令；以 / 开头的文件路径等仍然是聊天。"""
    if user_input in {"/tasks", "/stats", "/sessions", "/help"}:
        return user_input
    if user_input == "/compact" or user_input.startswith("/compact "):
        return "/compact"
    return None


async def handle_input(harness, user_input, sessions_dir):
    """处理斜杠命令或聊天输入；sessions_dir 是所有会话的存放目录。"""
    command = command_name(user_input)
    if command == "/tasks":
        for task in harness.processes.tasks.values():
            print(json.dumps(harness.processes.describe_task(task), ensure_ascii=False))
        if harness.subagents is not None:
            for agent in harness.subagents.agents.values():
                print(json.dumps({key: agent[key] for key in (
                    "agent_id", "status", "session_dir"
                )}, ensure_ascii=False))
    elif command == "/stats":
        print(format_session_usage(harness.session))
    elif command == "/sessions":
        print("\n".join(list_sessions(sessions_dir)))
    elif command == "/help":
        print("/compact [--from N --to M] [focus]  /tasks  /stats  /sessions  /exit")
    elif command == "/compact":
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
