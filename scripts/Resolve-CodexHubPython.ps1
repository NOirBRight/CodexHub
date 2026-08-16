[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [switch]$PrintPath,
    [switch]$PreferBundled,
    [switch]$RequirePytest
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$scriptRoot = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
    $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = (Resolve-Path (Join-Path $scriptRoot '..')).Path
}
$script:CodexHubPythonScriptDirectory = $scriptRoot

function Test-CodexHubPython313 {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }

    # Avoid nested quotes in this probe: Windows PowerShell 5.1 applies its
    # native-command quoting rules before launching Python.  A PATH entry may
    # also be a wrapper that PowerShell cannot execute under redirected output;
    # treat that candidate as unavailable and continue to the next one.
    $versionOutput = @()
    $status = 1
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'SilentlyContinue'
        $versionOutput = @(& $Path '-c' 'import sys; print(sys.version_info[0] * 100 + sys.version_info[1])' 2>$null)
        $status = $LASTEXITCODE
    }
    catch {
        return $false
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($status -ne 0 -or $versionOutput.Count -eq 0) {
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

function Test-CodexHubPythonPytest {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        # Windows PowerShell 5.1 promotes stderr from a .cmd wrapper to an
        # ErrorRecord even when the native stream is redirected. The probe is
        # expected to fail for a runtime without pytest, so keep that failure
        # local and return its process status instead.
        $ErrorActionPreference = 'SilentlyContinue'
        & $Path '-c' 'import pytest' 1>$null 2>$null
        $status = $LASTEXITCODE
    }
    catch {
        $status = 1
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    return $status -eq 0
}

function Clear-CodexHubPythonRuntimeSelectors {
    # PYTHONHOME can redirect a valid selected executable to an incompatible
    # stdlib. PYTHONPATH can make the pytest/version probe import host modules.
    # Clear both before any probe, not only before the final child launch.
    foreach ($name in @(
        'PYTHONHOME',
        'PYTHONPATH',
        'PYTHONSTARTUP',
        'PYTHONUSERBASE',
        'VIRTUAL_ENV',
        'CONDA_PREFIX',
        'CONDA_DEFAULT_ENV',
        'CONDA_PROMPT_MODIFIER',
        'PIPENV_ACTIVE'
    )) {
        Remove-Item -LiteralPath ("Env:{0}" -f $name) -ErrorAction SilentlyContinue
    }
}

function Test-CodexHubPythonCandidate {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [switch]$NeedsPytest
    )

    if (-not (Test-CodexHubPython313 -Path $Path)) {
        return $false
    }
    if ($NeedsPytest -and -not (Test-CodexHubPythonPytest -Path $Path)) {
        return $false
    }
    return $true
}

function Assert-CodexHubPythonRequirements {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [switch]$NeedsPytest
    )

    if (-not (Test-CodexHubPython313 -Path $Path)) {
        throw "CodexHub requires Python 3.13 or newer; explicit interpreter is not compatible: $Path"
    }
    if ($NeedsPytest -and -not (Test-CodexHubPythonPytest -Path $Path)) {
        throw "CodexHub test runtime requires pytest in the selected Python interpreter: $Path. Run .\scripts\codexhub-python.cmd -m pip install --upgrade pytest and retry."
    }
}

function Get-CodexHubPythonLauncherPath {
    foreach ($commandName in @('py.exe', 'py')) {
        $launchers = @(Get-Command $commandName -All -ErrorAction SilentlyContinue)
        foreach ($launcher in $launchers) {
            if ($null -eq $launcher -or [string]::IsNullOrWhiteSpace([string]$launcher.Source)) {
                continue
            }
            try {
                $pathOutput = @(& $launcher.Source '-3.13' '-c' 'import sys; print(sys.executable)' 2>$null)
                if ($LASTEXITCODE -eq 0 -and $pathOutput.Count -gt 0) {
                    $path = ([string]($pathOutput | Select-Object -Last 1)).Trim()
                    if ((Test-Path -LiteralPath $path -PathType Leaf) -and (Test-CodexHubPython313 -Path $path)) {
                        return $path
                    }
                }
            }
            catch {
                # A stale Windows Store/py launcher must not prevent the
                # resolver from trying the remaining PATH candidates.
                continue
            }
        }
    }
    return $null
}

function Set-CodexHubPythonEnvironment {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$SourceRoot
    )

    # A resolved parent must pass the exact same executable to every child.
    # Keeping both names in sync also prevents the Gateway-specific override
    # from re-selecting a different interpreter in Rust or a fixture launcher.
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    # Do not let an activated Hermes/Conda/Pipenv environment change the
    # interpreter's stdlib or prefix after the concrete executable is chosen.
    Clear-CodexHubPythonRuntimeSelectors
    $env:CODEXHUB_PYTHON = $fullPath
    $env:CODEXHUB_PROXY_PYTHON = $fullPath
    $env:CODEXHUB_E2E_PYTHON = $fullPath
    $sourcePython = Join-Path ([System.IO.Path]::GetFullPath($SourceRoot)) 'src-python'
    if (Test-Path -LiteralPath $sourcePython -PathType Container) {
        # Keep repository imports available without inheriting arbitrary host
        # modules from Hermes/Conda/Pipenv PYTHONPATH entries.
        $env:PYTHONPATH = $sourcePython
    }
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
        [switch]$PreferBundled,
        [switch]$RequirePytest
    )

    Clear-CodexHubPythonRuntimeSelectors

    $explicitValues = @(
        # E2E runners pass this value through isolated .cmd launchers. Treat
        # it as the same hard binding as the normal repository overrides so
        # a nested resolver cannot silently select a different host Python.
        $env:CODEXHUB_E2E_PYTHON,
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
        Assert-CodexHubPythonRequirements -Path $path -NeedsPytest:$RequirePytest
        return (Set-CodexHubPythonEnvironment -Path $path -SourceRoot $Root)
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
    # Prefer the conventional ``python.exe`` resolution before the versioned
    # shim.  Activation promises that bare ``python`` and ``pytest`` resolve
    # through the selected directory; a versioned-only shim can otherwise
    # leave an older ``python.exe`` earlier in the remaining PATH.
    foreach ($commandName in @('python.exe', 'python', 'python3.13.exe', 'python3.13')) {
        # A fresh shell may have an embedded 3.13 runtime or another valid
        # interpreter ahead of the development Python on PATH.  Enumerate all
        # command resolutions so -RequirePytest can skip a 3.13 interpreter
        # that lacks the repository test dependencies instead of stopping at
        # the first command with the right major/minor version.
        $commands = @(Get-Command $commandName -All -ErrorAction SilentlyContinue)
        foreach ($command in $commands) {
            if ($null -ne $command -and -not [string]::IsNullOrWhiteSpace([string]$command.Source)) {
                $hostCandidates.Add([string]$command.Source)
            }
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

    $compatibleWithoutPytest = $false
    foreach ($candidate in $candidatePaths) {
        if ((Test-CodexHubPython313 -Path $candidate) -and
            $RequirePytest -and
            -not (Test-CodexHubPythonPytest -Path $candidate)) {
            $compatibleWithoutPytest = $true
            continue
        }
        if (Test-CodexHubPythonCandidate -Path $candidate -NeedsPytest:$RequirePytest) {
            return (Set-CodexHubPythonEnvironment -Path $candidate -SourceRoot $Root)
        }
    }

    if ($RequirePytest -and $compatibleWithoutPytest) {
        throw 'CodexHub test runtime requires Python 3.13 or newer with pytest. Run .\scripts\codexhub-python.cmd -m pip install --upgrade pytest and retry.'
    }

    $ambient = Get-Command 'python' -ErrorAction SilentlyContinue
    $ambientPath = if ($null -ne $ambient) { [string]$ambient.Source } else { '<not found>' }
    throw "CodexHub requires Python 3.13 or newer, but no compatible interpreter was found (ambient python: $ambientPath). Use .\scripts\codexhub-python.cmd or set CODEXHUB_PYTHON to a Python 3.13+ executable."
}

if ($MyInvocation.InvocationName -ne '.') {
    $resolved = Resolve-CodexHubPythonPath -Root $RepoRoot -PreferBundled:$PreferBundled -RequirePytest:$RequirePytest
    if ($PrintPath) {
        Write-Output $resolved
    }
    else {
        & $resolved '--version'
        exit $LASTEXITCODE
    }
}
