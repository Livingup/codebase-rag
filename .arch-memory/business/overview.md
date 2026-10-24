# 业务总览

定位：CodebaseRAG 是面向编程 Agent 的 Git 感知代码知识库，从已登记 Git 仓库生产文件版本级业务知识，供语义检索与深度上下文分析使用。

## 领域清单

| 域 | 类型 | 职责 | 文件 |
|---|---|---|---|
| workflow | 核心 | Git 事件驱动的知识生产、项目初始化、内存视图编排与失败补偿 | domains/workflow.md |
| event_detection | 支撑 | Git 变化的采集、HEAD 比对分类与事件发布 | domains/event_detection.md |
| git_operations | 支撑 | Git 访问、变更识别与文件版本身份 | domains/git_operations.md |
| knowledge_sync | 支撑 | 文件知识提交的受理、任务外发箱与重试入口 | domains/knowledge_sync.md |
| qdrant_storage | 支撑 | 文件版本知识的持久化存取与完整性判定 | domains/qdrant_storage.md |
| memory_qdrant | 支撑 | 内存检索实例的生命周期与缓存规则 | domains/memory_qdrant.md |
| mcp_service | 支撑 | 面向外部编程客户端的 MCP 工具输入约束、应用调用编排与结果表达 | domains/mcp_service.md |
| context_engine | 支撑 | 代码库深度上下文分析 agent 的执行 | domains/context_engine.md |
| management_api | 支撑 | 项目登记、管理检索、运维状态与配置重载的本地管理面 | domains/management_api.md |
| agent | 通用 | 多服务商 LLM 接入与 Agent 执行运行时 | domains/agent.md |
| embedding | 通用 | 稠密/稀疏向量生成与 token 计数 | domains/embedding.md |
| mcp_client | 通用 | 外部 MCP Server 的工具发现与调用 | domains/mcp_client.md |

## 域间关系

- event_detection → workflow：事件，GitEvent 发布给编排层。
- workflow → git_operations / agent / embedding / knowledge_sync / qdrant_storage / memory_qdrant：同步，编排知识生产与内存视图更新。
- mcp_service → memory_qdrant：同步，代码知识检索使用项目内存索引；实例生命周期由 memory_qdrant 管理。
- mcp_service → embedding / git_operations / knowledge_sync / management_api / context_engine：同步，分别用于查询向量化与描述 token 校验、blob 与版本身份、知识提交、登记状态视图、上下文分析委托。
- context_engine → agent / mcp_client：同步，分析 agent 执行与外部工具接入。
- knowledge_sync → qdrant_storage / memory_qdrant：同步，持久化发布与内存视图刷新。
- management_api → event_detection / workflow / memory_qdrant / qdrant_storage / embedding / agent / knowledge_sync：同步，登记监控注册、任务触发与状态聚合。
- event_detection → git_operations：同步，读取仓库 HEAD。

## 主业务链路

- 知识生产：event_detection(事件) → workflow(编排) → git_operations(变更与版本) → agent(业务描述生成) → embedding(向量化) → knowledge_sync(受理) → qdrant_storage(持久化) → memory_qdrant(视图更新)
- 知识检索：mcp_service(两阶段检索) → memory_qdrant(混合召回) → mcp_service(重排序) → 客户端
- 客户端知识提交：mcp_service(add_file_knowledge) → knowledge_sync(受理) → qdrant_storage(持久化) → memory_qdrant(视图刷新)
- 深度上下文分析：mcp_service(codebase_context_analysis) → context_engine(分析) → agent(推理) + mcp_client(外部工具) + 本地源码工具
