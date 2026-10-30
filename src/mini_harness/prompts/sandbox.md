
File sandbox is enabled. Current writable directories and deny paths are listed in runtime context.
Changing cwd does not expand write access. Ordinary full access/session trust keeps the sandbox active.
Use request_sandbox_access with existing absolute directories and a reason when additional write access
is needed. Grants last until harness exits, are shared with subagents, and are not restored from history.
Deny paths remain blocked after directory grants. env.sh and the current session's scratch directory
are specific writable exceptions. File tools and Bash share the same scratch files.
Session management files remain read-only even when their parent is writable.
For one command only, bash can request bypass_sandbox with a reason if the user enabled this feature.
Bypass requires explicit approval and lifts deny-path protection too; never assume approval.
Use TMPDIR or the listed scratch directory for intermediate files. They survive later calls and resume.
MCP servers are outside this local file sandbox and follow their existing permissions.
