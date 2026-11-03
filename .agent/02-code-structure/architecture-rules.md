# 代码架构分层规范（architecture-rules）

本文是全项目分层词汇的唯一来源：`.agent/01-business-and-features/domain-*.md` 的源码清单子标签、`.agent/04-tech-decisions/tech-*.md` 的代码落点描述，都必须复用本文定义的层级名与目录名。

## 1. 架构基线

- 形态：Python 3.11 单进程 asyncio 单体，模块化 `XxxService` 服务架构；不引入 COLA/DDY 四层，不使用线程池执行业务逻辑（Git/rg 子进程除外）。
- 命名：服务门面统一 `XxxService`（如 `WorkflowOrchestratorService`、`MemoryQdrantService`），由各域 `service.py` 承载；数据模型与异常分别由 `models.py`、`errors.py` 承载。
- 组装：`src/codebase_rag/__main__.py` 是唯一组装根——服务按 Layer 0→3 依赖顺序构造，依赖通过构造函数注入，跨服务回调用 setter 在组装根接线。
- 并发：单事件循环；内存实例缓存状态由单一 `asyncio.Lock` 保护；同项目 workflow 任务由 `ProjectTaskQueue` 串行、跨项目并行。
- 错误：各服务使用自己的异常体系（如 `MemoryQdrantError` 子类、`GitFileNotFoundError`），异常逐层包装上抛。
- 日志：统一 `structlog`，事件名 snake_case（如 `services_initializing`）。
- 配置：`ConfigManager`（`config/loader.py`）是运行时配置唯一持有者；各服务通过注册 reconciler 支持热重载。
- 测试：`tests/unit/` 与 `tests/integration/`；异步单测用 `unittest.IsolatedAsyncioTestCase` + `SimpleNamespace` 伪造依赖；pytest `asyncio_mode=auto`。

## 2. 目录职责映射

```text
src/codebase_rag/
├── __main__.py                        # [组装根] 服务构造顺序、回调接线、Management API + MCP Server 启动
├── common/                            # [公共基础设施] 跨服务复用，禁止包含业务分支
│   ├── keypool.py                     #   KeyPool 多 Key 轮换熔断原语
│   ├── logging.py                     #   structlog 封装
│   ├── token_counter.py               #   Token 计数三级回退
│   └── types.py                       #   跨服务公共类型
├── config/                            # [配置层]
│   ├── schema.py                      #   AppConfig（pydantic）12 个顶层配置段
│   └── loader.py                      #   ConfigManager：YAML+.env 加载、${VAR} 解析、热重载 reconciler 协调
└── services/                          # [服务域层] 每个子目录一个业务域
    ├── <domain>/
    │   ├── service.py                 #   [域服务门面] 该域唯一服务类与全部业务分支
    │   ├── models.py                  #   [域数据模型] 领域对象与枚举
    │   ├── errors.py                  #   [域异常体系] 该域异常基类与子类
    │   ├── providers/                 #   [服务商适配器] 仅 agent/embedding 有：外部 LLM/向量服务商协议适配
    │   │   ├── base.py                #     抽象基类（LLMProvider / EmbeddingProvider）
    │   │   ├── <name>.py              #     一个服务商一个文件，只做报文转换，协议由 pythonsdk（llm-sdk）承担
    │   │   └── llm_sdk_common.py      #     [agent 专用] llm-sdk 客户端配置、流事件聚合与错误映射
    │   └── <辅助模块>.py              #   [域内辅助实现] task_queue/store/watcher/file_filter 等实现细节
    ├── agent/                         # 12 个业务域：agent、embedding、event_detection、
    ├── context_engine/                #   git_operations、knowledge_sync、management_api、
    ├── embedding/                     #   mcp_client、mcp_service、memory_qdrant、
    ├── event_detection/               #   qdrant_storage、workflow
    │   └── hooks/                     #   [Git hook 脚本模板] post-commit/merge/checkout/rewrite（无扩展名）
    ├── git_operations/
    ├── knowledge_sync/
    ├── management_api/
    │   ├── routes.py                  #   [REST 路由适配] /api/v1 全部管理端点
    │   └── admin_ui.py                #   [REST 路由适配] 本地 Admin UI 单页（GET / 与 /admin）
    ├── mcp_client/
    ├── mcp_service/
    │   └── server.py                  #   [MCP 工具北向适配] 全部 MCP 工具 schema 定义与分发
    ├── memory_qdrant/
    ├── qdrant_storage/
    └── workflow/
ops/windows/                           # [运维脚本] Windows 服务安装/注册/启停/构建/更新 PowerShell 脚本
tests/unit/ tests/integration/         # [测试]
config.example.yaml .env.example       # [配置模板] 仓库只留模板，真实配置不入 Git
```

## 3. 当你新增某类代码时该放哪

| 要新增的代码 | 放置位置 | 必须同步的动作 |
| :--- | :--- | :--- |
| 新业务域 | `services/<domain>/{service.py, models.py, errors.py, __init__.py}` | `__main__.py` ServiceContainer 按依赖分层实例化并接线；`config/schema.py` 增配置段；同步 `config.example.yaml` 与 `.env.example` |
| 新 LLM 服务商 | `services/agent/providers/<name>.py`（实现 `LLMProvider`） | 协议必须来自 `pythonsdk`（llm-sdk）；`agent/service.py` 工厂 `_SUPPORTED_FORMATS` 注册 api_format；缺失协议先在 llm-sdk 补齐 |
| 新向量服务商 | `services/embedding/providers/<name>.py`（实现 EmbeddingProvider 抽象） | 协议必须来自 `pythonsdk`；`embedding/service.py` 工厂注册；llm-sdk 未支持时先在 SDK 补齐客户端 |
| 新 MCP 工具 | `mcp_service/server.py` 定义工具 schema + `service.py` 实现 handler + `handle_call_tool` 分发 | 工具描述必须内嵌调用边界与参数约束语义 |
| 新 REST 管理端点 | `management_api/routes.py` | 错误体统一 `ErrorResponse{error:{code,message}}` |
| 新配置字段 | `config/schema.py` 对应段 | 同步 `config.example.yaml`、`.env.example` 与相关 reconciler |
| 新 Git hook 类型 | `event_detection/hooks/<hook-name>` 模板 + `MANAGED_HOOKS` 列表 | 模板必须含 `# Codebase RAG Event Hook` 托管标记 |
| 新运维脚本 | `ops/windows/*.ps1` | 公共函数放 `ops/windows/common.ps1` |
| 新异步后台循环 | 所属域 `service.py` 内 `asyncio.create_task` + shutdown 取消 | 参考补偿循环（workflow）与空闲清扫（memory_qdrant） |

## 4. 绝对禁止的跨层/跨模块调用行为

1. **禁止服务内部自行实例化其他域服务**：跨域服务实例一律由组装根构造注入（`__main__.py` ServiceContainer）。
2. **跨域 import 仅限类型与异常**：允许 import 其他域的 `service.py` 类名（仅做类型注解）、`models.py`、`errors.py`；禁止调用其他域服务的内部私有方法（`_` 前缀）。
3. **禁止出现循环构造依赖**：需要反向调用时必须用 setter 回调在组装根接线——现有四个破环回调：`event_detection.subscribe(workflow.on_git_event)`、`memory_qdrant.set_rebuild_handler(workflow.rebuild_memory_instance)`、`mcp_service.set_move_heal_handler(management_api.heal_moved_project)`、`knowledge_sync.set_retry_availability_provider(workflow.is_compensation_running)`。
4. **禁止绕过 ConfigManager 读配置**：服务不得自行读 YAML/环境变量；热更新必须注册 reconciler（`register_reconciler`），不得自行 watch 文件。
5. **禁止在 `common/` 写业务分支**：common 只放与业务无关的基础设施。
6. **禁止多进程/线程执行业务逻辑**：单进程 asyncio 是既定约束；阻塞调用必须 `asyncio.to_thread` 或子进程（参考 Git 命令执行器）。
7. **禁止服务直接写其他域的数据目录**：每个域只写自己的存储（如 knowledge_sync 只写 `data_dir/knowledge_sync/`，workflow 只写 `retry_queue_*.json` 与 `dead_letter_*.json`）。
8. **禁止把真实 `.env`、`config.yaml`、密钥提交入 Git**：仓库只保留 `*.example` 模板。
9. **禁止在 `providers/` 内直接使用第三方 LLM/向量 SDK 或裸 HTTP 客户端**：`openai` / `anthropic` / `google-genai` / `httpx` 不得用于模型与向量请求；协议、SSE 解析、重试与错误分类统一由 `pythonsdk`（llm-sdk）承担（见 `.agent/04-tech-decisions/tech-llm-sdk-adoption.md`）。

## 5. 本项目无 UI 现状文档的说明

本项目是纯后端服务；唯一界面是 `management_api/admin_ui.py` 内嵌的单页本地状态页（无设计稿/切图体系），其现状记录在 `.agent/01-business-and-features/domain-management-api.md` 的 `FEAT-MGMT-05`，故不建立 `.agent/03-ui-current-state/` 目录。
