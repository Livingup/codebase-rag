# 持久化存储 qdrant_storage · 技术设计

## 写入与读取

- 不变量：批量写入必须等待 Qdrant 确认后才向调用方报告成功。
- 不变量：原始 point 命中不得视为完整文件版本；只有 chunk 0..chunk_count-1 齐全的知识集才构成可读版本，并存多套完整集时取字典序最小。
- 方案：project_id 迁移只改写 payload 身份字段，保留 point ID 与向量。
