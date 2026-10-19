# 四项实测问题的最小修正

> 本会话内执行，使用 executing-plans；保留当前工作区，最后一次独立增量审查。

目标：改进长任务预算、token 估算、Bash 凭据隔离和诊断，同时优先清晰。
范围是已有机制的有界修正，不加新模块、配置框架或通用重试系统。

- 循环：继续用 --max-steps 表示图节点数；新会话默认由 200 提高到 1000，明确帮助说明。旧会话保留保存值，可显式覆盖。
- token：ContextManager 在内存保存上次主请求的输入快照、实际 input_tokens 和本地估计。前缀相同时用实际输入加估计增量；前缀改变或无 usage 时退回粗估。启动/恢复的首次请求仍粗估，不持久化额外锚点。
- Blocking 与 Auto 都使用同一个校准后的计数；摘要和结构性裁剪之后按新历史粗估。CLI 对支持 stream_usage 的模型适配器开启该选项。
- Bash：只从启动环境过滤当前支持的模型凭据变量，保留普通项目环境；env.sh 仍允许有意传入项目所需值。
- 诊断：现有事件增加 model_end（usage、结束原因、耗时）及工具调用 ID/退出码；非零 Bash 结果正确记录 error。CLI 展示异常因果链，不加日志框架或新 flag。

文件：agent.py、graph/nodes.py、context/{manager,compact}.py、tools/processes.py、responses.py、cli/{config,main,interactive,display}.py。

## 清单

- [x] 写最小行为回归并观察 RED：40 轮默认循环、实际 usage 校准与 Blocking、密钥环境隔离、模型/工具事件和 CLI 异常根因。
- [x] 最小实现并运行对应 GREEN；扩展本地 SSE 验证流式 usage 的真实适配器传递。
- [x] 更新 README，检查源码增量与阅读路径。
- [x] 全量 pytest、Ruff、CLI 检查；一次独立审查和统一必要修正。最终 163 项通过，产品源码净增 66 行，无新模块。

## Review Focus

- 改写历史、Micro、完整/部分压缩后不能继续套用旧请求用量；无 usage 与重启保持可用。
- Auto/Blocking 使用一致计数，Reactive 成功后校准对应实际重试输入。
- 默认预算足以完成 40 轮，用户设置的预算仍有效，旧会话设置保留。
- 模型凭据留在父进程，普通环境、cwd、stdin、env.sh 行为保持。
- usage 在真实流式适配器中保留；事件不污染 stdout，非零退出正确入日志，错误保留因果链。
