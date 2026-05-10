param(
    [string]$EnvFile,
    [string]$SourceRoot,
    [string]$Version,
    [string]$TagName,
    [string]$Owner,
    [string]$Repo,
    [string]$Token,
    [string]$RemoteName,
    [string]$Branch,
    [string]$ZipPath,
    [string]$Sha256Path,
    [string]$ReleaseName,
    [string]$ReleaseBody
)

. "$PSScriptRoot\common.ps1"

$defaultSourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$resolvedEnvFile = Resolve-CodebaseRagEnvFile -EnvFile $EnvFile -BaseDir $defaultSourceRoot
Import-CodebaseRagEnvFile $resolvedEnvFile | Out-Null

Assert-CommandExists "git"

if (-not $SourceRoot) { $SourceRoot = $defaultSourceRoot }
if (-not $Version) { throw "Pass -Version to specify the release version." }
if (-not $TagName) { $TagName = "v$Version" }
if (-not $Owner) { $Owner = Get-CodebaseRagRequiredEnv "GITEE_OWNER" }
if (-not $Repo) { $Repo = Get-CodebaseRagRequiredEnv "GITEE_REPO" }
if (-not $Token) { $Token = Get-CodebaseRagRequiredEnv "GITEE_TOKEN" }
if (-not $RemoteName) {
    if ($env:CODEBASE_RAG_GIT_REMOTE) { $RemoteName = $env:CODEBASE_RAG_GIT_REMOTE } else { $RemoteName = "origin" }
}
if (-not $Branch) {
    if ($env:CODEBASE_RAG_GIT_BRANCH) { $Branch = $env:CODEBASE_RAG_GIT_BRANCH } else { $Branch = "master" }
}
if (-not $ReleaseName) { $ReleaseName = "CodebaseRAG $Version" }
if (-not $ReleaseBody) { $ReleaseBody = "CodebaseRAG Windows release $Version" }
if (-not $ZipPath) {
    $ZipPath = Join-Path $SourceRoot "dist\release\CodebaseRAG-$Version-windows-x64.zip"
}
if (-not $Sha256Path) { $Sha256Path = "$ZipPath.sha256" }

if (-not (Test-Path $ZipPath)) { throw "Release zip does not exist: $ZipPath" }
if (-not (Test-Path $Sha256Path)) { throw "Release checksum does not exist: $Sha256Path" }

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$apiBase = "https://gitee.com/api/v5"
$encodedOwner = [uri]::EscapeDataString($Owner)
$encodedRepo = [uri]::EscapeDataString($Repo)
$encodedTag = [uri]::EscapeDataString($TagName)

function Invoke-GiteeJson {
    param(
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$Uri,
        [hashtable]$Body
    )
    try {
        if ($Body) {
            $resp = Invoke-WebRequest -UseBasicParsing -Method $Method -Uri $Uri -Body $Body -ContentType "application/x-www-form-urlencoded"
        } else {
            $resp = Invoke-WebRequest -UseBasicParsing -Method $Method -Uri $Uri
        }
        $content = $resp.Content.Trim()
        if (-not $content -or $content -eq "null") { return $null }
        return ConvertFrom-Json $content
    }
    catch {
        $errBody = ""
        if ($_.Exception.Response) {
            $statusCode = [int]$_.Exception.Response.StatusCode
            try { $errBody = $_.ErrorDetails.Message } catch {}
            throw "Gitee API failed: $Method $Uri status=$statusCode body=$errBody"
        }
        throw
    }
}

function Get-GiteeReleaseByTag {
    param([string]$TargetTag)
    $page = 1
    while ($true) {
        $uri = "$apiBase/repos/$encodedOwner/$encodedRepo/releases?access_token=$([uri]::EscapeDataString($Token))&page=$page&per_page=50"
        $resp = Invoke-WebRequest -UseBasicParsing -Method Get -Uri $uri
        $body = $resp.Content.Trim()
        if (-not $body -or $body -eq "null" -or $body -eq "[]") { return $null }
        $releases = ConvertFrom-Json $body
        if ($releases.Count -eq 0) { return $null }
        foreach ($r in $releases) {
            if ($r.tag_name -eq $TargetTag) { return $r }
        }
        $page++
        if ($releases.Count -lt 50) { return $null }
    }
}

function Remove-GiteeReleaseAssetIfExists {
    param(
        [Parameter(Mandatory = $true)]$Release,
        [Parameter(Mandatory = $true)][string]$FileName
    )
    $attachFiles = $null
    if ($Release.PSObject.Properties.Match("attach_files").Count -gt 0) { $attachFiles = $Release.attach_files }
    if (-not $attachFiles) { return }
    foreach ($asset in $attachFiles) {
        if ($asset.name -eq $FileName) {
            $assetId = $asset.id
            if (-not $assetId) { continue }
            $deleteUri = "$apiBase/repos/$encodedOwner/$encodedRepo/releases/$($Release.id)/attach_files/$assetId"
            Invoke-GiteeJson -Method Delete -Uri $deleteUri -Body @{ access_token = $Token } | Out-Null
            Write-Host "Deleted existing Gitee release asset: $FileName"
        }
    }
}

function Add-GiteeReleaseAsset {
    param(
        [Parameter(Mandatory = $true)][int]$ReleaseId,
        [Parameter(Mandatory = $true)][string]$FilePath
    )
    Add-Type -AssemblyName System.Net.Http
    $encodedToken = [uri]::EscapeDataString($Token)
    $uploadUri = "$apiBase/repos/$encodedOwner/$encodedRepo/releases/$ReleaseId/attach_files?access_token=$encodedToken"
    $client = [System.Net.Http.HttpClient]::new()
    $content = [System.Net.Http.MultipartFormDataContent]::new()
    $fileStream = [IO.File]::OpenRead((Resolve-Path $FilePath).Path)
    try {
        $fileContent = [System.Net.Http.StreamContent]::new($fileStream)
        $fileContent.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse("application/octet-stream")
        $content.Add($fileContent, "file", [IO.Path]::GetFileName($FilePath))
        $response = $client.PostAsync($uploadUri, $content).GetAwaiter().GetResult()
        $responseBody = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) {
            throw "Gitee asset upload failed: status=$([int]$response.StatusCode) body=$responseBody"
        }
        Write-Host "Uploaded Gitee release asset: $([IO.Path]::GetFileName($FilePath))"
    }
    finally {
        $fileStream.Dispose()
        $content.Dispose()
        $client.Dispose()
    }
}

Push-Location $SourceRoot
try {
    $localTag = git tag --list $TagName
    if (-not $localTag) { throw "Local git tag does not exist: $TagName. Run push-source-to-remote.ps1 first." }
    git ls-remote --exit-code --tags $RemoteName $TagName | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Remote git tag does not exist on ${RemoteName}: $TagName. Run push-source-to-remote.ps1 first." }
}
finally {
    Pop-Location
}

$release = Get-GiteeReleaseByTag -TargetTag $TagName
if ($release) {
    $releaseId = [int]$release.id
    $updateUri = "$apiBase/repos/$encodedOwner/$encodedRepo/releases/$releaseId"
    $release = Invoke-GiteeJson -Method Patch -Uri $updateUri -Body @{
        access_token = $Token
        tag_name = $TagName
        name = $ReleaseName
        body = $ReleaseBody
        prerelease = "false"
    }
    Write-Host "Updated Gitee release: $TagName"
}
else {
    $createUri = "$apiBase/repos/$encodedOwner/$encodedRepo/releases"
    $release = Invoke-GiteeJson -Method Post -Uri $createUri -Body @{
        access_token = $Token
        tag_name = $TagName
        target_commitish = $Branch
        name = $ReleaseName
        body = $ReleaseBody
        prerelease = "false"
    }
    Write-Host "Created Gitee release: $TagName"
}

$release = Get-GiteeReleaseByTag -TargetTag $TagName
foreach ($filePath in @($ZipPath, $Sha256Path)) {
    $fileName = [IO.Path]::GetFileName($filePath)
    Remove-GiteeReleaseAssetIfExists -Release $release -FileName $fileName
    Add-GiteeReleaseAsset -ReleaseId ([int]$release.id) -FilePath $filePath
    $release = Get-GiteeReleaseByTag -TargetTag $TagName
}

Write-Host "Gitee release published: https://gitee.com/$Owner/$Repo/releases/tag/$TagName"
