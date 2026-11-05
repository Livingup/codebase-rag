# 业务域职责边界与全局业务铁律

定位：CodebaseRAG 是面向编程 Agent 的 Git 感知代码知识库，从已登记 Git 仓库生产文件版本级业务知识，供语义检索与深度上下文分析使用。

## 1. 全局业务铁律（跨域生效）

1. **project_id 身份唯一锚定仓库根 commit**：`uuid5(uuid.NAMESPACE_DNS, "codebase-rag:project_id:v1:{root_commit}")`；`.git/codebase-rag/project_id` 文件仅为读缓存，身份不信任缓存文件与调用方声称的路径。
2. **MCP 返回必须准确表达可证明的结果**：不得以无活跃内存实例证明未索引，或以活跃实例证明可检索；不得将真实读取失败表述为"没有知识"；必须保留客户端能据以纠正的参数、路径、文件版本及内容超限信息（详见 `domain-mcp-service.md` 各 FEAT 约束）。
3. **知识完整性以完整知识集为准**：同一 `file_version_id` 下 `chunk 0..chunk_count-1` 全部存在才构成可读版本；缺失任一 chunk 不得被读取或判定为已存在；并存多套完整集取 `knowledge_set_id` 字典序最小。
4. **纯重命名（blob 不变）必须复用原知识**：仅更新路径与版本元数据，不得重新走 LLM 生成与向量化。
5. **持久写入确认不可被附带失败反转**：持久写入或任务承接确认后，附带的内存刷新、任务清理失败不得反转已确认结果（best-effort）。
6. **登记与检索解耦**：登记状态只反映登记与初始化任务事实，明确声明"不保证可检索"；状态机 `registered → initializing → ready`。
7. **单进程 asyncio 内一切锁均为进程内锁**：不引入分布式锁；单飞（single-flight）共享是并发去重的统一手段。

## 2. 业务域清单与职责边界

| 域 | 类型 | 职责 | 域文档 |
|---|---|---|---|
| workflow | 核心 | Git 事件驱动的知识生产编排、项目初始化、内存视图 staging 提升与失败补偿 | domain-workflow.md |
| event_detection | 支撑 | Git hook 注入与巡检、.git watcher 兜底、HEAD 比对分类并发布 GitEvent | domain-event-detection.md |
| git_operations | 支撑 | Git CLI 异步访问、变更解析、file_version_id 身份、.gitignore 风格文件过滤与图片模态识别 | domain-git-operations.md |
| knowledge_sync | 支撑 | 文件版本知识提交受理、JSON 任务外发箱、两级互斥锁、重试入口 | domain-knowledge-sync.md |
| qdrant_storage | 支撑 | Qdrant Server 连接与 collection 管理、批量写入确认、完整知识集读取、project_id 迁移 | domain-qdrant-storage.md |
| memory_qdrant | 支撑 | 内存检索实例生命周期、LRU 缓存、单飞懒重建、稠密向量检索 | domain-memory-qdrant.md |
| mcp_service | 支撑 | 面向外部编程客户端的 MCP 工具（多模态检索/知识读写/忽略规则管理/登记查询/上下文分析委托） | domain-mcp-service.md |
| context_engine | 支撑 | 深度上下文分析 agent 执行（多轮搜索+读码+报告） | domain-context-engine.md |
| management_api | 支撑 | 项目登记/注销/恢复/移动自愈、管理检索、运维状态、配置重载、Admin UI | domain-management-api.md |
| agent | 通用 | 多服务商 LLM 接入、Key 调度、并发容量、Agent 执行运行时（含多模态消息） | domain-agent.md |
| embedding | 通用 | Gemini Embedding-2 稠密多模态向量生成（文本 / 文本+图片）、8192 token 计数与批处理门禁 | domain-embedding.md |
| mcp_client | 通用 | 外部 MCP Server 连接、工具发现与调用 | domain-mcp-client.md |
| common（公共底座） | 公共 | Key 池、Token 计数、结构化日志、配置加载与热重载、组装根 | domain-common.md |

## 3. 域间协作关系（含级联方向）

- event_detection → workflow：事件发布（`subscribe` 回调接线，GitEvent 单向流动）。
- workflow → git_operations / agent / embedding / knowledge_sync / qdrant_storage / memory_qdrant：同步编排知识生产与内存视图更新。
- memory_qdrant → workflow：懒重建回调（`set_rebuild_handler`，搜索无活跃实例时反向触发）。
- mcp_service → memory_qdrant / embedding / git_operations / knowledge_sync：检索、忽略规则管理与知识提交；→ management_api：移动自愈回调（`set_move_heal_handler`）与登记状态查询；→ context_engine：上下文分析委托。
- context_engine → agent / mcp_client：分析 agent 执行与外部工具接入。
- knowledge_sync → qdrant_storage / memory_qdrant：持久化发布与内存视图刷新；→ workflow：重试可用性探测回调（`set_retry_availability_provider`）。
- management_api → event_detection / workflow / memory_qdrant / qdrant_storage / embedding / agent / knowledge_sync：登记监控注册、任务触发与状态聚合。
- agent / embedding / mcp_service → common（KeyPool）：多 Key 轮换熔断统一原语。
- 全部服务 → config（ConfigManager）：配置热重载 reconciler 注册。

## 4. 主业务链路

- 知识生产：event_detection（事件）→ workflow（编排）→ git_operations（变更与版本 + .git/codebase-rag/ignore 过滤 + 模态识别）→ agent（业务描述生成，图片走视觉看图）→ embedding（`gemini-embedding-2` 文本或「描述+图片」多模态向量化）→ knowledge_sync（受理与外发箱）→ qdrant_storage（持久化确认）→ memory_qdrant（staging 加载与提升）。
- 知识检索：mcp_service（两阶段检索）→ memory_qdrant（稠密向量召回）→ mcp_service（LLM/Jev 重排 + 图片知识点附加 `ImageContent`）→ 客户端。
- 客户端知识提交：mcp_service（add_file_knowledge）→ knowledge_sync（受理）→ qdrant_storage（持久化）→ memory_qdrant（视图刷新）。
- 深度上下文分析：mcp_service（codebase_context_analysis）→ context_engine（分析编排）→ agent（推理）+ mcp_client（外部工具）+ ripgrep 本地搜索。
