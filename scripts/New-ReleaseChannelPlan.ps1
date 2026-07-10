[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("stable", "beta")]
    [string]$Flavor,
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$Commit = "HEAD",
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $DryRun) {
    throw "This tool is plan-only; -DryRun is required and no release will be published."
}

function Resolve-GitCommit([string]$Ref) {
    $resolved = (& git -C $RepoRoot rev-parse --verify "$Ref^{commit}" 2>$null).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($resolved)) {
        throw "Git ref does not resolve to a commit: $Ref"
    }
    return $resolved
}

$commitSha = Resolve-GitCommit $Commit
$mainSha = Resolve-GitCommit "main"
$devSha = Resolve-GitCommit "dev"

if ($Flavor -eq "beta") {
    if ($Version -notmatch '^0\.1\.4-beta\.[1-9][0-9]*$') {
        throw "Beta publication requires a v0.1.4-beta.N prerelease version."
    }
    & git -C $RepoRoot merge-base --is-ancestor $commitSha $devSha
    if ($LASTEXITCODE -ne 0) {
        throw "Beta publication requires a commit on or ancestor of dev."
    }
    $assetPrefix = "CodexHubBeta"
    $installer = "${assetPrefix}_${Version}_x64-setup.exe"
    $plan = [ordered]@{
        flavor = "beta"
        version = $Version
        commit = $commitSha
        dry_run = $true
        manifest = [ordered]@{
            name = "latest-beta.json"
            asset_url = "https://github.com/NOirBRight/CodexHub/releases/download/v$Version/$installer"
        }
        immutable_release = [ordered]@{
            tag = "v$Version"
            prerelease = $true
            assets = @($installer, "$installer.sig")
        }
        channel_release = [ordered]@{
            tag = "beta"
            prerelease = $true
            assets = @("latest-beta.json")
        }
    }
}
else {
    if ($Version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') {
        throw "Stable publication requires a stable version without a prerelease suffix."
    }
    if ($commitSha -ne $mainSha) {
        throw "Stable publication requires the exact main commit; dev must never publish Stable."
    }
    $installer = "CodexHub_${Version}_x64-setup.exe"
    $plan = [ordered]@{
        flavor = "stable"
        version = $Version
        commit = $commitSha
        dry_run = $true
        manifest = [ordered]@{
            name = "latest.json"
            asset_url = "https://github.com/NOirBRight/CodexHub/releases/download/v$Version/$installer"
        }
        immutable_release = [ordered]@{
            tag = "v$Version"
            prerelease = $false
            assets = @($installer, "$installer.sig", "latest.json")
        }
        channel_release = $null
    }
}

$plan | ConvertTo-Json -Depth 8 -Compress
