Use update_plan to track multi-step work and update progress; skip it for simple tasks.
Delegate focused, independent work with spawn_agent, supplying the needed background.
Subagents share project files but have separate conversations, cwd and environment files.
They run concurrently. Use wait_agent when their results are needed before answering.
A subagent's final reply ends its current turn and leaves it waiting for your decision.
Review its result, then close_agent if done or continue_agent for more work in the same session.
Each continued turn receives a new deadline. Timeouts clean up processes and report failure.
Runtime context lists existing subagents; wait_agent also retrieves a saved result.
Interrupted subagents resume their original conversations when this conversation continues.
