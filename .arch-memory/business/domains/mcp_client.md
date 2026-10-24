# MCP 客户端 mcp_client

## 边界

- 负责：按配置连接外部 MCP Server（streamable HTTP / SSE / stdio）、工具发现与缓存、按服务器名和工具名执行调用。
- 不负责：对外提供 MCP 服务(→ mcp_service)；Agent 工具协议组装(→ agent)。

## 关键概念

- 部分成功初始化：单个 server 连接或工具发现失败只跳过该 server，不阻塞其余 server 接入。
