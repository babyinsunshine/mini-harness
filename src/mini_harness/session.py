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
        """directory 存放会话数据；cwd 只在新会话中设置初始项目目录。"""
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.id = self.directory.name
        self.database = self.directory / "state.sqlite"
        self.runtime_database = self.directory / "runtime.sqlite"
        self._lock = self.database.open("a+b")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise RuntimeError(f"Session {self.id} is already open") from None
        # Async checkpoint transactions must not contend with synchronous runtime writes.
        self.connection = sqlite3.connect(self.runtime_database)
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
        self._recorded_event_ids = set()
        if self.transcript.exists():
            try:
                self._load_event_ids()
            except Exception:
                self.close()
                raise
        # Unfinished tool attempts are errors when the session reopens.
        self.connection.execute("UPDATE executions SET status='error' WHERE status='started'")
        self.connection.commit()

    def _load_event_ids(self):
        with self.transcript.open("r+b") as stream:
            while True:
                position = stream.tell()
                line = stream.readline()
                if not line:
                    return
                try:
                    self._recorded_event_ids.add(json.loads(line)["id"])
                except (ValueError, UnicodeError):
                    if line.endswith(b"\n"):
                        raise ValueError(f"Corrupt transcript record at byte {position}") from None
                    stream.truncate(position)
                    return
                if not line.endswith(b"\n"):
                    stream.write(b"\n")
                    return

    def get(self, key: str, default=None):
        """读取会话元数据；键不存在时返回 default。"""
        row = self.connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value):
        """保存可序列化为 JSON 的会话元数据。"""
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

    def resolve_path(self, path: str) -> Path:
        """展开 ~，并按会话当前工作目录解析相对路径。"""
        return (self.cwd / Path(path).expanduser()).resolve()

    def record(self, kind: str, *, event_id: str | None = None, **data):
        """追加一条事件；event_id 用于去重，data 保存该事件的字段。"""
        event_id = event_id or str(uuid4())
        if event_id in self._recorded_event_ids:
            return
        entry = {"id": event_id, "kind": kind, **data}
        with self.transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._recorded_event_ids.add(event_id)

    def record_message(self, message: BaseMessage):
        if message.id is None:
            message.id = str(uuid4())
        self.record("message", event_id=message.id, message=message_to_dict(message))

    def artifact_path(self, text: str) -> Path:
        digest = hashlib.sha256(text.encode()).hexdigest()
        return self.artifacts / f"{digest}.txt"

    def save_artifact(self, text: str) -> Path:
        path = self.artifact_path(text)
        if not path.exists():
            path.write_text(text, encoding="utf-8", newline="")
        return path

    def prepare_tool_output(self, output: str, max_chars: int = 12000) -> dict:
        """按字符数限制单次工具输出；超长原文落盘，只返回路径和短预览。"""
        if len(output) <= max_chars:
            return {"output": output}
        return {
            "output_path": str(self.save_artifact(output)),
            "preview": output[: min(2000, max_chars)],
            "truncated": True,
        }

    def get_execution(self, call_id: str):
        """按工具调用 ID 读取 (状态, 序列化结果)；从未执行时返回 None。"""
        row = self.connection.execute(
            "SELECT status, result FROM executions WHERE id=?", (call_id,)
        ).fetchone()
        if row is None:
            return None
        return row[0], json.loads(row[1]) if row[1] else None

    def start_execution(self, call_id: str):
        """执行前落盘；call_id 是模型给出的工具调用 ID。"""
        with self.connection:
            self.connection.execute(
                "INSERT INTO executions VALUES (?, 'started', NULL)", (call_id,)
            )

    def finish_execution(self, call_id: str, result: dict):
        """保存 message_to_dict() 序列化后的工具结果，供恢复时复用。"""
        status = "error" if result["data"].get("status") == "error" else "done"
        with self.connection:
            self.connection.execute(
                "UPDATE executions SET status=?, result=? WHERE id=?",
                (status, json.dumps(result, ensure_ascii=False), call_id),
            )

    def close(self):
        self.connection.close()
        self._lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
