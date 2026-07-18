# Issue #150 manual gate: packaged Windows process trace for OpenAI usage probes.
# Proves, against the packaged portable build:
#   1. automatic (non-forced) refresh with no cache  -> exactly one app-server child
#   2. automatic (non-forced) refresh with valid cache -> zero child starts
#   3. explicit manual (forced) refresh              -> exactly one bounded child
#   4. immediate second manual refresh               -> zero child starts (coalesced)
#   5. three concurrent manual refreshes             -> exactly one child total
#   6. afterwards: no CodexHub-owned app-server child remains
# The script starts and stops its own CodexHub.exe instance and touches nothing else.

[CmdletBinding()]
param(
    [string]$PortableDir = "D:\Workstation\CodexHub\.worktrees\issue-150\output\portable\CodexHub_0.1.6_portable_a54fdc25",
    [int]$BridgePort = 14231,
    # Bridge: launch `CodexHub.exe web-bridge --port N` (isolated CLI process,
    # no single-instance conflict with a running app). App: launch the full app.
    [ValidateSet("Bridge", "App")]
    [string]$Mode = "Bridge"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$exe = Join-Path $PortableDir "CodexHub.exe"
if (-not (Test-Path -LiteralPath $exe)) { throw "packaged executable not found: $exe" }

$cachePath = Join-Path $env:USERPROFILE ".codex\proxy\openai-usage-cache.json"
$bridge = "http://127.0.0.1:$BridgePort/api/invoke"

function Get-AppServerChildren([int]$ParentPid) {
    Get-CimInstance Win32_Process -Filter "ParentProcessId = $ParentPid" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'app-server' }
}

function Invoke-Usage([bool]$Force) {
    $body = @{
        command = "openai_usage_completions"
        args    = @{ forceRefresh = $Force }
    } | ConvertTo-Json -Compress
    $response = Invoke-RestMethod -Uri $bridge -Method Post -ContentType "application/json" -Body $body -TimeoutSec 60
    return $response
}

function Measure-ChildStarts([int]$ParentPid, [scriptblock]$Action) {
    $job = Start-Job -ArgumentList $ParentPid -ScriptBlock {
        param($ppid)
        $seen = @{}
        $deadline = (Get-Date).AddSeconds(90)
        while ((Get-Date) -lt $deadline) {
            Get-CimInstance Win32_Process -Filter "ParentProcessId = $ppid" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -match 'app-server' } |
                ForEach-Object {
                    if (-not $seen.ContainsKey([string]$_.ProcessId)) {
                        $seen[[string]$_.ProcessId] = $true
                        $_.ProcessId
                    }
                }
            Start-Sleep -Milliseconds 100
        }
    }
    $actionOutput = $null
    try {
        $actionOutput = & $Action
    }
    finally {
        Start-Sleep -Seconds 2
        Stop-Job $job -ErrorAction SilentlyContinue
        $output = @(Receive-Job $job -ErrorAction SilentlyContinue)
        Remove-Job $job -Force -ErrorAction SilentlyContinue
    }
    return @{
        starts = @($output | Select-Object -Unique)
        action = $actionOutput
    }
}

if ($Mode -eq "App") {
    $running = Get-Process -Name CodexHub -ErrorAction SilentlyContinue
    if ($running) { throw "another CodexHub instance is running (PID $($running.Id -join ',')); close it first" }
    $app = Start-Process -FilePath $exe -PassThru
}
else {
    $app = Start-Process -FilePath $exe -ArgumentList @("web-bridge", "--port", "$BridgePort") -PassThru -WindowStyle Hidden
}
$script:exitCode = 1
try {
    $deadline = (Get-Date).AddSeconds(90)
    $up = $false
    while ((Get-Date) -lt $deadline -and -not $up) {
        try {
            Invoke-RestMethod -Uri "http://127.0.0.1:$BridgePort/api/invoke" -Method Post -ContentType "application/json" -Body '{"command":"get_app_flavor","args":{}}' -TimeoutSec 5 | Out-Null
            $up = $true
        }
        catch { Start-Sleep -Seconds 1 }
    }
    if (-not $up) { throw "web bridge did not come up on port $BridgePort" }
    Write-Host "target up (PID $($app.Id), mode $Mode); waiting for startup probes to settle"

    # Startup Official model refresh may spawn its own app-server children;
    # wait until none remain so phase counts are attributable to usage probes.
    $settleDeadline = (Get-Date).AddSeconds(120)
    $quietSince = $null
    while ((Get-Date) -lt $settleDeadline) {
        $children = @(Get-AppServerChildren $app.Id)
        if ($children.Count -eq 0) {
            if ($null -eq $quietSince) { $quietSince = Get-Date }
            if ((Get-Date) - $quietSince -gt [TimeSpan]::FromSeconds(5)) { break }
        }
        else { $quietSince = $null }
        Start-Sleep -Milliseconds 500
    }
    if ($null -eq $quietSince) { throw "startup app-server children did not settle" }
    Write-Host "settled; no app-server children"

    # Phase 1: automatic refresh without a cache -> exactly one child.
    Remove-Item -LiteralPath $cachePath -Force -ErrorAction SilentlyContinue
    $m = Measure-ChildStarts $app.Id { Invoke-Usage $false }
    Write-Host "phase1 response: $($m.action | ConvertTo-Json -Compress -Depth 3)"
    $starts = @($m.starts)
    Write-Host "phase1 automatic-no-cache: $($starts.Count) child start(s) [$($starts -join ',')]"
    if ($starts.Count -ne 1) { throw "phase1 expected exactly 1 child start" }
    if (-not (Test-Path -LiteralPath $cachePath)) { throw "phase1 expected the cache to be written" }

    # Phase 2: automatic refresh with a valid cache -> zero child starts.
    $m = Measure-ChildStarts $app.Id { Invoke-Usage $false }
    $starts = @($m.starts)
    Write-Host "phase2 automatic-cache-valid: $($starts.Count) child start(s)"
    if ($starts.Count -ne 0) { throw "phase2 expected zero child starts" }

    # Phase 3: explicit manual refresh -> exactly one bounded child.
    $m = Measure-ChildStarts $app.Id { Invoke-Usage $true }
    $starts = @($m.starts)
    Write-Host "phase3 manual-forced: $($starts.Count) child start(s) [$($starts -join ',')]"
    if ($starts.Count -ne 1) { throw "phase3 expected exactly 1 child start" }

    # Phase 4: a second manual refresh fired while a manual probe is still
    # in flight must coalesce -> exactly one child across both requests.
    $m = Measure-ChildStarts $app.Id {
        $bg = Start-Job -ArgumentList $bridge -ScriptBlock {
            param($uri)
            Invoke-RestMethod -Uri $uri -Method Post -ContentType "application/json" `
                -Body '{"command":"openai_usage_completions","args":{"forceRefresh":true}}' -TimeoutSec 60 | Out-Null
        }
        $inFlight = $false
        $deadline = (Get-Date).AddSeconds(20)
        while ((Get-Date) -lt $deadline -and -not $inFlight) {
            if (Get-AppServerChildren $app.Id) { $inFlight = $true; break }
            Start-Sleep -Milliseconds 100
        }
        if (-not $inFlight) { throw "phase4 probe did not start" }
        Invoke-Usage $true | Out-Null
        $bg | Wait-Job | Out-Null
        $bg | Remove-Job -Force
    }
    $starts = @($m.starts)
    Write-Host "phase4 manual-during-inflight: $($starts.Count) child start(s) [$($starts -join ',')]"
    if ($starts.Count -ne 1) { throw "phase4 expected exactly 1 child start total" }

    # Phase 5: three concurrent manual refreshes -> exactly one child total.
    $m = Measure-ChildStarts $app.Id {
        $jobs = 1..3 | ForEach-Object {
            Start-Job -ArgumentList $bridge -ScriptBlock {
                param($uri)
                Invoke-RestMethod -Uri $uri -Method Post -ContentType "application/json" `
                    -Body '{"command":"openai_usage_completions","args":{"forceRefresh":true}}' -TimeoutSec 60 | Out-Null
            }
        }
        $jobs | Wait-Job | Out-Null
        $jobs | Remove-Job -Force
    }
    $starts = @($m.starts)
    Write-Host "phase5 concurrent-manual: $($starts.Count) child start(s) [$($starts -join ',')]"
    if ($starts.Count -ne 1) { throw "phase5 expected exactly 1 child start total" }

    # Phase 6: settle, then assert no owned app-server child remains.
    Start-Sleep -Seconds 12
    $leftover = @(Get-AppServerChildren $app.Id)
    Write-Host "phase6 residual children: $($leftover.Count)"
    if ($leftover.Count -ne 0) { throw "phase6 expected no residual app-server child" }

    Write-Host "ISSUE-150 MANUAL GATE: PASS"
    $script:exitCode = 0
}
finally {
    if ($app -and -not $app.HasExited) {
        Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue
        $app.WaitForExit(15000) | Out-Null
    }
}
exit $script:exitCode
