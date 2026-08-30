use serde::{Deserialize, Serialize};
use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::path::Path;
#[cfg(target_os = "linux")]
use std::path::PathBuf;
use std::process::Command;
use std::thread;
use std::time::{Duration, Instant};

pub(crate) const RESTART_REQUIRED_ERROR: &str = "codex_desktop_restart_required";
pub(crate) const CLOSE_TIMEOUT_ERROR: &str = "codex_desktop_close_timeout";
pub(crate) const RESTART_UNSUPPORTED_ERROR: &str = "codex_desktop_restart_unsupported";
pub(crate) const SWITCH_REOPEN_FAILED_ERROR: &str = "codex_desktop_switch_failed_reopen_failed";
pub(crate) const SWITCH_RELAUNCH_FAILED_ERROR: &str =
    "codex_desktop_switched_relaunch_failed";
pub(crate) const SWITCH_STATE_UNCERTAIN_ERROR: &str = "codex_desktop_switch_state_uncertain";
pub(crate) const BECAME_RUNNING_ERROR: &str = "codex_desktop_became_running_before_commit";
const CODEX_CLOSE_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SwitchMutationError {
    message: String,
    rollback_failed: bool,
}

impl SwitchMutationError {
    pub(crate) fn rollback_failed(&self) -> bool {
        self.rollback_failed
    }
}

impl fmt::Display for SwitchMutationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl From<String> for SwitchMutationError {
    fn from(message: String) -> Self {
        Self {
            message,
            rollback_failed: false,
        }
    }
}

impl From<crate::file_transaction::FileTransactionError> for SwitchMutationError {
    fn from(error: crate::file_transaction::FileTransactionError) -> Self {
        Self {
            message: error.to_string(),
            rollback_failed: error.rollback_failed(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct CodexDesktopStatus {
    pub running: bool,
    pub restart_supported: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CodexRestartResult {
    NotRunning,
    Restarted,
    SwitchFailedReopened,
    SwitchedRelaunchFailed,
}

#[derive(Debug)]
pub(crate) struct CoordinatedSwitch<T> {
    pub(crate) value: Option<T>,
    pub(crate) restart_result: CodexRestartResult,
    pub(crate) switch_error: Option<String>,
}

pub(crate) trait CodexDesktopLifecycle {
    fn status(&self) -> Result<CodexDesktopStatus, String>;
    fn request_close(&self, timeout: Duration) -> Result<(), String>;
    fn wait_for_running(&self, running: bool, timeout: Duration) -> Result<bool, String>;
    fn launch(&self) -> Result<(), String>;
}

pub(crate) struct SystemCodexDesktopLifecycle;

pub(crate) fn status() -> Result<CodexDesktopStatus, String> {
    SystemCodexDesktopLifecycle.status()
}

pub(crate) fn launch() -> Result<(), String> {
    SystemCodexDesktopLifecycle.launch()
}

pub(crate) fn coordinate_switch<T, F, E>(
    restart_codex: bool,
    switch: F,
) -> Result<CoordinatedSwitch<T>, String>
where
    F: FnOnce() -> Result<T, E>,
    E: Into<SwitchMutationError>,
{
    let lock_path = switch_lock_path()?;
    coordinate_switch_with(
        &SystemCodexDesktopLifecycle,
        &lock_path,
        restart_codex,
        switch,
    )
}

/// Run a background Codex configuration mutation only when Codex Desktop is
/// stopped. Automatic work is never authorized to close or relaunch Codex.
pub(crate) fn coordinate_unattended<T, F, E>(mutation: F) -> Result<Option<T>, E>
where
    F: FnOnce() -> Result<T, E>,
    E: From<String>,
{
    let lock_path = switch_lock_path().map_err(E::from)?;
    coordinate_unattended_with(&SystemCodexDesktopLifecycle, &lock_path, mutation)
}

/// Serialize a non-lifecycle writer with every Codex configuration and
/// Official publication transaction. This prevents a rollback snapshot from
/// overwriting a user setting saved during the transaction.
pub(crate) fn serialize_config_writer<T>(writer: impl FnOnce() -> Result<T, String>) -> Result<T, String> {
    let lock_path = switch_lock_path()?;
    serialize_config_writer_with_path(&lock_path, writer)
}

fn serialize_config_writer_with_path<T>(
    lock_path: &Path,
    writer: impl FnOnce() -> Result<T, String>,
) -> Result<T, String> {
    let _lock = acquire_switch_lock(lock_path)?;
    writer()
}

fn switch_lock_path() -> Result<std::path::PathBuf, String> {
    Ok(crate::runtime_paths::codex_target_home_dir()?
        .join("proxy")
        .join("codex-desktop-switch.lock"))
}

/// Recheck the process immediately before a caller's Codex-directory commit.
/// The caller must already hold the shared switch lock through one of the
/// coordinators above; this second check closes the long-network-work race.
pub(crate) fn run_if_stopped<T, F, E>(mutation: F) -> Result<Option<T>, E>
where
    F: FnOnce() -> Result<T, E>,
    E: From<String>,
{
    run_if_stopped_with(&SystemCodexDesktopLifecycle, mutation)
}

fn run_if_stopped_with<T, B, F, E>(backend: &B, mutation: F) -> Result<Option<T>, E>
where
    B: CodexDesktopLifecycle,
    F: FnOnce() -> Result<T, E>,
    E: From<String>,
{
    if backend.status().map_err(E::from)?.running {
        return Ok(None);
    }
    mutation().map(Some)
}

fn coordinate_unattended_with<T, B, F, E>(
    backend: &B,
    lock_path: &Path,
    mutation: F,
) -> Result<Option<T>, E>
where
    B: CodexDesktopLifecycle,
    F: FnOnce() -> Result<T, E>,
    E: From<String>,
{
    let _lock = acquire_switch_lock(lock_path).map_err(E::from)?;
    if backend.status().map_err(E::from)?.running {
        return Ok(None);
    }
    mutation().map(Some)
}

pub(crate) fn coordinate_switch_with<T, B, F, E>(
    backend: &B,
    lock_path: &Path,
    restart_codex: bool,
    switch: F,
) -> Result<CoordinatedSwitch<T>, String>
where
    B: CodexDesktopLifecycle,
    F: FnOnce() -> Result<T, E>,
    E: Into<SwitchMutationError>,
{
    coordinate_switch_with_timeout(backend, lock_path, restart_codex, CODEX_CLOSE_TIMEOUT, switch)
}

fn coordinate_switch_with_timeout<T, B, F, E>(
    backend: &B,
    lock_path: &Path,
    restart_codex: bool,
    close_timeout: Duration,
    switch: F,
) -> Result<CoordinatedSwitch<T>, String>
where
    B: CodexDesktopLifecycle,
    F: FnOnce() -> Result<T, E>,
    E: Into<SwitchMutationError>,
{
    let _lock = acquire_switch_lock(lock_path)?;
    let status = backend.status()?;
    let initially_running = status.running;
    if initially_running && !restart_codex {
        return Err(format!(
            "{RESTART_REQUIRED_ERROR}: Codex Desktop is running; explicit restart authorization is required"
        ));
    }
    if initially_running && !status.restart_supported {
        return Err(format!(
            "{RESTART_UNSUPPORTED_ERROR}: the installed Codex Desktop lifecycle is not supported"
        ));
    }

    if initially_running {
        let close_started = Instant::now();
        backend.request_close(close_timeout)?;
        let elapsed = close_started.elapsed();
        if elapsed >= close_timeout {
            return Err(format!(
                "{CLOSE_TIMEOUT_ERROR}: Codex Desktop close exceeded 10 seconds; configuration was not changed"
            ));
        }
        let remaining = close_timeout - elapsed;
        if !backend.wait_for_running(false, remaining)? {
            return Err(format!(
                "{CLOSE_TIMEOUT_ERROR}: Codex Desktop did not exit within 10 seconds; configuration was not changed"
            ));
        }
    }

    match switch().map_err(Into::into) {
        Ok(value) if !initially_running => Ok(CoordinatedSwitch {
            value: Some(value),
            restart_result: CodexRestartResult::NotRunning,
            switch_error: None,
        }),
        Ok(value) => {
            let relaunched = backend.launch().and_then(|()| {
                backend
                    .wait_for_running(true, Duration::from_secs(15))
                    .and_then(|running| {
                        running.then_some(()).ok_or_else(|| {
                            "Codex Desktop did not appear within 15 seconds".to_string()
                        })
                    })
            });
            Ok(CoordinatedSwitch {
                value: Some(value),
                restart_result: if relaunched.is_ok() {
                    CodexRestartResult::Restarted
                } else {
                    CodexRestartResult::SwitchedRelaunchFailed
                },
                switch_error: relaunched.err(),
            })
        }
        Err(switch_error) => {
            if switch_error.rollback_failed() {
                let containment = backend.status().and_then(|status| {
                    if !status.running {
                        return Ok(());
                    }
                    if !restart_codex {
                        return Err(
                            "Codex Desktop started during the transaction, but this caller did not authorize process control"
                                .to_string(),
                        );
                    }
                    let close_started = Instant::now();
                    backend.request_close(close_timeout)?;
                    let elapsed = close_started.elapsed();
                    if elapsed >= close_timeout {
                        return Err(
                            "Codex Desktop close exceeded 10 seconds after the rollback failure"
                                .to_string(),
                        );
                    }
                    let remaining = close_timeout - elapsed;
                    if backend.wait_for_running(false, remaining)? {
                        Ok(())
                    } else {
                        Err(
                            "Codex Desktop did not exit within 10 seconds after the rollback failure"
                                .to_string(),
                        )
                    }
                });
                return Err(match containment {
                    Ok(()) => format!(
                        "{SWITCH_STATE_UNCERTAIN_ERROR}: the configuration transaction and its rollback both failed; Codex Desktop remains closed. Restore the reported files from a known-good backup, then start Codex Desktop manually. Details: {switch_error}"
                    ),
                    Err(containment_error) => format!(
                        "{SWITCH_STATE_UNCERTAIN_ERROR}: the configuration transaction and its rollback both failed; Codex Desktop may still be running because its state could not be contained ({containment_error}). Close Codex Desktop manually before restoring the reported files from a known-good backup. Details: {switch_error}"
                    ),
                });
            }
            if !initially_running {
                return Err(switch_error.to_string());
            }
            let switch_error = switch_error.to_string();
            if !backend.status()?.running {
                backend.launch().map_err(|reopen_error| {
                    format!(
                        "{SWITCH_REOPEN_FAILED_ERROR}: switch failed ({switch_error}); reopening the original Codex Desktop failed ({reopen_error})"
                    )
                })?;
            }
            if !backend.wait_for_running(true, Duration::from_secs(15))? {
                return Err(format!(
                    "{SWITCH_REOPEN_FAILED_ERROR}: switch failed ({switch_error}); the original Codex Desktop did not reappear within 15 seconds"
                ));
            }
            Ok(CoordinatedSwitch {
                value: None,
                restart_result: CodexRestartResult::SwitchFailedReopened,
                switch_error: Some(switch_error),
            })
        }
    }
}

fn acquire_switch_lock(path: &Path) -> Result<File, String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            format!(
                "failed to create Codex Desktop switch lock directory {}: {error}",
                parent.display()
            )
        })?;
    }
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(path)
        .map_err(|error| {
            format!(
                "failed to open Codex Desktop switch lock {}: {error}",
                path.display()
            )
        })?;
    file.lock().map_err(|error| {
        format!(
            "failed to acquire Codex Desktop switch lock {}: {error}",
            path.display()
        )
    })?;
    Ok(file)
}

#[cfg(target_os = "linux")]
fn wait_for_status<B: CodexDesktopLifecycle>(
    backend: &B,
    running: bool,
    timeout: Duration,
) -> Result<bool, String> {
    let started = Instant::now();
    loop {
        if backend.status()?.running == running {
            return Ok(true);
        }
        if started.elapsed() >= timeout {
            return Ok(false);
        }
        thread::sleep(Duration::from_millis(100));
    }
}

#[cfg(target_os = "linux")]
#[derive(Debug, Clone)]
struct LinuxCodexInstallation {
    launch_path: PathBuf,
    executable_path: PathBuf,
}

#[cfg(target_os = "linux")]
fn detect_linux_installation() -> Option<LinuxCodexInstallation> {
    let mut candidates = Vec::new();
    if let Some(path) = std::env::var_os("CODEXHUB_CODEX_DESKTOP")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
    {
        candidates.push(path);
    }
    if let Ok(path) = which::which("chatgpt") {
        candidates.push(path);
    }
    candidates.push(PathBuf::from("/usr/lib/chatgpt/codex-launcher"));
    candidates.push(PathBuf::from("/usr/bin/chatgpt"));
    if let Ok(path) = which::which("Codex") {
        candidates.push(path);
    }

    candidates.into_iter().find_map(|launch_path| {
        if !launch_path.is_file() {
            return None;
        }
        let canonical_launch = fs::canonicalize(&launch_path).ok()?;
        let sibling_binary = canonical_launch
            .parent()
            .map(|parent| parent.join("ChatGPT"))
            .filter(|path| path.is_file());
        let executable_path = sibling_binary
            .and_then(|path| fs::canonicalize(path).ok())
            .unwrap_or_else(|| canonical_launch.clone());
        Some(LinuxCodexInstallation {
            launch_path,
            executable_path,
        })
    })
}

#[cfg(target_os = "linux")]
fn linux_main_process_ids(proc_root: &Path, executable: &Path) -> Result<Vec<u32>, String> {
    let mut pids = Vec::new();
    let entries = fs::read_dir(proc_root)
        .map_err(|error| format!("failed to inspect {}: {error}", proc_root.display()))?;
    for entry in entries.flatten() {
        let Some(pid) = entry
            .file_name()
            .to_str()
            .and_then(|name| name.parse::<u32>().ok())
        else {
            continue;
        };
        if pid == std::process::id() {
            continue;
        }
        let process_root = entry.path();
        let Ok(process_executable) = fs::read_link(process_root.join("exe")) else {
            continue;
        };
        if process_executable != executable {
            continue;
        }
        let command_line = fs::read(process_root.join("cmdline")).unwrap_or_default();
        let is_helper = command_line
            .split(|byte| *byte == 0)
            .skip(1)
            .any(|argument| argument.starts_with(b"--type="));
        if !is_helper {
            pids.push(pid);
        }
    }
    pids.sort_unstable();
    Ok(pids)
}

#[cfg(target_os = "linux")]
impl CodexDesktopLifecycle for SystemCodexDesktopLifecycle {
    fn status(&self) -> Result<CodexDesktopStatus, String> {
        let Some(installation) = detect_linux_installation() else {
            return Ok(CodexDesktopStatus {
                running: false,
                restart_supported: false,
            });
        };
        Ok(CodexDesktopStatus {
            running: !linux_main_process_ids(Path::new("/proc"), &installation.executable_path)?
                .is_empty(),
            restart_supported: true,
        })
    }

    fn request_close(&self, _timeout: Duration) -> Result<(), String> {
        let installation = detect_linux_installation().ok_or_else(|| {
            "Codex Desktop is not installed or its executable path cannot be normalized".to_string()
        })?;
        for pid in linux_main_process_ids(Path::new("/proc"), &installation.executable_path)? {
            let status = Command::new("/bin/kill")
                .args(["-TERM", &pid.to_string()])
                .status()
                .map_err(|error| format!("failed to send SIGTERM to Codex Desktop: {error}"))?;
            if !status.success() {
                return Err(format!(
                    "failed to send SIGTERM to the exact Codex Desktop process {pid}"
                ));
            }
        }
        Ok(())
    }

    fn wait_for_running(&self, running: bool, timeout: Duration) -> Result<bool, String> {
        wait_for_status(self, running, timeout)
    }

    fn launch(&self) -> Result<(), String> {
        let installation = detect_linux_installation().ok_or_else(|| {
            "Codex Desktop is not installed. Install it or set CODEXHUB_CODEX_DESKTOP.".to_string()
        })?;
        Command::new(&installation.launch_path)
            .spawn()
            .map_err(|error| {
                format!(
                    "failed to open Codex Desktop at {}: {error}",
                    installation.launch_path.display()
                )
            })?;
        Ok(())
    }
}

#[cfg(target_os = "windows")]
fn run_windows_lifecycle_script(script: &str, timeout: Duration) -> Result<String, String> {
    use std::os::windows::process::CommandExt;
    use std::process::Stdio;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    let mut child = Command::new("powershell.exe")
        .args(["-NoProfile", "-NonInteractive", "-Command", script])
        .creation_flags(CREATE_NO_WINDOW)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("failed to run Codex Desktop lifecycle command: {error}"))?;
    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if started.elapsed() < timeout => {
                thread::sleep(Duration::from_millis(50));
            }
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!(
                    "Codex Desktop lifecycle command timed out after {} ms",
                    timeout.as_millis()
                ));
            }
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!(
                    "failed to wait for Codex Desktop lifecycle command: {error}"
                ));
            }
        }
    }
    let output = child
        .wait_with_output()
        .map_err(|error| format!("failed to collect Codex Desktop lifecycle output: {error}"))?;
    if !output.status.success() {
        let error = String::from_utf8_lossy(&output.stderr).trim().to_string();
        return Err(if error.is_empty() {
            "Codex Desktop lifecycle command failed".to_string()
        } else {
            error
        });
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

#[cfg(any(target_os = "windows", test))]
const WINDOWS_APPX_RESOLUTION: &str = r#"
$ErrorActionPreference = 'Stop'
$packages = @(Get-AppxPackage -Name 'OpenAI.Codex' -ErrorAction Stop)
if ($packages.Count -eq 0) { Write-Output 'unsupported'; exit 0 }
if ($packages.Count -ne 1) { throw 'Expected exactly one OpenAI.Codex AppX package.' }
$package = $packages[0]
$manifest = Get-AppxPackageManifest -Package $package
$applications = @($manifest.Package.Applications.Application | Where-Object { [string]$_.EntryPoint -eq 'Windows.FullTrustApplication' })
if ($applications.Count -ne 1) { throw 'Expected exactly one Windows.FullTrustApplication in the OpenAI.Codex manifest.' }
$application = $applications[0]
$relativeExecutable = [string]$application.Executable
if ([string]::IsNullOrWhiteSpace($relativeExecutable) -or [IO.Path]::IsPathRooted($relativeExecutable)) { throw 'The OpenAI.Codex executable must be a non-empty relative path.' }
$segments = @($relativeExecutable -split '[\\/]')
if ($segments.Count -eq 0 -or @($segments | Where-Object { $_ -eq '' -or $_ -eq '.' -or $_ -eq '..' }).Count -gt 0) { throw 'The OpenAI.Codex executable contains an unsafe path segment.' }
$installRoot = [IO.Path]::GetFullPath([string]$package.InstallLocation).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
$installPrefix = $installRoot + [IO.Path]::DirectorySeparatorChar
$executable = [IO.Path]::GetFullPath((Join-Path $installRoot $relativeExecutable))
if (-not $executable.StartsWith($installPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'The OpenAI.Codex executable escapes its AppX install location.' }
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw 'The OpenAI.Codex executable does not exist.' }
$aumid = $package.PackageFamilyName + '!' + [string]$application.Id
"#;

#[cfg(any(target_os = "windows", test))]
const WINDOWS_RESTART_MANAGER_TYPE: &str = r#"
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

public static class CodexHubRestartManager
{
    [StructLayout(LayoutKind.Sequential)]
    private struct FileTime
    {
        public uint Low;
        public uint High;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct UniqueProcess
    {
        public uint ProcessId;
        public FileTime StartTime;
    }

    private const uint ProcessQueryLimitedInformation = 0x1000;
    private const uint WaitTimeout = 258;
    private static int lastCancellationRequested;

    public static bool LastCancellationRequested {
        get { return Interlocked.CompareExchange(ref lastCancellationRequested, 0, 0) != 0; }
    }
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(
        uint desiredAccess,
        bool inheritHandle,
        uint processId
    );

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool QueryFullProcessImageName(
        IntPtr process,
        uint flags,
        StringBuilder executablePath,
        ref uint executablePathLength
    );

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetProcessTimes(
        IntPtr process,
        out FileTime creationTime,
        out FileTime exitTime,
        out FileTime kernelTime,
        out FileTime userTime
    );

    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    private static extern uint RmStartSession(
        out uint sessionHandle,
        uint sessionFlags,
        StringBuilder sessionKey
    );

    [DllImport("rstrtmgr.dll")]
    private static extern uint RmRegisterResources(
        uint sessionHandle,
        uint fileCount,
        IntPtr fileNames,
        uint applicationCount,
        [In] UniqueProcess[] applications,
        uint serviceCount,
        IntPtr serviceNames
    );

    [DllImport("rstrtmgr.dll")]
    private static extern uint RmShutdown(
        uint sessionHandle,
        uint actionFlags,
        IntPtr statusCallback
    );

    [DllImport("rstrtmgr.dll")]
    private static extern uint RmEndSession(uint sessionHandle);

    [DllImport("rstrtmgr.dll")]
    private static extern uint RmCancelCurrentTask(uint sessionHandle);

    public static uint ProbeSession()
    {
        uint sessionHandle;
        var sessionKey = new StringBuilder(64);
        uint result = RmStartSession(out sessionHandle, 0, sessionKey);
        if (result == 0) RmEndSession(sessionHandle);
        return result;
    }

    public static uint GracefulShutdown(
        uint processId,
        string expectedExecutablePath,
        int timeoutMilliseconds
    )
    {
        Interlocked.Exchange(ref lastCancellationRequested, 0);
        var preparationDeadline = System.Diagnostics.Stopwatch.StartNew();
        IntPtr processHandle = OpenProcess(ProcessQueryLimitedInformation, false, processId);
        if (processHandle == IntPtr.Zero)
            throw new InvalidOperationException(
                "OpenProcess failed with code " + Marshal.GetLastWin32Error() + "."
            );

        uint sessionHandle = 0;
        var sessionKey = new StringBuilder(64);
        bool sessionStarted = false;
        try
        {
            var actualExecutablePath = new StringBuilder(32768);
            uint actualExecutablePathLength = (uint)actualExecutablePath.Capacity;
            if (!QueryFullProcessImageName(
                processHandle,
                0,
                actualExecutablePath,
                ref actualExecutablePathLength
            ))
                throw new InvalidOperationException(
                    "QueryFullProcessImageName failed with code " +
                    Marshal.GetLastWin32Error() + "."
                );
            if (!String.Equals(
                Path.GetFullPath(actualExecutablePath.ToString()),
                Path.GetFullPath(expectedExecutablePath),
                StringComparison.OrdinalIgnoreCase
            ))
                throw new InvalidOperationException(
                    "The Codex Desktop process identity changed before shutdown."
                );

            FileTime creationTime;
            FileTime exitTime;
            FileTime kernelTime;
            FileTime userTime;
            if (!GetProcessTimes(
                processHandle,
                out creationTime,
                out exitTime,
                out kernelTime,
                out userTime
            ))
                throw new InvalidOperationException(
                    "GetProcessTimes failed with code " + Marshal.GetLastWin32Error() + "."
                );

            uint result = RmStartSession(out sessionHandle, 0, sessionKey);
            if (result != 0) return result;
            sessionStarted = true;
            var process = new UniqueProcess {
                ProcessId = processId,
                StartTime = creationTime
            };
            result = RmRegisterResources(
                sessionHandle,
                0,
                IntPtr.Zero,
                1,
                new[] { process },
                0,
                IntPtr.Zero
            );
            if (result != 0) return result;

            int shutdownTimeoutMilliseconds = timeoutMilliseconds -
                (int)preparationDeadline.ElapsedMilliseconds;
            if (shutdownTimeoutMilliseconds <= 0) return WaitTimeout;

            var shutdownFinished = new ManualResetEvent(false);
            var watchdogThread = new Thread(() => {
                if (!shutdownFinished.WaitOne(shutdownTimeoutMilliseconds))
                {
                    Interlocked.Exchange(ref lastCancellationRequested, 1);
                    RmCancelCurrentTask(sessionHandle);
                }
            });
            watchdogThread.IsBackground = true;
            watchdogThread.Start();
            uint shutdownResult;
            try
            {
                shutdownResult = RmShutdown(sessionHandle, 0, IntPtr.Zero);
            }
            finally
            {
                shutdownFinished.Set();
                watchdogThread.Join();
                shutdownFinished.Dispose();
            }
            return shutdownResult;
        }
        finally
        {
            if (sessionStarted) RmEndSession(sessionHandle);
            CloseHandle(processHandle);
        }
    }
}
'@
"#;

#[cfg(any(target_os = "windows", test))]
const WINDOWS_RESTART_MANAGER_CLOSE: &str = r#"
$matches = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and [IO.Path]::GetFullPath($_.ExecutablePath) -ieq $executable })
if ($matches.Count -eq 0) { exit 0 }
$matchIds = @($matches | ForEach-Object { [uint32]$_.ProcessId })
$roots = @($matches | Where-Object { $matchIds -notcontains [uint32]$_.ParentProcessId })
if ($roots.Count -ne 1) { throw 'Expected exactly one Codex Desktop process-tree root.' }
$root = $roots[0]
$restartManagerTimeoutMilliseconds = [int]($closeTimeoutMilliseconds - $closeDeadline.ElapsedMilliseconds - 500)
if ($restartManagerTimeoutMilliseconds -le 0) { throw 'Codex Desktop close deadline expired before Restart Manager shutdown.' }
$result = [CodexHubRestartManager]::GracefulShutdown([uint32]$root.ProcessId, $executable, $restartManagerTimeoutMilliseconds)
if ($result -ne 0) { throw "Restart Manager graceful shutdown failed with code $result." }
"#;

#[cfg(any(target_os = "windows", test))]
fn parse_windows_status(output: &str) -> Result<CodexDesktopStatus, String> {
    match output.trim() {
        "unsupported" => Ok(CodexDesktopStatus {
            running: false,
            restart_supported: false,
        }),
        "supported=1 running=0" => Ok(CodexDesktopStatus {
            running: false,
            restart_supported: true,
        }),
        "supported=1 running=1" => Ok(CodexDesktopStatus {
            running: true,
            restart_supported: true,
        }),
        other => Err(format!(
            "Codex Desktop lifecycle returned an unrecognized status: {other:?}"
        )),
    }
}

#[cfg(target_os = "windows")]
impl CodexDesktopLifecycle for SystemCodexDesktopLifecycle {
    fn status(&self) -> Result<CodexDesktopStatus, String> {
        let script = format!(
            "{}\nif (-not $executable) {{ exit 0 }}\n$running = @(Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -and [IO.Path]::GetFullPath($_.ExecutablePath) -ieq $executable }}).Count -gt 0\nWrite-Output ('supported=1 running=' + [int]$running)",
            WINDOWS_APPX_RESOLUTION
        );
        parse_windows_status(&run_windows_lifecycle_script(
            &script,
            Duration::from_secs(5),
        )?)
    }

    fn request_close(&self, timeout: Duration) -> Result<(), String> {
        let timeout_ms = timeout.as_millis();
        let script = format!(
            "$closeDeadline = [Diagnostics.Stopwatch]::StartNew()\n$closeTimeoutMilliseconds = {timeout_ms}\n{WINDOWS_APPX_RESOLUTION}\n{WINDOWS_RESTART_MANAGER_TYPE}\n{WINDOWS_RESTART_MANAGER_CLOSE}"
        );
        run_windows_lifecycle_script(&script, timeout).map(|_| ())
    }

    fn wait_for_running(&self, running: bool, timeout: Duration) -> Result<bool, String> {
        let expected = if running { 1 } else { 0 };
        let timeout_ms = timeout.as_millis();
        let script = format!(
            "{}\n$deadline = [Diagnostics.Stopwatch]::StartNew()\ndo {{\n  $isRunning = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {{ $_.ExecutablePath -and [IO.Path]::GetFullPath($_.ExecutablePath) -ieq $executable }}).Count -gt 0\n  if ([int]$isRunning -eq {}) {{ Write-Output 'matched=1'; exit 0 }}\n  Start-Sleep -Milliseconds 100\n}} while ($deadline.ElapsedMilliseconds -lt {})\nWrite-Output 'matched=0'",
            WINDOWS_APPX_RESOLUTION, expected, timeout_ms
        );
        let output = run_windows_lifecycle_script(&script, timeout + Duration::from_secs(2))?;
        match output.trim() {
            "matched=1" => Ok(true),
            "matched=0" => Ok(false),
            "unsupported" => Ok(false),
            other => Err(format!(
                "Codex Desktop lifecycle returned an unrecognized wait result: {other:?}"
            )),
        }
    }

    fn launch(&self) -> Result<(), String> {
        let script = format!(
            "{}\nif (-not $aumid) {{ throw 'Codex Desktop AppX manifest was not found.' }}\nStart-Process ('shell:AppsFolder\\' + $aumid)",
            WINDOWS_APPX_RESOLUTION
        );
        run_windows_lifecycle_script(&script, Duration::from_secs(5)).map(|_| ())
    }
}

#[cfg(not(any(target_os = "linux", target_os = "windows")))]
impl CodexDesktopLifecycle for SystemCodexDesktopLifecycle {
    fn status(&self) -> Result<CodexDesktopStatus, String> {
        Ok(CodexDesktopStatus {
            running: false,
            restart_supported: false,
        })
    }

    fn request_close(&self, _timeout: Duration) -> Result<(), String> {
        Err(RESTART_UNSUPPORTED_ERROR.to_string())
    }

    fn wait_for_running(&self, _running: bool, _timeout: Duration) -> Result<bool, String> {
        Ok(false)
    }

    fn launch(&self) -> Result<(), String> {
        Err(RESTART_UNSUPPORTED_ERROR.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::{Cell, RefCell};
    use std::path::PathBuf;
    use std::sync::mpsc;

    struct FakeLifecycle {
        running: Cell<bool>,
        close_completes: bool,
        launch_succeeds: bool,
        events: RefCell<Vec<&'static str>>,
    }

    impl FakeLifecycle {
        fn running() -> Self {
            Self {
                running: Cell::new(true),
                close_completes: true,
                launch_succeeds: true,
                events: RefCell::new(Vec::new()),
            }
        }
    }

    impl CodexDesktopLifecycle for FakeLifecycle {
        fn status(&self) -> Result<CodexDesktopStatus, String> {
            self.events.borrow_mut().push("status");
            Ok(CodexDesktopStatus {
                running: self.running.get(),
                restart_supported: true,
            })
        }

        fn request_close(&self, _timeout: Duration) -> Result<(), String> {
            self.events.borrow_mut().push("close");
            Ok::<(), String>(())
        }

        fn wait_for_running(&self, running: bool, _timeout: Duration) -> Result<bool, String> {
            self.events.borrow_mut().push(if running {
                "wait-running"
            } else {
                "wait-stopped"
            });
            if !running && self.close_completes {
                self.running.set(false);
                return Ok(true);
            }
            if running && self.launch_succeeds {
                self.running.set(true);
                return Ok(true);
            }
            Ok(false)
        }

        fn launch(&self) -> Result<(), String> {
            self.events.borrow_mut().push("launch");
            if self.launch_succeeds {
                Ok(())
            } else {
                Err("launch failed".to_string())
            }
        }
    }

    fn lock_path(name: &str) -> PathBuf {
        let thread_name = std::thread::current()
            .name()
            .unwrap_or("test")
            .chars()
            .map(|character| {
                if character.is_ascii_alphanumeric() || matches!(character, '-' | '_' | '.') {
                    character
                } else {
                    '_'
                }
            })
            .collect::<String>();
        std::env::temp_dir().join(format!(
            "codexhub-codex-desktop-{name}-{}-{}.lock",
            std::process::id(),
            thread_name
        ))
    }

    #[test]
    fn running_desktop_without_authorization_does_not_mutate() {
        let backend = FakeLifecycle::running();
        let mutated = Cell::new(false);

        let error = coordinate_switch_with(&backend, &lock_path("unauthorized"), false, || {
            mutated.set(true);
            Ok::<(), String>(())
        })
        .expect_err("authorization error");

        assert!(error.contains(RESTART_REQUIRED_ERROR));
        assert!(!mutated.get());
        assert_eq!(&*backend.events.borrow(), &["status"]);
    }

    #[test]
    fn authorized_switch_closes_mutates_and_relaunches_in_order() {
        let backend = FakeLifecycle::running();

        let result = coordinate_switch_with(&backend, &lock_path("restart"), true, || {
            backend.events.borrow_mut().push("switch");
            Ok::<&str, String>("switched")
        })
        .expect("coordinated switch");

        assert_eq!(result.value, Some("switched"));
        assert_eq!(result.restart_result, CodexRestartResult::Restarted);
        assert_eq!(
            &*backend.events.borrow(),
            &[
                "status",
                "close",
                "wait-stopped",
                "switch",
                "launch",
                "wait-running"
            ]
        );
    }

    #[test]
    fn close_timeout_aborts_before_mutation_or_force_kill() {
        let backend = FakeLifecycle {
            close_completes: false,
            ..FakeLifecycle::running()
        };
        let mutated = Cell::new(false);

        let error = coordinate_switch_with(&backend, &lock_path("timeout"), true, || {
            mutated.set(true);
            Ok::<(), String>(())
        })
        .expect_err("close timeout");

        assert!(error.contains(CLOSE_TIMEOUT_ERROR));
        assert!(!mutated.get());
        assert_eq!(
            &*backend.events.borrow(),
            &["status", "close", "wait-stopped"]
        );
    }

    #[test]
    fn switch_failure_reopens_original_desktop() {
        let backend = FakeLifecycle::running();

        let result = coordinate_switch_with::<(), _, _, _>(
            &backend,
            &lock_path("switch-failure"),
            true,
            || Err("config failed".to_string()),
        )
        .expect("failure outcome");

        assert_eq!(result.value, None);
        assert_eq!(result.switch_error.as_deref(), Some("config failed"));
        assert_eq!(
            result.restart_result,
            CodexRestartResult::SwitchFailedReopened
        );
        assert!(backend.running.get());
    }

    #[test]
    fn rollback_failure_keeps_desktop_closed_and_requires_manual_recovery() {
        let backend = FakeLifecycle::running();

        let error = coordinate_switch_with::<(), _, _, _>(
            &backend,
            &lock_path("rollback-failure"),
            true,
            || {
                backend.events.borrow_mut().push("switch");
                Err(crate::file_transaction::FileTransactionError::RollbackFailed {
                    operation: "publish failed".to_string(),
                    rollback: vec!["injected restore failure".to_string()],
                })
            },
        )
        .expect_err("uncertain transaction state");

        assert!(error.contains(SWITCH_STATE_UNCERTAIN_ERROR));
        assert!(error.contains("remains closed"));
        assert!(!backend.running.get());
        assert_eq!(
            &*backend.events.borrow(),
            &["status", "close", "wait-stopped", "switch", "status"]
        );
    }

    #[test]
    fn rollback_failure_closes_desktop_that_reappeared_during_the_transaction() {
        let backend = FakeLifecycle::running();

        let error = coordinate_switch_with::<(), _, _, _>(
            &backend,
            &lock_path("rollback-failure-race"),
            true,
            || {
                backend.events.borrow_mut().push("switch");
                backend.running.set(true);
                Err(crate::file_transaction::FileTransactionError::RollbackFailed {
                    operation: "publish failed".to_string(),
                    rollback: vec!["injected restore failure".to_string()],
                })
            },
        )
        .expect_err("uncertain transaction state");

        assert!(error.contains(SWITCH_STATE_UNCERTAIN_ERROR));
        assert!(error.contains("remains closed"));
        assert!(!backend.running.get());
        assert_eq!(
            &*backend.events.borrow(),
            &[
                "status",
                "close",
                "wait-stopped",
                "switch",
                "status",
                "close",
                "wait-stopped"
            ]
        );
    }

    #[test]
    fn initially_stopped_rollback_failure_does_not_control_a_new_process_without_authorization() {
        let backend = FakeLifecycle {
            running: Cell::new(false),
            ..FakeLifecycle::running()
        };

        let error = coordinate_switch_with::<(), _, _, _>(
            &backend,
            &lock_path("initially-stopped-rollback-race"),
            false,
            || {
                backend.events.borrow_mut().push("switch");
                backend.running.set(true);
                Err(crate::file_transaction::FileTransactionError::RollbackFailed {
                    operation: "publish failed".to_string(),
                    rollback: vec!["injected restore failure".to_string()],
                })
            },
        )
        .expect_err("uncertain transaction state");

        assert!(error.contains(SWITCH_STATE_UNCERTAIN_ERROR));
        assert!(error.contains("may still be running"));
        assert!(backend.running.get());
        assert_eq!(
            &*backend.events.borrow(),
            &["status", "switch", "status"]
        );
    }

    #[test]
    fn initially_stopped_rollback_failure_contains_a_new_process_when_authorized() {
        let backend = FakeLifecycle {
            running: Cell::new(false),
            ..FakeLifecycle::running()
        };

        let error = coordinate_switch_with::<(), _, _, _>(
            &backend,
            &lock_path("authorized-initially-stopped-rollback-race"),
            true,
            || {
                backend.events.borrow_mut().push("switch");
                backend.running.set(true);
                Err(crate::file_transaction::FileTransactionError::RollbackFailed {
                    operation: "publish failed".to_string(),
                    rollback: vec!["injected restore failure".to_string()],
                })
            },
        )
        .expect_err("uncertain transaction state");

        assert!(error.contains("remains closed"));
        assert!(!backend.running.get());
        assert_eq!(
            &*backend.events.borrow(),
            &["status", "switch", "status", "close", "wait-stopped"]
        );
    }

    #[test]
    fn rolled_back_error_text_cannot_impersonate_a_rollback_failure() {
        let backend = FakeLifecycle::running();
        let marker = crate::file_transaction::ROLLBACK_FAILED_ERROR;

        let result = coordinate_switch_with::<(), _, _, _>(
            &backend,
            &lock_path("rollback-marker-spoof"),
            true,
            || {
                Err(crate::file_transaction::FileTransactionError::RolledBack(
                    format!("upstream said {marker}: but rollback succeeded"),
                ))
            },
        )
        .expect("safe rollback should reopen the original desktop");

        assert_eq!(result.restart_result, CodexRestartResult::SwitchFailedReopened);
        assert!(backend.running.get());
    }

    #[test]
    fn successful_switch_with_relaunch_failure_keeps_switched_value() {
        let backend = FakeLifecycle {
            launch_succeeds: false,
            ..FakeLifecycle::running()
        };

        let result = coordinate_switch_with(&backend, &lock_path("launch-failure"), true, || {
            Ok::<&str, String>("switched")
        })
        .expect("partial success");

        assert_eq!(result.value, Some("switched"));
        assert_eq!(
            result.restart_result,
            CodexRestartResult::SwitchedRelaunchFailed
        );
        assert!(!backend.running.get());
    }

    #[test]
    fn stopped_desktop_mutates_without_close_or_launch() {
        let backend = FakeLifecycle {
            running: Cell::new(false),
            ..FakeLifecycle::running()
        };

        let result = coordinate_switch_with(&backend, &lock_path("stopped"), false, || {
            backend.events.borrow_mut().push("switch");
            Ok::<&str, String>("switched")
        })
        .expect("switch while stopped");

        assert_eq!(result.value, Some("switched"));
        assert_eq!(result.restart_result, CodexRestartResult::NotRunning);
        assert_eq!(&*backend.events.borrow(), &["status", "switch"]);
    }

    #[test]
    fn unattended_mutation_is_deferred_while_desktop_is_running() {
        let backend = FakeLifecycle::running();
        let mutated = Cell::new(false);

        let result = coordinate_unattended_with(&backend, &lock_path("unattended"), || {
            mutated.set(true);
            Ok::<&str, String>("changed")
        })
        .expect("unattended gate");

        assert_eq!(result, None);
        assert!(!mutated.get());
        assert_eq!(&*backend.events.borrow(), &["status"]);
    }

    #[test]
    fn unattended_mutation_runs_while_desktop_is_stopped() {
        let backend = FakeLifecycle {
            running: Cell::new(false),
            ..FakeLifecycle::running()
        };

        let result = coordinate_unattended_with(&backend, &lock_path("unattended-stopped"), || {
            backend.events.borrow_mut().push("switch");
            Ok::<&str, String>("changed")
        })
        .expect("unattended gate");

        assert_eq!(result, Some("changed"));
        assert_eq!(&*backend.events.borrow(), &["status", "switch"]);
    }

    #[test]
    fn unattended_commit_rechecks_a_process_that_started_during_network_work() {
        let backend = FakeLifecycle {
            running: Cell::new(false),
            ..FakeLifecycle::running()
        };
        let committed = Cell::new(false);

        let result = coordinate_unattended_with(&backend, &lock_path("unattended-race"), || {
            backend.events.borrow_mut().push("network");
            backend.running.set(true);
            let committed_value = run_if_stopped_with(&backend, || {
                committed.set(true);
                Ok::<&str, String>("committed")
            })?;
            assert_eq!(committed_value, None);
            Ok::<&str, String>("deferred")
        })
        .expect("unattended race gate");

        assert_eq!(result, Some("deferred"));
        assert!(!committed.get());
        assert_eq!(&*backend.events.borrow(), &["status", "network", "status"]);
    }

    #[test]
    fn windows_status_parser_fails_closed_on_ambiguous_output() {
        assert_eq!(
            parse_windows_status("supported=1 running=1").unwrap(),
            CodexDesktopStatus {
                running: true,
                restart_supported: true,
            }
        );
        assert_eq!(
            parse_windows_status("unsupported").unwrap(),
            CodexDesktopStatus {
                running: false,
                restart_supported: false,
            }
        );
        assert!(parse_windows_status("warning\nsupported=1 running=0").is_err());
        assert!(parse_windows_status("supported=1 running=10").is_err());
    }

    #[test]
    fn windows_lifecycle_contract_uses_exact_appx_identity_and_non_forced_restart_manager_close() {
        assert!(WINDOWS_APPX_RESOLUTION.contains("$ErrorActionPreference = 'Stop'"));
        assert!(WINDOWS_APPX_RESOLUTION.contains("Get-AppxPackage -Name 'OpenAI.Codex'"));
        assert!(WINDOWS_APPX_RESOLUTION.contains("Windows.FullTrustApplication"));
        assert!(WINDOWS_APPX_RESOLUTION.contains("$packages.Count -ne 1"));
        assert!(WINDOWS_APPX_RESOLUTION.contains("$applications.Count -ne 1"));
        assert!(WINDOWS_APPX_RESOLUTION.contains("Test-Path -LiteralPath $executable"));
        assert!(WINDOWS_RESTART_MANAGER_TYPE.contains("RmRegisterResources"));
        assert!(WINDOWS_RESTART_MANAGER_TYPE.contains("RmShutdown(sessionHandle, 0"));
        assert!(WINDOWS_RESTART_MANAGER_TYPE.contains("OpenProcess"));
        assert!(WINDOWS_RESTART_MANAGER_TYPE.contains("QueryFullProcessImageName"));
        assert!(WINDOWS_RESTART_MANAGER_TYPE.contains("GetProcessTimes"));
        assert!(WINDOWS_RESTART_MANAGER_TYPE.contains("RmCancelCurrentTask"));
        assert!(WINDOWS_RESTART_MANAGER_CLOSE.contains("$roots.Count -ne 1"));
        assert!(
            WINDOWS_RESTART_MANAGER_CLOSE
                .find("$roots.Count -ne 1")
                .unwrap()
                < WINDOWS_RESTART_MANAGER_CLOSE
                    .find("$restartManagerTimeoutMilliseconds =")
                    .unwrap()
        );
        assert!(WINDOWS_RESTART_MANAGER_TYPE.contains("preparationDeadline.ElapsedMilliseconds"));
        assert!(WINDOWS_RESTART_MANAGER_TYPE.contains("watchdogThread.Join()"));
        assert!(!WINDOWS_RESTART_MANAGER_TYPE.contains("RmForceShutdown"));
        assert!(!WINDOWS_RESTART_MANAGER_CLOSE.contains("Stop-Process"));
        assert!(!WINDOWS_RESTART_MANAGER_CLOSE.contains("taskkill"));
    }

    struct TimedCloseLifecycle {
        running: Cell<bool>,
        close_wait_budget: Cell<Option<Duration>>,
        close_delay: Duration,
    }

    impl CodexDesktopLifecycle for TimedCloseLifecycle {
        fn status(&self) -> Result<CodexDesktopStatus, String> {
            Ok(CodexDesktopStatus {
                running: self.running.get(),
                restart_supported: true,
            })
        }

        fn request_close(&self, _timeout: Duration) -> Result<(), String> {
            thread::sleep(self.close_delay);
            Ok(())
        }

        fn wait_for_running(&self, running: bool, timeout: Duration) -> Result<bool, String> {
            if !running {
                self.close_wait_budget.set(Some(timeout));
            }
            self.running.set(running);
            Ok(true)
        }

        fn launch(&self) -> Result<(), String> {
            Ok(())
        }
    }

    #[test]
    fn close_request_and_exit_poll_share_one_ten_second_deadline() {
        let backend = TimedCloseLifecycle {
            running: Cell::new(true),
            close_wait_budget: Cell::new(None),
            close_delay: Duration::from_millis(25),
        };

        coordinate_switch_with(&backend, &lock_path("shared-close-deadline"), true, || {
            Ok::<(), String>(())
        })
        .expect("coordinated switch");

        let wait_budget = backend
            .close_wait_budget
            .get()
            .expect("close wait budget should be recorded");
        assert!(wait_budget < CODEX_CLOSE_TIMEOUT);
    }

    #[test]
    fn exhausted_close_deadline_aborts_without_status_poll_or_mutation() {
        let backend = TimedCloseLifecycle {
            running: Cell::new(true),
            close_wait_budget: Cell::new(None),
            close_delay: Duration::from_millis(25),
        };
        let mutated = Cell::new(false);

        let error = coordinate_switch_with_timeout(
            &backend,
            &lock_path("exhausted-close-deadline"),
            true,
            Duration::from_millis(10),
            || {
                mutated.set(true);
                Ok::<(), String>(())
            },
        )
        .expect_err("an exhausted close deadline must fail closed");

        assert!(error.contains(CLOSE_TIMEOUT_ERROR));
        assert_eq!(backend.close_wait_budget.get(), None);
        assert!(!mutated.get());
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn windows_restart_manager_helper_compiles_and_rejects_a_reused_process_identity() {
        let wrong_executable = std::env::temp_dir().join("not-the-powershell-host.exe");
        let escaped = wrong_executable
            .to_string_lossy()
            .replace('\'', "''");
        let script = format!(
            "{WINDOWS_RESTART_MANAGER_TYPE}\ntry {{ [CodexHubRestartManager]::GracefulShutdown([uint32]$PID, '{escaped}', 100) | Out-Null; Write-Output 'unexpected-accept' }} catch {{ Write-Output $_.Exception.ToString() }}"
        );
        let output = run_windows_lifecycle_script(&script, Duration::from_secs(5))
            .expect("Restart Manager helper should compile and reject the wrong executable");

        assert!(output.contains("process identity changed"), "{output}");
        assert!(!output.contains("unexpected-accept"), "{output}");
    }

    #[cfg(target_os = "windows")]
    #[test]
    #[ignore = "requires an interactive Windows session; strict E2E runs it explicitly"]
    fn windows_restart_manager_gracefully_closes_a_controlled_gui_fixture() {
        let script = format!(
            r#"{WINDOWS_RESTART_MANAGER_TYPE}
$fixtureExecutable = (Get-Command powershell.exe -ErrorAction Stop).Source
$fixtureCommand = 'Add-Type -AssemblyName System.Windows.Forms; $form = New-Object Windows.Forms.Form; $form.Text = ''CodexHub Restart Manager Fixture''; [Windows.Forms.Application]::Run($form)'
$encodedFixtureCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($fixtureCommand))
$fixture = $null
try {{
    $fixture = Start-Process -FilePath $fixtureExecutable -ArgumentList @('-NoProfile', '-STA', '-EncodedCommand', $encodedFixtureCommand) -PassThru
    $deadline = [Diagnostics.Stopwatch]::StartNew()
    do {{
        Start-Sleep -Milliseconds 50
        $fixture.Refresh()
    }} while (-not $fixture.HasExited -and $fixture.MainWindowHandle -eq 0 -and $deadline.ElapsedMilliseconds -lt 5000)
    if ($fixture.HasExited -or $fixture.MainWindowHandle -eq 0) {{ throw 'The controlled GUI fixture did not create a main window.' }}
    $result = [CodexHubRestartManager]::GracefulShutdown([uint32]$fixture.Id, $fixtureExecutable, 3000)
    if ($result -ne 0) {{ throw "Restart Manager returned $result for the controlled GUI fixture." }}
    if (-not $fixture.WaitForExit(5000)) {{ throw 'The controlled GUI fixture did not exit after graceful shutdown.' }}
    Write-Output 'graceful-close=1'
}}
finally {{
    if ($fixture -and -not $fixture.HasExited) {{ Stop-Process -Id $fixture.Id -Force -ErrorAction SilentlyContinue }}
}}
"#
        );
        let output = run_windows_lifecycle_script(&script, Duration::from_secs(15))
            .expect("Restart Manager should gracefully close the controlled GUI fixture");

        assert_eq!(output, "graceful-close=1");
    }

    #[cfg(target_os = "windows")]
    #[test]
    #[ignore = "requires an interactive Windows session; strict E2E runs it explicitly"]
    fn windows_restart_manager_cancels_a_temporarily_unresponsive_gui_and_cleans_the_session() {
        let script = format!(
            r#"{WINDOWS_RESTART_MANAGER_TYPE}
$fixtureExecutable = (Get-Command powershell.exe -ErrorAction Stop).Source
$fixtureCommand = 'Add-Type -AssemblyName System.Windows.Forms; $form = New-Object Windows.Forms.Form; $form.Text = ''CodexHub Unresponsive Restart Manager Fixture''; $form.Add_Shown({{ Start-Sleep -Seconds 3 }}); [Windows.Forms.Application]::Run($form)'
$encodedFixtureCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($fixtureCommand))
$fixture = $null
try {{
    $fixture = Start-Process -FilePath $fixtureExecutable -ArgumentList @('-NoProfile', '-STA', '-EncodedCommand', $encodedFixtureCommand) -PassThru
    $deadline = [Diagnostics.Stopwatch]::StartNew()
    do {{
        Start-Sleep -Milliseconds 50
        $fixture.Refresh()
    }} while (-not $fixture.HasExited -and $fixture.MainWindowHandle -eq 0 -and $deadline.ElapsedMilliseconds -lt 5000)
    if ($fixture.HasExited -or $fixture.MainWindowHandle -eq 0) {{ throw 'The unresponsive GUI fixture did not create a main window.' }}
    $result = [CodexHubRestartManager]::GracefulShutdown([uint32]$fixture.Id, $fixtureExecutable, 500)
    $fixture.Refresh()
    if (-not [CodexHubRestartManager]::LastCancellationRequested) {{ throw "Restart Manager cancellation was not requested; result=$result." }}
    $probeResult = [CodexHubRestartManager]::ProbeSession()
    if ($probeResult -ne 0) {{ throw "Restart Manager session cleanup probe failed with code $probeResult." }}
    Write-Output 'cancelled=1 session-clean=1'
}}
finally {{
    if ($fixture -and -not $fixture.HasExited) {{ Stop-Process -Id $fixture.Id -Force -ErrorAction SilentlyContinue }}
}}
"#
        );
        let output = run_windows_lifecycle_script(&script, Duration::from_secs(10))
            .expect("Restart Manager should cancel without force-killing the controlled fixture");

        assert_eq!(output, "cancelled=1 session-clean=1");
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn windows_watchdog_returns_short_output_and_fails_closed_on_throw() {
        assert_eq!(
            run_windows_lifecycle_script("Write-Output 'ready'", Duration::from_secs(5))
                .expect("short PowerShell command"),
            "ready"
        );
        let error = run_windows_lifecycle_script(
            "$ErrorActionPreference = 'Stop'; throw 'closed'",
            Duration::from_secs(5),
        )
        .expect_err("PowerShell throw must fail closed");
        assert!(error.contains("closed"));
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn windows_watchdog_terminates_a_hung_helper_within_its_bound() {
        let started = Instant::now();
        let error = run_windows_lifecycle_script(
            "Start-Sleep -Seconds 30",
            Duration::from_millis(500),
        )
        .expect_err("hung PowerShell helper must time out");

        assert!(error.contains("timed out"));
        assert!(started.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn failed_switch_and_failed_reopen_returns_stable_error() {
        let backend = FakeLifecycle {
            launch_succeeds: false,
            ..FakeLifecycle::running()
        };

        let error = coordinate_switch_with::<(), _, _, _>(
            &backend,
            &lock_path("switch-reopen-failure"),
            true,
            || Err("config failed".to_string()),
        )
        .expect_err("reopen failure");

        assert!(error.contains(SWITCH_REOPEN_FAILED_ERROR));
        assert!(error.contains("config failed"));
        assert!(error.contains("launch failed"));
    }

    struct AlwaysStopped;

    impl CodexDesktopLifecycle for AlwaysStopped {
        fn status(&self) -> Result<CodexDesktopStatus, String> {
            Ok(CodexDesktopStatus {
                running: false,
                restart_supported: true,
            })
        }

        fn request_close(&self, _timeout: Duration) -> Result<(), String> {
            unreachable!()
        }

        fn wait_for_running(&self, _running: bool, _timeout: Duration) -> Result<bool, String> {
            unreachable!()
        }

        fn launch(&self) -> Result<(), String> {
            unreachable!()
        }
    }

    #[test]
    fn concurrent_switch_waits_for_the_cross_process_lock_before_mutating() {
        let path = lock_path("concurrent");
        let holder = acquire_switch_lock(&path).expect("hold switch lock");
        let (ready_tx, ready_rx) = mpsc::channel();
        let (mutated_tx, mutated_rx) = mpsc::channel();
        let worker_path = path.clone();
        let worker = std::thread::spawn(move || {
            ready_tx.send(()).unwrap();
            coordinate_switch_with(&AlwaysStopped, &worker_path, false, || {
                mutated_tx.send(()).unwrap();
                Ok::<(), String>(())
            })
            .unwrap();
        });

        ready_rx.recv_timeout(Duration::from_secs(1)).unwrap();
        assert!(mutated_rx.recv_timeout(Duration::from_millis(50)).is_err());
        drop(holder);
        mutated_rx.recv_timeout(Duration::from_secs(1)).unwrap();
        worker.join().unwrap();
    }

    #[test]
    fn settings_writer_waits_for_an_active_publication_transaction() {
        let path = lock_path("settings-publication-serialization");
        let holder = acquire_switch_lock(&path).expect("hold publication lock");
        let (ready_tx, ready_rx) = mpsc::channel();
        let (written_tx, written_rx) = mpsc::channel();
        let worker_path = path.clone();
        let worker = std::thread::spawn(move || {
            ready_tx.send(()).unwrap();
            serialize_config_writer_with_path(&worker_path, || {
                written_tx.send(()).unwrap();
                Ok(())
            })
            .unwrap();
        });

        ready_rx.recv_timeout(Duration::from_secs(1)).unwrap();
        assert!(written_rx.recv_timeout(Duration::from_millis(50)).is_err());
        drop(holder);
        written_rx.recv_timeout(Duration::from_secs(1)).unwrap();
        worker.join().unwrap();
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn linux_process_detection_matches_exact_executable_and_excludes_helpers() {
        use std::os::unix::fs::symlink;

        let root = std::env::temp_dir().join(format!(
            "codexhub-linux-process-fixture-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&root);
        let proc_root = root.join("proc");
        let executable = root.join("ChatGPT");
        let near_match = root.join("ChatGPT-helper");
        fs::create_dir_all(&proc_root).unwrap();
        fs::write(&executable, b"fixture").unwrap();
        fs::write(&near_match, b"fixture").unwrap();

        for (pid, target, cmdline) in [
            (
                100_u32,
                &executable,
                b"ChatGPT\0--enable-features=x\0".as_slice(),
            ),
            (
                101_u32,
                &executable,
                b"ChatGPT\0--type=renderer\0".as_slice(),
            ),
            (102_u32, &near_match, b"ChatGPT-helper\0".as_slice()),
        ] {
            let process_root = proc_root.join(pid.to_string());
            fs::create_dir_all(&process_root).unwrap();
            symlink(target, process_root.join("exe")).unwrap();
            fs::write(process_root.join("cmdline"), cmdline).unwrap();
        }

        assert_eq!(
            linux_main_process_ids(&proc_root, &executable).unwrap(),
            vec![100]
        );
        let _ = fs::remove_dir_all(root);
    }
}
