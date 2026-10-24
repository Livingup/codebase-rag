# 知识同步 knowledge_sync

## 边界

- 负责：客户端与工作流提交的文件版本知识受理判定、JSON 任务外发箱、文件与版本两级互斥锁、重试入口与 project_id 迁移。
- 不负责：自动重试调度（补偿循环属 workflow）；知识生成与向量化(→ agent、embedding)；持久化写入(→ qdrant_storage)；对外受理语义表达(→ mcp_service)。

## 关键概念

- 外发箱（outbox）：按 file_version_id 存放的待发布任务与按文件路径存放的最新版本索引，服务重启后据此找回未发布知识。
