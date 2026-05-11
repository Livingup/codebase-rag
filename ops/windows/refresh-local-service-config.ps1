param(
    [string]$EnvFile,
    [string]$PackageRoot,
    [string]$DataDir,
    [string]$ServiceName,
    [string]$HealthUrl,
    [string]$ReloadUrl,
    [int]$HealthTimeoutSeconds = 60,
    [switch]$SkipReload,
    [switch]$Restart
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

$resolvedEnvFile = $null
if ($EnvFile) {
    $resolvedEnvFile = Resolve-CodebaseRagEnvFile -EnvFile $EnvFile
}
else {
    $defaultEnvFile = Join-Path $PackageRoot ".env"
    if (-not (Test-Path $defaultEnvFile)) {
        throw "Environment file is required. Pass -EnvFile <path> or place a real .env file in the package/project root: $PackageRoot"
    }
    $resolvedEnvFile = (Resolve-Path $defaultEnvFile).Path
}
Import-CodebaseRagEnvFile $resolvedEnvFile | Out-Null

Assert-Administrator

$resolvedDataDir = Get-CodebaseRagDataDir $DataDir
$configDir = Join-Path $resolvedDataDir "config"
$configPath = Join-Path $configDir "config.yaml"
$configTemplatePath = Join-Path $PackageRoot "config.example.yaml"

if (-not (Test-Path $configTemplatePath)) {
    throw "Config template does not exist: $configTemplatePath"
}

Ensure-Directory $configDir
Copy-Item -Path $configTemplatePath -Destination $configPath -Force
Write-Host "Config refreshed: $configPath"

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
if (-not (Wait-CodebaseRagHealth -HealthUrl $HealthUrl -TimeoutSeconds $HealthTimeoutSeconds)) {
    throw "Config hot reload completed, but health check failed: $HealthUrl"
}
Write-Host "Config hot reload completed and health check passed: $ReloadUrl"
