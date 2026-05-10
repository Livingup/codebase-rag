param(
    [string]$PackageRoot = (Resolve-Path ".").Path,
    [string]$ProgramDir,
    [string]$DataDir,
    [string]$Version
)

. "$PSScriptRoot\common.ps1"

Assert-Administrator
Assert-CommandExists "python"
Assert-CommandExists "git"

$resolvedProgramDir = Get-CodebaseRagProgramDir $ProgramDir
$resolvedDataDir = Get-CodebaseRagDataDir $DataDir
$configDir = Join-Path $resolvedDataDir "config"
$dataRuntimeDir = Join-Path $resolvedDataDir "runtime"
$logsDir = Join-Path $resolvedDataDir "logs"
$downloadsDir = Join-Path $resolvedDataDir "downloads"

Ensure-Directory $resolvedProgramDir
Ensure-Directory (Join-Path $resolvedProgramDir "releases")
Ensure-Directory (Join-Path $resolvedProgramDir "service")
Ensure-Directory $configDir
Ensure-Directory (Join-Path $resolvedDataDir "data")
Ensure-Directory $dataRuntimeDir
Ensure-Directory $logsDir
Ensure-Directory $downloadsDir

if (-not $Version) {
    $versionFile = Join-Path $PackageRoot "VERSION"
    if (Test-Path $versionFile) {
        $Version = (Get-Content $versionFile -Raw).Trim()
    }
    else {
        $Version = "dev"
    }
}

$releaseDir = Join-Path (Join-Path $resolvedProgramDir "releases") $Version
Ensure-Directory $releaseDir
Ensure-Directory (Join-Path $releaseDir "packages")
Ensure-Directory (Join-Path $releaseDir "scripts")

if (Test-Path (Join-Path $PackageRoot "packages")) {
    Copy-Item -Path (Join-Path $PackageRoot "packages\*") -Destination (Join-Path $releaseDir "packages") -Recurse -Force
}
if (Test-Path (Join-Path $PackageRoot "scripts")) {
    Copy-Item -Path (Join-Path $PackageRoot "scripts\*") -Destination (Join-Path $releaseDir "scripts") -Recurse -Force
}
if (Test-Path (Join-Path $PackageRoot "VERSION")) {
    Copy-Item -Path (Join-Path $PackageRoot "VERSION") -Destination (Join-Path $releaseDir "VERSION") -Force
}

$venvDir = Join-Path $releaseDir ".venv"
if (-not (Test-Path $venvDir)) {
    Invoke-NativeCommand "python" "-m" "venv" $venvDir
}

$pipExe = Join-Path $venvDir "Scripts\pip.exe"
$pythonExe = Join-Path $venvDir "Scripts\python.exe"
Invoke-NativeCommand $pythonExe "-m" "pip" "install" "--upgrade" "pip"

$packagesDir = Join-Path $PackageRoot "packages"
$wheelsDir = Join-Path $PackageRoot "wheels"
$wheel = Get-ChildItem -Path $packagesDir -Filter "codebase_rag-*.whl" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($wheel) {
    if (Test-Path $wheelsDir) {
        Invoke-NativeCommand $pipExe "install" "--no-index" "--find-links" $wheelsDir $wheel.FullName
    }
    else {
        Invoke-NativeCommand $pipExe "install" $wheel.FullName
    }
}
else {
    Invoke-NativeCommand $pipExe "install" $PackageRoot
}

$configPath = Join-Path $configDir "config.yaml"
$envPath = Join-Path $configDir ".env"
if (-not (Test-Path $configPath)) {
    Copy-Item -Path (Join-Path $PackageRoot "config.example.yaml") -Destination $configPath
}
if (-not (Test-Path $envPath)) {
    Copy-Item -Path (Join-Path $PackageRoot ".env.example") -Destination $envPath
}

Set-CodebaseRagCurrentRelease -ProgramDir $resolvedProgramDir -ReleaseDir $releaseDir
Set-Content -Path (Join-Path $dataRuntimeDir "current-version.txt") -Value $Version -Encoding UTF8

$runServicePath = Join-Path (Join-Path $resolvedProgramDir "service") "run-service.ps1"
@"
`$ErrorActionPreference = "Stop"
`$env:CODEBASE_RAG_CONFIG = "$configPath"
`$env:CODEBASE_RAG_DATA_DIR = "$resolvedDataDir"
`$pythonExe = Join-Path "$resolvedProgramDir" "current\.venv\Scripts\python.exe"
& `$pythonExe -m codebase_rag --config "$configPath"
"@ | Set-Content -Path $runServicePath -Encoding UTF8

Write-Host "Installation completed."
Write-Host "Program directory: $resolvedProgramDir"
Write-Host "Data directory: $resolvedDataDir"
Write-Host "Config file: $configPath"
Write-Host "Environment file: $envPath"
Write-Host "Register service: .\register-local-service.ps1"
