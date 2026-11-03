# Key 池调度与熔断 key-pool

关联功能单元：FEAT-COMMON-01（实现）、FEAT-AG-01 / FEAT-AG-04（agent 使用）、FEAT-EMB-01 / FEAT-EMB-02（embedding 使用）、FEAT-MCPS-02（jev_client 使用）。

## 1. 启用依据与背景坑点

- 场景：单进程服务需要同时接入多 LLM 服务商（6 种 api_format）与双向量服务商（Gemini 海外代理 / DashScope 国内直连），每服务商可配多个 API Key。
- 不这么做的故障：单 Key 遇到配额或鉴权问题时整链路中断；上游频控（429）与真实故障无法区分时，会把临时限流误判为 Key 失效而熔断，导致可用 Key 被大量摘除。

## 2. 选定的技术方案与强制契约

- **实现落点**：公共基础设施层 `common/keypool.py`（`KeyPool` 类，`asyncio.Lock` 串行化），由各使用方服务在初始化时各自持有实例。
- **轮换调度**：get_key 从游标位置起轮询一圈返回首个健康 Key 并推进游标；取 Key 时惰性恢复冷却到期 Key（healthy=True、失败计数清零）。
- **熔断契约**：consecutive_failures 达阈值（默认 3，`RetryConfig.max_retries_per_key`）→ 标记不健康 + 冷却（cooldown_until=now+cooldown_seconds，默认 60s）；report_success 完全复位；mark_unhealthy 供鉴权错误立即熔断。
- **频控豁免契约**（由使用方重试分支实现）：429 / RESOURCE_EXHAUSTED / Too Many Requests → 仅指数退避（agent 侧封顶 60s；embedding 侧下限 1.5s），**不 report_failure、不消耗 attempt**；仅鉴权失败、服务端故障与网络超时上报摘除。
- **热更新契约**：update_keys 按 Key 字符串匹配复用既有 KeyState——仍存在的 Key 沿用原健康度、失败计数与冷却状态，仅新 Key 新建状态。

## 3. 核心不变量与禁止项（Invariants & Prohibitions）

1. 频控类错误不得计入失败熔断——严禁在 429 分支调用 report_failure。
2. 热更新 Key 列表严禁重建全部 KeyState——必须按 Key 值匹配保留遥测状态（健康度/失败计数/冷却）。
3. 全部 Key 不健康时 get_key 返回 None，使用方必须显式处理（换 provider 或报错），严禁阻塞等待。
4. KeyPool 是纯基础设施，严禁在 common/keypool.py 添加任何业务分支或特定服务商逻辑。
