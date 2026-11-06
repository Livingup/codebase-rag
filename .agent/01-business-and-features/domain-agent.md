# Agent 运行时 agent

## 一、业务域职责边界与域内铁律

- 负责：多 LLM 服务商接入、Key 调度、并发容量选择、Agent 定义注册与执行循环（本地工具与 MCP 工具）、上下文压缩。
- 不负责：业务提示词与业务流程——知识生产与上下文分析的提示词均由使用方注册。
- 域内铁律：一次 agent 多轮执行必须绑定同一 LLM 服务商（启动前按优先级选择有并发容量的服务商并等待空位），不得逐轮更换；压缩失败不得中断主执行流程。

## 二、业务功能单元明细

### [FEAT-AG-01] LLM Provider 生命周期管理
- **业务目标**：多服务商注册表 + KeyPool + 并发信号量的初始化与热更新。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [初始化]：遍历 `llm.providers`；`api_format=="jev"` 的 provider 跳过（System One 决策模型，MCP reranker 专用，不经 agent 运行时）；每个 provider 创建实例（工厂按 api_format 懒加载，支持格式：gemini、openai_chat、openai_responses、anthropic、zhipu、deepseek；每种格式实例均为 llm-sdk 协议客户端的薄适配器，见 `.agent/04-tech-decisions/tech-llm-sdk-adoption.md`）、KeyPool、独立 `Semaphore(max_concurrency)`。
  2. [热更新三向 diff]：删除的 provider 整套移除；新增建全套；已存在则重建实例 + keypool.update_keys（保留健康状态）+ 仅当 max_concurrency 变化才重建信号量。
  3. [探活规则]：429 返回 degraded 且**不动 Key 健康度**；认证错误 mark_unhealthy；超时/其他错误 report_failure。
  4. [状态判定]：未初始化→unavailable；无 Key→not_configured；healthy=0→unavailable；部分不健康→degraded。
  5. [注册校验]：register_agent 校验 provider 存在且 model ∈ provider.models 列表；同名定义覆盖。
- **级联依赖功能单元（Cascade Links）**：FEAT-COMMON-01（KeyPool）、FEAT-COMMON-04（reconciler 注册）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-key-pool.md`、`.agent/04-tech-decisions/tech-llm-sdk-adoption.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/agent/service.py）：`src/codebase_rag/services/agent/service.py`（初始化与 reconcile 段）
  - 服务商适配器（services/agent/providers/base.py）：`src/codebase_rag/services/agent/providers/base.py`
  - 服务商适配器（services/agent/providers/llm_sdk_common.py）：`src/codebase_rag/services/agent/providers/llm_sdk_common.py` *(同类参考样板：llm-sdk 客户端构造、流事件聚合与错误映射)*
  - 服务商适配器（services/agent/providers/<name>.py）：`src/codebase_rag/services/agent/providers/anthropic.py`、`src/codebase_rag/services/agent/providers/deepseek.py`、`src/codebase_rag/services/agent/providers/gemini.py`、`src/codebase_rag/services/agent/providers/openai_chat.py`、`src/codebase_rag/services/agent/providers/openai_responses.py`、`src/codebase_rag/services/agent/providers/zhipu.py`

### [FEAT-AG-02] 并发容量选择与 Provider 绑定（select_provider）
- **业务目标**：agent 启动前按优先级选定有并发容量的服务商。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [选择规则]：while True 轮询，返回第一个信号量未锁满（`sem.locked()==False`）的服务商。
  2. [等待机制]：全部满时 `_slot_freed.clear()` 后阻塞等待，任一槽位释放方 set 该 Event 唤醒重试。
  3. [绑定不变量]：选定的 provider 绑定整个 agent 多轮对话生命周期，不再切换。
- **级联依赖功能单元（Cascade Links）**：无
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/agent/service.py）：`src/codebase_rag/services/agent/service.py`（select_provider 段）

### [FEAT-AG-03] Agent 注册与执行循环（run_agent）
- **业务目标**：通用 Agent 运行时——渲染提示词、合并工具、多轮 LLM 调用与工具执行。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [提示词渲染]：system prompt 做 `{var}` 模板替换。
  2. [工具合并]：本地工具与 MCP 工具合并；按 mcp_servers 白名单 include_tools 过滤；本地工具遮蔽 MCP 同名工具；MCP 发现失败仅告警跳过。
  3. [执行循环]：①超时检查（elapsed > timeout 抛 AgentTimeoutError，timeout 取 agent_def.timeout 或 execution.default_timeout）；②压缩检查（FEAT-AG-05）；③LLM 调用（FEAT-AG-04）；④finish_reason 为 stop/length → 终止（length 记截断警告）；tool_calls → 逐个执行工具并回填 tool 消息（工具抛 AgentTaskComplete 提前优雅退出）；其他 finish_reason 视为最终输出。
  4. [批量执行]：batch_run_agent 并发批量执行，单输入异常不互相影响。
  5. [trace]：支持 trace 文件日志（execution.trace_log_dir），初始化失败降级为无 trace。
- **级联依赖功能单元（Cascade Links）**：FEAT-MCPC-01（list_tools / call_tool）、FEAT-COMMON-01（KeyPool）、FEAT-AG-04。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/agent/service.py）：`src/codebase_rag/services/agent/service.py`（run_agent 段）*(同类参考样板：通用 agent 执行循环)*
  - 域数据模型（services/agent/models.py）：`src/codebase_rag/services/agent/models.py`
  - 域内辅助实现（services/agent/trace_logger.py）：`src/codebase_rag/services/agent/trace_logger.py`

### [FEAT-AG-04] LLM 调用双层重试与 429 退避
- **业务目标**：KeyPool 轮换 + 同 Key 重试的统一 LLM 调用容错。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [双层结构]：外层轮换 keypool.total_keys 次，内层同 Key 重试 max_retries（=retry.max_retries_per_key，默认 3）。
  2. [错误分支]：LLMRateLimitError → **不 report_failure、不消耗 attempt**（不熔断），rate_limit_count 超 max_rate_limit_retries（-1=无限，受任务超时兜底）则换 Key，退避 delay=min(base_delay×backoff^min(count,6), 60)（封顶 60s）；LLMAuthError → 立即 mark_unhealthy 换 Key 不重试；LLMAPIError → report_failure + 指数退避并计 attempt；未知异常 → report_failure 换 Key。
  3. [信号量纪律]：每次请求 provider_sem.acquire()，finally 释放并 set(_slot_freed) 唤醒 select_provider 等待者。
  4. [模型参数解析]：模型 ref 解析 alias / tool_choice / thinking(JSON) / custom_headers；max_output_tokens 取 llm.models 元数据。
- **级联依赖功能单元（Cascade Links）**：FEAT-COMMON-01（KeyPool）、FEAT-AG-02（信号量与事件）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-key-pool.md`、`.agent/04-tech-decisions/tech-llm-sdk-adoption.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/agent/service.py）：`src/codebase_rag/services/agent/service.py`（_llm_call_with_retry 段）
  - 域异常体系（services/agent/errors.py）：`src/codebase_rag/services/agent/errors.py`
  - 服务商适配器（services/agent/providers/llm_sdk_common.py）：`src/codebase_rag/services/agent/providers/llm_sdk_common.py`（llm-sdk 错误 kind → LLMAuthError/LLMRateLimitError/LLMAPIError 映射）

### [FEAT-AG-05] 上下文压缩
- **业务目标**：会话 token 接近模型上下文窗口时，由独立压缩链路提炼历史。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [触发条件]：agent_def.enable_context_compression 且 last_prompt_tokens ≥ llm.models[model].compression_threshold（首轮无 token 数据跳过）。
  2. [压缩协议]：定位最后一条 assistant 消息，将 [COMPRESSION_SYSTEM_PROMPT] + messages[1:last_asst_idx] 交给压缩 agent；压缩协议要求 8 分区输出（任务需求/背景/关键数据值/已完成/TODO/经验/注意事项/当前状态），并要求识别第一条 user 是否已是压缩结果以做增量合并。
  3. [结果采纳]：LLM 调用 compress_and_replace_history 工具且 compressed_context 非空 → 原地重建消息为 [system, user(压缩结果), 最后一条 assistant 起的后续消息]；调错工具/空结果 → 追加提示词重试；finish_reason=stop 且文本 >200 字符 → 文本兜底采用。
  4. [候选链与轮次]：候选链为 context_compression.providers 按 priority 排序（无则回退单 provider/model 配置），每候选最多 3 轮（max_comp_rounds=3）。
  5. [失败不中断]：全部候选耗尽返回 False，run_agent 不检查返回值——压缩失败不中断主流程，以原消息继续。
- **级联依赖功能单元（Cascade Links）**：FEAT-AG-04（压缩链路复用 LLM 调用）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域内辅助实现（services/agent/context_compression.py）：`src/codebase_rag/services/agent/context_compression.py`
  - 域服务门面（services/agent/service.py）：`src/codebase_rag/services/agent/service.py`（_compress_context 段）

## 三、域内量化参数速查

| 参数 | 默认值 | 配置键 |
| :--- | :--- | :--- |
| 同 Key 重试次数 | 3 | `agent.retry.max_retries_per_key` |
| Key 冷却时长 | 60s | `agent.retry.key_cooldown_seconds` |
| 退避基础延迟 / 倍数 / 封顶 | 1.0s / 2.0 / 60s | `agent.retry.base_delay / backoff`（封顶为代码常量） |
| 频控重试上限 | -1（无限，受任务超时兜底） | `agent.retry.max_rate_limit_retries` |
| 执行默认超时 | 见配置 execution.default_timeout | `agent.execution.default_timeout` |
| 上下文窗口默认值 | 128000 | `agent.llm.models.*.context_window` 缺省 |
