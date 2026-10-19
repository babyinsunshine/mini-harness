# Tool Permission Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 工具执行前按 allow / ask / deny 决策，并在 CLI 中完成本次审批。

**Architecture:** 以 PermissionMiddleware 作为最外层工具包装，规则和 CLI 交互分开。只有获准才进入现有 ExecutionMiddleware；CLI 复用唯一输入线程，向等待中的工具调用返回布尔审批结果。

**Tech Stack:** Python >=3.11、现有 LangChain / LangGraph、asyncio、shlex、pytest；不增加产品依赖。

**Spec:** [权限设计](../specs/2026-10-18-permissions-design.md)

## Global Constraints

- 延续项目原则：规则集中、代码可读，不增加通用策略框架，不改写 create_agent 的工具循环。
- 文件范围已由用户确认：项目和当前会话目录内读写直接允许；目录外询问，关键系统路径拒绝。
- 权限检查发生在 start_execution() 之前；未知工具与不能确定安全性的 Bash 默认询问。
- deny 不提供用户放行选项；ask 获准只允许当前调用，不生成后续调用的通行规则。
- 复用唯一输入线程，不增加数据库表；本次不提供沙箱或持久化等待审批。
- 每次只提交本任务的改动；保留工作区已有的上下文重构、进程和会话修改。
- 已有修改文件按本次增量暂存，不直接 git add 整个文件；无法独立暂存时先保留工作区改动，不回滚或混入既有工作。

## Review Focus

1. 路径含 ..、符号链接或 cwd 已移出项目：按真实路径和固定项目根目录判定，不能扩大允许范围。由任务 1 的路径测试覆盖。
2. 看似只读的命令带执行参数、命令替换或重定向：不得自动允许；危险词仅作为普通参数时不得误判为正在执行。由任务 1 的参数测试覆盖。
3. 后台事件触发 agent 时已有普通输入到达：不能把已缓冲的聊天内容当成审批同意。由任务 3 的输入路由测试覆盖。
4. 审批期间 Ctrl-C、EOF、/exit 或同批多个工具等待：不得执行未获准的调用，也不能残留等待任务或争抢输入。由任务 3 的生命周期测试覆盖。
5. env.sh 将被自动 source，且 Graph 结果可能尚未落盘：修改脚本要询问，恢复已有工具结果不重复询问或执行。由任务 1、2、4 覆盖。

## 文件与接口

- 新增 src/mini_harness/permissions.py：判定规则、原因文本、PermissionMiddleware。
- 修改 agent.py：注入权限 Middleware 与可选异步审批回调。
- 修改 cli.py：协调普通输入、审批请求、当前任务和后台完成事件。
- 修改 prompts.py / README.md：说明策略、拒绝结果及非交互行为。
- 新增 tests/test_permissions.py、tests/test_cli_permissions.py；扩展现有 Graph / CLI 测试。

## Task 1: 可独立验证的权限规则

**Files:** 新增 permissions.py、tests/test_permissions.py。

**Interfaces:** 产生 classify(tool_call: dict, session: Session) -> tuple[str, str]，返回动作 allow / ask / deny 和原因。使用现有 Session.resolve()、project_root、directory，不修改会话状态。

- [x] 写参数化测试，独立给定动作预期：项目文件 read/write/edit 为 allow，目录外为 ask；关键系统路径和 state.sqlite/runtime.sqlite/transcript.jsonl 的写入为 deny；env.sh 写入为 ask、读取为 allow。
- [x] 写路径测试：../outside、指向目录外的符号链接、cd 到目录外后使用相对路径，都为 ask；正常 artifacts 读写为 allow；系统路径保护优先于目录范围。
- [x] 写命令测试：pwd、项目内 cat/ls/rg 和受限参数的 git status/diff/log/show 为 allow；脚本、安装、联网、stdin 写入、未知工具为 ask；rm -rf、mkfs、dd 写设备、shutdown/reboot、git reset --hard/clean -f/push --force 为 deny。测试只传递字符串，禁止执行这些危险命令。
- [x] 写参数边界测试：rg --pre、ls 的未知参数、重定向、管道、命令替换、未闭合引号不能 allow；rg 搜索字符串中的 rm -rf 不得误判为 deny。只读命令中的目录外目标为 ask。

测试命名为 test_file_scope、test_resolved_paths、test_shell_decisions、test_shell_arguments，参数表写死预期动作。例如：

```python
assert classify({"name": "write_file", "args": {"path": "new.py"}}, session)[0] == "allow"
assert classify({"name": "bash", "args": {"command": "rm -rf build"}}, session)[0] == "deny"
assert classify({"name": "bash", "args": {"command": "python app.py"}}, session)[0] == "ask"
```

- [x] 运行 uv run pytest -q tests/test_permissions.py，确认缺少规则实现而失败。
- [x] 实现 classify() 与必要的小型路径/简单命令判断函数。使用 shlex 检查可识别命令和受限参数；复杂语法与不确定参数返回 ask，不自建完整 Shell 解析器。
- [x] 重跑上述测试，确认通过；检查函数注释说明判定顺序和边界。单独提交规则与对应测试。

## Task 2: 工具调用前的权限 Middleware

**Files:** 修改 permissions.py、agent.py；扩展 test_permissions.py、test_agent_cli.py。

**Interfaces:** 消费 classify()。产生 PermissionMiddleware(session, approve=None)，approve 是异步回调 (tool_call: dict, reason: str) -> bool。Harness.open(..., approve=None) 增加末尾可选参数，Harness.permissions 暴露 Middleware；现有参数及调用方式继续可用。

- [x] 写真实 Graph 测试：项目写入实际成功；目录外写入未经同意时目标不存在；同意后实际写入；拒绝和 deny 返回匹配 tool_call_id、status=error 的 ToolMessage。
- [x] 在审批回调里断言 session.execution(call_id) is None；拒绝后仍无 started 记录。验证权限决策和拒绝消息写入 transcript.jsonl，不增加 executions 表记录。
- [x] 保留已有恢复测试，并加入已保存 ToolMessage 的缺失结果恢复场景：审批回调不会再次调用，文件副作用不重复发生。

Graph 测试命名为 test_allowed_project_write、test_approval_precedes_execution、test_denied_write_has_no_execution、test_saved_result_skips_approval。拒绝场景在实际 Graph 调用后检查：

```python
assert not outside_target.exists()
assert harness.session.execution("denied-write") is None
assert results["denied-write"].status == "error"
```

- [x] 运行对应测试并观察 RED；在 PermissionMiddleware.awrap_tool_call() 中实现判断、等待和短路，只有获准调用一次 handler(request)。无 approve 回调的 ask 返回未执行结果。
- [x] 以 [permissions, execution, context] 顺序组装 create_agent；ExecutionMiddleware 保留自己的执行记录职责。为确实需要 env.sh 审批、无害复合命令的原测试显式注入本次同意回调，不关闭权限系统。
- [x] 运行 uv run pytest -q tests/test_permissions.py tests/test_agent_cli.py，确认通过。单独提交本任务改动。

## Task 3: CLI 单次审批与输入协调

**Files:** 修改 cli.py；新增 tests/test_cli_permissions.py。

**Interfaces:** interactive(harness, root) 内向 Harness.permissions.approve 安装回调。回调创建 Future[bool]，将 (tool_call, reason, future) 放入审批队列，再 await 结果。保持现有 Harness.stream() 文本接口；CLI 负责填入审批结果和取消等待。

- [x] 使用真实 Harness 和脚本模型编写 CLI 测试，替换的仅是终端输入来源与外部模型调用。对无害的目录外临时文件写入，y/yes 产生文件，n/空行不产生；断言调用确实尚未进入 executions 表再回答。
- [x] 写输入路由测试：两个工具同时请求审批时逐个展示并分别回答；已缓冲的普通输入仍作为下一次用户消息，不作为审批回答；后台完成事件排队并在当前轮结束后继续送达模型。
- [x] 写取消测试：审批中发 SIGINT，或输入 EOF、/exit、/quit，均没有文件副作用；取消后没有过时审批再次弹出，继续对话不会挂起。使用临时目录，必要时使用 stdlib pty 驱动真实 CLI。

CLI 测试命名为 test_approval_yes、test_approval_default_no、test_sequential_approvals、test_buffered_chat_is_not_approval、test_cancel_pending_approval，输入只能在看到真实审批提示后提供。以文件副作用和模型实际收到的用户消息为断言：

```python
assert not outside_target.exists()  # 回答审批前、拒绝或取消后
assert outside_target.read_text() == "approved"  # 明确同意并执行后
assert "queued user request" in received_user_messages  # 预先输入的聊天得到保留
```

- [x] 先运行 uv run pytest -q tests/test_cli_permissions.py 观察 RED。
- [x] 改造 interactive()：保留唯一 daemon 输入线程；等待当前 agent 时也监听审批队列，展示工具、路径/完整命令、原因和 Allow once? [y/N]，复用输入 Future。普通输入先保留，审批答案只对应当前问题；多个问题串行处理。
- [x] 审批等待同时关注当前任务完成/取消；取消或退出清理问题 Future，跳过已取消的队列项。退出 interactive() 后清除审批回调。继续保留后台进程完成通知与原 Ctrl-C 行为。
- [x] 运行 CLI 测试及已有 CLI/后台事件测试，确认通过。单独提交本任务改动。

## Task 4: 非交互验证、说明与完整回归

**Files:** 修改 test_openai_cli.py、prompts.py、README.md；同步现有设计文档中的权限状态。

**Interfaces:** -p 沿用不设置审批回调的执行路径，allow 执行、ask/deny 返回未执行结果；不增加自动同意参数。静态系统提示说明权限拒绝后不换工具绕过。

- [x] 扩展本地 SSE 端点测试：-p 下普通项目写入仍成功；需要审批的目录外写入收到拒绝 ToolMessage，文件不存在；测试不读取审批 stdin、不消耗真实模型额度。
- [x] 在 compact.py 的文件恢复处复用 classify()，仅自动重读 allow 文件；用 test_compaction_does_not_reread_external_file_without_new_approval 验证一次审批后外部文件的新内容不会被自动读入。

新增 test_noninteractive_approval_is_denied，在子进程退出后检查：

```python
assert result.returncode == 0  # 拒绝作为工具结果送回模型，模型正常结束该轮
assert not outside_target.exists()
assert any(message["role"] == "tool" for message in requests[-1]["messages"])
```

- [x] 运行 uv run pytest -q tests/test_openai_cli.py，检查真实 adapter、工具消息配对和恢复路径。
- [x] README 记录三类决策、文件范围、env.sh 例外、CLI 问答与 -p 行为；明确这是调用级审批，未知复杂脚本需要用户判断，未提供 OS 隔离。
- [x] 更新 prompts.py 的固定规则，保持每次请求的系统提示稳定；移除旧文档中“权限尚未实现”的描述。
- [x] 运行 uv run pytest -q、uv run ruff check .、git diff --check；分别确认完整测试通过、All checks passed、无空白错误。
- [x] 完成一次针对本功能增量的独立代码审查，修正实际问题后运行受影响测试；只提交本功能改动并报告验证结果。
