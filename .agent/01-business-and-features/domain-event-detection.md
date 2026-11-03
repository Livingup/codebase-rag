# Git 事件检测 event_detection

## 一、业务域职责边界与域内铁律

- 负责：Git hook 注入与周期巡检、.git 目录 watcher 兜底探测、HEAD 变化分类为分支切换或内容提交并发布 GitEvent。
- 不负责：事件的后续编排（→ workflow）；仓库登记存储（→ management_api）；HEAD 与分支读取实现（→ git_operations）。
- 域内铁律：事件类型必须以重读仓库 HEAD 与登记基线的比对结果为准，不得以 hook 名称作为最终分类；hook 上报必须短超时且失败放行，事件服务故障不得阻塞用户 Git 操作。

## 二、业务功能单元明细

### [FEAT-ED-01] 仓库副本接入与 HEAD 基线
- **业务目标**：仓库副本注册/恢复时建立事件监听基础设施与 HEAD 基线。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [接入校验]：IF 仓库 `.git` 不存在 → GitRepoNotFoundError。
  2. [重复接入]：内部状态 `_projects` / `_watchers` / `_dedup_timestamps` 均以 repo_key（`normalize_repo_key` 规范化路径）为键；同 repo_key 重复接入 = 刷新 HEAD 基线（路径重拼仅更新记录）；同 project_id 的其他副本是独立条目，互不顶替、各持各的 watcher；全部操作持 `_projects_lock` 串行。
  3. [接入动作]：统一重新执行 inject_hooks + 刷新 HEAD 基线 + 同步 watcher 启停。
  4. [重启恢复]：restore_project 以调用方提供的 last known HEAD 为基线，用于重启后补偿。
  5. [注销]：unregister_project(project_id) 移除该分区全部副本（逐副本停 watcher + 清 hook）；unregister_repo(git_repo_path) 仅移除单副本。
- **级联依赖功能单元（Cascade Links）**：FEAT-GIT-03（get_head_info）、FEAT-COMMON-06（repo_key 规范化）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/event_detection/service.py）：`src/codebase_rag/services/event_detection/service.py`（项目接入段）

### [FEAT-ED-02] Hook 注入与 wrapper 备份/托管标记
- **业务目标**：向 `.git/hooks` 注入四类托管通知钩子（post-commit / post-merge / post-checkout / post-rewrite），与用户已有 hook 共存。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [托管标记判定]：IF 已有 hook 且含 `# Codebase RAG Event Hook` 标记 → 视为托管脚本，直接覆盖升级。
  2. [用户 hook 保护]：ELIF 用户自有 hook → 先备份为 `<hook>.codebase-rag-backup`（备份已存在不重复备份）→ 写 wrapper：先执行原 hook（`if -x backup_path`）再通知本服务。
  3. [hook 上报 payload]：JSON 含 `project_id`（数据分区）+ `repo_path`（副本身份，JSON 转义并做 shell 单引号转义）+ `hook_type` + `args`——多副本同分区时靠 repo_path 区分事件来源；HTTP（curl POST `--max-time 2 || true`）或 named pipe，**失败不阻塞 Git**。
  4. [全新安装]：无既有 hook → 直接新建托管脚本；非 Windows chmod +x。
  5. [卸载规则]：remove_hooks 只删除含托管标记的脚本；备份存在则 rename 还原用户原 hook。
  6. [巡检状态]：hook 状态机 INSTALLED →（被删除/改写）MISSING / CONFLICT →（周期巡检重注入）INSTALLED；巡检按 (repo_key, state) 快照逐副本进行。
  7. [异常]：注入异常抛 HookInjectionError。
- **级联依赖功能单元（Cascade Links）**：无
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/event_detection/service.py）：`src/codebase_rag/services/event_detection/service.py`（hook 注入与巡检段）
  - 域异常体系（services/event_detection/errors.py）：`src/codebase_rag/services/event_detection/errors.py`
  - Git hook 脚本模板（services/event_detection/hooks/）：`src/codebase_rag/services/event_detection/hooks/post-commit`、`src/codebase_rag/services/event_detection/hooks/post-merge`、`src/codebase_rag/services/event_detection/hooks/post-checkout`、`src/codebase_rag/services/event_detection/hooks/post-rewrite` *(同类参考样板：wrapper 共存式 hook 注入)*

### [FEAT-ED-03] 事件分类与去重
- **业务目标**：把 hook 通知或 watcher 原始通知分类为结构化 GitEvent 并发布给 workflow。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [事件路由]：hook 事件带 git_repo_path → `normalize_repo_key` 直达对应副本；无 repo_path 的存量 hook 仅当该 project_id 恰有一个副本时按 project_id 兜底路由，多副本时记 hook_event_ambiguous_project_copies 丢弃；watcher 回调直接携带 repo_key。
  2. [去重窗口]：IF 距上次通知 < `dedup.window_ms=1000`（按 repo_key）→ 丢弃（同副本窗口内重复通知只处理一次）。
  3. [未注册拦截]：repo_key 未注册 → 忽略通知。
  4. [分类规则]：读 HEAD 异常 → 记日志返回不产事件；IF branch 变化 → `BRANCH_SWITCH`；ELIF commit 变化 → `CONTENT_COMMIT`；ELSE → event_no_change 丢弃。
  5. [事件发布]：构造 uuid4 event_id 的 GitEvent（携带 git_repo_path=该副本登记路径）后发布（经 subscribe 注册的回调，接线目标为 workflow.on_git_event）。
- **级联依赖功能单元（Cascade Links）**：FEAT-GIT-03（get_head_info）、FEAT-WF-01（事件消费者）、FEAT-COMMON-06（repo_key 规范化）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/event_detection/service.py）：`src/codebase_rag/services/event_detection/service.py`（事件分类段）
  - 域内辅助实现（services/event_detection/hook_receiver.py）：`src/codebase_rag/services/event_detection/hook_receiver.py`
  - 域数据模型（services/event_detection/models.py）：`src/codebase_rag/services/event_detection/models.py`

### [FEAT-ED-04] Watcher 兜底轮询
- **业务目标**：捕获 hook 抓不到的 HEAD 变化（git reset --hard、外部工具操作）。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [双通道并存]：hook 主动通知为主、watcher 轮询为兜底；按 `watcher.enabled` 启停（默认 False）。
  2. [轮询机制]：轮询 `.git/HEAD` + 解析出的 branch ref 文件内容组成快照 `(head_content, resolved_hash)`，快照变化才回调；选轮询而非事件监听的原因是 Windows ReadDirectoryChangesW 监听不到 .git 内部变化。
  3. [容错]：`.git` 不存在 → 记 error 直接退出；回调异常仅记日志；读文件 OSError → 快照置 ("","")。
  4. [监控范围]：watch_paths 覆盖 HEAD、refs/heads/、MERGE_HEAD、REBASE_HEAD。
- **级联依赖功能单元（Cascade Links）**：FEAT-ED-03（复用事件分类）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域内辅助实现（services/event_detection/watcher.py）：`src/codebase_rag/services/event_detection/watcher.py`

## 三、域内量化参数速查

| 参数 | 默认值 | 配置键 |
| :--- | :--- | :--- |
| 通知去重窗口 | 1000ms | `event_detection.dedup.window_ms` |
| watcher 启用 | false | `event_detection.watcher.enabled` |
| watcher 去抖周期 | 500ms | `event_detection.watcher.debounce_ms` |
| hook 上报超时 | 2s（curl --max-time） | hook 模板内固定 |
