# 显式 LangGraph 核心与简洁 CLI 事件

用户已确认迁移到 LangGraph：面向新手、模块化、机制直接可读，优化工具输出与执行事件，避免额外框架和大量兜底。

## 结构与边界

参考 DeerFlow 1.x 的 graph/builder.py、nodes.py、types.py 分工：
https://github.com/bytedance/deer-flow/tree/main-1.x/src/graph

- graph/types.py：消息状态、少量路由字段；不把数据库或进程句柄写入检查点。
- graph/nodes.py：collect → compact → model → response → permission → tools 六个阶段。
- graph/builder.py：显式节点与条件边，compile(checkpointer=...)。
- agent.py：资源生命周期、会话恢复、stream/events 和手动压缩入口。
- context.py、permissions.py 改为普通服务；保留 compact.py 的算法，不保留 Middleware 兼容层。

正常文本结束；完整工具调用逐个审批、执行、记录结果，随后收集后台事件并检查上下文。损坏、空白或截断回复由 response 节点补未执行反馈，最多修复两次。回复校验与反馈合并；不新增修复模型。

压缩在下一次主请求前执行，包含本批全部工具结果。实际超限仍只做一次 Reactive 重试，成功后才提交裁剪；摘要截断不替换历史。沿用现有日志补结果机制，不重放已执行工具。

工具按模型给出的顺序逐个审批、执行，保证审批使用当前 cwd；代价是暂不并行执行工具。批准仅用于本次调用。CLI 原有 Ctrl-C、EOF、审批和后台任务行为保持。

## 输出与事件

- read_file 返回 output、has_more、next_offset；超长选区继续落盘并返回预览。
- edit_file 返回简短 unified diff，长结果沿用现有落盘机制。
- Bash 返回前后共约 2000 字节预览、truncated 和 output_bytes，保留完整日志。
- Harness.events() 使用 LangGraph messages/custom 流，输出文本、模型开始、工具开始/结果、压缩、修复及后台事件；stream() 保留只输出文本的接口。
- CLI 正文写 stdout，状态与简短工具预览写 stderr。保持普通终端，无新 UI 依赖或事件总线。

## 验证

真实 graph 和临时文件验证六阶段路由、权限顺序、中断恢复、压缩持久化、坏回复有限修复。保留本地 SSE 适配器测试并覆盖截断工具参数。测试读取续页、diff、日志尾部与 CLI 状态事件；运行完整 pytest、Ruff、diff 检查。

本次不加入文件版本控制、回滚系统、MCP、子智能体或跨进程任务恢复。保存迁移前工作区快照，保留原有未提交工作。
