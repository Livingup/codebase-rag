# LLM / Embedding 协议统一委托 llm-sdk（multi-protocol-llm-client）

## 1. 启用依据与背景坑点

- Agent 域原有 6 个 provider（openai_chat / openai_responses / anthropic / gemini / deepseek / zhipu）各自直接调用 `openai` / `anthropic` / `google-genai` 官方 SDK，Embedding 域原有 2 个 provider（gemini / dashscope）直接用 `httpx` 手写 JSON 请求与响应解析。
- 由此产生 4 类重复实现与漂移风险：
  1. 错误分类各自实现（鉴权、429、超时、协议错误的判定在 Agent 与 Embedding 两套代码里不一致）。
  2. SSE 流式解析、tool_call 增量拼装、usage 归集在每个 provider 各写一遍。
  3. DashScope `text-embedding-v4` 的 `output_type=sparse` 端点无第三方 SDK 覆盖，只能手写。
  4. 依赖体积大且版本相互牵制（openai + anthropic + google-genai 三套 SDK 同时存在）。
- 结论：统一改为委托自研 `llm-sdk`（PyPI 包名 `multi-protocol-llm-client`，import 名 `pythonsdk`，零第三方依赖），并由 llm-sdk 补齐 DashScope sparse 客户端能力。

## 2. 选定的技术方案与强制契约

- **依赖注入位置**：llm-sdk 以本地 wheel 方式随仓接入（`wheels/multi_protocol_llm_client-*.whl`）。`pyproject.toml` 声明 `multi-protocol-llm-client>=1.0.0`，不声明 openai / anthropic / google-genai；`ops/windows/build-windows-package.ps1`、`start-source-takeover.ps1`、`install-local-service.ps1` 默认优先从仓库内 `wheels/` 目录（未命中时回退 `../llm-sdk/pythonsdk/dist` 或显式 `-SdkWheelDir`）为 `pip install` 与 `pip download` 追加 `--find-links`，使源码直装与发布包 `wheels/` 均能离线解析 SDK wheel。
- **Agent provider 契约为纯适配器**：`services/agent/providers/<name>.py` 只做「codebase-rag Message/ToolSchema → 协议报文」与「llm-sdk 流事件 → `LLMResponse`」两件事。
  - 协议客户端：`openai_chat`/`deepseek`/`zhipu` → `OpenAIChatCompletionsClient`；`openai_responses` → `OpenAIResponsesClient`；`anthropic` → `ClaudeMessagesClient`；`gemini` → `GeminiStreamClient`。
  - 共享桥接：`services/agent/providers/llm_sdk_common.py` 提供 `make_client_config`、`make_request_options`、`collect_stream`、错误映射 `error_from_sdk`。
  - 事件聚合映射：`text_delta`→`content`、`thinking_delta`→`thinking_content`、`tool_call_stop`→`ToolCall`、`response_complete`→`usage`/`finish_reason`；`forced_tool_name` 命中时落 `structured_output`。
  - 错误映射：`AUTH_ERROR`→`LLMAuthError`；`RATE_LIMIT`/`OVERLOADED`→`LLMRateLimitError`；`ABORTED`→`asyncio.CancelledError`（恢复任务取消语义）；其余→`LLMAPIError`。
  - base_url 版本段规则：OpenAI 系补 `/v1`（deepseek 原样、zhipu 补 `/api/paas/v4`）、Anthropic 补 `/v1`、Gemini 补 `/v1beta`；已带同版本段则不重复追加。
  - token 计数：OpenAI 系用 tiktoken；Gemini 用 `GeminiEmbeddingClient.count_tokens`；Anthropic 用 `ClaudeCountTokensClient`（`/v1/messages/count_tokens`）。
- **Embedding provider 契约**：`gemini.py`（`gemini-embedding-2`）委托 `GeminiEmbeddingClient.batch_embed_contents`（检索时 `taskType=RETRIEVAL_QUERY`、索引时 `taskType=RETRIEVAL_DOCUMENT`、支持 `MultimodalEmbedInput` 在单点 `parts` 中同时携带文本与 `inlineData` 图片 base64、子维度 < 3072 时自动 `l2_normalize`）。
- **重试归属不变**：llm-sdk 客户端一律 `RetryPolicy(max_attempts=1)` 关闭内置重试；Key 轮换、同 Key 重试、429 退避、熔断仍由 `agent/service.py` 与 `embedding/service.py` 承担（FEAT-AG-04 / FEAT-EMB-02）。
- **健康校验语义**：Embedding 的 `health_check` 由 SDK 调用真实接口（Gemini `countTokens`）实现；Agent provider 不再提供 `health_check`，实时探活统一走 `probe_stream`。

## 3. 核心不变量与禁止项

- 禁止在 `services/agent/providers/*` 与 `services/embedding/providers/*` 内直接 import `openai` / `anthropic` / `google-genai` 或直接用 `httpx` 发起 LLM/向量请求；协议细节一律经 `pythonsdk`。
- 禁止在 provider 内实现重试、退避、Key 选择；这些属于 service 层，provider 只做单次调用适配。
- 禁止修改 `collect_stream` 的错误映射分支绕过 `LLMAuthError` / `LLMRateLimitError` / `CancelledError` 三者的既有语义（service 层重试与 Key 熔断依赖它们）。
- 新增 LLM/向量服务商时，必须先确认 llm-sdk 已有对应协议客户端；缺失时在 llm-sdk 内补齐并出测试，再在本项目注册 provider，禁止在本项目内手写协议。
