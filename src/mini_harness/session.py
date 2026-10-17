"""Durable runtime metadata, an append-only transcript, and output files."""

import fcntl
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import BaseMessage, message_to_dict


class Session:
    def __init__(self, directory: Path, cwd: Path | None = None):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.id = self.directory.name
        self.database = self.directory / "state.sqlite"
        self._lock = self.database.open("a+b")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise RuntimeError(f"Session {self.id} is already open") from None
        self.connection = sqlite3.connect(self.database)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS executions "
            "(id TEXT PRIMARY KEY, status TEXT NOT NULL, result TEXT)"
        )
        if cwd is not None and self.get("cwd") is None:
            self.cwd = cwd
            self.set("project_root", str(Path(cwd).resolve()))
        if self.get("cwd") is None:
            self.close()
            raise ValueError("A new session requires a working directory")
        self.artifacts = self.directory / "artifacts"
        self.artifacts.mkdir(exist_ok=True)
        self.transcript = self.directory / "transcript.jsonl"
        self._events = set()
        if self.transcript.exists():
            try:
                self._load_events()
            except Exception:
                self.close()
                raise
        # A PID from an earlier harness process is never reused as a live handle.
        self.connection.execute("UPDATE executions SET status='unknown' WHERE status='started'")
        self.connection.commit()

    def _load_events(self):
        with self.transcript.open("r+b") as stream:
            while True:
                position = stream.tell()
                line = stream.readline()
                if not line:
                    return
                try:
                    self._events.add(json.loads(line)["id"])
                except (ValueError, UnicodeError):
                    if line.endswith(b"\n"):
                        raise ValueError(f"Corrupt transcript record at byte {position}") from None
                    stream.truncate(position)
                    return
                if not line.endswith(b"\n"):
                    stream.write(b"\n")
                    return

    def get(self, key: str, default=None):
        row = self.connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value):
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO metadata VALUES (?, ?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    @property
    def cwd(self) -> Path:
        return Path(self.get("cwd"))

    @cwd.setter
    def cwd(self, value: Path):
        self.set("cwd", str(Path(value).resolve()))

    def resolve(self, path: str) -> Path:
        return (self.cwd / Path(path).expanduser()).resolve()

    def record(self, kind: str, *, event_id: str | None = None, **data):
        event_id = event_id or str(uuid4())
        if event_id in self._events:
            return
        entry = {"id": event_id, "kind": kind, **data}
        with self.transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._events.add(event_id)

    def record_message(self, message: BaseMessage):
        if message.id is None:
            message.id = str(uuid4())
        self.record("message", event_id=message.id, message=message_to_dict(message))

    def save_artifact(self, text: str) -> Path:
        digest = hashlib.sha256(text.encode()).hexdigest()
        path = self.artifacts / f"{digest}.txt"
        if not path.exists():
            path.write_text(text, encoding="utf-8", newline="")
        return path

    def present(self, text: str, limit: int = 12000) -> dict:
        if len(text) <= limit:
            return {"output": text}
        return {
            "output_path": str(self.save_artifact(text)),
            "preview": text[: min(2000, limit)],
            "truncated": True,
        }

    def execution(self, call_id: str):
        row = self.connection.execute(
            "SELECT status, result FROM executions WHERE id=?", (call_id,)
        ).fetchone()
        return (row[0], json.loads(row[1]) if row[1] else None) if row else None

    def start_execution(self, call_id: str):
        with self.connection:
            self.connection.execute(
                "INSERT INTO executions VALUES (?, 'started', NULL)", (call_id,)
            )

    def finish_execution(self, call_id: str, result: dict):
        with self.connection:
            self.connection.execute(
                "UPDATE executions SET status='done', result=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False), call_id),
            )

    def close(self):
        self.connection.close()
        self._lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
