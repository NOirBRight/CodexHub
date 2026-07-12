[CmdletBinding()]
param(
    [ValidateSet("stable", "beta")]
    [string]$Flavor = "stable",
    [int]$Port = 18765,
    [string]$BridgeUrl = "",
    [string]$AppExe = "",
    [string]$InstallerPath = "",
    [string]$SignaturePath = "",
    [string]$ReleaseRoot = (Join-Path $env:TEMP "codexhub-update-e2e"),
    [string]$CurrentVersion = "",
    [string]$NextVersion = "",
    [switch]$Launch,
    [switch]$Install,
    [switch]$DownloadOnly,
    [switch]$KeepAlive,
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$flavorManifest = Get-Content -Raw -LiteralPath (Join-Path $repoRoot "config\build-flavors.json") | ConvertFrom-Json
$flavorConfig = $flavorManifest.$Flavor
# Stable defaults remain latest.json / CodexHub_ / 1421; beta defaults remain latest-beta.json / CodexHubBeta_ / 1431.
if ($null -eq $flavorConfig) {
    throw "Unknown update E2E flavor: $Flavor"
}
if ([string]::IsNullOrWhiteSpace($BridgeUrl)) {
    $BridgeUrl = "http://127.0.0.1:$($flavorConfig.bridgePort)/api/invoke"
}
$tauriConfigPath = Join-Path $repoRoot "src-tauri\tauri.conf.json"

function Get-TauriVersion {
    $config = Get-Content -Raw -LiteralPath $tauriConfigPath | ConvertFrom-Json
    [string]$config.version
}

function Get-NextPatchVersion([string]$Version) {
    $parts = $Version.Split(".")
    if ($parts.Count -ne 3) {
        throw "Expected semantic version with three parts, got '$Version'."
    }
    $patch = [int]$parts[2] + 1
    "{0}.{1}.{2}" -f $parts[0], $parts[1], $patch
}

function Get-ReleaseAsset {
    param([bool]$AllowDummy)

    if (-not [string]::IsNullOrWhiteSpace($InstallerPath)) {
        return (Resolve-Path -LiteralPath $InstallerPath).Path
    }

    $bundleDir = Join-Path $repoRoot "src-tauri\target\release\bundle\nsis"
    $assetPrefix = [string]$flavorConfig.releaseAssetPrefix
    $candidate = Get-ChildItem -LiteralPath $bundleDir -Filter "${assetPrefix}_*_x64-setup.exe" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime |
        Select-Object -Last 1

    if ($null -ne $candidate) {
        return $candidate.FullName
    }

    if ($AllowDummy) {
        $dummy = Join-Path $ReleaseRoot "${assetPrefix}_${NextVersion}_x64-setup.exe"
        [System.IO.File]::WriteAllText($dummy, "virtual CodexHub update asset")
        return $dummy
    }

    throw "No NSIS installer found. Run scripts\build-windows-release.ps1 first or pass -InstallerPath."
}

function Get-AssetSignature([string]$AssetPath, [bool]$AllowDummy) {
    $sigPath = if ([string]::IsNullOrWhiteSpace($SignaturePath)) { "$AssetPath.sig" } else { $SignaturePath }
    if (Test-Path -LiteralPath $sigPath -PathType Leaf) {
        return (Get-Content -Raw -LiteralPath $sigPath).Trim()
    }
    if ($AllowDummy) {
        return "virtual-signature"
    }
    throw "No updater signature found. Expected '$sigPath' or pass -SignaturePath."
}

function Write-VirtualRelease {
    New-Item -ItemType Directory -Force -Path $ReleaseRoot | Out-Null

    $asset = Get-ReleaseAsset -AllowDummy:$ValidateOnly
    $assetName = Split-Path -Leaf $asset
    $targetAsset = [System.IO.Path]::GetFullPath((Join-Path $ReleaseRoot $assetName))
    if ((Resolve-Path -LiteralPath $asset).Path -ne $targetAsset) {
        Copy-Item -LiteralPath $asset -Destination $targetAsset -Force
    }

    $signature = Get-AssetSignature -AssetPath $asset -AllowDummy:$ValidateOnly
    $encodedAsset = [Uri]::EscapeDataString($assetName)
    $assetUrl = "http://127.0.0.1:$Port/$encodedAsset"

    $manifest = [ordered]@{
        version = $NextVersion
        notes = "virtual CodexHub update`n- E2E detection path`n- E2E install path"
        pub_date = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ", [Globalization.CultureInfo]::InvariantCulture)
        platforms = [ordered]@{
            "windows-x86_64-nsis" = [ordered]@{
                signature = $signature
                url = $assetUrl
            }
            "windows-x86_64" = [ordered]@{
                signature = $signature
                url = $assetUrl
            }
        }
    }

    $manifestName = [string]$flavorConfig.updaterManifestName
    $manifestPath = Join-Path $ReleaseRoot $manifestName
    $json = $manifest | ConvertTo-Json -Depth 8
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($manifestPath, $json + [Environment]::NewLine, $utf8NoBom)
    $manifestPath
}

function New-StaticServerScript {
    $scriptPath = Join-Path $ReleaseRoot "static-release-server.cjs"
    $source = @'
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(process.argv[2]);
const port = Number(process.argv[3]);

const server = http.createServer((request, response) => {
  const url = new URL(request.url, "http://127.0.0.1");
  const name = decodeURIComponent(url.pathname.replace(/^\/+/, "") || "latest.json");
  const filePath = path.resolve(root, name);
  if (!filePath.startsWith(root + path.sep) && filePath !== root) {
    response.writeHead(403);
    response.end("forbidden");
    return;
  }
  fs.readFile(filePath, (error, body) => {
    if (error) {
      response.writeHead(404);
      response.end("not found");
      return;
    }
    response.writeHead(200, {
      "content-type": name.endsWith(".json") ? "application/json" : "application/octet-stream",
      "content-length": body.length,
    });
    response.end(body);
  });
});

server.listen(port, "127.0.0.1", () => {
  console.log(`virtual CodexHub update server listening on ${port}`);
});

process.on("SIGTERM", () => server.close(() => process.exit(0)));
'@
    [System.IO.File]::WriteAllText($scriptPath, $source, [System.Text.UTF8Encoding]::new($false))
    $scriptPath
}

function Wait-Url([string]$Url, [int]$TimeoutSeconds = 20) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2 | Out-Null
            return
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    } while ((Get-Date) -lt $deadline)
    throw "Timed out waiting for $Url"
}

function Invoke-BridgeCommand([string]$Command) {
    $body = @{ command = $Command; args = @{} } | ConvertTo-Json -Depth 8
    $response = Invoke-RestMethod -Uri $BridgeUrl -Method Post -ContentType "application/json" -Body $body -TimeoutSec 120
    if ($response.ok -ne $true) {
        throw "Bridge command '$Command' failed: $($response.error)"
    }
    $response.value
}

function Wait-Bridge([int]$TimeoutSeconds = 30) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            Invoke-BridgeCommand "get_app_version" | Out-Null
            return
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    } while ((Get-Date) -lt $deadline)
    throw "Timed out waiting for CodexHub bridge at $BridgeUrl"
}

function Wait-InstallStatusPhase {
    param(
        [string[]]$ExpectedPhases,
        [int]$TimeoutSeconds = 120
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $status = Invoke-BridgeCommand "get_app_update_install_status"
        if ($status.phase -eq "failed") {
            throw "Update install failed: $($status.message)"
        }
        if ($ExpectedPhases -contains $status.phase) {
            return $status
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)

    throw "Timed out waiting for update install phase '$($ExpectedPhases -join ", ")'."
}

function Start-AppForE2E([string]$Endpoint) {
    if ([string]::IsNullOrWhiteSpace($AppExe)) {
        $script:AppExe = Join-Path $repoRoot "src-tauri\target\debug\codexhub.exe"
    }
    if (-not (Test-Path -LiteralPath $AppExe -PathType Leaf)) {
        throw "App executable not found: $AppExe. Build a debug app first with 'cargo build' in src-tauri."
    }

    $info = [System.Diagnostics.ProcessStartInfo]::new()
    $info.FileName = (Resolve-Path -LiteralPath $AppExe).Path
    $info.UseShellExecute = $false
    $info.Environment["CODEXHUB_UPDATE_E2E_ENDPOINT"] = $Endpoint
    if ($DownloadOnly) {
        $info.Environment["CODEXHUB_UPDATE_E2E_SKIP_INSTALL"] = "1"
    }
    [System.Diagnostics.Process]::Start($info)
}

if ([string]::IsNullOrWhiteSpace($CurrentVersion)) {
    $CurrentVersion = Get-TauriVersion
}
if ([string]::IsNullOrWhiteSpace($NextVersion)) {
    $NextVersion = Get-NextPatchVersion $CurrentVersion
}

$manifestPath = Write-VirtualRelease
$manifestName = [string]$flavorConfig.updaterManifestName
$manifestUrl = "http://127.0.0.1:$Port/$manifestName"
Write-Host "Virtual release manifest: $manifestPath"
Write-Host "Virtual release endpoint: $manifestUrl"

if ($ValidateOnly) {
    $roundTrip = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    if ($roundTrip.version -ne $NextVersion -or -not $roundTrip.platforms."windows-x86_64") {
        throw "Generated $manifestName failed validation."
    }
    Write-Host "Virtual release manifest validated."
    exit 0
}

$node = Get-Command node -ErrorAction Stop
$serverScript = New-StaticServerScript
$server = Start-Process -FilePath $node.Source -ArgumentList @($serverScript, $ReleaseRoot, "$Port") -PassThru -WindowStyle Hidden
$appProcess = $null

try {
    Wait-Url $manifestUrl

    if ($Launch) {
        $appProcess = Start-AppForE2E $manifestUrl
        Wait-Bridge 30
    }

    $status = Invoke-BridgeCommand "check_app_update"
    if ($status.available -ne $true -or $status.latest_version -ne $NextVersion) {
        throw "Expected update $NextVersion, got available=$($status.available), latest=$($status.latest_version)."
    }
    Write-Host "Detected virtual CodexHub update $($status.current_version) -> $($status.latest_version)."

    if ($DownloadOnly) {
        Invoke-BridgeCommand "start_app_update_install" | Out-Null
        $installStatus = Wait-InstallStatusPhase -ExpectedPhases @("restarting") -TimeoutSeconds 180
        if ($installStatus.target_version -ne $NextVersion) {
            throw "Expected download-only target $NextVersion, got $($installStatus.target_version)."
        }
        if ($installStatus.downloaded_bytes -le 0) {
            throw "Expected downloaded bytes to be recorded."
        }
        Write-Host "Download-only update path reached $($installStatus.phase) after $($installStatus.downloaded_bytes) bytes."
    }

    if ($Install) {
        try {
            Invoke-BridgeCommand "start_app_update_install" | Out-Null
            try {
                Wait-InstallStatusPhase -ExpectedPhases @("restarting") -TimeoutSeconds 180 | Out-Null
            }
            catch {
                Write-Host "Install status polling ended while the app restarted: $($_.Exception.Message)"
            }
        }
        catch {
            Write-Host "Install request ended while the app restarted: $($_.Exception.Message)"
        }
        if ($Launch -and $null -ne $appProcess) {
            $appProcess.WaitForExit(180000) | Out-Null
        }
        Wait-Bridge 120
        $versionInfo = Invoke-BridgeCommand "get_app_version"
        if ($versionInfo.current_version -ne $NextVersion) {
            throw "Expected installed version $NextVersion after quiet update, got $($versionInfo.current_version)."
        }
        try {
            $completion = Invoke-BridgeCommand "consume_app_update_completion"
            if ($null -ne $completion -and $completion.completed -ne $true) {
                throw "Pending update completion did not validate target $($completion.target_version)."
            }
        }
        catch {
            Write-Host "Completion status was already consumed or unavailable: $($_.Exception.Message)"
        }
        Write-Host "Quiet install verified. CodexHub restarted at $($versionInfo.current_version)."
    }

    if ($KeepAlive -and -not $Install) {
        Write-Host "KeepAlive enabled. Virtual release server and app will keep running for manual UI testing."
        Write-Host "  Server PID: $($server.Id)"
        if ($null -ne $appProcess) {
            Write-Host "  App PID:    $($appProcess.Id)"
        }
        Write-Host "  Bridge:     $BridgeUrl"
        Write-Host "  Endpoint:   $manifestUrl"
    }
}
finally {
    if (-not $KeepAlive -and $null -ne $appProcess -and -not $appProcess.HasExited -and -not $Install) {
        Stop-Process -Id $appProcess.Id -Force -ErrorAction SilentlyContinue
    }
    if (-not $KeepAlive -and $null -ne $server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    }
}
