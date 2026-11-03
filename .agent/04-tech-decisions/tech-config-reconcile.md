# 配置热重载协调 config-reconcile

关联功能单元：FEAT-COMMON-04（ConfigManager 实现）、FEAT-MGMT-04（POST /api/v1/config/reload 入口）、FEAT-AG-01 / FEAT-EMB-01 / FEAT-MCPC-02 / FEAT-QS-01（reconciler 注册方）。

## 1. 启用依据与背景坑点

- 场景：正式服务以 Windows Service 长期运行（运维约定见 AGENTS.md 第 9.1 节），调参（Key 轮换、模型优先级、并发信号量、向量库连接、外部 MCP Server）不允许每次都重启进程。
- 不这么做的故障：直接替换配置对象会撕裂运行中状态（如换 Key 丢失健康度、换连接产生半初始化客户端）；全量重建所有服务则等于进程内重启，失去热重载意义。

## 2. 选定的技术方案与强制契约

- **实现落点**：配置层 `config/loader.py`（`ConfigManager`）；各服务 reconcile 方法驻留在自身域服务门面（`services/<domain>/service.py`），经 `register_reconciler` 注册。
- **执行协议**：reload 持锁 → 磁盘重读（config.yaml + 同目录 .env，overlay 优先于 os.environ，`${VAR}` 无值抛 ValueError）→ 先 detect_immutable_changes → 按 dict 注册顺序逐个执行 reconciler(old,new) → **任一失败抛异常中断且不 swap 配置** → 全部成功才替换内存 AppConfig。
- **差量 reconcile 契约**（各服务自行 diff，只动变更部分）：
  - agent：provider 三向 diff；仅 max_concurrency 变化才重建信号量；Key 变化走 keypool.update_keys 保态。
  - embedding：active_provider 切换才重建 provider+KeyPool；仅并发变化重建双信号量。
  - mcp_client：仅"新增或配置对象不等"的 server 重新 discover，未变更的保留工具缓存。
  - qdrant_storage：连接参数变化平滑换客户端。
- **不可变参数感知**：management_api.server.host/port、mcp.servers 各 (host,port) 集合、system.data_dir、system.log.level 发生变化 → 仅记 warning 并返回 requires_restart 与原因清单，不阻止配置生效、不在进程内热改、不引发服务中断。
- **运维触发**：`.\ops\windows\refresh-local-service-config.ps1` 或 `POST http://127.0.0.1:18080/api/v1/config/reload`；涉及不可变参数时运维脚本 `-Restart` 重启。

## 3. 核心不变量与禁止项（Invariants & Prohibitions）

1. 任一 reconciler 失败时严禁替换内存 AppConfig——新配置整体不生效，保持旧配置继续运行。
2. 进程不可变参数（监听地址端口/数据根目录/日志根级别）严禁在进程内热改——必须走 requires_restart 返回路径。
3. 严禁服务绕过 ConfigManager 自行读配置文件或 watch 文件——一切热更新必须经 reconciler 注册。
4. reconciler 内严禁抛出未包装异常导致 reload 半途崩溃而不清理——失败必须让 reload 整体回退到旧配置。
5. reconcile 只重建变更部分，严禁全量重建未变更的连接/实例/缓存（破坏 Key 健康度与工具缓存延续性）。
