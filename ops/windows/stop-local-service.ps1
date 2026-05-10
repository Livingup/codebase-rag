param(
    [string]$EnvFile,
    [string]$ServiceName,
    [int]$TimeoutSeconds = 60
)

. "$PSScriptRoot\common.ps1"

$resolvedEnvFile = Resolve-CodebaseRagEnvFile -EnvFile $EnvFile
Import-CodebaseRagEnvFile $resolvedEnvFile | Out-Null

Assert-Administrator
$ServiceName = Get-CodebaseRagServiceName $ServiceName
Stop-CodebaseRagService -ServiceName $ServiceName -TimeoutSeconds $TimeoutSeconds
Write-Host "Service stopped: $ServiceName"
