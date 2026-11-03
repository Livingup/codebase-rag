# 公共底座 common 与 config（组装根）

## 一、业务域职责边界与域内铁律

- 负责：跨服务复用的基础设施（Key 池、Token 计数、结构化日志、仓库副本路径规范化）、配置加载与热重载协调（ConfigManager）、服务组装根（__main__.py）。
- 不负责：任何业务分支（common/ 严禁包含业务逻辑）；各服务自己的 reconciler 实现（各服务注册）。
- 域内铁律：ConfigManager 是运行时配置唯一持有者（Single Source of Truth）；组装根是唯一允许实例化与接线全部服务的地方。

## 二、业务功能单元明细

### [FEAT-COMMON-01] Key 池调度（KeyPool）
- **业务目标**：为 agent / embedding / mcp_service(jev) 提供同构的多 Key 轮询调度与健康熔断原语。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [轮询算法]：取 Key 时先扫描全部状态，凡"不健康且当前时刻 ≥ cooldown_until"即自动恢复（healthy=True、失败计数清零、记 key_recovered）；随后从游标起轮询一圈，返回首个 healthy 的 Key 并推进游标；全不健康返回 None；peek() 不推进游标做 best-effort 取 Key。
  2. [熔断规则]：report_failure 将 consecutive_failures+1，达到阈值即标记不健康并进入冷却（cooldown_until=now+cooldown_seconds）；成功则完全复位；mark_unhealthy 供认证错误立即熔断。
  3. [频控豁免]：频控类错误（429 / RESOURCE_EXHAUSTED / Too Many Requests）只做指数退避重试，不得计入失败熔断；仅鉴权失败、服务端故障与网络超时上报摘除（由各使用方的重试分支实现，见 FEAT-AG-04 / FEAT-EMB-02）。
  4. [热更新保留状态]：update_keys 按 Key 字符串匹配复用既有 KeyState（健康度、失败计数、冷却状态原样保留），仅新 Key 新建状态，游标归零。
  5. [空池拦截]：空 Key 列表抛 ValueError。
- **级联依赖功能单元（Cascade Links）**：FEAT-AG-01、FEAT-EMB-01、FEAT-MCPS-02（使用方）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-key-pool.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 公共基础设施（common/keypool.py）：`src/codebase_rag/common/keypool.py` *(同类参考样板：状态保留式 Key 池)*

### [FEAT-COMMON-02] Token 计数器（TokenCounter）
- **业务目标**：三级回退的 token 计数与消耗预估。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [三级回退]：count_tokens 依次尝试 Provider SDK → TokenCounter（Embedding API）→ 本地 len//4 估算。
  2. [预估]：estimate_prompt_tokens 用 llm.models[model].context_window（默认 128000）估算剩余输出空间。
- **级联依赖功能单元（Cascade Links）**：FEAT-EMB-02（Embedding API 通道）、FEAT-AG-03（消费方）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 公共基础设施（common/token_counter.py）：`src/codebase_rag/common/token_counter.py`

### [FEAT-COMMON-03] 结构化日志
- **业务目标**：structlog 统一日志封装。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [格式]：统一 structlog；事件名 snake_case。
  2. [配置]：setup_logging 按 system.log 配置（level/format/file/error_file/retention_days/max_bytes/backup_count）。
- **级联依赖功能单元（Cascade Links）**：无（全域消费）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 公共基础设施（common/logging.py）：`src/codebase_rag/common/logging.py`
  - 公共基础设施（common/types.py）：`src/codebase_rag/common/types.py`

### [FEAT-COMMON-04] 配置加载与热重载协调（ConfigManager）
- **业务目标**：YAML + .env → 运行时 AppConfig；热重载时协调各服务 reconciler。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [加载规则]：自动加载 config 同目录 .env（parse_env_file 支持 export 前缀、引号剥离、注释）；递归解析 ${VAR}——overlay（.env 解析结果）优先于 os.environ，两者皆无则抛 ValueError；最后 AppConfig.model_validate。
  2. [路径解析]：优先 CODEBASE_RAG_CONFIG 环境变量，回退 ./config.yaml。
  3. [热重载协议]：reload 持锁 → 从磁盘重载新配置 → 先 detect_immutable_changes → 按 dict 注册顺序逐个执行 reconciler(old,new)，任一失败抛异常中断（**不 swap 配置**）→ 全部成功才替换内存 AppConfig，并记录 last_reconciled_services / last_restart_reasons。
  4. [不可变参数清单]（仅检测并返回 requires_restart，不阻止配置生效）：management_api.server.host / port、mcp.servers 各 server 的 (host,port) 集合、system.data_dir、system.log.level。
  5. [注册顺序]：reconciler 注册顺序即服务构造/初始化顺序（mcp_client 在 __init__ 注册，embedding/agent 在 initialize() 注册等）。
  6. [配置段结构]：AppConfig 顶层 12 段——system、qdrant_storage、memory_qdrant、agent、embedding、event_detection、management_api、git_operations、workflow、mcp、context_engine、mcp_client。
- **级联依赖功能单元（Cascade Links）**：全部服务的 reconcile（FEAT-AG-01、FEAT-EMB-01、FEAT-MCPC-02、FEAT-QS-01 等）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-config-reconcile.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 配置层（config/loader.py）：`src/codebase_rag/config/loader.py` *(同类参考样板：reconciler 链协调)*
  - 配置层（config/schema.py）：`src/codebase_rag/config/schema.py`

### [FEAT-COMMON-05] 服务组装根（__main__）
- **业务目标**：单进程启动全部服务并运行双 HTTP Server。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [构造分层]：Layer 0（无内部依赖）embedding、git_ops、qdrant_storage、memory_qdrant、mcp_client、token_counter、agent → Layer 1（单依赖）event_detection、knowledge_sync_store、knowledge_sync → Layer 2（多依赖）workflow → Layer 3（顶层入口）management_api、mcp_service、context_engine。
  2. [回调接线四处]：event_detection.subscribe(workflow.on_git_event)；memory_qdrant.set_rebuild_handler(workflow.rebuild_memory_instance)；mcp_service.set_move_heal_handler(management_api.heal_moved_project)；knowledge_sync.set_retry_availability_provider(workflow.is_compensation_running)。
  3. [初始化顺序]：Layer 0→1→2→3 逐层 initialize；shutdown 按逆序。
  4. [双 Server]：Management API（FastAPI，/api/v1 + Admin 路由）与 MCP Server（每 config.mcp.servers 条目一个 uvicorn 实例，SSE + Streamable HTTP 双传输同端口）经 asyncio.gather 并发运行。
  5. [入口]：`python -m codebase_rag` 或 `codebase-rag` 脚本；`--config` 显式指定或自动探测。
- **级联依赖功能单元（Cascade Links）**：全部 FEAT（构造与接线目标）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 组装根（src/codebase_rag/__main__.py）：`src/codebase_rag/__main__.py` *(同类参考样板：分层组装根 + setter 破环)*

### [FEAT-COMMON-06] 仓库副本路径规范化（normalize_repo_key）
- **业务目标**：为本地 Git 仓库副本提供唯一规范化身份键 repo_key，是注册表、内存实例、任务队列、事件路由的统一实例键来源。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [规范化链]：`normalize_repo_key(path)` = `normcase(realpath(abspath(normpath(path)))`——依次做分隔符/圆点清理、绝对化、符号链接解析、大小写归一（Windows），同一磁盘位置的多种写法坍缩为同一键。
  2. [双重用途]：repo_key 既是身份键也是可用路径——git 子进程操作直接使用 repo_key，无需二次转换。
  3. [唯一实现]：全项目严禁在其它域自写路径归一逻辑，必须复用本函数。
- **级联依赖功能单元（Cascade Links）**：FEAT-MGMT-01/02/03（注册表键）、FEAT-MQ-01/05（实例键）、FEAT-WF-01/02（任务与重建路由）、FEAT-ED-01/03（事件路由）、FEAT-MCPS-02/03（MCP 调用链）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-repo-copy-identity.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 公共基础设施（common/paths.py）：`src/codebase_rag/common/paths.py`

## 三、域内量化参数速查

| 参数 | 默认值 | 来源 |
| :--- | :--- | :--- |
| KeyPool 冷却时长 | 60s | `RetryConfig.key_cooldown_seconds` |
| KeyPool 熔断阈值 | 3 次连续失败 | `RetryConfig.max_retries_per_key` |
| 上下文窗口缺省 | 128000 | `agent.llm.models.*.context_window` |
| 管理面默认端口 | config.management_api.server（本地部署 .env 常用 18080） | 部署配置 |
| MCP 默认端口 | config.mcp.servers（本地部署 .env 常用 19090） | 部署配置 |
