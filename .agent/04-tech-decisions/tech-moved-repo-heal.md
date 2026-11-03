# 仓库移动自愈 moved-repo-heal

关联功能单元：FEAT-MGMT-02（heal_moved_project 实现）、FEAT-MCPS-03（信号来源：每次 MCP 读解析 project_id 时 fire-and-forget 上报）、FEAT-MCPS-02（搜索无活跃实例时同步自愈重试一次）、FEAT-MGMT-01（复用注册主链）。

## 1. 启用依据与背景坑点

- 场景：用户在本机移动/重命名已登记仓库目录后，编程客户端仍持旧路径或新路径调用 MCP 工具；若不做自愈，检索与知识提交将持续报"未注册"。
- 不这么做的故障：仓库移动后必须人工重新注册并触发全量重建；更危险的是复制/双克隆场景（同一 project_id 两个活目录）若自动切换登记路径，会造成知识写入指向错误工作目录。

## 2. 选定的技术方案与强制契约

- **实现落点**：管理面域服务门面 `services/management_api/service.py` 的 `heal_moved_project`；MCP 读路径以客户端传入的当前仓库根目录作为移动信号（`services/mcp_service/service.py` 的 `_schedule_move_heal` / `_heal_moved_project_quietly`）。
- **判定契约（三态 + 决策表）**：① incoming_path 本身是 Git 仓库根（不上溯）；② 从 incoming_path 的 root commit 重新派生的 project_id 与目标一致；③ 对同 project_id 的每个已登记副本定三态：`alive`（.git 目录含 HEAD，或 worktree 指针指向的 gitdir 含 HEAD）/ `gone`（不满足 alive 且所在卷在线）/ `unknown`（不满足 alive 但卷不可用，如网络盘、移动硬盘离线）。决策：`unknown` 存在 → 拒绝；`gone` 为 0（没有任何副本消失）→ 拒绝，视为新增克隆，必须显式注册；`gone ≥ 1` → 自愈，且**只迁移 gone 的那几条**，alive 副本的登记、hooks、watcher、内存实例完全不动。多副本语义详见 `.agent/04-tech-decisions/tech-repo-copy-identity.md`。
- **活/死判据**：不得以“`.git` 是否存在”作为存活判据——只带 `hooks/` 的幽灵 `.git` 会把已消失副本误判为存活并永久阻塞后续自愈；统一用 `_git_metadata_present()`（目录需含 HEAD，指针文件需 gitdir 目标含 HEAD）+ `_path_volume_available()`（盘符/挂载点可用）。
- **执行契约（顺序敏感）**：第一步先对 gone 副本调 `event_detection.unregister_repo(path)`（停 watcher + remove_hooks，后者无 makedirs，对消失路径安全）；第二步 `register_project(incoming, display_name=继承 gone 副本的 display_name)`；第三步对 gone 副本执行 `workflow.unregister_repo` + `memory.destroy_repo_instances` + 移出 `_projects` + 保存注册表。第一步先行的目的：切断“旧 watcher 发现 hook 缺失 → inject_hooks → 在死路径重建幽灵 .git”的因果链。第二步失败时仍将 gone 副本移出注册表并重存，避免注册表长期指向已不存在路径。
- **并发契约**：single-flight 键为 `(project_id, incoming repo_key)`（`_in_flight_heals` + `asyncio.shield`），同一 project 的不同副本并行移动时互不吞并，单个调用者取消不影响自愈执行。
- **热路径契约**：同步预检只做注册表比对与 `.git`/卷探针（无 git 子进程），热路径（incoming 已登记或无 gone 副本）立即返回 False；重量级身份重派生仅在预检命中后执行。
- **下游守卫**：`event_detection.inject_hooks` 在 `.git` 不存在时记 `hook_injection_skipped_missing_repo` 并直接返回（绝不 makedirs）；`_check_and_repair_hooks` 对 `.git` 已消失的副本记 `hook_health_check_skipped_missing_repo` 并跳过（不做自动注销，避免把离线盘误删）；`workflow.get_repo_key_for_project` 优先返回 `.git` 仍存在的副本，全失效时回退首个匹配。
- **搜索联动契约**：搜索遇 NoActiveInstanceError 时先同步自愈再重试一次，仍失败才抛 ProjectNotIndexedError。

## 3. 核心不变量与禁止项（Invariants & Prohibitions）

1. 自愈身份必须以传入仓库根 commit 重新派生的 project_id 为依据——严禁采信 .git 缓存文件或客户端声称的路径。
2. `gone == 0`（没有任何副本消失）时严禁自动登记 incoming 路径——复制/双克隆场景必须显式注册；判定的关键词是“哪些副本消失了”，而不是“用户移动了哪一个”。
3. 迁移与清理必须按 repo_key 精确到副本：严禁按 project_id 批量注销——alive 副本的登记、hooks、watcher、内存实例一律不得触碰。
4. 任何组件（hook 注入、hook 巡检、路径探测）严禁向已消失的路径写盘或建目录——幽灵 `.git` 既是噪声源，又会反过来污染存活判据。
5. 卷离线的副本严禁当成已删除：`unknown` 状态下不做任何自动登记或清理。
6. 自愈仅恢复既有登记身份，严禁构成新的注册授权（register_project 工具的调用边界约束不被自愈绕过）。
7. 重启后被剪枝的登记条目可凭随仓库携带的 .git 身份缓存恢复入口，但恢复仍需通过身份重派生校验。
8. MCP 读路径的 fire-and-forget 自愈严禁阻塞调用主流程。
