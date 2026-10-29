"""同项目的文件锁共享；每个 Session 单独保存自己读到的内容版本。"""

import fcntl
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path

from ..session import Session


def _file_hash(target: Path) -> str | None:
    try:
        with target.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except FileNotFoundError:
        return None


def _remember_version(session: Session, target: Path, version: str | None):
    versions = session.get("file_versions", {})
    versions[str(target)] = version
    session.set("file_versions", versions)


@contextmanager
def access_file(
    session: Session, target: Path, *, writing: bool = False, record_version: bool = True,
):
    """短时独占访问；修改前检查版本，成功后记录版本，关闭句柄自动释放锁。"""
    target = target.resolve()
    if session.sandbox is not None:
        session.sandbox.check_path(
            target, writing=writing, env_file=session.directory / "env.sh",
        )
    configured_state = Path(os.environ.get("XDG_STATE_HOME", ""))
    state_home = (configured_state if configured_state.is_absolute()
                  else Path.home() / ".local" / "state")
    lock_directory = state_home / "mini-harness" / "file-locks"
    lock_directory.mkdir(parents=True, exist_ok=True)
    lock_name = hashlib.sha256(str(target).encode()).hexdigest()
    # 保留旁路锁文件，目标文件创建/删除不会改变锁；锁是否有效由 flock 决定。
    with (lock_directory / lock_name).open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.seek(0)
            owner = lock.read().strip() or "unknown"
            raise ValueError(
                f"File is locked by another agent ({owner}): {target}. Read it again later."
            ) from None
        lock.seek(0)
        lock.truncate()
        lock.write(session.id)
        lock.flush()

        version = _file_hash(target) if writing or record_version else None
        if writing:
            versions = session.get("file_versions", {})
            if str(target) not in versions and version is not None:
                raise ValueError(f"Read the existing file before modifying it: {target}")
            if versions.get(str(target)) != version:
                raise ValueError(
                    f"File changed since you last read it: {target}. "
                    "Read it again before modifying it."
                )
        elif record_version and version is None:
            # 即使读取报告文件不存在，也记住这个事实，允许随后显式重新创建。
            _remember_version(session, target, None)

        yield
        if record_version:
            _remember_version(session, target, _file_hash(target) if writing else version)
