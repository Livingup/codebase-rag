param(
    [string]$EnvFile,
    [string]$DataDir,
    [ValidateSet("app", "error", "stdout", "stderr", "all")]
    [string]$Log = "app",
    [int]$Tail = 200
)

. "$PSScriptRoot\common.ps1"

$resolvedEnvFile = Resolve-CodebaseRagEnvFile -EnvFile $EnvFile
Import-CodebaseRagEnvFile $resolvedEnvFile | Out-Null

$resolvedDataDir = Get-CodebaseRagDataDir $DataDir
$logsDir = Join-Path $resolvedDataDir "logs"

if (-not (Test-Path $logsDir)) {
    throw "Logs directory does not exist: $logsDir"
}

$paths = @()
switch ($Log) {
    "app" { $paths = @(Join-Path $logsDir "app.log") }
    "error" { $paths = @(Join-Path $logsDir "error.log") }
    "stdout" { $paths = @(Join-Path $logsDir "service.stdout.log") }
    "stderr" { $paths = @(Join-Path $logsDir "service.stderr.log") }
    "all" {
        $paths = @(
            (Join-Path $logsDir "app.log"),
            (Join-Path $logsDir "error.log"),
            (Join-Path $logsDir "service.stdout.log"),
            (Join-Path $logsDir "service.stderr.log")
        )
    }
}

$existingPaths = @($paths | Where-Object { Test-Path $_ })
if ($existingPaths.Count -eq 0) {
    throw "No matching log files exist yet under: $logsDir"
}

Write-Host "Tailing logs:"
foreach ($path in $existingPaths) {
    Write-Host "  $path"
}

Get-Content -Path $existingPaths -Tail $Tail -Wait
