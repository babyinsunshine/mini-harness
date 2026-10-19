"""CLI 参数与会话配置恢复；上下文默认值统一来自 ContextPolicy。"""

import argparse
import json
import os
import sqlite3
from pathlib import Path

from ..context.policy import ContextPolicy


def build_parser():
    """定义命令行参数；恢复会话后的默认值由 resolve_settings 填入。"""
    cli_parser = argparse.ArgumentParser(description="A minimal LangGraph coding harness")
    cli_parser.add_argument("--model", default=os.getenv("HARNESS_MODEL"), help="provider:model")
    cli_parser.add_argument("--base-url", help="Optional provider API endpoint")
    cli_parser.add_argument("-p", "--prompt", help="Run one prompt and exit")
    cli_parser.add_argument("--cwd", type=Path, default=Path.cwd())
    cli_parser.add_argument(
        "--session-dir", type=Path, default=Path.home() / ".mini-harness" / "sessions"
    )
    cli_parser.add_argument("--resume", metavar="SESSION_ID")
    cli_parser.add_argument("--list-sessions", action="store_true")
    cli_parser.add_argument(
        "--context-window",
        type=int,
        help="Model context window in tokens (default: conservative 128000)",
    )
    cli_parser.add_argument("--output-reserve", type=int)
    cli_parser.add_argument(
        "--micro-keep-rounds", type=int, help="Recent complete tool groups to protect"
    )
    cli_parser.add_argument("--micro-keep-tokens", type=int, help="Recent tool-output token budget")
    cli_parser.add_argument(
        "--micro-min-savings", type=int, help="Minimum tokens saved by a Micro batch"
    )
    cli_parser.add_argument(
        "--micro-idle-seconds", type=float, help="Request gap before stronger Micro pruning"
    )
    cli_parser.add_argument("--auto-compact", action=argparse.BooleanOptionalAction, default=None)
    cli_parser.add_argument("--max-steps", type=int, help="Graph node step limit (default: 1000)")
    cli_parser.add_argument(
        "--process-timeout", type=float, help="Maximum command runtime in seconds"
    )
    return cli_parser


DEFAULTS = {
    "model": None,
    "base_url": None,
    "context_window": ContextPolicy.window,
    "output_reserve": ContextPolicy.reserve,
    "micro_keep_rounds": ContextPolicy.micro_keep_rounds,
    "micro_keep_tokens": ContextPolicy.micro_keep_tokens,
    "micro_min_savings": ContextPolicy.micro_min_savings,
    "micro_idle_seconds": ContextPolicy.micro_idle_seconds,
    "auto_compact": ContextPolicy.auto,
    "max_steps": 1000,
    "process_timeout": 3600,
}


def resolve_settings(args, session_dir):
    """按显式参数、会话保存值、默认值的优先级，补齐 args 中的配置。"""
    saved_settings = {}
    if args.resume:
        with sqlite3.connect(session_dir / "runtime.sqlite") as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key='config'").fetchone()
            saved_settings = json.loads(row[0]) if row else {}
    for key, default in DEFAULTS.items():
        if getattr(args, key) is None:
            setattr(args, key, saved_settings.get(key, default))


def list_sessions(sessions_dir):
    """列出 sessions_dir 下已存在检查点的会话 ID。"""
    return sorted(path.parent.name for path in sessions_dir.glob("*/state.sqlite"))
