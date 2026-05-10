param(
    [string]$ServiceName = "CodebaseRAG",
    [string]$HealthUrl = "http://127.0.0.1:18080/api/v1/health",
    [int]$HealthTimeoutSeconds = 60
)

. "$PSScriptRoot\common.ps1"

Assert-Administrator
Start-CodebaseRagService -ServiceName $ServiceName
if (-not (Wait-CodebaseRagHealth -HealthUrl $HealthUrl -TimeoutSeconds $HealthTimeoutSeconds)) {
    throw "Service started, but health check failed: $HealthUrl"
}
Write-Host "Service started and health check passed: $ServiceName"
