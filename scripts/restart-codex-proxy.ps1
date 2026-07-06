param(
    [int]$ProxyPort = 9099,
    [int]$StartupTimeoutSeconds = 20,
    [string]$Python = $env:CODEXHUB_PYTHON
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $PSCommandPath
$RepoRoot = Split-Path -Parent $ScriptDir
$ProxyDir = Join-Path $RepoRoot 'src-python'
$ProxyScript = Join-Path $ProxyDir 'codex_proxy.py'
$HealthUrl = "http://127.0.0.1:$ProxyPort/health"
$LogDir = Join-Path $RepoRoot '.runtime-logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (-not $Python) {
    $candidate = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
    if (Test-Path -LiteralPath $candidate) {
        $Python = $candidate
    }
    else {
        $Python = 'python'
    }
}

if (-not $env:CODEX_HOME) {
    $env:CODEX_HOME = Join-Path $env:USERPROFILE '.codex'
}
$env:CODEX_PROXY_PORT = [string]$ProxyPort

function Get-ProxyProcesses {
    $self = $PID
    $scriptPattern = [Regex]::Escape($ProxyScript)
    $portPattern = [Regex]::Escape([string]$ProxyPort)
    @(Get-CimInstance Win32_Process | Where-Object {
        if ($_.ProcessId -eq $self) {
            $false
        }
        else {
            $name = [string]$_.Name
            $commandLine = [string]$_.CommandLine
            $name -match '(?i)^python(?:w)?\.exe$' -and
                $commandLine -match $scriptPattern -and
                $commandLine -match "(?i)(?:^|\s)--port\s+$portPattern(?:\s|$)"
        }
    })
}

function Test-ProxyHealth {
    try {
        $response = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 1
        return ($response.ok -eq $true)
    }
    catch {
        return $false
    }
}

$oldProcesses = @(Get-ProxyProcesses)
$oldPids = @($oldProcesses | ForEach-Object { $_.ProcessId })

foreach ($process in $oldProcesses) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 350

$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stdout = Join-Path $LogDir "codex-proxy-$ProxyPort-$timestamp.out.log"
$stderr = Join-Path $LogDir "codex-proxy-$ProxyPort-$timestamp.err.log"

$newProcess = Start-Process `
    -FilePath $Python `
    -ArgumentList @($ProxyScript, '--port', [string]$ProxyPort) `
    -WorkingDirectory $RepoRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -PassThru

$deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    if (Test-ProxyHealth) {
        $result = [pscustomobject]@{
            ok = $true
            old_pids = $oldPids
            new_pid = $newProcess.Id
            health_url = $HealthUrl
            stdout = $stdout
            stderr = $stderr
        } | ConvertTo-Json -Depth 4
        Write-Host $result
        exit 0
    }
    Start-Sleep -Milliseconds 300
}

$stderrTail = @()
if (Test-Path -LiteralPath $stderr) {
    $stderrTail = @(Get-Content -LiteralPath $stderr -Tail 40 -ErrorAction SilentlyContinue)
}

throw "Proxy did not become healthy within $StartupTimeoutSeconds seconds at $HealthUrl. new_pid=$($newProcess.Id) old_pids=$($oldPids -join ',') stderr_tail=$($stderrTail -join ' | ')"
