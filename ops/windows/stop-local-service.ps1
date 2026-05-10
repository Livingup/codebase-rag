param(
    [string]$ServiceName = "CodebaseRAG",
    [int]$TimeoutSeconds = 60
)

. "$PSScriptRoot\common.ps1"

Assert-Administrator
Stop-CodebaseRagService -ServiceName $ServiceName -TimeoutSeconds $TimeoutSeconds
Write-Host "Service stopped: $ServiceName"
