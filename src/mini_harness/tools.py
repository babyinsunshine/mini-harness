"""Local file operations; model-facing wrappers are assembled below."""

from .session import Session


def read_file(session: Session, path: str, offset: int = 1, limit: int = 200) -> str:
    """Read a 1-based line range, with line numbers."""
    if offset < 1 or limit < 1:
        raise ValueError("offset and limit must be positive")
    result = []
    with session.resolve(path).open(encoding="utf-8", newline="") as stream:
        for number, line in enumerate(stream, 1):
            if number >= offset + limit:
                break
            if number >= offset:
                result.append(f"{number}: {line}")
    return "".join(result)


def write_file(session: Session, path: str, content: str) -> str:
    """Write a complete UTF-8 file, creating missing parent directories."""
    target = session.resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="")
    return f"Wrote {target}"


def edit_file(session: Session, path: str, old_text: str, new_text: str) -> str:
    """Replace an exact, unique nonempty match; preserve other bytes/newlines."""
    target = session.resolve(path)
    with target.open(encoding="utf-8", newline="") as stream:
        original = stream.read()
    if not old_text or original.count(old_text) != 1:
        raise ValueError("old_text must be nonempty and occur exactly once; read more context")
    target.write_text(original.replace(old_text, new_text, 1), encoding="utf-8", newline="")
    return f"Edited {target}"
