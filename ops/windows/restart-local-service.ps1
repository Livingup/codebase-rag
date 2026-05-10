param(
    [string]$EnvFile,
    [string]$ServiceName,
    [string]$HealthUrl,
    [int]$TimeoutSeconds = 60
)

. "$PSScriptRoot\common.ps1"

$resolvedEnvFile = Resolve-CodebaseRagEnvFile -EnvFile $EnvFile
Import-CodebaseRagEnvFile $resolvedEnvFile | Out-Null

Assert-Administrator
$ServiceName = Get-CodebaseRagServiceName $ServiceName
$HealthUrl = Get-CodebaseRagHealthUrl $HealthUrl
Stop-CodebaseRagService -ServiceName $ServiceName -TimeoutSeconds $TimeoutSeconds
Start-CodebaseRagService -ServiceName $ServiceName -TimeoutSeconds $TimeoutSeconds
if (-not (Wait-CodebaseRagHealth -HealthUrl $HealthUrl -TimeoutSeconds $TimeoutSeconds)) {
    throw "Service restarted, but health check failed: $HealthUrl"
}
Write-Host "Service restarted and health check passed: $ServiceName"
