param(
    [string]$PackageRoot = (Resolve-Path ".").Path,
    [string]$EnvFile,
    [string]$ProgramDir,
    [string]$DataDir,
    [string]$Version
)

. "$PSScriptRoot\common.ps1"

$resolvedEnvFile = $null
if ($EnvFile) {
    $resolvedEnvFile = Resolve-CodebaseRagEnvFile -EnvFile $EnvFile
}
else {
    $defaultEnvFile = Join-Path $PackageRoot ".env"
    if (-not (Test-Path $defaultEnvFile)) {
        throw "Environment file is required. Pass -EnvFile <path> or place a real .env file in the package/project root: $PackageRoot"
    }
    $resolvedEnvFile = (Resolve-Path $defaultEnvFile).Path
}
Import-CodebaseRagEnvFile $resolvedEnvFile | Out-Null

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
        Invoke-NativeCommand $pipExe "install" "--upgrade" "--force-reinstall" "--no-index" "--find-links" $wheelsDir $wheel.FullName
    }
    else {
        Invoke-NativeCommand $pipExe "install" "--upgrade" "--force-reinstall" $wheel.FullName
    }
}
else {
    if (Test-Path $wheelsDir) {
        Invoke-NativeCommand $pipExe "install" "--upgrade" "--force-reinstall" "--find-links" $wheelsDir $PackageRoot
    }
    else {
        Invoke-NativeCommand $pipExe "install" "--upgrade" "--force-reinstall" $PackageRoot
    }
}

$configPath = Join-Path $configDir "config.yaml"
$envPath = Join-Path $configDir ".env"
$configTemplatePath = Join-Path $PackageRoot "config.example.yaml"
if (-not (Test-Path $configTemplatePath)) {
    throw "Config template does not exist: $configTemplatePath"
}
if (Test-Path $configPath) {
    Write-Host "Existing config preserved (manual edits kept): $configPath"
}
else {
    Copy-Item -Path $configTemplatePath -Destination $configPath -Force
}
if ($resolvedEnvFile -and ((Resolve-Path -LiteralPath $resolvedEnvFile).Path -ne (Resolve-Path -LiteralPath $envPath -ErrorAction SilentlyContinue).Path)) {
    Copy-Item -Path $resolvedEnvFile -Destination $envPath -Force
}
$envDataDir = $resolvedDataDir -replace "\\", "/"
$envContent = Get-Content -Path $envPath -Raw
if ($envContent -match "(?m)^CODEBASE_RAG_DATA_DIR=") {
    $envContent = $envContent -replace "(?m)^CODEBASE_RAG_DATA_DIR=.*$", "CODEBASE_RAG_DATA_DIR=$envDataDir"
}
else {
    $envContent = "CODEBASE_RAG_DATA_DIR=$envDataDir`r`n" + $envContent
}
Set-Content -Path $envPath -Value $envContent -Encoding UTF8

Set-CodebaseRagCurrentRelease -ProgramDir $resolvedProgramDir -ReleaseDir $releaseDir
Set-Content -Path (Join-Path $dataRuntimeDir "current-version.txt") -Value $Version -Encoding UTF8

$runServicePath = Join-Path (Join-Path $resolvedProgramDir "service") "run-service.ps1"
@"
`$ErrorActionPreference = "Stop"
`$env:CODEBASE_RAG_CONFIG = "$configPath"
`$env:CODEBASE_RAG_ENV_FILE = "$envPath"
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
