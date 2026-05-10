param(
    [string]$ServiceName = "CodebaseRAG",
    [string]$ProgramDir,
    [string]$DataDir,
    [string]$HealthUrl = "http://127.0.0.1:18080/api/v1/health"
)

. "$PSScriptRoot\common.ps1"

$resolvedProgramDir = Get-CodebaseRagProgramDir $ProgramDir
$resolvedDataDir = Get-CodebaseRagDataDir $DataDir
$configPath = Get-CodebaseRagConfigPath $resolvedDataDir
$envPath = Get-CodebaseRagEnvPath $resolvedDataDir

Write-Host "Codebase RAG local diagnostics"
Write-Host "Program directory: $resolvedProgramDir"
Write-Host "Data directory: $resolvedDataDir"
Write-Host "Config file: $configPath"
Write-Host "Environment file: $envPath"

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
