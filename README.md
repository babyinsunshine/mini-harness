# mini-harness

一个用于学习的 CLI coding agent：LangGraph 组织执行循环，LangChain 提供模型与工具接口。
以代码易读、机制精简为先，功能按模块扩展。

## 快速开始

需要 Python 3.11+、Bash、POSIX 系统和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
export OPENAI_API_KEY='YOUR_API_KEY'
uv run mini-harness
```

默认使用 DeepSeek Flash，接口为 `https://api.deepseek.com`。
切换兼容接口可传入 `--model openai:MODEL_ID --base-url https://API_ENDPOINT/v1`；
使用 Anthropic 时安装 `--extra anthropic` 并设置对应环境变量。

## 功能

- **基础工具**：Bash、文件读写与精确编辑；后台任务、stdin 和进程清理。
- **上下文**：工具结果裁剪、自动/手动摘要、超限恢复，完整长输出保存到磁盘。
- **会话**：SQLite 检查点、中断恢复、任务清单和缓存统计。
- **子智能体**：独立会话并行工作，支持继续、关闭和超时清理。
- **文件协作**：短时文件锁与各会话的哈希校验，冲突时提示重新读取。
- **扩展**：Skills 和 MCP 都通过工具按需发现与调用。

## 常用操作

```bash
uv run mini-harness --cwd /path/to/project
uv run mini-harness -p '查看项目并解释测试入口'
uv run mini-harness --list-sessions
uv run mini-harness --resume SESSION_ID
uv run mini-harness --help
```

交互命令：`/help`、`/compact`、`/tasks`、`/stats`、`/sessions`、`/exit`。
默认执行权限审批；`--full-access` 跳过全部审批。当前执行环境使用本机权限，尚无操作系统沙箱。

## Skills 与 MCP

项目技能放在 `.agents/skills/<name>/SKILL.md`，可附带 `references/` 和 `scripts/`。

MCP 支持 stdio 和 Streamable HTTP；本机配置放在已忽略的 `.mini-harness/` 目录：

```bash
uv run --extra mcp mini-harness --mcp-config .mini-harness/mcp.json
```

通用配置格式及 [Windows-MCP 示例](examples/mcp.windows-wsl.json) 见
[机制说明](docs/architecture.md#mcp)。先复制模板到本机配置目录，再填写实际路径和认证信息。

## 代码结构

```text
src/mini_harness/
├── agent.py       # 组装会话、资源与图
├── graph/         # 状态、节点和执行边
├── context/       # 上下文压缩与预算
├── tools/         # 文件、进程、权限、计划、子智能体、Skills、MCP
├── session.py     # 会话元数据、执行记录和文件产物
├── subagents.py   # 子智能体生命周期
├── prompts/       # 提示词
└── cli/           # 输入、事件显示和统计
```

执行流程见 [状态图](docs/agent-graph.md)，各模块原理见 [机制说明](docs/architecture.md)。

## 本地配置与隐私

密钥通过环境变量传入；实际 MCP 地址、个人路径和认证信息保存在本机配置中。
`.env`、本地 MCP JSON、`.mini-harness/`、会话数据库、日志及 worktree 已由 `.gitignore` 排除。
`examples/` 只保留通用模板和占位符。

会话默认保存在 `~/.mini-harness/sessions/`，可通过 `--session-dir` 调整。
同项目的内置文件工具共享锁；Bash、MCP 和外部编辑器的写入需要另行协调。

## 开发验证

```bash
uv sync --locked --all-extras --dev
uv run --no-sync ruff check .
uv run --no-sync pytest -q
```
