"""文件读取、完整写入与精确替换；不依赖模型工具包装。"""

from difflib import unified_diff

from ..session import Session
from .file_guard import access_file


def read_file(
    session: Session, path: str, offset: int = 1, limit: int = 200, *,
    max_chars: int = 12000, record_version: bool = True,
) -> dict:
    """按 1 开始的行号分页；record_version=False 供尚未确认送达模型的自动附件读取。"""
    if offset < 1 or limit < 1:
        raise ValueError("offset and limit must be positive")
    if not 1 <= max_chars <= 100000:
        raise ValueError("max_chars must be between 1 and 100000")
    selected_lines = []
    used_chars = 0
    has_more = False
    target = session.resolve_path(path)
    with access_file(session, target, record_version=record_version):
        try:
            with target.open(encoding="utf-8", newline="") as stream:
                for line_number, line in enumerate(stream, 1):
                    if line_number < offset:
                        continue
                    numbered = f"{line_number}: {line}"
                    if len(selected_lines) >= limit or used_chars + len(numbered) > max_chars:
                        if not selected_lines:
                            raise ValueError(
                                f"Line {line_number} exceeds max_chars={max_chars}. "
                                "Increase max_chars (up to 100000), "
                                "or inspect it in pieces with Bash."
                            )
                        has_more = True
                        break
                    selected_lines.append(numbered)
                    used_chars += len(numbered)
        except UnicodeDecodeError:
            raise ValueError(
                "File is not UTF-8 text. Use read_image for PNG/JPEG; "
                "inspect other encodings or binary files with Bash."
            ) from None
    if not target.is_relative_to(session.directory):
        recent = session.get("recent_files", [])
        recent = [item for item in recent if item["path"] != str(target)]
        recent.append({"path": str(target), "offset": offset, "limit": limit})
        session.set("recent_files", recent[-5:])
    return {
        "output": "".join(selected_lines),
        "start_line": offset if selected_lines else None,
        "end_line": offset + len(selected_lines) - 1 if selected_lines else None,
        "has_more": has_more,
        "next_offset": offset + len(selected_lines) if has_more else None,
    }


def write_file(session: Session, path: str, content: str) -> str:
    """将完整 content 写入 path，创建缺失的父目录；已有文件会覆盖。"""
    target = session.resolve_path(path)
    with access_file(session, target, writing=True):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
    return f"Wrote {target}"


def edit_file(session: Session, path: str, old_text: str, new_text: str) -> str:
    """将唯一且非空的 old_text 替换为 new_text，其余内容和换行保持原样。"""
    target = session.resolve_path(path)
    with access_file(session, target, writing=True):
        with target.open(encoding="utf-8", newline="") as stream:
            original = stream.read()
        if not old_text or original.count(old_text) != 1:
            raise ValueError("old_text must be nonempty and occur exactly once; read more context")
        updated = original.replace(old_text, new_text, 1)
        target.write_text(updated, encoding="utf-8", newline="")
    diff = "\n".join(
        unified_diff(
            original.splitlines(),
            updated.splitlines(),
            fromfile=str(target),
            tofile=str(target),
            n=2,
            lineterm="",
        )
    )
    return f"Edited {target}\n{diff}"
