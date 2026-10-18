# 工具权限审批设计

## 目标与范围

为现有 CLI 增加工具执行前的权限审批。用户要求：拒绝明确危险的执行，询问可能有风险的指令，普通文件读写等安全操作直接允许。
延续项目原则：规则集中、代码可读，不增加通用策略框架，不改写 create_agent 的工具循环。
以下是供评审的具体方案；文件范围采用项目目录和当前会话目录这一建议默认值。

## 方案选择

- 采用自定义 `PermissionMiddleware.awrap_tool_call`，使用小型规则函数产生 allow / ask / deny。
- CLI 提供异步审批回调；Middleware 不直接调用 input()。
- LangChain 内置 HumanInTheLoopMiddleware 适合持久化等待审批，本次只做当前运行期间的单次确认。
- 不把检查散到每个工具中；通过当前 agent 注册的工具调用统一进入 Middleware。

## 默认规则

| 结果 | 操作 |
| --- | --- |
| allow | 项目和当前会话目录中的普通 read_file / write_file / edit_file；查询、停止当前会话进程；可可靠识别的简单只读 Bash 命令 |
| ask | 目录外文件访问、env.sh 修改、删除文件、安装依赖、联网或执行脚本、stdin 写入，以及不能确定安全性的 Bash 命令 |
| deny | 可明确识别的递归强制删除、磁盘格式化或写裸设备、关机重启、破坏性 Git 操作（如 reset --hard、clean -f、push --force）、关键系统路径写入 |

文件路径沿用 Session.resolve()，按解析后的实际路径判断所在目录。项目根目录沿用会话保存的 project_root，cd 不扩大自动允许范围。
关键系统写入路径包括 /etc、/boot、/proc、/sys、/dev 下的目标；普通目录外访问询问。
env.sh 会被 Bash 自动 source，因此修改它总要确认；读取它可以直接允许。
本会话的 state.sqlite、runtime.sqlite 和 transcript.jsonl 允许读取，但禁止通过模型文件工具改写，避免破坏检查点和恢复记录。

Bash 只对可识别的简单命令和受限参数组合自动允许，例如 pwd、ls、cat、head、tail、rg，以及 git status/diff/log/show。
重定向、命令替换、解释器、命令包装器、复杂 Shell 语法或未知命令默认询问，不通过猜测放行。
明确识别到危险行为时拒绝；初版不尝试证明任意脚本的内部行为安全，也不提供文件系统或网络沙箱。
规则函数必须区分实际命令和用于搜索、输出的普通字符串，避免把参数中出现危险命令名称直接当成执行。
未知工具默认询问。deny 不提供用户放行选项；ask 获准只允许当前调用，不生成后续调用的通行规则。

## 调用顺序与记录

```text
模型产生工具调用
    → PermissionMiddleware 判定
    → ask 时等待 CLI 的本次答复；deny 或未获批准时直接返回错误 ToolMessage
    → 获准后进入 ExecutionMiddleware，记录 started
    → handler 执行工具，原有逻辑保存结果
```

权限检查发生在 start_execution() 之前。被拒绝的调用返回匹配 tool_call_id 的 ToolMessage，说明未执行和原因，并写入原始记录；不伪造一次已经开始的执行。
允许和拒绝的权限决策以现有 Session.record() 记录，关联 call_id；不增加数据库表。
已经执行并有保存结果的调用仍沿用现有恢复逻辑，不再次询问或重放。

## CLI 交互与生命周期

- 复用唯一输入线程，由事件循环协调普通输入、审批和当前 agent 任务，避免多个 input() 竞争。
- 显示工具名、目标路径或完整命令、询问原因，以及 Allow once? [y/N]。
- 只接受明确的 y/yes 为同意；回车或拒绝不执行。多个并行工具调用的审批逐个展示。
- Ctrl-C 取消当前轮及待审批操作；EOF 或 /exit 退出并完成原有进程清理，不执行待审批操作。
- 后台进程完成事件仍排队，待审批期间不抢占当前 agent 轮次。
- -p 非交互模式不读取审批输入：allow 正常执行，ask 和 deny 返回拒绝结果给模型。
- 本次不记住“以后总是允许”，不添加自动放行开关。重启后尚未执行的调用沿用 pending_results() 补未执行结果，不保留待审批状态。

## 文件职责

- 新增 permissions.py：规则判定、原因文本与 PermissionMiddleware。
- agent.py：注入审批回调，调整 Middleware 顺序；保留原有执行记录和恢复机制。
- cli.py：接入审批请求并复用现有输入线程；实现非交互拒绝和取消处理。
- README.md / prompts.py：说明执行策略和权限拒绝的含义；新增规则不通过动态改写系统提示实现。

## 验证

- 规则测试覆盖允许、询问、拒绝，以及真实路径、参数、引号和复杂 Shell 的边界；危险字符串只用于判定，不执行危险命令。
- Graph 集成测试使用真实临时文件和无害命令，验证允许有实际效果，拒绝与未同意没有副作用，审批前没有 started 记录，结果与 tool_call_id 配对。
- 验证已有成功、失败和中断记录恢复时不重放工具。
- CLI 测试覆盖同意、拒绝、默认拒绝、连续审批、取消/EOF、非交互模式以及后台完成事件，不依赖付费模型。
- 运行完整 pytest、Ruff 和 git diff --check，保留已有上下文、进程和恢复测试的覆盖。
