param(
    [string]$EnvFile,
    [string]$ServiceName,
    [string]$NssmExe = "nssm.exe"
)

. "$PSScriptRoot\common.ps1"

$resolvedEnvFile = Resolve-CodebaseRagEnvFile -EnvFile $EnvFile
Import-CodebaseRagEnvFile $resolvedEnvFile | Out-Null

Assert-Administrator
Assert-CommandExists $NssmExe

$ServiceName = Get-CodebaseRagServiceName $ServiceName
$service = Get-ServiceIfExists $ServiceName
if (-not $service) {
    Write-Host "Service does not exist; nothing to unregister: $ServiceName"
    exit 0
}

Stop-CodebaseRagService -ServiceName $ServiceName
Invoke-NativeCommand $NssmExe "remove" $ServiceName "confirm"
Write-Host "Service unregistered: $ServiceName"
