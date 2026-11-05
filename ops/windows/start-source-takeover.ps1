param(
    [string]$EnvFile,
    [string]$SourceRoot,
    [string]$ConfigPath,
    [string]$ServiceName,
    [switch]$InstallDevDependencies
)

. "$PSScriptRoot\common.ps1"

$defaultSourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$resolvedEnvFile = Resolve-CodebaseRagEnvFile -EnvFile $EnvFile -BaseDir $defaultSourceRoot
Import-CodebaseRagEnvFile $resolvedEnvFile | Out-Null

Assert-Administrator
Assert-CommandExists "python"
Assert-CommandExists "git"

if (-not $SourceRoot) {
    if ($env:CODEBASE_RAG_SOURCE_ROOT) {
        $SourceRoot = $env:CODEBASE_RAG_SOURCE_ROOT
    }
    else {
        $SourceRoot = $defaultSourceRoot
    }
}
if (-not $ConfigPath) {
    if ($env:CODEBASE_RAG_CONFIG) {
        $ConfigPath = $env:CODEBASE_RAG_CONFIG
    }
    else {
        $ConfigPath = Get-CodebaseRagConfigPath
    }
}
$ServiceName = Get-CodebaseRagServiceName $ServiceName

if (-not (Test-Path $SourceRoot)) {
    throw "Source root does not exist: $SourceRoot"
}
if (-not (Test-Path $ConfigPath)) {
    throw "Config file does not exist: $ConfigPath"
}

Stop-CodebaseRagService -ServiceName $ServiceName

$venvDir = Join-Path $SourceRoot ".venv"
if (-not (Test-Path $venvDir)) {
    Invoke-NativeCommand "python" "-m" "venv" $venvDir
}

$pythonExe = Join-Path $venvDir "Scripts\python.exe"
$pipExe = Join-Path $venvDir "Scripts\pip.exe"
Invoke-NativeCommand $pythonExe "-m" "pip" "install" "--upgrade" "pip"

$extraPipArgs = @()
$repoWheelsDir = Join-Path $SourceRoot "wheels"
if (Test-Path $repoWheelsDir) {
    $extraPipArgs = @("--find-links", $repoWheelsDir)
}

if ($InstallDevDependencies) {
    Invoke-NativeCommand $pipExe "install" "-e" "$SourceRoot[dev]" @extraPipArgs
}
else {
    Invoke-NativeCommand $pipExe "install" "-e" $SourceRoot @extraPipArgs
}

$env:CODEBASE_RAG_CONFIG = $ConfigPath
Write-Host "Official service is stopped. Starting takeover from local source."
Write-Host "Source root: $SourceRoot"
Write-Host "Config file: $ConfigPath"
Write-Host "Resource env file: $resolvedEnvFile"
Write-Host "Stop source instance: press Ctrl+C in this window."
& $pythonExe -m codebase_rag --config $ConfigPath
