param(
    [string]$EnvFile,
    [string]$SourceRoot,
    [string]$RemoteName,
    [string]$Branch,
    [string]$CommitMessage,
    [switch]$CreateTag,
    [string]$TagName
)

. "$PSScriptRoot\common.ps1"

$defaultSourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$resolvedEnvFile = Resolve-CodebaseRagEnvFile -EnvFile $EnvFile -BaseDir $defaultSourceRoot
Import-CodebaseRagEnvFile $resolvedEnvFile | Out-Null

Assert-CommandExists "git"

if (-not $SourceRoot) {
    if ($env:CODEBASE_RAG_SOURCE_ROOT) {
        $SourceRoot = $env:CODEBASE_RAG_SOURCE_ROOT
    }
    else {
        $SourceRoot = $defaultSourceRoot
    }
}
if (-not $RemoteName) {
    if ($env:CODEBASE_RAG_GIT_REMOTE) {
        $RemoteName = $env:CODEBASE_RAG_GIT_REMOTE
    }
    else {
        $RemoteName = "origin"
    }
}
if (-not $Branch -and $env:CODEBASE_RAG_GIT_BRANCH) {
    $Branch = $env:CODEBASE_RAG_GIT_BRANCH
}
$createTagEnabled = $CreateTag.IsPresent

Push-Location $SourceRoot
try {
    if (-not $Branch) {
        $Branch = (git branch --show-current).Trim()
    }
    if (-not $Branch) {
        throw "Cannot detect current Git branch. Pass -Branch or set CODEBASE_RAG_GIT_BRANCH in the env file."
    }

    git status --short
    $dirty = (git status --porcelain=v1)
    if ($dirty) {
        if (-not $CommitMessage) {
            throw "Working tree has uncommitted changes. Pass -CommitMessage to commit them."
        }
        Invoke-NativeCommand "git" "add" "-A"
        Invoke-NativeCommand "git" "commit" "-m" $CommitMessage
    }

    Invoke-NativeCommand "git" "push" "-u" $RemoteName $Branch

    if ($createTagEnabled) {
        if (-not $TagName) { throw "Pass -TagName when using -CreateTag." }
        $existingTag = git tag --list $TagName
        if (-not $existingTag) {
            Invoke-NativeCommand "git" "tag" $TagName
        }
        Invoke-NativeCommand "git" "push" $RemoteName $TagName
    }

    Write-Host "Source pushed: $RemoteName/$Branch"
}
finally {
    Pop-Location
}
