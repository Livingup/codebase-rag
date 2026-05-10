# AGENTS.md

本文档约定 Codebase RAG 在 Windows 本地运行场景下的开发、测试、提交、打包、发布与用户端更新流程。目标用户主要在 Windows 本地长期运行服务，因此所有运维脚本集中放在 `ops/windows/`，本文档只说明脚本用途和调用方式，不内嵌脚本源码。

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
dist/
build/
*.zip
用户本地项目仓库
真实 API Key
```

### 1.2 Windows 正式安装目录

正式用户本地服务按“程序目录”和“数据目录”分离：

```text
C:\Program Files\CodebaseRAG\
  current\                    # 当前版本 junction，指向 releases/<version>
  releases\
    <version>\
      .venv\
      packages\
      scripts\
      VERSION
  service\
    run-service.ps1

C:\ProgramData\CodebaseRAG\
  config\
    config.yaml
    .env
  data\
  logs\
  backups\
  runtime\
  downloads\
```

`C:\Program Files\CodebaseRAG` 可以随版本替换；`C:\ProgramData\CodebaseRAG` 是用户配置、数据、日志和备份目录，更新时不得覆盖。

## 2. Windows 运维脚本目录

所有 Windows 运维脚本位于：

```text
ops/windows/
```

脚本清单：

```text
common.ps1                    # 公共函数，被其他脚本 dot-source 引用
install-local-service.ps1     # 从发布包或源码目录安装程序文件、venv、配置模板和 run-service.ps1
register-local-service.ps1    # 使用 NSSM 注册 Windows Service
unregister-local-service.ps1  # 卸载 Windows Service
start-local-service.ps1       # 启动正式服务并执行健康检查
stop-local-service.ps1        # 停止正式服务
restart-local-service.ps1     # 重启正式服务并执行健康检查
doctor-local-service.ps1      # 本地诊断 Python/Git/目录/端口/健康检查
start-source-takeover.ps1     # 停正式服务，用本地最新源码接管正式配置启动测试
push-source-to-remote.ps1     # 提交并推送源码到远程 Git 仓库
build-windows-release.ps1     # 构建 Windows 发布 zip
publish-gitee-release.ps1     # 发布 Windows zip 和 sha256 到 Gitee Release
update-from-zip.ps1           # 用户端用最新 zip 更新本地服务，失败回滚
```

需要管理员权限的脚本必须在“以管理员身份运行”的 PowerShell 中执行。

## 3. `.env` 资源文件与脚本参数

### 3.1 `.env` 只放通用稳定资源

`.env` 提供不常变化的资源配置：本地路径、API Key、服务名、远程仓库身份等。用户拿到源码或 zip 后只需填写一次。

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

发布到 Gitee 还需填写：

```text
CODEBASE_RAG_GIT_REMOTE
CODEBASE_RAG_GIT_BRANCH
GITEE_OWNER
GITEE_REPO
GITEE_TOKEN
```

脚本读取 `.env` 的顺序：

1. 显式传入 `-EnvFile <path>`。
2. 环境变量 `CODEBASE_RAG_ENV_FILE`。
3. 当前执行目录下的 `.env`。
4. 源码根目录或发布包根目录下的 `.env`。

缺少必填资源变量时，脚本直接失败并提示缺少的变量名。

### 3.2 动态参数通过脚本参数传入

每次发布、推送、更新时会变化的值（版本号、tag、提交消息、zip 路径等）不放 `.env`，由脚本参数显式传入：

- **`-Version`**：发布版本号，构建和发布时必传。
- **`-TagName`**：Git tag 名称，不传时默认为 `v<Version>`。
- **`-CommitMessage`**：提交消息，有未提交修改时必传。
- **`-CreateTag`**：开关，推送时是否创建 tag。
- **`-ReleaseName`**：Gitee Release 标题，不传时默认为 `CodebaseRAG <Version>`。
- **`-ReleaseBody`**：Gitee Release 正文，不传时默认为 `CodebaseRAG Windows release <Version>`。
- **`-PackageZip`**：更新包 zip 路径，更新时必传。

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
- 如涉及持久化结构变更，先备份 `C:\ProgramData\CodebaseRAG`。
- 不要让正式服务和源码实例同时监听同一项目仓库。

### 5.2 启动源码接管测试

在仓库根目录执行：

```powershell
.\ops\windows\start-source-takeover.ps1 -EnvFile .\.env
```

指定正式配置路径：

```powershell
.\ops\windows\start-source-takeover.ps1 -EnvFile .\.env -ConfigPath "C:\ProgramData\CodebaseRAG\config\config.yaml"
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

提交、推送并创建版本 tag：

```powershell
.\ops\windows\push-source-to-remote.ps1 -EnvFile .\.env -CommitMessage "release 0.2.0" -CreateTag -TagName v0.2.0
```

## 7. 构建 Windows 发布包

### 7.1 构建命令

在仓库根目录执行：

```powershell
.\ops\windows\build-windows-release.ps1 -EnvFile .\.env -Version 0.2.0
```

跳过测试构建：

```powershell
.\ops\windows\build-windows-release.ps1 -EnvFile .\.env -Version 0.2.0 -SkipTests
```

### 7.2 构建输出

默认输出到：

```text
dist\release\CodebaseRAG-<version>-windows-x64.zip
dist\release\CodebaseRAG-<version>-windows-x64.zip.sha256
```

发布 zip 内包含：

```text
VERSION
release-manifest.json
config.example.yaml
.env.example
packages\codebase_rag-<version>-py3-none-any.whl
wheels\*.whl
scripts\*.ps1
```

发布 zip 不包含：

```text
.git/
src/
tests/
.env
config.yaml
data/
logs/
.venv/
```

## 8. 发布流程

固定发布目标：Gitee Release。

发布前必须确认 `.env` 已填写 Gitee 身份资源：

```text
GITEE_OWNER
GITEE_REPO
GITEE_TOKEN
```

确定发布步骤：

1. 开发完成后执行源码接管测试。
2. 提交并推送源码到 Gitee 远程仓库。
3. 创建并推送版本 tag。
4. 构建 Windows zip 和 sha256。
5. 在干净 Windows 环境中安装测试。
6. 发布 zip 和 sha256 到 Gitee Release。
7. 打开 Gitee Release 页面确认附件可下载。

示例（以 0.2.0 为例）：

```powershell
.\ops\windows\start-source-takeover.ps1 -EnvFile .\.env -InstallDevDependencies
.\ops\windows\push-source-to-remote.ps1 -EnvFile .\.env -CommitMessage "release 0.2.0" -CreateTag -TagName v0.2.0
.\ops\windows\build-windows-release.ps1 -EnvFile .\.env -Version 0.2.0
.\ops\windows\publish-gitee-release.ps1 -EnvFile .\.env -Version 0.2.0
```

## 9. 用户端首次安装

用户下载并解压：

```text
CodebaseRAG-<version>-windows-x64.zip
```

在解压目录执行：

```powershell
Copy-Item .\.env.example .\.env
notepad .\.env
.\scripts\install-local-service.ps1 -EnvFile .\.env
.\scripts\register-local-service.ps1 -EnvFile .\.env
.\scripts\start-local-service.ps1 -EnvFile .\.env
```

首次安装会复制 `.env` 到正式配置目录：

```text
C:\ProgramData\CodebaseRAG\config\.env
```

用户只维护真实 `.env` 资源文件；默认不需要修改脚本。只有需要调整服务内部参数时，才编辑：

```text
C:\ProgramData\CodebaseRAG\config\config.yaml
```

诊断：

```powershell
.\scripts\doctor-local-service.ps1 -EnvFile .\.env
```

## 10. 用户端下载最新 zip 更新本地服务

用户下载最新：

```text
CodebaseRAG-<new-version>-windows-x64.zip
```

在已安装版本或新 zip 解压目录中执行：

```powershell
.\scripts\update-from-zip.ps1 -EnvFile .\.env -PackageZip "D:\Downloads\CodebaseRAG-<new-version>-windows-x64.zip"
```

脚本会执行：

1. 读取当前版本。
2. 解压新 zip。
3. 备份 `C:\ProgramData\CodebaseRAG\config`、`data`、`runtime`。
4. 停止正式服务。
5. 安装新版本到 `C:\Program Files\CodebaseRAG\releases\<new-version>`。
6. 切换 `current`。
7. 启动服务。
8. 访问健康检查。
9. 如果失败，切回旧版本并重新启动服务。

## 11. 日常运维命令

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
Get-Content "C:\ProgramData\CodebaseRAG\logs\service.stdout.log" -Tail 200 -Wait
Get-Content "C:\ProgramData\CodebaseRAG\logs\service.stderr.log" -Tail 200 -Wait
```

卸载服务注册，不删除用户数据：

```powershell
.\ops\windows\unregister-local-service.ps1 -EnvFile .\.env
```

## 12. 关键约束

- 正式服务默认监听 `127.0.0.1`，不要默认暴露到局域网。
- 用户真实 `.env`、真实配置、密钥、日志、数据不得提交到 Git。
- 更新不得覆盖 `C:\ProgramData\CodebaseRAG\config\.env` 和 `config.yaml`。
- 发布包只交付运行所需 wheel、依赖 wheel、配置模板和运维脚本。
- 开发接管测试必须先停正式服务，避免两个实例同时监听同一端口或同一 Git 仓库。
- 涉及持久化结构变更时，必须先备份再接管测试或更新。
