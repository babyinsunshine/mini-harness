"""文件读取、完整写入与精确替换；不依赖模型工具包装。"""

from difflib import unified_diff

from ..session import Session


def read_file(session: Session, path: str, offset: int = 1, limit: int = 200) -> dict:
    """path 相对会话 cwd；offset 是从 1 开始的行号，limit 是最多读取的行数。"""
    if offset < 1 or limit < 1:
        raise ValueError("offset and limit must be positive")
    selected_lines = []
    has_more = False
    target = session.resolve_path(path)
    with target.open(encoding="utf-8", newline="") as stream:
        for line_number, line in enumerate(stream, 1):
            if line_number >= offset + limit:
                has_more = True
                break
            if line_number >= offset:
                selected_lines.append(f"{line_number}: {line}")
    if not target.is_relative_to(session.directory):
        recent = session.get("recent_files", [])
        recent = [item for item in recent if item["path"] != str(target)]
        recent.append({"path": str(target), "offset": offset, "limit": limit})
        session.set("recent_files", recent[-5:])
    return {
        "output": "".join(selected_lines),
        "has_more": has_more,
        "next_offset": offset + limit if has_more else None,
    }


def write_file(session: Session, path: str, content: str) -> str:
    """将完整 content 写入 path，创建缺失的父目录；已有文件会覆盖。"""
    target = session.resolve_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="")
    return f"Wrote {target}"


def edit_file(session: Session, path: str, old_text: str, new_text: str) -> str:
    """将唯一且非空的 old_text 替换为 new_text，其余内容和换行保持原样。"""
    target = session.resolve_path(path)
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
