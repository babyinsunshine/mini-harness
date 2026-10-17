import asyncio
import shlex
from pathlib import Path

import pytest

from mini_harness.session import Session


async def wait_done(manager, task_id):
    for _ in range(100):
        task = manager.tasks[task_id]
        if task.process.returncode is not None:
            await task.watcher
            return task
        await asyncio.sleep(0.02)
    pytest.fail("process did not finish")


async def test_cwd_persists_but_export_does_not(tmp_path):
    from mini_harness.processes import ProcessManager

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
    from mini_harness.processes import ProcessManager

    env_file = tmp_path / "env.sh"
    env_file.write_text("export HARNESS_TEST_ENV=first\n")
    with Session(tmp_path / "session", tmp_path) as session:
        async with ProcessManager(session, env_file=env_file) as manager:
            assert (await manager.run('printf %s "$HARNESS_TEST_ENV"'))["preview"] == "first"
            env_file.write_text("export HARNESS_TEST_ENV=second\n")
            assert (await manager.run('printf %s "$HARNESS_TEST_ENV"'))["preview"] == "second"


async def test_timeout_backgrounds_and_stdin_eof_reaches_original_process(tmp_path):
    from mini_harness.processes import ProcessManager

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
    from mini_harness.processes import ProcessManager

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
    from mini_harness.processes import ProcessManager

    with Session(tmp_path / "session", tmp_path) as session:
        async with ProcessManager(session) as manager:
            result = await manager.run("sleep 1000 & echo $!; wait", run_in_background=True)
            output = Path(result["output_path"])
            for _ in range(100):
                if output.read_text().strip():
                    break
                await asyncio.sleep(0.01)
            child = int(output.read_text().strip())
            await manager.stop(result["task_id"])
            # Linux can retain a dead adopted child as a zombie briefly; it cannot execute.
            stat = Path(f"/proc/{child}/stat")
            assert not stat.exists() or stat.read_text().split()[2] == "Z"
            with pytest.raises(ValueError, match="exited|closed"):
                await manager.send_input(result["task_id"], "late")


async def test_cancel_foreground_stops_process(tmp_path):
    from mini_harness.processes import ProcessManager

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
    from mini_harness.processes import ProcessManager

    target = tmp_path / "nested"
    target.mkdir()
    with Session(tmp_path / "session", tmp_path) as session:
        async with ProcessManager(session) as manager:
            result = await manager.run(f"cd {shlex.quote(str(target))}; exit 7")
            assert result["exit_code"] == 7
            assert session.cwd == target


async def test_reopened_session_reports_interrupted_background_task(tmp_path):
    from mini_harness.processes import ProcessManager

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
