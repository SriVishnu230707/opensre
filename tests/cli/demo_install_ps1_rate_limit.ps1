# Run from the repository root: pwsh -NoProfile -File tests/cli/demo_install_ps1_rate_limit.ps1
# HTTP calls are stubbed. This demonstration never downloads or installs a binary.
$ErrorActionPreference = "Stop"
$installer = Join-Path (Split-Path -Parent $PSScriptRoot) "..\install.ps1"
. (Resolve-Path $installer) -SkipMain

$previousGhToken = $env:GH_TOKEN
$previousGithubToken = $env:GITHUB_TOKEN

try {
    $env:GH_TOKEN = "demo-token"
    $env:GITHUB_TOKEN = ""
    $script:metadataAttempts = 0
    $script:assetHeaders = $null

    function Invoke-RestMethod {
        param($Uri, $Headers)
        $script:metadataAttempts++
        if ($Headers.ContainsKey("Authorization")) {
            if ($Headers.Authorization -ne "Bearer demo-token") {
                throw "Expected the demo token on the metadata request"
            }
            Write-Host "metadata attempt 1: token present; simulated HTTP 401"
            $failure = [System.Exception]::new("simulated invalid token")
            $failure | Add-Member -NotePropertyName StatusCode -NotePropertyValue 401
            throw $failure
        }

        Write-Host "metadata attempt 2: anonymous; simulated success"
        return @{ tag_name = "v1.2.3" }
    }

    function Invoke-WebRequest {
        param($Uri, $Headers, $OutFile)
        $script:assetHeaders = $Headers
        Write-Host "asset request: authorization absent"
    }

    $release = Invoke-OpenSreRestMethod -Uri "https://api.github.com/demo"
    if ($release.tag_name -ne "v1.2.3" -or $script:metadataAttempts -ne 2) {
        throw "The anonymous metadata fallback did not complete as expected"
    }
    Invoke-OpenSreDownloadFileWithProgress -Uri "https://github.com/demo.zip" -OutFile "unused.zip"
    if ($script:assetHeaders.ContainsKey("Authorization")) {
        throw "The token leaked to the asset request"
    }

    foreach ($status in @(403, 429)) {
        $script:rateLimitAttempts = 0
        try {
            Invoke-OpenSreWithRetry -Description "fetch release metadata from GitHub" -Operation {
                $script:rateLimitAttempts++
                $failure = [System.Exception]::new("simulated rate limit")
                $failure | Add-Member -NotePropertyName StatusCode -NotePropertyValue $status
                throw $failure
            } | Out-Null
            throw "Expected an HTTP $status failure"
        }
        catch {
            if ($_.Exception.Message -notmatch "GitHub release API returned HTTP $status.*GH_TOKEN") {
                throw
            }
            if ($script:rateLimitAttempts -ne 1) {
                throw "HTTP $status was retried unexpectedly"
            }
            Write-Output "metadata HTTP ${status}: actionable guidance after one attempt"
        }
    }

    Write-Output "DEMO PASSED"
}
finally {
    $env:GH_TOKEN = $previousGhToken
    $env:GITHUB_TOKEN = $previousGithubToken
}
