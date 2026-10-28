# Optional Filesystem Sandbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Add an optional Linux/WSL2 filesystem sandbox with explicit, temporary user authorization.

**Architecture:** A small `Sandbox` runtime object owns path rules, extra writable directories and Bubblewrap arguments. Bash runs in a fresh restricted process using host networking; file tools and automatic context reads check the same policy. Existing graph stages and MCP permission behavior remain intact.

**Tech Stack:** Python 3.11+, asyncio, Bubblewrap, existing LangGraph/LangChain tools and CLI approval callback.

**Spec:** `docs/superpowers/specs/2026-10-28-optional-sandbox-design.md`

## Global Constraints

- Default off, Linux / WSL2 only; no new Python dependency or graph node.
- Initial `project_root` is fixed. Read access follows original user permissions except explicit deny paths; host networking remains available.
- `env.sh`, private temporary storage and output/cwd channels are explicit exceptions, not a writable session directory.
- Extra write grants live only in memory, are shared with children and disappear after restart.
- Full access and session trust cannot auto-approve sandbox expansion or one-call bypass; missing approval callbacks deny requests.
- MCP keeps existing permissions, including full-access behavior.
- Keep implementation readable; no persistent executor, grant database or automatic fallback to unsandboxed execution.

## Review Focus

- A saved graph approval must not authorize a bypass after process restart: Task 3 tests interrupted-call recovery.
- Symlink paths must not open a different scope than shown in approval: Task 1 tests canonical path boundaries.
- A denied skill or AGENTS.md must not leak through automatic context loading: Task 1 tests both paths.
- Background services and their children must stop when their owning command is stopped: Task 2 uses real Bubblewrap.
- Protected paths must remain blocked after approving a parent directory: Task 3 tests deny precedence.

---

### Task 1: Shared path policy and content reads

**Files:** Create `src/mini_harness/sandbox.py`, `tests/test_sandbox.py`; modify `session.py`, `tools/file_guard.py`, `tools/skills.py`, `tools/permissions.py`, `context/manager.py`.

**Interfaces:** `Session.sandbox` defaults to `None`; `Sandbox.load(project_root, config_path=None)` loads the three JSON fields in the spec; `check_path(path, *, writing=False, env_file=None)` raises `PermissionError` on violations. `write_paths(paths)` validates and canonicalizes existing directories; `grant(paths)` adds them in memory. `status()` supplies serializable current rules for runtime context.

- [ ] Write failing tests: project writes succeed; outside writes fail; outside reads succeed; deny paths block reads/writes even inside project or granted directories; symlinks follow their actual target; config remains protected; env.sh is the sole session-file exception.
- [ ] Add tests showing blocked skills are absent from discovery, direct loading fails, and blocked AGENTS.md content does not enter runtime context.
- [ ] Run `uv run --no-sync pytest tests/test_sandbox.py -q`; observe failure before production code exists.
- [ ] Implement the policy and call it from file access, skill loading, permission classification and AGENTS.md injection; compaction already reads through file tools.
- [ ] Run the focused tests plus `tests/test_session_tools.py`, `tests/test_skills.py`, `tests/test_permissions.py`, `tests/test_context.py`; expect all passing.
- [ ] Commit `feat: add shared sandbox path policy`.

### Task 2: Restricted Bash processes

**Files:** Modify `sandbox.py`, `tools/processes.py`; create `tests/test_sandbox_processes.py`.

**Interfaces:** `Sandbox.require_runtime()` validates Linux and the Bubblewrap executable; `Sandbox.command(argv, *, env_file, cwd_file)` returns argv for an isolated process. `ProcessManager.run(..., bypass_sandbox=False)` retains the existing return shape and lifecycle.

- [ ] Write real-process tests: project write succeeds, outside write fails, outside read succeeds, denied content is unavailable, private temp works, cwd persists, env.sh is sourced, host HTTP service remains reachable, and stop_task removes a background process tree.
- [ ] Include a path with shell punctuation in cwd metadata tests. Require actual Bubblewrap for integration cases and explicitly skip when unavailable.
- [ ] Run `uv run --no-sync pytest tests/test_sandbox_processes.py -q`; observe genuine feature failures with Bubblewrap available.
- [ ] Build a read-only host filesystem view, overlay project/grants and dedicated env/cwd channels, mount private tmp/dev/proc, use a PID namespace and retain host networking. Apply config protection and deny masks last. Missing deny paths fail explicitly.
- [ ] Replace cwd FD dependence with a narrowly exposed cwd file channel if required by Bubblewrap; preserve safe shell quoting, cancellation and cleanup.
- [ ] Run focused integration tests and `tests/test_processes.py`; expect all passing.
- [ ] Commit `feat: run bash commands inside optional sandbox`.

### Task 3: Approval, CLI and session integration

**Files:** Modify `agent.py`, `subagents.py`, `tools/factory.py`, `tools/permissions.py`, `cli/config.py`, `cli/main.py`, `cli/interactive.py`; add `tests/test_sandbox_agent.py` and CLI assertions to existing tests.

**Interfaces:** `Harness.open(..., sandbox=False, sandbox_config=None, parent_sandbox=None)` creates/reuses runtime policy. `request_sandbox_access(paths, reason)` requests session-scoped write access. Bash gains `bypass_sandbox=False` and `reason=None`. Reuse `Harness.events()` clearing of pending calls and approvals, and existing missing-result recovery, so interrupted approvals are not replayed.

- [ ] Write failing tests for extra-directory approval, denial, disabled requests/bypass, missing reason, full-access and session-trust separation, one-call bypass, interrupted-call recovery, child sharing, restart revocation and unchanged MCP full-access calls.
- [ ] Test CLI config precedence and `--sandbox` / `--no-sandbox`, including resume; special approvals offer allow/deny without setting general session trust.
- [ ] Run `uv run --no-sync pytest tests/test_sandbox_agent.py -q`; observe failures for missing integration.
- [ ] Wire policy before ProcessManager/context initialization; preserve original project root, share runtime policy with children, add sandbox state to runtime context, and register request tools only when enabled.
- [ ] Route explicit expansion/bypass through the existing approval callback with normalized scopes and no trust/full-access shortcut. Recover interrupted special calls without replaying them.
- [ ] Run new tests plus `tests/test_cli_permissions.py`, `tests/test_subagents.py`, `tests/test_mcp.py` and relevant configuration tests; expect all passing.
- [ ] Commit `feat: expose explicit sandbox authorization in cli`.

### Task 4: Usage documentation and final validation

**Files:** Modify `README.md`, `docs/architecture.md`; add a generic sandbox configuration example without personal paths or credentials.

- [ ] Document installation, optional CLI flags, deny paths, in-memory grants, exceptions, bypass and MCP scope. Keep README concise and place mechanism details in the architecture guide.
- [ ] Run `uv run --no-sync pytest -q`, `uv run --no-sync ruff check .`, `uv run --no-sync mini-harness --help`, and `git diff --check`; expect passing tests/checks and documented flags in help.
- [ ] Independently review the whole branch against spec, plan and real-process results; fix material findings with regression tests.
- [ ] Commit final documentation and report the worktree/branch and verified behavior. Do not merge without user direction.
