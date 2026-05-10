param(
    [string]$EnvFile,
    [string]$SourceRoot,
    [string]$Version,
    [string]$TagName,
    [string]$Owner,
    [string]$Repo,
    [string]$Token,
    [string]$RemoteName,
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

if (-not $SourceRoot) {
    if ($env:CODEBASE_RAG_SOURCE_ROOT) { $SourceRoot = $env:CODEBASE_RAG_SOURCE_ROOT } else { $SourceRoot = $defaultSourceRoot }
}
if (-not $Version) { $Version = Get-CodebaseRagRequiredEnv "CODEBASE_RAG_RELEASE_VERSION" }
if (-not $TagName) {
    if ($env:CODEBASE_RAG_RELEASE_TAG) { $TagName = $env:CODEBASE_RAG_RELEASE_TAG } else { $TagName = "v$Version" }
}
if (-not $Owner) { $Owner = Get-CodebaseRagRequiredEnv "GITEE_OWNER" }
if (-not $Repo) { $Repo = Get-CodebaseRagRequiredEnv "GITEE_REPO" }
if (-not $Token) { $Token = Get-CodebaseRagRequiredEnv "GITEE_TOKEN" }
if (-not $RemoteName) {
    if ($env:CODEBASE_RAG_GIT_REMOTE) { $RemoteName = $env:CODEBASE_RAG_GIT_REMOTE } else { $RemoteName = "origin" }
}
if (-not $ReleaseName) {
    if ($env:CODEBASE_RAG_RELEASE_NAME) { $ReleaseName = $env:CODEBASE_RAG_RELEASE_NAME } else { $ReleaseName = "CodebaseRAG $Version" }
}
if (-not $ReleaseBody) {
    if ($env:CODEBASE_RAG_RELEASE_BODY) { $ReleaseBody = $env:CODEBASE_RAG_RELEASE_BODY } else { $ReleaseBody = "CodebaseRAG Windows release $Version" }
}
if (-not $ZipPath) {
    if ($env:CODEBASE_RAG_RELEASE_ZIP) {
        $ZipPath = $env:CODEBASE_RAG_RELEASE_ZIP
    }
    else {
        $outputRoot = $env:CODEBASE_RAG_RELEASE_OUTPUT_DIR
        if (-not $outputRoot) { $outputRoot = Join-Path $SourceRoot "dist\release" }
        $ZipPath = Join-Path $outputRoot "CodebaseRAG-$Version-windows-x64.zip"
    }
}
if (-not $Sha256Path) {
    if ($env:CODEBASE_RAG_RELEASE_SHA256) { $Sha256Path = $env:CODEBASE_RAG_RELEASE_SHA256 } else { $Sha256Path = "$ZipPath.sha256" }
}

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
            return Invoke-RestMethod -Method $Method -Uri $Uri -Body $Body -ContentType "application/x-www-form-urlencoded"
        }
        return Invoke-RestMethod -Method $Method -Uri $Uri
    }
    catch {
        $response = $_.Exception.Response
        if ($response) {
            $statusCode = [int]$response.StatusCode
            $reader = New-Object IO.StreamReader($response.GetResponseStream())
            $content = $reader.ReadToEnd()
            throw "Gitee API failed: $Method $Uri status=$statusCode body=$content"
        }
        throw
    }
}

function Get-GiteeReleaseByTag {
    param([string]$TargetTag)
    $uri = "$apiBase/repos/$encodedOwner/$encodedRepo/releases/tags/$([uri]::EscapeDataString($TargetTag))?access_token=$([uri]::EscapeDataString($Token))"
    try {
        return Invoke-RestMethod -Method Get -Uri $uri
    }
    catch {
        $response = $_.Exception.Response
        if ($response -and ([int]$response.StatusCode -eq 404)) { return $null }
        throw
    }
}

function Remove-GiteeReleaseAssetIfExists {
    param(
        [Parameter(Mandatory = $true)]$Release,
        [Parameter(Mandatory = $true)][string]$FileName
    )
    if (-not $Release.attach_files) { return }
    foreach ($asset in $Release.attach_files) {
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
    $uploadUri = "$apiBase/repos/$encodedOwner/$encodedRepo/releases/$ReleaseId/attach_files"
    $client = [System.Net.Http.HttpClient]::new()
    $content = [System.Net.Http.MultipartFormDataContent]::new()
    $fileStream = [IO.File]::OpenRead((Resolve-Path $FilePath).Path)
    try {
        $fileContent = [System.Net.Http.StreamContent]::new($fileStream)
        $fileContent.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse("application/octet-stream")
        $content.Add([System.Net.Http.StringContent]::new($Token), "access_token")
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
