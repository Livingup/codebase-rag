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
if (-not $CommitMessage -and $env:CODEBASE_RAG_COMMIT_MESSAGE) {
    $CommitMessage = $env:CODEBASE_RAG_COMMIT_MESSAGE
}
if (-not $TagName -and $env:CODEBASE_RAG_RELEASE_TAG) {
    $TagName = $env:CODEBASE_RAG_RELEASE_TAG
}
$createTagEnabled = $CreateTag.IsPresent -or ($env:CODEBASE_RAG_CREATE_TAG -eq "true")

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
            throw "Working tree has uncommitted changes. Pass -CommitMessage or set CODEBASE_RAG_COMMIT_MESSAGE in the env file."
        }
        Invoke-NativeCommand "git" "add" "-A"
        Invoke-NativeCommand "git" "commit" "-m" $CommitMessage
    }

    Invoke-NativeCommand "git" "push" "-u" $RemoteName $Branch

    if ($createTagEnabled) {
        if (-not $TagName) { throw "Pass -TagName or set CODEBASE_RAG_RELEASE_TAG when creating a tag." }
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
