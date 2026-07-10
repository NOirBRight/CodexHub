function Assert-ReleaseChannelVersion {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("stable", "beta")]
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
    if ($Flavor -eq "stable" -and $hasPrerelease) {
        throw "Stable release requires a version without a prerelease suffix."
    }
    if ($Flavor -eq "beta" -and -not $hasPrerelease) {
        throw "Beta release requires a prerelease version."
    }
}
