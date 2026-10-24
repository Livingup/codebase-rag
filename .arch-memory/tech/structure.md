# 项目结构地图

## 域映射

- `src/codebase_rag/services/workflow/**` [workflow]
- `src/codebase_rag/services/event_detection/**` [event_detection]
- `src/codebase_rag/services/git_operations/**` [git_operations]
- `src/codebase_rag/services/knowledge_sync/**` [knowledge_sync]
- `src/codebase_rag/services/qdrant_storage/**` [qdrant_storage]
- `src/codebase_rag/services/memory_qdrant/**` [memory_qdrant]
- `src/codebase_rag/services/mcp_service/**` [mcp_service]
- `src/codebase_rag/services/context_engine/**` [context_engine]
- `src/codebase_rag/services/management_api/**` [management_api]
- `src/codebase_rag/services/agent/**` [agent]
- `src/codebase_rag/services/embedding/**` [embedding]
- `src/codebase_rag/services/mcp_client/**` [mcp_client]
- `tests/unit/test_workflow_*.py` [workflow]
- `tests/unit/test_memory_qdrant_concurrency.py` [memory_qdrant]
- `tests/unit/test_mcp_*.py` [mcp_service]
- `tests/unit/test_jev_reranker.py` [mcp_service]
- `tests/unit/test_management_*.py` [management_api]
- `tests/unit/test_knowledge_sync_receipts.py` [knowledge_sync]
- `tests/unit/test_qdrant_write_ack.py` [qdrant_storage]
- `tests/unit/test_git_blob_read_strict.py` [git_operations]
- `tests/unit/test_deepseek_tool_choice.py` [agent]
- `tests/integration/test_agent_providers_smoke.py` [agent]
- `src/codebase_rag/common/**` [公共]
- `src/codebase_rag/config/**` [公共]
- `src/codebase_rag/__main__.py` [公共]
- `tests/**` [公共]
- `ops/windows/**` [公共]
- `scripts/**` [公共]

## 节点标注

- `src/codebase_rag/services/memory_qdrant/service.py` MEMORY_QDRANT-01、MEMORY_QDRANT-02、MEMORY_QDRANT-03、MEMORY_QDRANT-04、MEMORY_QDRANT-05
- `src/codebase_rag/__main__.py` [公共·服务组装与回调接线]
- `src/codebase_rag/services/mcp_service/server.py` [mcp_service·工具响应适配]
- `src/codebase_rag/services/mcp_service/service.py` MCP_SERVICE-03、MCP_SERVICE-07、→ mechanisms/moved-repo-heal
- `src/codebase_rag/services/management_api/service.py` MCP_SERVICE-04、→ mechanisms/moved-repo-heal
- `src/codebase_rag/common/keypool.py` → mechanisms/key-pool
- `src/codebase_rag/config/loader.py` → mechanisms/config-reconcile
- `src/codebase_rag/services/mcp_service/jev_client.py` [mcp_service·Jev 重排序客户端]
- `src/codebase_rag/services/knowledge_sync/store.py` [knowledge_sync·任务外发箱与最新版本索引]
- `src/codebase_rag/services/event_detection/hooks/post-*` [event_detection·hook 脚本模板]
