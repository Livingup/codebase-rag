# Git 操作 git_operations

## 边界

- 负责：Git CLI 异步访问、仓库校验、HEAD/分支读取、文件树与提交差异解析、blob 内容与版本身份计算、文件过滤三层中的黑名单与白名单两层。
- 不负责：事件分类(→ event_detection)；灰区类型的 LLM 判定(→ workflow)；知识存取(→ qdrant_storage)。

## 关键概念

- file_version_id：由规范化相对路径与 blob hash 派生的文件内容版本身份。
- 纯重命名：路径变化但 blob 不变的变更，知识点可直接复用。
- 灰区（uncertain）：黑名单与白名单都未命中的文件集合，交由下游分类。
