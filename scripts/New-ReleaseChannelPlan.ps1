[CmdletBinding()]
param(
    [ValidateSet("normal", "debug")]
    [string]$Flavor = "normal",
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$Commit = "HEAD",
    [string]$RepoRoot = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$scriptRoot = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
    $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $scriptRoot
}
. (Join-Path $scriptRoot "ReleaseChannel.ps1")

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
$isPrerelease = Test-ReleaseVersionIsPrerelease -Version $Version

if ($commitSha -ne $mainSha) {
    throw "Normal and debug publication requires the exact main commit."
}

$normalInstaller = Get-ReleaseArtifactName -Flavor "normal" -Version $Version
$debugInstaller = Get-ReleaseArtifactName -Flavor "debug" -Version $Version
$normalManifest = Get-ReleaseManifestName -Flavor "normal"
$debugManifest = Get-ReleaseManifestName -Flavor "debug"
$normalPortable = "CodexHub_{0}_portable_{1}.zip" -f $Version, $commitSha.Substring(0, 8)
$selectedInstaller = Get-ReleaseArtifactName -Flavor $Flavor -Version $Version
$selectedManifest = Get-ReleaseManifestName -Flavor $Flavor
$normalLinuxAppImage = Get-LinuxReleaseArtifactName -Flavor "normal" -Version $Version -Kind appimage
$debugLinuxAppImage = Get-LinuxReleaseArtifactName -Flavor "debug" -Version $Version -Kind appimage
$normalLinuxDeb = Get-LinuxReleaseArtifactName -Flavor "normal" -Version $Version -Kind deb
$debugLinuxDeb = Get-LinuxReleaseArtifactName -Flavor "debug" -Version $Version -Kind deb
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
        prerelease = $isPrerelease
        assets = @(
            $normalInstaller,
            "$normalInstaller.sig",
            $normalManifest,
            $normalPortable,
            $debugInstaller,
            "$debugInstaller.sig",
            $debugManifest
        )
    }
    linux_assets = @(
        $normalLinuxAppImage,
        "$normalLinuxAppImage.sig",
        $normalLinuxDeb,
        $debugLinuxAppImage,
        "$debugLinuxAppImage.sig",
        $debugLinuxDeb
    )
    channel_release = $null
}

$plan | ConvertTo-Json -Depth 8 -Compress
