# 知识同步 knowledge_sync

## 一、业务域职责边界与域内铁律

- 负责：文件版本知识提交的受理判定、JSON 任务外发箱、文件与版本两级互斥锁、重试入口与 project_id 迁移委托。
- 不负责：自动重试调度（补偿循环属 workflow FEAT-WF-06）；知识生成与向量化（→ agent、embedding）；Qdrant 写入协议（→ qdrant_storage）；对外受理语义表达（→ mcp_service）。
- 域内铁律：发布互斥以文件路径锁 + 文件版本锁两级进程内锁串行；同一路径的新版本提交必须先标记最新并取消旧版本待办；持久写入或任务承接确认后，附带的内存刷新、任务清理失败不得反转已确认结果。

## 二、业务功能单元明细

### [FEAT-KS-01] 知识提交受理与发布（submit_points 双锁流程）
- **业务目标**：保证同一文件同一版本的知识点发布串行化且幂等。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [锁顺序固定]：先获取文件锁（key=`{project_id}:{file_path}`，路径 `\`→`/` 归一化）→ 锁内若 update_latest=True 则先 `set_latest` + `cancel_stale_file_jobs`（最新版本登记与旧任务取消先于一切发布）→ 再获取版本锁（key=`{project_id}:{file_version_id}`）→ 锁内处理 job。反序获取仅出现在 ensure_published（只拿版本锁，不碰 latest）。
  2. [job 复用]：锁内查 job：已存在且非替换时，IF status=="cancelled" → 直接返回 cancelled_stale=True；否则复用既有 job 重发布。
  3. [发布确认]：发布前复查 `get_latest`，IF 不等于本版本 → job 置 cancelled（last_error="cancelled_by_newer_file_version"），返回 cancelled_stale；`check_existence` 已存在则跳过写入；否则 `upsert_file_points`，**返回值非 True 即抛 RuntimeError("Knowledge write was not confirmed.")**。
  4. [成功收尾]：成功后 best-effort 删除 job，按 update_memory 刷新内存镜像。
  5. [受理依据]：只有任务本身保存成功才构成可恢复的受理依据；发布失败保留完整 ready 任务于外发箱并累计重试计数。
- **级联依赖功能单元（Cascade Links）**：FEAT-KS-02（store 读写）、FEAT-QS-02（upsert_file_points）、FEAT-QS-04（check_existence）、FEAT-KS-04（内存镜像刷新）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-knowledge-outbox.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/knowledge_sync/service.py）：`src/codebase_rag/services/knowledge_sync/service.py`（submit_points / _publish_locked 段）*(同类参考样板：两级锁+外发箱)*
  - 域数据模型（services/knowledge_sync/models.py）：`src/codebase_rag/services/knowledge_sync/models.py`

### [FEAT-KS-02] 外发箱持久化与失败重试（store + retry_ready_jobs）
- **业务目标**：写入失败不丢数据，落盘为可重试 job，由补偿循环驱动重试。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [失败落盘]：_publish_locked 捕获任意异常后：retry_count += 1（无上限累计）、last_error 截断 500 字符、status="ready" 落盘，返回 retry_job_saved=True。
  2. [重试门控]：retry_ready_jobs 受 `set_retry_availability_provider` 可用性探测门控——provider 异常或未初始化时 `can_retry_pending()` 返回 False（补偿循环正在跑时避免并发重试）。
  3. [重试防重]：仅取 status=="ready" 的 job，逐个持版本锁后二次校验当前仍为 ready 才发布（防并发重复发布）；成功/跳过计 succeeded，出错计 failed；批量上限 batch_size=10。
  4. [存储结构]：job 落盘 `{store_dir}/jobs/{project_id}/{file_version_id}.json`（含全量 points、`dense_vector` 与 `file_type`；不再序列化稀疏向量，读取旧 JSON 时额外字段自然忽略）；最新版本索引落盘 `{store_dir}/latest/{project_id}/{sha256(归一化file_path)}.json`；写入均走 `path+".tmp"` + `os.replace` 原子替换。
  5. [读容错]：JSONDecodeError / OSError / KeyError → 返回 None（视为无 job）；get_latest 额外校验 project_id/file_path 匹配。
  6. [旧任务取消]：cancel_stale_file_jobs 遍历项目全部 job，同 file_path 且 file_version_id != latest → 置 cancelled 并回写。
  7. [外发箱目录]：`{workflow.data_dir}/knowledge_sync/`。
- **级联依赖功能单元（Cascade Links）**：FEAT-WF-06（补偿循环驱动方）、FEAT-QS-02（重发布写入）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-knowledge-outbox.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域内辅助实现（services/knowledge_sync/store.py）：`src/codebase_rag/services/knowledge_sync/store.py` *(同类参考样板：tmp+replace 原子 JSON 外发箱)*
  - 域服务门面（services/knowledge_sync/service.py）：`src/codebase_rag/services/knowledge_sync/service.py`（retry_ready_jobs 段）

### [FEAT-KS-03] 幂等快路径与最新版本管理
- **业务目标**：嵌入生成流程前的预检去重与版本时效判定。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [try_skip_existing_file_version]：双锁（先文件后版本）内查 Qdrant 存在性；不存在返回 None 交回正常流程；存在则更新 latest、清 job、刷新内存后返回 skipped_existing——**从不发布或复用 pending job**。
  2. [ensure_published]：仅持版本锁；IF Qdrant 已有完整知识 → 清 job 返回跳过；IF 无 job → 返回 error="knowledge_sync_job_not_found"。
  3. [is_stale_file_version]：判定给定版本是否已被同路径更新版本取代（补偿循环据此跳过重试）。
  4. [has_ready_job]：外发箱存在 ready 状态 job 的探询（纯重命名复用路径据此触发 ensure_published）。
- **级联依赖功能单元（Cascade Links）**：FEAT-QS-04（check_existence）、FEAT-WF-03（消费方）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-knowledge-outbox.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/knowledge_sync/service.py）：`src/codebase_rag/services/knowledge_sync/service.py`（幂等快路径段）

### [FEAT-KS-04] 内存镜像刷新（按 repo_key）与 project_id 迁移
- **业务目标**：发布后同步内存索引视图（定位到来源仓库副本）；project_id 派生规则升级时迁移外发箱归属。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [两条刷新路径]：`upsert_memory_from_qdrant`（从 Qdrant Server 拉取→写内存）与 `upsert_memory_points`（直接用 job points 转换，payload 附 knowledge_set_id/chunk_index/chunk_count，点 ID 为 uuid5）；两者均先删 old_file_version_id 旧点。
  2. [副本定位]：submit_points / ensure_published / try_skip_existing_file_version 均接受可选 `repo_key`——内存刷新按 repo_key 定位该副本的活跃实例（get_active_instance_info(repo_key)）；repo_key 为 None（补偿重试等无副本目标场景）时刷新为 no-op，job 凭证与持久化结果不受影响。
  3. [best-effort 边界]：无活跃实例或 handle 为 None 时静默返回；所有内存刷新异常仅记日志，不反转已确认的持久化结果。
  4. [迁移]：rebrand_project_id 委托 store——jobs 逐条搬家到新 project_id 目录，latest 重写 project_id 字段，空目录清理。
- **级联依赖功能单元（Cascade Links）**：FEAT-MQ-03（活跃实例）、FEAT-QS-03（get_file_points）、FEAT-MGMT-01（迁移触发方）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/knowledge_sync/service.py）：`src/codebase_rag/services/knowledge_sync/service.py`（内存镜像与迁移段）

## 三、域内量化参数速查

| 参数 | 默认值 | 来源 |
| :--- | :--- | :--- |
| 重试批大小 | 10 | retry_ready_jobs 方法默认 |
| last_error 截断长度 | 500 字符 | 代码常量 |
| 外发箱根目录 | `{workflow.data_dir}/knowledge_sync/` | `__main__.py` 组装 |
