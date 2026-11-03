# 内存检索实例 memory_qdrant

## 一、业务域职责边界与域内铁律

- 负责：内存 Qdrant 实例生命周期与缓存状态的统一管理（创建/提升/淘汰/销毁/懒重建）、活跃实例映射、混合检索。
- 不负责：实例创建、数据加载、提升、清理这一整条工作流的编排（→ workflow）；持久化读写（→ qdrant_storage）。
- 域内铁律：本服务独占内存实例生命周期与缓存状态；等待共享懒加载任务时不得持有实例缓存锁（重建回调自身会操作实例缓存）；实例身份键是本地仓库副本 repo_key（`normalize_repo_key` 规范化路径，见 FEAT-COMMON-05）而非 project_id——同一 project_id 的多个本地副本各持独立实例，严禁共用或互相顶替。

## 二、业务功能单元明细

### [FEAT-MQ-01] 实例创建与 LRU 缓存
- **业务目标**：按 `repo_key:branch@commit` 键创建内存 Qdrant 实例并纳入 LRU 缓存。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [实例身份]：`create_instance(repo_key, branch_name, commit_hash, *, project_id=None, staging=False)`——repo_key 是实例主键；project_id 仅为实例元数据（供 FEAT-MQ-06 全量销毁按分区过滤），缺省取 repo_key；编排方（workflow）必须显式传真实 project_id。
  2. [缓存命中]：非 staging 创建命中缓存 → touch + move_to_end 后直接返回既有实例。
  3. [staging 键规则]：staging 实例 key = `{base_key}#staging:{uuid4}`，永不复用；创建后加入 `_loading` 保护集合。
  4. [容量保护]：缓存满时先淘汰（FEAT-MQ-04）；staging 创建传 `protected_repo_key` 防止淘汰同副本当前活跃实例，且 `allow_over_capacity=True` 允许超容量（无安全淘汰对象时沿用允许超容量策略，不新增硬上限；提升后不主动缩容）。
  5. [向量配置]：按配置建 dense（含 HNSW m/ef_construct，未启用则 None）+ sparse 双向量。
- **级联依赖功能单元（Cascade Links）**：FEAT-WF-02（staging 创建方）、FEAT-COMMON-06（repo_key 规范化）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-memory-instance-lifecycle.md`、`.agent/04-tech-decisions/tech-repo-copy-identity.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/memory_qdrant/service.py）：`src/codebase_rag/services/memory_qdrant/service.py`（create_instance 段）
  - 域数据模型（services/memory_qdrant/models.py）：`src/codebase_rag/services/memory_qdrant/models.py`

### [FEAT-MQ-02] 混合检索（dense+sparse RRF 融合）
- **业务目标**：副本活跃实例上稠密+稀疏双路召回并 RRF 融合排序。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [实例获取]：`hybrid_search(repo_key, ...)` / `dense_search(repo_key, ...)` 取该副本的活跃实例；无活跃实例 → 触发懒重建（FEAT-MQ-05）。
  2. [top_k 钳制]：`k = min(top_k or default_top_k, max_top_k)`。
  3. [双路召回]：dense、sparse 各自 `query_points(limit=prefetch_limit, with_payload=True)`。
  4. [RRF 融合]：`score += weight/(rrf_k + rank + 1)`（两路累加同 ID）；取前 k；**不做分数阈值过滤**；`touch_search()` 更新检索活跃时间。
- **级联依赖功能单元（Cascade Links）**：FEAT-MCPS-02（检索消费方）、FEAT-MGMT-04（REST 搜索消费方）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-repo-copy-identity.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/memory_qdrant/service.py）：`src/codebase_rag/services/memory_qdrant/service.py`（hybrid_search 段）

### [FEAT-MQ-03] 活跃实例管理与 staging 晋升
- **业务目标**：维护 repo_key→活跃实例映射，完成 staging 实例原地转正。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [set_active_instance]：要求实例已在缓存（否则 InstanceNotFoundError），O(1) 设 `_active[repo_key]`。
  2. [set_active_handle（staging 晋升）]：锁内将 handle 移出 `_loading`；IF handle 是 staging key → 关闭并移除旧正式 key 实例，把 staging 条目改名为正式 key，再设 `_active[inst.repo_key]` + touch。
  3. [状态机]：loading →（加载完成并提升）active；loading →（加载失败，由创建链路清理）销毁；active →（同副本另一实例提升）inactive →（非活跃清理或 LRU 淘汰）销毁。
  4. [多副本并存]：同 project_id 的两个副本可同时持有各自活跃实例，互不影响。
- **级联依赖功能单元（Cascade Links）**：FEAT-WF-02 / FEAT-WF-05（提升调用方）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-memory-instance-lifecycle.md`、`.agent/04-tech-decisions/tech-repo-copy-identity.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/memory_qdrant/service.py）：`src/codebase_rag/services/memory_qdrant/service.py`（活跃实例管理段）

### [FEAT-MQ-04] LRU 淘汰与空闲清扫
- **业务目标**：缓存容量与内存占用控制。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [淘汰优先级]：缓存满时依次尝试：①最旧的非活跃且非 loading 实例 → ②空闲超时的活跃实例（idle_since = last_search_time or last_accessed，排除 protected_active_key 与 loading）。
  2. [无可淘汰裁决]：staging 场景允许超容量（仅告警）；普通场景抛 CacheFullError。
  3. [loading 保护]：loading 实例不得被 LRU 淘汰或非活跃实例清理销毁；销毁 loading 实例的操作仅限创建它的链路、分区/副本实例销毁、服务关闭。
  4. [空闲清扫]：后台 `_idle_sweep_loop` 周期清扫**仅活跃实例**中超时的（按 inst.repo_key 移除 active 映射后逐出，下次检索懒重建）；清扫间隔 = max(idle_timeout×60/3, 60s)。
- **级联依赖功能单元（Cascade Links）**：无
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-memory-instance-lifecycle.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/memory_qdrant/service.py）：`src/codebase_rag/services/memory_qdrant/service.py`（LRU 与清扫段）

### [FEAT-MQ-05] 懒加载单飞重建
- **业务目标**：检索时发现某副本无活跃实例，自动从 Qdrant Server 重建，并发请求共享同一次重建。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [单飞约束]：同一 repo_key 同一时刻仅限一个 in-flight 懒加载；`_in_flight_rebuilds[repo_key]` 无任务则 create_task（finally 校验 current_task 后清理槽位），并发请求 `asyncio.shield(task)` 共享同一重建任务。
  2. [失败不缓存]：懒加载失败不得缓存失败结果，下一个请求必须重新发起重建。
  3. [取消不传播]：单个请求被取消不得取消共享的重建任务（shield 保护；任务异常经 done_callback 吞掉防告警）。
  4. [跨副本/跨项目并行]：不同 repo_key 的懒加载可并行，互不阻塞。
  5. [失败语义]：handler 失败 / rebuild 任务被取消且自身非取消 / 无 handler / 重试仍无实例 → 抛 NoActiveInstanceError（语义为"无活跃实例且无法懒重建"）。
  6. [锁纪律]：等待共享懒加载任务时不得持有实例缓存锁。
  7. [脏引用清理]：活跃引用指向已逐出实例时清理脏引用后走重建。
  8. [重建回调契约]：`set_rebuild_handler` 注册的回调收 repo_key，由 workflow 用其注册表解析 project_id 与仓库路径后重建（FEAT-WF-02）。
- **级联依赖功能单元（Cascade Links）**：FEAT-WF-02（rebuild handler，经 set_rebuild_handler 接线）、FEAT-COMMON-06（repo_key 规范化）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-memory-instance-lifecycle.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/memory_qdrant/service.py）：`src/codebase_rag/services/memory_qdrant/service.py`（懒加载重建段）*(同类参考样板：进程内单飞 + shield)*
  - 域异常体系（services/memory_qdrant/errors.py）：`src/codebase_rag/services/memory_qdrant/errors.py`

### [FEAT-MQ-06] 实例销毁与缓存键迁移
- **业务目标**：分区注销/单副本注销清理与 commit 变化后的缓存键原地迁移。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [全量销毁]：`destroy_all_instances(project_id)` 按实例元数据 project_id 清分区全部实例（各副本）并移除对应 active 映射（分区注销 FEAT-MGMT-03 / 身份迁移用）。
  2. [单副本销毁]：`destroy_repo_instances(repo_key)` 仅清该副本实例并移除其 active 映射；同分区其他副本实例保持（单副本注销 FEAT-MGMT-03 用）。
  3. [选择性销毁]：`destroy_inactive_instances(repo_key)` 保留该副本活跃实例与 loading 中的 staging（staging 提升后清场用）。
  4. [缓存键迁移]：`update_cache_commit(repo_key, branch, old, new)` 锁内弹出旧键、改 commit_hash、塞入新键并同步 active 引用；旧键不存在返回 False。
- **级联依赖功能单元（Cascade Links）**：FEAT-WF-03（update_cache_commit 调用方）、FEAT-MGMT-03（分区/副本注销调用方）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-repo-copy-identity.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/memory_qdrant/service.py）：`src/codebase_rag/services/memory_qdrant/service.py`（销毁与迁移段）

## 三、域内量化参数速查

| 参数 | 默认值 | 配置键 |
| :--- | :--- | :--- |
| 缓存容量上限 | 5 | `memory_qdrant.cache.max_branches` |
| 默认检索 top_k | 10 | `memory_qdrant.search.default_top_k` |
| 检索 top_k 上限 | 50 | `memory_qdrant.search.max_top_k` |
| 双路 prefetch 上限 | 50 | `memory_qdrant.search.prefetch_limit` |
| dense / sparse 权重 | 0.7 / 0.3 | `memory_qdrant.search.dense_weight / sparse_weight` |
| RRF k 常数 | 60 | `memory_qdrant.search.rrf_k` |
| 空闲超时 | 30 分钟（0=永不） | `memory_qdrant.cache.idle_timeout_minutes` |
