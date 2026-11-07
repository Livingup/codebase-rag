# Git 操作 git_operations

## 一、业务域职责边界与域内铁律

- 负责：Git CLI 异步子进程访问、仓库校验、HEAD/分支读取、文件树与提交差异解析、blob 内容与二进制/Git LFS 读取、版本身份计算、基于 `.gitignore` 语法的文件过滤（全局 `exclude_patterns` + 仓库级 `.git/codebase-rag/ignore`）与文件模态自动识别（`text` / `image` / `unsupported_binary`）。
- 不负责：事件分类（→ event_detection）；知识存取（→ qdrant_storage）。
- 域内铁律：文件版本身份必须由规范化相对路径与 blob hash 派生；同一仓库路径的绝对/相对/反斜杠混用写法必须归一为同一身份；仓库级忽略规则存放在 `.git/codebase-rag/ignore`，不污染用户工作区。

## 二、业务功能单元明细

### [FEAT-GIT-01] 变更解析与纯重命名识别
- **业务目标**：解析两次 ref 之间的结构化文件变更，识别可复用知识的纯重命名。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [差异命令]：`git diff --name-status old..new`，按状态首字母分 A（新增）/ M（修改）/ D（删除）/ R（重命名）。
  2. [重命名裁决]：R 状态 IF `old_blob == new_blob`（均可取到）→ 纯重命名 `RenamedFileEntry`（下游复用知识、不触发 LLM/embedding）；ELSE（内容变化的重命名）→ 拆解为删除 + 新增两条路径处理。
  3. [过滤集成]：所有路径（重命名按 `new_path`）统一过 `.gitignore` 风格文件过滤（FEAT-GIT-02，合并全局 `exclude_patterns` 与仓库 `.git/codebase-rag/ignore`）后归入 `included`。
  4. [容错]：blob 取不到的条目跳过。
- **级联依赖功能单元（Cascade Links）**：FEAT-GIT-02（过滤）、FEAT-WF-03（纯重命名消费方）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/git_operations/service.py）：`src/codebase_rag/services/git_operations/service.py`（差异解析段）
  - 域数据模型（services/git_operations/models.py）：`src/codebase_rag/services/git_operations/models.py`

### [FEAT-GIT-02] gitignore 风格文件过滤与模态识别
- **业务目标**：基于 `.gitignore` 语法规则与自动模态检测确定性裁决每个文件的收录去留，无需 LLM 灰区分类。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [双层 `.gitignore` 规则链]：按顺序先评估全局 `exclude_patterns`，再评估目标仓库 `.git/codebase-rag/ignore` 中的自定义规则；完整支持 `#` 注释、目录后缀 `/`、`*`、`**`、`!pattern` 反选覆盖（后命中的规则覆盖先命中的规则）及反斜杠转义（如 `foo\ bar`、`\#literal`）；任一父目录被规则排除时，文件级 `!pattern` 不得单独重新包含其后代，必须先解除父目录排除。通配符与转义编译统一委托 `pathspec.GitIgnoreSpec`（`GitIgnoreSpecPattern` 语义，依赖 `pathspec>=1.1`），禁止再自研 glob→正则 翻译器；在此之上由 `FileFilter._has_ignored_ancestor` 与 `_is_target_excluded` 按自顶向下顺序对各级祖先目录（`is_dir=True`）及目标文件（`is_dir=False`）直接基于编译后的 `GitIgnoreSpecPattern` 严格按“最后命中规则生效”裁决（消除 `SimpleGiBackend` 中 `*`/`**` 优先级高于目录规则 `!*/`/`!dir/` 的反转缺陷，以及 `dir/**` 误命中 `dir` 自身的问题）；相对路径归一化自动剥离前导 `./` 与 `/`。
  2. [模态准入范围]：`classify_file_modality` 自动识别文件模态——仅允许非忽略的 UTF-8 文本文件（`text`）与 `gemini-embedding-2` 支持的 `.png`（`image/png`，校验 PNG 魔数 `\x89PNG\r\n\x1a\n`）、`.jpg` / `.jpeg`（`image/jpeg`，校验 JPEG 魔数 `\xff\xd8\xff`）图片文件（`image`，且单张图片 ≤ 20MB）；其余已知二进制扩展名或包含 NUL 字节的未知二进制文件统一归为 `unsupported_binary` 并拦截。
  3. [仓库级忽略规则管理]：`GitOperationsService` 提供 `get_ignore_file_path`、`get_ignore_content`、`get_ignore_rules`、`set_ignore_content`、`add_ignore_rules`、`remove_ignore_rules`、`is_file_ignored` 接口读写 `.git/codebase-rag/ignore`。
  4. [规则编译与热更新]：全局规则在初始化与 `update_config` 时编译为 `GitIgnoreSpec`；仓库规则按调用读取并叠加到全局规则之后，`update_config` 支持配置热重载。
- **级联依赖功能单元（Cascade Links）**：FEAT-MCPS-06（`manage_project_ignore` 工具调用方）、FEAT-MCPS-05（`add_file_knowledge` 忽略与模态校验）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域内辅助实现（services/git_operations/file_filter.py）：`src/codebase_rag/services/git_operations/file_filter.py` *(同类参考样板：gitignore 规则编译与模态识别)*
  - 域服务门面（services/git_operations/service.py）：`src/codebase_rag/services/git_operations/service.py`（ignore 规则管理与模态检测段）

### [FEAT-GIT-03] file_version_id 与 blob/LFS 读取
- **业务目标**：内容寻址的文件版本标识与 Git 对象（含文本、图片二进制及 Git LFS 指针文件）内容访问。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [身份计算式]：`file_version_id = sha256(normalized_path + blob_hash)`——路径归一为 `/` 分隔后拼接 blob SHA-1，输出 64 位 hex；路径+内容双因子，因此纯重命名的 old/new 是两个不同 fvid。
  2. [blob 读取双模式]：`get_blob_hash` 默认兼容模式（缺失与错误都返回 None）；`strict=True` 严格模式（缺失即无文件、错误上抛、输出做 mode/type/40|64-hex 格式校验，失败抛 `GitCommandError`）——严格模式仅由文件知识读取入口（FEAT-MCPS-04）显式启用。
  3. [文本与二进制/Git LFS 读取]：`get_file_content` 读取 UTF-8 文本；`get_file_bytes` 与 `get_workdir_file_bytes` 通过 `_run_git_bytes` 读取原始字节，并自动检测 Git LFS 指针文件（以 `version https://git-lfs.github.com/spec/v1` 开头）——解析指针中的 sha256 OID 与 size，优先读取 Git common dir 下 `.git/lfs/objects/` 对应对象；工作区文件仅在 SHA-256 与 size 均匹配指针时使用；否则执行 `git lfs smudge` 并再次校验结果。禁止把与指针版本不一致的当前工作区内容作为历史 blob 返回。
  4. [工作区 blob 计算]：`get_workdir_blob_hash` 优先使用 `git hash-object --filters --path <file_path>` 以确保启用 Git LFS 过滤器的文件计算出与 Git 索引一致的 blob SHA-1。
- **级联依赖功能单元（Cascade Links）**：无（被 WF/MCPS/MGMT 广泛消费）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/git_operations/service.py）：`src/codebase_rag/services/git_operations/service.py`（身份计算与读取方法段）*(同类参考样板：双模式读取与 LFS 解析接口)*

### [FEAT-GIT-04] Git 命令执行器
- **业务目标**：统一 Git 子进程执行、并发与超时保护。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [并发控制]：per-repo Semaphore 限制同仓库并发（`max_concurrency=4`）。
  2. [超时保护]：`asyncio.wait_for(command_timeout=30s)` 超时 → kill 子进程 + `GitTimeoutError`。
  3. [双通道执行]：`_run_git` 执行 UTF-8 `errors=replace` 文本解码；`_run_git_bytes` 原样返回 `stdout` 原始字节供图片二进制读取使用。
- **级联依赖功能单元（Cascade Links）**：无
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/git_operations/service.py）：`src/codebase_rag/services/git_operations/service.py`（命令执行器段）
  - 域异常体系（services/git_operations/errors.py）：`src/codebase_rag/services/git_operations/errors.py`

## 三、域内量化参数速查

| 参数 | 默认值 | 配置键 |
| :--- | :--- | :--- |
| Git 可执行文件 | git | `git_operations.git_executable` |
| 命令超时 | 30s | `git_operations.command_timeout` |
| 单仓库并发上限 | 4 | `git_operations.max_concurrency` |
