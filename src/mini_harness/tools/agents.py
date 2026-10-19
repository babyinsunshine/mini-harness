"""父智能体专用的四个委派工具。"""

from langchain_core.tools import tool


def create_agent_tools(manager):
    @tool
    async def spawn_agent(task: str, timeout: float = 600) -> dict:
        """Start a subagent in a separate session and return immediately.

        Supply a focused task and necessary background. Project files are shared.
        timeout is this turn's total runtime in seconds, including approval waits.
        """
        return manager.spawn(task, timeout)

    @tool
    async def wait_agent(agent_id: str) -> dict:
        """Wait for a subagent's current turn, or retrieve its saved status and result."""
        return await manager.wait(agent_id)

    @tool
    async def continue_agent(agent_id: str, message: str, timeout: float | None = None) -> dict:
        """Send another instruction in the original subagent session and return immediately.

        The subagent must have stopped running. Its deadline restarts; timeout=None
        keeps the previous duration. Use after waiting, failure or timeout.
        """
        return manager.continue_agent(agent_id, message, timeout)

    @tool
    async def close_agent(agent_id: str) -> dict:
        """Close a subagent, stop its running work and retain its session records."""
        return await manager.close_agent(agent_id)

    return [spawn_agent, wait_agent, continue_agent, close_agent]
