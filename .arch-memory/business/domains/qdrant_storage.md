# 持久化存储 qdrant_storage

## 边界

- 负责：网络 Qdrant 的连接与 collection/schema 管理、按文件版本的批量写入与分页读取、完整知识集判定、project_id 迁移。
- 不负责：内存检索视图(→ memory_qdrant)；当前 HEAD 版本选择、知识生成与任务重试（调用方给定项目与版本范围）。

## 关键概念

- 完整知识集：同一 file_version_id 下 chunk 0..chunk_count-1 全部存在的记录集合；缺失任一 chunk 不构成可读版本。
- knowledge_set_id：一次知识提交全部分块的确定性集合标识，用于并存多套描述集合时选取。
