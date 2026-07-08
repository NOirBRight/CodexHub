use crate::config::{self, CommandOutcome, CommandRunner, ProcessCommandRunner};
use std::fs;
use std::path::{Path, PathBuf};

const WINDOWS_TASK_NAME: &str = "CodexHubProxy";
const MACOS_LABEL: &str = "com.codexhub.proxy";
const MACOS_PLIST_FILE: &str = "com.codexhub.proxy.plist";
const LINUX_SERVICE_FILE: &str = "codexhub-proxy.service";

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
    let program = Path::new("schtasks");
    let args = vec![
        "/Create".to_string(),
        "/TN".to_string(),
        WINDOWS_TASK_NAME.to_string(),
        "/TR".to_string(),
        windows_task_command(exe),
        "/SC".to_string(),
        "ONLOGON".to_string(),
        "/RL".to_string(),
        "LIMITED".to_string(),
        "/F".to_string(),
    ];
    run_autostart_command("create Windows autostart task", program, &args, runner)?;

    Ok(format!(
        "Autostart enabled via Windows Task Scheduler task {WINDOWS_TASK_NAME}"
    ))
}

fn remove_windows_autostart(runner: &dyn CommandRunner) -> Result<String, String> {
    let program = Path::new("schtasks");
    let args = vec![
        "/Delete".to_string(),
        "/TN".to_string(),
        WINDOWS_TASK_NAME.to_string(),
        "/F".to_string(),
    ];
    let label = "delete Windows autostart task";
    let outcome = runner
        .run(program, &args)
        .map_err(|error| format!("{label} failed to start: {error}"))?;

    if outcome.code != Some(0) && !is_windows_task_missing(&outcome) {
        return Err(config::format_command_failure(
            label, program, &args, &outcome,
        ));
    }

    Ok(format!(
        "Autostart removed from Windows Task Scheduler task {WINDOWS_TASK_NAME}"
    ))
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
    args.push(LINUX_SERVICE_FILE.to_string());
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
        .join(MACOS_PLIST_FILE))
}

fn linux_service_path(paths: &dyn AutostartPathProvider) -> Result<PathBuf, String> {
    Ok(paths
        .home_dir()?
        .join(".config")
        .join("systemd")
        .join("user")
        .join(LINUX_SERVICE_FILE))
}

fn windows_task_command(exe: &Path) -> String {
    format!("\"{}\" start", exe.to_string_lossy())
}

fn macos_plist_content(exe: &Path) -> String {
    let exe = escape_xml(&exe.to_string_lossy());
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "https://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{MACOS_LABEL}</string>
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
"#
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

fn is_windows_task_missing(outcome: &CommandOutcome) -> bool {
    command_output_contains(
        outcome,
        &[
            "cannot find the file specified",
            "task does not exist",
            "the system cannot find",
        ],
    )
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
                "unit codexhub-proxy.service does not exist",
                "unit file codexhub-proxy.service does not exist",
                "codexhub-proxy.service does not exist",
                "codexhub-proxy.service not found",
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
        remove_autostart_with_dependencies, set_autostart_with_dependencies, AutostartFileSystem,
        AutostartPathProvider, OperatingSystem, LINUX_SERVICE_FILE, MACOS_PLIST_FILE,
        WINDOWS_TASK_NAME,
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
        let runner = RecordingRunner::successful();

        let message = set_autostart_with_dependencies(
            true,
            OperatingSystem::Windows,
            &paths,
            &filesystem,
            &runner,
        )
        .unwrap();

        assert!(message.contains(WINDOWS_TASK_NAME));
        assert_eq!(filesystem.writes.borrow().len(), 0);
        assert_eq!(
            runner.commands.borrow().as_slice(),
            &[RecordedCommand {
                program: PathBuf::from("schtasks"),
                args: vec![
                    "/Create".to_string(),
                    "/TN".to_string(),
                    WINDOWS_TASK_NAME.to_string(),
                    "/TR".to_string(),
                    r#""C:\Program Files\CodexHub\codexhub.exe" start"#.to_string(),
                    "/SC".to_string(),
                    "ONLOGON".to_string(),
                    "/RL".to_string(),
                    "LIMITED".to_string(),
                    "/F".to_string(),
                ],
            }]
        );
    }

    #[test]
    fn windows_remove_autostart_deletes_logon_task() {
        let paths = FakePaths::default();
        let filesystem = MemoryFileSystem::default();
        let runner = RecordingRunner::successful();

        remove_autostart_with_dependencies(OperatingSystem::Windows, &paths, &filesystem, &runner)
            .unwrap();

        assert_eq!(
            runner.commands.borrow().as_slice(),
            &[RecordedCommand {
                program: PathBuf::from("schtasks"),
                args: vec![
                    "/Delete".to_string(),
                    "/TN".to_string(),
                    WINDOWS_TASK_NAME.to_string(),
                    "/F".to_string(),
                ],
            }]
        );
    }

    #[test]
    fn windows_set_autostart_false_tolerates_missing_logon_task() {
        let paths = FakePaths::new_with_current_exe_error(
            "current exe should not be needed".to_string(),
            PathBuf::from(r"C:\Users\codexhub"),
        );
        let filesystem = MemoryFileSystem::default();
        let runner =
            RecordingRunner::failed(1, "", "ERROR: The system cannot find the file specified.");

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
            &[RecordedCommand {
                program: PathBuf::from("schtasks"),
                args: vec![
                    "/Delete".to_string(),
                    "/TN".to_string(),
                    WINDOWS_TASK_NAME.to_string(),
                    "/F".to_string(),
                ],
            }]
        );
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
            .join(MACOS_PLIST_FILE);
        assert!(filesystem
            .created_dirs
            .borrow()
            .contains(&home.join("Library").join("LaunchAgents")));
        let writes = filesystem.writes.borrow();
        let plist = writes.get(&plist_path).unwrap();
        assert!(plist.contains("<string>com.codexhub.proxy</string>"));
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
                .join(MACOS_PLIST_FILE)]
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
            .join(LINUX_SERVICE_FILE);
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
                        LINUX_SERVICE_FILE.to_string(),
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
            .join(LINUX_SERVICE_FILE);
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
                        LINUX_SERVICE_FILE.to_string(),
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
                .join(LINUX_SERVICE_FILE)]
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
                "Failed to disable unit: Unit file codexhub-proxy.service does not exist.",
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
                .join(LINUX_SERVICE_FILE)]
        );
        assert_eq!(
            runner.commands.borrow().as_slice(),
            &[
                RecordedCommand {
                    program: PathBuf::from("systemctl"),
                    args: vec![
                        "--user".to_string(),
                        "disable".to_string(),
                        LINUX_SERVICE_FILE.to_string(),
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
                .join(LINUX_SERVICE_FILE)]
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
                "Unit codexhub-proxy.service is not enabled.",
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
                .join(LINUX_SERVICE_FILE)]
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
                .join(LINUX_SERVICE_FILE)]
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
                        LINUX_SERVICE_FILE.to_string(),
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
                .join(LINUX_SERVICE_FILE)]
        );
    }

    #[test]
    fn command_runner_failure_includes_exit_code_stdout_and_stderr() {
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
        assert!(error.contains("printed stdout"));
        assert!(error.contains("printed stderr"));
        assert!(error.contains("schtasks"));
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
