# Minimal Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 实现已商定的可运行 CLI harness，包括本地工具、进程、会话和压缩。
**Architecture:** create_agent 管理循环；ContextMiddleware 管理上下文；本地 SQLite 和文件管理执行状态及输出。全部本地实现，不添加子 agent、MCP 等未讨论模块。
**Tech Stack:** Python >=3.11、LangChain 1.x、LangGraph 1.x、SQLite、asyncio、pytest。
**Spec:** docs/superpowers/specs/2026-10-17-harness.md

## Global Constraints

- Python >=3.11；复用 create_agent，不重写 agent loop。
- 默认四个基础工具，进程控制为薄封装；无 PTY。
- 保存原始历史；压缩和恢复不重复执行已完成或结果未知的命令。
- 本地 CLI，模块可直接阅读，无通用插件框架。

## Review Focus

- 命令的引号、非零退出、中文路径不得破坏 cwd 捕获。
- 后台子进程继承的管道和进程组必须正常清理。
- 压缩边界不得拆开并行工具调用组。
- 模型取消、空摘要和错误不得提交损坏的上下文。
- graph checkpoint 与外部工具的落盘时间差不得导致命令重复执行。

### Task 1: Sessions and filesystem tools

**Files:** src/mini_harness/session.py, tools.py; tests/test_session_tools.py
**Interfaces:** Session(directory, cwd), Session.cwd, save_artifact(text), record_message(message), get/set metadata; create_tools(session, processes) -> list[BaseTool]. read_file(path, offset=1, limit=200), write_file(path, content), edit_file(path, old_text, new_text).

- [x] Write tests: reopen session retains cwd; repeated event IDs do not duplicate transcript; artifact preview stable; competing session writer fails; ambiguous edit leaves file unchanged; partial read line numbers and CRLF are preserved.
- [x] Run `uv run pytest tests/test_session_tools.py -q`; expect missing implementation failures.
- [x] Implement concrete Session and file tool functions; no generic storage interface.
- [x] Run the task tests and full current suite; commit passing implementation.

### Task 2: Bash and managed processes

**Files:** src/mini_harness/processes.py, tools.py; tests/test_processes.py
**Interfaces:** ProcessManager(session, env_file=None), async run(command, timeout=120, run_in_background=False), send_input(task_id, text='', eof=False), stop(task_id), close(), drain_events(). run returns serializable status/task_id/exit_code/output_path/preview.

- [x] Write real subprocess tests: foreground cwd persists, export disappears, env_file loads each time; quoted command and nonzero exit preserve cwd; foreground timeout backgrounds same process; background cwd isolated; stdin and EOF work; stop and cancellation clean children; output remains on disk.
- [x] Run `uv run pytest tests/test_processes.py -q`; expect missing implementation failures.
- [x] Implement one process registry with asyncio lock and completion queue; connect Bash/input/stop tool wrappers. Use process group termination and bounded output previews.
- [x] Run task tests and full current suite; commit.

### Task 3: Context pipeline

**Files:** src/mini_harness/context.py, prompts.py; tests/test_context.py
**Interfaces:** ContextPolicy(window, reserve, auto_buffer, block_buffer, keep_rounds, micro_trigger, micro_keep, notes), ContextMiddleware(session, model, policy), async compact(messages, instructions='', start=None, end=None), update_notes(messages), prepare(messages), cleanup().

- [x] Write tests: Micro preserves recent results and original transcript; full tool groups remain together; auto summary commits only on success; notes reuse retains uncovered tail; stale background update rejected; summary window includes output reserve; partial compact preserves outside segment; explicit overflow gets bounded recovery; ordinary error does not retry; three failures trip breaker.
- [x] Run `uv run pytest tests/test_context.py -q`; expect missing implementation failures.
- [x] Implement local token budgeting and one shared compact path; use model response state override for reactive recovery after inspecting installed middleware API. Persist messages and note coverage together via graph state or versioned metadata.
- [x] Run task tests and full current suite; commit.

### Task 4: Graph and CLI integration

**Files:** src/mini_harness/agent.py, cli.py, __main__.py; tests/test_agent_cli.py; README.md
**Interfaces:** Harness.open(cwd, directory, model, policy, env_file=None), async stream(text), compact(instructions, start, end), close(); command mini-harness --model provider:model [-p prompt] [--resume id] [--session-dir dir].

- [x] Write offline real-create_agent integration tests using a scripted BaseChatModel: read/edit/bash round-trip; reopen checkpoint continues compressed conversation; persisted tool result is reused; unknown execution never repeats; background completion reaches next model request; --help and missing model configuration return useful results.
- [x] Run `uv run pytest tests/test_agent_cli.py -q`; expect missing implementation failures.
- [x] Implement async SQLite checkpointer lifecycle and streaming CLI, input() on a daemon reader thread, /compact, /tasks, /sessions, /exit, cancellation, model/provider configuration, docs with exact commands.
- [x] Run `uv run pytest -q`, `uv run ruff check .`, `uv run mini-harness --help`, and compile checks. Review full branch for unnecessary layers and missing failure paths; fix important findings with reproducing tests.
- [x] Commit verified result and record API smoke-test limitation if no credentials were provided.

## Completion record

Implemented locally on `feat/bootstrap` in a new standalone repository.
The user's request to begin followed approval of the design; implementation ran in this session.
Task 1, Task 2 and Task 3 tests were observed failing before implementation, then passing.
Task 4 uses the actual create_agent graph, SQLite and subprocesses with only the model substituted.

Independent whole-project review found three Important issues; all were reproduced with failing tests
and fixed in one pass:

1. Shell leader exit did not imply all descendants exited. Process groups now have one cleanup
   task, invoked even after shell exit; subsequent cleanup does not signal an old group again.
2. Synchronous metadata writes could block the event loop while the async checkpointer held the same
   SQLite write lock. Runtime metadata moved to `runtime.sqlite`; the session lock covers both DBs.
3. Oversized summarization used to omit old input. Summaries now process all selected rounds in
   bounded batches and merge prior summaries. A single unfit round fails without committing a new
   context or coverage boundary. Both local budget and provider overflow paths have regression tests.

An additional recovery regression normalizes env_file to an absolute path before saving settings.
The local HTTP/SSE test exercises the real OpenAI adapter, CLI, tool calls, stream text and resume.

Final validation: `uv run pytest -q` → **39 passed**; `uv run ruff check .` → passed;
`uv run python -m compileall -q src` → passed; `uv build` → wheel and sdist built.

Review boundaries retained explicitly: real remote authentication, billing, rate limits and model
behavior await user-provided provider configuration; Linux is the verified platform; power-loss
durability was not fault-injected. Subagents, MCP, skills, permissions/sandboxing, tasks/plans and
long-term memory still require their module design discussion. No deferred minor review findings.

Ruling: a separate runtime SQLite file is preferable to making all metadata operations async;
the cost is one extra local database file and a changed first-release storage layout.
Work stays on the local development branch; there is no parent project branch or remote to merge.
