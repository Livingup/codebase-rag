# Git 事件检测 event_detection

## 边界

- 负责：Git hook 注入与周期巡检、.git 目录 watcher 兜底探测、HEAD 变化分类为分支切换或内容提交并发布 GitEvent。
- 不负责：事件的后续编排(→ workflow)；仓库登记存储(→ management_api)；HEAD 与分支读取(→ git_operations)。

## 关键概念

- 托管 hook：带托管标记的 post-commit / post-merge / post-checkout / post-rewrite 脚本，由本域生成和修复。
- 兜底 watcher：按去抖周期轮询 .git/HEAD 与 ref 文件快照的补充探测通道，覆盖 hook 丢失或不触发的场景。

## 状态流转

- hook：INSTALLED →（被删除或改写）MISSING / CONFLICT →（周期巡检重注入）INSTALLED
