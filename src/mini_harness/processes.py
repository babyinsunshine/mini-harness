"""One new Bash per call; live processes keep stdin and an output file."""

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .session import Session


@dataclass
class ProcessTask:
    id: str
    process: asyncio.subprocess.Process
    output: Path
    cwd_file: Path
    command: str
    watcher: asyncio.Task | None = None
    cleanup: asyncio.Task | None = None
    background: bool = False
    notified: bool = False
    reason: str | None = None


class ProcessManager:
    def __init__(self, session: Session, env_file: Path | None = None, max_runtime=3600):
        self.session = session
        self.env_file = str(Path(env_file).resolve()) if env_file else ""
        self.environment = dict(os.environ)
        self.max_runtime = max_runtime
        self.tasks: dict[str, ProcessTask] = {}
        self.events = asyncio.Queue()
        self._lock = asyncio.Lock()
        saved = self.session.get("processes", {})
        for record in saved.values():
            if record["status"] == "running":
                record.update(status="interrupted", reason="harness_restarted")
                self.events.put_nowait(record)
        self.session.set("processes", saved)

    async def run(self, command: str, timeout: float = 120, run_in_background: bool = False):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        async with self._lock:
            task_id = str(uuid4())
            output = self.session.artifacts / f"process-{task_id}.log"
            cwd_file = self.session.artifacts / f"process-{task_id}.cwd"
            with output.open("wb") as log, cwd_file.open("wb") as directory:
                # EXIT also runs for `exit N` and failed commands. Command text stays
                # a positional argument; metadata paths never become shell code.
                script = (
                    f"trap 'pwd -P >&{directory.fileno()}' EXIT\n"
                    'if [ -n "$1" ]; then source "$1" || exit $?; fi\n'
                    'eval -- "$2"'
                )
                process = await asyncio.create_subprocess_exec(
                    "bash",
                    "--noprofile",
                    "--norc",
                    "-c",
                    script,
                    "mini-harness",
                    self.env_file,
                    command,
                    cwd=self.session.cwd,
                    env=self.environment,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=log,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                    pass_fds=(directory.fileno(),),
                )
            task = ProcessTask(task_id, process, output, cwd_file, command)
            self.tasks[task_id] = task
            task.background = run_in_background
            task.watcher = asyncio.create_task(self._watch(task))
            if not run_in_background:
                try:
                    await asyncio.wait_for(asyncio.shield(task.watcher), timeout)
                except TimeoutError:
                    task.background = True
                    if task.watcher.done():
                        self._notify(task)
                except asyncio.CancelledError:
                    await self._terminate(task)
                    await task.watcher
                    raise
                else:
                    if cwd_file.exists():
                        directory_name = cwd_file.read_text().rstrip("\n")
                        if directory_name and Path(directory_name).is_dir():
                            self.session.cwd = Path(directory_name)
            result = self.describe(task)
            self._persist(result)
            self.session.record("process", **result, command=command)
            return result

    async def _watch(self, task: ProcessTask):
        try:
            await asyncio.wait_for(task.process.wait(), self.max_runtime)
        except TimeoutError:
            task.reason = "runtime_limit"
            await self._terminate(task)
        finally:
            # A shell can exit while background children keep its process group alive.
            await self._terminate(task)
            if task.process.stdin:
                task.process.stdin.close()
            if task.background:
                self._notify(task)

    def _notify(self, task: ProcessTask):
        if not task.notified:
            result = self.describe(task)
            self._persist(result)
            self.events.put_nowait(result)
            task.notified = True

    def _persist(self, result):
        records = self.session.get("processes", {})
        records[result["task_id"]] = result
        self.session.set("processes", records)

    def describe(self, task: ProcessTask) -> dict:
        with task.output.open("rb") as stream:
            preview = stream.read(2000).decode("utf-8", errors="replace")
        return {
            "task_id": task.id,
            "status": "running" if task.process.returncode is None else "exited",
            "exit_code": task.process.returncode,
            "output_path": str(task.output),
            "preview": preview,
            "reason": task.reason,
        }

    async def send_input(self, task_id: str, text: str = "", eof: bool = False):
        task = self.tasks[task_id]
        stdin = task.process.stdin
        if task.process.returncode is not None or stdin is None or stdin.is_closing():
            raise ValueError("Process has exited or stdin is closed")
        try:
            stdin.write(text.encode())
            await stdin.drain()
            if eof:
                stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            raise ValueError("Process has exited or stdin is closed") from None
        return self.describe(task)

    async def _terminate(self, task: ProcessTask):
        # Clean a group exactly once, while its ownership is current. A completed
        # task must not later signal an unrelated process group after PID reuse.
        if task.cleanup is None:
            task.cleanup = asyncio.create_task(self._kill_group(task))
        await asyncio.shield(task.cleanup)

    async def _kill_group(self, task: ProcessTask):
        try:
            os.killpg(task.process.pid, signal.SIGTERM)
            await asyncio.sleep(0.1)
            # The shell may exit before its children; kill the group either way.
            os.killpg(task.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await task.process.wait()

    async def stop(self, task_id: str):
        task = self.tasks[task_id]
        task.reason = "stopped"
        await self._terminate(task)
        await task.watcher
        return self.describe(task)

    def drain_events(self) -> list[dict]:
        result = []
        while not self.events.empty():
            result.append(self.events.get_nowait())
        return result

    async def close(self):
        for task in self.tasks.values():
            await self._terminate(task)
        if self.tasks:
            await asyncio.gather(*(task.watcher for task in self.tasks.values()))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()
