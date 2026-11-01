# Latest Checkpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 默认只保留最新有效检查点，避免历史状态重复累积。

**Architecture:** 新增 open_checkpointer(database) 连接封装，沿用 AsyncSqliteSaver；SQLite 触发器在写入同一事务内清理旧快照和旧 writes。连接开放前整理已有历史。

**Tech Stack:** Python、aiosqlite、LangGraph。

**Spec:** docs/superpowers/specs/2026-11-01-latest-checkpoint-design.md

## Global Constraints

- 每个 thread / namespace 独立保留最新一份检查点。
- 最新检查点关联的 pending writes 必须保留，中断恢复不重放已执行工具。
- 本轮不增加历史模式开关，不修改图片消息格式，不迁移或删除用户真实会话。
- 保持代码和注释简洁，不新增依赖。

## Review Focus

- 清理写入失败时，之前的有效状态仍可读取。
- 多 namespace / thread 之间不能互相删除当前状态。
- 旧会话最新 pending writes 在打开后仍然可恢复。
- 图片与完整对话必须在会话重开后保留。
- 普通取消、后台通知和子会话恢复沿用既有语义。

### Task 1: 默认保留最新检查点

**Files:** 新增 src/mini_harness/checkpoints.py、tests/test_checkpoints.py；修改 agent.py 和 docs/architecture.md。

**Interfaces:** open_checkpointer(database: Path) 提供异步上下文管理器，返回带有原有 lock / conn / checkpoint API 的 AsyncSqliteSaver。

- [x] **Step 1:** 写真实 SQLite / Graph 回归：图片对话仅一份快照、旧 pending writes 恢复不重放、thread / namespace 隔离、清理失败回滚。
- [x] **Step 2:** 运行 `uv run --no-sync pytest -q tests/test_checkpoints.py`，Expected: 因旧历史仍被保留而失败。
- [x] **Step 3:** 实现连接封装、原子清理触发器和旧历史整理；agent.py 只替换连接入口，补充机制说明。
- [x] **Step 4:** 运行上述回归及 tests/test_agent_cli.py、tests/test_interruption.py、tests/test_images.py、tests/test_subagents.py，Expected: 全部通过。
- [ ] **Step 5:** `uv run --no-sync ruff check .`、`uv run --no-sync pytest -q`、`git diff --check`，Expected: 全部通过；提交本任务。
