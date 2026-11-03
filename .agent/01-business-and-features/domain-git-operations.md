# Git 操作 git_operations

## 一、业务域职责边界与域内铁律

- 负责：Git CLI 异步子进程访问、仓库校验、HEAD/分支读取、文件树与提交差异解析、blob 内容与版本身份计算、文件过滤三层中的黑名单（L1）与白名单（L2）两层、灰区类型键缓存存储。
- 不负责：事件分类（→ event_detection）；灰区类型的 LLM 判定（→ workflow FEAT-WF-04）；知识存取（→ qdrant_storage）。
- 域内铁律：文件版本身份必须由规范化相对路径与 blob hash 派生；同一仓库路径的绝对/相对/反斜杠混用写法必须归一为同一身份。

## 二、业务功能单元明细

### [FEAT-GIT-01] 变更解析与纯重命名识别
- **业务目标**：解析两次 ref 之间的结构化文件变更，识别可复用知识的纯重命名。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [差异命令]：`git diff --name-status old..new`，按状态首字母分 A（新增）/ M（修改）/ D（删除）/ R（重命名）。
  2. [重命名裁决]：R 状态 IF old_blob == new_blob（均可取到）→ 纯重命名 RenamedFileEntry（下游复用知识、不触发 LLM/embedding）；ELSE（内容变化的重命名）→ 拆解为删除 + 新增两条路径处理。
  3. [过滤集成]：所有路径（重命名按 new_path）统一过三层过滤（FEAT-GIT-02）后分别归入 included / uncertain。
  4. [容错]：blob 取不到的条目跳过。
- **级联依赖功能单元（Cascade Links）**：FEAT-GIT-02（过滤）、FEAT-WF-03（纯重命名消费方）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/git_operations/service.py）：`src/codebase_rag/services/git_operations/service.py`（差异解析段）
  - 域数据模型（services/git_operations/models.py）：`src/codebase_rag/services/git_operations/models.py`

### [FEAT-GIT-02] 三层文件过滤
- **业务目标**：确定性裁决每个文件的收录去留，L1/L2 零成本、L3 交灰区分类。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [L1 黑名单（最高优先级）]：`exclude_patterns` glob（fnmatch.translate 预编译正则 match）命中 → definitive exclude。
  2. [L2 白名单]：`include_extensions`（小写扩展名）或 `include_filenames`（精确 basename）命中 → definitive include。
  3. [L3 灰区]：其余文件 → uncertain，交 workflow 灰区分类（FEAT-WF-04）。
  4. [性能目标]：10k 文件过滤 < 10ms；`update_config` 支持配置热重载。
- **级联依赖功能单元（Cascade Links）**：FEAT-WF-04（L3 消费方）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域内辅助实现（services/git_operations/file_filter.py）：`src/codebase_rag/services/git_operations/file_filter.py` *(同类参考样板：预编译 glob 过滤)*

### [FEAT-GIT-03] file_version_id 与 blob 读取
- **业务目标**：内容寻址的文件版本标识与 Git 对象内容访问。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [身份计算式]：`file_version_id = sha256(normalized_path + blob_hash)`——路径归一为 `/` 分隔后拼接 blob SHA-1，输出 64 位 hex；路径+内容双因子，因此纯重命名的 old/new 是两个不同 fvid。
  2. [blob 读取双模式]：`get_blob_hash` 默认兼容模式（缺失与错误都返回 None）；`strict=True` 严格模式（缺失即无文件、错误上抛、输出做 mode/type/40|64-hex 格式校验，失败抛 GitCommandError）——严格模式仅由文件知识读取入口（FEAT-MCPS-04）显式启用。
  3. [内容读取]：`get_file_content` 走 git show，"not a valid object name"/"does not exist" → GitFileNotFoundError。
  4. [其他公开方法]：`get_workdir_blob_hash`（hash-object 工作区文件）、`get_head_info`（symbolic-ref 失败 → detached HEAD，branch_name=None）、`get_root_commit`（rev-list --max-parents=0，多根取字典序最小，空仓库返回 None）、`get_all_branches`、`validate_repository`、`get_file_tree`。
- **级联依赖功能单元（Cascade Links）**：无（被 WF/MCPS/MGMT 广泛消费）。
- **关联外部索引指针**：
  - 前端目前 UI 现状：无
  - 技术架构抉择：无
- **【本功能全栈源码文件清单（Source File Manifest）】**：
  - 域服务门面（services/git_operations/service.py）：`src/codebase_rag/services/git_operations/service.py`（身份计算与读取方法段）*(同类参考样板：双模式读取接口)*

### [FEAT-GIT-04] Git 命令执行器
- **业务目标**：统一 Git 子进程执行、并发与超时保护。
- **当前生效的业务需求约束（Current Truth Rules）**：
  1. [并发控制]：per-repo Semaphore 限制同仓库并发（`max_concurrency=4`）。
  2. [超时保护]：`asyncio.wait_for(command_timeout=30s)` 超时 → kill 子进程 + GitTimeoutError。
  3. [解码]：UTF-8 errors=replace 容错解码。
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
