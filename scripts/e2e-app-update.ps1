[CmdletBinding()]
param(
    [int]$Port = 18765,
    [string]$BridgeUrl = "http://127.0.0.1:1421/api/invoke",
    [string]$AppExe = "",
    [string]$InstallerPath = "",
    [string]$SignaturePath = "",
    [string]$ReleaseRoot = (Join-Path $env:TEMP "codexhub-update-e2e"),
    [string]$CurrentVersion = "",
    [string]$NextVersion = "",
    [switch]$Launch,
    [switch]$Install,
    [switch]$KeepAlive,
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
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
    $candidate = Get-ChildItem -LiteralPath $bundleDir -Filter "CodexHub_*_x64-setup.exe" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime |
        Select-Object -Last 1

    if ($null -ne $candidate) {
        return $candidate.FullName
    }

    if ($AllowDummy) {
        $dummy = Join-Path $ReleaseRoot "CodexHub_${NextVersion}_x64-setup.exe"
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
    $targetAsset = Join-Path $ReleaseRoot $assetName
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

    $manifestPath = Join-Path $ReleaseRoot "latest.json"
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
    [System.Diagnostics.Process]::Start($info)
}

if ([string]::IsNullOrWhiteSpace($CurrentVersion)) {
    $CurrentVersion = Get-TauriVersion
}
if ([string]::IsNullOrWhiteSpace($NextVersion)) {
    $NextVersion = Get-NextPatchVersion $CurrentVersion
}

$manifestPath = Write-VirtualRelease
$manifestUrl = "http://127.0.0.1:$Port/latest.json"
Write-Host "Virtual release manifest: $manifestPath"
Write-Host "Virtual release endpoint: $manifestUrl"

if ($ValidateOnly) {
    $roundTrip = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    if ($roundTrip.version -ne $NextVersion -or -not $roundTrip.platforms."windows-x86_64") {
        throw "Generated latest.json failed validation."
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

    if ($Install) {
        try {
            Invoke-BridgeCommand "install_app_update" | Out-Null
        }
        catch {
            Write-Host "Install request ended while the app restarted: $($_.Exception.Message)"
        }
        Write-Host "Install path was invoked. Reopen CodexHub and verify the installed version if the updater did not restart automatically."
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
