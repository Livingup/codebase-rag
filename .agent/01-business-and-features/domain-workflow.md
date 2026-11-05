# 工作流编排 workflow

## 一、业务域职责边界与域内铁律

- 负责：Git 事件到知识落地的整条编排——副本初始化（全量/增量）、提交同步（含文本与 `.png`/`.jpg`/`.jpeg` 图片文件知识生产）、分支切换、失败补偿与死信归档、内存视图 staging 构建；内部维护 `_project_repos`（repo_key→project_id）注册表，同一 project_id 可映射多个本地副本。
- 不负责：内存实例生命周期与缓存状态（→ memory_qdrant）；知识受理凭证与外发箱（→ knowledge_sync）；持久化读写协议（→ qdrant_storage）；LLM 调用与向量化（→ agent、embedding）；事件采集与分类（→ event_detection）。
- 域内铁律：构建新内存实例必须走临时 staging 实例，持久化知识加载成功后才原子提升为活跃实例；失败只销毁 staging，严禁在活跃实例上原地重建。

## 二、业务功能单元明细

### [FEAT-WF-01] Git 事件接收与副本任务队列
- **业务目标**：把 GitEvent 转成 WorkflowTask 并按仓库副本串行、跨副本并行调度。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [事件路由]：事件带 git_repo_path → `normalize_repo_key` 后必须在 `_project_repos`（repo_key→project_id 注册表）命中，否则记 workflow_event_unroutable_repo 丢弃；无路径的存量事件仅当该 project_id 恰有一个副本时兜底命中。
  2. [任务身份]：WorkflowTask 携带 repo_key；队列键 = `task.repo_key or task.project_id`（后者仅为兼容无 repo_key 的存量任务，多副本时无法消歧直接跳过）。
  3. [串行规则]：同一 repo_key 的任务 FIFO 串行（per-copy worker），不同副本/项目并行。
  4. [执行解析]：`_execute_task` 按 repo_key 解析 `(project_id, repo_path)`（repo_key 本身即规范化可用路径）；repo 未注册 → 记告警跳过。
  5. [队列合并现状]：`ProjectTaskQueue` 队列层不做同类型合并；实际去重依赖 event_detection 去重窗口（FEAT-ED-03）+ 执行层 staleness 判断（FEAT-KS-03）。
  6. [异常隔离]：worker 内 executor 异常仅记日志，不中断该副本后续任务。
  7. [状态查询]：`get_workflow_status(repo_key)` 按副本返回 `current_task`/`queued_tasks`/`last_completed`（`_current_tasks`/`_last_completed` 键为 repo_key）。
- **级联依赖功能单元（Cascade Links）**：FEAT-ED-03（GitEvent 来源，经 `subscribe` 回调）、FEAT-COMMON-06（repo_key 规范化）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-repo-copy-identity.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/workflow/service.py）：`src/codebase_rag/services/workflow/service.py`（on_git_event / 任务入队段）
  - 域内辅助实现（services/workflow/task_queue.py）：`src/codebase_rag/services/workflow/task_queue.py`
  - 域数据模型（services/workflow/models.py）：`src/codebase_rag/services/workflow/models.py`

### [FEAT-WF-02] 分支切换与懒重建（staging 提升流程）
- **业务目标**：分支切换或实例被淘汰后重建内存知识实例，且重建全程不中断当前可搜索性。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [缓存命中分支]：IF `get_cached_instance` 命中 `(repo_key, branch, commit)` 三元组 → 直接 `set_active_instance` 返回 `cache_hit`，不做任何重建。
  2. [diff 重建主流程]：取新分支文件树的 `file_version_id` 集合，与该副本旧活跃实例集合做差集得 `to_add` / `to_remove` / `unchanged` → 创建 `staging=True` 临时实例（旧实例保持可搜）→ 从 Qdrant 批量加载 `unchanged ∪ to_add` → 仅加载全部成功后才 `set_active_handle` + `destroy_inactive_instances(repo_key)`（原子提升）。
  3. [失败保护]：加载失败 → 销毁 staging 实例后上抛异常，旧活跃实例必须完好无损。
  4. [懒重建入口]：`rebuild_memory_instance(repo_key)` 由 memory_qdrant 搜索发现无活跃实例时经 `set_rebuild_handler` 回调；从 `_project_repos` 解析 project_id（repo 未注册 → `WorkflowError`）、从 repo_key 读 HEAD（branch 为空 → 回退 `"HEAD"`）。
- **级联依赖功能单元（Cascade Links）**：FEAT-GIT-03（get_file_tree / get_head_info）、FEAT-MQ-03（staging 提升）、FEAT-MQ-06（销毁非活跃实例）、FEAT-QS-03（get_files_points 加载）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-memory-instance-lifecycle.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/workflow/service.py）：`src/codebase_rag/services/workflow/service.py`（分支切换与懒重建段）*(同类参考样板：staging 事务模式)*

### [FEAT-WF-03] 内容提交知识生产（含纯重命名复用与图片视觉理解）
- **业务目标**：对一次 commit 的变更文件集（文本或 `.png`/`.jpg`/`.jpeg` 图片）生成/同步业务描述知识点并落地持久层与内存层。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [变更解析入口]：`get_changed_files(previous→commit)` 经过 `.gitignore` 风格过滤后返回 `added`/`modified`/`deleted`/`renamed` 四类文件，并发处理（文件级 Semaphore，`content_commit.max_file_concurrency=5`）。
  2. [纯重命名复用]：`renamed` 且 blob 不变 → 取 `old_file_version_id` 源 points；IF 源为空且外发箱有 ready job → `ensure_published` 后重取；克隆 points 仅改 `file_path`/`blob_hash`（保留 `file_type` 与 `dense_vector`，无 LLM、无 embedding）→ `submit_points(source="rename_clone", update_latest=True, update_memory=True)`；IF `cancelled_stale` / `skipped_existing` → 静默返回；IF 无源 points → 记 `failed("no source points found")`。
  3. [单文件守卫链]（added/modified）：stale 版本 skip → 内容寻址去重（`check_existence` 命中则按 repo_key 回补该副本内存并删旧版本点）→ 外发箱 ready 则 `ensure_published`（`cache_hit`）→ `get_file_bytes` + `detect_file_modality` 自动识别模态（`unsupported_binary` 直接跳过；`image` 将图片构造为 `image_url` data URL 多模态消息发给 `knowledge_producer` 视觉模型看图生成 `business_description`；`text` 解码 UTF-8 文本发给 `knowledge_producer`）→ 知识点逐条校验 `point_token_limit=8192`（图片文件每条额外加 `IMAGE_TOKEN_COST=258` tokens，超限整批拒绝重生成）→ `embedding.embed` 向量化（图片文件以 `MultimodalEmbedInput(text=desc, image_bytes=raw_bytes, mime_type=mime_type)` 将文字描述与图片联合向量化）→ `submit_points(repo_key=副本)`。
  4. [Provider 容量感知]：`select_provider` 全满时阻塞等待空位；同一服务商多 agent 逐个尝试、全败移出候选；全部候选耗尽 → `WorkflowError("All knowledge_producer providers exhausted")`。
  5. [失败重试入队]：added/modified 处理失败 → 进入持久化重试队列；入队前先移除同 `file_path` 但不同 `file_version_id` 的旧记录（新版本取代旧版本），再按 `file_version_id` 精确去重。
  6. [收尾]：全部文件处理完成后 `update_cache_commit` 更新活跃实例缓存键。
- **级联依赖功能单元（Cascade Links）**：FEAT-GIT-01（get_changed_files）、FEAT-GIT-02（detect_file_modality）、FEAT-GIT-03（get_file_bytes / get_file_content）、FEAT-QS-04（check_existence）、FEAT-KS-01（submit_points）、FEAT-KS-03（ensure_published / is_stale_file_version / has_ready_job）、FEAT-AG-02（select_provider）、FEAT-AG-03（run_agent）、FEAT-EMB-02（embed）、FEAT-MQ-06（update_cache_commit）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-knowledge-outbox.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/workflow/service.py）：`src/codebase_rag/services/workflow/service.py`（内容提交与单文件守卫链段）*(同类参考样板：守卫链幂等设计)*

### [FEAT-WF-05] 项目初始化（全量/增量双模式）
- **业务目标**：项目接入时建立基线知识。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [增量模式（默认）]：`initialization.mode=="incremental"` → 空启动：读文件树 → `check_existence` 已持久化的 `file_version_id` → 仅把已存在 points 批量载入 staging 实例，**不**为缺失文件触发生成；后续 commit 增量增长。
  2. [全量模式]：预过滤已存在版本（skip）→ Semaphore 并发处理 pending 文件（`sync_source="initialization_backfill"`）→ 失败文件入重试队列 → Progress 跟踪（`current_file`/`processed`/`failed`）→ staging 批量加载 processed 集合 → 全部成功才提升，否则销毁 staging 并上抛。
  3. [任务排队]：初始化任务经副本任务队列按 `repo_key` 串行执行（`enqueue_initialization` 携带仓库路径）。
- **级联依赖功能单元（Cascade Links）**：FEAT-WF-01（队列）、FEAT-WF-03（单文件守卫链复用）、FEAT-QS-04（check_existence）、FEAT-MQ-03（staging 提升）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-memory-instance-lifecycle.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/workflow/service.py）：`src/codebase_rag/services/workflow/service.py`（初始化段）

### [FEAT-WF-06] 补偿循环与重试/死信
- **业务目标**：后台兜底重试失败文件与外发箱任务，不可恢复者归档死信。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [启动]：服务启动后常驻后台任务，先 sleep 5s 等项目注册完成。
  2. [每轮顺序]：①计算退避 `backoff = min(base_backoff×2^consecutive_failures, max_backoff)`，实际 sleep = `max(backoff−距上轮耗时, scan_interval)`；②汇集所有已注册 project_id（取 `_project_repos` 值去重）的持久化重试队列（`{data_dir}/retry_queue_{project_id}.json`）；③**先执行** `knowledge_sync.retry_ready_jobs`（外发箱优先于文件重试）；④逐条处理重试记录。
  3. [单条重试裁决]：该 project_id 无任何已注册副本 → 死信；repo 取仍存活的注册副本（`get_repo_key_for_project` 优先返回 `.git` 仍存在的副本，全失效时回退首个匹配；blob 内容寻址，任一持有该对象的副本皆可）；`is_stale_file_version` → 移出队列视为成功；blob 探测 `GitFileNotFoundError` → 死信（`reason="blob_not_found"`）；其他 Git 异常 → `retry_count+1` 留队；agent 重跑（`skip_memory_update=True`、`update_latest=False`、`source="compensation_retry"`）异常 → `retry_count+1`，IF `≥ max_file_retries` → 死信（`"max_retries_exceeded"`）。
  4. [死信归档]：共三种死信条件（blob 消失 / 重试超限 / 项目未注册），持久化到 `{data_dir}/dead_letter_{project_id}.json`。
  5. [探针退避]：本轮首个 LLM 失败且零成功 → `consecutive_failures+1` 并中断本轮；任一成功则清零。
  6. [重试期间不做内存更新]：补偿重试 `skip_memory_update=True`，内存视图由下一次事件触发重建。
- **级联依赖功能单元（Cascade Links）**：FEAT-KS-02（retry_ready_jobs）、FEAT-KS-03（is_stale_file_version）、FEAT-GIT-03（get_file_bytes / get_file_content）、FEAT-WF-03（agent 重跑全链）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-knowledge-outbox.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/workflow/service.py）：`src/codebase_rag/services/workflow/service.py`（补偿循环段）

## 三、域内量化参数速查（来源：config schema 默认值）

| 参数 | 默认值 | 配置键 |
| :--- | :--- | :--- |
| 分支切换 diff 并发 | 3 | `workflow.branch_switch.max_diff_concurrency` |
| 提交处理文件并发 | 5 | `workflow.content_commit.max_file_concurrency` |
| 知识点 token 上限 | 8192 | `workflow.content_commit.point_token_limit` |
| 知识生成 agent 超时 | 120s | `workflow.knowledge_producer.timeout` |
| 初始化文件并发 | 10 | `workflow.initialization.max_file_concurrency` |
| 初始化模式 | incremental | `workflow.initialization.mode` |
| 补偿扫描间隔 | 60s | `workflow.compensation.scan_interval` |
| 补偿基础退避 / 最大退避 | 30s / 1800s | `workflow.compensation.base_backoff / max_backoff` |
| 补偿批大小 | 10 | `workflow.compensation.batch_size` |
| 文件最大重试次数 | 50 | `workflow.compensation.max_file_retries` |
