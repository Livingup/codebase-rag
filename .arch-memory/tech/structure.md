# 项目结构地图

## 域映射

- `src/codebase_rag/services/memory_qdrant/**` [memory_qdrant]
- `tests/unit/test_memory_qdrant_concurrency.py` [memory_qdrant]
- `src/codebase_rag/services/mcp_service/**` [mcp_service]
- `tests/unit/test_mcp_*.py` [mcp_service]

## 节点标注

- `src/codebase_rag/services/memory_qdrant/service.py` MEMORY_QDRANT-01、MEMORY_QDRANT-02、MEMORY_QDRANT-03、MEMORY_QDRANT-04、MEMORY_QDRANT-05
- `src/codebase_rag/__main__.py` [公共·服务组装与回调接线]
- `src/codebase_rag/services/mcp_service/server.py` [mcp_service·工具响应适配]
- `src/codebase_rag/services/mcp_service/service.py` MCP_SERVICE-03
- `src/codebase_rag/services/management_api/service.py` MCP_SERVICE-04
