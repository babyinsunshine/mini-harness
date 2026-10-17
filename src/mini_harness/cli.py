"""A small CLI: stdin input, streamed text, and a few session commands."""

import argparse
import asyncio
import json
import os
import shlex
import signal
import sqlite3
import sys
import threading
from pathlib import Path
from uuid import uuid4

from langchain.chat_models import init_chat_model

from .agent import Harness
from .context import ContextPolicy


def parser():
    result = argparse.ArgumentParser(description="A minimal LangChain coding harness")
    result.add_argument("--model", default=os.getenv("HARNESS_MODEL"), help="provider:model")
    result.add_argument("--base-url", help="Optional provider API endpoint")
    result.add_argument("-p", "--prompt", help="Run one prompt and exit")
    result.add_argument("--cwd", type=Path, default=Path.cwd())
    result.add_argument(
        "--session-dir", type=Path, default=Path.home() / ".mini-harness" / "sessions"
    )
    result.add_argument("--resume", metavar="SESSION_ID")
    result.add_argument("--list-sessions", action="store_true")
    result.add_argument("--env-file", type=Path)
    result.add_argument(
        "--context-window",
        type=int,
        help="Model context window in tokens (default: conservative 128000)",
    )
    result.add_argument("--output-reserve", type=int)
    result.add_argument("--auto-compact", action=argparse.BooleanOptionalAction, default=None)
    result.add_argument(
        "--notes",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Maintain session notes in the background",
    )
    result.add_argument("--max-steps", type=int, help="LangGraph execution step limit")
    result.add_argument("--process-timeout", type=float, help="Maximum command runtime in seconds")
    return result


DEFAULTS = {
    "model": None,
    "base_url": None,
    "context_window": 128000,
    "output_reserve": 8192,
    "env_file": None,
    "notes": False,
    "auto_compact": True,
    "max_steps": 200,
    "process_timeout": 3600,
}


def resolve_settings(args, directory):
    saved = {}
    if args.resume:
        with sqlite3.connect(directory / "state.sqlite") as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key='config'").fetchone()
            saved = json.loads(row[0]) if row else {}
    for key, default in DEFAULTS.items():
        if getattr(args, key) is None:
            setattr(args, key, saved.get(key, default))


def sessions(directory):
    return sorted(path.parent.name for path in directory.glob("*/state.sqlite"))


async def show(harness, text):
    async for part in harness.stream(text):
        print(part, end="", flush=True)
    print()


async def command(harness, line, root):
    if line == "/tasks":
        for task in harness.processes.tasks.values():
            print(json.dumps(harness.processes.describe(task), ensure_ascii=False))
    elif line == "/sessions":
        print("\n".join(sessions(root)))
    elif line == "/help":
        print("/compact [--from N --to M] [focus]  /tasks  /sessions  /exit")
    elif line == "/compact" or line.startswith("/compact "):
        options = argparse.ArgumentParser(add_help=False, exit_on_error=False)
        options.add_argument("--from", dest="start", type=int)
        options.add_argument("--to", dest="end", type=int)
        scope, focus = options.parse_known_args(shlex.split(line)[1:])
        await harness.compact(" ".join(focus), scope.start, scope.end)
        print("Context compacted.")
    else:
        await show(harness, line)


async def interactive(harness, root):
    loop = asyncio.get_running_loop()
    incoming = asyncio.Queue()
    ready = threading.Event()
    current = None

    def read_input():
        # A daemon avoids an executor shutdown hanging on a blocked input().
        while True:
            ready.wait()
            ready.clear()
            try:
                line = input("> ")
            except EOFError:
                line = None
            try:
                loop.call_soon_threadsafe(incoming.put_nowait, line)
            except RuntimeError:
                return
            if line is None or line.strip() in {"/exit", "/quit"}:
                return

    def interrupt():
        if current is not None and not current.done():
            current.cancel()
        else:
            print("\nUse /exit or Ctrl-D to exit.", flush=True)

    threading.Thread(target=read_input, daemon=True).start()
    loop.add_signal_handler(signal.SIGINT, interrupt)
    user_input = None
    try:
        while True:
            if user_input is None:
                ready.set()
                user_input = asyncio.create_task(incoming.get())
            completion = asyncio.create_task(harness.processes.events.get())
            done, _ = await asyncio.wait(
                [user_input, completion], return_when=asyncio.FIRST_COMPLETED
            )
            if completion in done:
                harness.processes.events.put_nowait(completion.result())
            else:
                completion.cancel()
                await asyncio.gather(completion, return_exceptions=True)
            if user_input in done:
                line = user_input.result()
                user_input = None
                if line is None or line.strip() in {"/exit", "/quit"}:
                    break
                if not line.strip():
                    continue
                action = command(harness, line.strip(), root)
            else:
                action = show(harness, None)
            current = asyncio.create_task(action)
            try:
                await current
            except asyncio.CancelledError:
                print("\nInterrupted. Background tasks remain available in /tasks.")
            except Exception as error:
                print(f"Error: {error}", file=sys.stderr)
            finally:
                current = None
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        if user_input:
            user_input.cancel()
            await asyncio.gather(user_input, return_exceptions=True)


async def run(args, directory):
    settings = {"max_tokens": args.output_reserve}
    if args.base_url:
        settings["base_url"] = args.base_url
    model = init_chat_model(args.model, **settings)
    policy = ContextPolicy(
        window=args.context_window,
        reserve=args.output_reserve,
        notes=args.notes,
        auto=args.auto_compact,
    )
    async with Harness.open(
        args.cwd, directory, model, policy, args.env_file, args.max_steps, args.process_timeout
    ) as harness:
        harness.session.set("model", args.model)
        harness.session.set(
            "config",
            {
                key: str(getattr(args, key))
                if isinstance(getattr(args, key), Path)
                else getattr(args, key)
                for key in DEFAULTS
            },
        )
        print(
            f"Session: {harness.session.id}\nContext window: {policy.window} tokens",
            file=sys.stderr,
        )
        if args.prompt is not None:
            await show(harness, args.prompt)
        else:
            await interactive(harness, args.session_dir)


def main():
    argument_parser = parser()
    args = argument_parser.parse_args()
    if args.list_sessions:
        print("\n".join(sessions(args.session_dir)))
        return
    if args.resume and Path(args.resume).name != args.resume:
        argument_parser.error("--resume requires a session ID, not a path")
    directory = args.session_dir / (args.resume or str(uuid4()))
    if args.resume:
        database = directory / "state.sqlite"
        if not database.exists():
            argument_parser.error(f"Session does not exist: {args.resume}")
    resolve_settings(args, directory)
    if not args.model:
        argument_parser.error("Set --model provider:model or HARNESS_MODEL")
    try:
        asyncio.run(run(args, directory))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as error:
        argument_parser.exit(1, f"Error: {error}\n")
