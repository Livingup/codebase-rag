# AGENTS.md

本文档约定 Codebase RAG 在 Windows 本地运行场景下的开发、测试、提交、打包、本地安装与用户端更新流程。目标用户主要在 Windows 本地长期运行服务，因此所有运维脚本集中放在 `ops/windows/`，本文档只说明脚本用途和调用方式，不内嵌脚本源码。

## 0. 项目架构约束记忆路由表（L0）

项目工程约束记忆位于 `.agent/` 目录：13 份业务域文档（`.agent/01-business-and-features/`）、1 份分层规范（`.agent/02-code-structure/architecture-rules.md`，全项目分层词汇唯一来源）、7 篇非平凡技术机制文档（`.agent/04-tech-decisions/`）。本项目为纯后端服务，无独立 UI 现状文档（说明见 architecture-rules.md 第 5 节）。查找业务规则、放码位置或源码清单时，从下表按关键词定位功能单元（共 56 个），跳转至对应域文档。

| 功能单元 ID | 功能名称 | 业务关键词 | 所属业务域文档与锚点 | 关联技术架构抉择 |
| --- | --- | --- | --- | --- |
| `FEAT-COMMON-01` | Key 池调度 | API Key, 轮换, 熔断, 冷却, 429, KeyPool, keypool | `.agent/01-business-and-features/domain-common.md#FEAT-COMMON-01` | `.agent/04-tech-decisions/tech-key-pool.md` |
| `FEAT-COMMON-02` | Token 计数器 | token 计数, 三级回退, 上下文窗口, 预估 | `.agent/01-business-and-features/domain-common.md#FEAT-COMMON-02` | 无 |
| `FEAT-COMMON-03` | 结构化日志 | structlog, 日志, snake_case, 日志轮转 | `.agent/01-business-and-features/domain-common.md#FEAT-COMMON-03` | 无 |
| `FEAT-COMMON-04` | 配置加载与热重载协调 | 配置, 热重载, reload, reconciler, .env, 不可变参数, requires_restart | `.agent/01-business-and-features/domain-common.md#FEAT-COMMON-04` | `.agent/04-tech-decisions/tech-config-reconcile.md` |
| `FEAT-COMMON-05` | 服务组装根 | 组装根, 启动, ServiceContainer, 回调接线, 双 Server | `.agent/01-business-and-features/domain-common.md#FEAT-COMMON-05` | 无 |
| `FEAT-COMMON-06` | 仓库副本路径规范化 | repo_key, normalize_repo_key, 路径归一, 副本身份, 规范化路径 | `.agent/01-business-and-features/domain-common.md#FEAT-COMMON-06` | `.agent/04-tech-decisions/tech-repo-copy-identity.md` |
| `FEAT-WF-01` | Git 事件接收与副本任务队列 | 事件接收, 任务队列, 串行调度, GitEvent, ProjectTaskQueue, repo_key, 副本路由 | `.agent/01-business-and-features/domain-workflow.md#FEAT-WF-01` | `.agent/04-tech-decisions/tech-repo-copy-identity.md` |
| `FEAT-WF-02` | 分支切换与懒重建 | 分支切换, staging, 原子提升, 懒重建, 内存实例重建 | `.agent/01-business-and-features/domain-workflow.md#FEAT-WF-02` | `.agent/04-tech-decisions/tech-memory-instance-lifecycle.md` |
| `FEAT-WF-03` | 内容提交知识生产 | 提交同步, 知识生产, 纯重命名复用, 守卫链, 知识生成 | `.agent/01-business-and-features/domain-workflow.md#FEAT-WF-03` | `.agent/04-tech-decisions/tech-knowledge-outbox.md` |
| `FEAT-WF-04` | 灰区文件 L3 分类 | 灰区, LLM 分类, 类型键, uncertain, 灰区缓存 | `.agent/01-business-and-features/domain-workflow.md#FEAT-WF-04` | 无 |
| `FEAT-WF-05` | 项目初始化（全量/增量） | 初始化, 全量, 增量, 基线知识, backfill | `.agent/01-business-and-features/domain-workflow.md#FEAT-WF-05` | `.agent/04-tech-decisions/tech-memory-instance-lifecycle.md` |
| `FEAT-WF-06` | 补偿循环与死信 | 补偿循环, 重试, 死信, 退避, retry_queue, dead_letter | `.agent/01-business-and-features/domain-workflow.md#FEAT-WF-06` | `.agent/04-tech-decisions/tech-knowledge-outbox.md` |
| `FEAT-ED-01` | 仓库副本接入与 HEAD 基线 | 副本接入, HEAD 基线, 重启恢复, 事件注册, repo_key | `.agent/01-business-and-features/domain-event-detection.md#FEAT-ED-01` | 无 |
| `FEAT-ED-02` | Hook 注入与 wrapper 备份 | Git hook, wrapper, 备份, 托管标记, post-commit, 巡检 | `.agent/01-business-and-features/domain-event-detection.md#FEAT-ED-02` | 无 |
| `FEAT-ED-03` | 事件分类与去重 | 事件分类, 去重窗口, BRANCH_SWITCH, CONTENT_COMMIT | `.agent/01-business-and-features/domain-event-detection.md#FEAT-ED-03` | 无 |
| `FEAT-ED-04` | Watcher 兜底轮询 | watcher, 轮询, 兜底, git reset, 去抖 | `.agent/01-business-and-features/domain-event-detection.md#FEAT-ED-04` | 无 |
| `FEAT-GIT-01` | 变更解析与纯重命名识别 | diff, 变更解析, 纯重命名, name-status, added modified deleted | `.agent/01-business-and-features/domain-git-operations.md#FEAT-GIT-01` | 无 |
| `FEAT-GIT-02` | 三层文件过滤 | 黑名单, 白名单, 灰区, 文件过滤, exclude_patterns | `.agent/01-business-and-features/domain-git-operations.md#FEAT-GIT-02` | 无 |
| `FEAT-GIT-03` | file_version_id 与 blob 读取 | file_version_id, blob, sha256, 严格模式, get_file_content | `.agent/01-business-and-features/domain-git-operations.md#FEAT-GIT-03` | 无 |
| `FEAT-GIT-04` | Git 命令执行器 | 子进程, 超时, GitTimeoutError, 并发上限 | `.agent/01-business-and-features/domain-git-operations.md#FEAT-GIT-04` | 无 |
| `FEAT-KS-01` | 知识提交受理与发布 | 提交受理, 双锁, 发布确认, wait=True, cancelled_stale | `.agent/01-business-and-features/domain-knowledge-sync.md#FEAT-KS-01` | `.agent/04-tech-decisions/tech-knowledge-outbox.md` |
| `FEAT-KS-02` | 外发箱持久化与失败重试 | 外发箱, outbox, ready, 重试, 原子写, tmp replace | `.agent/01-business-and-features/domain-knowledge-sync.md#FEAT-KS-02` | `.agent/04-tech-decisions/tech-knowledge-outbox.md` |
| `FEAT-KS-03` | 幂等快路径与最新版本管理 | 幂等, 跳过, 存在性, stale, 最新版本 | `.agent/01-business-and-features/domain-knowledge-sync.md#FEAT-KS-03` | `.agent/04-tech-decisions/tech-knowledge-outbox.md` |
| `FEAT-KS-04` | 内存镜像刷新与迁移 | 内存刷新, best-effort, rebrand, 镜像 | `.agent/01-business-and-features/domain-knowledge-sync.md#FEAT-KS-04` | 无 |
| `FEAT-QS-01` | 连接生命周期与自愈 | 连接, 自愈, PoolTimeout, 客户端重建, 探活 | `.agent/01-business-and-features/domain-qdrant-storage.md#FEAT-QS-01` | `.agent/04-tech-decisions/tech-config-reconcile.md` |
| `FEAT-QS-02` | 确定性点写入 | upsert, 确认, uuid5, knowledge_set_id, point ID | `.agent/01-business-and-features/domain-qdrant-storage.md#FEAT-QS-02` | `.agent/04-tech-decisions/tech-knowledge-outbox.md` |
| `FEAT-QS-03` | 完整知识集读取 | scroll, 完整知识集, chunk, 字典序最小, 读取 | `.agent/01-business-and-features/domain-qdrant-storage.md#FEAT-QS-03` | `.agent/04-tech-decisions/tech-knowledge-outbox.md` |
| `FEAT-QS-04` | 存在性检查与迁移 | check_existence, 内容寻址, 去重, rebrand, set_payload | `.agent/01-business-and-features/domain-qdrant-storage.md#FEAT-QS-04` | 无 |
| `FEAT-MQ-01` | 实例创建与 LRU 缓存 | 实例创建, LRU, staging, 缓存命中, 容量, repo_key, 副本实例 | `.agent/01-business-and-features/domain-memory-qdrant.md#FEAT-MQ-01` | `.agent/04-tech-decisions/tech-memory-instance-lifecycle.md` |
| `FEAT-MQ-02` | 混合检索 | 混合检索, RRF, dense, sparse, prefetch, 融合 | `.agent/01-business-and-features/domain-memory-qdrant.md#FEAT-MQ-02` | 无 |
| `FEAT-MQ-03` | 活跃实例管理与 staging 晋升 | 活跃实例, 提升, staging, active, 转正 | `.agent/01-business-and-features/domain-memory-qdrant.md#FEAT-MQ-03` | `.agent/04-tech-decisions/tech-memory-instance-lifecycle.md` |
| `FEAT-MQ-04` | LRU 淘汰与空闲清扫 | 淘汰, 空闲清扫, 超容量, idle_timeout | `.agent/01-business-and-features/domain-memory-qdrant.md#FEAT-MQ-04` | `.agent/04-tech-decisions/tech-memory-instance-lifecycle.md` |
| `FEAT-MQ-05` | 懒加载单飞重建 | 懒加载, 单飞, shield, NoActiveInstance, 共享重建 | `.agent/01-business-and-features/domain-memory-qdrant.md#FEAT-MQ-05` | `.agent/04-tech-decisions/tech-memory-instance-lifecycle.md` |
| `FEAT-MQ-06` | 实例销毁与缓存键迁移 | 销毁, update_cache_commit, 缓存键, 注销清理, destroy_repo_instances, 单副本销毁 | `.agent/01-business-and-features/domain-memory-qdrant.md#FEAT-MQ-06` | `.agent/04-tech-decisions/tech-repo-copy-identity.md` |
| `FEAT-MCPS-01` | 多查询分组语义搜索 | codebase_search, 多查询, 多仓库, 分组, top_k | `.agent/01-business-and-features/domain-mcp-service.md#FEAT-MCPS-01` | 无 |
| `FEAT-MCPS-02` | 两阶段检索引擎 | 两阶段检索, 召回, 重排, reranker, Jev, 自愈重试 | `.agent/01-business-and-features/domain-mcp-service.md#FEAT-MCPS-02` | 无 |
| `FEAT-MCPS-03` | 仓库根契约校验 | git_repo_path, 仓库根, project_id, 不上溯, ProjectNotRegistered | `.agent/01-business-and-features/domain-mcp-service.md#FEAT-MCPS-03` | `.agent/04-tech-decisions/tech-moved-repo-heal.md` |
| `FEAT-MCPS-04` | 已提交知识查询 | get_file_knowledge, 知识查询, 严格模式, HEAD 版本 | `.agent/01-business-and-features/domain-mcp-service.md#FEAT-MCPS-04` | 无 |
| `FEAT-MCPS-05` | 知识提交 | add_file_knowledge, 受理, blob_hash, 未覆盖, accepted | `.agent/01-business-and-features/domain-mcp-service.md#FEAT-MCPS-05` | `.agent/04-tech-decisions/tech-knowledge-outbox.md` |
| `FEAT-MCPS-06` | 登记类北向工具 | project_status, list_projects, register_project, 登记状态, 初始化中 | `.agent/01-business-and-features/domain-mcp-service.md#FEAT-MCPS-06` | 无 |
| `FEAT-CE-01` | 深度上下文分析编排 | 上下文分析, 实现现状报告, submit_report, ripgrep, 多候选 | `.agent/01-business-and-features/domain-context-engine.md#FEAT-CE-01` | 无 |
| `FEAT-CE-02` | 本地工具集与 rg 执行 | search_code, read_file, list_directory, rg, 行号 | `.agent/01-business-and-features/domain-context-engine.md#FEAT-CE-02` | 无 |
| `FEAT-MGMT-01` | 仓库副本注册主链与身份派生 | 注册, project_id, uuid5, 身份迁移, 注册表, 批量注册, repo_key, 多副本 | `.agent/01-business-and-features/domain-management-api.md#FEAT-MGMT-01` | `.agent/04-tech-decisions/tech-repo-copy-identity.md` |
| `FEAT-MGMT-02` | 仓库移动自愈 | 移动自愈, heal, 双克隆, 身份重派生, single-flight | `.agent/01-business-and-features/domain-management-api.md#FEAT-MGMT-02` | `.agent/04-tech-decisions/tech-moved-repo-heal.md` |
| `FEAT-MGMT-03` | 仓库副本注销与重启恢复 | 注销, unregister_repo, unregister_project, 单副本, 整分区, 重启恢复, 裁剪, 保留持久数据 | `.agent/01-business-and-features/domain-management-api.md#FEAT-MGMT-03` | `.agent/04-tech-decisions/tech-repo-copy-identity.md` |
| `FEAT-MGMT-04` | REST 管理端点 | REST, /api/v1, health, config reload, 管理搜索, 状态 | `.agent/01-business-and-features/domain-management-api.md#FEAT-MGMT-04` | `.agent/04-tech-decisions/tech-config-reconcile.md` |
| `FEAT-MGMT-05` | 本地 Admin UI | Admin UI, 本地后台, 单页状态页 | `.agent/01-business-and-features/domain-management-api.md#FEAT-MGMT-05` | 无 |
| `FEAT-AG-01` | Provider 生命周期管理 | provider, 服务商, 探活, 三向 diff, degraded | `.agent/01-business-and-features/domain-agent.md#FEAT-AG-01` | `.agent/04-tech-decisions/tech-key-pool.md`、`.agent/04-tech-decisions/tech-llm-sdk-adoption.md` |
| `FEAT-AG-02` | 并发容量选择与绑定 | 并发容量, 信号量, provider 绑定, 等待空位 | `.agent/01-business-and-features/domain-agent.md#FEAT-AG-02` | 无 |
| `FEAT-AG-03` | Agent 注册与执行循环 | run_agent, 工具调用, 执行循环, trace, 批量执行 | `.agent/01-business-and-features/domain-agent.md#FEAT-AG-03` | 无 |
| `FEAT-AG-04` | LLM 调用双层重试 | 429, 退避, 双层重试, 熔断, 鉴权失败, 换 Key | `.agent/01-business-and-features/domain-agent.md#FEAT-AG-04` | `.agent/04-tech-decisions/tech-key-pool.md`、`.agent/04-tech-decisions/tech-llm-sdk-adoption.md` |
| `FEAT-AG-05` | 上下文压缩 | 上下文压缩, compress_and_replace_history, 压缩阈值, 八分区 | `.agent/01-business-and-features/domain-agent.md#FEAT-AG-05` | 无 |
| `FEAT-EMB-01` | 双 Provider 初始化 | embedding, Gemini, DashScope, 密钥健康, 双通道 | `.agent/01-business-and-features/domain-embedding.md#FEAT-EMB-01` | `.agent/04-tech-decisions/tech-key-pool.md`、`.agent/04-tech-decisions/tech-llm-sdk-adoption.md` |
| `FEAT-EMB-02` | embed 门禁与重试 | 向量, 批处理门禁, batch_size, query 模式, document 模式 | `.agent/01-business-and-features/domain-embedding.md#FEAT-EMB-02` | `.agent/04-tech-decisions/tech-key-pool.md`、`.agent/04-tech-decisions/tech-llm-sdk-adoption.md` |
| `FEAT-MCPC-01` | 三种 Transport 连接 | stdio, sse, streamable_http, 工具发现, 外部 MCP | `.agent/01-business-and-features/domain-mcp-client.md#FEAT-MCPC-01` | 无 |
| `FEAT-MCPC-02` | 懒初始化与差量重建 | 懒初始化, 部分成功, 差量重建, 工具缓存 | `.agent/01-business-and-features/domain-mcp-client.md#FEAT-MCPC-02` | `.agent/04-tech-decisions/tech-config-reconcile.md` |

配套全局文档：业务域边界与全局铁律见 `.agent/01-business-and-features/00-domain-boundaries.md`；代码分层与放码位置见 `.agent/02-code-structure/architecture-rules.md`。业务域文档共 13 份，非平凡技术机制文档共 7 篇。

## 1. 目录约定

### 1.1 Git 仓库内容

Git 仓库保存源码、配置模板、构建脚本、运维脚本和文档：

```text
src/                         # Python 源码
tests/                       # 测试代码
pyproject.toml               # Python 包与依赖定义
config.example.yaml          # 配置模板，不包含真实密钥
.env.example                 # 环境变量模板，不包含真实密钥
ops/windows/                 # Windows 本地服务运维脚本
AGENTS.md                    # 本文件
```

Git 仓库不保存：

```text
.env
config.yaml
data/
logs/
.venv/
.venv-build/
dist/
build/
*.zip
用户本地项目仓库
真实 API Key
```

### 1.2 Windows 正式安装目录

正式用户本地服务按“程序目录”和“数据目录”分离：

```text
${CODEBASE_RAG_PROGRAM_DIR}/
  current/                    # 当前版本 junction，指向 releases/<version>
  releases/
    <version>/
      .venv/
      packages/
      scripts/
      VERSION
  service/
    run-service.ps1

${CODEBASE_RAG_DATA_DIR}/
  config/
    config.yaml
    .env
  data/
  logs/
  backups/
  runtime/
  downloads/
```

`${CODEBASE_RAG_PROGRAM_DIR}` 可以随版本替换；`${CODEBASE_RAG_DATA_DIR}` 是用户配置、数据、日志和备份目录，更新时不得覆盖。两者均由 `.env` 中的同名变量指定，用户可自定义路径。

## 2. Windows 运维脚本目录

所有 Windows 运维脚本位于：

```text
ops/windows/
```

脚本清单：

```text
common.ps1                    # 公共函数，被其他脚本 dot-source 引用
install-local-service.ps1     # 从本地安装包或源码目录安装程序文件、venv、配置模板和 run-service.ps1
register-local-service.ps1    # 使用 NSSM 注册 Windows Service
unregister-local-service.ps1  # 卸载 Windows Service
start-local-service.ps1       # 启动正式服务并执行健康检查
stop-local-service.ps1        # 停止正式服务
restart-local-service.ps1     # 重启正式服务并执行健康检查
refresh-local-service-config.ps1 # 触发正式配置热 reload 或重启，支持动态感知进程不可变变更并提示重启
doctor-local-service.ps1      # 本地诊断 Python/Git/目录/端口/健康检查
start-source-takeover.ps1     # 停正式服务，用本地最新源码接管正式配置启动测试
push-source-to-remote.ps1     # 提交并推送源码到远程 Git 仓库
build-windows-package.ps1     # 从本地源码构建 Windows 本地安装 zip
update-from-zip.ps1           # 使用本地构建 zip 更新本地服务，失败回滚
```

需要管理员权限的脚本必须在“以管理员身份运行”的 PowerShell 中执行。

## 3. `.env` 资源文件与脚本参数

### 3.1 `.env` 只放通用稳定资源

`.env` 提供不常变化的资源配置：本地路径、API Key、服务名、远程仓库身份等。用户拿到源码后只需填写一次。

源码仓库模板：

```powershell
Copy-Item .\.env.example .\.env
notepad .\.env
```

运行服务必填项不在本文档手工枚举，统一按当前 `config.example.yaml` 的占位符确定：

- 当前启用配置中出现的 `${变量名}`，真实 `.env` 必须提供同名变量且值非空。
- 注释示例中的 provider、模型或外部服务占位符，只有在取消注释并启用后才需要提供。
- `config.example.yaml` 发生变更后，应同步检查 `.env.example` 和真实 `.env`，确保新增占位符都有对应变量。
- `.env.example` 允许同时包含运维脚本使用的变量；这类变量不一定出现在 `config.example.yaml` 中，但应能被对应脚本读取。

同步源码到远程 Git 仓库、安装目录、服务名、健康检查地址等运维脚本参数，按实际使用的脚本要求在 `.env` 中填写。

通用脚本读取 `.env` 的顺序：

1. 显式传入 `-EnvFile <path>`。
2. 环境变量 `CODEBASE_RAG_ENV_FILE`。
3. 当前执行目录下的 `.env`。
4. 源码根目录或本地安装包根目录下的 `.env`。

`install-local-service.ps1` 和 `refresh-local-service-config.ps1` 用于落地正式配置，要求使用真实 `.env`：

1. 显式传入 `-EnvFile <path>`。
2. 如果未传 `-EnvFile`，只读取源码根目录或本地安装包根目录下的 `.env`。
3. 如果仍然找不到真实 `.env`，脚本直接失败，提示用户提供。

安装和配置刷新脚本不得使用 `.env.example` 作为真实运行配置。

缺少必填资源变量时，脚本直接失败并提示缺少的变量名。

### 3.2 动态参数通过脚本参数传入

每次推送、构建、更新时会变化的值（版本号、tag、提交消息、zip 路径等）不放 `.env`，由脚本参数显式传入：

- **`-Version`**：本地安装包版本号，构建时必传。
- **`-TagName`**：Git tag 名称，需要创建源码版本 tag 时传入。
- **`-CommitMessage`**：提交消息，有未提交修改时必传。
- **`-CreateTag`**：开关，推送时是否创建 tag。
- **`-PackageZip`**：本地构建 zip 路径，更新时必传。

## 4. 前置依赖

开发者和正式用户本机需要：

```text
Windows 10/11
PowerShell 5.1+
Python 3.11+
Git for Windows
NSSM，用于注册 Windows Service
ripgrep 14+，提供 rg.exe，用于 codebase_context_analysis 的本地源码搜索
```

`nssm.exe` 需要在 PATH 中，或调用脚本时通过 `-NssmExe` 指定完整路径。
`rg.exe` 需要在 PATH 中。Windows 推荐安装命令：

```powershell
winget install --id BurntSushi.ripgrep.MSVC -e
```

如果没有 `winget`，可从 ripgrep 官方 release 下载 Windows MSVC zip，解压后把包含 `rg.exe` 的目录加入系统 PATH。

安装后验证：

```powershell
where.exe rg
rg --version
```

如果在服务运行后才安装或更新 ripgrep，需要重启 CodebaseRAG 服务；Context Engine 会在首次使用 `codebase_context_analysis` 时检测并缓存 `rg.exe` 可用性。

## 5. 开发完成后的本地源码接管测试

用途：开发者本地已经运行正式服务，但需要停掉正式服务，用最新源码接管正式配置和正式端口进行真实环境验证。

### 5.1 执行前要求

- 当前源码已完成修改。
- 确认修改不会破坏正式数据。
- 如涉及持久化结构变更，先备份 `${CODEBASE_RAG_DATA_DIR}`。
- 不要让正式服务和源码实例同时监听同一项目仓库。

### 5.2 启动源码接管测试

在仓库根目录执行：

```powershell
.\ops\windows\start-source-takeover.ps1 -EnvFile .\.env
```

指定正式配置路径：

```powershell
.\ops\windows\start-source-takeover.ps1 -EnvFile .\.env -ConfigPath "${CODEBASE_RAG_DATA_DIR}\config\config.yaml"
```

安装开发依赖后接管启动：

```powershell
.\ops\windows\start-source-takeover.ps1 -EnvFile .\.env -InstallDevDependencies
```

脚本会执行：

1. 停止正式 Windows Service。
2. 创建或复用源码目录 `.venv`。
3. 执行 editable install。
4. 使用正式 `config.yaml` 启动 `python -m codebase_rag`。
5. 该终端窗口作为源码服务进程，按 `Ctrl+C` 停止。

### 5.3 测试内容

至少验证：

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:18080/api/v1/health
```

同时验证：

- Management API 是否启动。
- MCP Server 是否启动，默认 `http://127.0.0.1:19090/mcp`。
- 已注册项目是否可恢复。
- 搜索是否正常。
- Git hook / watcher 是否正常。
- 配置 reload 是否正常。
- 日志是否正常。

### 5.4 恢复正式服务

停止源码实例：

```text
Ctrl+C
```

恢复正式服务：

```powershell
.\ops\windows\start-local-service.ps1 -EnvFile .\.env
```

如果需要重启正式服务：

```powershell
.\ops\windows\restart-local-service.ps1 -EnvFile .\.env
```

## 6. 代码提交到远程 Git 仓库

### 6.1 手动 Git 流程

```powershell
git status --short
git add -A
git commit -m "描述本次修改"
git push -u origin master
```

如果需要打 tag：

```powershell
git tag v0.1.1
git push origin v0.1.1
```

### 6.2 使用脚本提交推送

提交并推送当前分支：

```powershell
.\ops\windows\push-source-to-remote.ps1 -EnvFile .\.env
```

指定远程和分支：

```powershell
.\ops\windows\push-source-to-remote.ps1 -EnvFile .\.env -RemoteName origin -Branch master -CommitMessage "描述本次修改"
```

提交、推送并创建源码版本 tag：

```powershell
.\ops\windows\push-source-to-remote.ps1 -EnvFile .\.env -CommitMessage "version 0.2.0" -CreateTag -TagName v0.2.0
```

## 7. 从本地源码构建 Windows 安装包

Gitee 私有仓库 Release 附件不作为命令行交付渠道。正式用户安装和更新时，统一在本机拉取源码后构建本地安装包，再从本地 zip 安装或更新。

### 7.1 获取或更新源码

首次获取源码：

```powershell
git clone <repo-url> codebase-rag
```

已有源码时更新：

```powershell
git pull --ff-only
```

需要安装指定版本时，切换到对应 tag 或 commit：

```powershell
git checkout v0.2.0
```

### 7.2 构建命令

在源码仓库根目录执行：

```powershell
.\ops\windows\build-windows-package.ps1 -EnvFile .\.env -Version 0.2.0
```

跳过测试构建：

```powershell
.\ops\windows\build-windows-package.ps1 -EnvFile .\.env -Version 0.2.0 -SkipTests
```

### 7.3 构建输出

默认输出到：

```text
dist\packages\CodebaseRAG-<version>-windows-x64.zip
dist\packages\CodebaseRAG-<version>-windows-x64.zip.sha256
```

本地安装 zip 内包含：

```text
VERSION
package-manifest.json
config.example.yaml
.env.example
packages\codebase_rag-<version>-py3-none-any.whl
wheels\*.whl
scripts\*.ps1
```

本地安装 zip 不包含：

```text
.git/
src/
tests/
.env
config.yaml
data/
logs/
.venv/
.venv-build/
```

## 8. 用户端首次安装

### 8.1 准备 `.env`

用户在源码仓库根目录准备真实 `.env`：

```powershell
Copy-Item .\.env.example .\.env
notepad .\.env
```

必须填写规则：

- 以当前 `config.example.yaml` 为准，凡是启用配置中出现的 `${变量名}`，真实 `.env` 必须提供同名变量且值非空。
- Agent LLM Key 不按“至少一个”粗略判断，而是按当前启用的 provider、默认 provider、工作流队列、Context Engine、MCP reranker 等实际引用关系填写。
- 注释掉的 provider 示例不要求填写；只有取消注释并启用后，才需要补齐其中的占位符变量。
- 运维脚本变量按安装、注册服务、启动、健康检查、Git 推送等脚本的实际使用场景填写。

没有启用、也没有被脚本使用的 Key 可以删掉；被当前启用配置或脚本引用的变量不得留空占位。

### 8.2 构建并解压本地安装包

在源码仓库根目录执行：

```powershell
.\ops\windows\build-windows-package.ps1 -EnvFile .\.env -Version 0.2.0
Expand-Archive -Path .\dist\packages\CodebaseRAG-0.2.0-windows-x64.zip -DestinationPath .\dist\packages\CodebaseRAG-0.2.0-windows-x64 -Force
```

### 8.3 安装并启动服务

在解压后的本地安装包目录执行：

```powershell
.\scripts\install-local-service.ps1 -EnvFile <path-to-real-env>
.\scripts\register-local-service.ps1 -EnvFile <path-to-real-env>
.\scripts\start-local-service.ps1 -EnvFile <path-to-real-env>
```

如果 `.env` 就在源码仓库根目录，示例：

```powershell
.\scripts\install-local-service.ps1 -EnvFile D:\project\codebase-rag\.env
.\scripts\register-local-service.ps1 -EnvFile D:\project\codebase-rag\.env
.\scripts\start-local-service.ps1 -EnvFile D:\project\codebase-rag\.env
```

每次安装都会从安装包或源码目录的 `config.example.yaml` 重新生成正式配置文件：

```text
${CODEBASE_RAG_DATA_DIR}\config\config.yaml
```

每次安装都会复制用户提供的真实 `.env` 到正式配置目录：

```text
${CODEBASE_RAG_DATA_DIR}\config\.env
```

用户只维护真实 `.env` 资源文件；默认不需要修改脚本。安装脚本会根据 `.env` 中的 `CODEBASE_RAG_DATA_DIR` 覆盖正式 `.env` 内的同名变量，保证服务进程读取的数据目录与安装目录一致。

如果需要调整服务内部参数，优先修改源码中的 `config.example.yaml`，重新构建安装包并安装；或使用配置刷新脚本把最新模板同步到正式 `config.yaml` 并触发配置热 reload。

诊断：

```powershell
.\scripts\doctor-local-service.ps1 -EnvFile <path-to-real-env>
```

## 9. 用户端本地构建 zip 更新服务

用户在源码仓库更新到目标版本后，重新构建本地安装 zip：

```powershell
git pull --ff-only
.\ops\windows\build-windows-package.ps1 -EnvFile .\.env -Version 0.2.1
```

在已安装版本或新 zip 解压目录中执行：

```powershell
.\scripts\update-from-zip.ps1 -EnvFile <path-to-real-env> -PackageZip "D:\project\codebase-rag\dist\packages\CodebaseRAG-0.2.1-windows-x64.zip"
```

脚本会执行：

1. 读取当前版本。
2. 解压本地构建 zip。
3. 备份 `${CODEBASE_RAG_DATA_DIR}\config`、`data`、`runtime`。
4. 停止正式服务。
5. 安装新版本到 `${CODEBASE_RAG_PROGRAM_DIR}\releases\<new-version>`，并使用安装包内的 `config.example.yaml` 重新生成正式 `config.yaml`。
6. 切换 `current`。
7. 启动服务。
8. 访问健康检查。
9. 如果失败，切回旧版本并重新启动服务。

### 9.1 配置热重载（Hot-Reload）与服务更新规范

正式服务实际读取的是运行数据目录中的配置文件：

```text
${CODEBASE_RAG_DATA_DIR}\config\.env
${CODEBASE_RAG_DATA_DIR}\config\config.yaml
```

**配置维护与更新的两大场景标准流程**：

#### 场景 1：结构变更（新增、删除或重命名配置字段名）
- **触发条件**：新增功能特性、调整配置项命名、增删 Provider/Model 结构、引入新的环境变量等架构或功能变更。
- **操作规则**：
  1. **必须修改项目模板**：修改源码仓库根目录的 `config.example.yaml` 与 `.env.example`，同步更新 `src/codebase_rag/config/schema.py` 及相关测试。
  2. **严禁将正式配置写回仓库**：仓库中只保留模板，不包含任何真实密钥和环境私有数据。
  3. **同步到运行目录**：将新增/变更的配置结构同步至正式配置目录（`${CODEBASE_RAG_DATA_DIR}\config\`）。
  4. **触发更新**：执行 `.\ops\windows\refresh-local-service-config.ps1`。若涉及进程不可变参数（端口/系统目录等），追加 `-Restart` 重启服务。

#### 场景 2：数值变更（仅调整现有字段的值或参数）
- **触发条件**：日常调优参数、修改阈值、调整模型优先级或重排序策略、增删/轮换 API Key 凭据、调整并发信号量等。
- **操作规则**：
  1. **严禁修改项目中的 example 模板**：仓库中的 `config.example.yaml` 和 `.env.example` 是公开示例模板，不得因本地/生产环境调参而被修改或污染。
  2. **直接修改服务的配置文件**：直接编辑服务部署/数据目录中的真实配置文件（`${CODEBASE_RAG_DATA_DIR}\config\config.yaml` 或 `${CODEBASE_RAG_DATA_DIR}\config\.env`）。
  3. **触发平滑热重载**：在终端执行 `.\ops\windows\refresh-local-service-config.ps1`（或调用 `POST http://127.0.0.1:18080/api/v1/config/reload`）。
     - 服务端内部通过 `ConfigManager` 协同各服务 `Reconciler` 增量平滑迁移（KeyPool 密钥动态更新且完整保留现有健康度、失败计数与冷却状态；LLM/Embedding Provider 动态切换与连接重连）。
     - 只要不修改进程不可变参数，**无需重启服务**即可瞬间生效。

配置热重载接口：

```text
POST http://127.0.0.1:18080/api/v1/config/reload
```

系统内部采用 Reconciler 增量协同机制（ConfigManager + Service Reconcilers）：
- **动态 Reconcile**：动态刷新各 LLM Provider/Model、KeyPool 密钥（增删改并完整保留现有健康度、失败计数与冷却状态）、并发信号量大小、向量数据库客户端连接与探活、外部 MCP Server 连接、Workflow 代理注册等。
- **不可变参数感知**：自动检测进程不可变参数（Management API / MCP 监听端口或 Host、系统数据根目录、系统日志级别）。若存在不可变变更，API 成功完成支持动态更新的部分并返回 `requires_restart: true` 及原因列表，而不引发服务中断或异常崩溃。

更新规则速查表：

| 修改内容 | 推荐命令 | 是否需要重启 | 行为说明 |
| --- | --- | --- | --- |
| 调整字段值、参数规则、模型优先级或 Key 凭据 | `.\ops\windows\refresh-local-service-config.ps1` | 不需要 | 直接针对 `${CODEBASE_RAG_DATA_DIR}\config\` 触发热重载，Reconciler 平滑平移连接与状态。 |
| 改动核心服务端口、系统数据根目录等不可变参数 | `.\ops\windows\refresh-local-service-config.ps1 -Restart` | 需要 | 脚本在检测到需重启时自动或手动追加 `-Restart` 重启 Windows Service。 |
| 仅需同步指定外部配置文件并触发重载 | `.\ops\windows\refresh-local-service-config.ps1 -ConfigPath <path> -EnvFile <path>` | 视配置而定 | 可显式指定输入文件路径同步至正式配置目录并重载。 |

如果 Python 源码、依赖、运维脚本或入口逻辑发生变化，不使用轻量刷新流程，应重新构建 zip 并执行 `update-from-zip.ps1`。

## 10. 日常运维命令

查看服务：

```powershell
Get-Service CodebaseRAG
```

启动服务：

```powershell
.\ops\windows\start-local-service.ps1 -EnvFile .\.env
```

停止服务：

```powershell
.\ops\windows\stop-local-service.ps1 -EnvFile .\.env
```

重启服务：

```powershell
.\ops\windows\restart-local-service.ps1 -EnvFile .\.env
```

刷新正式配置并热 reload：

```powershell
.\ops\windows\refresh-local-service-config.ps1
```

诊断服务：

```powershell
.\ops\windows\doctor-local-service.ps1 -EnvFile .\.env
```

手动健康检查：

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:18080/api/v1/health
```

查看端口：

```powershell
Get-NetTCPConnection -LocalPort 18080 -State Listen
Get-NetTCPConnection -LocalPort 19090 -State Listen
```

查看日志：

```powershell
Get-Content "${CODEBASE_RAG_DATA_DIR}\logs\service.stdout.log" -Tail 200 -Wait
Get-Content "${CODEBASE_RAG_DATA_DIR}\logs\service.stderr.log" -Tail 200 -Wait
```

卸载服务注册，不删除用户数据：

```powershell
.\ops\windows\unregister-local-service.ps1 -EnvFile .\.env
```

## 11. 关键约束

- 正式服务默认监听 `127.0.0.1`，不要默认暴露到局域网。
- 用户真实 `.env`、真实配置、密钥、日志、数据不得提交到 Git。
- 安装和更新会使用用户提供的真实 `.env` 覆盖 `${CODEBASE_RAG_DATA_DIR}\config\.env`，并使用安装包内 `config.example.yaml` 重新生成 `${CODEBASE_RAG_DATA_DIR}\config\config.yaml`。
- 正式安装和更新必须使用用户本机从源码构建出的本地安装 zip。
- 不再通过 Gitee Release 发布或命令行下载安装包。
- 本地安装 zip 只交付运行所需 wheel、依赖 wheel、配置模板和运维脚本。
- 开发接管测试必须先停正式服务，避免两个实例同时监听同一端口或同一 Git 仓库。
- 涉及持久化结构变更时，必须先备份再接管测试或更新。

## 12. Qdrant 向量数据库持久化约定

当前 Codebase RAG 系统采用 **本地 Qdrant Docker 容器** 进行向量数据的持久化存储（访问地址为 `localhost:6333`）。本地持久化减少了对外部网络的依赖，提高了服务响应速度。

**云端 Qdrant 回退配置（文档不保存明文密钥）：**
云端地址与端口可记录在文档中；`QDRANT_API_KEY` 只保存在本机受控备份，不得复制到 `AGENTS.md`、Git 提交、Issue、聊天或日志中。回退参数格式如下：

```env
QDRANT_HOST=qdrant.mindloop.chat
QDRANT_PORT=48831
QDRANT_GRPC_PORT=32293
QDRANT_HTTPS=true
QDRANT_API_KEY=<从本机受控备份按需查询>
```

迁移时保留了以下两个本机备份：
- 正式数据目录备份（优先使用）：`C:\ProgramData\CodebaseRAG\config\.env.qdrant.bak`
- 源码工作区备份（本地临时副本，不得加入 Git）：`G:\project\codebase-rag\.env.qdrant.bak`

在管理员 PowerShell 中查询。第一段只确认密钥是否存在，不显示内容；第二段仅在实际回退时执行，会把完整 Qdrant 配置（包括密钥）显示在当前受控终端：

```powershell
$backup = 'C:\ProgramData\CodebaseRAG\config\.env.qdrant.bak'
if (-not (Test-Path -LiteralPath $backup)) {
    throw "Qdrant 回退备份不存在：$backup"
}

# 安全检查：只确认 API Key 存在，不显示密钥
if (Select-String -LiteralPath $backup -Pattern '^QDRANT_API_KEY=.+$' -Quiet) {
    'QDRANT_API_KEY 已保存在回退备份中'
} else {
    throw '回退备份中缺少 QDRANT_API_KEY'
}

# 仅在实际回退时查询完整参数；该命令会在终端显示密钥
Get-Content -LiteralPath $backup |
    Where-Object { $_ -match '^QDRANT_(HOST|PORT|GRPC_PORT|HTTPS|API_KEY)=' }
```

回退时将受控备份中的 `QDRANT_` 配置写入真实 `.env`，随后必须重启 CodebaseRAG 服务使进程环境生效：

```powershell
Restart-Service CodebaseRAG
```
