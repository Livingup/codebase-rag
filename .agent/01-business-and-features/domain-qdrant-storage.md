# 持久化存储 qdrant_storage

## 一、业务域职责边界与域内铁律

- 负责：Qdrant Server 的连接与 collection/schema 管理、按文件版本的批量写入（等待确认）与分页读取、完整知识集判定、project_id 迁移。
- 不负责：内存检索视图（→ memory_qdrant）；当前 HEAD 版本选择、知识生成与任务重试（调用方给定项目与版本范围）。
- 域内铁律：批量写入必须等待 Qdrant 确认后才向调用方报告成功；原始 point 命中不得视为完整文件版本——只有 chunk 0..chunk_count-1 齐全的知识集才构成可读版本；project_id 迁移只改写 payload 身份字段，保留 point ID 与向量。

## 二、业务功能单元明细

### [FEAT-QS-01] 连接生命周期与传输故障自愈
- **业务目标**：Qdrant 客户端长连接管理与传输层故障恢复。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [初始化]：创建客户端失败抛 QdrantConnectionError；`get_collections` 探活；确保 collection 存在并注册配置 reconciler。
  2. [连接平滑替换]：reconcile 时连接参数变化 → 新建客户端替换旧客户端（新连接失败抛 QdrantConnectionError 并关闭新客户端）。
  3. [自愈范围]：`_request_with_recovery` 仅对 `httpx.PoolTimeout` / `ResponseHandlingException`（含 `__cause__` 链）重试；恢复动作是 `_reset_client`（锁内换新、关旧）；其他异常不触发客户端重建。
- **级联依赖功能单元（Cascade Links）**：FEAT-COMMON-04（reconciler 注册）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-config-reconcile.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/qdrant_storage/service.py）：`src/codebase_rag/services/qdrant_storage/service.py`（连接与自愈段）
  - 域异常体系（services/qdrant_storage/errors.py）：`src/codebase_rag/services/qdrant_storage/errors.py`

### [FEAT-QS-02] 确定性点写入（upsert_file_points）
- **业务目标**：单文件版本知识点批量落库，点 ID 幂等可重写。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [knowledge_set_id]：`sha256(规范 JSON{project_id, file_version_id, payloads(排序键)})`——一次知识提交全部分块的确定性集合标识。
  2. [点 ID 公式]：`uuid5(NAMESPACE_URL, "codebase-rag:point:{project_id}:{file_version_id}:{knowledge_set_id}:{chunk_index}")`——同输入必得同点 ID，重写即覆盖。
  3. [payload 与向量规范]：每点只写 `dense_vectors`（3072 维 Cosine），payload 含 `project_id`、`file_version_id`、`file_path`、`file_type`（`"text"` | `"image"`）、`entity_type`、`entity_name`、`business_description`、`blob_hash`、`knowledge_set_id`、`chunk_index`、`chunk_count`。
  4. [批量确认]：按 `upsert_batch_size=100` 分批 `upsert(wait=True)`；异常包装 `QdrantOperationError` 上抛；返回 `True` 即服务端确认。
- **级联依赖功能单元（Cascade Links）**：FEAT-KS-01（调用方）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-knowledge-outbox.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/qdrant_storage/service.py）：`src/codebase_rag/services/qdrant_storage/service.py`（写入段）*(同类参考样板：确定性 uuid5 点 ID)*
  - 域数据模型（services/qdrant_storage/models.py）：`src/codebase_rag/services/qdrant_storage/models.py`

### [FEAT-QS-03] 完整知识集读取（get_file_points / get_files_points）
- **业务目标**：按版本批量读回"可用"的完整知识集。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [分页]：按 `scroll_batch_size=100` 分批 scroll（filter=project_id + MatchAny(file_version_ids)，带向量）。
  2. [集合选择]：按 fvid 分组后 `_select_readable_knowledge_set`：优先返回完整 chunked 集中 knowledge_set_id **字典序最小**者（`sorted(complete_sets.keys())[0]`）；无完整集才回退到无 knowledge_set_id 的 legacy 记录。
  3. [完整集判定算法]：按 knowledge_set_id 分组收集 chunk_index→record，当 `set(索引集合) == set(range(chunk_count))` 且 chunk_count>0 才算完整。
- **级联依赖功能单元（Cascade Links）**：FEAT-WF-02（staging 加载消费方）、FEAT-KS-04（内存镜像消费方）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-knowledge-outbox.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/qdrant_storage/service.py）：`src/codebase_rag/services/qdrant_storage/service.py`（读取段）

### [FEAT-QS-04] 存在性检查与 project_id 迁移
- **业务目标**：内容寻址去重的核心接口；project_id 派生规则升级后的存量重归属。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [存在性判定]：同批 scroll 仅取 payload 四字段（file_version_id/knowledge_set_id/chunk_index/chunk_count）；存在完整集 **或** 存在任一 legacy 记录（payload 缺 knowledge_set_id）即为存在——不把裸 payload 命中当可用集。
  2. [迁移 no-op]：新旧 project_id 相等返回 0；先 count(exact=True) 供观测，为 0 直接 no-op。
  3. [迁移方式]：`set_payload(payload={"project_id": new}, FilterSelector(old_filter), wait=True)`——只改 payload 字段，不重建点、不动向量与点 ID；计数或改写失败抛 QdrantOperationError。
  4. [存量向量重写]：`scripts/rewrite_dense_vectors.py` 通过 `load_config` + `ConfigManager` 读取真实配置，按 `management_api.registry_path` 的 JSON 数组注册表解析本地仓库，按 `qdrant_storage.collection.name` 与 `collection.dense_vector.name` 执行 scroll、`update_vectors(wait=True)` 和必要的 `set_payload(file_type)`；仅处理 `business_description` 非空的 Point，批大小不超过 dense Provider `batch_size`。正式迁移才构造并初始化 EmbeddingService；`dry_run=True` 只读扫描，不构造 EmbeddingService、不调用 Gemini、不写 Qdrant。图片 Point 必须按 payload `blob_hash` 经 `GitOperationsService.get_file_bytes` 读取对应版本，读取失败时记录告警并回退为文本嵌入。
- **级联依赖功能单元（Cascade Links）**：FEAT-WF-03（内容寻址去重消费方）、FEAT-MGMT-01（迁移触发方）、FEAT-EMB-02（重算向量）、FEAT-GIT-03（按 blob 版本读取图片）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/qdrant_storage/service.py）：`src/codebase_rag/services/qdrant_storage/service.py`（存在性检查与迁移段）
  - 运维脚本（scripts/）：`scripts/rewrite_dense_vectors.py`

## 三、域内量化参数速查

| 参数 | 默认值 | 配置键 |
| :--- | :--- | :--- |
| 并发上限 | 4 | `qdrant_storage.max_concurrency` |
| 连接池大小 | max(max_concurrency×2, 10) | 派生值 |
| 传输故障重试次数 / 基础退避 | 3 次 / 0.25s 指数退避 | 代码常量 |
| 写入批大小 | 100 | `qdrant_storage.upsert_batch_size` |
| 读取批大小 | 100 | `qdrant_storage.scroll_batch_size` |
