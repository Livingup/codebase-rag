param(
    [string]$EnvFile,
    [string]$ServiceName,
    [string]$HealthUrl,
    [int]$HealthTimeoutSeconds = 60
)

. "$PSScriptRoot\common.ps1"

$resolvedEnvFile = Resolve-CodebaseRagEnvFile -EnvFile $EnvFile
Import-CodebaseRagEnvFile $resolvedEnvFile | Out-Null

Assert-Administrator
$ServiceName = Get-CodebaseRagServiceName $ServiceName
$HealthUrl = Get-CodebaseRagHealthUrl $HealthUrl
Start-CodebaseRagService -ServiceName $ServiceName
if (-not (Wait-CodebaseRagHealth -HealthUrl $HealthUrl -TimeoutSeconds $HealthTimeoutSeconds)) {
    throw "Service started, but health check failed: $HealthUrl"
}
Write-Host "Service started and health check passed: $ServiceName"
