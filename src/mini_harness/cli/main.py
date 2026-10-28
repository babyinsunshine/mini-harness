"""CLI 启动入口：解析配置、创建模型、打开会话。"""

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

from langchain.chat_models import init_chat_model

from ..agent import Harness
from ..context.policy import ContextPolicy
from ..responses import format_error
from .config import DEFAULTS, build_parser, list_sessions, resolve_settings
from .display import display_events
from .interactive import run_interactive


async def run_cli(args, session_dir):
    """根据 CLI 配置打开单个会话，执行一次输入或进入交互模式。"""
    model_settings = {"max_tokens": args.output_reserve}
    if args.base_url:
        model_settings["base_url"] = args.base_url
    model = init_chat_model(args.model, **model_settings)
    if hasattr(model, "stream_usage"):
        model.stream_usage = True
    policy = ContextPolicy(
        window=args.context_window,
        reserve=args.output_reserve,
        micro_keep_rounds=args.micro_keep_rounds,
        micro_keep_tokens=args.micro_keep_tokens,
        micro_min_savings=args.micro_min_savings,
        micro_idle_seconds=args.micro_idle_seconds,
        auto=args.auto_compact,
    )
    async with Harness.open(
        args.cwd, session_dir, model, policy,
        max_steps=args.max_steps, process_timeout=args.process_timeout,
        full_access=args.full_access,
        mcp_config=args.mcp_config,
        sandbox=args.sandbox, sandbox_config=args.sandbox_config,
    ) as harness:
        harness.session.set(
            "config",
            {key: getattr(args, key) for key in DEFAULTS},
        )
        print(
            f"Session: {harness.session.id}\nContext window: {policy.window} tokens\n"
            f"Permissions: {'full access' if args.full_access else 'standard'}",
            file=sys.stderr,
        )
        if harness.session.sandbox is not None:
            print(f"Sandbox: enabled ({harness.session.get('project_root')})", file=sys.stderr)
        if args.prompt is not None:
            await display_events(harness, args.prompt)
        else:
            await run_interactive(harness, args.session_dir)


def main():
    cli_parser = build_parser()
    args = cli_parser.parse_args()
    if args.list_sessions:
        print("\n".join(list_sessions(args.session_dir)))
        return
    if args.resume and Path(args.resume).name != args.resume:
        cli_parser.error("--resume requires a session ID, not a path")
    session_dir = args.session_dir / (args.resume or str(uuid4()))
    if args.resume and not (session_dir / "state.sqlite").exists():
        cli_parser.error(f"Session does not exist: {args.resume}")
    resolve_settings(args, session_dir)
    if not args.model:
        cli_parser.error("Set --model provider:model or HARNESS_MODEL")
    try:
        asyncio.run(run_cli(args, session_dir))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as error:
        cli_parser.exit(1, f"Error: {format_error(error)}\n")
