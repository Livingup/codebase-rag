param(
    [string]$EnvFile,
    [string]$Version,
    [string]$SourceRoot,
    [string]$OutputRoot,
    [switch]$SkipTests
)

. "$PSScriptRoot\common.ps1"

$defaultSourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$resolvedEnvFile = Resolve-CodebaseRagEnvFile -EnvFile $EnvFile -BaseDir $defaultSourceRoot
Import-CodebaseRagEnvFile $resolvedEnvFile | Out-Null

Assert-CommandExists "python"
Assert-CommandExists "git"

if (-not $Version) {
    $Version = Get-CodebaseRagRequiredEnv "CODEBASE_RAG_RELEASE_VERSION"
}
if (-not $SourceRoot) {
    if ($env:CODEBASE_RAG_SOURCE_ROOT) {
        $SourceRoot = $env:CODEBASE_RAG_SOURCE_ROOT
    }
    else {
        $SourceRoot = $defaultSourceRoot
    }
}
if (-not $OutputRoot) {
    if ($env:CODEBASE_RAG_RELEASE_OUTPUT_DIR) {
        $OutputRoot = $env:CODEBASE_RAG_RELEASE_OUTPUT_DIR
    }
    else {
        $OutputRoot = Join-Path $SourceRoot "dist\release"
    }
}
Ensure-Directory $OutputRoot

Push-Location $SourceRoot
try {
    if (-not $SkipTests) {
        Invoke-NativeCommand "python" "-m" "compileall" "-q" "src"
        Invoke-NativeCommand "python" "-m" "pytest"
    }

    Invoke-NativeCommand "python" "-m" "pip" "install" "--upgrade" "build"
    Invoke-NativeCommand "python" "-m" "build" "--wheel"

    $wheel = Get-ChildItem -Path (Join-Path $SourceRoot "dist") -Filter "codebase_rag-*.whl" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $wheel) { throw "Built codebase_rag wheel was not found." }

    $packageRoot = Join-Path $OutputRoot "CodebaseRAG-$Version-windows-x64"
    if (Test-Path $packageRoot) { Remove-Item -Path $packageRoot -Recurse -Force }
    Ensure-Directory $packageRoot
    Ensure-Directory (Join-Path $packageRoot "packages")
    Ensure-Directory (Join-Path $packageRoot "wheels")
    Ensure-Directory (Join-Path $packageRoot "scripts")

    Copy-Item -Path $wheel.FullName -Destination (Join-Path $packageRoot "packages")
    Invoke-NativeCommand "python" "-m" "pip" "download" "--dest" (Join-Path $packageRoot "wheels") $wheel.FullName

    Copy-Item -Path (Join-Path $SourceRoot "config.example.yaml") -Destination $packageRoot
    Copy-Item -Path (Join-Path $SourceRoot ".env.example") -Destination $packageRoot
    Copy-Item -Path (Join-Path $PSScriptRoot "*.ps1") -Destination (Join-Path $packageRoot "scripts")
    Set-Content -Path (Join-Path $packageRoot "VERSION") -Value $Version -Encoding UTF8

    $gitCommit = (git rev-parse HEAD).Trim()
    $manifest = [ordered]@{
        product = "CodebaseRAG"
        version = $Version
        git_commit = $gitCommit
        package_format = 1
        requires_python = ">=3.11"
        requires_git = ">=2.40"
        entrypoint = "codebase-rag --config <config_path>"
        health_check = (Get-CodebaseRagHealthUrl $null)
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -Path (Join-Path $packageRoot "release-manifest.json") -Encoding UTF8

    $zipPath = Join-Path $OutputRoot "CodebaseRAG-$Version-windows-x64.zip"
    if (Test-Path $zipPath) { Remove-Item -Path $zipPath -Force }
    Compress-Archive -Path (Join-Path $packageRoot "*") -DestinationPath $zipPath

    $sha = (Get-FileHash -Algorithm SHA256 $zipPath).Hash
    Set-Content -Path "$zipPath.sha256" -Value "$sha  $(Split-Path $zipPath -Leaf)" -Encoding UTF8

    Write-Host "Release package generated: $zipPath"
    Write-Host "Checksum file: $zipPath.sha256"
}
finally {
    Pop-Location
}
