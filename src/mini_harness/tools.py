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
    target = session.resolve(path)
    if not target.is_relative_to(session.directory):
        recent = session.get("recent_files", [])
        recent = [item for item in recent if item["path"] != str(target)]
        recent.append({"path": str(target), "offset": offset, "limit": limit})
        session.set("recent_files", recent[-5:])
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


def create_tools(session: Session, processes):
    """Bind session resources without introducing a tool registry abstraction."""
    from langchain_core.tools import tool

    @tool("read_file")
    async def read(path: str, offset: int = 1, limit: int = 200) -> dict:
        """Read a UTF-8 file with 1-based line numbers. Use offset/limit for large files."""
        return session.present(read_file(session, path, offset, limit))

    @tool("write_file")
    async def write(path: str, content: str) -> str:
        """Write complete UTF-8 content to a file. Existing content is replaced."""
        return write_file(session, path, content)

    @tool("edit_file")
    async def edit(path: str, old_text: str, new_text: str) -> str:
        """Replace exactly one nonempty literal match. Read the file before editing."""
        return edit_file(session, path, old_text, new_text)

    @tool
    async def bash(command: str, timeout: float = 120, run_in_background: bool = False) -> dict:
        """Run Bash. timeout is foreground wait time; unfinished commands go to background.

        Each call is a new process. Main foreground cwd persists; exports do not.
        Read the output_path for full logs. Background directory changes are isolated.
        """
        return await processes.run(command, timeout, run_in_background)

    @tool
    async def send_input(task_id: str, text: str = "", eof: bool = False) -> dict:
        """Write text to an existing process's stdin; eof closes stdin after writing."""
        return await processes.send_input(task_id, text, eof)

    @tool
    async def stop_task(task_id: str) -> dict:
        """Stop a background command and its child processes."""
        return await processes.stop(task_id)

    return [bash, read, write, edit, send_input, stop_task]
