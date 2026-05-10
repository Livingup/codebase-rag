param(
    [string]$EnvFile,
    [string]$ServiceName,
    [string]$ProgramDir,
    [string]$DataDir,
    [string]$NssmExe = "nssm.exe"
)

. "$PSScriptRoot\common.ps1"

$resolvedEnvFile = Resolve-CodebaseRagEnvFile -EnvFile $EnvFile
Import-CodebaseRagEnvFile $resolvedEnvFile | Out-Null

Assert-Administrator
Assert-CommandExists $NssmExe

$ServiceName = Get-CodebaseRagServiceName $ServiceName
$resolvedProgramDir = Get-CodebaseRagProgramDir $ProgramDir
$resolvedDataDir = Get-CodebaseRagDataDir $DataDir
$runServicePath = Join-Path (Join-Path $resolvedProgramDir "service") "run-service.ps1"
$logsDir = Join-Path $resolvedDataDir "logs"
Ensure-Directory $logsDir

if (-not (Test-Path $runServicePath)) {
    throw "Service runner script does not exist: $runServicePath. Run install-local-service.ps1 first."
}

$existingService = Get-ServiceIfExists $ServiceName
if ($existingService) {
    Write-Host "Service already exists; updating NSSM configuration: $ServiceName"
}
else {
    Invoke-NativeCommand $NssmExe "install" $ServiceName "powershell.exe"
}

Invoke-NativeCommand $NssmExe "set" $ServiceName "AppParameters" "-NoProfile -ExecutionPolicy Bypass -File `"$runServicePath`""
Invoke-NativeCommand $NssmExe "set" $ServiceName "AppDirectory" (Join-Path $resolvedProgramDir "service")
Invoke-NativeCommand $NssmExe "set" $ServiceName "DisplayName" "Codebase RAG"
Invoke-NativeCommand $NssmExe "set" $ServiceName "Description" "Codebase RAG local Windows service"
Invoke-NativeCommand $NssmExe "set" $ServiceName "Start" "SERVICE_AUTO_START"
Invoke-NativeCommand $NssmExe "set" $ServiceName "AppStdout" (Join-Path $logsDir "service.stdout.log")
Invoke-NativeCommand $NssmExe "set" $ServiceName "AppStderr" (Join-Path $logsDir "service.stderr.log")
Invoke-NativeCommand $NssmExe "set" $ServiceName "AppRotateFiles" "1"
Invoke-NativeCommand $NssmExe "set" $ServiceName "AppRotateOnline" "1"
Invoke-NativeCommand $NssmExe "set" $ServiceName "AppRotateBytes" "52428800"
Invoke-NativeCommand $NssmExe "set" $ServiceName "AppStopMethodConsole" "15000"

Write-Host "Service registered: $ServiceName"
Write-Host "Start service: Start-Service $ServiceName"
