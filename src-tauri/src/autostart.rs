use crate::config::{self, CommandOutcome, CommandRunner, ProcessCommandRunner};
use serde::Serialize;
use std::fs;
use std::path::{Path, PathBuf};

fn windows_task_name() -> &'static str {
    crate::app_flavor::current().autostart_task_name()
}

fn macos_label() -> &'static str {
    crate::app_flavor::current().macos_label()
}

fn macos_plist_file() -> &'static str {
    crate::app_flavor::current().macos_plist_file()
}

fn linux_service_file() -> &'static str {
    crate::app_flavor::current().linux_service_file()
}

pub fn set_autostart(enabled: bool) -> Result<String, String> {
    let paths = RuntimePathProvider;
    let filesystem = RealAutostartFileSystem;
    let runner = ProcessCommandRunner;

    set_autostart_with_dependencies(
        enabled,
        OperatingSystem::current(),
        &paths,
        &filesystem,
        &runner,
    )
}

pub fn remove_autostart() -> Result<String, String> {
    let paths = RuntimePathProvider;
    let filesystem = RealAutostartFileSystem;
    let runner = ProcessCommandRunner;

    remove_autostart_with_dependencies(OperatingSystem::current(), &paths, &filesystem, &runner)
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AutostartStatus {
    pub enabled: bool,
    pub authoritative: bool,
    pub state: &'static str,
}

pub fn get_autostart_status() -> Result<AutostartStatus, String> {
    let paths = RuntimePathProvider;
    get_autostart_status_with_dependencies(
        OperatingSystem::current(),
        &paths,
        &ProcessCommandRunner,
    )
}

pub fn reconcile_settings(mut settings: crate::Settings) -> Result<crate::Settings, String> {
    let status = get_autostart_status()?;
    if status.authoritative && settings.auto_start_software != status.enabled {
        settings.auto_start_software = status.enabled;
        config::save_settings(settings)
    } else {
        Ok(settings)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[allow(dead_code)]
enum OperatingSystem {
    Windows,
    Macos,
    Linux,
    Unsupported(&'static str),
}

impl OperatingSystem {
    fn current() -> Self {
        #[cfg(target_os = "windows")]
        {
            Self::Windows
        }
        #[cfg(target_os = "macos")]
        {
            Self::Macos
        }
        #[cfg(target_os = "linux")]
        {
            Self::Linux
        }
        #[cfg(not(any(target_os = "windows", target_os = "macos", target_os = "linux")))]
        {
            Self::Unsupported(std::env::consts::OS)
        }
    }
}

trait AutostartPathProvider {
    fn current_exe(&self) -> Result<PathBuf, String>;
    fn home_dir(&self) -> Result<PathBuf, String>;
}

struct RuntimePathProvider;

impl AutostartPathProvider for RuntimePathProvider {
    fn current_exe(&self) -> Result<PathBuf, String> {
        std::env::current_exe().map_err(|error| {
            format!("failed to resolve current CodexHub executable for autostart: {error}")
        })
    }

    fn home_dir(&self) -> Result<PathBuf, String> {
        dirs::home_dir()
            .ok_or_else(|| "failed to resolve user home directory for autostart".to_string())
    }
}

trait AutostartFileSystem {
    fn create_dir_all(&self, path: &Path) -> Result<(), String>;
    fn write(&self, path: &Path, content: &str) -> Result<(), String>;
    fn remove_file_if_exists(&self, path: &Path) -> Result<(), String>;
}

struct RealAutostartFileSystem;

impl AutostartFileSystem for RealAutostartFileSystem {
    fn create_dir_all(&self, path: &Path) -> Result<(), String> {
        fs::create_dir_all(path)
            .map_err(|error| format!("failed to create {}: {error}", path.display()))
    }

    fn write(&self, path: &Path, content: &str) -> Result<(), String> {
        fs::write(path, content)
            .map_err(|error| format!("failed to write {}: {error}", path.display()))
    }

    fn remove_file_if_exists(&self, path: &Path) -> Result<(), String> {
        match fs::remove_file(path) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(format!("failed to remove {}: {error}", path.display())),
        }
    }
}

fn set_autostart_with_dependencies(
    enabled: bool,
    os: OperatingSystem,
    paths: &dyn AutostartPathProvider,
    filesystem: &dyn AutostartFileSystem,
    runner: &dyn CommandRunner,
) -> Result<String, String> {
    if !enabled {
        return remove_autostart_with_dependencies(os, paths, filesystem, runner);
    }

    let exe = paths.current_exe()?;
    match os {
        OperatingSystem::Windows => register_windows_autostart(&exe, runner),
        OperatingSystem::Macos => register_macos_autostart(&exe, paths, filesystem),
        OperatingSystem::Linux => register_linux_autostart(&exe, paths, filesystem, runner),
        OperatingSystem::Unsupported(name) => {
            Err(format!("autostart registration is not supported on {name}"))
        }
    }
}

fn remove_autostart_with_dependencies(
    os: OperatingSystem,
    paths: &dyn AutostartPathProvider,
    filesystem: &dyn AutostartFileSystem,
    runner: &dyn CommandRunner,
) -> Result<String, String> {
    match os {
        OperatingSystem::Windows => remove_windows_autostart(runner),
        OperatingSystem::Macos => remove_macos_autostart(paths, filesystem),
        OperatingSystem::Linux => remove_linux_autostart(paths, filesystem, runner),
        OperatingSystem::Unsupported(name) => {
            Err(format!("autostart removal is not supported on {name}"))
        }
    }
}

fn register_windows_autostart(exe: &Path, runner: &dyn CommandRunner) -> Result<String, String> {
    let program = Path::new("powershell.exe");
    let args = windows_powershell_args(&windows_register_script(exe));
    run_windows_command("create Windows autostart task", program, &args, runner)?;

    let status = query_windows_autostart(exe, runner)?;
    if !status.enabled {
        let _ = delete_windows_task(runner);
        return Err("Windows autostart registration failed readback verification".to_string());
    }

    Ok(format!(
        "Autostart enabled via Windows Task Scheduler task {}",
        windows_task_name()
    ))
}

fn remove_windows_autostart(runner: &dyn CommandRunner) -> Result<String, String> {
    delete_windows_task(runner)?;
    let status = query_windows_task(runner)?;
    if status.is_some() {
        return Err("Windows autostart removal failed readback verification".to_string());
    }

    Ok(format!(
        "Autostart removed from Windows Task Scheduler task {}",
        windows_task_name()
    ))
}

fn delete_windows_task(runner: &dyn CommandRunner) -> Result<(), String> {
    let program = Path::new("powershell.exe");
    let args = windows_powershell_args(&windows_delete_script());
    let label = "delete Windows autostart task";
    let outcome = runner
        .run(program, &args)
        .map_err(|error| format!("{label} failed to start: {error}"))?;

    if outcome.code != Some(0) {
        return Err(format!(
            "{label} failed{}",
            outcome
                .code
                .map(|code| format!(" with exit code {code}"))
                .unwrap_or_default()
        ));
    }

    Ok(())
}

fn get_autostart_status_with_dependencies(
    os: OperatingSystem,
    paths: &dyn AutostartPathProvider,
    runner: &dyn CommandRunner,
) -> Result<AutostartStatus, String> {
    if os != OperatingSystem::Windows {
        return Ok(AutostartStatus {
            enabled: false,
            authoritative: false,
            state: "unsupported-readback",
        });
    }
    let exe = paths.current_exe()?;
    query_windows_autostart(&exe, runner)
}

fn query_windows_autostart(
    expected_exe: &Path,
    runner: &dyn CommandRunner,
) -> Result<AutostartStatus, String> {
    let Some(readback) = query_windows_task(runner)? else {
        return Ok(AutostartStatus {
            enabled: false,
            authoritative: true,
            state: "missing",
        });
    };
    let command = xml_element(&readback.xml, "Command");
    let arguments = xml_element(&readback.xml, "Arguments");
    let enabled = xml_element(&readback.xml, "Enabled");
    let principal = xml_element(&readback.xml, "Principal").unwrap_or_default();
    let trigger = xml_element(&readback.xml, "LogonTrigger").unwrap_or_default();
    let principal_user = xml_element(&principal, "UserId");
    let trigger_user = xml_element(&trigger, "UserId");
    let has_one_principal = xml_opening_tag_count(&readback.xml, "Principal") == 1;
    let has_one_logon_trigger = readback.xml.matches("<LogonTrigger").count() == 1;
    let has_one_action = readback.xml.matches("<Exec").count() == 1;
    let matches = command
        .as_deref()
        .is_some_and(|value| windows_paths_equal(value, expected_exe))
        && arguments
            .as_deref()
            .is_none_or(|value| value.trim().is_empty())
        && enabled
            .as_deref()
            .is_none_or(|value| value.trim().eq_ignore_ascii_case("true"))
        && has_one_logon_trigger
        && has_one_action
        && has_one_principal
        && xml_element(&readback.xml, "Description").as_deref() == Some(WINDOWS_TASK_DESCRIPTION)
        && xml_element(&principal, "LogonType").as_deref() == Some("InteractiveToken")
        && xml_element(&principal, "RunLevel")
            .as_deref()
            .is_none_or(|run_level| run_level == "LeastPrivilege")
        && principal_user.as_deref() == Some(readback.current_sid.as_str())
        && trigger_user == principal_user;
    Ok(AutostartStatus {
        enabled: matches,
        authoritative: true,
        state: if matches {
            "enabled"
        } else {
            "malformed-or-stale"
        },
    })
}

struct WindowsTaskReadback {
    current_sid: String,
    xml: String,
}

fn query_windows_task(runner: &dyn CommandRunner) -> Result<Option<WindowsTaskReadback>, String> {
    let program = Path::new("powershell.exe");
    let args = windows_powershell_args(&windows_query_script());
    let outcome = runner
        .run(program, &args)
        .map_err(|error| format!("query Windows autostart task failed to start: {error}"))?;
    if outcome.code != Some(0) {
        return Err(format!(
            "query Windows autostart task failed{}",
            outcome
                .code
                .map(|code| format!(" with exit code {code}"))
                .unwrap_or_default()
        ));
    }
    let stdout = outcome.stdout.trim_start_matches('\u{feff}').trim();
    if stdout == WINDOWS_TASK_MISSING_MARKER {
        return Ok(None);
    }
    let normalized = stdout.replace("\r\n", "\n");
    let (marker, xml) = normalized
        .split_once('\n')
        .ok_or_else(|| "query Windows autostart task returned malformed readback".to_string())?;
    let current_sid = marker
        .strip_prefix(WINDOWS_TASK_SID_PREFIX)
        .filter(|sid| !sid.is_empty())
        .ok_or_else(|| "query Windows autostart task returned malformed identity".to_string())?;
    let xml = xml.trim_start_matches(['\r', '\n']);
    if !xml.contains("<Task") {
        return Err("query Windows autostart task returned malformed XML".to_string());
    }
    Ok(Some(WindowsTaskReadback {
        current_sid: current_sid.to_string(),
        xml: xml.to_string(),
    }))
}

const WINDOWS_TASK_DESCRIPTION: &str = "CodexHub-owned per-user autostart";
const WINDOWS_TASK_MISSING_MARKER: &str = "CODEXHUB_TASK_MISSING";
const WINDOWS_TASK_SID_PREFIX: &str = "CODEXHUB_CURRENT_SID=";

fn windows_powershell_args(script: &str) -> Vec<String> {
    vec![
        "-NoProfile".to_string(),
        "-NonInteractive".to_string(),
        "-ExecutionPolicy".to_string(),
        "Bypass".to_string(),
        "-Command".to_string(),
        script.to_string(),
    ]
}

fn powershell_literal(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

fn windows_scheduler_prelude() -> String {
    "$ErrorActionPreference='Stop';$utf8=New-Object System.Text.UTF8Encoding($false);[Console]::OutputEncoding=$utf8;$OutputEncoding=$utf8;$service=New-Object -ComObject 'Schedule.Service';$service.Connect();$folder=$service.GetFolder('\\');".to_string()
}

fn windows_register_script(exe: &Path) -> String {
    let task = powershell_literal(windows_task_name());
    let working_directory = powershell_literal(
        &exe.parent()
            .map(|parent| parent.to_string_lossy().into_owned())
            .unwrap_or_else(|| ".".to_string()),
    );
    let exe = powershell_literal(&exe.to_string_lossy());
    format!(
        "{}$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;$definition=$service.NewTask(0);$definition.RegistrationInfo.Description={};$definition.Principal.UserId=$sid;$definition.Principal.LogonType=3;$definition.Principal.RunLevel=0;$definition.Settings.Enabled=$true;$definition.Settings.StartWhenAvailable=$true;$definition.Settings.DisallowStartIfOnBatteries=$false;$definition.Settings.StopIfGoingOnBatteries=$false;$definition.Settings.MultipleInstances=2;$trigger=$definition.Triggers.Create(9);$trigger.Enabled=$true;$trigger.UserId=$sid;$action=$definition.Actions.Create(0);$action.Path={};$action.WorkingDirectory={};$folder.RegisterTaskDefinition({},$definition,6,$sid,$null,3,$null)|Out-Null",
        windows_scheduler_prelude(),
        powershell_literal(WINDOWS_TASK_DESCRIPTION),
        exe,
        working_directory,
        task,
    )
}

fn windows_query_script() -> String {
    format!(
        "{}$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;try{{$task=$folder.GetTask({})}}catch{{if($_.Exception.HResult -eq -2147024894){{Write-Output {};exit 0}}throw}};Write-Output ({}+$sid);Write-Output $task.Xml",
        windows_scheduler_prelude(),
        powershell_literal(windows_task_name()),
        powershell_literal(WINDOWS_TASK_MISSING_MARKER),
        powershell_literal(WINDOWS_TASK_SID_PREFIX),
    )
}

fn windows_delete_script() -> String {
    format!(
        "{}try{{$folder.DeleteTask({},0)}}catch{{if($_.Exception.HResult -ne -2147024894){{throw}}}}",
        windows_scheduler_prelude(),
        powershell_literal(windows_task_name()),
    )
}

fn run_windows_command(
    label: &str,
    program: &Path,
    args: &[String],
    runner: &dyn CommandRunner,
) -> Result<(), String> {
    let outcome = runner
        .run(program, args)
        .map_err(|_| format!("{label} failed to start"))?;
    if outcome.code == Some(0) {
        Ok(())
    } else {
        Err(format!(
            "{label} failed{}",
            outcome
                .code
                .map(|code| format!(" with exit code {code}"))
                .unwrap_or_default()
        ))
    }
}

fn xml_element(xml: &str, name: &str) -> Option<String> {
    let start_prefix = format!("<{name}");
    let end_tag = format!("</{name}>");
    let mut search_from = 0;
    let opening = loop {
        let relative = xml[search_from..].find(&start_prefix)?;
        let candidate = search_from + relative;
        let boundary = xml[candidate + start_prefix.len()..].chars().next()?;
        if boundary == '>' || boundary.is_ascii_whitespace() {
            break candidate;
        }
        search_from = candidate + start_prefix.len();
    };
    let opening_end = xml[opening..].find('>')? + opening;
    if xml[opening..opening_end].trim_end().ends_with('/') {
        return None;
    }
    let start = opening_end + 1;
    let end = xml[start..].find(&end_tag)? + start;
    Some(
        xml[start..end]
            .replace("&quot;", "\"")
            .replace("&apos;", "'")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&amp;", "&"),
    )
}

fn xml_opening_tag_count(xml: &str, name: &str) -> usize {
    let start_prefix = format!("<{name}");
    xml.match_indices(&start_prefix)
        .filter(|(start, _)| {
            xml[start + start_prefix.len()..]
                .chars()
                .next()
                .is_some_and(|boundary| boundary == '>' || boundary.is_ascii_whitespace())
        })
        .count()
}

fn windows_paths_equal(actual: &str, expected: &Path) -> bool {
    let normalize = |value: &str| {
        value
            .trim()
            .trim_matches('"')
            .trim_start_matches(r"\\?\")
            .replace('/', "\\")
            .to_lowercase()
    };
    normalize(actual) == normalize(&expected.to_string_lossy())
}

fn register_macos_autostart(
    exe: &Path,
    paths: &dyn AutostartPathProvider,
    filesystem: &dyn AutostartFileSystem,
) -> Result<String, String> {
    let plist_path = macos_plist_path(paths)?;
    let parent = plist_path.parent().ok_or_else(|| {
        format!(
            "failed to resolve parent directory for {}",
            plist_path.display()
        )
    })?;
    filesystem.create_dir_all(parent)?;
    filesystem.write(&plist_path, &macos_plist_content(exe))?;

    Ok(format!(
        "Autostart enabled via macOS LaunchAgent {}",
        plist_path.display()
    ))
}

fn remove_macos_autostart(
    paths: &dyn AutostartPathProvider,
    filesystem: &dyn AutostartFileSystem,
) -> Result<String, String> {
    let plist_path = macos_plist_path(paths)?;
    filesystem.remove_file_if_exists(&plist_path)?;

    Ok(format!(
        "Autostart removed from macOS LaunchAgent {}",
        plist_path.display()
    ))
}

fn register_linux_autostart(
    exe: &Path,
    paths: &dyn AutostartPathProvider,
    filesystem: &dyn AutostartFileSystem,
    runner: &dyn CommandRunner,
) -> Result<String, String> {
    let service_path = linux_service_path(paths)?;
    let parent = service_path.parent().ok_or_else(|| {
        format!(
            "failed to resolve parent directory for {}",
            service_path.display()
        )
    })?;
    filesystem.create_dir_all(parent)?;
    filesystem.write(&service_path, &linux_service_content(exe))?;

    let reload_args = linux_systemctl_args("daemon-reload");
    run_autostart_command(
        "reload Linux systemd user daemon",
        Path::new("systemctl"),
        &reload_args,
        runner,
    )?;

    let program = Path::new("systemctl");
    let args = linux_systemctl_args_with_unit("enable");
    run_autostart_command("enable Linux autostart service", program, &args, runner)?;

    Ok(format!(
        "Autostart enabled via Linux systemd user service {}",
        service_path.display()
    ))
}

fn remove_linux_autostart(
    paths: &dyn AutostartPathProvider,
    filesystem: &dyn AutostartFileSystem,
    runner: &dyn CommandRunner,
) -> Result<String, String> {
    let service_path = linux_service_path(paths)?;

    let program = Path::new("systemctl");
    let args = linux_systemctl_args_with_unit("disable");
    let disable_result = run_linux_systemctl_disable_best_effort(program, &args, runner);

    filesystem.remove_file_if_exists(&service_path)?;

    let reload_args = linux_systemctl_args("daemon-reload");
    let reload_result = run_linux_systemctl_cleanup_best_effort(program, &reload_args, runner);

    disable_result?;
    reload_result?;

    Ok(format!(
        "Autostart removed from Linux systemd user service {}",
        service_path.display()
    ))
}

fn run_autostart_command(
    label: &str,
    program: &Path,
    args: &[String],
    runner: &dyn CommandRunner,
) -> Result<(), String> {
    let outcome = runner
        .run(program, args)
        .map_err(|error| format!("{label} failed to start: {error}"))?;

    if outcome.code == Some(0) {
        Ok(())
    } else {
        Err(config::format_command_failure(
            label, program, args, &outcome,
        ))
    }
}

fn linux_systemctl_args(command: &str) -> Vec<String> {
    vec!["--user".to_string(), command.to_string()]
}

fn linux_systemctl_args_with_unit(command: &str) -> Vec<String> {
    let mut args = linux_systemctl_args(command);
    args.push(linux_service_file().to_string());
    args
}

fn run_linux_systemctl_disable_best_effort(
    program: &Path,
    args: &[String],
    runner: &dyn CommandRunner,
) -> Result<(), String> {
    run_linux_systemctl_cleanup_command(
        "disable Linux autostart service",
        program,
        args,
        runner,
        true,
    )
}

fn run_linux_systemctl_cleanup_best_effort(
    program: &Path,
    args: &[String],
    runner: &dyn CommandRunner,
) -> Result<(), String> {
    run_linux_systemctl_cleanup_command(
        "reload Linux systemd user daemon",
        program,
        args,
        runner,
        false,
    )
}

fn run_linux_systemctl_cleanup_command(
    label: &str,
    program: &Path,
    args: &[String],
    runner: &dyn CommandRunner,
    allow_missing_unit: bool,
) -> Result<(), String> {
    let outcome = match runner.run(program, args) {
        Ok(outcome) => outcome,
        Err(error) if is_nonfatal_systemctl_start_failure(&error) => return Ok(()),
        Err(error) => return Err(format!("{label} failed to start: {error}")),
    };

    if outcome.code == Some(0)
        || is_nonfatal_systemctl_cleanup_failure(&outcome, allow_missing_unit)
    {
        Ok(())
    } else {
        Err(config::format_command_failure(
            label, program, args, &outcome,
        ))
    }
}

fn macos_plist_path(paths: &dyn AutostartPathProvider) -> Result<PathBuf, String> {
    Ok(paths
        .home_dir()?
        .join("Library")
        .join("LaunchAgents")
        .join(macos_plist_file()))
}

fn linux_service_path(paths: &dyn AutostartPathProvider) -> Result<PathBuf, String> {
    Ok(paths
        .home_dir()?
        .join(".config")
        .join("systemd")
        .join("user")
        .join(linux_service_file()))
}

fn macos_plist_content(exe: &Path) -> String {
    let exe = escape_xml(&exe.to_string_lossy());
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "https://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{exe}</string>
    <string>start</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <false/>
</dict>
</plist>
"#,
        macos_label()
    )
}

fn linux_service_content(exe: &Path) -> String {
    format!(
        "[Unit]\nDescription=CodexHub Proxy\n\n[Service]\nType=simple\nExecStart={} start\n\n[Install]\nWantedBy=default.target\n",
        systemd_quote_exec_path(exe)
    )
}

fn systemd_quote_exec_path(path: &Path) -> String {
    let text = path.to_string_lossy();
    let escaped = text
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('%', "%%");
    if text
        .chars()
        .any(|character| character.is_whitespace() || character == '"' || character == '\\')
    {
        format!("\"{}\"", escaped)
    } else {
        escaped
    }
}

fn is_nonfatal_systemctl_start_failure(error: &str) -> bool {
    let text = error.to_ascii_lowercase();
    text.contains("failed to start systemctl")
        || text.contains("systemctl")
            && (text.contains("not found")
                || text.contains("no such file or directory")
                || text.contains("cannot find")
                || text.contains("not recognized"))
}

fn is_nonfatal_systemctl_cleanup_failure(
    outcome: &CommandOutcome,
    allow_missing_unit: bool,
) -> bool {
    let common_nonfatal = command_output_contains(
        outcome,
        &[
            "failed to connect to bus",
            "system has not been booted with systemd",
            "dbus_session_bus_address",
            "xdg_runtime_dir",
            "no medium found",
            "host is down",
        ],
    );

    if common_nonfatal {
        return true;
    }

    allow_missing_unit
        && command_output_contains(
            outcome,
            &[
                &format!("unit {} does not exist", linux_service_file()),
                &format!("unit file {} does not exist", linux_service_file()),
                &format!("{} does not exist", linux_service_file()),
                &format!("{} not found", linux_service_file()),
                "not enabled",
            ],
        )
}

fn command_output_contains(outcome: &CommandOutcome, needles: &[&str]) -> bool {
    let text = format!("{}\n{}", outcome.stdout, outcome.stderr).to_ascii_lowercase();
    needles.iter().any(|needle| text.contains(needle))
}

fn escape_xml(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

#[cfg(test)]
mod tests {
    use super::{
        get_autostart_status_with_dependencies, remove_autostart_with_dependencies,
        set_autostart_with_dependencies, AutostartFileSystem, AutostartPathProvider,
        OperatingSystem,
    };
    use crate::config::{CommandOutcome, CommandRunner};
    use std::cell::RefCell;
    use std::collections::{HashMap, VecDeque};
    use std::path::{Path, PathBuf};

    #[test]
    fn windows_set_autostart_creates_or_updates_logon_task() {
        let paths = FakePaths::new(
            PathBuf::from(r"C:\Program Files\CodexHub\codexhub.exe"),
            PathBuf::from(r"C:\Users\codexhub"),
        );
        let filesystem = MemoryFileSystem::default();
        let runner = RecordingRunner::sequence(vec![
            Ok(command_outcome(Some(0), "created", "")),
            Ok(command_outcome(
                Some(0),
                &windows_query_output(r"C:\Program Files\CodexHub\codexhub.exe"),
                "",
            )),
        ]);

        let message = set_autostart_with_dependencies(
            true,
            OperatingSystem::Windows,
            &paths,
            &filesystem,
            &runner,
        )
        .unwrap();

        assert!(message.contains(super::windows_task_name()));
        assert_eq!(filesystem.writes.borrow().len(), 0);
        assert_eq!(
            runner.commands.borrow().as_slice(),
            &[
                windows_register_command(Path::new(r"C:\Program Files\CodexHub\codexhub.exe")),
                windows_query_command(),
            ]
        );
    }

    #[test]
    fn windows_remove_autostart_deletes_logon_task() {
        let paths = FakePaths::default();
        let filesystem = MemoryFileSystem::default();
        let runner = RecordingRunner::sequence(vec![
            Ok(command_outcome(Some(0), "deleted", "")),
            Ok(command_outcome(
                Some(0),
                super::WINDOWS_TASK_MISSING_MARKER,
                "",
            )),
        ]);

        remove_autostart_with_dependencies(OperatingSystem::Windows, &paths, &filesystem, &runner)
            .unwrap();

        assert_eq!(
            runner.commands.borrow().as_slice(),
            &[windows_delete_command(), windows_query_command(),]
        );
    }

    #[test]
    fn windows_set_autostart_false_tolerates_missing_logon_task() {
        let paths = FakePaths::new_with_current_exe_error(
            "current exe should not be needed".to_string(),
            PathBuf::from(r"C:\Users\codexhub"),
        );
        let filesystem = MemoryFileSystem::default();
        let runner = RecordingRunner::sequence(vec![
            Ok(command_outcome(Some(0), "missing", "")),
            Ok(command_outcome(
                Some(0),
                super::WINDOWS_TASK_MISSING_MARKER,
                "",
            )),
        ]);

        set_autostart_with_dependencies(
            false,
            OperatingSystem::Windows,
            &paths,
            &filesystem,
            &runner,
        )
        .unwrap();

        assert_eq!(*paths.current_exe_calls.borrow(), 0);
        assert_eq!(
            runner.commands.borrow().as_slice(),
            &[windows_delete_command(), windows_query_command(),]
        );
    }

    #[test]
    fn windows_registration_uses_unelevated_current_user_interactive_token() {
        let command =
            windows_register_command(Path::new(r"C:\Users\测试 User\O'Brien\CodexHub.exe"));
        let script = command.args.last().unwrap();

        assert!(script.contains("New-Object -ComObject 'Schedule.Service'"));
        assert!(script.contains("$definition.Principal.UserId=$sid"));
        assert!(script.contains("$definition.Principal.LogonType=3"));
        assert!(script.contains("$definition.Principal.RunLevel=0"));
        assert!(script.contains("$definition.Triggers.Create(9)"));
        assert!(script.contains("RegisterTaskDefinition"));
        assert!(script.contains(",6,$sid,$null,3,$null)"));
        assert!(script.contains(r"C:\Users\测试 User\O''Brien\CodexHub.exe"));
        assert!(!script.contains("schtasks"));
    }

    #[test]
    fn windows_readback_rejects_stale_and_accepts_spaced_non_ascii_path() {
        let paths = FakePaths::new(
            PathBuf::from(r"C:\应用 程序\CodexHub\codexhub.exe"),
            PathBuf::from(r"C:\Users\codexhub"),
        );
        let stale = RecordingRunner::sequence(vec![Ok(command_outcome(
            Some(0),
            &windows_query_output(r"D:\Old\codexhub.exe"),
            "",
        ))]);
        let status =
            get_autostart_status_with_dependencies(OperatingSystem::Windows, &paths, &stale)
                .unwrap();
        assert!(!status.enabled);
        assert_eq!(status.state, "malformed-or-stale");

        let valid = RecordingRunner::sequence(vec![Ok(command_outcome(
            Some(0),
            &windows_query_output(r"C:\应用 程序\CodexHub\codexhub.exe"),
            "",
        ))]);
        assert!(
            get_autostart_status_with_dependencies(OperatingSystem::Windows, &paths, &valid,)
                .unwrap()
                .enabled
        );
    }

    #[test]
    fn windows_readback_rejects_other_user_and_malformed_task_shapes() {
        let paths = FakePaths::new(
            PathBuf::from(r"C:\CodexHub\codexhub.exe"),
            PathBuf::from(r"C:\Users\codexhub"),
        );
        let valid = windows_query_output(r"C:\CodexHub\codexhub.exe");
        let invalid_readbacks = [
            valid.replace(
                "<UserId>S-1-5-21-1000</UserId>",
                "<UserId>S-1-5-21-OTHER</UserId>",
            ),
            valid.replace(
                "<LogonType>InteractiveToken</LogonType>",
                "<LogonType>Password</LogonType>",
            ),
            valid.replace(
                "</LogonType>",
                "</LogonType><RunLevel>HighestAvailable</RunLevel>",
            ),
            valid.replace(super::WINDOWS_TASK_DESCRIPTION, "Not a CodexHub-owned task"),
            valid
                .replace(
                    "<Principal id=\"Author\">",
                    "<PrincipalSpoof id=\"Author\">",
                )
                .replace("</Principal>", "</PrincipalSpoof>"),
            valid.replace("</Principals>", "<Principal id=\"Other\" /></Principals>"),
            valid.replace("</Triggers>", "<LogonTrigger /></Triggers>"),
            valid.replace("</Actions>", "<Exec /></Actions>"),
        ];

        for readback in invalid_readbacks {
            let runner =
                RecordingRunner::sequence(vec![Ok(command_outcome(Some(0), &readback, ""))]);
            let status =
                get_autostart_status_with_dependencies(OperatingSystem::Windows, &paths, &runner)
                    .unwrap();
            assert!(!status.enabled, "invalid task was accepted: {readback}");
            assert_eq!(status.state, "malformed-or-stale");
        }
    }

    #[test]
    fn windows_enable_rolls_back_registration_when_readback_is_malformed() {
        let paths = FakePaths::new(
            PathBuf::from(r"C:\CodexHub\codexhub.exe"),
            PathBuf::from(r"C:\Users\codexhub"),
        );
        let filesystem = MemoryFileSystem::default();
        let runner = RecordingRunner::sequence(vec![
            Ok(command_outcome(Some(0), "created", "")),
            Ok(command_outcome(
                Some(0),
                "CODEXHUB_CURRENT_SID=S-1-5-21-1000\n<Task><Actions /></Task>",
                "",
            )),
            Ok(command_outcome(Some(0), "deleted", "")),
        ]);

        let error = set_autostart_with_dependencies(
            true,
            OperatingSystem::Windows,
            &paths,
            &filesystem,
            &runner,
        )
        .unwrap_err();

        assert!(error.contains("readback verification"));
        assert_eq!(runner.commands.borrow().len(), 3);
        assert_eq!(runner.commands.borrow()[2], windows_delete_command());
    }

    #[test]
    fn windows_missing_registration_reads_back_disabled() {
        let paths = FakePaths::new(
            PathBuf::from(r"C:\CodexHub\codexhub.exe"),
            PathBuf::from(r"C:\Users\codexhub"),
        );
        let runner = RecordingRunner::sequence(vec![Ok(command_outcome(
            Some(0),
            super::WINDOWS_TASK_MISSING_MARKER,
            "",
        ))]);

        let status =
            get_autostart_status_with_dependencies(OperatingSystem::Windows, &paths, &runner)
                .unwrap();
        assert!(!status.enabled);
        assert_eq!(status.state, "missing");
    }

    fn windows_task_xml(command: &str) -> String {
        format!(
            "<Task version=\"1.2\" xmlns=\"http://schemas.microsoft.com/windows/2004/02/mit/task\"><RegistrationInfo><Description>{}</Description></RegistrationInfo><Principals><Principal id=\"Author\"><UserId>S-1-5-21-1000</UserId><LogonType>InteractiveToken</LogonType></Principal></Principals><Triggers><LogonTrigger><Enabled>true</Enabled><UserId>S-1-5-21-1000</UserId></LogonTrigger></Triggers><Settings><Enabled>true</Enabled></Settings><Actions Context=\"Author\"><Exec><Command>{command}</Command></Exec></Actions></Task>",
            super::WINDOWS_TASK_DESCRIPTION,
        )
    }

    fn windows_query_output(command: &str) -> String {
        format!(
            "{}S-1-5-21-1000\n{}",
            super::WINDOWS_TASK_SID_PREFIX,
            windows_task_xml(command),
        )
    }

    fn windows_register_command(exe: &Path) -> RecordedCommand {
        RecordedCommand {
            program: PathBuf::from("powershell.exe"),
            args: super::windows_powershell_args(&super::windows_register_script(exe)),
        }
    }

    fn windows_delete_command() -> RecordedCommand {
        RecordedCommand {
            program: PathBuf::from("powershell.exe"),
            args: super::windows_powershell_args(&super::windows_delete_script()),
        }
    }

    fn windows_query_command() -> RecordedCommand {
        RecordedCommand {
            program: PathBuf::from("powershell.exe"),
            args: super::windows_powershell_args(&super::windows_query_script()),
        }
    }

    #[test]
    fn macos_set_autostart_writes_launch_agent_plist() {
        let home = PathBuf::from("home").join("alice");
        let exe = PathBuf::from("Applications")
            .join("CodexHub & Tools")
            .join("codexhub");
        let paths = FakePaths::new(exe, home.clone());
        let filesystem = MemoryFileSystem::default();
        let runner = RecordingRunner::successful();

        set_autostart_with_dependencies(true, OperatingSystem::Macos, &paths, &filesystem, &runner)
            .unwrap();

        assert!(runner.commands.borrow().is_empty());
        let plist_path = home
            .join("Library")
            .join("LaunchAgents")
            .join(super::macos_plist_file());
        assert!(filesystem
            .created_dirs
            .borrow()
            .contains(&home.join("Library").join("LaunchAgents")));
        let writes = filesystem.writes.borrow();
        let plist = writes.get(&plist_path).unwrap();
        assert!(plist.contains(&format!(
            "<string>{}</string>",
            super::macos_label()
        )));
        assert!(plist.contains("CodexHub &amp; Tools"));
        assert!(plist.contains("<string>start</string>"));
        assert!(plist.contains("<key>RunAtLoad</key>\n  <true/>"));
        assert!(plist.contains("<key>KeepAlive</key>\n  <false/>"));
    }

    #[test]
    fn macos_remove_autostart_deletes_launch_agent_plist_if_present() {
        let home = PathBuf::from("home").join("alice");
        let paths = FakePaths::new(PathBuf::from("codexhub"), home.clone());
        let filesystem = MemoryFileSystem::default();
        let runner = RecordingRunner::successful();

        remove_autostart_with_dependencies(OperatingSystem::Macos, &paths, &filesystem, &runner)
            .unwrap();

        assert!(runner.commands.borrow().is_empty());
        assert_eq!(
            filesystem.removed_files.borrow().as_slice(),
            &[home
                .join("Library")
                .join("LaunchAgents")
                .join(super::macos_plist_file())]
        );
    }

    #[test]
    fn linux_set_autostart_writes_systemd_user_service_and_enables_it() {
        let home = PathBuf::from("home").join("alice");
        let exe = PathBuf::from("opt/codexhub/codexhub");
        let paths = FakePaths::new(exe.clone(), home.clone());
        let filesystem = MemoryFileSystem::default();
        let runner = RecordingRunner::successful();

        set_autostart_with_dependencies(true, OperatingSystem::Linux, &paths, &filesystem, &runner)
            .unwrap();

        let service_path = home
            .join(".config")
            .join("systemd")
            .join("user")
            .join(super::linux_service_file());
        assert!(filesystem
            .created_dirs
            .borrow()
            .contains(&home.join(".config").join("systemd").join("user")));
        let writes = filesystem.writes.borrow();
        let service = writes.get(&service_path).unwrap();
        assert!(service.contains("[Unit]"));
        assert!(service.contains("[Service]"));
        assert!(service.contains(&format!("ExecStart={} start", exe.to_string_lossy())));
        assert!(service.contains("[Install]"));
        assert!(service.contains("WantedBy=default.target"));
        assert_eq!(
            runner.commands.borrow().as_slice(),
            &[
                RecordedCommand {
                    program: PathBuf::from("systemctl"),
                    args: vec!["--user".to_string(), "daemon-reload".to_string(),],
                },
                RecordedCommand {
                    program: PathBuf::from("systemctl"),
                    args: vec![
                        "--user".to_string(),
                        "enable".to_string(),
                        super::linux_service_file().to_string(),
                    ],
                }
            ]
        );
    }

    #[test]
    fn linux_set_autostart_escapes_systemd_exec_paths_with_spaces_quotes_and_percent() {
        let home = PathBuf::from("home").join("alice");
        let exe = PathBuf::from("opt/Codex Hub/quoted\"dir/codex%hub");
        let paths = FakePaths::new(exe, home.clone());
        let filesystem = MemoryFileSystem::default();
        let runner = RecordingRunner::successful();

        set_autostart_with_dependencies(true, OperatingSystem::Linux, &paths, &filesystem, &runner)
            .unwrap();

        let service_path = home
            .join(".config")
            .join("systemd")
            .join("user")
            .join(super::linux_service_file());
        let writes = filesystem.writes.borrow();
        let service = writes.get(&service_path).unwrap();
        assert!(service.contains("ExecStart=\"opt/Codex Hub/quoted\\\"dir/codex%%hub\" start"));
    }

    #[test]
    fn linux_set_autostart_returns_daemon_reload_failure_before_enabling() {
        let home = PathBuf::from("home").join("alice");
        let paths = FakePaths::new(PathBuf::from("codexhub"), home);
        let filesystem = MemoryFileSystem::default();
        let runner =
            RecordingRunner::sequence(vec![Ok(command_outcome(Some(1), "", "reload failed"))]);

        let error = set_autostart_with_dependencies(
            true,
            OperatingSystem::Linux,
            &paths,
            &filesystem,
            &runner,
        )
        .unwrap_err();

        assert!(error.contains("reload Linux systemd user daemon"));
        assert_eq!(
            runner.commands.borrow().as_slice(),
            &[RecordedCommand {
                program: PathBuf::from("systemctl"),
                args: vec!["--user".to_string(), "daemon-reload".to_string()],
            }]
        );
    }

    #[test]
    fn linux_remove_autostart_disables_service_and_deletes_service_file() {
        let home = PathBuf::from("home").join("alice");
        let paths = FakePaths::new(PathBuf::from("codexhub"), home.clone());
        let filesystem = MemoryFileSystem::default();
        let runner = RecordingRunner::successful();

        remove_autostart_with_dependencies(OperatingSystem::Linux, &paths, &filesystem, &runner)
            .unwrap();

        assert_eq!(
            runner.commands.borrow().as_slice(),
            &[
                RecordedCommand {
                    program: PathBuf::from("systemctl"),
                    args: vec![
                        "--user".to_string(),
                        "disable".to_string(),
                        super::linux_service_file().to_string(),
                    ],
                },
                RecordedCommand {
                    program: PathBuf::from("systemctl"),
                    args: vec!["--user".to_string(), "daemon-reload".to_string()],
                }
            ]
        );
        assert_eq!(
            filesystem.removed_files.borrow().as_slice(),
            &[home
                .join(".config")
                .join("systemd")
                .join("user")
                .join(super::linux_service_file())]
        );
    }

    #[test]
    fn linux_remove_autostart_deletes_service_file_when_disable_reports_missing_unit() {
        let home = PathBuf::from("home").join("alice");
        let paths = FakePaths::new(PathBuf::from("codexhub"), home.clone());
        let filesystem = MemoryFileSystem::default();
        let runner = RecordingRunner::sequence(vec![
            Ok(command_outcome(
                Some(1),
                "",
                &format!(
                    "Failed to disable unit: Unit file {} does not exist.",
                    super::linux_service_file()
                ),
            )),
            Ok(command_outcome(Some(0), "", "")),
        ]);

        remove_autostart_with_dependencies(OperatingSystem::Linux, &paths, &filesystem, &runner)
            .unwrap();

        assert_eq!(
            filesystem.removed_files.borrow().as_slice(),
            &[home
                .join(".config")
                .join("systemd")
                .join("user")
                .join(super::linux_service_file())]
        );
        assert_eq!(
            runner.commands.borrow().as_slice(),
            &[
                RecordedCommand {
                    program: PathBuf::from("systemctl"),
                    args: vec![
                        "--user".to_string(),
                        "disable".to_string(),
                        super::linux_service_file().to_string(),
                    ],
                },
                RecordedCommand {
                    program: PathBuf::from("systemctl"),
                    args: vec!["--user".to_string(), "daemon-reload".to_string()],
                }
            ]
        );
    }

    #[test]
    fn linux_remove_autostart_deletes_service_file_when_systemctl_is_unavailable() {
        let home = PathBuf::from("home").join("alice");
        let paths = FakePaths::new(PathBuf::from("codexhub"), home.clone());
        let filesystem = MemoryFileSystem::default();
        let runner = RecordingRunner::sequence(vec![
            Err("failed to start systemctl: program not found".to_string()),
            Err("failed to start systemctl: program not found".to_string()),
        ]);

        remove_autostart_with_dependencies(OperatingSystem::Linux, &paths, &filesystem, &runner)
            .unwrap();

        assert_eq!(
            filesystem.removed_files.borrow().as_slice(),
            &[home
                .join(".config")
                .join("systemd")
                .join("user")
                .join(super::linux_service_file())]
        );
    }

    #[test]
    fn linux_remove_autostart_succeeds_when_disable_reports_not_enabled() {
        let home = PathBuf::from("home").join("alice");
        let paths = FakePaths::new(PathBuf::from("codexhub"), home.clone());
        let filesystem = MemoryFileSystem::default();
        let runner = RecordingRunner::sequence(vec![
            Ok(command_outcome(
                Some(1),
                "",
                &format!("Unit {} is not enabled.", super::linux_service_file()),
            )),
            Ok(command_outcome(Some(0), "", "")),
        ]);

        remove_autostart_with_dependencies(OperatingSystem::Linux, &paths, &filesystem, &runner)
            .unwrap();

        assert_eq!(
            filesystem.removed_files.borrow().as_slice(),
            &[home
                .join(".config")
                .join("systemd")
                .join("user")
                .join(super::linux_service_file())]
        );
    }

    #[test]
    fn linux_remove_autostart_succeeds_when_user_bus_is_unavailable() {
        let home = PathBuf::from("home").join("alice");
        let paths = FakePaths::new(PathBuf::from("codexhub"), home.clone());
        let filesystem = MemoryFileSystem::default();
        let runner = RecordingRunner::sequence(vec![
            Ok(command_outcome(
                Some(1),
                "",
                "Failed to connect to bus: No medium found",
            )),
            Ok(command_outcome(
                Some(1),
                "",
                "Failed to connect to bus: No medium found",
            )),
        ]);

        remove_autostart_with_dependencies(OperatingSystem::Linux, &paths, &filesystem, &runner)
            .unwrap();

        assert_eq!(
            filesystem.removed_files.borrow().as_slice(),
            &[home
                .join(".config")
                .join("systemd")
                .join("user")
                .join(super::linux_service_file())]
        );
    }

    #[test]
    fn linux_remove_autostart_preserves_real_service_file_removal_failure() {
        let home = PathBuf::from("home").join("alice");
        let paths = FakePaths::new(PathBuf::from("codexhub"), home);
        let filesystem = MemoryFileSystem::new_with_remove_error("permission denied".to_string());
        let runner = RecordingRunner::failed(
            1,
            "",
            "Failed to connect to bus: $DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined",
        );

        let error = remove_autostart_with_dependencies(
            OperatingSystem::Linux,
            &paths,
            &filesystem,
            &runner,
        )
        .unwrap_err();

        assert!(error.contains("permission denied"));
        assert_eq!(runner.commands.borrow().len(), 1);
    }

    #[test]
    fn set_autostart_false_uses_removal_path_without_resolving_current_exe() {
        let home = PathBuf::from("home").join("alice");
        let paths = FakePaths::new_with_current_exe_error(
            "current exe should not be needed".to_string(),
            home.clone(),
        );
        let filesystem = MemoryFileSystem::default();
        let runner = RecordingRunner::successful();

        set_autostart_with_dependencies(
            false,
            OperatingSystem::Linux,
            &paths,
            &filesystem,
            &runner,
        )
        .unwrap();

        assert_eq!(*paths.current_exe_calls.borrow(), 0);
        assert_eq!(
            runner.commands.borrow().as_slice(),
            &[
                RecordedCommand {
                    program: PathBuf::from("systemctl"),
                    args: vec![
                        "--user".to_string(),
                        "disable".to_string(),
                        super::linux_service_file().to_string(),
                    ],
                },
                RecordedCommand {
                    program: PathBuf::from("systemctl"),
                    args: vec!["--user".to_string(), "daemon-reload".to_string()],
                }
            ]
        );
        assert_eq!(
            filesystem.removed_files.borrow().as_slice(),
            &[home
                .join(".config")
                .join("systemd")
                .join("user")
                .join(super::linux_service_file())]
        );
    }

    #[test]
    fn windows_command_failure_is_sanitized() {
        let paths = FakePaths::new(
            PathBuf::from(r"C:\CodexHub\codexhub.exe"),
            PathBuf::from(r"C:\Users\codexhub"),
        );
        let filesystem = MemoryFileSystem::default();
        let runner = RecordingRunner::failed(42, "printed stdout", "printed stderr");

        let error = set_autostart_with_dependencies(
            true,
            OperatingSystem::Windows,
            &paths,
            &filesystem,
            &runner,
        )
        .unwrap_err();

        assert!(error.contains("exit code 42"));
        assert!(!error.contains("printed stdout"));
        assert!(!error.contains("printed stderr"));
        assert!(!error.contains(r"C:\CodexHub"));
    }

    #[test]
    fn current_exe_failure_returns_useful_error() {
        let paths = FakePaths::new_with_current_exe_error(
            "simulated current_exe failure".to_string(),
            PathBuf::from("home").join("alice"),
        );
        let filesystem = MemoryFileSystem::default();
        let runner = RecordingRunner::successful();

        let error = set_autostart_with_dependencies(
            true,
            OperatingSystem::Windows,
            &paths,
            &filesystem,
            &runner,
        )
        .unwrap_err();

        assert!(error.contains("simulated current_exe failure"));
        assert!(runner.commands.borrow().is_empty());
    }

    #[derive(Debug, PartialEq, Eq)]
    struct RecordedCommand {
        program: PathBuf,
        args: Vec<String>,
    }

    struct RecordingRunner {
        commands: RefCell<Vec<RecordedCommand>>,
        outcomes: RefCell<VecDeque<Result<CommandOutcome, String>>>,
        fallback: Result<CommandOutcome, String>,
    }

    impl RecordingRunner {
        fn successful() -> Self {
            Self {
                commands: RefCell::new(Vec::new()),
                outcomes: RefCell::new(VecDeque::new()),
                fallback: Ok(command_outcome(Some(0), "ok", "")),
            }
        }

        fn failed(code: i32, stdout: &str, stderr: &str) -> Self {
            Self {
                commands: RefCell::new(Vec::new()),
                outcomes: RefCell::new(VecDeque::new()),
                fallback: Ok(command_outcome(Some(code), stdout, stderr)),
            }
        }

        fn sequence(outcomes: Vec<Result<CommandOutcome, String>>) -> Self {
            Self {
                commands: RefCell::new(Vec::new()),
                outcomes: RefCell::new(VecDeque::from(outcomes)),
                fallback: Err("unexpected command without configured outcome".to_string()),
            }
        }
    }

    impl CommandRunner for RecordingRunner {
        fn run(&self, program: &Path, args: &[String]) -> Result<CommandOutcome, String> {
            self.commands.borrow_mut().push(RecordedCommand {
                program: program.to_path_buf(),
                args: args.to_vec(),
            });
            self.outcomes
                .borrow_mut()
                .pop_front()
                .unwrap_or_else(|| self.fallback.clone())
        }
    }

    fn command_outcome(code: Option<i32>, stdout: &str, stderr: &str) -> CommandOutcome {
        CommandOutcome {
            code,
            stdout: stdout.to_string(),
            stderr: stderr.to_string(),
        }
    }

    struct FakePaths {
        current_exe: Result<PathBuf, String>,
        home_dir: PathBuf,
        current_exe_calls: RefCell<usize>,
    }

    impl FakePaths {
        fn new(current_exe: PathBuf, home_dir: PathBuf) -> Self {
            Self {
                current_exe: Ok(current_exe),
                home_dir,
                current_exe_calls: RefCell::new(0),
            }
        }

        fn new_with_current_exe_error(error: String, home_dir: PathBuf) -> Self {
            Self {
                current_exe: Err(error),
                home_dir,
                current_exe_calls: RefCell::new(0),
            }
        }
    }

    impl Default for FakePaths {
        fn default() -> Self {
            Self::new(PathBuf::from("codexhub"), PathBuf::from("home"))
        }
    }

    impl AutostartPathProvider for FakePaths {
        fn current_exe(&self) -> Result<PathBuf, String> {
            *self.current_exe_calls.borrow_mut() += 1;
            self.current_exe.clone()
        }

        fn home_dir(&self) -> Result<PathBuf, String> {
            Ok(self.home_dir.clone())
        }
    }

    #[derive(Default)]
    struct MemoryFileSystem {
        created_dirs: RefCell<Vec<PathBuf>>,
        writes: RefCell<HashMap<PathBuf, String>>,
        removed_files: RefCell<Vec<PathBuf>>,
        remove_error: Option<String>,
    }

    impl MemoryFileSystem {
        fn new_with_remove_error(remove_error: String) -> Self {
            Self {
                remove_error: Some(remove_error),
                ..Self::default()
            }
        }
    }

    impl AutostartFileSystem for MemoryFileSystem {
        fn create_dir_all(&self, path: &Path) -> Result<(), String> {
            self.created_dirs.borrow_mut().push(path.to_path_buf());
            Ok(())
        }

        fn write(&self, path: &Path, content: &str) -> Result<(), String> {
            self.writes
                .borrow_mut()
                .insert(path.to_path_buf(), content.to_string());
            Ok(())
        }

        fn remove_file_if_exists(&self, path: &Path) -> Result<(), String> {
            self.removed_files.borrow_mut().push(path.to_path_buf());
            if let Some(error) = &self.remove_error {
                return Err(error.clone());
            }
            Ok(())
        }
    }
}
