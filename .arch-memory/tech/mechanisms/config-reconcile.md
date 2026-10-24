# 配置热重载协调 config-reconcile

- 方案：各服务向 ConfigManager 注册 reconciler；重载时逐个执行新旧配置协调，全部成功后才替换生效配置，任一失败不应用新配置。
- 不变量：进程不可变参数（管理 API 与 MCP 监听地址端口、系统数据目录、日志根级别）发生变化时只标记 requires_restart 返回，不得在进程内热改。
