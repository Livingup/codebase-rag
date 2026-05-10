param(
    [string]$EnvFile,
    [string]$ServiceName,
    [string]$ProgramDir,
    [string]$DataDir,
    [string]$HealthUrl
)

. "$PSScriptRoot\common.ps1"

$resolvedEnvFile = Resolve-CodebaseRagEnvFile -EnvFile $EnvFile
Import-CodebaseRagEnvFile $resolvedEnvFile | Out-Null

$ServiceName = Get-CodebaseRagServiceName $ServiceName
$HealthUrl = Get-CodebaseRagHealthUrl $HealthUrl
$resolvedProgramDir = Get-CodebaseRagProgramDir $ProgramDir
$resolvedDataDir = Get-CodebaseRagDataDir $DataDir
$configPath = Get-CodebaseRagConfigPath $resolvedDataDir
$envPath = Get-CodebaseRagEnvPath $resolvedDataDir

Write-Host "Codebase RAG local diagnostics"
Write-Host "Program directory: $resolvedProgramDir"
Write-Host "Data directory: $resolvedDataDir"
Write-Host "Config file: $configPath"
Write-Host "Environment file: $envPath"
Write-Host "Loaded resource env: $resolvedEnvFile"

foreach ($command in @("python", "git")) {
    if (Get-Command $command -ErrorAction SilentlyContinue) {
        Write-Host "[OK] ${command}: $((& $command --version 2>$null) -join ' ')"
    }
    else {
        Write-Host "[FAIL] Command not found: $command"
    }
}

foreach ($path in @($resolvedProgramDir, $resolvedDataDir, $configPath, $envPath)) {
    if (Test-Path $path) {
        Write-Host "[OK] Exists: $path"
    }
    else {
        Write-Host "[FAIL] Missing: $path"
    }
}

$service = Get-ServiceIfExists $ServiceName
if ($service) {
    Write-Host "[OK] Service: $ServiceName status=$($service.Status)"
}
else {
    Write-Host "[WARN] Service is not registered: $ServiceName"
}

foreach ($port in @(18080, 19090)) {
    $listeners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($listeners) {
        Write-Host "[OK] Port listening: $port"
    }
    else {
        Write-Host "[WARN] Port is not listening: $port"
    }
}

if (Wait-CodebaseRagHealth -HealthUrl $HealthUrl -TimeoutSeconds 5) {
    Write-Host "[OK] Health check passed: $HealthUrl"
}
else {
    Write-Host "[WARN] Health check failed: $HealthUrl"
}
