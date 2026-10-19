import json

import pytest
from langchain_core.messages import HumanMessage

from mini_harness.session import Session
from mini_harness.tools.factory import create_tools
from mini_harness.tools.files import edit_file, read_file, write_file


def test_reopen_retains_cwd_and_deduplicates_history(tmp_path):
    directory = tmp_path / "session"
    with Session(directory, tmp_path) as session:
        session.cwd = tmp_path / "中文目录"
        message = HumanMessage(content="keep my original request", id="user-1")
        session.record_message(message)
        session.record_message(message)
    with Session(directory) as session:
        assert session.cwd == tmp_path / "中文目录"
        session.record_message(message)
    events = [
        json.loads(line) for line in (directory / "transcript.jsonl").read_text().splitlines()
    ]
    assert len(events) == 1
    assert events[0]["message"]["data"]["content"] == "keep my original request"


def test_large_output_artifact_and_preview_survive_reopen(tmp_path):
    text = "重要输出\n" * 2000
    with Session(tmp_path / "session", tmp_path) as session:
        shown = session.prepare_tool_output(text, max_chars=50)
        assert len(shown["preview"]) <= 2000
        assert open(shown["output_path"]).read() == text
    with Session(tmp_path / "session") as session:
        assert session.prepare_tool_output(text, max_chars=50) == shown


async def test_read_tool_spills_large_output_before_returning_it(tmp_path):
    from pathlib import Path

    text = "start " + "x" * 20000 + " END\n"
    (tmp_path / "large.txt").write_text(text)
    with Session(tmp_path / "session", tmp_path) as session:
        read = next(tool for tool in create_tools(session, None) if tool.name == "read_file")
        result = await read.ainvoke({"path": "large.txt"})
        assert result["truncated"] is True
        assert len(result["preview"]) <= 2000
        assert Path(result["output_path"]).read_text() == "1: " + text


def test_session_rejects_second_writer(tmp_path):
    with Session(tmp_path / "session", tmp_path):
        with pytest.raises(RuntimeError, match="already open"):
            Session(tmp_path / "session", tmp_path)


def test_exact_edit_preserves_crlf_and_ambiguous_edit_does_not_write(tmp_path):
    with Session(tmp_path / "session", tmp_path) as session:
        write_file(session, "a.txt", "one\r\ntwo\r\ntwo\r\n")
        assert read_file(session, "a.txt", offset=2, limit=1)["output"] == "2: two\r\n"
        with pytest.raises(ValueError, match="exactly once"):
            edit_file(session, "a.txt", "two", "three")
        assert (tmp_path / "a.txt").read_bytes() == b"one\r\ntwo\r\ntwo\r\n"
        edit_file(session, "a.txt", "one", "first")
        assert (tmp_path / "a.txt").read_bytes() == b"first\r\ntwo\r\ntwo\r\n"


def test_empty_edit_and_invalid_read_ranges_are_rejected(tmp_path):
    with Session(tmp_path / "session", tmp_path) as session:
        write_file(session, "a", "abc")
        with pytest.raises(ValueError):
            edit_file(session, "a", "", "x")
        with pytest.raises(ValueError):
            read_file(session, "a", offset=0)


def test_partial_last_log_record_is_recovered_after_crash(tmp_path):
    directory = tmp_path / "s"
    with Session(directory, tmp_path) as session:
        session.record("test", event_id="first", text="keep this")
    with (directory / "transcript.jsonl").open("ab") as stream:
        stream.write(b'{"id": "incomplete')
    with Session(directory) as session:
        session.record("test", event_id="second", text="continue")
    records = [
        json.loads(line) for line in (directory / "transcript.jsonl").read_text().splitlines()
    ]
    assert [record["id"] for record in records] == ["first", "second"]


async def test_runtime_metadata_does_not_wait_on_checkpoint_transaction(tmp_path):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    with Session(tmp_path / "s", tmp_path) as session:
        session.connection.execute("PRAGMA busy_timeout=20")
        async with AsyncSqliteSaver.from_conn_string(str(session.database)) as saver:
            await saver.setup()
            await saver.conn.execute("BEGIN IMMEDIATE")
            try:
                session.set("background_completion", {"status": "done"})
            finally:
                await saver.conn.rollback()
        assert session.get("background_completion") == {"status": "done"}


async def test_read_tool_reports_how_to_continue_and_eof(tmp_path):
    (tmp_path / "long.py").write_text("line\n" * 201)
    with Session(tmp_path / "s", tmp_path) as session:
        tool = next(t for t in create_tools(session, None) if t.name == "read_file")
        first = await tool.ainvoke({"path": "long.py"})
        assert first["has_more"] is True
        assert first["next_offset"] == 201
        last = await tool.ainvoke({"path": "long.py", "offset": first["next_offset"]})
        assert last["output"] == "201: line\n"
        assert last["has_more"] is False
        assert last["next_offset"] is None


def test_edit_result_shows_the_changed_lines(tmp_path):
    (tmp_path / "code.py").write_text("before\nkeep\n")
    with Session(tmp_path / "s", tmp_path) as session:
        result = edit_file(session, "code.py", "before", "after")
        assert "-before" in result and "+after" in result
        assert (tmp_path / "code.py").read_text() == "after\nkeep\n"
