# 业务总览

定位：CodebaseRAG 面向代码库检索，内存检索实例支撑按项目访问已索引的知识。

## 领域清单

| 域 | 类型 | 职责 | 文件 |
|---|---|---|---|
| memory_qdrant | 支撑 | 内存检索实例的生命周期与缓存规则 | domains/memory_qdrant.md |
| mcp_service | 支撑 | 面向外部编程客户端的 MCP 工具输入约束、应用调用编排与结果表达 | domains/mcp_service.md |

## 域间关系

- mcp_service → memory_qdrant：同步，代码知识检索使用项目内存索引；实例生命周期由 memory_qdrant 管理。
