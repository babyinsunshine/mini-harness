"""将文件、进程、计划和 skill 操作绑定成模型工具，新增工具从这里接入。"""

from langchain_core.tools import tool

from ..session import Session
from .agents import create_agent_tools
from .files import edit_file, read_file, write_file
from .images import read_image
from .mcp import create_mcp_tools
from .plan import PlanStep, update_plan
from .skills import list_skills, load_skill


def create_tools(
    session: Session, processes, subagents=None, *, include_plan=True, mcp_clients=None,
):
    """绑定会话资源；工具名和参数直接构成模型看到的调用协议。"""
    @tool("update_plan")
    async def update_plan_tool(plan: list[PlanStep], explanation: str | None = None) -> dict:
        """Replace the current task plan with the full list of steps and their statuses.

        Use for multi-step work, and update progress as you go. Skip simple tasks.
        At most one step may be in_progress. Pass [] to clear the plan.
        explanation optionally describes why the plan changed.
        """
        return update_plan(session, plan, explanation)

    @tool("list_skills")
    async def list_skills_tool() -> list[dict]:
        """Discover project skills and their descriptions before choosing one to load."""
        return list_skills(session)

    @tool("load_skill")
    async def load_skill_tool(name: str) -> dict:
        """Load a skill's full instructions and absolute directory. Use a name from list_skills.

        Resolve references and scripts relative to the returned directory.
        Read resources with read_file and run scripts with bash when instructed.
        """
        return load_skill(session, name)

    @tool("read_file")
    async def read_file_tool(
        path: str, offset: int = 1, limit: int = 200, max_chars: int = 12000,
    ) -> dict:
        """Read UTF-8 lines. offset is 1-based; limit is the maximum number of lines.

        Returns complete lines within max_chars (1..100000, including line numbers).
        When has_more is true, continue with next_offset; start_line/end_line show what was read.
        Increase max_chars for a long single line. Use read_image for PNG/JPEG.
        Records the full file version. Read again if an edit reports a changed/busy file.
        """
        return read_file(session, path, offset, limit, max_chars=max_chars)

    @tool("read_image")
    async def read_image_tool(path: str) -> list[dict]:
        """View a local PNG/JPEG image, at most 5 MiB, using a vision-capable model.

        Accepts absolute paths or paths relative to cwd, including saved MCP screenshots.
        Keeps an original snapshot in artifacts; use its path to re-read an old image.
        """
        return read_image(session, path)

    @tool("write_file")
    async def write_file_tool(path: str, content: str) -> str:
        """Write complete UTF-8 content. Read an existing file before replacing it.

        New files can be created directly. Changed or locked files return an error;
        read again before retrying. File versions are tracked separately per session.
        """
        return write_file(session, path, content)

    @tool("edit_file")
    async def edit_file_tool(path: str, old_text: str, new_text: str) -> dict:
        """Replace exactly one nonempty literal match. Read the file before editing.

        If another agent changed or locked it, read again before retrying.
        """
        return session.prepare_tool_output(edit_file(session, path, old_text, new_text))

    @tool
    async def bash(
        command: str, timeout: float = 120, run_in_background: bool = False,
        bypass_sandbox: bool = False, reason: str | None = None,
        max_output_bytes: int = 2000,
    ) -> dict:
        """Run Bash. timeout is foreground wait time in seconds.

        Unfinished commands go to background.
        Each call is a new process. Main foreground cwd persists; exports do not.
        Read the output_path for full logs. Background directory changes are isolated.
        max_output_bytes (1..100000) controls the head/tail preview; increase it to inspect more.
        Use wait_task to query/wait for background tasks instead of sleeping and polling logs.
        bypass_sandbox requests user approval to run this command outside the sandbox
        once; supply a reason. This requires the user to enable bypass requests.
        """
        return await processes.run(
            command, timeout, run_in_background, bypass_sandbox=bypass_sandbox,
            max_output_bytes=max_output_bytes,
        )

    @tool("request_sandbox_access")
    async def request_sandbox_access(paths: list[str], reason: str) -> dict:
        """Ask the user to make existing absolute directories writable until harness exits.

        Explain why access is needed. Approval is required even with full access or
        session trust. Deny paths remain blocked. Grants are shared with subagents,
        are not saved, and must be requested again after restarting the harness.
        """
        session.sandbox.grant(paths)
        return session.sandbox.status()

    @tool("wait_task")
    async def wait_task_tool(
        task_id: str, timeout: float = 30, max_output_bytes: int = 2000,
    ) -> dict:
        """Wait up to timeout seconds (0..120) for this session's process; 0 queries immediately.

        Returns status, exit_code, a head/tail preview and output_path for complete logs.
        max_output_bytes is 1..100000. Timing out does not stop the process.
        Old tasks after session restore return their saved status; they are not restarted.
        """
        return await processes.wait_task(task_id, timeout, max_output_bytes)

    @tool
    async def send_input(task_id: str, text: str = "", eof: bool = False) -> dict:
        """Write text to an existing process's stdin; eof closes stdin after writing."""
        return await processes.send_input(task_id, text, eof)

    @tool
    async def stop_task(task_id: str) -> dict:
        """Stop a background command and its child processes."""
        return await processes.stop_task(task_id)

    tools = [
        bash, read_file_tool, read_image_tool, write_file_tool, edit_file_tool,
        wait_task_tool, send_input, stop_task, list_skills_tool, load_skill_tool,
    ]
    if include_plan:
        tools.append(update_plan_tool)
    if session.sandbox is not None:
        tools.append(request_sandbox_access)
    if subagents is not None:
        tools.extend(create_agent_tools(subagents))
    if mcp_clients:
        tools.extend(create_mcp_tools(session, mcp_clients))
    return tools
