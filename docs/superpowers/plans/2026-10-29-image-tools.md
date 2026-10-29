# Image Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 让模型按需读取 PNG/JPEG，并用完整图片块替换实现简单历史清理。

**Architecture:** 新建图片工具模块，复用文件访问控制，返回标准图片内容块。主请求在工具结果组后发送图片；压缩继续复用现有 Micro 策略，摘要仅使用文字观察与原图路径。

**Tech Stack:** Python 3.11+、现有 LangChain/LangGraph、标准库，无新增依赖。

**Spec:** `docs/superpowers/specs/2026-10-29-image-tools-design.md`

## Global Constraints

- PNG/JPEG，最多 5 MiB；文件头识别，不解码、不缩放、不自动描述。
- 原图按哈希保存在 artifacts；图片粗估每张 4096 token，继续使用 API usage 校准。
- 权限与沙箱不绕过；无新增图节点，默认模型不变。
- 图片历史原位替换为固定提示，保留原图、文字和工具配对；恢复复用保存的内容。
- 当前会话执行，最后独立审阅；复用已隔离工作树的新分支，不修改沙箱 PR。

## Review Focus

1. 图片仅有路径而未送入视觉请求：Task 1 检查真实适配器发出的 user 图片块。
2. 多工具结果中间插入图片导致协议损坏：Task 1 检查所有结果先于图片附件。
3. 原文件修改或恢复后图片变化：Task 1 检查快照字节与恢复后的图片。
4. Base64 被计数或进入文本摘要：Task 2 检查大/小编码估算相同且摘要不含编码。
5. Micro 重写文字或工具关系：Task 2 检查旧图文字保留、近期图不变、重复清理稳定。

### Task 1: 图片工具和请求传输

**Files:** Create `src/mini_harness/tools/images.py`, `tests/test_images.py`; modify `tools/factory.py`, `tools/permissions.py`, `responses.py`, `prompts/system.md`.

**Interfaces:** `read_image(session: Session, path: str) -> list[dict]` 返回文字路径和标准 image block；`model_messages(messages)` 返回适配传输的消息列表，不改原消息。

- [x] 写失败测试：PNG/JPEG 字节与类型；超限/不支持拒绝且不落盘；权限/沙箱拒绝；图调用与恢复；多个工具结果后才追加图片；真实 OpenAI 适配器发出 user 图片块。
- [x] 运行 `uv run --no-sync pytest tests/test_images.py -q`，确认功能缺失导致失败。
- [x] 最简实现上述接口，注册 read_image 并复用 read_file 的权限规则，补充工具和系统提示说明。
- [x] 运行 `uv run --no-sync pytest tests/test_images.py tests/test_permissions.py tests/test_graph.py tests/test_session_tools.py -q`，预期全部通过。
- [x] 提交 `feat: add image reading tool and multimodal requests`。

### Task 2: 图片历史清理与摘要

**Files:** Modify `src/mini_harness/context/compact.py`, `src/mini_harness/prompts/summary.md`, `docs/architecture.md`, `README.md`; extend `tests/test_images.py`.

**Interfaces:** 继续使用 `conversation_text(messages)`, `estimate_tokens(messages)`, `micro_compact(messages, session, policy, idle=False)`；序列化文字时省略图片字节，估算单独计图片数量。

- [x] 写失败测试：近期图片保护、旧图整块替换且文字/配对保留、固定引用重复复用、原图能重读、编码长度不影响图片估算、摘要只含路径和已有观察。
- [x] 运行 `uv run --no-sync pytest tests/test_images.py -q`，确认新用例因现有压缩行为失败。
- [x] 修改压缩算法，图片分支复用已有候选和节省量判断；补充摘要提示和简短文档。
- [x] 运行 `uv run --no-sync pytest tests/test_images.py tests/test_context.py -q`，预期全部通过。
- [x] 提交 `feat: compact old image results by reference`。

### Task 3: 完整验证与独立审阅

**Files:** 按审阅发现补充必要回归测试；不增加无关功能。

- [x] 运行 `uv run --no-sync ruff check .`、`uv run --no-sync pytest -q -rs`、`uv run --no-sync mini-harness --help`、`git diff --check`，预期通过。
- [x] 使用 fresh reviewer 审阅起点 `3796377` 到最终 HEAD，仅检查本功能；重要发现用失败测试复现并修复，之后重跑全套。
- [x] 记录真实 API 未验证的范围、当前分支与使用方式；保留未合并的沙箱分支。

验证结果：完整本地测试 321 passed；Ruff、CLI help、diff check 通过。独立审阅未发现问题。真实视觉 API 未验证。
