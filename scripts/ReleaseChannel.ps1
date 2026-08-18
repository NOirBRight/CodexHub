function Assert-ReleaseVersion {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    $prereleaseIdentifier = '(?:(?:0|[1-9][0-9]*)|(?:[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))'
    $semVerPattern = '\A(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?<prerelease>-' +
        $prereleaseIdentifier + '(?:\.' + $prereleaseIdentifier + ')*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\z'
    if ($Version -notmatch $semVerPattern) {
        throw "Release version must be valid SemVer: $Version"
    }

}

function Test-ReleaseVersionIsPrerelease {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    Assert-ReleaseVersion -Version $Version
    return $Version -match '\A[0-9]+\.[0-9]+\.[0-9]+-'
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

function Get-LinuxReleaseArtifactName {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("normal", "debug")]
        [string]$Flavor,
        [Parameter(Mandatory = $true)]
        [string]$Version,
        [Parameter(Mandatory = $true)]
        [ValidateSet("appimage", "deb")]
        [string]$Kind
    )

    $suffix = if ($Flavor -eq "debug") { "_debug" } else { "" }
    switch ($Kind) {
        "appimage" { return "CodexHub_${Version}${suffix}_amd64.AppImage" }
        "deb" { return "CodexHub_${Version}${suffix}_amd64.deb" }
    }
}

function Get-UpdaterChannelTag {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    if (Test-ReleaseVersionIsPrerelease -Version $Version) {
        return "beta"
    }
    return "latest"
}

function Get-UpdaterEndpoint {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("normal", "debug")]
        [string]$Flavor,
        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    $manifestName = Get-ReleaseManifestName -Flavor $Flavor
    if ((Get-UpdaterChannelTag -Version $Version) -eq "beta") {
        return "https://github.com/NOirBRight/CodexHub/releases/download/beta/$manifestName"
    }
    return "https://github.com/NOirBRight/CodexHub/releases/latest/download/$manifestName"
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
