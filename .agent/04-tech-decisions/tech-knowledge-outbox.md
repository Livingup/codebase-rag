# 知识写入可靠性（外发箱模式）knowledge-outbox

关联功能单元：FEAT-KS-01 / FEAT-KS-02 / FEAT-KS-03 / FEAT-KS-04（实现）、FEAT-QS-02（upsert 确认）、FEAT-QS-03 / FEAT-QS-04（完整知识集判定）、FEAT-WF-03（生产方）、FEAT-WF-06（补偿循环驱动方）、FEAT-MCPS-05（受理语义出口）。

## 1. 启用依据与背景坑点

- 场景：知识生产链路（LLM 生成→向量化→持久写入→内存刷新）横跨多个可失败环节；Qdrant 写入与后续内存刷新、任务清理之间无跨存储事务；进程可能在任一环节重启。
- 不这么做的故障：写入后刷新失败就回滚会浪费昂贵的 LLM 产物；写入失败直接丢弃则知识永久缺失且客户端误以为已受理；半写状态（chunk 部分到达）被读取会产生残缺知识。

## 2. 选定的技术方案与强制契约

- **实现落点**：`services/knowledge_sync/service.py`（两级锁与发布协调）+ `services/knowledge_sync/store.py`（JSON 外发箱）；完整性判定在 `services/qdrant_storage/service.py`。
- **外发箱契约**：待发布任务按 `{store_dir}/jobs/{project_id}/{file_version_id}.json` 落盘（含全量 points 与向量）；最新版本索引按 `{store_dir}/latest/{project_id}/{sha256(归一化file_path)}.json` 落盘；写入走 `path+".tmp"` + `os.replace` 原子替换；服务重启后据此找回未发布知识。
- **两级锁契约**：文件锁（`{project_id}:{file_path}`）→ 版本锁（`{project_id}:{file_version_id}`），顺序固定；同路径新版本提交先 set_latest + cancel_stale_file_jobs（旧版本任务取消先于一切发布）；发布前复查 latest，被更新版本取代则置 cancelled。
- **写入确认契约**：upsert_file_points 分批 `upsert(wait=True)`；返回值非 True 即抛 RuntimeError("Knowledge write was not confirmed.")；确定性点 ID = `uuid5(NAMESPACE_URL, "codebase-rag:point:{project_id}:{fvid}:{kset}:{idx}")`，重写幂等覆盖。
- **完整知识集契约**：读取与存在性判定要求 chunk 0..chunk_count-1 齐全（`set(索引集合)==set(range(chunk_count))` 且 chunk_count>0）；并存多套完整集取 knowledge_set_id（payload 排序键规范 JSON 的 sha256）字典序最小；杜绝半写状态被读为可用版本。
- **重试契约**：发布失败 retry_count+1（无上限累计）、last_error 截断 500 字符、status=ready 落盘；补偿循环先重试外发箱再处理文件重试队列；retry_ready_jobs 持版本锁后二次校验仍为 ready 才发布（防并发重复）。
- **受理边界契约**：受理责任以正常文件系统及服务运行条件为边界，不承诺断电零丢失、确定处理期限或永久最终交付；只有任务本身保存成功才构成可恢复的受理依据。

## 3. 核心不变量与禁止项（Invariants & Prohibitions）

1. 持久写入或任务承接确认后，附带的内存刷新、任务清理失败严禁反转已确认结果——内存刷新一律 best-effort（异常仅记日志）。
2. 同一路径的新版本提交必须先标记最新并取消旧版本待办，严禁旧版本任务在新版本之后发布生效。
3. 严禁把原始 point 命中当作完整文件版本——必须按完整知识集算法判定。
4. upsert 严禁不等待确认就向调用方报告成功（wait=True 强制）。
5. 严禁把外发箱 pending job 当作已发布知识读取——try_skip_existing 只认 Qdrant 完整知识集，从不发布或复用 pending job。
