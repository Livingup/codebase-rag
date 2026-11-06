# 向量服务 embedding

## 一、业务域职责边界与域内铁律

- 负责：基于 `gemini-embedding-2` 的纯稠密（3072 维 Cosine）多模态向量生成（支持纯文本或「文本描述 + `.png`/`.jpg`/`.jpeg` 图片」联合嵌入）、token 计数、批处理与单点 8192 token 门禁。
- 不负责：知识点分块决策（调用方在提交前完成拆分）；向量持久化与检索（→ qdrant_storage、memory_qdrant）。
- 域内铁律：超出 `batch_size` 或单点 `max_tokens_per_text`（默认 8192，图片文件每点固定计入 `IMAGE_TOKEN_COST = 258` tokens）的输入必须在调用上游前阻断报错，不得截断后送入。

## 二、业务功能单元明细

### [FEAT-EMB-01] Gemini Embedding-2 初始化与密钥健康校验
- **业务目标**：`dense`（`gemini-embedding-2`）向量服务初始化与密钥健康验证。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [配置校验]：校验 `dense.active_provider` 存在于 `providers` 且有工厂实现、Key 列表非空（否则 `ValueError`）。
  2. [并发控制]：独立 Semaphore（`embedding.max_concurrency`，默认 3）控制并发请求。
  3. [密钥健康校验]：`_validate_keys` 启动时逐 Key 调用 `health_check`（真实调用 Gemini `countTokens` 接口）；失败 Key `mark_unhealthy`；**部分成功可启动，全部不健康抛 `RuntimeError` 阻断启动**。
  4. [模型与维度]：默认模型 `gemini-embedding-2`，输出维度 `3072`（若配置低于 3072 的 Matryoshka 子维度则自动执行 `l2_normalize`）。
  5. [SDK 委托]：由 `GeminiEmbeddingClient.batch_embed_contents` 完成协议与错误分类（见 `.agent/04-tech-decisions/tech-llm-sdk-adoption.md`）。
- **级联依赖功能单元（Cascade Links）**：FEAT-COMMON-01（KeyPool）、FEAT-COMMON-04（reconciler 注册）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-key-pool.md`、`.agent/04-tech-decisions/tech-llm-sdk-adoption.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/embedding/service.py）：`src/codebase_rag/services/embedding/service.py`（初始化段）
  - 服务商适配器（services/embedding/providers/base.py）：`src/codebase_rag/services/embedding/providers/base.py`
  - 服务商适配器（services/embedding/providers/<name>.py）：`src/codebase_rag/services/embedding/providers/gemini.py`

### [FEAT-EMB-02] embed 门禁与多模态向量生成
- **业务目标**：严格校验输入、拒绝自动分块的文本与「文本+图片」多模态稠密向量生成。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [双重门禁]：`_validate_batch_size`（`len(inputs) > batch_size`（默认 100）→ `ValueError`，提示"分块是调用方责任"）+ `_validate_tokens`（逐项经 `count_tokens` 校验文本 token，若为 `MultimodalEmbedInput` 图片输入则额外加 `IMAGE_TOKEN_COST = 258` tokens，且校验图片字节 ≤ 20MB 且 MIME 属于 `image/png` 或 `image/jpeg`；总 token 超 `max_tokens_per_text=8192` → `ValueError`）；门禁失败直接阻断请求，不得截断后送入。
  2. [多模态报文构造]：纯文本输入构造 `parts=[{"text": item}]`；图片输入 `MultimodalEmbedInput` 在同一个 `Content` 的 `parts` 中同时传入 `[{"text": item.text}, {"inlineData": {"mimeType": item.mime_type, "data": base64(item.image_bytes)}}]`，将文字描述与图片共同编码为单个 3072 维向量。
  3. [检索/索引双任务类型]：`is_query=True` 时传 `taskType="RETRIEVAL_QUERY"`；`is_query=False` 时传 `taskType="RETRIEVAL_DOCUMENT"`。
  4. [合成接口]：`embed(inputs, is_query)` 返回仅含 `dense_vector` 的 `List[EmbeddingResult]`；`count_tokens` 对外提供 token 计数。
  5. [重试规则]：总尝试数 = `keypool.total_keys × max_retries`；429 判定（llm-sdk `LLMClientError.kind ∈ {RATE_LIMIT, OVERLOADED}` / `status_code==429` / `"429"` / `RESOURCE_EXHAUSTED` / `Too Many Requests`）→ 不 `report_failure` 仅退避（`delay=max(base_delay×backoff^(attempt%max_retries), 1.5)`）；非 429 → `report_failure` 计入熔断。
  6. [热更新]：`max_concurrency` 变化重建信号量；`active_provider` 切换则 close 旧实例重建 provider+KeyPool；同 provider 仅 Key 变化则 `update_keys` 保态。
- **级联依赖功能单元（Cascade Links）**：FEAT-COMMON-01（KeyPool）、FEAT-COMMON-02（TokenCounter）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-key-pool.md`、`.agent/04-tech-decisions/tech-llm-sdk-adoption.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/embedding/service.py）：`src/codebase_rag/services/embedding/service.py`（embed 方法族与重试段）*(同类参考样板：门禁阻断式批处理)*
  - 服务商适配器（services/embedding/providers/<name>.py）：`src/codebase_rag/services/embedding/providers/gemini.py`

## 三、域内量化参数速查

| 参数 | 默认值 | 配置键 |
| :--- | :--- | :--- |
| 并发上限 | 3 | `embedding.max_concurrency` |
| 批大小上限 | 100 | `embedding.dense.providers.gemini.batch_size` |
| 单点 token 上限 | 8192 | `embedding.dense.providers.gemini.max_tokens_per_text` |
| 单张图片固定 token 消耗 | 258 | `common.types.IMAGE_TOKEN_COST` |
| 单张图片大小上限 | 20MB | `common.types.MAX_IMAGE_BYTES` |
| 稠密向量维度 | 3072 | `embedding.dense.providers.gemini.dimension` |
