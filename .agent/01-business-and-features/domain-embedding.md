# 向量服务 embedding

## 一、业务域职责边界与域内铁律

- 负责：稠密（Gemini）与稀疏（DashScope）向量生成、token 计数、批处理与单文本长度门禁。
- 不负责：知识点分块决策（调用方在提交前完成拆分）；向量持久化与检索（→ qdrant_storage、memory_qdrant）。
- 域内铁律：超出 batch_size 或单文本 max_tokens_per_text 的输入必须在调用上游前阻断报错，不得截断后送入。

## 二、业务功能单元明细

### [FEAT-EMB-01] 双 Provider 初始化与密钥健康校验
- **业务目标**：dense（gemini）+ sparse（dashscope）双通道向量服务初始化。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [配置校验]：校验 dense/sparse.active_provider 存在于 providers 且有工厂实现、Key 列表非空（否则 ValueError）。
  2. [并发隔离]：dense 与 sparse 各自独立 Semaphore（`embedding.max_concurrency`，默认 3）——Gemini（海外代理链路）与 DashScope（国内直连）两条链路互不争抢容量，不合并为单一全局信号量。
  3. [密钥健康校验]：_validate_keys 启动时逐 Key health_check；失败 Key mark_unhealthy；**部分成功可启动，全部不健康抛 RuntimeError 阻断启动**。
  4. [sparse 维度]：默认 2048。
- **级联依赖功能单元（Cascade Links）**：FEAT-COMMON-01（KeyPool）、FEAT-COMMON-04（reconciler 注册）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-key-pool.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/embedding/service.py）：`src/codebase_rag/services/embedding/service.py`（初始化段）
  - 服务商适配器（services/embedding/providers/base.py）：`src/codebase_rag/services/embedding/providers/base.py`
  - 服务商适配器（services/embedding/providers/<name>.py）：`src/codebase_rag/services/embedding/providers/gemini.py`、`src/codebase_rag/services/embedding/providers/dashscope.py`

### [FEAT-EMB-02] embed 方法族与批处理门禁
- **业务目标**：严格校验输入、拒绝自动分块的双模向量生成。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [dense 双重门禁]：_validate_batch_size（len(texts) > batch_size（默认 100）→ ValueError，提示"分块是调用方责任"）+ _validate_tokens（逐文本经 countTokens 校验，超 max_tokens_per_text（默认 2048）→ ValueError）；门禁失败直接阻断请求，不得截断后送入。
  2. [sparse 门禁]：只校验 batch_size，不校验 token。
  3. [双模规则]：embed_sparse(texts, is_query)——is_query=True 且 DashScope 时走 embed_batch_for_query（text_type=query 检索模式），否则 embed_batch（document 索引模式）；索引与检索使用不同模式。
  4. [合成接口]：embed 并行 gather dense+sparse 合成 EmbeddingResult；count_tokens 对外提供 token 计数。
  5. [重试规则]：总尝试数 = keypool.total_keys × max_retries；按 provider_name 是否含 "dense" 选信号量；429 判定（status_code==429 / "429" / RESOURCE_EXHAUSTED / Too Many Requests）→ 不 report_failure 仅退避（delay=max(base_delay×backoff^(attempt%max_retries), 1.5)）；非 429 → report_failure 计入熔断。
  6. [热更新]：max_concurrency 变化重建双信号量；active_provider 切换则 close 旧实例重建 provider+KeyPool；同 provider 仅 Key 变化则 update_keys 保态。
- **级联依赖功能单元（Cascade Links）**：FEAT-COMMON-01（KeyPool）、FEAT-COMMON-02（TokenCounter）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：`.agent/04-tech-decisions/tech-key-pool.md`
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/embedding/service.py）：`src/codebase_rag/services/embedding/service.py`（embed 方法族与重试段）*(同类参考样板：门禁阻断式批处理)*

## 三、域内量化参数速查

| 参数 | 默认值 | 配置键 |
| :--- | :--- | :--- |
| 双通道并发 | 3（dense/sparse 各一） | `embedding.max_concurrency` |
| 批大小上限 | 100 | `embedding.batch_size` |
| 单文本 token 上限 | 2048 | `embedding.max_tokens_per_text` |
| sparse 维度 | 2048 | `embedding.sparse` 段 |
