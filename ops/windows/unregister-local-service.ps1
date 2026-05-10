param(
    [string]$ServiceName = "CodebaseRAG",
    [string]$NssmExe = "nssm.exe"
)

. "$PSScriptRoot\common.ps1"

Assert-Administrator
Assert-CommandExists $NssmExe

$service = Get-ServiceIfExists $ServiceName
if (-not $service) {
    Write-Host "Service does not exist; nothing to unregister: $ServiceName"
    exit 0
}

Stop-CodebaseRagService -ServiceName $ServiceName
Invoke-NativeCommand $NssmExe "remove" $ServiceName "confirm"
Write-Host "Service unregistered: $ServiceName"
