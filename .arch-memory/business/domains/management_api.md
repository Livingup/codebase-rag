# 管理面 management_api

## 边界

- 负责：项目登记、注销与重启恢复、管理检索、初始化/同步任务触发、健康与客户端状态、配置热重载接口和本地 Admin UI。
- 不负责：面向编程客户端的 MCP 工具表达(→ mcp_service)；知识生产编排(→ workflow)；配置协调机制本身(→ 公共 config)。

## 关键概念

- project_id：由仓库根 commit 派生的稳定身份，同一仓库的不同路径表示必须解析为同一 ID。
