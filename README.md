# mini-harness

基于 LangChain `create_agent` / LangGraph 的 CLI coding agent。
核心目标：代码易读、机制精简，功能集中在有明确职责的模块里。

## 运行

需要 Python 3.11+、Bash 和 POSIX 系统（目前在 Linux 验证）。在本项目目录安装：

```bash
uv sync --extra anthropic
export ANTHROPIC_API_KEY='你的密钥'
uv run mini-harness --model 'anthropic:YOUR_MODEL_ID' --cwd /path/to/project
```

OpenAI 或兼容接口：

```bash
uv sync --extra openai
export OPENAI_API_KEY='你的密钥'
uv run mini-harness --model 'openai:YOUR_MODEL_ID' --base-url 'https://YOUR_ENDPOINT/v1'
```

也可以设置 `HARNESS_MODEL`。密钥由供应商 SDK 读取，不保存到会话。
工具会使用当前用户的本地权限执行命令和修改文件；权限策略和沙箱尚待单独设计。

```bash
# 单次任务
uv run mini-harness --model 'anthropic:YOUR_MODEL_ID' -p '查看项目并解释测试入口'
# 查看和恢复会话
uv run mini-harness --list-sessions
uv run mini-harness --resume SESSION_ID
```

终端启动时显示会话 ID。恢复时沿用已保存的模型名、endpoint、上下文预算、
env_file 路径等设置，显式传入的新参数优先。`--cwd` 默认当前目录；已有会话保留
自己的工作目录。日常自动化验证使用离线模型驱动真实 graph；另外已完成 DeepSeek
真实 API 的工具调用、会话恢复和手动压缩验证。

本次通过的 DeepSeek 配置：

```bash
uv sync --extra openai
export OPENAI_API_KEY='你的 DeepSeek API 密钥'
uv run mini-harness --model 'openai:deepseek-flash' --base-url 'https://api.deepseek.com'
```

验证范围和结果见 [DeepSeek 实测记录](docs/validation/2026-10-17-deepseek.md)。

## 工具与执行

基础工具：`bash`、`read_file`、`write_file`、`edit_file`。搜索通过 Bash 中的 `rg`。
`read_file` 使用从 1 开始的行号；`edit_file` 只接受唯一、非空的精确匹配。

进程控制只补充 `send_input(task_id, text, eof)` 和 `stop_task(task_id)`：

- 每次 Bash 都启动新进程，继承 harness 启动环境；可用 `--env-file /path/env.sh`
  在每次命令前加载虚拟环境或变量。一次命令里的临时 `export` 不跨调用。
- 前台命令完成后保存真实 cwd；同一会话 Bash 调用串行。后台任务不会回写 cwd。
- `timeout` 是前台等待时间，默认 120 秒；未完成时保留原进程并转后台。
  `run_in_background=True` 则直接后台运行。
- stdout/stderr 持续写入文件，结果返回日志路径、预览、任务 ID 和退出码。
  stdin 使用普通管道，可追加输入和发送 EOF；没有 PTY。
- 后台完成通知在安全的模型调用点注入；空闲的交互 CLI 可以继续处理完成事件。
- `--process-timeout` 控制命令最长运行时间，默认一小时。停止和正常退出会清理进程组。
  shell 结束后也会清理遗留子进程；长服务使用 `run_in_background=True`。
- 恢复后将原来的活动进程记录标为 interrupted，不会使用旧 PID。

项目根目录的 `AGENTS.md` 会在模型调用前重新加载。

## 上下文压缩

设计基线是讨论中选定的[压缩机制文章](https://deepseek.csdn.net/6a2233c610ee7a33f277cf93.html)。
机制如下：

1. 大型文件工具输出落盘，返回路径与预览；Bash 输出直接写日志文件。
2. Micro：达到压力阈值后，将较旧工具结果替换为稳定的文件引用，保留近期结果。
3. Auto：超过自动阈值时，复用有效会话笔记或生成九项结构化摘要，并保留近期完整轮次。
4. Blocking：仍超过预算时停止发送普通模型请求。
5. Reactive：收到明确的上下文超限错误后，更积极地压缩并重试一次。

模型窗口通过 `--context-window` 指定，默认保守使用 128000。
输出预留通过 `--output-reserve` 指定，默认 8192。自动阈值为窗口减输出预留再减
13000 安全余量；阻塞阈值使用 3000 余量。工具定义、项目指令也计入本地估算。
这是 UTF-8 大小的保守估算，不是供应商精确 tokenizer；请按实际模型配置窗口。

压缩后会按预算重新读取最近通过文件工具查看的文件范围；项目指令每次请求重新加载。
原始历史不删除，摘要含有原始 transcript 路径。工具调用与对应结果始终整组保留。
连续三次自动压缩失败后停止自动尝试；手动压缩成功会重置计数。
摘要输入过大时按完整轮次分段生成并合并，覆盖全部输入；单轮无法容纳时安全失败，
保留原上下文。不会静默删除未经总结的旧需求。

```text
/compact                         手动压缩
/compact focus on migration      带补充要求的压缩
/compact --from 1 --to 4          仅压缩指定完整轮次，保留两侧消息
/tasks                           查看当前进程任务
/sessions                        查看会话 ID
/help
/exit
```

部分压缩范围从 0 开始，左闭右开；每个 assistant 消息及其全部工具结果属于同一轮。
`--no-auto-compact` 可关闭自动摘要，保留手动压缩和请求预算检查。

`--notes` 启用后台会话笔记：模型生成笔记，程序保存内容与覆盖边界；压缩时优先复用。
后台写入会验证上下文代次，避免旧笔记覆盖新状态。默认关闭，不增加常规调用成本。
笔记与普通摘要生成共用同一模型；没有另建一个工具型 agent。

Micro 是可移植的本地实现，不依赖 Anthropic 内部 cache_edits。
模型摘要调用是独立调用，目前不复刻 CC 的缓存共享 fork 优化。

## 会话与恢复

```text
~/.mini-harness/sessions/<session_id>/
├── state.sqlite        # LangGraph checkpoints
├── runtime.sqlite      # 运行元数据、笔记和工具执行记录
├── transcript.jsonl    # 原始消息和执行/压缩事件，追加写入
└── artifacts/          # 完整工具输出、进程日志、cwd 回传文件
```

会话目录可通过 `--session-dir` 更改。同一会话只允许一个写入者。
运行元数据与异步 checkpoint 分库，避免后台完成通知或笔记写入与 checkpoint 争锁。
恢复加载当前有效上下文，直接复用已保存摘要。
已经落盘的工具结果按调用 ID 复用；已开始但结果未知的调用不会自动重跑，
而是让模型先检查文件或日志。恢复上下文也不会回滚已经修改的项目文件。

交互时 Ctrl-C 取消当前执行并保留后台任务；Ctrl-D 或 `/exit` 退出并清理进程。

## 阅读代码

| 文件 | 职责 |
| --- | --- |
| `agent.py` | create_agent 组装、checkpoint 生命周期、工具结果恢复 |
| `tools.py` | 文件操作和模型工具的薄封装 |
| `processes.py` | Bash、cwd、stdin、后台进程、完成事件和取消 |
| `context.py` | 压缩预算、规则清理、摘要、后台笔记和超限恢复 |
| `prompts.py` | 普通 agent 与摘要提示词 |
| `session.py` | 会话锁、SQLite 元数据、JSONL、输出文件 |
| `cli.py` | input()、流式显示、CLI 命令和中断 |

子 agent、MCP、skills、任务/计划、长期记忆、权限和 hooks 的具体方案继续逐模块讨论，
当前没有为它们增加占位实现或通用插件框架。

## 验证

```bash
uv sync --all-extras
uv run pytest -q
uv run ruff check .
uv run mini-harness --help
```

测试使用真实临时文件、Bash 进程、SQLite 和 LangGraph，只替换外部模型调用。
另有本地 HTTP/SSE 测试，通过真实 OpenAI 适配器验证 CLI 工具调用、流输出和会话恢复。
真实 API 已验证上述 DeepSeek 配置；其他供应商仍需分别验证。

详细设计与实施记录见 [spec](docs/superpowers/specs/2026-10-17-harness.md)
和 [plan](docs/superpowers/plans/2026-10-17-bootstrap.md)。
