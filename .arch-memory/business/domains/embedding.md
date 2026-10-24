# 向量服务 embedding

## 边界

- 负责：稠密（Gemini）与稀疏（DashScope）向量生成、token 计数、批处理与单文本长度门禁。
- 不负责：知识点分块决策（调用方在提交前完成拆分）；向量持久化与检索(→ qdrant_storage、memory_qdrant)。

## 关键概念

- 文档模式 / 查询模式：稀疏向量生成区分 text_type=document 与 query，索引与检索使用不同模式。
