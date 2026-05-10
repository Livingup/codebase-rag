param(
    [string]$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$RemoteName = "origin",
    [string]$Branch,
    [string]$CommitMessage,
    [switch]$CreateTag,
    [string]$TagName
)

. "$PSScriptRoot\common.ps1"

Assert-CommandExists "git"

Push-Location $SourceRoot
try {
    if (-not $Branch) {
        $Branch = (git branch --show-current).Trim()
    }
    if (-not $Branch) {
        throw "Cannot detect current Git branch. Pass -Branch explicitly."
    }

    git status --short
    $dirty = (git status --porcelain=v1)
    if ($dirty) {
        if (-not $CommitMessage) {
            throw "Working tree has uncommitted changes. Pass -CommitMessage."
        }
        Invoke-NativeCommand "git" "add" "-A"
        Invoke-NativeCommand "git" "commit" "-m" $CommitMessage
    }

    Invoke-NativeCommand "git" "push" "-u" $RemoteName $Branch

    if ($CreateTag) {
        if (-not $TagName) { throw "Pass -TagName when creating a tag." }
        Invoke-NativeCommand "git" "tag" $TagName
        Invoke-NativeCommand "git" "push" $RemoteName $TagName
    }

    Write-Host "Source pushed: $RemoteName/$Branch"
}
finally {
    Pop-Location
}
