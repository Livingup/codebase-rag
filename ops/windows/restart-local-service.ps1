param(
    [string]$ServiceName = "CodebaseRAG",
    [string]$HealthUrl = "http://127.0.0.1:18080/api/v1/health",
    [int]$TimeoutSeconds = 60
)

. "$PSScriptRoot\common.ps1"

Assert-Administrator
Stop-CodebaseRagService -ServiceName $ServiceName -TimeoutSeconds $TimeoutSeconds
Start-CodebaseRagService -ServiceName $ServiceName -TimeoutSeconds $TimeoutSeconds
if (-not (Wait-CodebaseRagHealth -HealthUrl $HealthUrl -TimeoutSeconds $TimeoutSeconds)) {
    throw "Service restarted, but health check failed: $HealthUrl"
}
Write-Host "Service restarted and health check passed: $ServiceName"
