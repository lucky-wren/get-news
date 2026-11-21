# News Getter - One Click Deploy (PowerShell 5.1 fallback, NO Python installed)
# ALL ASCII ON PURPOSE: Windows PowerShell 5.1 reads BOM-less .ps1 as ANSI(GBK).
# A UTF-8 file with Chinese text without BOM garbles quotes and breaks the parser.
# This file is pure ASCII and carries a UTF-8 BOM, so it is codepage-independent.
# Chinese verification marks are decoded from base64 at runtime.
# Main path is deploy.py; this file is only used when python is not on PATH.

[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Say([string]$m) { Write-Host $m }
function Mask([string]$s, [string]$t) {
    if ([string]::IsNullOrEmpty($t)) { return $s }
    return $s -replace [regex]::Escape($t), '***'
}

$root = $env:NEWS_ROOT
if (-not $root) { $root = (Get-Location).Path }
$root = [IO.Path]::GetFullPath($root).TrimEnd('\', '/')
if (-not (Test-Path (Join-Path $root 'config.yaml'))) {
    throw "config.yaml not found. Put deploy.bat / deploy.ps1 in the project root."
}

$repo = $env:NEWS_REPO
if (-not $repo) {
    $m = Select-String -Path (Join-Path $root 'config.yaml') -Pattern 'github_repo:\s*"([^"]+)"' | Select-Object -First 1
    if ($m -and $m.Matches.Count -gt 0) { $repo = $m.Matches[0].Groups[1].Value }
}
if (-not $repo) { $repo = 'lucky-wren/get-news' }
$branch = if ($env:NEWS_BRANCH) { $env:NEWS_BRANCH } else { 'main' }
$apiBase = if ($env:NEWS_API_BASE) { $env:NEWS_API_BASE } else { 'https://api.github.com' }
$api = "$apiBase/repos/$repo"

$token = $env:NEWS_GITHUB_TOKEN
if (-not $token) {
    Say "GitHub PAT: classic needs repo + workflow; fine-grained needs Contents RW + Workflows RW."
    $sec = Read-Host "Enter GitHub Token (hidden, Enter to cancel)" -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}
$token = ([string]$token).Trim()
if (-not $token) { throw "Empty token, cancelled." }

$H = @{ Authorization = "Bearer $token"; 'User-Agent' = 'news-deploy'; Accept = 'application/vnd.github+json' }
# Chinese marks are base64-decoded at runtime so this .ps1 stays pure ASCII.
$marksB64 = @('5YeG5aSH5pel5b+X55uu5b2V', '6KaG55uW6LWE6K6v6I635Y+W5pWw5o2u')
$marks = @($marksB64 | ForEach-Object {
    [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($_))
}) + @('continue-on-error', 'mkdir -p logs')

function EscPath([string]$rel) {
    return (($rel -replace '\\', '/') -split '/' | ForEach-Object { [uri]::EscapeDataString($_) }) -join '/'
}
function Skip([string]$rel) {
    $parts = ($rel -replace '\\', '/') -split '/'
    foreach ($p in $parts) {
        if (@('.git','__pycache__','_sim','_verify_final','node_modules','.pytest_cache','.mypy_cache') -contains $p) { return $true }
    }
    if (@('.pyc','.log','.tmp','.out','.zip') -contains [IO.Path]::GetExtension($rel)) { return $true }
    if ($parts[0] -eq 'logs' -and $parts[-1] -ne '.gitkeep') { return $true }
    return $false
}
function GetSha([string]$rel) {
    $u = "$api/contents/$(EscPath $rel)?ref=$branch"
    try {
        $r = Invoke-RestMethod -Uri $u -Headers $H -Method Get
        return [string]$r.sha
    } catch {
        $code = 0
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        if ($code -eq 404) { return $null }
        throw "GET $rel failed: $(Mask $_.Exception.Message $token)"
    }
}
function PutFile([string]$rel, [string]$full) {
    $sha = GetSha $rel
    $body = @{ message = "deploy: news getter $rel";
               content = [Convert]::ToBase64String([IO.File]::ReadAllBytes($full));
               branch = $branch }
    if ($sha) { $body['sha'] = $sha }
    try {
        Invoke-RestMethod -Uri "$api/contents/$(EscPath $rel)" -Headers $H -Method Put `
            -Body ($body | ConvertTo-Json -Depth 4) -ContentType 'application/json' | Out-Null
    } catch {
        $msg = Mask ($_.Exception.Message) $token
        if ($msg -notmatch '422|same|identical') { throw "PUT $rel failed: $msg" }
    }
}

try {
    Say "Repo: $repo ($branch) | Root: $root"
    $files = @(Get-ChildItem -LiteralPath $root -Recurse -File -Force | Where-Object {
        $rel = $_.FullName.Substring($root.Length).TrimStart('\', '/')
        -not (Skip $rel)
    })
    Say "Uploading $($files.Count) files (includes .github hidden folder)"
    $i = 0
    foreach ($f in $files) {
        $rel = $f.FullName.Substring($root.Length).TrimStart('\', '/')
        $i++
        try { PutFile $rel $f.FullName }
        catch { Say "  [warn] $(Mask $_.Exception.Message $token)" }
        if ($i % 25 -eq 0) { Say "  $i / $($files.Count)" }
    }
    Say "Upload done: $i files"

    Say "Verifying remote workflow..."
    $r = Invoke-RestMethod -Uri "$api/contents/.github/workflows/daily-news.yml?ref=$branch" -Headers $H -Method Get
    $b64 = ($r.content -replace "`n", '') -replace "`r", ''
    $txt = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64))
    $missing = @($marks | Where-Object { $txt -notmatch [regex]::Escape($_) })
    if ($missing.Count -gt 0) {
        Say "FAIL: remote workflow is still OLD, missing: $($missing -join ', ')"
        Say "Most likely the PAT lacks the 'workflow' scope. GitHub silently rejects .github/workflows changes."
        Say "Regenerate the PAT (classic: repo+workflow) and run deploy.bat again."
        throw "Remote workflow verification failed"
    }
    Say "OK: remote workflow updated (log dir / continue-on-error / new commit message)."

    try {
        Invoke-RestMethod -Uri "$api/actions/workflows/daily-news.yml/dispatches" -Headers $H `
            -Method Post -Body (@{ ref = $branch } | ConvertTo-Json) -ContentType 'application/json' | Out-Null
        Say "OK: workflow_dispatch triggered. Check https://github.com/$repo/actions"
    } catch {
        Say "WARN: auto trigger failed (deploy is still OK). Click Run workflow manually."
    }
    $a = $repo.Split('/')
    Say "Done. Page: https://$($a[0]).github.io/$($a[1])/"
}
catch {
    Say "FAILED: $(Mask $_.Exception.Message $token)"
    exit 1
}
