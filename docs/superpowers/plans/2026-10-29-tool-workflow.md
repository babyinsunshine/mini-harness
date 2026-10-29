# Tool Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复临时文件、后台等待与输出读取体验，保持实现易懂。

**Architecture:** Session 提供真实 scratch，沙箱为当前会话开放同一目录；文件锁使用全局状态路径。复用现有 watcher 实现 wait_task，read_file 直接返回有界完整行分页，Bash 输出支持显式上限。

**Tech Stack:** 现有 Python 3.11+、LangGraph、LangChain、Bubblewrap，无新增依赖。

**Spec:** `docs/superpowers/specs/2026-10-29-tool-workflow-design.md`

## Global Constraints

- session/tmp 跨调用和恢复保留，管理文件仍只读；deny_paths、配置与运行文件保护优先。
- 全局锁路径遵循 XDG_STATE_HOME，默认 ~/.local/state/mini-harness/file-locks；目标绝对路径哈希。
- wait_task 默认等待 30 秒，允许 0..120 秒；超时/取消等待不终止后台任务。
- 输出上限：Bash 默认 2000 字节、最多 100000；read_file 默认 12000 字符、最多 100000。
- 不新增依赖、图节点、浏览器能力或自动安装系统工具。

## Review Focus

1. scratch 符号链接或父目录移动不能获得数据库/配置/拒绝路径访问：Task 1 回归。
2. 主、子和不同项目会话访问同一目标时仍共享锁：Task 1 真实进程及不同 project_root 回归。
3. wait_task 超时、取消或收到旧任务 ID 时不影响正在运行的进程：Task 2 回归。
4. 输出上限错误不能在命令执行后才报错：Task 2 验证副作用未发生。
5. read_file 的字符预算不能跳过未交付行，较长单行与非 UTF-8 有明确处理：Task 2 回归。

### Task 1: 共享 scratch 与用户状态目录锁

**Files:** session.py、sandbox.py、agent.py、tools/processes.py、tools/file_guard.py、prompts/sandbox.md；tests/test_sandbox_processes.py、tests/test_sandbox.py、tests/test_file_conflicts.py、tests/conftest.py。

**Interfaces:** `Session.scratch_directory: Path`；沿用 `Sandbox.check_path` 和 `Sandbox.command`，当前会话 env_file 决定 scratch 例外。

- [ ] 先写失败测试：文件工具→真实 Bash→文件工具及恢复会话共享 scratch，拒绝管理文件与 scratch 符号链接逃逸，scratch 内保护路径不可通过移动祖先绕过；锁不污染项目且跨项目会话互斥。
- [ ] 运行相关新测试，确认因 tmpfs/路径规则/旧锁位置失败。
- [ ] 最小修改，更新旧临时目录隔离测试为共享目录契约；测试将 XDG_STATE_HOME 隔离到 tmp_path。
- [ ] 运行 `uv run --no-sync pytest tests/test_sandbox.py tests/test_sandbox_processes.py tests/test_file_conflicts.py -q -rs`，预期通过。
- [ ] 提交 `fix: share session scratch across file tools and bash`。

### Task 2: 等待任务与完整输出分页

**Files:** tools/processes.py、factory.py、files.py、permissions.py、plan.py、cli/display.py、prompts/system.md；tests/test_processes.py、test_session_tools.py、test_graph.py、test_plan.py。

**Interfaces:** `ProcessManager.wait_task(task_id, timeout=30, max_output_bytes=2000)`；`run(..., max_output_bytes=2000)`；`read_file(..., max_chars=12000)` 返回完整行分页，保留原有 output/has_more/next_offset 字段。

- [ ] 先写失败测试：查询/等待/超时/取消与恢复任务；真实图无需审批即可等待；调大 Bash 输出上限看到中间内容，非法参数不执行命令；read_file 字符分页不跳行，长行可提高上限，非 UTF-8 提示明确。
- [ ] 运行新测试，确认缺少接口或当前截断行为导致失败。
- [ ] 复用 watcher 与日志实现等待/可调预览；实现完整行分页和清晰错误；修正模型可见参数说明、rg 回退和字体按需提示。
- [ ] 运行 `uv run --no-sync pytest tests/test_processes.py tests/test_session_tools.py tests/test_graph.py tests/test_plan.py -q`，预期通过。
- [ ] 提交 `fix: add task waiting and bounded complete file pages`。

### Task 3: 文档与完整验证

**Files:** README.md、docs/architecture.md，以及审阅发现对应的最小修复和回归测试。

- [ ] 更新 scratch 生命周期、锁位置/旧进程重启、wait_task、输出参数说明。
- [ ] 执行 Ruff、完整 pytest、CLI help、git diff --check；预期通过。
- [ ] 最后独立审阅 2bf0788..HEAD，重要发现先失败回归再修复；记录验证结果，保留草稿 PR，不合并。
