import json

import pytest
from langchain_core.messages import HumanMessage


def test_reopen_retains_cwd_and_deduplicates_history(tmp_path):
    from mini_harness.session import Session

    directory = tmp_path / "session"
    with Session(directory, tmp_path) as session:
        session.cwd = tmp_path / "中文目录"
        message = HumanMessage(content="keep my original request", id="user-1")
        session.record_message(message)
        session.record_message(message)
    with Session(directory) as session:
        assert session.cwd == tmp_path / "中文目录"
        session.record_message(message)
    events = [json.loads(line) for line in (directory / "transcript.jsonl").read_text().splitlines()]
    assert len(events) == 1
    assert events[0]["message"]["data"]["content"] == "keep my original request"


def test_large_output_artifact_and_preview_survive_reopen(tmp_path):
    from mini_harness.session import Session

    text = "重要输出\n" * 2000
    with Session(tmp_path / "session", tmp_path) as session:
        shown = session.present(text, limit=50)
        assert len(shown["preview"]) <= 2000
        assert open(shown["output_path"]).read() == text
    with Session(tmp_path / "session") as session:
        assert session.present(text, limit=50) == shown


def test_session_rejects_second_writer(tmp_path):
    from mini_harness.session import Session

    with Session(tmp_path / "session", tmp_path):
        with pytest.raises(RuntimeError, match="already open"):
            Session(tmp_path / "session", tmp_path)


def test_exact_edit_preserves_crlf_and_ambiguous_edit_does_not_write(tmp_path):
    from mini_harness.session import Session
    from mini_harness.tools import edit_file, read_file, write_file

    with Session(tmp_path / "session", tmp_path) as session:
        write_file(session, "a.txt", "one\r\ntwo\r\ntwo\r\n")
        assert read_file(session, "a.txt", offset=2, limit=1) == "2: two\r\n"
        with pytest.raises(ValueError, match="exactly once"):
            edit_file(session, "a.txt", "two", "three")
        assert (tmp_path / "a.txt").read_bytes() == b"one\r\ntwo\r\ntwo\r\n"
        edit_file(session, "a.txt", "one", "first")
        assert (tmp_path / "a.txt").read_bytes() == b"first\r\ntwo\r\ntwo\r\n"


def test_empty_edit_and_invalid_read_ranges_are_rejected(tmp_path):
    from mini_harness.session import Session
    from mini_harness.tools import edit_file, read_file, write_file

    with Session(tmp_path / "session", tmp_path) as session:
        write_file(session, "a", "abc")
        with pytest.raises(ValueError):
            edit_file(session, "a", "", "x")
        with pytest.raises(ValueError):
            read_file(session, "a", offset=0)
