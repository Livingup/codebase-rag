param(
    [string]$EnvFile,
    [string]$PackageRoot,
    [string]$DataDir,
    [string]$ServiceName,
    [string]$HealthUrl,
    [string]$ReloadUrl,
    [int]$HealthTimeoutSeconds = 60,
    [switch]$SyncTemplate,
    [switch]$SkipReload,
    [switch]$Restart,
    [switch]$AutoRestart
)

. "$PSScriptRoot\common.ps1"

function Get-DefaultPackageRoot {
    $sourceRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..") -ErrorAction SilentlyContinue
    if ($sourceRoot -and (Test-Path (Join-Path $sourceRoot.Path "config.example.yaml"))) {
        return $sourceRoot.Path
    }

    $packageRoot = Resolve-Path (Join-Path $PSScriptRoot "..") -ErrorAction SilentlyContinue
    if ($packageRoot -and (Test-Path (Join-Path $packageRoot.Path "config.example.yaml"))) {
        return $packageRoot.Path
    }

    return (Resolve-Path ".").Path
}

if (-not $PackageRoot) {
    $PackageRoot = Get-DefaultPackageRoot
}
else {
    $PackageRoot = (Resolve-Path $PackageRoot).Path
}

Assert-Administrator

$resolvedDataDir = Get-CodebaseRagDataDir $DataDir
$configDir = Join-Path $resolvedDataDir "config"
$configPath = Join-Path $configDir "config.yaml"
$envPath = Join-Path $configDir ".env"
$configTemplatePath = Join-Path $PackageRoot "config.example.yaml"
$envTemplatePath = Join-Path $PackageRoot ".env.example"

Ensure-Directory $configDir

# 1. 运行时 config.yaml 维护策略：
# - 若现场配置不存在：从代码仓库复制通用模板作为初始底稿 (Seed)
# - 若现场配置已存在：默认保留现场配置，仅当显式传入 -SyncTemplate 时才覆盖
if (-not (Test-Path $configPath)) {
    if (-not (Test-Path $configTemplatePath)) {
        throw "Config template does not exist: $configTemplatePath"
    }
    Copy-Item -Path $configTemplatePath -Destination $configPath -Force
    Write-Host "Config initialized from template: $configPath"
} elseif ($SyncTemplate) {
    if (-not (Test-Path $configTemplatePath)) {
        throw "Config template does not exist: $configTemplatePath"
    }
    Copy-Item -Path $configTemplatePath -Destination $configPath -Force
    Write-Host "Config overwritten from template (-SyncTemplate): $configPath"
} else {
    Write-Host "Using live runtime configuration: $configPath"
}

# 2. 运行时 .env 维护策略：
# - 若显式传入 -EnvFile：同步该文件至数据目录并校正 CODEBASE_RAG_DATA_DIR
# - 若未传入 -EnvFile 且现场 .env 不存在：从 .env.example 复制种子
# - 若未传入 -EnvFile 且现场 .env 已存在：完全保留现场私有凭据，绝不覆盖
if ($EnvFile) {
    $resolvedEnvFile = Resolve-CodebaseRagEnvFile -EnvFile $EnvFile
    $resolvedEnvSource = (Resolve-Path $resolvedEnvFile).Path
    $resolvedEnvTarget = $envPath
    if (Test-Path $envPath) {
        $resolvedEnvTarget = (Resolve-Path $envPath).Path
    }
    if ([String]::Compare($resolvedEnvSource, $resolvedEnvTarget, $true) -ne 0) {
        Copy-Item -Path $resolvedEnvSource -Destination $envPath -Force
    }
    $envDataDir = $resolvedDataDir -replace "\\", "/"
    $envContent = Get-Content -Path $envPath -Raw
    if ($envContent -match "(?m)^CODEBASE_RAG_DATA_DIR=") {
        $envContent = $envContent -replace "(?m)^CODEBASE_RAG_DATA_DIR=.*$", "CODEBASE_RAG_DATA_DIR=$envDataDir"
    } else {
        $envContent = "CODEBASE_RAG_DATA_DIR=" + $envDataDir + [Environment]::NewLine + $envContent
    }
    Set-Content -Path $envPath -Value $envContent -Encoding UTF8
    Write-Host "Environment refreshed from source: $envPath"
} else {
    if ((-not (Test-Path $envPath)) -and (Test-Path $envTemplatePath)) {
        Copy-Item -Path $envTemplatePath -Destination $envPath -Force
        Write-Host "Environment initialized from .env.example seed: $envPath"
    } else {
        Write-Host "Using live runtime environment: $envPath"
    }
}

$ServiceName = Get-CodebaseRagServiceName $ServiceName
$HealthUrl = Get-CodebaseRagHealthUrl $HealthUrl
if (-not $ReloadUrl) {
    $ReloadUrl = $HealthUrl -replace "/health$", "/config/reload"
}

if ($Restart) {
    Stop-CodebaseRagService -ServiceName $ServiceName -TimeoutSeconds $HealthTimeoutSeconds
    Start-CodebaseRagService -ServiceName $ServiceName -TimeoutSeconds $HealthTimeoutSeconds
    if (-not (Wait-CodebaseRagHealth -HealthUrl $HealthUrl -TimeoutSeconds $HealthTimeoutSeconds)) {
        throw "Service restarted, but health check failed: $HealthUrl"
    }
    Write-Host "Service restarted and health check passed: $ServiceName"
    return
}

if ($SkipReload) {
    Write-Host "Config reload skipped."
    return
}

$service = Get-ServiceIfExists $ServiceName
if (-not $service -or $service.Status -ne "Running") {
    Write-Host "Config hot reload skipped because service is not running: $ServiceName"
    return
}

$response = Invoke-WebRequest -UseBasicParsing -Method Post -Uri $ReloadUrl -TimeoutSec $HealthTimeoutSeconds
if ($response.StatusCode -lt 200 -or $response.StatusCode -ge 300) {
    throw "Config hot reload failed: $ReloadUrl"
}

try {
    $result = $response.Content | ConvertFrom-Json
    if ($result.reconciled_services) {
        Write-Host "Reconciled services: $($result.reconciled_services -join ', ')"
    }
    if ($result.requires_restart) {
        Write-Warning "Configuration changes require service restart to take full effect: $($result.restart_reasons -join '; ')"
        if ($AutoRestart) {
            Write-Host "AutoRestart is enabled. Restarting service..."
            Stop-CodebaseRagService -ServiceName $ServiceName -TimeoutSeconds $HealthTimeoutSeconds
            Start-CodebaseRagService -ServiceName $ServiceName -TimeoutSeconds $HealthTimeoutSeconds
            if (-not (Wait-CodebaseRagHealth -HealthUrl $HealthUrl -TimeoutSeconds $HealthTimeoutSeconds)) {
                throw "Service restarted, but health check failed: $HealthUrl"
            }
            Write-Host "Service restarted and health check passed: $ServiceName"
            return
        }
        else {
            Write-Host "Tip: Run with -Restart or -AutoRestart to apply restart-required changes."
        }
    }
}
catch {
    # If response is not JSON or parsing fails, fallback
}

if (-not (Wait-CodebaseRagHealth -HealthUrl $HealthUrl -TimeoutSeconds $HealthTimeoutSeconds)) {
    throw "Config hot reload completed, but health check failed: $HealthUrl"
}
Write-Host "Config hot reload completed and health check passed: $ReloadUrl"
