# AGENTS.md

本文档约定 Codebase RAG 在 Windows 本地运行场景下的开发、测试、提交、打包、本地安装与用户端更新流程。目标用户主要在 Windows 本地长期运行服务，因此所有运维脚本集中放在 `ops/windows/`，本文档只说明脚本用途和调用方式，不内嵌脚本源码。

## 1. 目录约定

### 1.1 Git 仓库内容

Git 仓库保存源码、配置模板、构建脚本、运维脚本和文档：

```text
src/                         # Python 源码
tests/                       # 测试代码
pyproject.toml               # Python 包与依赖定义
config.example.yaml          # 配置模板，不包含真实密钥
.env.example                 # 环境变量模板，不包含真实密钥
ops/windows/                 # Windows 本地服务运维脚本
AGENTS.md                    # 本文件
```

Git 仓库不保存：

```text
.env
config.yaml
data/
logs/
.venv/
.venv-build/
dist/
build/
*.zip
用户本地项目仓库
真实 API Key
```

### 1.2 Windows 正式安装目录

正式用户本地服务按“程序目录”和“数据目录”分离：

```text
${CODEBASE_RAG_PROGRAM_DIR}/
  current/                    # 当前版本 junction，指向 releases/<version>
  releases/
    <version>/
      .venv/
      packages/
      scripts/
      VERSION
  service/
    run-service.ps1

${CODEBASE_RAG_DATA_DIR}/
  config/
    config.yaml
    .env
  data/
  logs/
  backups/
  runtime/
  downloads/
```

`${CODEBASE_RAG_PROGRAM_DIR}` 可以随版本替换；`${CODEBASE_RAG_DATA_DIR}` 是用户配置、数据、日志和备份目录，更新时不得覆盖。两者均由 `.env` 中的同名变量指定，用户可自定义路径。

## 2. Windows 运维脚本目录

所有 Windows 运维脚本位于：

```text
ops/windows/
```

脚本清单：

```text
common.ps1                    # 公共函数，被其他脚本 dot-source 引用
install-local-service.ps1     # 从本地安装包或源码目录安装程序文件、venv、配置模板和 run-service.ps1
register-local-service.ps1    # 使用 NSSM 注册 Windows Service
unregister-local-service.ps1  # 卸载 Windows Service
start-local-service.ps1       # 启动正式服务并执行健康检查
stop-local-service.ps1        # 停止正式服务
restart-local-service.ps1     # 重启正式服务并执行健康检查
doctor-local-service.ps1      # 本地诊断 Python/Git/目录/端口/健康检查
start-source-takeover.ps1     # 停正式服务，用本地最新源码接管正式配置启动测试
push-source-to-remote.ps1     # 提交并推送源码到远程 Git 仓库
build-windows-package.ps1     # 从本地源码构建 Windows 本地安装 zip
update-from-zip.ps1           # 使用本地构建 zip 更新本地服务，失败回滚
```

需要管理员权限的脚本必须在“以管理员身份运行”的 PowerShell 中执行。

## 3. `.env` 资源文件与脚本参数

### 3.1 `.env` 只放通用稳定资源

`.env` 提供不常变化的资源配置：本地路径、API Key、服务名、远程仓库身份等。用户拿到源码后只需填写一次。

源码仓库模板：

```powershell
Copy-Item .\.env.example .\.env
notepad .\.env
```

运行服务必填：

```text
CODEBASE_RAG_DATA_DIR
QDRANT_API_KEY
DASHSCOPE_API_KEY
GEMINI_EMBED_KEY_1
```

同步源码到远程 Git 仓库可选填写：

```text
CODEBASE_RAG_GIT_REMOTE
CODEBASE_RAG_GIT_BRANCH
```

脚本读取 `.env` 的顺序：

1. 显式传入 `-EnvFile <path>`。
2. 环境变量 `CODEBASE_RAG_ENV_FILE`。
3. 当前执行目录下的 `.env`。
4. 源码根目录或本地安装包根目录下的 `.env`。

缺少必填资源变量时，脚本直接失败并提示缺少的变量名。

### 3.2 动态参数通过脚本参数传入

每次推送、构建、更新时会变化的值（版本号、tag、提交消息、zip 路径等）不放 `.env`，由脚本参数显式传入：

- **`-Version`**：本地安装包版本号，构建时必传。
- **`-TagName`**：Git tag 名称，需要创建源码版本 tag 时传入。
- **`-CommitMessage`**：提交消息，有未提交修改时必传。
- **`-CreateTag`**：开关，推送时是否创建 tag。
- **`-PackageZip`**：本地构建 zip 路径，更新时必传。

## 4. 前置依赖

开发者和正式用户本机需要：

```text
Windows 10/11
PowerShell 5.1+
Python 3.11+
Git for Windows
NSSM，用于注册 Windows Service
```

`nssm.exe` 需要在 PATH 中，或调用脚本时通过 `-NssmExe` 指定完整路径。

## 5. 开发完成后的本地源码接管测试

用途：开发者本地已经运行正式服务，但需要停掉正式服务，用最新源码接管正式配置和正式端口进行真实环境验证。

### 5.1 执行前要求

- 当前源码已完成修改。
- 确认修改不会破坏正式数据。
- 如涉及持久化结构变更，先备份 `${CODEBASE_RAG_DATA_DIR}`。
- 不要让正式服务和源码实例同时监听同一项目仓库。

### 5.2 启动源码接管测试

在仓库根目录执行：

```powershell
.\ops\windows\start-source-takeover.ps1 -EnvFile .\.env
```

指定正式配置路径：

```powershell
.\ops\windows\start-source-takeover.ps1 -EnvFile .\.env -ConfigPath "${CODEBASE_RAG_DATA_DIR}\config\config.yaml"
```

安装开发依赖后接管启动：

```powershell
.\ops\windows\start-source-takeover.ps1 -EnvFile .\.env -InstallDevDependencies
```

脚本会执行：

1. 停止正式 Windows Service。
2. 创建或复用源码目录 `.venv`。
3. 执行 editable install。
4. 使用正式 `config.yaml` 启动 `python -m codebase_rag`。
5. 该终端窗口作为源码服务进程，按 `Ctrl+C` 停止。

### 5.3 测试内容

至少验证：

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:18080/api/v1/health
```

同时验证：

- Management API 是否启动。
- MCP Server 是否启动，默认 `http://127.0.0.1:19090/mcp`。
- 已注册项目是否可恢复。
- 搜索是否正常。
- Git hook / watcher 是否正常。
- 配置 reload 是否正常。
- 日志是否正常。

### 5.4 恢复正式服务

停止源码实例：

```text
Ctrl+C
```

恢复正式服务：

```powershell
.\ops\windows\start-local-service.ps1 -EnvFile .\.env
```

如果需要重启正式服务：

```powershell
.\ops\windows\restart-local-service.ps1 -EnvFile .\.env
```

## 6. 代码提交到远程 Git 仓库

### 6.1 手动 Git 流程

```powershell
git status --short
git add -A
git commit -m "描述本次修改"
git push -u origin master
```

如果需要打 tag：

```powershell
git tag v0.1.1
git push origin v0.1.1
```

### 6.2 使用脚本提交推送

提交并推送当前分支：

```powershell
.\ops\windows\push-source-to-remote.ps1 -EnvFile .\.env
```

指定远程和分支：

```powershell
.\ops\windows\push-source-to-remote.ps1 -EnvFile .\.env -RemoteName origin -Branch master -CommitMessage "描述本次修改"
```

提交、推送并创建源码版本 tag：

```powershell
.\ops\windows\push-source-to-remote.ps1 -EnvFile .\.env -CommitMessage "version 0.2.0" -CreateTag -TagName v0.2.0
```

## 7. 从本地源码构建 Windows 安装包

Gitee 私有仓库 Release 附件不作为命令行交付渠道。正式用户安装和更新时，统一在本机拉取源码后构建本地安装包，再从本地 zip 安装或更新。

### 7.1 获取或更新源码

首次获取源码：

```powershell
git clone <repo-url> codebase-rag
```

已有源码时更新：

```powershell
git pull --ff-only
```

需要安装指定版本时，切换到对应 tag 或 commit：

```powershell
git checkout v0.2.0
```

### 7.2 构建命令

在源码仓库根目录执行：

```powershell
.\ops\windows\build-windows-package.ps1 -EnvFile .\.env -Version 0.2.0
```

跳过测试构建：

```powershell
.\ops\windows\build-windows-package.ps1 -EnvFile .\.env -Version 0.2.0 -SkipTests
```

### 7.3 构建输出

默认输出到：

```text
dist\packages\CodebaseRAG-<version>-windows-x64.zip
dist\packages\CodebaseRAG-<version>-windows-x64.zip.sha256
```

本地安装 zip 内包含：

```text
VERSION
package-manifest.json
config.example.yaml
.env.example
packages\codebase_rag-<version>-py3-none-any.whl
wheels\*.whl
scripts\*.ps1
```

本地安装 zip 不包含：

```text
.git/
src/
tests/
.env
config.yaml
data/
logs/
.venv/
.venv-build/
```

## 8. 用户端首次安装

### 8.1 准备 `.env`

用户在源码仓库根目录准备真实 `.env`：

```powershell
Copy-Item .\.env.example .\.env
notepad .\.env
```

必须填写：

- `CODEBASE_RAG_DATA_DIR`：本地数据存储目录路径。
- `QDRANT_API_KEY`：Qdrant 向量数据库 API Key。
- `DASHSCOPE_API_KEY`：阿里云 DashScope API Key（Embedding 用）。
- `GEMINI_EMBED_KEY_1`：Google Gemini Embedding API Key。
- 至少一个 Agent LLM Key（如 `ZHIPU_KEY_1`、`GEMINI_KEY_1` 等）。

没有的 Key 直接删掉该行，不要留空占位。

### 8.2 构建并解压本地安装包

在源码仓库根目录执行：

```powershell
.\ops\windows\build-windows-package.ps1 -EnvFile .\.env -Version 0.2.0
Expand-Archive -Path .\dist\packages\CodebaseRAG-0.2.0-windows-x64.zip -DestinationPath .\dist\packages\CodebaseRAG-0.2.0-windows-x64 -Force
```

### 8.3 安装并启动服务

在解压后的本地安装包目录执行：

```powershell
.\scripts\install-local-service.ps1 -EnvFile <path-to-real-env>
.\scripts\register-local-service.ps1 -EnvFile <path-to-real-env>
.\scripts\start-local-service.ps1 -EnvFile <path-to-real-env>
```

如果 `.env` 就在源码仓库根目录，示例：

```powershell
.\scripts\install-local-service.ps1 -EnvFile D:\project\codebase-rag\.env
.\scripts\register-local-service.ps1 -EnvFile D:\project\codebase-rag\.env
.\scripts\start-local-service.ps1 -EnvFile D:\project\codebase-rag\.env
```

首次安装会复制 `.env` 到正式配置目录：

```text
${CODEBASE_RAG_DATA_DIR}\config\.env
```

用户只维护真实 `.env` 资源文件；默认不需要修改脚本。只有需要调整服务内部参数时，才编辑：

```text
${CODEBASE_RAG_DATA_DIR}\config\config.yaml
```

诊断：

```powershell
.\scripts\doctor-local-service.ps1 -EnvFile <path-to-real-env>
```

## 9. 用户端本地构建 zip 更新服务

用户在源码仓库更新到目标版本后，重新构建本地安装 zip：

```powershell
git pull --ff-only
.\ops\windows\build-windows-package.ps1 -EnvFile .\.env -Version 0.2.1
```

在已安装版本或新 zip 解压目录中执行：

```powershell
.\scripts\update-from-zip.ps1 -EnvFile <path-to-real-env> -PackageZip "D:\project\codebase-rag\dist\packages\CodebaseRAG-0.2.1-windows-x64.zip"
```

脚本会执行：

1. 读取当前版本。
2. 解压本地构建 zip。
3. 备份 `${CODEBASE_RAG_DATA_DIR}\config`、`data`、`runtime`。
4. 停止正式服务。
5. 安装新版本到 `${CODEBASE_RAG_PROGRAM_DIR}\releases\<new-version>`。
6. 切换 `current`。
7. 启动服务。
8. 访问健康检查。
9. 如果失败，切回旧版本并重新启动服务。

## 10. 日常运维命令

查看服务：

```powershell
Get-Service CodebaseRAG
```

启动服务：

```powershell
.\ops\windows\start-local-service.ps1 -EnvFile .\.env
```

停止服务：

```powershell
.\ops\windows\stop-local-service.ps1 -EnvFile .\.env
```

重启服务：

```powershell
.\ops\windows\restart-local-service.ps1 -EnvFile .\.env
```

诊断服务：

```powershell
.\ops\windows\doctor-local-service.ps1 -EnvFile .\.env
```

手动健康检查：

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:18080/api/v1/health
```

查看端口：

```powershell
Get-NetTCPConnection -LocalPort 18080 -State Listen
Get-NetTCPConnection -LocalPort 19090 -State Listen
```

查看日志：

```powershell
Get-Content "${CODEBASE_RAG_DATA_DIR}\logs\service.stdout.log" -Tail 200 -Wait
Get-Content "${CODEBASE_RAG_DATA_DIR}\logs\service.stderr.log" -Tail 200 -Wait
```

卸载服务注册，不删除用户数据：

```powershell
.\ops\windows\unregister-local-service.ps1 -EnvFile .\.env
```

## 11. 关键约束

- 正式服务默认监听 `127.0.0.1`，不要默认暴露到局域网。
- 用户真实 `.env`、真实配置、密钥、日志、数据不得提交到 Git。
- 更新不得覆盖 `${CODEBASE_RAG_DATA_DIR}\config\.env` 和 `config.yaml`。
- 正式安装和更新必须使用用户本机从源码构建出的本地安装 zip。
- 不再通过 Gitee Release 发布或命令行下载安装包。
- 本地安装 zip 只交付运行所需 wheel、依赖 wheel、配置模板和运维脚本。
- 开发接管测试必须先停正式服务，避免两个实例同时监听同一端口或同一 Git 仓库。
- 涉及持久化结构变更时，必须先备份再接管测试或更新。
