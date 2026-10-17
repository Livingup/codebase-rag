# 技术总览

## 架构基线

- 分层与模块切分：按服务模块组织；服务内区分 service、models、errors，不引入 COLA。
- 命名：服务对象使用 XxxService，服务、模型、异常分别由 service.py、models.py、errors.py 承载。
- 组装：应用入口是组装根，依赖通过构造注入，回调通过 setter 接线。
- 逻辑承载：MemoryQdrantService 独占内存实例生命周期与缓存状态；WorkflowOrchestratorService 承担创建临时实例、加载、提升、清理的编排。
- 并发：单进程 asyncio；内存缓存状态由单一 asyncio 锁保护，同项目 workflow 任务由 ProjectTaskQueue 串行。
- 错误：各服务使用自己的异常体系，MemoryQdrant 使用 MemoryQdrantError 子类，异常逐层包装。
- 日志：使用 structlog，事件名使用 snake_case。
- 测试：异步单元测试使用 unittest.IsolatedAsyncioTestCase，以 SimpleNamespace 伪造依赖；pytest 运行，asyncio_mode=auto。
