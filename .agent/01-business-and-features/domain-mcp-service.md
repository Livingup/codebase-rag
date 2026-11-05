# MCP 服务 mcp_service

## 一、业务域职责边界与域内铁律

- 负责：将登记、忽略规则管理、知识读写和多模态检索结果适配为外部编程客户端可理解、可据以行动的 MCP 工具返回；两阶段检索编排；git_repo_path 契约校验；仓库移动自愈信号上报。
- 不负责：项目登记存储（→ management_api）；知识持久化与同步任务（→ knowledge_sync / qdrant_storage）；Git 工作流队列（→ workflow）；Git 对象读取和版本计算（→ git_operations）；内存索引生命周期（→ memory_qdrant）。
- 域内铁律（MCP 语义准确性七约束）：
  1. MCP 返回必须准确表达操作能够证明的结果；不得以无活跃内存实例证明未索引，或以活跃实例证明可检索。
  2. MCP 返回必须保留客户端能据以纠正的参数、路径、文件版本及内容超限信息；不得将真实读取失败表述为没有知识。
  3. 文件知识提交仅限存在明确的处理承接依据时确认已受理；已有版本未覆盖提交文字时必须准确说明。
  4. 注册表保存失败必须进入注册失败响应；注册失败响应不得宣称已完整回滚。
  5. MCP 展示不得额外触发索引加载、修改搜索排序或改变注册授权边界。
  6. MCP 返回可保留正常报告、知识说明正文、实际返回数量及用于核对对象的标识和版本；不得机械删除所有数字。
  7. `git_repo_path` 不是 Git 仓库根目录时必须返回参数校验错误；不得上溯推测仓库根目录或静默纠正路径。

## 二、业务功能单元明细

### [FEAT-MCPS-01] 多查询分组语义搜索（MCP 工具 codebase_search）
- **业务目标**：一次调用并发执行多条自然语言查询 × 多仓库的语义搜索，结果按"查询→仓库"两层分组返回，并对命中的图片类型知识点附带返回 MCP `ImageContent` 多模态内容。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [输入契约]：必填 `queries`（数组）；每条 query 必填 `git_repo_paths`（仓库根绝对路径数组）与 `query`；可选 `top_k`（缺省取 10，统一 `min(top_k, 50)` 截断）；缺参抛 `ValueError("Missing required parameter: 'queries'.")` 等。
  2. [并发与超时]：全部 `(query×repo)` 组合经 `asyncio.gather(return_exceptions=True)` 并发；IF 整体超过 120s（硬编码）THEN 所有未完成任务按 `TimeoutError("Search timed out after 120s")` 记为该仓库失败。
  3. [单项失败隔离]：单仓库异常写入该组 `error`（渲染"⚠ 查询失败"），不阻塞其他仓库。
  4. [空结果语义]：IF 总结果数与错误数均为 0 THEN 返回"本次检索未返回结果……这不表示源码中不存在相关实现"。
  5. [多模态图片返回]：当命中的 `SearchResultPoint.file_type == "image"` 时，除在 `TextContent` 中输出计算出的文件绝对路径（标注 `[image]`）与 `business_description` 外，自动读取对应仓库图片并追加 `ImageContent(type="image", data=base64, mimeType="image/png"|"image/jpeg")` 返回给客户端。
- **级联依赖功能单元（Cascade Links）**：FEAT-MCPS-02（单仓检索引擎）、FEAT-MCPS-03（project_id 解析）、FEAT-GIT-03（读取仓库图片字节）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - MCP 工具北向适配（services/mcp_service/server.py）：`src/codebase_rag/services/mcp_service/server.py`（codebase_search 工具定义与 handler）
  - 域服务门面（services/mcp_service/service.py）：`src/codebase_rag/services/mcp_service/service.py`（search 与 read_repo_image_base64 方法）

### [FEAT-MCPS-02] 两阶段检索引擎（向量召回 + 自愈重试 + LLM 重排）
- **业务目标**：先稠密向量召回粗筛，再由 LLM/Jev 重排精选 top_k。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [Stage 1 召回]：query 经 `embedding.embed([query], is_query=True)` 向量化（`gemini-embedding-2` `RETRIEVAL_QUERY`）→ `hybrid_search(repo_key, top_k=recall_limit=400)`——`repo_key` 为调用方仓库根的规范化键（`project_id` 仍从 `.git` 缓存解析用于分区与自愈信号）。
  2. [无实例自愈重试]：IF 抛 `NoActiveInstanceError` THEN 同步自愈重试一次——调移动自愈 handler；自愈失败或重试仍抛错 THEN 转 `ProjectNotIndexedError`（"no active index and lazy rebuild failed. Ensure the project is registered..."）；IF 召回为空 THEN 返回 `[]`。
  3. [跳过重排]：IF 召回数 ≤ `top_k` THEN 跳过重排直接按向量分返回（`reason="candidates_leq_top_k"`），保留 `file_type`。
  4. [Stage 2 重排]：`pick_n = min(top_k+5, 候选数)`；重排器基于候选点的 `business_description` 按 `providers` priority 顺序迭代：`api_format=="jev"` 走 `JevRerankerClient`，否则走 Agent 工具调用式重排（`submit_scores`/`end_task` 工具，分数 clamp 到 `[0,1]`，同一 id 取最高分）；单 provider 失败降级下一个；全部耗尽抛 `RerankerError`。
  5. [超时回退]：重排经 `asyncio.wait_for`（默认 60s）；超时或失败回退未重排的 `top_k`。
  6. [路径规范化]：结果 `file_path` 以 `os.path.join(project_path, rel)` 规范化为绝对路径，并保留 `file_type`。
- **级联依赖功能单元（Cascade Links）**：FEAT-EMB-02（查询向量化）、FEAT-MQ-02（稠密向量检索）、FEAT-MQ-05（懒重建）、FEAT-AG-03（Agent 式重排）、FEAT-MGMT-02（自愈 handler）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/mcp_service/service.py）：`src/codebase_rag/services/mcp_service/service.py`（两阶段检索段）*(同类参考样板：provider 降级链)*
  - 域内辅助实现（services/mcp_service/jev_client.py）：`src/codebase_rag/services/mcp_service/jev_client.py`（Jev 重排序客户端：payload 构造、KeyPool 轮换、并发控制、超时重试）

### [FEAT-MCPS-03] 仓库根契约校验与 project_id 读缓存
- **业务目标**：MCP 读路径快速解析 project_id，同时强制 git_repo_path 必须是仓库根。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [根校验]：IF 路径下不存在 `.git`（目录或 worktree 文件均可）THEN 抛 `InvalidGitRepoPathError`（"git_repo_path 必须为 .git 所在父目录……服务端不会自动上溯推测仓库根"）——**绝不从子目录上溯猜根**。
  2. [登记校验]：IF `.git/codebase-rag/project_id` 文件缺失或为空 THEN 抛 `ProjectNotRegisteredError`（需先经管理 API 注册）。
  3. [自愈信号]：解析成功后 fire-and-forget 调度后台移动自愈（不阻塞调用）。
- **级联依赖功能单元（Cascade Links）**：FEAT-MGMT-02（自愈 handler，经 `set_move_heal_handler` 接线）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-moved-repo-heal.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/mcp_service/service.py）：`src/codebase_rag/services/mcp_service/service.py`（project_id 解析段）
  - 域数据模型（services/mcp_service/models.py）：`src/codebase_rag/services/mcp_service/models.py`

### [FEAT-MCPS-04] 已提交知识查询（MCP 工具 get_file_knowledge）
- **业务目标**：查询指定文件在当前 HEAD 版本下已提交的可读知识说明。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [输入契约]：必填 `git_repo_path`、`file_paths`（绝对路径数组，服务端动态算相对路径）。
  2. [严格 Git 读取]：取 HEAD commit；每文件规范化为 POSIX 相对路径后并发查询：`get_blob_hash(strict=True)` 为 None（文件不在 HEAD）THEN 返回空结果；否则算 `file_version_id` 后查 Qdrant `get_file_points`，仅收集非空 `business_description`。
  3. [错误区分]：严格模式把 Git 读取错误与"正常无文件"区分开（仅文件知识读取入口启用严格模式，其他 Git 读取调用保留兼容行为）；单文件异常捕获为该文件 `error`（"⚠ 查询失败"），不阻塞其他文件。
  4. [空结果表达]：空 → "返回 0 条知识说明……本次未返回该文件当前已提交版本的可读知识说明"。
- **级联依赖功能单元（Cascade Links）**：FEAT-GIT-03（get_head_info / get_blob_hash strict）、FEAT-QS-03（get_file_points）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - MCP 工具北向适配（services/mcp_service/server.py）：`src/codebase_rag/services/mcp_service/server.py`（get_file_knowledge 工具定义与 handler）
  - 域服务门面（services/mcp_service/service.py）：`src/codebase_rag/services/mcp_service/service.py`（文件知识读取段）

### [FEAT-MCPS-05] 知识提交（MCP 工具 add_file_knowledge）
- **业务目标**：受理客户端提交的文件版本知识说明（支持文本文件与 `.png`/`.jpg`/`.jpeg` 图片文件）。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [输入契约]：必填 `git_repo_path`、`file_path`、`blob_hash`（工作区内容的 Git blob SHA-1）、`knowledge_descriptions`（非空字符串数组，逐条校验非空）；工具描述明确声明仅支持文本文件与 `.png` / `.jpg` / `.jpeg` 图片文件（单张图片 ≤ 20MB），无需传入 `file_type`，由服务端自动识别。
  2. [忽略规则与模态校验]：先校验 `is_file_ignored`（命中全局 `exclude_patterns` 或 `.git/codebase-rag/ignore` 则拒绝并提示使用 `manage_project_ignore`）；复算工作区 `blob_hash`；再通过 `get_workdir_file_bytes` + `detect_file_modality` 自动识别 `file_type`（`"text"` 或 `"image"`，若为 `"unsupported_binary"` 则报错拒绝）。
  3. [8192 Token 门禁与多模态嵌入]：逐条校验 `count_tokens(desc) + (258 if file_type == "image" else 0) <= point_token_limit (8192)`；当 `file_type == "image"` 时，对每条 `business_description` 构造 `MultimodalEmbedInput(text=desc, image_bytes=raw_bytes, mime_type=mime_type)`，将「文字描述 + 原始图片」一起送入 `gemini-embedding-2` 生成多模态向量，并在 `PointData` 中记录 `file_type="image"`。
  4. [受理 outcome 判定]：仅 `skipped_existing` → `unchanged`（表达为"该文件版本已有知识，本次未覆盖已有内容"）；仅 `published` → `accepted`（"知识提交已受理……无需重复提交；不代表立即可检索"）；`retry_saved` 且 `can_retry_pending()` → 也算 `accepted`；`cancelled_stale` → `McpServiceError`"该文件版本已被后续提交替代……"；其余 → "服务未能确认可继续处理本次知识，请稍后重试相同文件版本；无需因此重建索引"。
  5. [版本一致性]：返回的 `file_version_id` 与期望不一致 → `McpServiceError`；展示层失败走 `isError=True`。
- **级联依赖功能单元（Cascade Links）**：FEAT-GIT-02（is_file_ignored / detect_file_modality）、FEAT-GIT-03（get_workdir_blob_hash / get_workdir_file_bytes）、FEAT-EMB-02（count_tokens / embed）、FEAT-KS-03（try_skip_existing_file_version）、FEAT-KS-01（submit_points）、FEAT-KS-02（can_retry_pending）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-knowledge-outbox.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - MCP 工具北向适配（services/mcp_service/server.py）：`src/codebase_rag/services/mcp_service/server.py`（add_file_knowledge 工具定义与 handler）
  - 域服务门面（services/mcp_service/service.py）：`src/codebase_rag/services/mcp_service/service.py`（知识提交段）

### [FEAT-MCPS-06] 登记与忽略规则类北向工具（project_status / list_projects / register_project / manage_project_ignore / codebase_context_analysis）
- **业务目标**：向客户端暴露登记状态查询、项目登记、仓库忽略规则管理与上下文分析入口。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [project_status]：必填 `git_repo_path`；语义为"只报告登记事实，不保证可检索"；已登记 + 初始化任务运行中（`current_task.task_type=="initialization"`）附加"初始化进行中"；未登记则明确说明。
  2. [list_projects]：无参，列出 `display_name` + 路径；空 → "未注册任何 Git 仓库。"
  3. [register_project]：必填 `git_repo_path`，可选 `display_name`；工具描述内嵌强制调用边界（仅用户明确要求注册时才可调用，禁止私自/默认调用）；成功返回"项目已登记，初始化已安排……登记完成不代表检索已就绪"；`ManagementApiError` 转 `ValueError` 抛出。
  4. [manage_project_ignore]：必填 `git_repo_path`、`action`（`get` | `add` | `remove` | `set`），可选 `patterns`（字符串列表）或 `content`（完整规则文本）；读写 `.git/codebase-rag/ignore`（完全遵循 `.gitignore` 语法规则，仅控制 codebase-rag 知识点跟踪过滤）。
  5. [codebase_context_analysis]：委托 `context_engine`（FEAT-CE-01）执行，输入任务描述与目标仓库。
  6. [MCP Server 形态]：同端口双传输并存——SSE（`GET /sse` + `POST /messages`，legacy）与 Streamable HTTP（`POST|GET|DELETE /mcp`，stateless）。
- **级联依赖功能单元（Cascade Links）**：FEAT-MGMT-04（登记状态查询）、FEAT-MGMT-01（注册主链）、FEAT-GIT-02（`.git/codebase-rag/ignore` 读写）、FEAT-CE-01（分析委托）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - MCP 工具北向适配（services/mcp_service/server.py）：`src/codebase_rag/services/mcp_service/server.py`（登记与忽略规则类工具定义与分发 handle_call_tool）*(同类参考样板：工具 schema 内嵌调用边界)*
  - 域服务门面（services/mcp_service/service.py）：`src/codebase_rag/services/mcp_service/service.py`（manage_project_ignore 段）

## 三、域内量化参数速查

| 参数 | 默认值 | 配置键 |
| :--- | :--- | :--- |
| 检索默认 top_k / 上限 | 10 / 50 | `mcp`（search 段，与 memory_qdrant 共用） |
| 单条知识点 token 上限 | 8192 | `mcp.knowledge_submission.point_token_limit` |
| 召回上限 recall_limit | 400 | `mcp.reranker.recall_limit` |
| 重排超时 | 60s | `mcp.reranker.timeout` |
| 多仓搜索总超时 | 120s | server.py 硬编码 |
