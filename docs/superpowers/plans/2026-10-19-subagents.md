# Subagents Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline; the user explicitly requested implementation after merging PR #2.

**Goal:** 支持独立会话、可继续、可审批且能清理的后台子智能体。

**Architecture:** SubagentManager 只管理任务和元数据；每轮打开现有 Harness，结束后释放资源。共用现有后台事件队列，不改图连线。

**Tech Stack:** Python 3.11 asyncio、LangGraph、SQLite；不加依赖。

**Spec:** ../specs/2026-10-19-subagents-design.md

## Global Constraints

- 保持文件职责清晰，复用已有会话锁、压缩和工具执行记录。
- 子智能体没有 update_plan 或子智能体工具；审批规则与父智能体相同。
- timeout 默认 600 秒，必须有限且大于零；继续沿用原时限并重新计时。

## Review Focus

- 子图事件不能污染父图流；测试主输出只含主模型回复。
- 父 cwd 改变不能扩大子权限；测试从父固定项目根继承范围。
- 空闲时子审批必须可处理；测试真实 CLI 审批与来源标识。
- 超时审批必须解除等待；测试取消审批 future 后终端继续可用。
- 重启和取消不能重放未知工具副作用；测试原子 Session 和中断工具结果恢复。

### Task 1: 子会话运行与工具接入

**Files:** 新建 subagents.py、tools/agents.py、prompts/main.md、prompts/subagent.md；修改 agent.py、tools/factory.py、tools/permissions.py、context/manager.py、prompts/system.md。

**Interfaces:** SubagentManager.spawn(task, timeout)、wait(agent_id)、continue_agent(agent_id, message, timeout)、close_agent(agent_id)、resume_interrupted()、interrupt()；Harness.open(parent_session=None, parent_permissions=None)；Harness.events(user_input, input_id=None)。

- [x] 写 tests/test_subagents.py：隔离、父标识、锁、权限、原会话继续、新计时、超时杀进程、失败通知、重启恢复、主输出隔离。
- [x] `uv run pytest tests/test_subagents.py -q` 观察缺失功能失败。
- [x] 实现管理器和四个工具；复用后台队列、Session、审批策略。
- [x] 定向测试通过后整理简短注释。

### Task 2: CLI 审批、通知与说明

**Files:** 修改 cli/interactive.py、cli/display.py、README.md；新建 tests/test_subagents_cli.py。

**Interfaces:** 沿用 completion_events；子事件以 agent_id 标识；Permissions 审批回调携带来源。

- [x] 写空闲子审批、超时审批解除、后台结果触发父模型的 CLI 测试，观察失败。
- [x] 复用审批处理逻辑，使空闲与主轮运行时都接收子审批；显示子状态。
- [x] `uv run pytest -q`、`uv run ruff check .`、`git diff --check`、CLI help。
- [ ] 独立只读审查；修复实质问题后提交、推送功能分支和新 PR，保留供用户审阅。
