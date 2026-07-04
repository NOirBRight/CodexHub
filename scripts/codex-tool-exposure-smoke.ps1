param(
    [string]$Workspace = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [string]$OutputDir = (Join-Path (Join-Path $PSScriptRoot '..') 'output\cli-tool-exposure-smoke'),
    [string]$OfficialDirectModel = 'gpt-5.5',
    [string]$OfficialProxyModel = 'openai/gpt-5.5',
    [string]$ThirdPartyModel = 'volc/glm-5.2',
    [string]$ProxyBaseUrl = '',
    [string[]]$CaseName = @(),
    [string]$CodexCommand = 'codex.cmd',
    [string]$Sandbox = 'read-only',
    [int]$TimeoutSeconds = 900,
    [switch]$RunBrowserSmoke
)

$ErrorActionPreference = 'Stop'

function New-SmokeCase {
    param(
        [string]$Name,
        [string]$Model,
        [string]$Prompt,
        [string[]]$Config = @(),
        [string[]]$Expect = @(),
        [string[]]$Reject = @(),
        [string[]]$RejectArtifact = @()
    )
    [pscustomobject]@{
        Name = $Name
        Model = $Model
        Prompt = $Prompt
        Config = $Config
        Expect = $Expect
        Reject = $Reject
        RejectArtifact = $RejectArtifact
    }
}

function ConvertTo-ProcessArgument {
    param([AllowNull()][string]$Argument)
    if ($null -eq $Argument -or $Argument.Length -eq 0) {
        return '""'
    }
    if ($Argument -notmatch '[\s"]') {
        return $Argument
    }
    $escaped = $Argument -replace '(\\*)"', '$1$1\"'
    $escaped = $escaped -replace '(\\+)$', '$1$1'
    return '"' + $escaped + '"'
}

function Add-ProcessArgument {
    param(
        [System.Diagnostics.ProcessStartInfo]$StartInfo,
        [System.Collections.Generic.List[string]]$Arguments,
        [string]$Argument
    )
    [void]$Arguments.Add($Argument)
    if ($null -ne $StartInfo.ArgumentList) {
        [void]$StartInfo.ArgumentList.Add($Argument)
    }
}

function Get-NewestSessionAfter {
    param([datetime]$Since)
    $sessionsRoot = Join-Path $HOME '.codex\sessions'
    if (-not (Test-Path $sessionsRoot)) {
        return $null
    }
    Get-ChildItem -LiteralPath $sessionsRoot -Recurse -Filter '*.jsonl' |
        Where-Object { $_.LastWriteTime -ge $Since.AddSeconds(-5) } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
}

function Get-ThreadIdFromStdout {
    param([string]$Stdout)
    foreach ($line in ($Stdout -split "`r?`n")) {
        if (-not $line.Trim()) {
            continue
        }
        try {
            $event = $line | ConvertFrom-Json
        }
        catch {
            continue
        }
        if ($event.type -eq 'thread.started' -and $event.thread_id) {
            return [string]$event.thread_id
        }
    }
    return $null
}

function Get-SessionByThreadId {
    param([string]$ThreadId)
    if (-not $ThreadId) {
        return $null
    }
    $sessionsRoot = Join-Path $HOME '.codex\sessions'
    if (-not (Test-Path $sessionsRoot)) {
        return $null
    }
    Get-ChildItem -LiteralPath $sessionsRoot -Recurse -Filter "*$ThreadId.jsonl" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
}

function Save-SessionExcerpt {
    param(
        [string]$SessionPath,
        [string]$Destination
    )
    if (-not $SessionPath -or -not (Test-Path $SessionPath)) {
        Set-Content -LiteralPath $Destination -Value 'No session jsonl found for this case.' -Encoding UTF8
        return
    }
    $pattern = 'tool_search|mcp__node_repl|node_repl|multi_agent_v1|spawn_agent|wait_agent|close_agent|browser|Browser|SENTINEL|route_reason|upstream|model'
    Select-String -LiteralPath $SessionPath -Pattern $pattern |
        Select-Object -First 160 |
        ForEach-Object { $_.Line } |
        Set-Content -LiteralPath $Destination -Encoding UTF8
}

function Save-ProxyEventTail {
    param([string]$Destination)
    $eventsPath = Join-Path $HOME '.codex\proxy\codex-proxy-events.jsonl'
    if (-not (Test-Path $eventsPath)) {
        Set-Content -LiteralPath $Destination -Value 'No proxy event log found.' -Encoding UTF8
        return
    }
    Get-Content -LiteralPath $eventsPath -Tail 240 | Set-Content -LiteralPath $Destination -Encoding UTF8
}

function Invoke-CodexSmokeCase {
    param([pscustomobject]$Case)

    $caseDir = Join-Path $OutputDir $Case.Name
    New-Item -ItemType Directory -Force -Path $caseDir | Out-Null

    $promptPath = Join-Path $caseDir 'prompt.txt'
    $stdoutPath = Join-Path $caseDir 'stdout.jsonl'
    $stderrPath = Join-Path $caseDir 'stderr.txt'
    $lastMessagePath = Join-Path $caseDir 'last-message.txt'
    $sessionExcerptPath = Join-Path $caseDir 'session-excerpt.jsonl'
    $proxyEventsPath = Join-Path $caseDir 'proxy-events-tail.jsonl'
    $metadataPath = Join-Path $caseDir 'metadata.json'

    Set-Content -LiteralPath $promptPath -Value $Case.Prompt -Encoding UTF8

    $start = Get-Date
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $CodexCommand
    $processArgs = [System.Collections.Generic.List[string]]::new()
    foreach ($arg in @(
        'exec',
        '--json',
        '-C', $Workspace,
        '-m', $Case.Model,
        '-s', $Sandbox,
        '-o', $lastMessagePath
    )) {
        Add-ProcessArgument -StartInfo $psi -Arguments $processArgs -Argument $arg
    }
    foreach ($config in $Case.Config) {
        Add-ProcessArgument -StartInfo $psi -Arguments $processArgs -Argument '-c'
        Add-ProcessArgument -StartInfo $psi -Arguments $processArgs -Argument $config
    }
    Add-ProcessArgument -StartInfo $psi -Arguments $processArgs -Argument '-'
    if ($null -eq $psi.ArgumentList) {
        $psi.Arguments = ($processArgs | ForEach-Object { ConvertTo-ProcessArgument $_ }) -join ' '
    }
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $psi
    [void]$process.Start()
    $process.StandardInput.Write($Case.Prompt)
    $process.StandardInput.Close()

    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $completed = $process.WaitForExit($TimeoutSeconds * 1000)
    if (-not $completed) {
        $process.Kill($true)
        $process.WaitForExit()
    }
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    Set-Content -LiteralPath $stdoutPath -Value $stdout -Encoding UTF8
    Set-Content -LiteralPath $stderrPath -Value $stderr -Encoding UTF8

    $threadId = Get-ThreadIdFromStdout -Stdout $stdout
    $session = Get-SessionByThreadId -ThreadId $threadId
    if (-not $session) {
        $session = Get-NewestSessionAfter -Since $start
    }
    Save-SessionExcerpt -SessionPath $session.FullName -Destination $sessionExcerptPath
    Save-ProxyEventTail -Destination $proxyEventsPath

    $lastMessageText = ''
    if (Test-Path $lastMessagePath) {
        $lastMessageText = Get-Content -LiteralPath $lastMessagePath -Raw
    }
    $artifactText = @(
        $stdout,
        $stderr,
        (Get-Content -LiteralPath $sessionExcerptPath -Raw)
    ) -join "`n"

    $missing = @($Case.Expect | Where-Object { $lastMessageText -notmatch [regex]::Escape($_) })
    $rejected = @($Case.Reject | Where-Object { $lastMessageText -match [regex]::Escape($_) })
    $rejectedArtifacts = @($Case.RejectArtifact | Where-Object { $artifactText -match [regex]::Escape($_) })
    $status = if ($completed -and $process.ExitCode -eq 0 -and $missing.Count -eq 0 -and $rejected.Count -eq 0 -and $rejectedArtifacts.Count -eq 0) { 'passed' } else { 'failed' }

    [pscustomobject]@{
        name = $Case.Name
        model = $Case.Model
        config = $Case.Config
        status = $status
        exit_code = $process.ExitCode
        timed_out = -not $completed
        started_at = $start.ToString('o')
        ended_at = (Get-Date).ToString('o')
        stdout = $stdoutPath
        stderr = $stderrPath
        last_message = $lastMessagePath
        thread_id = $threadId
        session_jsonl = if ($session) { $session.FullName } else { $null }
        session_excerpt = $sessionExcerptPath
        proxy_events_tail = $proxyEventsPath
        missing = $missing
        rejected = $rejected
        rejected_artifacts = $rejectedArtifacts
    } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $metadataPath -Encoding UTF8

    Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$officialDirectConfig = @('model_provider="openai"')
$proxyConfig = @('model_provider="custom"')
if ($ProxyBaseUrl) {
    $proxyConfig += "model_providers.custom.base_url=`"$ProxyBaseUrl`""
}

$cases = @()
$cases += New-SmokeCase `
        -Name 'official-direct-node-repl' `
        -Model $OfficialDirectModel `
        -Prompt 'Regression smoke. Use native tool_search to search for node_repl js, then call the discovered mcp__node_repl.js tool to evaluate JavaScript string "official-direct-node-repl-ok". Do not use shell. Final answer must include SENTINEL:official-direct-node-repl-ok and the exact tool name you called.' `
        -Config $officialDirectConfig `
        -Expect @('SENTINEL:official-direct-node-repl-ok', 'mcp__node_repl') `
        -Reject @('browser tool not exposed') `
        -RejectArtifact @('unsupported call: tool_search')
$cases += New-SmokeCase `
        -Name 'official-proxy-node-repl' `
        -Model $OfficialProxyModel `
        -Prompt 'Regression smoke through the CodexHub official proxy route. Use native tool_search to search for node_repl js, then call the discovered mcp__node_repl.js tool to evaluate JavaScript string "official-proxy-node-repl-ok". Do not use shell. Final answer must include SENTINEL:official-proxy-node-repl-ok and the exact tool name you called.' `
        -Config $proxyConfig `
        -Expect @('SENTINEL:official-proxy-node-repl-ok', 'mcp__node_repl') `
        -Reject @('browser tool not exposed') `
        -RejectArtifact @('unsupported call: tool_search')
$cases += New-SmokeCase `
        -Name 'third-party-node-repl-direct' `
        -Model $ThirdPartyModel `
        -Prompt 'Regression smoke through the CodexHub third-party route. Do not call tool_search. Call the visible mcp__node_repl__js tool exactly once with JavaScript code nodeRepl.write("third-party-node-repl-ok"). Do not use shell. After that one tool result, stop tool use and write the final answer. Final answer must include SENTINEL:third-party-node-repl-ok and the exact tool name you called.' `
        -Config $proxyConfig `
        -Expect @('SENTINEL:third-party-node-repl-ok', 'mcp__node_repl') `
        -RejectArtifact @('unsupported call: tool_search')
$cases += New-SmokeCase `
        -Name 'third-party-subagent-lifecycle' `
        -Model $ThirdPartyModel `
        -Prompt 'Regression smoke through the CodexHub third-party route. Do not call tool_search. Run exactly one subagent lifecycle using visible tools: multi_agent_v1__spawn_agent with a child prompt that returns SENTINEL:third-party-subagent-child-ok, then multi_agent_v1__wait_agent, then multi_agent_v1__close_agent. Final answer must include SENTINEL:third-party-subagent-child-ok plus spawn/wait/close tool names.' `
        -Config $proxyConfig `
        -Expect @('SENTINEL:third-party-subagent-child-ok', 'multi_agent_v1') `
        -RejectArtifact @('unsupported call: tool_search')
$cases += New-SmokeCase `
        -Name 'official-proxy-subagent-discovery' `
        -Model $OfficialProxyModel `
        -Prompt 'Regression smoke through the CodexHub official proxy route. Use native tool_search to discover Codex multi_agent/subagent spawn_agent tools, then run exactly one lifecycle: spawn a child that returns SENTINEL:official-proxy-subagent-child-ok, wait for it, and close it. Final answer must include SENTINEL:official-proxy-subagent-child-ok plus spawn/wait/close tool names.' `
        -Config $proxyConfig `
        -Expect @('SENTINEL:official-proxy-subagent-child-ok', 'multi_agent_v1') `
        -Reject @('unsupported call: tool_search')
$cases += New-SmokeCase `
        -Name 'browser-negative-cli' `
        -Model $OfficialProxyModel `
        -Prompt "# In app browser`nCurrent URL: https://example.test/negative-smoke`nRegression smoke in a CLI/no-in-app-browser environment. Load and follow the browser:control-in-app-browser skill, use native tool_search for node_repl js if needed, and attempt to select the iab browser. If there is no in-app browser session, final answer must include SENTINEL:browser-session-unavailable and must not say browser tool not exposed." `
        -Config $proxyConfig `
        -Expect @('SENTINEL:browser-session-unavailable') `
        -Reject @('browser tool not exposed')

if ($RunBrowserSmoke) {
    $cases += New-SmokeCase `
        -Name 'browser-desktop-smoke' `
        -Model $OfficialProxyModel `
        -Prompt "# In app browser`nCurrent URL: https://example.test/browser-smoke`nRegression smoke in Codex Desktop with an in-app browser session. Load and follow browser:control-in-app-browser, use node_repl/browser-client.mjs to select iab, read browser.documentation(), then read the current page title and URL or take a screenshot/DOM read. Final answer must include SENTINEL:browser-desktop-smoke plus browser URL/title, or SENTINEL:browser-session-unavailable if no iab session exists." `
        -Config $proxyConfig `
        -Expect @('SENTINEL:browser-desktop-smoke') `
        -Reject @('browser tool not exposed')
}

if ($CaseName.Count -gt 0) {
    $wanted = @{}
    foreach ($name in $CaseName) {
        $wanted[$name] = $true
    }
    $cases = @($cases | Where-Object { $wanted.ContainsKey($_.Name) })
    if ($cases.Count -eq 0) {
        throw "No smoke cases matched -CaseName: $($CaseName -join ', ')"
    }
}

$results = foreach ($case in $cases) {
    Write-Host "Running $($case.Name) with $($case.Model)..."
    Invoke-CodexSmokeCase -Case $case
}

$summaryPath = Join-Path $OutputDir 'summary.json'
$results | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $summaryPath -Encoding UTF8
$results | Format-Table name, model, status, exit_code, timed_out -AutoSize
Write-Host "Saved smoke artifacts to $OutputDir"
