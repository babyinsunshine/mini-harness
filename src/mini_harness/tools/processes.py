"""One new Bash per call; live processes keep stdin and an output file."""

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ..session import Session

MODEL_CREDENTIALS = {
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
    "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_AD_TOKEN",
}


@dataclass
class ProcessTask:
    id: str
    process: asyncio.subprocess.Process
    output_path: Path
    cwd_file: Path
    command: str
    watch_task: asyncio.Task | None = None
    cleanup_task: asyncio.Task | None = None
    background: bool = False
    notified: bool = False
    reason: str | None = None


class ProcessManager:
    def __init__(self, session: Session, max_runtime_seconds=3600):
        """max_runtime_seconds 限制进程总运行时间，包含转入后台后的时间。"""
        self.session = session
        self.env_file = session.directory / "env.sh"
        self.env_file.touch(exist_ok=True)
        # 模型凭据留在父进程；env.sh 仍可显式传入项目所需变量。
        self.environment = {
            key: value for key, value in os.environ.items() if key not in MODEL_CREDENTIALS
        }
        self.max_runtime_seconds = max_runtime_seconds
        self.tasks: dict[str, ProcessTask] = {}
        self.completion_events = asyncio.Queue()
        self._run_lock = asyncio.Lock()
        saved_tasks = self.session.get("processes", {})
        for record in saved_tasks.values():
            if record["status"] == "running":
                record.update(status="interrupted", reason="harness_restarted")
                self.completion_events.put_nowait(record)
        self.session.set("processes", saved_tasks)

    async def run(self, command: str, timeout: float = 120, run_in_background: bool = False):
        """timeout 是前台等待秒数；超时或 run_in_background=True 时保留后台进程。"""
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        async with self._run_lock:
            task_id = str(uuid4())
            output_path = self.session.artifacts / f"process-{task_id}.log"
            cwd_file = self.session.artifacts / f"process-{task_id}.cwd"
            with output_path.open("wb") as output_stream, cwd_file.open("wb") as cwd_stream:
                # EXIT also runs for `exit N` and failed commands. Command text stays
                # a positional argument; metadata paths never become shell code.
                script = (
                    f"trap 'pwd -P >&{cwd_stream.fileno()}' EXIT\n"
                    'source "$1" || exit $?\n'
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
                    stdout=output_stream,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                    pass_fds=(cwd_stream.fileno(),),
                )
            task = ProcessTask(task_id, process, output_path, cwd_file, command)
            self.tasks[task_id] = task
            task.background = run_in_background
            task.watch_task = asyncio.create_task(self._watch_process(task))
            if not run_in_background:
                try:
                    await asyncio.wait_for(asyncio.shield(task.watch_task), timeout)
                except TimeoutError:
                    task.background = True
                    if task.watch_task.done():
                        self._notify_completion(task)
                except asyncio.CancelledError:
                    await self._terminate(task)
                    await task.watch_task
                    raise
                else:
                    if cwd_file.exists():
                        new_cwd = cwd_file.read_text().rstrip("\n")
                        if new_cwd and Path(new_cwd).is_dir():
                            self.session.cwd = Path(new_cwd)
            result = self.describe_task(task)
            self._save_task_state(result)
            self.session.record("process", **result, command=command)
            return result

    async def _watch_process(self, task: ProcessTask):
        """等待子进程结束或达到总时限，再清理进程组并通知后台完成。"""
        try:
            await asyncio.wait_for(task.process.wait(), self.max_runtime_seconds)
        except TimeoutError:
            task.reason = "runtime_limit"
            await self._terminate(task)
        finally:
            # A shell can exit while background children keep its process group alive.
            await self._terminate(task)
            if task.process.stdin:
                task.process.stdin.close()
            if task.background:
                self._notify_completion(task)

    def _notify_completion(self, task: ProcessTask):
        if not task.notified:
            result = self.describe_task(task)
            self._save_task_state(result)
            self.completion_events.put_nowait(result)
            task.notified = True

    def _save_task_state(self, result):
        records = self.session.get("processes", {})
        records[result["task_id"]] = result
        self.session.set("processes", records)

    def describe_task(self, task: ProcessTask) -> dict:
        """从进程句柄和日志生成当前任务状态，不等待任务完成。"""
        with task.output_path.open("rb") as stream:
            output_bytes = os.fstat(stream.fileno()).st_size
            preview = stream.read(output_bytes if output_bytes <= 2000 else 500)
            if output_bytes > 2000:
                # 保留开头的命令信息，以及尾部常见的报错和测试结论。
                stream.seek(output_bytes - 1500)
                preview += b"\n[... middle omitted; see output_path ...]\n" + stream.read(1500)
        return {
            "task_id": task.id,
            "status": "running" if task.process.returncode is None else "exited",
            "exit_code": task.process.returncode,
            "output_path": str(task.output_path),
            "preview": preview.decode("utf-8", errors="replace"),
            "truncated": output_bytes > 2000,
            "output_bytes": output_bytes,
            "reason": task.reason,
        }

    async def send_input(self, task_id: str, text: str = "", eof: bool = False):
        """向 task_id 对应进程的 stdin 写入 text；eof=True 时随后关闭输入。"""
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
        return self.describe_task(task)

    async def _terminate(self, task: ProcessTask):
        # Clean a group exactly once, while its ownership is current. A completed
        # task must not later signal an unrelated process group after PID reuse.
        if task.cleanup_task is None:
            task.cleanup_task = asyncio.create_task(self._kill_group(task))
        await asyncio.shield(task.cleanup_task)

    async def _kill_group(self, task: ProcessTask):
        try:
            os.killpg(task.process.pid, signal.SIGTERM)
            await asyncio.sleep(0.1)
            # The shell may exit before its children; kill the group either way.
            os.killpg(task.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await task.process.wait()

    async def stop_task(self, task_id: str):
        """停止本会话的任务及子进程，等待清理完成并返回最终状态。"""
        task = self.tasks[task_id]
        task.reason = "stopped"
        await self._terminate(task)
        await task.watch_task
        return self.describe_task(task)

    def drain_events(self) -> list[dict]:
        result = []
        while not self.completion_events.empty():
            result.append(self.completion_events.get_nowait())
        return result

    async def close(self):
        for task in self.tasks.values():
            await self._terminate(task)
        if self.tasks:
            await asyncio.gather(*(task.watch_task for task in self.tasks.values()))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        # 超时或再次 Ctrl-C 可以取消执行，但必须等所有进程清理后再关闭 Session。
        cleanup = asyncio.create_task(self.close())
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        await cleanup
        if cancelled:
            raise asyncio.CancelledError
