param(
    [string]$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$ConfigPath,
    [string]$ServiceName = "CodebaseRAG",
    [switch]$InstallDevDependencies
)

. "$PSScriptRoot\common.ps1"

Assert-Administrator
Assert-CommandExists "python"
Assert-CommandExists "git"

if (-not $ConfigPath) {
    $ConfigPath = Get-CodebaseRagConfigPath
}

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
if ($InstallDevDependencies) {
    Invoke-NativeCommand $pipExe "install" "-e" "$SourceRoot[dev]"
}
else {
    Invoke-NativeCommand $pipExe "install" "-e" $SourceRoot
}

$env:CODEBASE_RAG_CONFIG = $ConfigPath
Write-Host "Official service is stopped. Starting takeover from local source."
Write-Host "Source root: $SourceRoot"
Write-Host "Config file: $ConfigPath"
Write-Host "Stop source instance: press Ctrl+C in this window."
& $pythonExe -m codebase_rag --config $ConfigPath
