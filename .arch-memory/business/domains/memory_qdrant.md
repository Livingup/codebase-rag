# 内存检索实例 memory_qdrant

## 边界

- 负责：实例生命周期与缓存状态的统一管理。
- 不负责：实例创建、数据加载、提升、清理这一整条工作流的编排。

## 关键概念

- loading：临时实例已创建、尚未提升为活跃实例的加载状态。
- in-flight 重建：某项目正在执行的一次懒加载。

## 状态流转

- loading →（加载完成并提升）active
- loading →（加载失败，由创建链路清理）销毁
- active →（同项目另一实例提升）inactive →（非活跃清理或 LRU 淘汰）销毁
- active →（空闲清扫或空闲活跃实例淘汰）销毁

## 需求约束

- MEMORY_QDRANT-01 同一 project_id 同一时刻仅限一个 in-flight 懒加载，并发请求必须共享这一次的结果。
- MEMORY_QDRANT-02 loading 实例不得被 LRU 淘汰或非活跃实例清理销毁；销毁它的操作仅限创建它的链路、项目全部实例销毁、服务关闭。
- MEMORY_QDRANT-03 懒加载失败不得缓存失败结果，下一个请求必须重新发起重建。
- MEMORY_QDRANT-04 单个请求被取消不得取消共享的重建任务。
- MEMORY_QDRANT-05 不同 project_id 的懒加载可并行，互不阻塞。
