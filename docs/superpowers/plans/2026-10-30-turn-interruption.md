# 对话打断 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ctrl-C 后完成前台清理与消息保存，等待用户继续，后台任务不丢失。

**Architecture:** 复用 Harness 的取消出口和工具执行记录。ProcessManager 保留前台中断状态，CLI 只增加通知暂停标志。

**Tech Stack:** Python asyncio、LangGraph、SQLite、pytest。

**Spec:** docs/superpowers/specs/2026-10-30-turn-interruption-design.md

## Global Constraints

- 保留 Ctrl-C 入口与新消息排队，不新增开关、图节点、依赖或通用调度框架。
- 前台 Bash 清理完成后返回；后台任务保留；文件和远程副作用不回滚、不自动重放。
- 中断后历史保存为合法完整的工具调用组，下一轮从新输入启动。
- 不操作用户真实会话、MCP 桌面或个人配置；不推送或合并本分支。

## Review Focus

- 工具批次中，已完成结果应保留，未开始的工具不得执行。
- 连续 Ctrl-C 不得打断清理或提前显示输入提示符。
- 暂停期间后台完成事件不应自行启动请求；只读命令不解除暂停。
- MCP 的远端副作用可能已发生，只能说明结果未确认。
- 模型流被截断时，未完成的工具参数不得执行；短文件写入不留异步写入任务。

---

### Task 1: 保存中断结果并暂停自动继续

**Files:**
- Modify: src/mini_harness/agent.py、graph/nodes.py、tools/processes.py、cli/interactive.py
- Modify: docs/architecture.md
- Test: tests/test_interruption.py（复用现有模型和 CLI fixture）、tests/test_mcp.py

**Interfaces:**
- Consumes: Harness.restore_tool_messages(messages)、ProcessManager._terminate(task)、CLI run_turn。
- Produces: Harness 取消出口保存完整历史；前台 Bash 的取消异常携带最终任务记录；CLI 暂停完成通知触发模型，聊天输入恢复。

- [x] **Step 1:** 先写回归测试：批次中断立即保存配对结果且不重放；Bash 保留输出路径；后台通知暂停与聊天恢复；重复 Ctrl-C；模型流和文件写入边界；MCP 中断不丢配对、不重试。
- [x] **Step 2:** 运行新增测试，确认预期缺陷为 RED；已有 MCP/文件行为已经满足要求时记录为现有行为验证。
- [x] **Step 3:** 仅修改上述取消出口、进程结果和 CLI 暂停逻辑，保持普通执行与审批流程。
- [x] **Step 4:** 运行 `uv run --no-sync pytest -q` 和 `uv run --no-sync ruff check src tests`，预期全部通过；`git diff --check` 无输出。
- [x] **Step 5:** 更新架构说明并提交，执行一次独立审阅；重要发现以 RED→GREEN 修复，最后保留工作树和分支。

## 验证与审阅记录

- 初次全量验证：354 passed in 180.33s；Ruff、git diff --check 通过。
- 独立审阅发现 Bash 启动登记、父轮次初始化/CLI Task 入口两个取消缺口；还发现以 `/` 开头的合法聊天未解除暂停，按实际影响列入修复。
- 四个失败回归用例 RED→GREEN；10 项打断测试通过。最终全量：358 passed in 183.83s；Ruff、git diff --check 通过。
- 保留既有子智能体生命周期与子会话进程清理规则；子会话的后台进程仍受其生命周期管理。
- 重复打断保护针对 CLI Ctrl-C；直接嵌入 API 的调用方仍需管理自己的 Task.cancel() 与生命周期。
- 无推迟的小问题。最终修复由回归与全量测试验证，不重复分派审阅。本地分支与工作树保留。
