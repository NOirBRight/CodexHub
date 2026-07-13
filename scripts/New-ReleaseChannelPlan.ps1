[CmdletBinding()]
param(
    [ValidateSet("normal", "debug")]
    [string]$Flavor = "normal",
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$Commit = "HEAD",
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "ReleaseChannel.ps1")

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
Assert-ReleaseFlavorVersion -Flavor $Flavor -Version $Version

if ($commitSha -ne $mainSha) {
    throw "Normal and debug publication requires the exact main commit."
}

$normalInstaller = Get-ReleaseArtifactName -Flavor "normal" -Version $Version
$debugInstaller = Get-ReleaseArtifactName -Flavor "debug" -Version $Version
$normalManifest = Get-ReleaseManifestName -Flavor "normal"
$debugManifest = Get-ReleaseManifestName -Flavor "debug"
$selectedInstaller = Get-ReleaseArtifactName -Flavor $Flavor -Version $Version
$selectedManifest = Get-ReleaseManifestName -Flavor $Flavor
$plan = [ordered]@{
    flavor = $Flavor
    version = $Version
    commit = $commitSha
    dry_run = $true
    manifest = [ordered]@{
        name = $selectedManifest
        asset_url = "https://github.com/NOirBRight/CodexHub/releases/download/v$Version/$selectedInstaller"
    }
    immutable_release = [ordered]@{
        tag = "v$Version"
        prerelease = $false
        assets = @(
            $normalInstaller,
            "$normalInstaller.sig",
            $normalManifest,
            $debugInstaller,
            "$debugInstaller.sig",
            $debugManifest
        )
    }
    channel_release = $null
}

$plan | ConvertTo-Json -Depth 8 -Compress
