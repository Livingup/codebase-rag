# 仓库移动自愈 moved-repo-heal

关联功能单元：FEAT-MGMT-02（heal_moved_project 实现）、FEAT-MCPS-03（信号来源：每次 MCP 读解析 project_id 时 fire-and-forget 上报）、FEAT-MCPS-02（搜索无活跃实例时同步自愈重试一次）、FEAT-MGMT-01（复用注册主链）。

## 1. 启用依据与背景坑点

- 场景：用户在本机移动/重命名已登记仓库目录后，编程客户端仍持旧路径或新路径调用 MCP 工具；若不做自愈，检索与知识提交将持续报"未注册"。
- 不这么做的故障：仓库移动后必须人工重新注册并触发全量重建；更危险的是复制/双克隆场景（同一 project_id 两个活目录）若自动切换登记路径，会造成知识写入指向错误工作目录。

## 2. 选定的技术方案与强制契约

- **实现落点**：管理面域服务门面 `services/management_api/service.py` 的 `heal_moved_project`；MCP 读路径以客户端传入的当前仓库根目录作为移动信号（`services/mcp_service/service.py` 的 `_schedule_move_heal` / `_heal_moved_project_quietly`）。
- **判定契约（全部满足才自愈）**：① incoming_path 本身是 Git 仓库根（不上溯）；② 从 incoming_path 的 root commit 重新派生的 project_id 与目标一致；③ incoming 的 repo_key 未登记，且二选一：(a) 该 project_id 已登记副本中存在路径 .git 已不存在的失联条目（存在任一存活副本则拒绝——复制/双克隆场景必须显式注册），或 (b) 该 project_id 完全无登记副本（重启恢复被裁剪但 .git 缓存随仓库迁移）。自愈执行时先移除该分区的全部失联条目再走 register_project。多副本语义详见 `.agent/04-tech-decisions/tech-repo-copy-identity.md`。
- **并发契约**：按 project_id 合并为单次执行——`_in_flight_heals` 字典 + `asyncio.shield`，并发调用者共享同一在途任务，单个调用者取消不影响自愈执行。
- **热路径契约**：同步预检只做注册表比对与存在性检查（无 git 子进程），立即返回 False；重量级身份重派生仅在预检命中后执行。
- **搜索联动契约**：搜索遇 NoActiveInstanceError 时先同步自愈再重试一次，仍失败才抛 ProjectNotIndexedError。

## 3. 核心不变量与禁止项（Invariants & Prohibitions）

1. 自愈身份必须以传入仓库根 commit 重新派生的 project_id 为依据——严禁采信 .git 缓存文件或客户端声称的路径。
2. 同身份仓库存在任一存活登记副本（复制/双克隆）时严禁自动切换或新增登记——切换仅限显式注册。
3. 自愈仅恢复既有登记身份，严禁构成新的注册授权（register_project 工具的调用边界约束不被自愈绕过）。
4. 重启后被剪枝的登记条目可凭随仓库携带的 .git 身份缓存恢复入口，但恢复仍需通过身份重派生校验。
5. MCP 读路径的 fire-and-forget 自愈严禁阻塞调用主流程。
