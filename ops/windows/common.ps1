Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-CodebaseRagProgramDir {
    param([string]$ProgramDir)
    if ($ProgramDir) { return $ProgramDir }
    if ($env:CODEBASE_RAG_PROGRAM_DIR) { return $env:CODEBASE_RAG_PROGRAM_DIR }
    return (Join-Path $env:ProgramFiles "CodebaseRAG")
}

function Get-CodebaseRagDataDir {
    param([string]$DataDir)
    if ($DataDir) { return $DataDir }
    if ($env:CODEBASE_RAG_DATA_DIR) { return $env:CODEBASE_RAG_DATA_DIR }
    return (Join-Path $env:ProgramData "CodebaseRAG")
}

function Get-CodebaseRagConfigPath {
    param([string]$DataDir)
    return (Join-Path (Get-CodebaseRagDataDir $DataDir) "config\config.yaml")
}

function Get-CodebaseRagEnvPath {
    param([string]$DataDir)
    return (Join-Path (Get-CodebaseRagDataDir $DataDir) "config\.env")
}

function Get-CodebaseRagCurrentDir {
    param([string]$ProgramDir)
    return (Join-Path (Get-CodebaseRagProgramDir $ProgramDir) "current")
}

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Please run PowerShell as Administrator."
    }
}

function Assert-CommandExists {
    param([Parameter(Mandatory = $true)][string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Command not found: $Name. Please install it and add it to PATH."
    }
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: $FilePath $($Arguments -join ' ')"
    }
}

function Ensure-Directory {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
}

function Get-ServiceIfExists {
    param([Parameter(Mandatory = $true)][string]$ServiceName)
    return Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
}

function Stop-CodebaseRagService {
    param(
        [string]$ServiceName = "CodebaseRAG",
        [int]$TimeoutSeconds = 60
    )
    $service = Get-ServiceIfExists $ServiceName
    if (-not $service) { return }
    if ($service.Status -eq "Stopped") { return }
    Stop-Service -Name $ServiceName
    $service.WaitForStatus("Stopped", [TimeSpan]::FromSeconds($TimeoutSeconds))
}

function Start-CodebaseRagService {
    param(
        [string]$ServiceName = "CodebaseRAG",
        [int]$TimeoutSeconds = 60
    )
    $service = Get-ServiceIfExists $ServiceName
    if (-not $service) { throw "Service does not exist: $ServiceName" }
    if ($service.Status -ne "Running") {
        Start-Service -Name $ServiceName
        $service.WaitForStatus("Running", [TimeSpan]::FromSeconds($TimeoutSeconds))
    }
}

function Wait-CodebaseRagHealth {
    param(
        [string]$HealthUrl = "http://127.0.0.1:18080/api/v1/health",
        [int]$TimeoutSeconds = 60
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 5
            if ($response.StatusCode -eq 200) { return $true }
        }
        catch {
            Start-Sleep -Seconds 2
        }
    }
    return $false
}

function New-CodebaseRagBackup {
    param([string]$DataDir)
    $resolvedDataDir = Get-CodebaseRagDataDir $DataDir
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $backupRoot = Join-Path $resolvedDataDir "backups\$timestamp"
    Ensure-Directory $backupRoot
    foreach ($name in @("config", "data", "runtime")) {
        $source = Join-Path $resolvedDataDir $name
        if (Test-Path $source) {
            Copy-Item -Path $source -Destination (Join-Path $backupRoot $name) -Recurse -Force
        }
    }
    return $backupRoot
}

function Set-CodebaseRagCurrentRelease {
    param(
        [Parameter(Mandatory = $true)][string]$ProgramDir,
        [Parameter(Mandatory = $true)][string]$ReleaseDir
    )
    $currentDir = Get-CodebaseRagCurrentDir $ProgramDir
    if (Test-Path $currentDir) {
        Remove-Item -Path $currentDir -Force
    }
    New-Item -ItemType Junction -Path $currentDir -Target $ReleaseDir | Out-Null
}
