param(
    [Parameter(Position = 0)]
    [string]$WorkspacePath = (Get-Location).Path,

    [switch]$RefreshCatalog,

    [switch]$RepairCustomHistory,

    [switch]$RepairUiState,

    [switch]$ForceRestartApp,

    [switch]$NoWaitForAppExit,

    [switch]$CheckOfficialUpstream,

    [switch]$StrictOfficialPreflight,

    [Alias('h')]
    [switch]$Help
)

$ErrorActionPreference = 'Stop'

function Show-Usage {
    @'
Usage:
  launch-codex-proxy-app.ps1 [WorkspacePath]
  launch-codex-proxy-app.ps1 refresh
  launch-codex-proxy-app.ps1 repair-history
  launch-codex-proxy-app.ps1 repair-ui-state
  launch-codex-proxy-app.ps1 -RefreshCatalog [WorkspacePath]
  launch-codex-proxy-app.ps1 -RepairCustomHistory
  launch-codex-proxy-app.ps1 -RepairUiState
  launch-codex-proxy-app.ps1 -CheckOfficialUpstream [WorkspacePath]

Ensures the local proxy is healthy, then launches Codex App with a session-scoped
proxy config overlay. WorkspacePath defaults to the current directory.

Catalog refresh is manual. Use the refresh suffix or -RefreshCatalog to fetch
the latest upstream model list before launching. Normal launches reuse the
existing generated catalog.

If Codex App is already running, the launcher waits until it exits.
-ForceRestartApp is accepted for compatibility but intentionally ignored to
avoid losing the active conversation. Prefer codex-mode.cmd for normal
official/proxy switching.
The launcher also clears stale remote-host selection before local proxy launches
so project conversations stay attached to local workspace roots.
Official upstream preflight is optional because it can be slower and less
reliable than an authenticated Codex App request. Use -CheckOfficialUpstream to
run it, and add -StrictOfficialPreflight to fail the launch when it is not
reachable.
'@ | Write-Host
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath exited with code $LASTEXITCODE"
    }
}

function Invoke-Timed {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Label,

        [Parameter(Mandatory = $true)]
        [scriptblock]$ScriptBlock
    )

    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    try {
        & $ScriptBlock
    }
    finally {
        $stopwatch.Stop()
        Write-Host ("{0} completed in {1:n1}s" -f $Label, $stopwatch.Elapsed.TotalSeconds)
    }
}

if ($Help) {
    Show-Usage
    exit 0
}

if ($WorkspacePath -ieq 'refresh') {
    $RefreshCatalog = $true
    $WorkspacePath = (Get-Location).Path
}

$RunRepairOnly = $false
if ($WorkspacePath -ieq 'repair-history') {
    $RepairCustomHistory = $true
    $RunRepairOnly = $true
    $WorkspacePath = (Get-Location).Path
}

if ($WorkspacePath -ieq 'repair-ui-state') {
    $RepairUiState = $true
    $RunRepairOnly = $true
    $WorkspacePath = (Get-Location).Path
}

$ScriptDir = Split-Path -Parent $PSCommandPath
$RepoRoot = Split-Path -Parent $ScriptDir
$ProxyDir = Join-Path $RepoRoot 'src-python'
$ConfigDir = Join-Path $RepoRoot 'config'
$ProxyHost = '127.0.0.1'
$ProxyPort = '9099'
$ProxyBaseUrl = "http://${ProxyHost}:$ProxyPort"
$CatalogSync = Join-Path $ProxyDir 'catalog_sync.py'
$ConfigOverlay = Join-Path $ProxyDir 'config_overlay.py'
$GlobalStateRepair = Join-Path $ProxyDir 'global_state_repair.py'
$HistoryOverlay = Join-Path $ProxyDir 'history_overlay.py'
$ProxyRunner = Join-Path $ScriptDir 'run-codex-proxy.ps1'
$HealthUrl = "$ProxyBaseUrl/health"
$CatalogPath = Join-Path $env:USERPROFILE '.codex\model-catalogs\codexhub-model-catalog.json'
$CodexDir = Join-Path $env:USERPROFILE '.codex'
$ConfigPath = Join-Path $env:USERPROFILE '.codex\config.toml'
$GlobalStatePath = Join-Path $env:USERPROFILE '.codex\.codex-global-state.json'
$SessionId = '{0}-{1}' -f $PID, (Get-Date -Format 'yyyyMMddHHmmss')
$ConfigBackupPath = Join-Path $ScriptDir "config.toml.session-$SessionId.bak"
$GlobalStateBackupPath = Join-Path $ScriptDir "global-state.session-$SessionId.bak"
$HistoryRepairBackupRoot = Join-Path $ScriptDir "history-promote-custom-openai-$SessionId"
$CodexAppStartTimeoutSeconds = 120

function Get-CodexAppProcesses {
    $self = $PID
    @(Get-CimInstance Win32_Process | Where-Object {
        if ($_.ProcessId -eq $self) {
            $false
        }
        else {
            $name = [string]$_.Name
            $commandLine = [string]$_.CommandLine
            $isDesktopProcess = $name -ceq 'Codex.exe'
            $isStoreAppServer = (
                $name -ieq 'codex.exe' -and
                $commandLine -match '(?i)\bapp-server\b' -and
                $commandLine -match '(?i)\\WindowsApps\\OpenAI\.Codex_[^\\]+\\app\\resources\\codex\.exe'
            )
            $isDesktopProcess -or $isStoreAppServer
        }
    })
}

function Wait-CodexAppExit {
    $running = @(Get-CodexAppProcesses)
    if ($running.Count -eq 0) {
        return
    }

    if ($ForceRestartApp) {
        Write-Warning '-ForceRestartApp is disabled to avoid losing the active conversation. Close Codex App manually, then rerun the launcher.'
    }

    if ($NoWaitForAppExit) {
        $ids = ($running | ForEach-Object { "$($_.Name):$($_.ProcessId)" }) -join ', '
        throw "Codex App is already running ($ids). Close it first, then rerun the launcher."
    }
    else {
        Write-Host 'Codex App is already running.'
        Write-Host 'Close all Codex windows; this launcher will continue when the app exits.'
    }

    while (@(Get-CodexAppProcesses).Count -gt 0) {
        Start-Sleep -Milliseconds 750
    }
}

function Wait-CodexAppStart {
    $deadline = (Get-Date).AddSeconds($CodexAppStartTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (@(Get-CodexAppProcesses).Count -gt 0) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    return (@(Get-CodexAppProcesses).Count -gt 0)
}

function Test-ProxyHealth {
    try {
        $response = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 2
        return ($response.ok -eq $true)
    }
    catch {
        return $false
    }
}

function Convert-CimDateTime {
    param(
        [Parameter(Mandatory = $false)]
        $Value
    )

    if ($null -eq $Value) {
        return $null
    }
    if ($Value -is [DateTime]) {
        return $Value
    }

    try {
        return [Management.ManagementDateTimeConverter]::ToDateTime([string]$Value)
    }
    catch {
        return $null
    }
}

function Get-ProxyProcesses {
    $self = $PID
    $pythonPattern = [Regex]::Escape((Join-Path $ProxyDir 'codex_proxy.py'))
    $runnerPattern = [Regex]::Escape($ProxyRunner)
    $portPattern = [Regex]::Escape([string]$ProxyPort)
    $runnerInvokePattern = "(?i)(?:^|\s)(?:-|/)File\s+`"?$runnerPattern`"?(?:\s|$)"
    @(Get-CimInstance Win32_Process | Where-Object {
        if ($_.ProcessId -eq $self) {
            $false
        }
        else {
            $name = [string]$_.Name
            $commandLine = [string]$_.CommandLine
            $isProxyPythonFile = (
                $name -match '(?i)^python(?:w)?\.exe$' -and
                $commandLine -match $pythonPattern -and
                $commandLine -match "(?i)(?:^|\s)--port\s+$portPattern(?:\s|$)"
            )
            $isProxyPythonInline = (
                $name -match '(?i)^python(?:w)?\.exe$' -and
                $commandLine -match '(?i)\bcodex_proxy\.run_server\(' -and
                $commandLine -match "(?i)(?:^|[,\s])$portPattern(?:[,\s\)]|$)"
            )
            $isProxyRunner = (
                $name -ieq 'powershell.exe' -and
                $commandLine -match $runnerInvokePattern
            )
            $isProxyPythonFile -or $isProxyPythonInline -or $isProxyRunner
        }
    })
}

function Test-ProxyNeedsRestart {
    $dependencyPaths = @(
        (Join-Path $ProxyDir 'codex_proxy.py'),
        (Join-Path $ProxyDir 'catalog_sync.py'),
        (Join-Path $ProxyDir 'catalog.py'),
        (Join-Path $ProxyDir 'providers_config.py'),
        (Join-Path $ConfigDir 'catalog_policy.toml'),
        (Join-Path $ConfigDir 'providers.toml')
    )
    $latestDependency = $dependencyPaths |
        Where-Object { Test-Path -LiteralPath $_ } |
        ForEach-Object { Get-Item -LiteralPath $_ } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($null -eq $latestDependency) {
        return $false
    }

    foreach ($process in @(Get-ProxyProcesses)) {
        $startedAt = Convert-CimDateTime $process.CreationDate
        if ($null -eq $startedAt) {
            continue
        }
        if ($startedAt -lt $latestDependency.LastWriteTime) {
            return $true
        }
    }
    return $false
}

function Stop-ProxyProcesses {
    foreach ($process in @(Get-ProxyProcesses)) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Start-ProxyProcess {
    $startArgs = @(
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        "`"$ProxyRunner`""
    )
    Start-Process -FilePath 'powershell.exe' -ArgumentList $startArgs -WorkingDirectory $ScriptDir -WindowStyle Hidden | Out-Null
}

function Wait-ProxyHealth {
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        if (Test-ProxyHealth) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Test-OfficialUpstreamConnectivity {
    $body = '{"model":"openai/gpt-5.5","input":"proxy upstream preflight"}'
    try {
        Invoke-WebRequest -Uri "$ProxyBaseUrl/v1/responses" -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 20 | Out-Null
        return $true
    }
    catch {
        $response = $_.Exception.Response
        $statusCode = $null
        if ($null -ne $response) {
            try {
                $statusCode = [int]$response.StatusCode
            }
            catch {
                $statusCode = $null
            }
        }

        if ($statusCode -eq 401) {
            return $true
        }

        $detail = $_.Exception.Message
        if ($statusCode) {
            $detail = "HTTP $statusCode; $detail"
        }
        Write-Warning "Official upstream preflight failed through ${ProxyBaseUrl}: $detail"
        return $false
    }
}

function Test-OldCustomProxyOverlay {
    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        return $false
    }
    $text = Get-Content -LiteralPath $ConfigPath -Raw
    return (
        $text -match '# BEGIN CODEX PROXY SESSION CONFIG' -and
        $text -match '(?m)^\s*model_provider\s*=\s*"custom"\s*$'
    )
}

function Repair-CustomHistory {
    Write-Host 'Repairing old custom-bucket proxy history into the openai bucket...'
    Invoke-Timed -Label 'Custom history repair' -ScriptBlock {
        Invoke-Checked -FilePath 'python' -Arguments @(
            $HistoryOverlay,
            'promote-custom-to-openai',
            '--codex-dir',
            $CodexDir,
            '--backup-root',
            $HistoryRepairBackupRoot
        )
    }
}

function Repair-UiState {
    Invoke-Timed -Label 'Global UI state repair' -ScriptBlock {
        Invoke-Checked -FilePath 'python' -Arguments @(
            $GlobalStateRepair,
            'repair',
            '--state',
            $GlobalStatePath,
            '--backup',
            $GlobalStateBackupPath
        )
    }
}

$ResolvedWorkspacePath = (Resolve-Path -LiteralPath $WorkspacePath).Path
Wait-CodexAppExit

Repair-UiState
if ($RunRepairOnly -and $RepairUiState -and -not $RepairCustomHistory) {
    exit 0
}

if ($RepairCustomHistory -or (Test-OldCustomProxyOverlay)) {
    Repair-CustomHistory
    if ($RunRepairOnly) {
        exit 0
    }
}

Push-Location -LiteralPath $ProxyDir
try {
    if ($RefreshCatalog -or -not (Test-Path -LiteralPath $CatalogPath)) {
        Write-Host 'Catalog refresh...'
        Invoke-Timed -Label 'Catalog refresh' -ScriptBlock {
            Invoke-Checked -FilePath 'python' -Arguments @($CatalogSync, '--sync') | Out-Null
        }
    }
    else {
        Write-Host 'Using existing proxy catalog. Add refresh to the launcher command to update it.'
    }
}
finally {
    Pop-Location
}

if (Test-ProxyHealth) {
    if (Test-ProxyNeedsRestart) {
        Write-Host 'Proxy code changed; restarting proxy...'
        Stop-ProxyProcesses
        Start-ProxyProcess
        if (-not (Wait-ProxyHealth)) {
            throw "Proxy did not become healthy within 20 seconds at $HealthUrl"
        }
    }
    else {
        Write-Host 'Proxy already running.'
    }
}
else {
    Start-ProxyProcess
    Write-Host 'Proxy started.'

    if (-not (Wait-ProxyHealth)) {
        throw "Proxy did not become healthy within 20 seconds at $HealthUrl"
    }
}

if ($CheckOfficialUpstream) {
    if (-not (Test-OfficialUpstreamConnectivity)) {
        $message = 'Official Codex upstream is not reachable through the local proxy. Official GPT models may fail until CMYNetwork/FlClash routing recovers.'
        if ($StrictOfficialPreflight) {
            throw $message
        }
        Write-Warning $message
    }
}
else {
    Write-Host 'Skipping official upstream preflight. Use -CheckOfficialUpstream to test it before launch.'
}

$ConfigOverlayApplied = $false

Write-Host 'Launching Codex App...'
try {
    Invoke-Timed -Label 'Config overlay apply' -ScriptBlock {
        Invoke-Checked -FilePath 'python' -Arguments @($ConfigOverlay, 'apply', '--config', $ConfigPath, '--backup', $ConfigBackupPath, '--catalog', $CatalogPath, '--base-url', $ProxyBaseUrl)
    }
    $ConfigOverlayApplied = $true

    & codex app $ResolvedWorkspacePath

    if (Wait-CodexAppStart) {
        Write-Host 'Codex App is running with proxy session config.'
        Write-Host 'Keep this launcher window open; config.toml will be restored after Codex exits.'
        Invoke-Timed -Label 'Waiting for Codex App exit' -ScriptBlock {
            Wait-CodexAppExit
        }
    }
    else {
        throw "Codex App did not appear within $CodexAppStartTimeoutSeconds seconds."
    }
}
finally {
    $restoreErrors = @()
    if ($ConfigOverlayApplied) {
        Write-Host 'Restoring original Codex config...'
        try {
            Invoke-Timed -Label 'Config overlay restore' -ScriptBlock {
                Invoke-Checked -FilePath 'python' -Arguments @($ConfigOverlay, 'restore', '--config', $ConfigPath, '--backup', $ConfigBackupPath)
            }
        }
        catch {
            $restoreErrors += "config.toml restore failed: $($_.Exception.Message)"
        }
    }
    if ($restoreErrors.Count -gt 0) {
        throw ($restoreErrors -join '; ')
    }
}
