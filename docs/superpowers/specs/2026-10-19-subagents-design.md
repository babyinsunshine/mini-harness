# 独立会话子智能体

用户已确定实现范围：CLI 学习项目，复用 Harness，机制简洁，不新增依赖。

- 同进程 asyncio.Task 并行运行；每个子智能体在父会话的 subagents/<id> 中保存独立 Session，复用文件锁。
- 子会话保存 parent_session_id 和 parent_session_dir，继承固定项目根目录，初始 cwd 使用父会话当前目录。
- 复用模型、压缩、文件和进程工具；不提供 update_plan 或子智能体管理工具。
- 审批按子会话 cwd 检查，策略、会话信任和审批回调与父会话一致；CLI 显示审批来源。
- spawn_agent(task, timeout=600) 立即返回 ID；wait_agent(agent_id) 等待并取得本轮结果。
- 一轮完成进入 waiting，通知主智能体；主模型选择 continue_agent(agent_id, message, timeout=None) 或 close_agent(agent_id)。继续复用原会话，重新计时，缺省沿用原时限。
- 超时进入 timed_out，清理该轮会话和 Bash 进程，反馈主智能体；可以明确继续。
- 退出或中断时取消运行任务并记为 interrupted；恢复父对话的下一轮自动继续这些原子会话，补齐中断工具结果，不直接重放命令。
- waiting/timed_out/failed 不自动续跑；closed 不再继续。所有对话记录持久保存。
- 复用现有后台队列，让子结果既能在主循环中收集，也能在 CLI 空闲时触发处理；子输出不混入主输出流。

验证：真实 LangGraph、SQLite、锁、文件和 Bash；仅模型 API 与终端输入替换为确定性测试。
