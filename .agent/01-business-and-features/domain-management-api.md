# 管理面 management_api

## 一、业务域职责边界与域内铁律

- 负责：仓库副本登记、注销（单副本/整分区）与重启恢复、仓库移动自愈、管理检索、初始化/同步任务触发、健康与客户端状态、配置热重载接口、本地 Admin UI、注册表持久化。
- 不负责：面向编程客户端的 MCP 工具表达（→ mcp_service）；知识生产编排（→ workflow）；配置协调机制本身（→ config 层）。
- 域内铁律：project_id 必须由仓库根 commit 派生；发现仓库内缓存 ID 与派生结果不一致时执行迁移，不得重建项目身份；注册表以 repo_key（`normalize_repo_key` 规范化路径，FEAT-COMMON-05）为唯一键——同一 project_id 的多个本地副本各占一条登记（hooks/watcher/内存实例独立），登记入口严禁互相顶替；注册主链保存失败必须显式进入失败响应并提示已产生的部分副作用。

## 二、业务功能单元明细

### [FEAT-MGMT-01] 仓库副本注册主链与 project_id 派生/迁移
- **业务目标**：登记一个本地 Git 仓库副本为知识生产项目成员，建立稳定身份。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [注册五步]：① `validate_repository` 校验 .git；② 从 root commit 派生 project_id：`uuid5(uuid.NAMESPACE_DNS, "codebase-rag:project_id:v1:{root_commit}")`（前缀常量 `_PROJECT_ID_DERIVATION_PREFIX`，跨 clone/镜像稳定）；无 commit THEN 拒绝"has no commits yet"；③ event_detection 注册该副本；④ workflow 注册该副本（repo_key→project_id 映射）；⑤ enqueue_initialization（按副本串行队列异步初始化）。
  2. [多副本注册]：注册表键为 repo_key；同 repo_key 重复注册 = 刷新既有条目（保留 registered_at/display_name，路径重拼记 project_path_respelled）；同 project_id 新 repo_key = 新增副本条目（记 project_copy_registered），既有副本不受影响。
  3. [身份迁移]：IF `.git/codebase-rag/project_id` 缓存与派生值不一致 THEN 执行 5 步迁移（Qdrant rebrand_project_id → workflow → knowledge_sync → 销毁旧内存实例 → 注册表各条目 project_id 字段改写，键仍为 repo_key 无需换键，各步幂等可重试）。
  4. [保存失败表达]：注册表保存失败（`_save_registry(raise_on_error=True)`，临时文件 + `os.replace` 原子写）THEN 报"项目登记未完成：无法保存注册表……部分操作可能已执行，未进行完整回滚"——注册跨越登记、监控和任务安排的部分副作用不采用全链事务回滚。
  5. [display_name]：缺省取已有名或目录名。
  6. [批量注册]：POST /projects/batch 逐项独立调用 register_project，单项失败仅记录、成功项保留登记，返回 total/succeeded/failed 明细。
- **级联依赖功能单元（Cascade Links）**：FEAT-GIT-03（validate_repository / get_root_commit）、FEAT-ED-01（注册）、FEAT-WF-05（初始化入队）、FEAT-QS-04（rebrand）、FEAT-KS-04（外发箱迁移）、FEAT-MQ-06（销毁旧实例）、FEAT-COMMON-06（repo_key 规范化）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无（Admin UI 见 FEAT-MGMT-05）
  - 技术架构抉择：`.agent/04-tech-decisions/tech-repo-copy-identity.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/management_api/service.py）：`src/codebase_rag/services/management_api/service.py`（注册主链与迁移段）*(同类参考样板：派生身份 + 原子注册表)*

### [FEAT-MGMT-02] 仓库移动自愈（heal_moved_project）
- **业务目标**：仓库被移动后，读路径传入新根路径时自动重新登记。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [自愈判定三条件（全部满足）]：① incoming_path 本身是 Git 仓库根（不上溯）；② 从 incoming_path 的 root commit 派生的 id == project_id（身份来自仓库内容，不信任调用方声明或缓存文件）；③ 按副本三态判定——alive（`.git` 含 HEAD，或 worktree 指针的 gitdir 含 HEAD）/ gone（不满足 alive 且所在卷在线）/ unknown（不满足 alive 但卷不可用）。存在 unknown → 不自愈；gone 为 0（没有任何副本消失，即新增克隆）→ 不自愈，必须显式注册；gone ≥ 1 → 自愈，且**只迁移 gone 的那几条**，alive 副本的登记、hooks、watcher、内存实例不受影响。
  2. [身份不匹配]：记 move_heal_identity_mismatch 并放弃。
  3. [并发合并]：single-flight 键为 `(project_id, incoming repo_key)`（`_in_flight_heals` + `asyncio.shield`，同一副本的并发调用加入同一任务，不同副本并行移动互不吞并，调用者取消不影响在途自愈）。
  4. [同步预检]：纯注册表比对 + `.git`/卷探针（无 git 子进程），热路径（incoming 已登记、存在 unknown 或无 gone 副本）立即返回 False。
  5. [自愈执行顺序]：① 对 gone 副本调 event_detection.unregister_repo（先切断旧 watcher，避免巡检重注入 hooks 在死路径重建目录）→ ② `register_project(incoming, display_name=继承 gone 副本的 display_name)` → ③ 对 gone 副本执行 workflow.unregister_repo + memory.destroy_repo_instances + 移出注册表 + 重存注册表；②失败时仍移出 gone 副本并重存，不留指向已消失路径的条目。自愈仅恢复既有登记身份，不构成新的注册授权。
  6. [幽灵 .git 防护]：事件域 hook 注入遇 `.git` 不存在时跳过且不建目录（`hook_injection_skipped_missing_repo`），hook 巡检跳过已消失副本（`hook_health_check_skipped_missing_repo`）；存活判据不得只看 `.git` 是否存在，否则幽灵 `.git` 会把失联副本判活并永久阻塞后续自愈。
- **级联依赖功能单元（Cascade Links）**：FEAT-MGMT-01（复用注册链）、FEAT-MCPS-03（信号来源）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-moved-repo-heal.md`、`.agent/04-tech-decisions/tech-repo-copy-identity.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/management_api/service.py）：`src/codebase_rag/services/management_api/service.py`（移动自愈段）*(同类参考样板：single-flight + 身份重派生自愈)*

### [FEAT-MGMT-03] 仓库副本注销（单副本/整分区）与重启恢复
- **业务目标**：停止本地跟踪（hooks/watcher/内存实例/登记条目）；服务重启后恢复登记。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [整分区注销 unregister_project(project_id)]：对 project_id 的全部登记副本执行 ① event_detection.unregister_project → ② workflow.unregister_project → ③ memory.destroy_all_instances（分区全部实例）→ ④ 逐条移出注册表并保存；响应含 unregistered_repos 数量；**保留 Qdrant 持久数据以便复用**，响应恒 deleted_points=0；无任何副本抛 ProjectNotFoundError。
  2. [单副本注销 unregister_repo(git_repo_path)]：仅对目标 repo_key 执行 event_detection.unregister_repo + workflow.unregister_repo + memory.destroy_repo_instances + 移出该条登记；同分区其他副本的跟踪与实例保持；路径未登记抛 ProjectNotFoundError；同样不删 Qdrant 数据（deleted_points=0）。
  3. [重启恢复]：initialize() 加载注册表 JSON（字段 project_id/git_repo_path/display_name，repo_key 加载时按路径重派生）；IF 条目 .git 目录不存在或重新注册失败 THEN 跳过并在循环后重存注册表裁剪陈旧条目（仅 skipped>0 时重写）；成功项走完整 register_project（含重新入队初始化）；同 project_id 多条目逐条恢复、互不顶替；按条目逐项容错，不做跨项目事务。
- **级联依赖功能单元（Cascade Links）**：FEAT-ED-01（注销）、FEAT-WF-01（注销）、FEAT-MQ-06（destroy_all_instances / destroy_repo_instances）、FEAT-MGMT-01（恢复复用注册链）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-repo-copy-identity.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/management_api/service.py）：`src/codebase_rag/services/management_api/service.py`（删除与恢复段）

### [FEAT-MGMT-04] REST 管理端点（/api/v1）
- **业务目标**：本地管理面 HTTP API。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [路由全量清单]（错误体统一 `ErrorResponse{error:{code,message}}`）：`POST /projects`（201/400 INVALID_REQUEST）、`POST /projects/batch`（201）、`GET /projects`（每副本一条）、`GET /projects/{project_id}`（404 PROJECT_NOT_FOUND，多副本时取首个注册副本）、`DELETE /projects/{project_id}`（unregister_project，404）、`DELETE /repos`（unregister_repo，body UnregisterRepoRequest{git_repo_path}，404/400 INVALID_REQUEST）、`POST /projects/{project_id}/search`（404/503 SERVICE_UNAVAILABLE，搜索首个注册副本实例）、`POST /projects/{project_id}/rebuild`（RebuildRequest.force，作用于首个注册副本）、`POST /projects/{project_id}/sync`（首个注册副本）、`GET /projects/{project_id}/status`（首个注册副本）、`GET /health`（无鉴权）、`GET /status/clients`（仅本机，?live= 实探）、`POST /status/clients/{client_id}/probe`（仅本机，404 CLIENT_STATUS_ITEM_NOT_FOUND）、`POST /config/reload`（失败 500 RELOAD_ERROR）。
  2. [REST 搜索]：embed 查询（空结果→503）→ hybrid_search(top_k) → 按 score_threshold（默认 0.0）过滤并统计 below_threshold；filters.entity_type / file_path_prefix 转 MemorySearchFilters；top_k 默认 10。
  3. [状态汇总规则]：有当前任务 → initializing；有活跃实例 → ready；否则 → registered。
  4. [配置重载]：POST /config/reload 调 ConfigManager.reload()（重读 config.yaml + .env、校验、执行各服务 reconciler、替换内存 AppConfig），返回 requires_restart / restart_reasons / reconciled_services。
  5. [本地访问控制]：/status/clients 系列与 Admin UI 均 require_local_request——client.host 不在 {127.0.0.1, ::1, ::ffff:127.0.0.1, localhost, testclient} 则 403（无密码本地后台）。
- **级联依赖功能单元（Cascade Links）**：FEAT-MGMT-01/02/03、FEAT-EMB-02（搜索向量化）、FEAT-MQ-02（hybrid_search）、FEAT-COMMON-04（ConfigManager.reload）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无（Admin UI 见 FEAT-MGMT-05）
  - 技术架构抉择：`.agent/04-tech-decisions/tech-config-reconcile.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - REST 路由适配（services/management_api/routes.py）：`src/codebase_rag/services/management_api/routes.py` *(同类参考样板：统一错误体路由)*
  - 域服务门面（services/management_api/service.py）：`src/codebase_rag/services/management_api/service.py`（搜索/状态/重载段）
  - 域数据模型（services/management_api/models.py）：`src/codebase_rag/services/management_api/models.py`

### [FEAT-MGMT-05] 本地 Admin UI
- **业务目标**：内嵌单页 HTML 的本地管理后台（GET / 与 GET /admin）。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [访问控制]：require_local_request 限本机回环地址；Admin UI 未启用（is_admin_ui_enabled=False）时返回 404。
  2. [形态]：admin_ui.py 内嵌单页 HTML 的只读状态页，无独立前端工程、无设计稿/切图体系。
- **级联依赖功能单元（Cascade Links）**：FEAT-MGMT-04（共享服务）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无独立 UI 文档（内嵌单页，现状即本条目）
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - REST 路由适配（services/management_api/admin_ui.py）：`src/codebase_rag/services/management_api/admin_ui.py`

## 三、域内量化参数速查

| 参数 | 默认值 | 配置键 |
| :--- | :--- | :--- |
| 管理面监听 | 127.0.0.1:18080（部署默认；代码默认 config.management_api.server） | `management_api.server.host/port` |
| 注册表路径 | `{data_dir}` 下 registry JSON | `management_api.registry_path` |
| 搜索 score_threshold | 0.0 | `management_api`（search 段） |
