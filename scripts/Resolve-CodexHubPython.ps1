[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [switch]$PrintPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$script:CodexHubPythonScriptDirectory = $PSScriptRoot

function Test-CodexHubPython313 {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }

    # Avoid nested quotes in this probe: Windows PowerShell 5.1 applies its
    # native-command quoting rules before launching Python.
    $versionOutput = @(& $Path '-c' 'import sys; print(sys.version_info[0] * 100 + sys.version_info[1])' 2>$null)
    if ($LASTEXITCODE -ne 0 -or $versionOutput.Count -eq 0) {
        return $false
    }

    $versionNumber = 0
    if (-not [int]::TryParse(
        ([string]($versionOutput | Select-Object -Last 1)).Trim(),
        [ref]$versionNumber
    )) {
        return $false
    }
    return $versionNumber -ge 313
}

function Get-CodexHubPythonLauncherPath {
    foreach ($commandName in @('py.exe', 'py')) {
        $launcher = Get-Command $commandName -ErrorAction SilentlyContinue
        if ($null -eq $launcher -or [string]::IsNullOrWhiteSpace([string]$launcher.Source)) {
            continue
        }

        $pathOutput = @(& $launcher.Source '-3.13' '-c' 'import sys; print(sys.executable)' 2>$null)
        if ($LASTEXITCODE -eq 0 -and $pathOutput.Count -gt 0) {
            $path = ([string]($pathOutput | Select-Object -Last 1)).Trim()
            if ((Test-Path -LiteralPath $path -PathType Leaf) -and (Test-CodexHubPython313 -Path $path)) {
                return $path
            }
        }
    }
    return $null
}

function Set-CodexHubPythonEnvironment {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    # A resolved parent must pass the exact same executable to every child.
    # Keeping both names in sync also prevents the Gateway-specific override
    # from re-selecting a different interpreter in Rust or a fixture launcher.
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $env:CODEXHUB_PYTHON = $fullPath
    $env:CODEXHUB_PROXY_PYTHON = $fullPath
    # Some third-party helpers still spawn a literal `python` or `pytest`
    # instead of using the parent's executable. Put the selected interpreter
    # first on PATH so those nested processes cannot fall back to the Hermes
    # Python 3.11 environment.
    $pythonDirectory = Split-Path -Parent $fullPath
    $pathSeparator = [string][System.IO.Path]::PathSeparator
    $managedPathEntries = @($pythonDirectory, $script:CodexHubPythonScriptDirectory)
    $existingPathEntries = if ([string]::IsNullOrWhiteSpace([string]$env:PATH)) {
        @()
    }
    else {
        @($env:PATH -split [regex]::Escape($pathSeparator))
    }
    $unmanagedPathEntries = @($existingPathEntries | Where-Object {
        $entry = [string]$_
        -not [string]::IsNullOrWhiteSpace($entry) -and
            -not ($managedPathEntries | Where-Object {
                $_ -and $entry.Equals([string]$_, [System.StringComparison]::OrdinalIgnoreCase)
            })
    })
    $env:PATH = (@($managedPathEntries) + $unmanagedPathEntries) -join $pathSeparator
    return $fullPath
}

function Resolve-CodexHubPythonPath {
    param(
        [string]$Root = $RepoRoot,
        [switch]$PreferBundled
    )

    $explicitValues = @(
        $env:CODEXHUB_PYTHON,
        $env:CODEXHUB_PROXY_PYTHON
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }

    foreach ($explicit in $explicitValues) {
        $explicitText = [string]$explicit
        $resolvedCommand = Get-Command $explicitText -ErrorAction SilentlyContinue
        $path = if ($null -ne $resolvedCommand -and
            -not [string]::IsNullOrWhiteSpace([string]$resolvedCommand.Source)) {
            [string]$resolvedCommand.Source
        }
        else {
            [System.IO.Path]::GetFullPath($explicitText)
        }
        if (-not (Test-CodexHubPython313 -Path $path)) {
            throw "CodexHub requires Python 3.13 or newer; explicit interpreter is not compatible: $path"
        }
        return (Set-CodexHubPythonEnvironment -Path $path)
    }

    $candidatePaths = [System.Collections.Generic.List[string]]::new()
    $localCandidates = @(
        (Join-Path $Root '.venv-ci\Scripts\python.exe'),
        (Join-Path $Root '.venv\Scripts\python.exe')
    )
    $bundledCandidates = @(
        (Join-Path $Root 'src-tauri\resources\python\python.exe')
    )
    $launcherPath = Get-CodexHubPythonLauncherPath
    $hostCandidates = [System.Collections.Generic.List[string]]::new()
    if (-not [string]::IsNullOrWhiteSpace($launcherPath)) {
        $hostCandidates.Add($launcherPath)
    }
    foreach ($commandName in @('python3.13.exe', 'python3.13', 'python.exe', 'python')) {
        $command = Get-Command $commandName -ErrorAction SilentlyContinue
        if ($null -ne $command -and -not [string]::IsNullOrWhiteSpace([string]$command.Source)) {
            $hostCandidates.Add([string]$command.Source)
        }
    }

    if ($PreferBundled) {
        foreach ($candidate in $bundledCandidates + $localCandidates + $hostCandidates) {
            if (-not $candidatePaths.Contains($candidate)) {
                $candidatePaths.Add($candidate)
            }
        }
    }
    else {
        foreach ($candidate in $localCandidates + $hostCandidates + $bundledCandidates) {
            if (-not $candidatePaths.Contains($candidate)) {
                $candidatePaths.Add($candidate)
            }
        }
    }

    foreach ($candidate in $candidatePaths) {
        if (Test-CodexHubPython313 -Path $candidate) {
            return (Set-CodexHubPythonEnvironment -Path $candidate)
        }
    }

    $ambient = Get-Command 'python' -ErrorAction SilentlyContinue
    $ambientPath = if ($null -ne $ambient) { [string]$ambient.Source } else { '<not found>' }
    throw "CodexHub requires Python 3.13 or newer, but no compatible interpreter was found (ambient python: $ambientPath). Use .\scripts\codexhub-python.cmd or set CODEXHUB_PYTHON to a Python 3.13+ executable."
}

if ($MyInvocation.InvocationName -ne '.') {
    $resolved = Resolve-CodexHubPythonPath -Root $RepoRoot
    if ($PrintPath) {
        Write-Output $resolved
    }
    else {
        & $resolved '--version'
        exit $LASTEXITCODE
    }
}
