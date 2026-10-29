"""两个独立会话及真实子进程之间的文件冲突；只替代外部模型。"""

import subprocess
import sys

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from test_agent_cli import ScriptedModel, call, consume

from mini_harness.agent import Harness
from mini_harness.session import Session
from mini_harness.tools.files import edit_file, read_file, write_file


def test_same_target_uses_shared_user_lock_across_project_roots(tmp_path, monkeypatch):
    from mini_harness.tools.file_guard import access_file

    first, second = tmp_path / "first-project", tmp_path / "second-project"
    first.mkdir()
    second.mkdir()
    target = tmp_path / "shared.txt"
    target.write_text("shared")
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    with Session(tmp_path / "a", first) as a, Session(tmp_path / "b", second) as b:
        with access_file(a, target):
            with pytest.raises(ValueError, match="locked"):
                read_file(b, str(target))
        assert read_file(b, str(target))["output"] == "1: shared"
    assert not (first / ".mini-harness").exists()
    assert not (second / ".mini-harness").exists()
    assert len(list((state / "mini-harness" / "file-locks").iterdir())) == 1


@pytest.mark.parametrize("operation", ["write", "edit"])
def test_stale_changes_are_rejected_until_the_agent_reads_again(tmp_path, operation):
    target = tmp_path / "shared.py"
    target.write_text("first\nkeep\n")
    with Session(tmp_path / "a", tmp_path) as a, Session(tmp_path / "b", tmp_path) as b:
        read_file(a, "shared.py")
        read_file(b, "shared.py")
        edit_file(a, "shared.py", "first", "from A")

        with pytest.raises(ValueError, match="(?i)changed"):
            if operation == "write":
                write_file(b, "shared.py", "first\nfrom B\n")
            else:
                edit_file(b, "shared.py", "keep", "from B")
        assert target.read_text() == "from A\nkeep\n"

        read_file(b, "shared.py")
        if operation == "write":
            write_file(b, "shared.py", "from A\nfrom B\n")
        else:
            edit_file(b, "shared.py", "keep", "from B")
        assert target.read_text() == "from A\nfrom B\n"


def test_file_versions_survive_session_reopen(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("abc")
    with Session(tmp_path / "session", tmp_path) as session:
        read_file(session, "a.txt")
        assert session.get("file_versions")[str(target)] == (
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        )
    target.write_text("external change")
    with Session(tmp_path / "session") as session:
        with pytest.raises(ValueError, match="(?i)changed"):
            write_file(session, "a.txt", "stale replacement")
    assert target.read_text() == "external change"


def test_paginated_read_tracks_changes_outside_the_page(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("visible\nhidden\n")
    with Session(tmp_path / "session", tmp_path) as session:
        assert read_file(session, "a.txt", limit=1)["output"] == "1: visible\n"
        target.write_text("visible\nchanged outside the page\n")
        with pytest.raises(ValueError, match="(?i)changed"):
            write_file(session, "a.txt", "visible\nhidden\n")
    assert "changed outside the page" in target.read_text()


def test_path_aliases_use_the_same_recorded_version(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("before")
    (tmp_path / "alias.txt").symlink_to(target)
    with Session(tmp_path / "session", tmp_path) as session:
        read_file(session, "alias.txt")
        target.write_text("changed")
        with pytest.raises(ValueError, match="(?i)changed"):
            write_file(session, str(target), "stale")
    assert target.read_text() == "changed"


def test_existing_files_require_a_read_and_new_files_can_be_created(tmp_path):
    target = tmp_path / "existing.txt"
    target.write_text("keep")
    with Session(tmp_path / "session", tmp_path) as session:
        with pytest.raises(ValueError, match="(?i)read"):
            write_file(session, "existing.txt", "blind overwrite")
        assert target.read_text() == "keep"
        write_file(session, "new.txt", "first")
        edit_file(session, "new.txt", "first", "second")
        write_file(session, "new.txt", "third")
    assert (tmp_path / "new.txt").read_text() == "third"


def test_deletion_is_detected_and_a_missing_read_allows_recreation(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("before")
    with Session(tmp_path / "session", tmp_path) as session:
        read_file(session, "a.txt")
        target.unlink()
        with pytest.raises(ValueError, match="(?i)changed"):
            write_file(session, "a.txt", "stale")
        assert not target.exists()
        with pytest.raises(FileNotFoundError):
            read_file(session, "a.txt")
        write_file(session, "a.txt", "recreated")
    assert target.read_text() == "recreated"


def test_real_process_reports_lock_owner_and_can_write_after_release(tmp_path):
    from mini_harness.tools.file_guard import access_file

    target = tmp_path / "a.txt"
    target.write_text("before")
    with Session(tmp_path / "contender", tmp_path) as session:
        read_file(session, "a.txt")
    program = '''
import sys
from pathlib import Path
from mini_harness.session import Session
from mini_harness.tools.files import write_file
with Session(Path(sys.argv[1])) as session:
    try:
        print(write_file(session, "a.txt", "from contender"))
    except ValueError as error:
        print(error)
'''
    command = [sys.executable, "-c", program, str(tmp_path / "contender")]
    with Session(tmp_path / "owner-session", tmp_path) as owner:
        read_file(owner, "a.txt")
        with access_file(owner, target, writing=True):
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            assert result.returncode == 0, result.stderr
            assert "owner-session" in result.stdout and str(target) in result.stdout
            assert target.read_text() == "before"
            write_file(owner, "independent.txt", "another file is not blocked")
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert target.read_text() == "from contender"


def test_invalid_edit_releases_the_file_lock(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("repeat repeat")
    with Session(tmp_path / "a", tmp_path) as a, Session(tmp_path / "b", tmp_path) as b:
        read_file(a, "a.txt")
        read_file(b, "a.txt")
        with pytest.raises(ValueError, match="exactly once"):
            edit_file(a, "a.txt", "repeat", "new")
        write_file(b, "a.txt", "after failed edit")
    assert target.read_text() == "after failed edit"


@pytest.mark.parametrize("deleted", [False, True])
def test_automatic_compaction_reads_do_not_acknowledge_file_changes(tmp_path, deleted):
    from mini_harness.context.compact import restore_recent_files

    target = tmp_path / "a.txt"
    target.write_text("before")
    with Session(tmp_path / "reader", tmp_path) as reader:
        read_file(reader, "a.txt")
        if deleted:
            target.unlink()
        else:
            target.write_text("changed")
        attachments = restore_recent_files([], reader, 10000)
        assert bool(attachments) == (not deleted)
        # 附件可能尚未送达模型或被压缩预算丢弃，不能替代显式读取。
        with pytest.raises(ValueError, match="(?i)changed"):
            write_file(reader, "a.txt", "stale replacement")


def test_automatic_compaction_skips_a_busy_file_and_preserves_versions(tmp_path):
    from mini_harness.context.compact import restore_recent_files
    from mini_harness.tools.file_guard import access_file

    busy = tmp_path / "busy.txt"
    changed = tmp_path / "changed.txt"
    busy.write_text("busy")
    changed.write_text("before")
    with (
        Session(tmp_path / "reader", tmp_path) as reader,
        Session(tmp_path / "owner", tmp_path) as owner,
    ):
        read_file(reader, "busy.txt")
        read_file(reader, "changed.txt")
        read_file(owner, "busy.txt")
        changed.write_text("changed")
        with access_file(owner, busy, writing=True):
            attachments = restore_recent_files([], reader, 10000)
            assert len(attachments) == 1 and "changed.txt" in attachments[0].content
            with pytest.raises(ValueError, match="(?i)changed"):
                write_file(reader, "changed.txt", "stale replacement")


async def test_lock_conflict_reaches_model_and_a_new_read_recovers(tmp_path):
    from mini_harness.tools.file_guard import access_file

    target = tmp_path / "a.txt"
    target.write_text("before")
    model = ScriptedModel(responses=[
        call("write_file", {"path": "a.txt", "content": "blocked"}, "blocked"),
        AIMessage(content="Will read again later"),
    ])
    with Session(tmp_path / "owner-session", tmp_path) as owner:
        read_file(owner, "a.txt")
        async with Harness.open(tmp_path, tmp_path / "agent", model, full_access=True) as harness:
            with access_file(owner, target, writing=True):
                await consume(harness, "Try the busy file")
                result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
                assert result.status == "error" and result.tool_call_id == "blocked"
                assert "owner-session" in result.text
                assert target.read_text() == "before"
            model.responses.extend([
                call("read_file", {"path": "a.txt"}, "read"),
                call("write_file", {"path": "a.txt", "content": "recovered"}, "write"),
                AIMessage(content="Done"),
            ])
            assert await consume(harness, "Try again after reading") == "Done"
            assert target.read_text() == "recovered"
            saved = harness.session.get_execution("blocked")
            assert saved[0] == "error"
            assert "owner-session" in saved[1]["data"]["content"]
