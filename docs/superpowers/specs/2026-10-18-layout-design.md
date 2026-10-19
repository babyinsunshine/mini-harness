# 按职责整理学习用 harness

用户要求：同类代码按需放在一起，方便扩展；以少量代码和低阅读难度为优先。

本次只整理既有功能，直接沿用已授权的本会话内实施方式。四个目录：

- graph/：现有六阶段图；保留 builder、nodes、types 分工。
- context/：manager 负责生命周期，compact 保留各层算法，policy 集中预算配置。
- tools/：factory 明确列出模型工具，files、processes、permissions 各负责一类机制。
- cli/：config 处理参数/恢复，main 负责启动，interactive 处理终端循环，display 负责展示。

根目录保留 agent.py、session.py、responses.py、prompts.py 等直接入口或共享模块。
__init__.py 仅作包说明；使用明确模块导入，不增加旧路径转发层、插件注册表或通用基类。

简化以现有代码中的重复操作为限，优先直白分支、单一配置来源和明确名字。
不删除压缩、权限、中断恢复的有效保护，不以压行数替代可读性。
CLI 命令、事件结构、会话文件、模型工具名称与参数保持兼容。

验证：迁移前后完整 pytest，Ruff，CLI 两种入口，构建安装包后验证新模块被打包。
README 更新职责树、阅读顺序与新增功能位置。独立审查只检查本次快照增量。
