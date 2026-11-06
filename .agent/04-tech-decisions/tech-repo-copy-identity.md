# 副本身份解耦 repo-copy-identity

关联功能单元：FEAT-MGMT-01/02/03（注册/自愈/注销）、FEAT-MQ-01~06（实例键）、FEAT-WF-01/02/05（任务与重建路由）、FEAT-ED-01/03（事件路由）、FEAT-KS-01（内存刷新目标）、FEAT-MCPS-02/03（MCP 调用链）、FEAT-COMMON-05（normalize_repo_key）。

## 1. 启用依据与背景坑点

- 场景：project_id 由 root commit 派生（跨 clone/镜像稳定），同一台机器上同一 project_id 会出现多个本地副本（fork、上游+个人 clone、模板仓库复制品）。若注册表、内存实例、任务队列以 project_id 为主键，后注册副本会顶替先注册副本的路径与活跃实例——查询返回"路径是 A 的、知识点却描述 B 版本内容"的语义错位。
- 不这么做的故障：单进程内双副本交替查询共用同一 active 单例（最后装载者的 HEAD 树），搜索结果跨副本污染；事件 hooks 路径互相顶替导致提交同步写错工作目录。
- 部署前提：每用户本地部署一套服务 + 共享云端 Qdrant 数据集——project_id 是持久层"数据分区键"（多租户隔离），repo_key 是本地"运行时实例键"，两者正交。

## 2. 选定的技术方案与强制契约

- **双命名空间**：持久层按 (project_id, file_version_id) 寻址与隔离（不变）；注册/实例/任务/事件全部改用 repo_key（`normalize_repo_key(path)` = normcase(realpath(abspath(normpath(path)))），结果本身是可用路径，git 操作直接复用）。
- **实现落点**：`src/codebase_rag/common/paths.py`（唯一规范化实现，严禁各域自写路径归一）；management_api 注册表 `Dict[repo_key, ProjectRecord]`（ProjectRecord.project_id 降为分区指针）；memory_qdrant `_active: Dict[repo_key, cache_key]`、cache key = `repo_key:branch@commit`、`_CachedInstance.project_id` 仅为分区销毁元数据；workflow `_project_repos: Dict[repo_key, project_id]`、`WorkflowTask.repo_key`、队列键 `task.repo_key or task.project_id`；event_detection `_projects`/`_watchers`/`_dedup_timestamps` 键为 repo_key、hook payload 携带 repo_path、GitEvent/HookEvent 携带 git_repo_path。
- **注册/注销语义**：同 repo_key 重复注册 = 刷新；同 project_id 新路径 = 新增副本（project_copy_registered）；`unregister_repo(path)` 单副本注销、`unregister_project(project_id)` 整分区注销——两者恒不删 Qdrant points（deleted_points=0）。
- **MCP/管理调用链**：MCP 工具以客户端传入的仓库根路径解析 project_id（分区）+ repo_key（实例），search/dense_search 传 repo_key；管理面按 project_id 寻址的端点（search/rebuild/sync/status）取首个注册副本（确定性管理视图，多副本消歧走 MCP 路径层）。
- **管理面 REST 按 project_id 的端点语义**：多副本时为"首个注册副本"的确定性管理视图；面向用户的多副本操作走 MCP 工具（路径寻址）或 DELETE /repos（单副本注销）。

## 3. 核心不变量与禁止项（Invariants & Prohibitions）

1. 严禁用 project_id 作为内存实例、注册表、任务队列、watcher/hook 状态的键——project_id 只允许出现在持久层分区寻址与实例元数据中。
2. 严禁在任何域自写路径规范化——必须复用 `common/paths.py` 的 `normalize_repo_key`（大小写/符号链接/网络盘归一）。
3. 同一 project_id 的多个副本严禁共用或顶替彼此的活跃实例、hooks、watcher 与登记条目；副本间交互仅通过持久层（同 fvid 知识点天然共享复用）。
4. 注销（单副本或整分区）严禁删除持久层 Qdrant points——数据属于共享分区，删除需未来显式的数据管理操作。
5. hook 脚本 payload 必须携带 repo_path；无 repo_path 的存量 hook 事件仅允许在"该分区恰有一个副本"时兜底路由，多副本歧义必须丢弃并告警，严禁随机选择副本。
6. move-heal 的判定基准是“哪些已登记副本消失了”而非“移动了哪一个”：存在 unknown（卷离线）或 gone 为 0（无副本消失，即复制/新克隆）时严禁自动登记 incoming；gone ≥ 1 时只迁移 gone 那几条，alive 副本的登记/实例/hooks 不得触碰；分区无任何登记时仍可凭 .git 身份缓存恢复（详见 tech-moved-repo-heal.md）。
