import asyncio
import shlex
from pathlib import Path

import pytest

from mini_harness.session import Session
from mini_harness.tools.processes import ProcessManager


async def test_bash_filters_model_keys_but_keeps_project_environment(tmp_path, monkeypatch):
    keys = (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
        "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_AD_TOKEN",
    )
    for key in keys:
        monkeypatch.setenv(key, "fake-model-key")
    monkeypatch.setenv("PROJECT_VALUE", "project")
    with Session(tmp_path / "s", tmp_path) as session:
        async with ProcessManager(session) as manager:
            variables = "".join(f"${{{key}-}}" for key in keys)
            result = await manager.run(f'printf "%s|%s" "{variables}" "$PROJECT_VALUE"')
            assert result["preview"] == "|project"
            # 有意写入 env.sh 的项目配置仍生效。
            manager.env_file.write_text("export OPENAI_API_KEY=explicit-project-value\n")
            result = await manager.run('printf %s "$OPENAI_API_KEY"')
            assert result["preview"] == "explicit-project-value"


async def test_long_log_preview_keeps_start_and_final_error(tmp_path):
    with Session(tmp_path / "s", tmp_path) as session:
        async with ProcessManager(session) as processes:
            result = await processes.run(
                "python3 -c \"print('BEGIN' + 'x' * 4000 + 'FINAL ERROR')\""
            )
            assert "BEGIN" in result["preview"] and "FINAL ERROR" in result["preview"]
            assert result["truncated"] is True
            assert result["output_bytes"] > 4000


async def test_wait_task_queries_times_out_then_returns_completion(tmp_path):
    with Session(tmp_path / "s", tmp_path) as session:
        async with ProcessManager(session) as processes:
            started = await processes.run("cat", run_in_background=True)
            task_id = started["task_id"]
            assert (await processes.wait_task(task_id, timeout=0))["status"] == "running"
            assert (await processes.wait_task(task_id, timeout=0.01))["status"] == "running"
            assert processes.tasks[task_id].process.returncode is None
            await processes.send_input(task_id, "finished\n", eof=True)
            result = await processes.wait_task(task_id, timeout=2)
            assert result["status"] == "exited" and result["exit_code"] == 0
            assert result["preview"] == "finished\n"
            assert len(processes.drain_events()) == 1


async def test_cancel_wait_does_not_cancel_process_watcher(tmp_path):
    with Session(tmp_path / "s", tmp_path) as session:
        async with ProcessManager(session) as processes:
            result = await processes.run("cat", run_in_background=True)
            task_id = result["task_id"]
            waiting = asyncio.create_task(processes.wait_task(task_id, timeout=30))
            await asyncio.sleep(0)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            assert not processes.tasks[task_id].watch_task.done()
            await processes.send_input(task_id, "still alive", eof=True)
            assert (await processes.wait_task(task_id, timeout=2))["preview"] == "still alive"


async def test_output_limit_can_reveal_middle_and_wait_uses_the_same_limit(tmp_path):
    with Session(tmp_path / "s", tmp_path) as session:
        async with ProcessManager(session) as processes:
            command = "python3 -c \"print('BEGIN' + 'x' * 2000 + 'MIDDLE' + 'y' * 2000 + 'END')\""
            small = await processes.run(command)
            assert "MIDDLE" not in small["preview"] and small["truncated"]
            large = await processes.wait_task(small["task_id"], timeout=0, max_output_bytes=10000)
            assert "MIDDLE" in large["preview"] and not large["truncated"]
            assert large["preview"] == Path(large["output_path"]).read_text()
            assert not (await processes.run(command, max_output_bytes=10000))["truncated"]


async def test_preview_remains_bounded_if_log_grows_during_read(tmp_path, monkeypatch):
    import os

    with Session(tmp_path / "s", tmp_path) as session:
        async with ProcessManager(session) as processes:
            result = await processes.run("python3 -c \"print('x' * 4000)\"")
            output = Path(result["output_path"])
            original_fstat = os.fstat

            def append_after_stat(fd):
                snapshot = original_fstat(fd)
                with output.open("ab") as stream:
                    stream.write(b"y" * 20000)
                return snapshot

            with monkeypatch.context() as patch:
                patch.setattr(os, "fstat", append_after_stat)
                shown = processes.describe_task(processes.tasks[result["task_id"]])
            assert len(shown["preview"].encode()) < 2100


@pytest.mark.parametrize("limit", [0, 100001])
async def test_invalid_output_limit_does_not_execute_command(tmp_path, limit):
    with Session(tmp_path / "s", tmp_path) as session:
        async with ProcessManager(session) as processes:
            with pytest.raises(ValueError, match="output|bytes"):
                await processes.run("touch must-not-exist", max_output_bytes=limit)
            assert not (tmp_path / "must-not-exist").exists()


@pytest.mark.parametrize("timeout", [-1, 121])
async def test_invalid_wait_timeout_keeps_task_running(tmp_path, timeout):
    with Session(tmp_path / "s", tmp_path) as session:
        async with ProcessManager(session) as processes:
            result = await processes.run("cat", run_in_background=True)
            with pytest.raises(ValueError, match="timeout"):
                await processes.wait_task(result["task_id"], timeout=timeout)
            assert processes.tasks[result["task_id"]].process.returncode is None


async def wait_done(manager, task_id):
    for _ in range(100):
        task = manager.tasks[task_id]
        if task.process.returncode is not None:
            await task.watch_task
            return task
        await asyncio.sleep(0.02)
    pytest.fail("process did not finish")


async def test_cwd_persists_but_export_does_not(tmp_path):
    target = tmp_path / "space 中文"
    target.mkdir()
    with Session(tmp_path / "session", tmp_path) as session:
        async with ProcessManager(session) as manager:
            result = await manager.run(
                f"cd {shlex.quote(str(target))}; export LOCAL_TEST_VALUE=hello"
            )
            assert result["exit_code"] == 0
            assert session.cwd == target
            result = await manager.run('printf "%s|%s" "$PWD" "${LOCAL_TEST_VALUE-unset}"')
            assert result["preview"] == f"{target}|unset"
            result = await manager.run("false")
            assert result["exit_code"] == 1
            assert session.cwd == target


async def test_env_script_is_loaded_each_command(tmp_path):
    with Session(tmp_path / "session", tmp_path) as session:
        async with ProcessManager(session) as manager:
            env_file = session.directory / "env.sh"
            env_file.write_text("export HARNESS_TEST_ENV=first\n")
            assert (await manager.run('printf %s "$HARNESS_TEST_ENV"'))["preview"] == "first"
            env_file.write_text("export HARNESS_TEST_ENV=second\n")
            assert (await manager.run('printf %s "$HARNESS_TEST_ENV"'))["preview"] == "second"


async def test_session_environment_survives_cd_and_resume(tmp_path):
    directory = tmp_path / "session space 中文"
    env_file = directory / "env.sh"
    target = tmp_path / "other directory"
    target.mkdir()
    with Session(directory, tmp_path) as session:
        async with ProcessManager(session) as manager:
            assert env_file.is_file()
            env_file.write_text("export HARNESS_SESSION_VALUE=first\n")
            assert (await manager.run('printf %s "$HARNESS_SESSION_VALUE"'))["preview"] == "first"
            await manager.run(f"cd {shlex.quote(str(target))}")
            env_file.write_text("export HARNESS_SESSION_VALUE=second\n")
            result = await manager.run('printf "%s|%s" "$PWD" "$HARNESS_SESSION_VALUE"')
            assert result["preview"] == f"{target}|second"
    with Session(directory) as session:
        async with ProcessManager(session) as manager:
            result = await manager.run('printf "%s|%s" "$PWD" "$HARNESS_SESSION_VALUE"')
            assert result["preview"] == f"{target}|second"
    with Session(tmp_path / "another session", tmp_path) as session:
        async with ProcessManager(session) as manager:
            result = await manager.run('printf %s "${HARNESS_SESSION_VALUE-unset}"')
            assert result["preview"] == "unset"


async def test_timeout_backgrounds_and_stdin_eof_reaches_original_process(tmp_path):
    with Session(tmp_path / "session", tmp_path) as session:
        async with ProcessManager(session) as manager:
            result = await manager.run("cat", timeout=0.02)
            assert result["status"] == "running"
            task_id = result["task_id"]
            await manager.send_input(task_id, "hello 中文\n", eof=True)
            task = await wait_done(manager, task_id)
            assert task.process.returncode == 0
            assert Path(result["output_path"]).read_text() == "hello 中文\n"
            events = manager.drain_events()
            assert len(events) == 1
            assert events[0]["task_id"] == task_id
            assert manager.drain_events() == []


async def test_background_cd_never_changes_session_cwd(tmp_path):
    target = tmp_path / "other"
    target.mkdir()
    with Session(tmp_path / "session", tmp_path) as session:
        async with ProcessManager(session) as manager:
            result = await manager.run(
                f"cd {shlex.quote(str(target))}; pwd", run_in_background=True
            )
            await wait_done(manager, result["task_id"])
            assert session.cwd == tmp_path
            assert Path(result["output_path"]).read_text().strip() == str(target)


async def test_stop_kills_descendants_and_stdin_after_exit_is_clear(tmp_path):
    with Session(tmp_path / "session", tmp_path) as session:
        async with ProcessManager(session) as manager:
            result = await manager.run("sleep 1000 & echo $!; wait", run_in_background=True)
            output = Path(result["output_path"])
            for _ in range(100):
                if output.read_text().strip():
                    break
                await asyncio.sleep(0.01)
            child = int(output.read_text().strip())
            await manager.stop_task(result["task_id"])
            # Linux can retain a dead adopted child as a zombie briefly; it cannot execute.
            stat = Path(f"/proc/{child}/stat")
            assert not stat.exists() or stat.read_text().split()[2] == "Z"
            with pytest.raises(ValueError, match="exited|closed"):
                await manager.send_input(result["task_id"], "late")


async def test_cancel_foreground_stops_process(tmp_path):
    with Session(tmp_path / "session", tmp_path) as session:
        async with ProcessManager(session) as manager:
            running = asyncio.create_task(manager.run("sleep 1000"))
            while not manager.tasks:
                await asyncio.sleep(0.01)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert all(task.process.returncode is not None for task in manager.tasks.values())


async def test_explicit_exit_captures_directory(tmp_path):
    target = tmp_path / "nested"
    target.mkdir()
    with Session(tmp_path / "session", tmp_path) as session:
        async with ProcessManager(session) as manager:
            result = await manager.run(f"cd {shlex.quote(str(target))}; exit 7")
            assert result["exit_code"] == 7
            assert session.cwd == target


async def test_reopened_session_reports_interrupted_background_task(tmp_path):
    directory = tmp_path / "s"
    with Session(directory, tmp_path) as session:
        async with ProcessManager(session) as manager:
            result = await manager.run("cat", run_in_background=True)
            saved = session.get("processes")
            assert saved[result["task_id"]]["status"] == "running"
        # Simulate a crash record: no live process is carried into the next harness.
        session.set("processes", saved)
    with Session(directory) as session:
        async with ProcessManager(session) as manager:
            event = manager.drain_events()[0]
            assert event["status"] == "interrupted"
            assert event["task_id"] == result["task_id"]
            assert manager.tasks == {}
            restored = await manager.wait_task(result["task_id"], timeout=0)
            assert restored["status"] == "interrupted"
            with pytest.raises(ValueError, match="Unknown task"):
                await manager.wait_task("unknown", timeout=0)


async def test_cleanup_handles_children_after_shell_leader_exits(tmp_path):
    with Session(tmp_path / "s", tmp_path) as session:
        manager = ProcessManager(session)
        child = None
        try:
            result = await manager.run("sleep 60 & echo $!")
            child = int(Path(result["output_path"]).read_text().strip())
            await manager.close()
            stat = Path(f"/proc/{child}/stat")
            assert not stat.exists() or stat.read_text().split()[2] == "Z"
        finally:
            if child:
                import os
                import signal

                try:
                    os.kill(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass
