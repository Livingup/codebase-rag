# MCP 客户端 mcp_client

## 一、业务域职责边界与域内铁律

- 负责：按配置连接外部 MCP Server（streamable HTTP / SSE / stdio）、工具发现与缓存、按服务器名和工具名执行调用。
- 不负责：对外提供 MCP 服务（→ mcp_service）；Agent 工具协议组装（→ agent）。
- 域内铁律：初始化采用部分成功策略——单个 server 连接或工具发现失败只跳过该 server，不阻塞其余 server 接入。

## 二、业务功能单元明细

### [FEAT-MCPC-01] 三种 Transport 连接与工具调用
- **业务目标**：标准 MCP 协议接入外部 Server。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [transport 分发]：_open_transport 按配置分发——stdio（须有 command，StdioServerParameters(command,args,env)）、sse（须有 url）、streamable_http（默认，须有 url）；缺字段抛 ValueError。
  2. [工具发现]：discover_tools 新开 transport + ClientSession initialize 后 list_tools 并缓存。
  3. [调用方式]：call_tool 每次新开连接执行并拼接 text 块返回。
  4. [未注册拦截]：调用不存在的 server 抛 ServerNotFoundError。
- **级联依赖功能单元（Cascade Links）**：FEAT-AG-03（工具合并消费方）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/mcp_client/service.py）：`src/codebase_rag/services/mcp_client/service.py`
  - 域数据模型（services/mcp_client/models.py）：`src/codebase_rag/services/mcp_client/models.py`

### [FEAT-MCPC-02] 懒初始化与热更新差量重建
- **业务目标**：首次使用时连接全部 MCP Server；配置热更新只重建变更项。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [懒初始化]：list_tools / call_tool 首次调用 _ensure_initialized（双检锁）。
  2. [部分成功]：遍历 mcp_client.servers 逐个 discover，单 server 失败仅记 warning 跳过，成功者保留；即使全部失败 _initialized 仍置 True 不阻断流程。
  3. [差量重建]：reconcile diff 新旧 servers——删除的移除；仅"新增或配置对象不等（old != new）"的 server 重新 discover，**未变更的 server 不重建**（保留工具缓存）；重建失败仅 warning 不回滚。
- **级联依赖功能单元（Cascade Links）**：FEAT-COMMON-04（reconciler 注册）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-config-reconcile.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/mcp_client/service.py）：`src/codebase_rag/services/mcp_client/service.py`（初始化与 reconcile 段）

## 三、域内量化参数速查

| 参数 | 默认值 | 配置键 |
| :--- | :--- | :--- |
| servers 清单 | 空（按配置列举） | `mcp_client.servers.*`（type: stdio/sse/streamable_http） |
