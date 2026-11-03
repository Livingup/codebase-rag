# 内存实例生命周期 memory-instance-lifecycle

关联功能单元：FEAT-MQ-01 ~ FEAT-MQ-06（实现）、FEAT-WF-02 / FEAT-WF-05（staging 创建与提升编排）、FEAT-MCPS-02 / FEAT-MGMT-04（检索消费方触发懒重建）。

## 1. 启用依据与背景坑点

- 场景：内存检索实例（每仓库副本每分支一个嵌入式 Qdrant 实例，实例键 repo_key:branch@commit）构建成本高（需从持久层全量拉取），且检索服务要求全程可用；缓存容量有限（默认 5 个分支）需淘汰。实例键取 repo_key（本地副本规范路径）而非 project_id：同分区的多个本地副本各持独立实例，杜绝后注册副本顶替先注册副本的查询语义错位（详见 `.agent/04-tech-decisions/tech-repo-copy-identity.md`）。
- 不这么做的故障：在活跃实例上原地重建会在构建期间使检索不可用或读到半构建状态；并发懒重建会重复拉取造成放大；LRU 淘汰误删构建中或活跃实例会导致检索失败。

## 2. 选定的技术方案与强制契约

- **实现落点**：`services/memory_qdrant/service.py`（MemoryQdrantService 独占实例生命周期与缓存状态；编排属 workflow）。
- **staging 原子提升契约**：构建新实例必须创建 `staging=True` 临时实例（key=`{base_key}#staging:{uuid4}`，永不复用，加入 `_loading` 保护集合）→ 持久化知识加载成功后才 `set_active_handle` 原地转正（关闭旧正式 key 实例、staging 条目改名正式 key、设 active）→ 失败只销毁 staging，旧活跃实例保持可搜。此事务模式在分支切换 / 懒重建 / 全量初始化四处复用。
- **单飞懒重建契约**：同一 repo_key 同一时刻仅一个 in-flight 重建——`_in_flight_rebuilds[repo_key]` 槽位 + `asyncio.shield(task)` 共享；失败不缓存失败结果（下次请求重新发起）；单请求取消不传播到共享任务；不同副本/项目并行。重建回调收 repo_key，由 workflow 注册表解析 project_id 与路径。
- **LRU 淘汰契约**：淘汰优先级 ①最旧非活跃且非 loading → ②空闲超时活跃实例（idle_timeout 默认 30 分钟，排除 protected_active_key 与 loading）；无可淘汰对象时 staging 场景允许超容量（仅告警），普通场景抛 CacheFullError。
- **空闲清扫契约**：后台 `_idle_sweep_loop` 仅清扫活跃实例（间隔 max(idle×60/3, 60s)），移除 active 映射后逐出，下次检索懒重建。
- **部署依据**：懒加载防重采用进程内单飞而非分布式锁——单进程部署是既定架构约束（见 architecture-rules.md）。

## 3. 核心不变量与禁止项（Invariants & Prohibitions）

1. loading（staging 构建中）实例不得被 LRU 淘汰或非活跃实例清理销毁——销毁它的操作仅限创建它的链路、项目全部实例销毁、服务关闭。
2. 等待共享懒加载任务时不得持有实例缓存锁——重建回调自身会操作实例缓存，持锁等待会死锁。
3. 严禁在活跃实例上原地重建——必须走 staging 先构建后原子提升。
4. 懒加载失败结果严禁缓存——下一个请求必须重新发起重建（NoActiveInstanceError 语义 = "无活跃实例且无法懒重建"）。
5. 提升后不主动缩容——临时实例超容量是允许状态，等待自然 LRU 收敛。
