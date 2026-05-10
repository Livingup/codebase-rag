param(
    [string]$EnvFile,
    [string]$PackageZip,
    [string]$ProgramDir,
    [string]$DataDir,
    [string]$ServiceName,
    [string]$HealthUrl,
    [int]$HealthTimeoutSeconds = 60
)

. "$PSScriptRoot\common.ps1"

$resolvedEnvFile = Resolve-CodebaseRagEnvFile -EnvFile $EnvFile
Import-CodebaseRagEnvFile $resolvedEnvFile | Out-Null

Assert-Administrator

if (-not $PackageZip) {
    throw "Pass -PackageZip to specify the local installation zip file path."
}
$ServiceName = Get-CodebaseRagServiceName $ServiceName
$HealthUrl = Get-CodebaseRagHealthUrl $HealthUrl
$resolvedProgramDir = Get-CodebaseRagProgramDir $ProgramDir
$resolvedDataDir = Get-CodebaseRagDataDir $DataDir
$runtimeDir = Join-Path $resolvedDataDir "runtime"
$downloadsDir = Join-Path $resolvedDataDir "downloads"
Ensure-Directory $runtimeDir
Ensure-Directory $downloadsDir

if (-not (Test-Path $PackageZip)) {
    throw "Installation package does not exist: $PackageZip"
}

$oldVersionPath = Join-Path $runtimeDir "current-version.txt"
$oldVersion = $null
if (Test-Path $oldVersionPath) {
    $oldVersion = (Get-Content $oldVersionPath -Raw).Trim()
}
$oldCurrent = Get-CodebaseRagCurrentDir $resolvedProgramDir
$oldCurrentTarget = $null
if (Test-Path $oldCurrent) {
    $oldCurrentTarget = (Get-Item $oldCurrent).Target
}

$extractRoot = Join-Path $downloadsDir ([IO.Path]::GetFileNameWithoutExtension($PackageZip))
if (Test-Path $extractRoot) { Remove-Item -Path $extractRoot -Recurse -Force }
Ensure-Directory $extractRoot
Expand-Archive -Path $PackageZip -DestinationPath $extractRoot

$manifestPath = Join-Path $extractRoot "package-manifest.json"
if (-not (Test-Path $manifestPath)) {
    throw "Installation package is missing package-manifest.json: $PackageZip"
}
$manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
$newVersion = [string]$manifest.version
if (-not $newVersion) { throw "Package manifest is missing version." }

$backupDir = New-CodebaseRagBackup -DataDir $resolvedDataDir
Write-Host "Pre-update backup: $backupDir"

try {
    Stop-CodebaseRagService -ServiceName $ServiceName
    & (Join-Path $extractRoot "scripts\install-local-service.ps1") -EnvFile $resolvedEnvFile -PackageRoot $extractRoot -ProgramDir $resolvedProgramDir -DataDir $resolvedDataDir -Version $newVersion
    Start-CodebaseRagService -ServiceName $ServiceName
    if (-not (Wait-CodebaseRagHealth -HealthUrl $HealthUrl -TimeoutSeconds $HealthTimeoutSeconds)) {
        throw "Post-update health check failed: $HealthUrl"
    }
    Write-Host "Update completed: $oldVersion -> $newVersion"
}
catch {
    Write-Host "Update failed; attempting rollback: $($_.Exception.Message)"
    Stop-CodebaseRagService -ServiceName $ServiceName
    if ($oldCurrentTarget -and (Test-Path $oldCurrentTarget)) {
        Set-CodebaseRagCurrentRelease -ProgramDir $resolvedProgramDir -ReleaseDir $oldCurrentTarget
    }
    if ($oldVersion) {
        Set-Content -Path $oldVersionPath -Value $oldVersion -Encoding UTF8
    }
    Start-CodebaseRagService -ServiceName $ServiceName
    throw
}
