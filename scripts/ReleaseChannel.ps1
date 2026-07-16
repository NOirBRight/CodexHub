function Assert-ReleaseFlavorVersion {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("normal", "debug")]
        [string]$Flavor,
        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    $prereleaseIdentifier = '(?:(?:0|[1-9][0-9]*)|(?:[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))'
    $semVerPattern = '\A(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?<prerelease>-' +
        $prereleaseIdentifier + '(?:\.' + $prereleaseIdentifier + ')*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\z'
    if ($Version -notmatch $semVerPattern) {
        throw "Release version must be valid SemVer: $Version"
    }

    $hasPrerelease = $Matches.ContainsKey("prerelease") -and -not [string]::IsNullOrEmpty($Matches["prerelease"])
    if ($hasPrerelease) {
        throw "Normal and debug release flavors require a version without a prerelease suffix."
    }
}

function Get-ReleaseArtifactName {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("normal", "debug")]
        [string]$Flavor,
        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    $suffix = if ($Flavor -eq "debug") { "_debug" } else { "" }
    return "CodexHub_${Version}${suffix}_x64-setup.exe"
}

function Get-ReleaseManifestName {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("normal", "debug")]
        [string]$Flavor
    )

    if ($Flavor -eq "debug") {
        return "latest-debug.json"
    }
    return "latest.json"
}

function Get-FlavorTargetRoot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$TauriDir,
        [Parameter(Mandatory = $true)]
        [ValidateSet("normal", "debug")]
        [string]$Flavor
    )

    if ($Flavor -eq "debug") {
        return Join-Path $TauriDir "target\build-flavors\debug"
    }
    return Join-Path $TauriDir "target"
}
