# File Conflict Protection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 防止文件工具之间同时修改和根据旧内容覆盖文件，冲突清晰反馈给模型。

**Architecture:** 一个 `tools/file_guard.py` 封装锁与版本检查；三个文件工具通过上下文管理器使用它。哈希复用 Session metadata，异常复用已有工具错误处理。

**Tech Stack:** Python 3.11、fcntl、hashlib、SQLite、pytest；不增加依赖。

**Spec:** `docs/superpowers/specs/2026-10-27-file-conflicts-design.md`

## Global Constraints

- 本项目用于学习，以代码简洁、易读为先。
- 不增加模型工具、图节点或重试队列。
- 保护范围为使用相同锁协议的内置文件工具。

## Review Focus

- 两个独立 Session 读同一文件后，第二个写入应拒绝覆盖第一个。
- 页外内容、CRLF 和路径别名不能绕过完整文件的版本检查。
- 文件不存在、未读已有文件、读取后删除应有清楚且可恢复的行为。
- 异常和跨进程竞争不能泄漏锁、阻塞模型或写坏文件。
- 模型收到有调用 ID 的错误结果，能够重新读取后继续。

### Task 1: 文件锁与版本检查

**Files:** 新增 `src/mini_harness/tools/file_guard.py`、`tests/test_file_conflicts.py`；修改 `tools/files.py`、`tools/factory.py`、`context/compact.py`、`prompts/system.md`、`tests/test_session_tools.py`、`tests/test_agent_cli.py` 和 README。

**Interfaces:**
- 复用 `Session.get/set`、`Session.resolve_path`，不修改 Session 接口。
- 提供 `access_file(session, target: Path, *, writing=False)` 上下文管理器。
- 读取保存完整文件哈希，成功修改更新自己的哈希，已有文件未读或版本变化时抛出 `ValueError`。
- 审查修正：`read_file(..., record_version=False)` 只供压缩附件使用，不暴露给模型工具；自动重读不确认新版本，忙文件跳过。

- [x] 写行为测试：两个 Session 分别读写，旧版全量写与局部编辑均被拒绝，重读后成功。
- [x] 写行为测试：哈希跨重启保留，分页检查页外变更，路径别名共享记录，新建/删除/未读文件处理正确。
- [x] 写锁竞争测试：真实子进程持有/竞争生产锁，忙时无副作用，异常后可再次修改。
- [x] 写真实 graph 测试：锁冲突作为工具错误交给模型，后续能够恢复。
- [x] 运行 `uv run --no-sync pytest -q tests/test_file_conflicts.py --tb=short`，观察缺失功能导致的失败。
- [x] 实现小型上下文管理器并接入三个文件工具，补充必要注释和工具使用说明。
- [x] 更新已有测试，使已有文件在修改前先读取；保留原来的行为断言。
- [x] 运行冲突测试和 `tests/test_session_tools.py`，应全部通过。
- [x] 运行完整 `pytest -q`、ruff、CLI help、`git diff --check`，应全部通过。
- [x] 进行一次独立代码审查，修复实际问题；保持用户工作目录原样。
