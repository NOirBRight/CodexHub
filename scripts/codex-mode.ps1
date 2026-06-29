param(
    [Parameter(Position = 0)]
    [string]$Mode = 'status',

    [switch]$RefreshCatalog,

    [switch]$NoProxyStart,

    [switch]$ForceProxyRestart,

    [switch]$ForceCloseCodex,

    [switch]$SkipUiStateRepair,

    [Alias('h')]
    [switch]$Help
)

$ErrorActionPreference = 'Stop'

function Show-Usage {
    @'
Usage:
  codex-mode.cmd status
  codex-mode.cmd official
  codex-mode.cmd proxy
  codex-mode.cmd refresh
  codex-mode.cmd history-status
  codex-mode.cmd history-custom
  codex-mode.cmd history-openai
  codex-mode.cmd consolidate-official

Single-history Codex mode switcher. It no longer copies sessions/state between
mode-buckets. Normal switching rewrites config.toml and normalizes history
provider labels to match the selected provider.

Modes:
  official             Switch live config to provider openai.
  proxy                Switch live config to provider custom through http://127.0.0.1:9099/v1.
  refresh              Refresh proxy model catalog only.
  history-status       Count openai/custom provider labels in JSONL and state_5.sqlite.
  history-custom       Repair command: normalize live history labels openai -> custom.
  history-openai       Repair command: normalize live history labels custom -> openai.
  consolidate-official Restore the old official bucket as main line, then merge active-only tail.
  status               Show active config, history summary, and proxy health.

Safety:
  Close Codex App before official/proxy/history/consolidate commands.
  -ForceCloseCodex is accepted for compatibility but intentionally ignored.
  Legacy mode-buckets are kept as backups and are not written by normal switching.
'@ | Write-Host
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Text
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    $parent = Split-Path -Parent $Path
    if ($parent) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    [System.IO.File]::WriteAllText($Path, $Text, $encoding)
}

function Read-TextIfExists {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (Test-Path -LiteralPath $Path) {
        return [System.IO.File]::ReadAllText($Path)
    }
    return ''
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath exited with code $LASTEXITCODE"
    }
}

function Convert-ToTomlLiteral {
    param([Parameter(Mandatory = $true)][string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

function Remove-ProxyOverlayBlocks {
    param([Parameter(Mandatory = $true)][string]$Text)
    return [regex]::Replace(
        $Text,
        '(?ms)^\s*# BEGIN CODEX PROXY(?: (?:SESSION|BUCKET))? CONFIG\s*$.*?^\s*# END CODEX PROXY(?: (?:SESSION|BUCKET))? CONFIG\s*$\r?\n?',
        ''
    )
}

function Remove-TopLevelKeys {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][string[]]$Keys
    )

    $keySet = @{}
    foreach ($key in $Keys) {
        $keySet[$key] = $true
    }

    $result = New-Object System.Collections.Generic.List[string]
    $inTopLevel = $true

    foreach ($line in ($Text -split "`r?`n")) {
        if ($line -match '^\s*\[') {
            $inTopLevel = $false
        }

        $match = [regex]::Match($line, '^\s*([A-Za-z0-9_-]+)\s*=')
        if ($inTopLevel -and $match.Success -and $keySet.ContainsKey($match.Groups[1].Value)) {
            continue
        }

        $result.Add($line)
    }

    return ($result -join "`n")
}

function Remove-TomlSectionsMatching {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][string]$Pattern
    )

    $result = New-Object System.Collections.Generic.List[string]
    $skip = $false
    $regex = New-Object regex($Pattern, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)

    foreach ($line in ($Text -split "`r?`n")) {
        $match = [regex]::Match($line, '^\s*\[([^\]]+)\]\s*(?:#.*)?$')
        if ($match.Success) {
            $section = $match.Groups[1].Value.Trim()
            $skip = $regex.IsMatch($section)
            if ($skip) {
                continue
            }
        }

        if (-not $skip) {
            $result.Add($line)
        }
    }

    return ($result -join "`n")
}

function Remove-FeatureFlags {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][string[]]$Keys
    )

    $keySet = @{}
    foreach ($key in $Keys) {
        $keySet[$key] = $true
    }

    $result = New-Object System.Collections.Generic.List[string]
    $inFeatures = $false

    foreach ($line in ($Text -split "`r?`n")) {
        $sectionMatch = [regex]::Match($line, '^\s*\[([^\]]+)\]\s*(?:#.*)?$')
        if ($sectionMatch.Success) {
            $inFeatures = $sectionMatch.Groups[1].Value.Trim() -ieq 'features'
            $result.Add($line)
            continue
        }

        $keyMatch = [regex]::Match($line, '^\s*([A-Za-z0-9_-]+)\s*=')
        if ($inFeatures -and $keyMatch.Success -and $keySet.ContainsKey($keyMatch.Groups[1].Value)) {
            continue
        }

        $result.Add($line)
    }

    return (($result -join "`n").TrimEnd() + "`n")
}

function Get-CleanBaseConfig {
    param([Parameter(Mandatory = $true)][string]$Text)

    $clean = Remove-ProxyOverlayBlocks -Text $Text
    $clean = Remove-TomlSectionsMatching -Text $clean -Pattern '^model_providers\.(custom|codex_proxy)$'
    $clean = Remove-TopLevelKeys -Text $clean -Keys @(
        'model',
        'model_provider',
        'model_catalog_json',
        'openai_base_url',
        'base_url',
        'wire_api',
        'requires_openai_auth',
        'supports_websockets',
        'oss_provider',
        'model_context_window',
        'model_max_output_tokens'
    )
    $clean = Remove-FeatureFlags -Text $clean -Keys @('responses_websockets', 'responses_websockets_v2')
    return $clean.TrimStart()
}

function Backup-ConfigBeforeWrite {
    param(
        [Parameter(Mandatory = $true)][string]$ConfigPath,
        [Parameter(Mandatory = $true)][string]$TargetMode
    )

    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        return
    }

    $backupDir = Join-Path $ScriptDir 'mode-backups'
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
    $stamp = Get-Date -Format 'yyyyMMddHHmmss'
    $backupPath = Join-Path $backupDir "config.$TargetMode.$stamp.toml.bak"
    Copy-Item -LiteralPath $ConfigPath -Destination $backupPath -Force
}

function Insert-ProxyProviderSection {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][string]$BaseUrlValue
    )

    $provider = @(
        '[model_providers.custom]',
        'name = "Codex Proxy"',
        "base_url = $BaseUrlValue",
        'wire_api = "responses"',
        'requires_openai_auth = true',
        'supports_websockets = false',
        ''
    ) -join "`n"
    $provider += "`n"

    $match = [regex]::Match($Text, '(?m)^\s*\[')
    if ($match.Success) {
        return $Text.Insert($match.Index, $provider)
    }
    if ($Text.Trim()) {
        return $Text.TrimEnd() + "`n`n" + $provider
    }
    return $provider
}

function Set-ConfigForMode {
    param(
        [Parameter(Mandatory = $true)][string]$ConfigPath,
        [Parameter(Mandatory = $true)][ValidateSet('official', 'proxy')][string]$TargetMode
    )

    $base = Get-CleanBaseConfig -Text (Read-TextIfExists -Path $ConfigPath)
    Backup-ConfigBeforeWrite -ConfigPath $ConfigPath -TargetMode $TargetMode
    if ($TargetMode -eq 'official') {
        $prefix = "model = `"gpt-5.5`"`nmodel_provider = `"openai`"`n`n"
        Write-Utf8NoBom -Path $ConfigPath -Text ($prefix + $base)
        return
    }

    $catalogValue = Convert-ToTomlLiteral -Value 'model-catalogs/codex-proxy-official-ollama.json'
    $baseUrlValue = Convert-ToTomlLiteral -Value 'http://127.0.0.1:9099/v1'
    $prefix = @(
        '# BEGIN CODEX PROXY CONFIG',
        'model = "openai/gpt-5.5"',
        'model_provider = "custom"',
        "model_catalog_json = $catalogValue",
        '# END CODEX PROXY CONFIG',
        ''
    ) -join "`n"
    $prefix += "`n"
    $base = Insert-ProxyProviderSection -Text $base -BaseUrlValue $baseUrlValue
    Write-Utf8NoBom -Path $ConfigPath -Text ($prefix + $base)
}

function Get-CodexAppProcesses {
    $self = $PID
    @(Get-CimInstance Win32_Process | Where-Object {
        if ($_.ProcessId -eq $self) {
            $false
        }
        else {
            $name = [string]$_.Name
            $commandLine = [string]$_.CommandLine
            $isDesktop = $name -ceq 'Codex.exe'
            $isAppServer = (
                $commandLine -match '(?i)\bapp-server\b' -and
                (
                    $commandLine -match '(?i)\\OpenAI\.Codex_[^\\]+\\app\\resources\\codex\.exe' -or
                    $commandLine -match '(?i)\\@openai\\codex\\bin\\codex\.js' -or
                    $commandLine -match '(?i)\\codex\.CMD\b'
                )
            )
            $isDesktop -or $isAppServer
        }
    })
}

function Assert-CodexClosed {
    $running = @(Get-CodexAppProcesses)
    if ($running.Count -eq 0) {
        return
    }

    if ($ForceCloseCodex) {
        Write-Warning '-ForceCloseCodex is disabled in single-history mode to avoid losing the active conversation. Close Codex App manually, then rerun the command.'
    }

    $summary = ($running | Select-Object -First 10 | ForEach-Object {
        "$($_.Name):$($_.ProcessId)"
    }) -join ', '
    throw "Codex App/app-server is still running ($summary). Close Codex App before this operation."
}

function Get-ProxyProcesses {
    $pythonPattern = [Regex]::Escape((Join-Path $ProxyDir 'codex_proxy.py'))
    $runnerPattern = [Regex]::Escape((Join-Path $ScriptDir 'run-codex-proxy.ps1'))
    @(Get-CimInstance Win32_Process | Where-Object {
        $commandLine = [string]$_.CommandLine
        ($_.Name -ieq 'python.exe' -and $commandLine -match $pythonPattern) -or
        ($_.Name -ieq 'powershell.exe' -and $commandLine -match $runnerPattern)
    })
}

function Convert-CimDateTime {
    param($Value)
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

function Test-ProxyHealth {
    try {
        return Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 2
    }
    catch {
        return $null
    }
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
        if ($null -ne $startedAt -and $startedAt -lt $latestDependency.LastWriteTime) {
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
    $runner = Join-Path $ScriptDir 'run-codex-proxy.ps1'
    $startArgs = @(
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        "`"$runner`""
    )
    Start-Process -FilePath 'powershell.exe' -ArgumentList $startArgs -WorkingDirectory $ScriptDir -WindowStyle Hidden | Out-Null
}

function Wait-ProxyHealth {
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        $health = Test-ProxyHealth
        if ($null -ne $health -and $health.ok -eq $true) {
            return $health
        }
        Start-Sleep -Milliseconds 500
    }
    return $null
}

function Ensure-ProxyReady {
    if ($NoProxyStart) {
        Write-Host 'Skipping proxy start because -NoProxyStart was supplied.'
        return
    }

    $health = Test-ProxyHealth
    if ($null -ne $health -and $health.ok -eq $true) {
        if ((Test-ProxyNeedsRestart) -and -not $ForceProxyRestart) {
            Write-Warning 'Proxy code/catalog changed after the running proxy started. Leaving it running; use -ForceProxyRestart only after finishing active conversations.'
        }
        elseif ($ForceProxyRestart) {
            Write-Host 'Restarting local proxy because -ForceProxyRestart was supplied...'
            Stop-ProxyProcesses
            Start-ProxyProcess
            $health = Wait-ProxyHealth
            if ($null -eq $health -or $health.ok -ne $true) {
                throw "Proxy did not become healthy within 20 seconds at $HealthUrl"
            }
        }

        Write-Host "Proxy healthy at $HealthUrl (build: $($health.build))."
        return
    }

    Write-Host 'Starting local proxy for proxy mode...'
    Start-ProxyProcess
    $newHealth = Wait-ProxyHealth
    if ($null -eq $newHealth -or $newHealth.ok -ne $true) {
        throw "Proxy did not become healthy within 20 seconds at $HealthUrl"
    }
    Write-Host "Proxy healthy at $HealthUrl (build: $($newHealth.build))."
}

function Refresh-ProxyCatalog {
    Write-Host 'Refreshing proxy model catalog...'
    Push-Location -LiteralPath $ProxyDir
    try {
        Invoke-Checked -FilePath 'python' -Arguments @((Join-Path $ProxyDir 'catalog_sync.py'), '--sync') | Out-Null
    }
    finally {
        Pop-Location
    }
}

function Repair-UiStateFile {
    param(
        [Parameter(Mandatory = $true)][string]$StatePath,
        [Parameter(Mandatory = $true)][string]$BackupLabel
    )

    if ($SkipUiStateRepair -or -not (Test-Path -LiteralPath $StatePath)) {
        return
    }

    $backupDir = Join-Path $ScriptDir 'mode-backups'
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    $backupPath = Join-Path $backupDir ("global-state.$BackupLabel.$(Get-Date -Format 'yyyyMMddHHmmss').bak")
    Invoke-Checked -FilePath 'python' -Arguments @(
        (Join-Path $ProxyDir 'global_state_repair.py'),
        'repair',
        '--state',
        $StatePath,
        '--backup',
        $backupPath
    )
}

function Repair-ActiveUiState {
    param([Parameter(Mandatory = $true)][string]$Label)
    Repair-UiStateFile -StatePath (Join-Path $CodexDir '.codex-global-state.json') -BackupLabel $Label
}

function Detect-ActiveMode {
    $text = Read-TextIfExists -Path (Join-Path $CodexDir 'config.toml')
    if (
        $text -match '(?i)model_provider\s*=\s*[''"]custom[''"]' -or
        $text -match '(?i)model_catalog_json\s*=.*codex-proxy-official-ollama\.json' -or
        $text -match '# BEGIN CODEX PROXY CONFIG'
    ) {
        return 'proxy'
    }
    return 'official'
}

function Set-CurrentMode {
    param([Parameter(Mandatory = $true)][ValidateSet('official', 'proxy')][string]$TargetMode)
    New-Item -ItemType Directory -Path $ModeStateRoot -Force | Out-Null
    Write-Utf8NoBom -Path (Join-Path $ModeStateRoot 'current.txt') -Text ($TargetMode + "`n")
}

function Get-CurrentHistoryProvider {
    if ((Detect-ActiveMode) -eq 'proxy') {
        return 'custom'
    }
    return 'openai'
}

function Switch-ConfigMode {
    param([Parameter(Mandatory = $true)][ValidateSet('official', 'proxy')][string]$TargetMode)
    Assert-CodexClosed

    if ($TargetMode -eq 'proxy') {
        if ($RefreshCatalog -or -not (Test-Path -LiteralPath $CatalogPath)) {
            Refresh-ProxyCatalog
        }
    }

    $targetProvider = if ($TargetMode -eq 'proxy') { 'custom' } else { 'openai' }
    Invoke-HistoryOverlay -TargetProvider $targetProvider
    Set-ConfigForMode -ConfigPath (Join-Path $CodexDir 'config.toml') -TargetMode $TargetMode
    Repair-ActiveUiState -Label $TargetMode
    Set-CurrentMode -TargetMode $TargetMode
    if ($TargetMode -eq 'proxy') {
        Ensure-ProxyReady
    }
    Write-Host "Codex config switched to $TargetMode. Start Codex App after this command returns."
}

function Invoke-HistoryOverlay {
    param([Parameter(Mandatory = $true)][ValidateSet('custom', 'openai')][string]$TargetProvider)
    Assert-CodexClosed

    $stamp = Get-Date -Format 'yyyyMMddHHmmss'
    $historyOverlay = Join-Path $ProxyDir 'history_overlay.py'
    if ($TargetProvider -eq 'custom') {
        $backupRoot = Join-Path $ScriptDir "history-openai-to-custom-$stamp"
        Invoke-Checked -FilePath 'python' -Arguments @(
            $historyOverlay,
            'normalize-fast',
            '--codex-dir',
            $CodexDir,
            '--backup-root',
            $backupRoot,
            '--target',
            'custom'
        )
    }
    else {
        $backupRoot = Join-Path $ScriptDir "history-custom-to-openai-$stamp"
        Invoke-Checked -FilePath 'python' -Arguments @(
            $historyOverlay,
            'normalize-fast',
            '--codex-dir',
            $CodexDir,
            '--backup-root',
            $backupRoot,
            '--target',
            'openai'
        )
    }
    Write-Host "History labels normalized to $TargetProvider. Backup: $backupRoot"
}

function Invoke-HistoryStatus {
    Invoke-Checked -FilePath 'python' -Arguments @(
        (Join-Path $ProxyDir 'history_consolidate.py'),
        'status',
        '--codex-dir',
        $CodexDir
    )
}

function Invoke-ConsolidateOfficial {
    Assert-CodexClosed

    $sourceDir = Join-Path $CodexDir 'mode-buckets\official'
    if (-not (Test-Path -LiteralPath $sourceDir)) {
        throw "Official bucket not found: $sourceDir"
    }

    $stamp = Get-Date -Format 'yyyyMMddHHmmss'
    $backupRoot = Join-Path $ScriptDir "consolidate-official-$stamp"
    $targetProvider = Get-CurrentHistoryProvider
    Invoke-Checked -FilePath 'python' -Arguments @(
        (Join-Path $ProxyDir 'history_consolidate.py'),
        'official-main',
        '--codex-dir',
        $CodexDir,
        '--source-dir',
        $sourceDir,
        '--backup-root',
        $backupRoot,
        '--target-provider',
        $targetProvider
    )
    Write-Host "Official bucket consolidated into active history using provider $targetProvider. Backup: $backupRoot"
}

function Show-ConfigSummary {
    $text = Read-TextIfExists -Path (Join-Path $CodexDir 'config.toml')
    foreach ($key in @('model', 'model_provider', 'model_catalog_json', 'openai_base_url')) {
        $match = [regex]::Match($text, "(?m)^\s*$([regex]::Escape($key))\s*=\s*(.+?)\s*$")
        if ($match.Success) {
            Write-Host ("  {0}: {1}" -f $key, $match.Groups[1].Value)
        }
    }

    $customProvider = [regex]::Match($text, '(?ms)^\s*\[model_providers\.custom\]\s*(?<body>.*?)(?=^\s*\[|\z)')
    if ($customProvider.Success) {
        foreach ($key in @('base_url', 'supports_websockets', 'requires_openai_auth')) {
            $match = [regex]::Match($customProvider.Groups['body'].Value, "(?m)^\s*$([regex]::Escape($key))\s*=\s*(.+?)\s*$")
            if ($match.Success) {
                Write-Host ("  model_providers.custom.{0}: {1}" -f $key, $match.Groups[1].Value)
            }
        }
    }
}

function Convert-CimDateTimeSafe {
    param($Value)
    if ($null -eq $Value) {
        return $null
    }
    if ($Value -is [datetime]) {
        return $Value
    }
    try {
        return [System.Management.ManagementDateTimeConverter]::ToDateTime([string]$Value)
    }
    catch {
        return $null
    }
}

function Show-ConfigFreshnessWarning {
    param([object[]]$Running)

    if ($Running.Count -eq 0) {
        return
    }
    $configPath = Join-Path $CodexDir 'config.toml'
    if (-not (Test-Path -LiteralPath $configPath)) {
        return
    }
    $lastWrite = (Get-Item -LiteralPath $configPath).LastWriteTime
    $starts = @($Running | ForEach-Object { Convert-CimDateTimeSafe -Value $_.CreationDate } | Where-Object { $null -ne $_ })
    if ($starts.Count -eq 0) {
        return
    }
    $earliestStart = ($starts | Sort-Object | Select-Object -First 1)
    if ($earliestStart -lt $lastWrite) {
        Write-Warning ("config.toml was written at {0}, after Codex/App-server started at {1}. Restart Codex App for the running frontend to load this config." -f $lastWrite.ToString('yyyy-MM-dd HH:mm:ss'), $earliestStart.ToString('yyyy-MM-dd HH:mm:ss'))
    }
}

function Show-GlobalStateWarning {
    $statePath = Join-Path $CodexDir '.codex-global-state.json'
    if (-not (Test-Path -LiteralPath $statePath)) {
        return
    }

    try {
        $summaryJson = & python -c "import json, pathlib, sys; p=pathlib.Path(sys.argv[1]); data=json.loads(p.read_text(encoding='utf-8-sig')); print(json.dumps({'selected': data.get('selected-remote-host-id'), 'auto_connect_count': len(data.get('remote-connection-auto-connect-by-host-id') or {})}, ensure_ascii=True))" $statePath
        $state = $summaryJson | ConvertFrom-Json
    }
    catch {
        Write-Warning "Could not parse ${statePath}: $($_.Exception.Message)"
        return
    }

    $selected = $state.selected
    if ($selected) {
        Write-Warning "global state still selects remote host '$selected'; local conversations may appear empty until repaired and Codex App is restarted."
    }
    if ($state.auto_connect_count -gt 0) {
        Write-Warning 'global state still has remote auto-connect entries; local conversations may appear empty until repaired.'
    }
}

function Show-Status {
    $detected = Detect-ActiveMode
    $running = @(Get-CodexAppProcesses)
    $health = Test-ProxyHealth

    Write-Host "Detected mode from config: $detected"
    Write-Host "Codex App/app-server running: $($running.Count -gt 0)"
    if ($running.Count -gt 0) {
        $summary = ($running | Select-Object -First 10 | ForEach-Object { "$($_.Name):$($_.ProcessId)" }) -join ', '
        Write-Host "  processes: $summary"
        Show-ConfigFreshnessWarning -Running $running
    }
    Show-GlobalStateWarning
    if ($null -ne $health -and $health.ok -eq $true) {
        Write-Host "Proxy: healthy at $HealthUrl (build: $($health.build))"
    }
    else {
        Write-Host "Proxy: not healthy at $HealthUrl"
    }
    if (Test-Path -LiteralPath (Join-Path $CodexDir 'mode-buckets')) {
        Write-Host 'Legacy mode-buckets: present but ignored by normal switching'
    }

    Write-Host 'Active config:'
    Show-ConfigSummary
    Write-Host 'History:'
    Invoke-HistoryStatus
}

$Mode = $Mode.ToLowerInvariant()
if ($Help) {
    Show-Usage
    exit 0
}

$ScriptDir = Split-Path -Parent $PSCommandPath
$RepoRoot = Split-Path -Parent $ScriptDir
$ProxyDir = Join-Path $RepoRoot 'src-python'
$ConfigDir = Join-Path $RepoRoot 'config'
$CodexDir = Join-Path $env:USERPROFILE '.codex'
$ModeStateRoot = Join-Path $ScriptDir 'mode-state'
$CatalogPath = Join-Path $CodexDir 'model-catalogs\codex-proxy-official-ollama.json'
$HealthUrl = 'http://127.0.0.1:9099/health'

switch ($Mode) {
    'status' { Show-Status }
    'official' { Switch-ConfigMode -TargetMode 'official' }
    'proxy' { Switch-ConfigMode -TargetMode 'proxy' }
    'refresh' { Refresh-ProxyCatalog }
    'history-status' { Invoke-HistoryStatus }
    'history-custom' { Invoke-HistoryOverlay -TargetProvider 'custom' }
    'history-openai' { Invoke-HistoryOverlay -TargetProvider 'openai' }
    'consolidate-official' { Invoke-ConsolidateOfficial }
    'repair-main' { Invoke-ConsolidateOfficial }
    default {
        Show-Usage
        throw "Unknown mode: $Mode"
    }
}
