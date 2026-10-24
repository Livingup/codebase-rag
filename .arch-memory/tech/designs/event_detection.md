# Git 事件检测 event_detection · 技术设计

## 事件分类

- 不变量：事件类型必须以重读仓库 HEAD 与登记基线的比对结果为准，不得以 hook 名称作为最终分类。
- 方案：hook 主动通知与 .git 目录轮询 watcher 双通道并存；同项目在去重窗口内的重复通知只处理一次。

## Hook 注入

- 不变量：用户已有 hook 必须先备份为 .codebase-rag-backup，再以 wrapper 方式共存执行；仅允许删除带托管标记的脚本。
- 不变量：hook 上报必须短超时且失败放行，事件服务故障不得阻塞 Git 操作。
