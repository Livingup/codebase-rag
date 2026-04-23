# Codebase RAG 多 Git 分支记忆同步方案

> 技术栈：Qdrant Server + Python (qdrant-client in-memory mode)

---

## 一、核心问题

RAG 系统的"记忆"（向量化的代码语义）是基于某个代码快照生成的。Git 分支意味着**同一个仓库同时存在多个不同的代码快照**。

核心矛盾：

- 如果按分支隔离存储 → 95% 数据冗余（分支间绝大多数文件相同）
- 如果混在一个池子里 → 语义搜索会命中其他分支的"幽灵数据"

**目标：让 RAG 的记忆始终与当前分支的代码保持一致，同时零冗余存储。**

---

## 二、解法：存储与检索分离 + 内容寻址

### 2.1 存储层（Qdrant Server）

**设计原则：以文件内容的 hash 为唯一标识，而不是以分支+文件路径为标识。**

- 以 `file_version_id = hash(filePath + blobHash)` 作为文件版本的唯一标识，存入 point 的 payload
- **相同路径+相同内容只存一份**，不管它属于多少个分支
- 这一层**永远不做语义搜索**，只做按 `file_version_id` 精确存取
- 本质上就是一个 `file_version_id → points` 的 KV 持久化存储

为什么这样设计？因为 Git 分支间 95%+ 的文件是相同的。内容寻址彻底回避了"冗余 vs 隔离"的矛盾 — 相同内容天然去重，不同内容天然隔离。

**Point 存储的不是源码片段，而是 Agent 升维生成的自然语言业务描述知识点。** 当文件内容被提交时，Agent 会分析源码并生成结构化的业务描述文本，embedding 是基于这个业务描述计算的，而非源码本身。这样语义检索的匹配精度远高于直接对代码做 embedding。

**Qdrant Collection：`codebase`**

面向业务的源码问答知识库。通过向量化和结构化元数据，存储软件项目各模块的业务逻辑描述，支持自然语言检索和问答。知识点基于源码分析生成，确保与项目同步。系统通过 `project_id` 实现多租户隔离，支持多个项目共享此集合。

**知识点与源码文件的关系：**

- 每个知识点 point 与源码文件版本（`filePath + blobHash`）绑定
- **一个源码文件版本可以有多个知识点**（大文件需要语义分块，详见下文分块策略）
- 知识点中的 `business_description` 始终是该文件**最新版本的完整业务描述**，不是记录变化的增量描述

**Point 结构：**

```
Point {
  id:      Qdrant 自动生成 UUID
  vector:  {
    dense:  基于 business_description 的稠密向量,
    sparse: 基于 business_description 的稀疏向量
  }
  payload: {
    file_version_id:      keyword  // hash(filePath + blobHash)，同一文件版本的所有分块共享此 ID，用于按文件版本批量查询
    file_path:            keyword  // 文件或目录的绝对路径，精确定位知识点对应的源码位置，支持精确过滤和检索
    entity_type:          keyword  // 实体类型枚举：
                                   //   'module' — 业务模块，描述整个模块的整体职责和子组件关系
                                   //   'class'  — 类，描述类的整体业务功能、继承关系和主要方法
                                   //   'file'   — 非类文件（配置文件、资源等），描述其在项目中的作用和内容结构
    entity_name:          keyword  // 实体名称（类名、模块名等），用于精确匹配和语义关联
    business_description: text     // 向量对应的原始文本，Agent 升维生成的自然语言业务描述，不能为空
    project_id:           keyword  // 项目唯一标识，多租户隔离，确保不同项目的知识点互不干扰
    blob_hash:            keyword  // Git blob hash（文件内容唯一标识），用于内容寻址和去重判断
  }
}
```

**为什么 `file_version_id` 需要包含 `filePath`？** Git blob hash 只关注文件内容，不包含路径。两个不同路径的文件如果内容完全相同，它们的 blob hash 一样，但所在的包/模块不同，Agent 生成的业务描述也会不同。加上 filePath 才能保证每个文件位置的知识点独立存储。

**Qdrant Collection 设计要点：**

- Point ID 由 Qdrant 自动生成 UUID，无需自定义
- 通过 payload 中的 `file_version_id` 按文件版本批量查询/删除所有分块
- 存储 dense vector + sparse vector + payload（含业务描述和结构化元数据）
- 通过 `project_id` 实现多项目隔离，多个项目共享同一个 Collection
- 可关闭 HNSW 索引（因为不做 ANN 搜索，纯当 KV 用，节省资源）

#### 2.1.1 业务描述 `business_description` 的格式规范

**核心原则：尽最大能力描述出更多的业务逻辑细节！** 我们不是要一个简洁的概括描述！因为包含越多业务细节的描述构建的知识库越精准，一个高质量的业务描述知识库对软件项目的高质量迭代开发起到了决定性的影响。描述中一定要包含充分的业务关键词、详细的功能目的说明、业务实现说明，这是后续检索成功率的关键！

每个知识点的业务描述必须遵循以下 **5 层结构化模板**，从宏观定位到微观细节，确保检索时能从多个维度命中：

```
## 项目结构域
[说明该文件在项目中的技术定位]
取值范围：前端源码 | 后端源码 | 配置文件 | 测试代码 | 构建脚本 | 数据库脚本 | 文档 | 其他

## 业务域
[说明该文件所属的业务模块]
示例：用户管理模块、订单模块、支付模块、权限认证模块、消息通知模块等

## 功能描述
[一句话说明该文件/类实现的核心功能是什么]

## 目的描述
[说明为什么需要这个文件/类，它解决了什么问题，在系统中扮演什么角色]

## 业务逻辑细节
[详细描述具体的业务实现逻辑，这是最重要的部分，必须包含充分的业务关键词]

### 类/文件定义
[类的声明、继承关系、实现的接口等]

### 核心方法/函数
[逐个描述核心方法的业务逻辑、入参、出参、处理流程]

### 辅助方法/函数
[描述辅助方法的作用和调用场景]

### 关键业务规则
[描述代码中体现的业务规则、校验逻辑、边界条件等]
```

**示例：一个完整的业务描述**

```
## 项目结构域
后端源码

## 业务域
用户管理模块 - 用户认证子模块

## 功能描述
实现用户登录认证功能，包括账号密码验证、JWT令牌生成和登录日志记录。

## 目的描述
作为系统的安全入口，确保只有合法用户能够访问系统资源。该类是认证流程的核心控制器，协调密码验证服务、令牌服务和日志服务完成完整的登录流程。

## 业务逻辑细节

### 类/文件定义
LoginController 是一个 Spring REST 控制器，使用 @RestController 注解，映射路径为 /api/auth。依赖注入了 UserService、JwtTokenService、LoginLogService 三个服务。

### 核心方法/函数
1. **login(LoginRequest request)** - 用户登录方法
   - 入参：LoginRequest 包含 username(用户名) 和 password(密码)
   - 处理流程：
     a. 调用 UserService.findByUsername() 查询用户
     b. 使用 BCrypt 验证密码是否匹配
     c. 检查用户状态是否为启用状态
     d. 调用 JwtTokenService.generateToken() 生成访问令牌
     e. 记录登录日志（IP、时间、设备信息）
   - 出参：LoginResponse 包含 token、refreshToken、过期时间
   - 异常处理：用户不存在抛出 UserNotFoundException，密码错误抛出 BadCredentialsException

2. **logout(String token)** - 用户登出方法
   - 将当前令牌加入黑名单，使其立即失效

### 辅助方法/函数
- **recordLoginLog()** - 私有方法，记录登录日志到数据库
- **validateLoginAttempts()** - 检查登录失败次数，超过5次锁定账户30分钟

### 关键业务规则
- 密码连续错误5次，账户锁定30分钟
- JWT令牌有效期为2小时，刷新令牌有效期为7天
- 登录成功后清除之前的失败计数
```

#### 2.1.2 Token 限制与大型源码文件分块策略

**重要限制：** 单个知识点的 `business_description` 文本不能超过 **2048 tokens**！超过此限制会导致嵌入向量生成失败。

当源码文件的业务描述超过 2048 tokens 时，必须采用语义分块策略进行拆分。

**核心原则：垂直分块（必须「竖着拆」，禁止「横着拆」）**

向量检索是**「单点命中」机制**——用户的搜索查询只会命中某一个知识点，而不是多个知识点的组合。因此每个分块必须是「自包含」的，能够独立被检索和理解。

**❌ 错误做法（横着拆）**：按 5 层结构水平切分

```
分块1: 项目结构域 + 业务域
分块2: 功能描述 + 目的描述
分块3: 业务逻辑细节
```

问题：搜索"后端用户登录JWT令牌"时，可能命中"分块3"，但该分块没有"后端""用户登录"等定位词，导致语义相似度不够高，检索失败或排名靠后。

**✅ 正确做法（竖着拆）**：每个分块都保留完整的定位信息

```
分块1: 项目结构域 + 业务域 + 功能 + 目的 + 类定义
分块2: 项目结构域 + 业务域 + 核心方法详情
分块3: 项目结构域 + 业务域 + 辅助方法与内部类
```

优势：无论用户从「技术栈」「业务模块」「功能」「实现细节」哪个角度搜索，每个分块都能命中，且命中后立即获得完整上下文。

**冗余是值得的**：用少量存储空间（每个分块多几十个 token）换取检索命中率的大幅提升！

**分块结构要求：**

| 分块类型 | 必须包含 | 重点展开 |
|----------|----------|----------|
| 第 1 个分块（概述） | 完整 5 层结构 | 类/文件定义 |
| 第 2~N 个分块（细节） | 项目结构域 + 业务域 | 业务逻辑细节的不同部分 |

**分块粒度控制：**

- 每个核心方法单独一个分块
- 多个小型辅助方法可合并为一个分块
- 内部类/枚举可单独一个分块
- 建议每个分块控制在 **1500-1800 tokens**，留出安全余量

**分块示例：**

假设 `GeminiEmbeddingService.java` 的完整业务描述超过 2048 tokens，应拆分为：

**知识点 1：概述与定义**（包含前 4 层 + 类定义）

- file_path: `C:/xxx/GeminiEmbeddingService.java`
- entity_name: `GeminiEmbeddingService`
- business_description:

```
## 项目结构域
后端源码

## 业务域
AI能力模块 - 向量嵌入子模块

## 功能描述
实现文本向量嵌入服务，调用 Google Gemini API 将文本转换为向量表示，支持批量处理和Token计数。

## 目的描述
作为RAG系统的核心组件，负责将业务文本转换为可检索的向量形式，是知识库语义搜索的基础设施。

## 业务逻辑细节

### 类/文件定义
GeminiEmbeddingService 实现 EmbeddingService 接口，使用 @Service 注解标记为Spring组件。
依赖注入 WebClient 用于HTTP调用，配置项包括 API_KEY、MODEL_NAME、BATCH_SIZE 等。
```

**知识点 2：核心方法详情**（保留前 2 层定位 + 核心方法）

- file_path: `C:/xxx/GeminiEmbeddingService.java`（相同）
- entity_name: `GeminiEmbeddingService`（相同）
- business_description:

```
## 项目结构域
后端源码

## 业务域
AI能力模块 - 向量嵌入子模块

## 业务逻辑细节

### 核心方法/函数
1. **createEmbeddings(List<String> texts)** - 批量文本向量化
   - 入参：待向量化的文本列表
   - 处理流程：按 BATCH_SIZE 分批 → 构建API请求 → 调用Gemini接口 → 解析响应提取向量
   - 出参：List<float[]> 向量列表，与输入文本一一对应
   - 重试机制：失败自动重试3次，指数退避

2. **countTokens(String text)** - Token计数
   - 调用Gemini的countTokens接口，返回文本的token数量
   - 用于分块前的长度预估
```

**知识点 3：辅助方法与内部类**（保留前 2 层定位 + 辅助内容）

- file_path: `C:/xxx/GeminiEmbeddingService.java`（相同）
- entity_name: `GeminiEmbeddingService`（相同）
- business_description:

```
## 项目结构域
后端源码

## 业务域
AI能力模块 - 向量嵌入子模块

## 业务逻辑细节

### 辅助方法/函数
- **buildRequest()** - 构建HTTP请求体
- **parseResponse()** - 解析API响应，提取embedding数组
- **handleRateLimit()** - 处理API限流，实现退避重试

### 内部类
1. **EmbeddingRequest** - 请求DTO，包含texts数组和model字段
2. **EmbeddingResponse** - 响应DTO，包含embeddings数组
3. **CountTokensRequest/Response** - Token计数的请求响应对
```

**分块要点：**

- 每个分块都保留「项目结构域」和「业务域」，确保单独检索时能定位上下文
- 第一个分块包含完整的前 4 层（定位信息）+ 类定义
- 后续分块简化前置信息，重点展开业务逻辑细节的不同部分

### 2.2 检索层（qdrant-client in-memory 实例）

**设计原则：检索索引是临时的、分支专属的、按需组装的。**

- 每个分支维护一个独立的 **qdrant-client in-memory 实例**（`QdrantClient(location=":memory:")`）
- 实例中**有且仅有**当前分支版本的代码向量
- 所有语义检索都在这个 in-memory 实例上执行
- 分支切换时，重新组装一个新的 in-memory 实例

**qdrant-client in-memory 能力：**

- 稠密向量搜索（HNSW）
- 稀疏关键词搜索（BM25）
- Payload 过滤（keyword / integer / text 全类型支持）
- 混合检索（Prefetch + RRF 融合）
- 纯内存，无磁盘 IO，多个分支实例可同时驻留
- API 与 Qdrant Server 完全一致，存储层和检索层代码可复用

### 2.3 两层如何协作

```
存储层（Qdrant Server, 持久化）      检索层（qdrant-client :memory:, 临时）
┌──────────────────────┐            ┌──────────────────────┐
│                      │            │                      │
│  hash_A → embedding  │   按需     │  当前分支的文件        │
│  hash_B → embedding  │  ─提取→    │  只包含 hash_A, C, D  │
│  hash_C → embedding  │            │                      │
│  hash_D → embedding  │            │  语义搜索在这里执行    │
│  hash_E → embedding  │            │                      │
│  ...                 │            └──────────────────────┘
│                      │
│  所有分支所有版本的    │
│  embedding 都在这里   │
│  但不做搜索！         │
└──────────────────────┘
```

**存储层**解决"不重复计算"，**检索层**解决"不查错版本"。

---

## 三、分支切换流程

### 3.1 标准流程

```
Step 1: git ls-tree -r <branch>
        ↓
        得到该分支所有文件的 content_hash 列表
        （Git 原生能力，万级文件毫秒级）

Step 2: 拿这批 hash 去 Qdrant 批量提取对应的 embedding
        ↓
        命中 = 直接拿到向量（绝大多数情况）
        未命中 = 新文件/新修改，需要计算 embedding 后存入 Qdrant

Step 3: 用提取出来的向量构建一个新的 qdrant-client in-memory 实例
        ↓
        万级向量 upsert 耗时约 200-500ms

Step 4: 将这个内存索引设为"当前活跃索引"
        ↓
        后续所有语义检索都走这个索引
```

### 3.2 性能预估

| 操作 | 10k 文件 | 50k 文件 |
|------|---------|---------|
| git ls-tree 获取 hash | ~10ms | ~50ms |
| Qdrant 批量 retrieve | ~200-500ms | ~1-2s |
| qdrant-client in-memory 构建 | ~200-500ms | ~500ms-1s |
| **首次切换总耗时** | **~500ms-1s** | **~2-4s** |
| **切回已缓存分支** | **~0ms** | **~0ms** |

### 3.3 内存索引缓存

构建出来的 qdrant-client in-memory 实例按 `branch@commitHash` 缓存在 `dict` 中。

- 切回旧分支 → 直接复用缓存的实例，零成本
- 多个分支实例**同时驻留内存**（各实例独立，无冲突）
- 切换 = 换一个 dict 引用
- LRU 策略淘汰最久未用的分支实例，控制总内存（可用 `functools.lru_cache` 或自实现 `OrderedDict` LRU）

**内存占用估算（单分支）：**

| 组成 | 10k chunks (768d) |
|------|------------------|
| 稠密向量 (HNSW) | ~35 MB |
| 稀疏索引 (BM25) | ~15 MB |
| Payload | ~5 MB |
| **合计** | **~55 MB / 分支** |

缓存 5 个分支 ≈ 275 MB，完全可控。

---

## 四、Git 提交与 RAG 同步工作流

### 4.1 核心设计决策

**RAG 只跟踪 Git 已落地的文件变更，不关心工作区和暂存区。**

`git ls-tree -r HEAD` 是唯一的真相来源。未 commit 的编辑、暂存区的文件，RAG 一概不管。

**记忆范围：只关注源码文件和项目配置文件。** `git ls-tree -r HEAD` 的结果需要经过过滤，排除不需要记忆的文件：

- **纳入记忆**：源码文件（.py, .ts, .js, .java, .go, .rs, ...）、项目配置文件（pyproject.toml, requirements.txt, setup.cfg, Dockerfile, docker-compose.yml, ...）
- **排除**：二进制文件（.pyc, .so, .exe, .whl, 图片, 字体, ...）、日志文件（.log）、构建产物（__pycache__/, dist/, build/, node_modules/, .eggs/）、IDE 配置（.idea/, .vscode/）、数据文件（.csv, .sql dump, ...）、虚拟环境（.venv/, venv/）等非项目源码文件

过滤方式（`git ls-tree -r HEAD` 本身只输出已跟踪文件，.gitignore 中的文件不会出现，无需额外处理）：

- **白名单优先**：按文件扩展名维护允许的源码/配置文件类型列表
- **可配置**：提供项目级别的配置文件，允许用户自定义纳入/排除规则

这个决策带来的简化：

- 不需要文件系统 Watcher
- 不需要自行计算文件 hash（Git blob hash 直接可用）
- 不需要处理脏数据、暂存区状态
- 只需要监听一个信号：**HEAD commit hash 是否变了**

### 4.2 两类记忆操作

Codebase 记忆的维护有且只有两类操作，触发条件和处理逻辑完全不同：

| | 操作一：分支切换（视图切换） | 操作二：内容提交（知识生产） |
|---|---|---|
| **本质** | 切换内存 DB 的"视图"，展示不同分支的已有知识 | 为变更的文件生产新的业务描述知识点 |
| **触发** | `post-checkout`（分支名变化） | `post-commit`, `post-merge`, `post-rewrite`（分支名不变，commit hash 变化） |
| **是否调用 Agent** | ❌ 不调用，纯数据搬运 | ✅ 调用 Agent 升维生成业务描述 |
| **是否写入 Qdrant** | ❌ 不写入，只从 Qdrant 读取 | ✅ 新知识点写入 Qdrant |

### 4.3 操作一：分支切换（视图切换）

**触发条件**：`post-checkout` hook，且分支名发生了变化。

**核心逻辑**：内存 in-memory 实例中当前装的是旧分支的 points，需要替换为新分支的 points。不产生新知识，只做数据搬运。

```
git checkout feature-pay（从 main 切到 feature-pay）
        │
        ▼
Step 1: 优先检查缓存池
        缓存 key = feature-pay@<commit_hash>
        ├── 命中 → 直接切换引用，0ms，结束 ✅
        └── 未命中 → 继续
        │
        ▼
Step 2: 获取新分支的文件 hash 清单（过滤后）
        new_hashes = git ls-tree -r feature-pay
                     → 过滤出源码和配置文件
                     → {filePath: blobHash}
        │
        ▼
Step 3: 获取当前内存实例中已有的 file_version_id 集合
        mem_versions = 当前 in-memory 实例中所有 point 的 file_version_id
        │
        ▼
Step 4: 计算差异
        需要删除: mem_versions 中有，new_versions 中没有的
        需要加入: new_versions 中有，mem_versions 中没有的
        保持不动: 两边都有的（大多数）
        │
        ▼
Step 5: 执行增量更新
        ├── 从 in-memory 实例中按 file_version_id 删除“需要删除”的所有 points（含全部分块）
        └── 从 Qdrant Server 按 file_version_id 批量提取“需要加入”的所有 points（含全部分块）
            → upsert 到 in-memory 实例
        │
        ▼
Step 6: 注册到缓存池，设为当前活跃索引 ✅
```

**关键点**：全程不调用 Agent，不计算 embedding，只在 in-memory 实例和 Qdrant Server 之间搬运已有的 points。

### 4.4 操作二：内容提交（知识生产）

**触发条件**：`post-commit` / `post-merge` / `post-rewrite` hook（分支名不变，commit hash 变化）。即：所有形式的文件内容提交，包括本地 commit、git pull、git merge、git rebase。**分支切换导致的文件变更不算提交。**

**核心逻辑**：找出变更文件 → Agent 分析源码生成业务描述 → 计算 embedding → 存入 Qdrant Server → 更新 in-memory 实例。

```
git commit / git pull / git merge（产生新的 commit）
        │
        ▼
Step 1: 获取变更文件列表（区分变更类型）
        git diff --name-status old_commit..new_commit
        → 过滤出源码和配置文件
        → 得到：
          A (新增): [PayChannel.java]
          M (修改): [PaymentService.java]
          D (删除): [OldGateway.java]
        │
        ▼
Step 2: 按变更类型分三条路径处理
```

**路径 A — 新建文件：**

```
A (新增) PayChannel.java
        │
        ▼
A-1: 获取新文件内容和 blob hash
     new_hash = git ls-tree new_commit -- PayChannel.java → blobHash
     new_content = git show new_hash → 文件内容
     file_version_id = hash(filePath + new_hash)
        │
        ▼
A-2: 检查 Qdrant 中是否已有该 file_version_id 的 points
     ├── 已存在 → 跳过（该内容在其他分支已处理过）
     └── 不存在 → 继续
        │
        ▼
A-3: ★ 调用 Agent 从零生成业务描述
     输入：完整的源码文件内容
     输出：按 5 层结构化模板生成业务描述（格式见 2.1.1）
     若描述超过 2048 tokens，按垂直分块策略拆分为多个知识点（见 2.1.2）
        │
        ▼
A-4: 对每个分块计算 embedding → 构建 point（Qdrant 自动分配 UUID）
     → 批量写入 Qdrant Server + upsert 到 in-memory 实例
```

**路径 D — 删除文件：**

```
D (删除) OldGateway.java
        │
        ▼
D-1: 从 old_commit 的文件树获取被删文件的 blobHash
     old_hash = git ls-tree old_commit -- OldGateway.java → blobHash
     file_version_id = hash(filePath + old_hash)
        │
        ▼
D-2: 从 in-memory 实例中按 file_version_id 删除该文件版本的所有 points（含全部分块）
     → Qdrant 中保留（其他分支可能还引用，由 GC 负责清理）
```

**路径 M — 修改文件（增量知识更新）：**

```
M (修改) PaymentService.java
        │
        ▼
M-1: 获取新旧两个版本的信息
     old_hash = git ls-tree old_commit -- PaymentService.java → blobHash
     new_hash = git ls-tree new_commit -- PaymentService.java → blobHash
     old_file_version_id = hash(filePath + old_hash)
     new_file_version_id = hash(filePath + new_hash)
        │
        ▼
M-2: 检查 Qdrant 中是否已有 new_file_version_id 的 points
     ├── 已存在 → 跳过 Agent 生成，直接到 M-7
     └── 不存在 → 继续
        │
        ▼
M-3: 从 Qdrant 中按 old_file_version_id 读取旧版本的所有 points
     → 提取并拼接所有分块的旧 business_description
        │
        ▼
M-4: 获取文件 diff
     git diff old_commit..new_commit -- PaymentService.java → diff 内容
        │
        ▼
M-5: ★ 调用 Agent 基于旧描述 + diff 增量生成新的业务描述
     输入：{
       old_business_description: 旧版本的完整业务描述文本（所有分块拼接）,
       file_diff:               本次修改的 diff 内容
     }
     输出：最新版本的完整 business_description（按 5 层结构化模板，格式见 2.1.1）
     Agent 基于旧描述和 diff 理解变更意图，生成更新后的完整业务描述
     若描述超过 2048 tokens，按垂直分块策略拆分（见 2.1.2）
        │
        ▼
M-6: 对每个分块计算 embedding → 构建 point（Qdrant 自动分配 UUID）
     → 批量写入 Qdrant
        │
        ▼
M-7: 更新 in-memory 实例
     ├── 按 old_file_version_id 删除旧版本的所有 points（含全部分块）
     └── upsert 新版本的所有 points（含全部分块）
```

**三条路径处理完毕后：**

```
Step 3: 更新缓存 key 为新的 commit hash ✅
```

**关键点**：

- **新建文件**：Agent 从零分析完整源码生成业务描述
- **修改文件**：Agent 基于旧 `business_description` + `diff` 增量更新，省 Token、保持描述连续性、更精准
- **删除文件**：仅从 in-memory 实例移除，Qdrant Server 中保留（其他分支可能还引用，由 GC 负责清理）
- 内容寻址保证：如果 `file_version_id` 已存在于 Qdrant，说明该内容在其他分支已处理过，直接跳过，不重复调用 Agent
- 大文件自动分块：business_description 超过 2048 tokens 时，按垂直分块策略拆分为多个 points，通过 file_version_id 关联

### 4.5 各 Git 操作归类

| Git 操作 | HEAD 变化 | 触发的记忆操作 |
|----------|----------|--------------|
| **git checkout \<branch\>** | ✅ 分支名变 | **操作一：视图切换** |
| **git commit** | ✅ commit hash 变 | **操作二：知识生产** |
| **git pull** | ✅ commit hash 变 | **操作二：知识生产** |
| **git merge** | ✅ commit hash 变 | **操作二：知识生产** |
| **git rebase** | ✅ commit hash 变 | **操作二：知识生产** |
| **git commit --amend** | ✅ commit hash 变 | **操作二：知识生产** |
| **git add** | ❌ | 无 |
| **git push** | ❌ | 无 |
| **文件编辑（未提交）** | ❌ | 无 |

**HEAD 没变 = RAG 不动。分支名变了 = 操作一。commit hash 变了 = 操作二。**

### 4.6 事件监听（Git Hooks 驱动）

**前置条件：项目根目录必须存在 `.git/` 目录，否则不启动 codebase 记忆。本系统专为 Git 项目设计。**

Agent 启动时：检查 `.git/` 是否存在 → 存在则注入 hook 脚本到 `.git/hooks/` → 执行一次全量初始化构建。

**Git 原生支持在特定操作完成后执行脚本（hooks），这是事件驱动，不是轮询。** 不管用户通过命令行、IDE 还是 GUI 工具执行 Git 操作，hook 都会触发。

需要注册的 4 个 hooks，完整覆盖所有会改变 HEAD 的操作：

| Hook | 触发时机 | 覆盖的 Git 操作 |
|------|---------|----------------|
| `post-commit` | commit 完成后 | git commit |
| `post-merge` | merge 完成后 | git merge, git pull（pull = fetch + merge） |
| `post-checkout` | checkout 完成后 | git checkout, git switch, git restore |
| `post-rewrite` | 历史改写完成后 | git rebase, git commit --amend |

Hook 脚本做的事情很简单：通知 Python 进程"HEAD 变了"。通知方式可以是：
- 往本地端口发一个 HTTP 请求（FastAPI / Flask 端点）
- 写一个信号文件，Python 进程用 watchdog 或 watchfiles 监听
- 通过 Unix Domain Socket / Named Pipe 发消息

**兜底机制**：用 watchdog 或 watchfiles 库监听 `.git/HEAD` 和 `.git/refs/heads/` 文件变化，覆盖用户绕过 hook 的场景（如直接用其他 Git 工具操作）。

```
主触发：Git Hooks（精准、零延迟、覆盖所有标准 Git 操作）
兜底：  watchdog 或 watchfiles 监听 .git/ 目录（覆盖非标准场景）

Python 进程收到通知后：
  → 读取当前 HEAD commit hash
  → 与上一次记录对比
  → 判断分支名是否变化
  → 分支名变了 → 操作一（视图切换，4.3）
  → commit hash 变了 → 操作二（知识生产，4.4）
  → 无变化 → 忽略
```

### 4.7 同步耗时

| 场景 | 类型 | 变更规模 | 耗时 |
|------|------|---------|------|
| **git checkout（缓存命中）** | 操作一 | — | ~0ms |
| **git checkout（首次）** | 操作一 | 全量 10k 文件 | ~500ms-1s |
| **git commit** | 操作二 | 改了 3 个文件 | ~数秒（Agent 生成业务描述 + embedding） |
| **git pull** | 操作二 | 远端改了 20 个文件 | ~十数秒（批量 Agent 处理） |
| **git merge** | 操作二 | 合并改了 50 个文件 | ~数十秒（批量 Agent 处理） |
| **git rebase（无内容变化）** | 操作二 | 0 个文件实际变化 | ~0ms（hash 全部命中 Qdrant，跳过 Agent） |

---

## 五、全局 Embedding 缓存的 GC（垃圾回收）

Qdrant 中的 embedding 以 content_hash 为 key，随着分支迭代会积累不再被任何分支引用的孤儿数据。

**GC 策略：**

- 定期获取所有活跃分支的文件 content_hash 集合（通过 git for-each-ref + git ls-tree）
- Qdrant 中不在任何活跃分支引用的 content_hash → 标记为可回收
- 超过 N 天未被引用 → 删除
- 或者更简单：设置 Qdrant Collection 的容量上限 + LRU 淘汰

---

## 六、为什么不在 Qdrant 上直接做分支隔离？

| 方案 | 问题 |
|------|------|
| **Collection-per-branch** | 95% 数据冗余，存储爆炸 |
| **branches 标签过滤** | 创建分支时需全量更新标签，维护复杂度极高 |
| **单池混合搜索+结果过滤** | 搜出大量错误版本的幽灵数据，结果污染 |

**根因**：Qdrant 的搜索粒度是整个 Collection 或按 payload 过滤，无法低成本地表达"这次搜索只看这些 point"这种动态视图。

而 qdrant-client in-memory 实例天然就是一个动态视图 — 里面装了什么就搜什么，没有多余数据。

---

## 七、方案全景图

```
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│   Qdrant Server (持久化层)                                        │
│   ┌────────────────────────────────────────────────────────────┐ │
│   │ content_hash → {                                           │ │
│   │   vector: dense + sparse (基于业务描述文本),                 │ │
│   │   payload: { business_description, file_path, lang, ... }  │ │
│   │ }                                                          │ │
│   │                                                            │ │
│   │ 全量去重存储，只按 hash 精确存取，不做语义搜索                 │ │
│   │ Point 内容 = Agent 升维生成的业务描述知识点（非源码）          │ │
│   └────────────────────────────────────────────────────────────┘ │
│                            │                                     │
│               按 hash 列表批量提取                                 │
│                            │                                     │
│   ┌────────────────────────┼─────────────────────────────────┐   │
│   │          内存实例缓存池 (dict + LRU)                      │   │
│   │                        │                                 │   │
│   │  ┌──────────────┐ ┌───┴──────────┐ ┌──────────────┐     │   │
│   │  │ main@a1b2c3  │ │feat-pay@d4e5 │ │hotfix@f6g7   │     │   │
│   │  │              │ │  ← 当前活跃   │ │              │     │   │
│   │  │qdrant:memory:│ │qdrant:memory:│ │qdrant:memory:│     │   │
│   │  │ ~55MB        │ │ ~55MB        │ │ ~55MB        │     │   │
│   │  │              │ │              │ │              │     │   │
│   │  │ 稠密+稀疏     │ │ 稠密+稀疏     │ │ 稠密+稀疏     │     │   │
│   │  │ +payload过滤  │ │ +payload过滤  │ │ +payload过滤  │     │   │
│   │  └──────────────┘ └──────────────┘ └──────────────┘     │   │
│   │                                                         │   │
│   │  切分支 = 切 dict 引用 (0ms)                              │   │
│   │  新分支 = 从 Qdrant Server 提取 + 构建 (~500ms-1s)        │   │
│   │  LRU 淘汰控制总内存 (默认 5 个分支 ≈ 275MB)              │   │
│   └─────────────────────────────────────────────────────────┘   │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

---

## 八、总结

| 维度 | 方案 |
|------|------|
| **Point 内容** | Agent 升维生成的自然语言业务描述知识点（5 层结构化模板，非源码片段） |
| **Point ID** | Qdrant 自动生成 UUID，无需自定义 |
| **file_version_id** | `hash(filePath + blobHash)`，同一文件版本所有分块的关联 ID，用于批量查询/删除 |
| **业务描述格式** | 5 层结构：项目结构域 → 业务域 → 功能描述 → 目的描述 → 业务逻辑细节 |
| **大文件分块** | 垂直分块（每块保留定位信息），单块 ≤ 2048 tokens |
| **存储** | Qdrant Server Collection `codebase`，内容寻址去重，多项目共享，只存不查 |
| **检索** | qdrant-client in-memory 实例，分支专属，按需组装，只查不存 |
| **操作一：分支切换** | 视图切换 — diff file_version_id 集合 → 增删 points（含全部分块），不调用 Agent |
| **操作二：内容提交** | 知识生产 — 新建(从零生成) / 修改(旧描述+diff增量更新) / 删除(移除 in-memory 实例) |
| **切回旧分支** | dict 缓存命中，零成本切换 |
| **事件驱动** | Git Hooks（post-commit / post-merge / post-checkout / post-rewrite），非轮询 |
| **数据冗余** | 零（内容寻址去重） |
| **幽灵数据** | 零（in-memory 实例只含当前分支） |
| **GC** | 定期清理无分支引用的孤儿 embedding |

**一句话总结**：Qdrant Server 是跨分支共享的知识仓库（只存不查），qdrant-client in-memory 实例是分支专属的搜索视图（只查不存）。两类操作：分支切换 = 视图切换（按 file_version_id 搬运已有 points，亚秒级）；内容提交 = 知识生产（Agent 按 5 层模板升维 → 垂直分块 → embedding → 入库）。零冗余，零幽灵数据。
