# 上下文分析 context_engine

## 一、业务域职责边界与域内铁律

- 负责：接受编程任务描述，驱动分析 agent 多轮搜索、阅读源码，产出带精确源码引用的 Markdown 实现现状报告。
- 不负责：知识库写入与索引维护；报告之外的方案设计与编码（由调用客户端完成）；分析工具的 LLM 协议组装（→ agent）。
- 域内铁律：RAG 种子（codebase_search 召回的候选文件）仅用于定位，实现真相以源码读取为准；agent 未调用 submit_report 就结束时必须返回明确的未完成结果，已访问文件数不得用作分析完成度的证明。

## 二、业务功能单元明细

### [FEAT-CE-01] 深度上下文分析编排（context_analysis）
- **业务目标**：按任务描述自主执行多轮"搜索→阅读→分析"，产出含源码引用的结构化报告（典型 30s-10min，支持多仓库）。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [ripgrep 门禁]：IF ripgrep 不可用 THEN 不跑 agent，直接返回"错误：ripgrep (rg) 未安装，无法执行代码搜索。请安装 ripgrep 后重试。"（提供安装指引，不降级为无搜索分析）。
  2. [候选链降级]：按 providers priority 注册多个候选 agent（context_engine、context_engine_1…，temperature=0.0，工具=4 个本地 schema + 配置的 MCP servers 白名单）；单个 agent 异常（非 AgentTaskComplete）降级下一个候选；全失败 → "⚠️ 分析过程异常：{last_error}"。
  3. [报告提交协议]：submit_report 触发 AgentTaskComplete 结束；IF 正常结束但 final_report 为空（agent 未调用 submit_report）THEN 返回"⚠️ 分析未完成：未生成最终报告，不能据此确认任务结论。"
  4. [懒初始化]：agent 注册由 `_init_lock` 保护，首次调用时完成。
- **级联依赖功能单元（Cascade Links）**：FEAT-AG-03（run_agent）、FEAT-MCPC-01（经 MCP 调 codebase_search 等外部工具）、FEAT-MCPS-06（MCP 工具入口）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/context_engine/service.py）：`src/codebase_rag/services/context_engine/service.py`
  - 域内辅助实现（services/context_engine/prompts.py）：`src/codebase_rag/services/context_engine/prompts.py`（CONTEXT_ENGINE_PROMPT）

### [FEAT-CE-02] 本地工具集与 ripgrep 执行
- **业务目标**：为分析 agent 提供 search_code / read_file / list_directory / submit_report 四个本地工具。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [search_code]：必填 `pattern`，可选 `git_repo_path`（缺省搜全部目标仓库并按仓分段合并）、`file_glob`、`fixed_strings`、`max_results`（默认 30）、`search_filenames`；ripgrep 固定参数 `--max-count 5 --max-columns 200 --context 2 --line-number --heading --color never`；超时（rg_timeout 默认 10s）→"错误：ripgrep 搜索超时……请尝试更精确的搜索模式"；FileNotFoundError →"错误：ripgrep (rg) 未安装。无法执行代码搜索。"；exit 1 → 无匹配；exit>1 → 截断 200 字符错误；输出超 max_results×8 行截断；文件名模式用 `rg --files` + Python 大小写不敏感正则二次过滤（Windows 无管道故分两步）。
  2. [read_file]：支持绝对路径（codebase_search 返回值）与相对路径（逐仓库尝试）；可选 1-based start_line/end_line；超过 max_file_read_lines（默认 500）截断并提示"请指定 start_line/end_line 分段读取"；输出带行号。
  3. [list_directory]：max_depth 默认 2；跳过隐藏目录及 node_modules/__pycache__/.git；<1MB 文本文件附行数，否则标 KB；空路径或 "." 列出全部仓库根。
  4. [submit_report]：必填 `report`，写入 state.final_report 并抛 AgentTaskComplete 终止任务。
  5. [可用性检查]：check_ripgrep_available 执行 `rg --version`（5s 超时）；不可用时初始化记 context_engine_ripgrep_not_found 警告，后续分析被门禁拦截。
- **级联依赖功能单元（Cascade Links）**：无（仅文件系统与 rg 子进程）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域内辅助实现（services/context_engine/tools.py）：`src/codebase_rag/services/context_engine/tools.py` *(同类参考样板：本地工具 schema + rg 子进程封装)*

## 三、域内量化参数速查

| 参数 | 默认值 | 配置键 |
| :--- | :--- | :--- |
| agent 执行超时 | 3600s | `context_engine.default_timeout` |
| 单次 LLM 请求超时 | 1800s | `context_engine.request_timeout` |
| rg 搜索超时 | 10s | `context_engine.rg_timeout` |
| 搜索结果上限 | 30 | 工具参数 max_results |
| 读文件行数上限 | 500 | `context_engine.max_file_read_lines` |
| 目录列举深度 | 2 | 工具参数 max_depth |
