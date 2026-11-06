# 纯稠密向量存储与集合迁移（Dense-only Storage）

关联功能单元：FEAT-QS-01、FEAT-MQ-02、FEAT-EMB-01 / FEAT-EMB-02、FEAT-MCPS-02、FEAT-MGMT-04。

## 1. 启用依据与终态边界

- 原 `codebase` 集合同时声明稠密与稀疏向量；仅重写或删除 Point 的稀疏数据不能删除集合级稀疏 Schema，因此最终采用纯稠密新集合迁移，而不是保留兼容 Schema。
- 当前 Embedding 仅使用 `gemini-embedding-2`（3072 维 Cosine），检索唯一入口为 `dense_search`。`EmbeddingResult`、`PointData`、Outbox 与 Qdrant Point 均只携带 `dense_vector` / `dense_vectors`，不保留稀疏兼容字段、混合检索包装或 RRF 死代码。
- `QdrantStorageService._validate_collection_schema` 要求持久化集合恰好包含配置指定的唯一 `dense_vectors`，维度为 3072；任何稀疏 Schema、额外/缺失稠密向量或维度不匹配均阻断启动。

## 2. 集合命名与访问契约

- 物理集合：`codebase_dense_only`，仅含 `dense_vectors`（3072 维 Cosine）。
- 对外访问名：`codebase`，作为 Qdrant Alias 指向 `codebase_dense_only`，因此配置、MCP、管理 API 与存储服务无需改变集合名称。
- 禁止把 `global_dev_reference`、`project_source_qa` 纳入本项目的升级或清理范围；它们不属于本次授权操作对象。
- 本机 qdrant-client 的别名写接口为 `update_collection_aliases`；不得凭其他版本经验误用不存在的方法。
- 集合存在性必须用 `AsyncQdrantClient.collection_exists()`（会解析 Alias）判断；`get_collections()` 只返回物理集合，若用它判断会把别名误判为“不存在”，进而 `create_collection` 被 Qdrant 以 “Alias with the same name already exists” 拒绝，导致服务启动失败。
- 依赖约束：`qdrant-client>=1.12.0,<1.19.0`。Qdrant 要求客户端与服务端 major 一致且 minor 相差不超过 1；当前服务端为 1.17.x，无上限会装到 1.20.x 并产生不兼容警告。

## 3. 已执行的迁移与验证证据

1. 迁移前停止 CodebaseRAG Windows 服务，避免并发写入。
2. 创建源集合快照并下载至容器外：`C:\ProgramData\CodebaseRAG\backups\upgrade-cleanup-20261106-053625\codebase-6337435039344044-2026-11-06-13-49-06.snapshot`；文件大小 257171456 bytes，SHA-256 `e828d9f60e66dab93914d5524ee5e581f9fb3f9f9839dc0a560ab73caa4a5650`。
3. 新集合复制全部 8887 个 Point 的 ID、payload 与 `dense_vectors`，并复制源 payload 索引定义；不复制任何稀疏向量或稀疏 Schema。
4. 对全部 8887 个 Point 逐一比较 ID、规范化 payload 与完整稠密向量 SHA-256，全部一致；目标集合与 `codebase` 别名的稠密查询均返回结果。
5. 全量验证通过后删除旧 `codebase` 物理集合并建立同名 Alias；最终 Point 数仍为 8887，稀疏 Schema 不存在。

## 4. 回滚与禁止项

- 配置与环境备份目录：`C:\ProgramData\CodebaseRAG\backups\upgrade-cleanup-20261106-053625`。安装版旧 release 保留用于应用回滚。
- 数据回滚必须从上述容器外快照恢复到独立集合并重新指向 Alias，禁止在唯一数据副本上直接试验恢复。
- 禁止重新引入 `SparseVector`、`sparse_vector`、`query_sparse`、`hybrid_search`、RRF 参数或只转发 `dense_search` 的无意义混合检索包装。
- 禁止为兼容旧 Schema 而跳过启动校验；发现旧集合时必须显式迁移，不得静默接受。
