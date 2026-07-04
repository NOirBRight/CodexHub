use crate::config::{self, CommandRunner, ConfigPaths, ProcessCommandRunner};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

pub fn sync_history(target_provider: Option<&str>) -> Result<String, String> {
    let paths = ConfigPaths::runtime()?;
    let python = config::find_python();
    let runner = ProcessCommandRunner;

    sync_history_with_paths(target_provider, &paths, &python, &runner)
}

fn sync_history_with_paths(
    target_provider: Option<&str>,
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
) -> Result<String, String> {
    let target_provider = match target_provider {
        None | Some("custom") => "custom",
        Some("openai") => "openai",
        Some(value) => {
            return Err(format!(
                "unsupported history repair target: {value}; expected custom or openai"
            ))
        }
    };
    fs::create_dir_all(paths.proxy_dir()).map_err(|error| {
        format!(
            "failed to create history backup parent {}: {error}",
            paths.proxy_dir().display()
        )
    })?;

    let backup_root = history_manual_backup_root(paths);
    let outcome = config::run_python_script(
        "history bucket repair",
        python,
        paths.history_overlay_script(),
        vec![
            "repair-history".to_string(),
            "--codex-dir".to_string(),
            paths.codex_dir().to_string_lossy().into_owned(),
            "--backup-root".to_string(),
            backup_root.to_string_lossy().into_owned(),
            "--target".to_string(),
            target_provider.to_string(),
            "--ledger-root".to_string(),
            paths.proxy_dir().to_string_lossy().into_owned(),
        ],
        runner,
    )?;

    let stdout = outcome.stdout.trim();
    let mut message = format!(
        "History bucket repair completed for {target_provider}; backup root: {}",
        backup_root.display()
    );
    if !stdout.is_empty() {
        message.push('\n');
        message.push_str(stdout);
    }

    Ok(message)
}

pub fn migrate_official_history_to_unified() -> Result<String, String> {
    let paths = ConfigPaths::runtime()?;
    let python = config::find_python();
    let runner = ProcessCommandRunner;

    migrate_official_history_to_unified_with_paths(&paths, &python, &runner)
}

fn migrate_official_history_to_unified_with_paths(
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
) -> Result<String, String> {
    fs::create_dir_all(paths.proxy_dir()).map_err(|error| {
        format!(
            "failed to create history backup parent {}: {error}",
            paths.proxy_dir().display()
        )
    })?;
    let backup_root = history_backup_root(paths, "history-official-to-unified");
    let outcome = config::run_python_script(
        "official history migration",
        python,
        paths.history_overlay_script(),
        vec![
            "migrate-official-to-unified".to_string(),
            "--codex-dir".to_string(),
            paths.codex_dir().to_string_lossy().into_owned(),
            "--backup-root".to_string(),
            backup_root.to_string_lossy().into_owned(),
        ],
        runner,
    )?;
    let stdout = outcome.stdout.trim();
    let mut message = format!(
        "Official history migration completed; backup root: {}",
        backup_root.display()
    );
    if !stdout.is_empty() {
        message.push('\n');
        message.push_str(stdout);
    }
    Ok(message)
}

pub fn restore_official_history_from_unified() -> Result<String, String> {
    let paths = ConfigPaths::runtime()?;
    let python = config::find_python();
    let runner = ProcessCommandRunner;

    restore_official_history_from_unified_with_paths(&paths, &python, &runner)
}

fn restore_official_history_from_unified_with_paths(
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
) -> Result<String, String> {
    fs::create_dir_all(paths.proxy_dir()).map_err(|error| {
        format!(
            "failed to create history backup parent {}: {error}",
            paths.proxy_dir().display()
        )
    })?;
    let backup_root = history_backup_root(paths, "history-unified-to-official");
    let outcome = config::run_python_script(
        "official history restore",
        python,
        paths.history_overlay_script(),
        vec![
            "restore-official-from-unified".to_string(),
            "--codex-dir".to_string(),
            paths.codex_dir().to_string_lossy().into_owned(),
            "--backup-root".to_string(),
            backup_root.to_string_lossy().into_owned(),
            "--ledger-root".to_string(),
            paths.proxy_dir().to_string_lossy().into_owned(),
        ],
        runner,
    )?;
    let stdout = outcome.stdout.trim();
    let mut message = format!(
        "Official history restore completed; backup root: {}",
        backup_root.display()
    );
    if !stdout.is_empty() {
        message.push('\n');
        message.push_str(stdout);
    }
    Ok(message)
}

fn history_manual_backup_root(paths: &ConfigPaths) -> PathBuf {
    history_backup_root(paths, "history-bucket-repair")
}

fn history_backup_root(paths: &ConfigPaths, prefix: &str) -> PathBuf {
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or_default();

    paths.proxy_dir().join(format!("{prefix}-{stamp}"))
}

#[cfg(test)]
mod tests {
    use super::{
        migrate_official_history_to_unified_with_paths,
        restore_official_history_from_unified_with_paths, sync_history_with_paths,
    };
    use crate::config::{CommandOutcome, CommandRunner, ConfigPaths};
    use std::cell::RefCell;
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn sync_history_runs_unified_repair_and_returns_stdout_context() {
        let root = temp_root("history-default-custom");
        let codex_home = root.join("codex-home");
        let repo_root = root.join("repo-root");
        let paths = test_paths(&root);
        write_fake_history_script(&repo_root);
        let runner =
            RecordingRunner::successful("status=completed\nstate_rows=2\njsonl_applied=3\n");

        let result = sync_history_with_paths(None, &paths, Path::new("python-test"), &runner)
            .expect("history sync");

        assert!(result.contains("History bucket repair completed for custom"));
        assert!(result.contains("state_rows=2"));
        assert!(result.contains("jsonl_applied=3"));

        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 1);
        assert_eq!(commands[0].program, PathBuf::from("python-test"));
        assert_eq!(
            commands[0].args[0],
            repo_root
                .join("src-python")
                .join("history_overlay.py")
                .to_string_lossy()
        );
        assert_contains_sequence(&commands[0].args, &["repair-history"]);
        assert_arg_value(&commands[0].args, "--codex-dir", &codex_home);
        assert_arg_literal(&commands[0].args, "--target", "custom");
        assert_arg_value(
            &commands[0].args,
            "--ledger-root",
            &codex_home.join("proxy"),
        );
        let backup_root = PathBuf::from(arg_value(&commands[0].args, "--backup-root"));
        assert!(backup_root.starts_with(codex_home.join("proxy")));
        assert!(backup_root
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.starts_with("history-bucket-repair-")));
    }

    #[test]
    fn sync_history_repairs_openai_target_when_requested() {
        let root = temp_root("history-openai-target");
        let codex_home = root.join("codex-home");
        let repo_root = root.join("repo-root");
        let paths = test_paths(&root);
        write_fake_history_script(&repo_root);
        let runner = RecordingRunner::successful("state_rows=1\n");

        let result =
            sync_history_with_paths(Some("openai"), &paths, Path::new("python-test"), &runner)
                .expect("history sync");

        assert!(result.contains("History bucket repair completed for openai"));
        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 1);
        assert_contains_sequence(&commands[0].args, &["repair-history"]);
        assert_arg_value(&commands[0].args, "--codex-dir", &codex_home);
        assert_arg_literal(&commands[0].args, "--target", "openai");
        assert_arg_value(
            &commands[0].args,
            "--ledger-root",
            &codex_home.join("proxy"),
        );
    }

    #[test]
    fn sync_history_rejects_invalid_target_provider() {
        let root = temp_root("history-invalid-target");
        let repo_root = root.join("repo-root");
        let paths = test_paths(&root);
        write_fake_history_script(&repo_root);
        let runner = RecordingRunner::successful("status=already-unified\n");

        let error =
            sync_history_with_paths(Some("official"), &paths, Path::new("python-test"), &runner)
                .expect_err("invalid target should fail");

        assert!(error.contains("unsupported history repair target"));
        assert_eq!(runner.commands.borrow().len(), 0);
    }

    #[test]
    fn migrate_official_history_command_uses_dedicated_subcommand() {
        let root = temp_root("history-migrate-official");
        let codex_home = root.join("codex-home");
        let repo_root = root.join("repo-root");
        let paths = test_paths(&root);
        write_fake_history_script(&repo_root);
        let runner = RecordingRunner::successful("status=completed\nstate_rows=2\n");

        let result = migrate_official_history_to_unified_with_paths(
            &paths,
            Path::new("python-test"),
            &runner,
        )
        .expect("migrate official");

        assert!(result.contains("Official history migration completed"));
        assert!(result.contains("state_rows=2"));
        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 1);
        assert_contains_sequence(&commands[0].args, &["migrate-official-to-unified"]);
        assert_arg_value(&commands[0].args, "--codex-dir", &codex_home);
        let backup_root = PathBuf::from(arg_value(&commands[0].args, "--backup-root"));
        assert!(backup_root
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.starts_with("history-official-to-unified-")));
    }

    #[test]
    fn restore_official_history_command_uses_ledger_root() {
        let root = temp_root("history-restore-official");
        let codex_home = root.join("codex-home");
        let repo_root = root.join("repo-root");
        let paths = test_paths(&root);
        write_fake_history_script(&repo_root);
        let runner = RecordingRunner::successful("status=completed\njsonl_restored=1\n");

        let result = restore_official_history_from_unified_with_paths(
            &paths,
            Path::new("python-test"),
            &runner,
        )
        .expect("restore official");

        assert!(result.contains("Official history restore completed"));
        assert!(result.contains("jsonl_restored=1"));
        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 1);
        assert_contains_sequence(&commands[0].args, &["restore-official-from-unified"]);
        assert_arg_value(&commands[0].args, "--codex-dir", &codex_home);
        assert_arg_value(
            &commands[0].args,
            "--ledger-root",
            &codex_home.join("proxy"),
        );
    }

    #[test]
    fn sync_history_failure_includes_command_stdout_and_stderr() {
        let root = temp_root("history-failure");
        let repo_root = root.join("repo-root");
        let paths = test_paths(&root);
        write_fake_history_script(&repo_root);
        let runner = RecordingRunner::failed(42, "printed stdout", "printed stderr");

        let error = sync_history_with_paths(None, &paths, Path::new("python-test"), &runner)
            .expect_err("history sync should fail");

        assert!(error.contains("history bucket repair failed"));
        assert!(error.contains("exit code 42"));
        assert!(error.contains("command: python-test"));
        assert!(error.contains("history_overlay.py"));
        assert!(error.contains("repair-history"));
        assert!(error.contains("printed stdout"));
        assert!(error.contains("printed stderr"));
    }

    #[derive(Debug, Clone)]
    struct RecordedCommand {
        program: PathBuf,
        args: Vec<String>,
    }

    struct RecordingRunner {
        commands: RefCell<Vec<RecordedCommand>>,
        outcome: CommandOutcome,
    }

    impl RecordingRunner {
        fn successful(stdout: &str) -> Self {
            Self {
                commands: RefCell::new(Vec::new()),
                outcome: CommandOutcome {
                    code: Some(0),
                    stdout: stdout.to_string(),
                    stderr: String::new(),
                },
            }
        }

        fn failed(code: i32, stdout: &str, stderr: &str) -> Self {
            Self {
                commands: RefCell::new(Vec::new()),
                outcome: CommandOutcome {
                    code: Some(code),
                    stdout: stdout.to_string(),
                    stderr: stderr.to_string(),
                },
            }
        }
    }

    impl CommandRunner for RecordingRunner {
        fn run(&self, program: &Path, args: &[String]) -> Result<CommandOutcome, String> {
            self.commands.borrow_mut().push(RecordedCommand {
                program: program.to_path_buf(),
                args: args.to_vec(),
            });
            Ok(self.outcome.clone())
        }
    }

    fn test_paths(root: &Path) -> ConfigPaths {
        ConfigPaths::new(root.join("codex-home"), root.join("repo-root"))
    }

    fn write_fake_history_script(repo_root: &Path) {
        let script = repo_root.join("src-python").join("history_overlay.py");
        fs::create_dir_all(script.parent().unwrap()).unwrap();
        fs::write(script, "# fake history overlay").unwrap();
    }

    fn temp_root(name: &str) -> PathBuf {
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "codexhub-history-{name}-{}-{suffix}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&path);
        fs::create_dir_all(&path).unwrap();
        path
    }

    fn assert_contains_sequence(args: &[String], values: &[&str]) {
        let mut position = 0;
        for value in values {
            position = args[position..]
                .iter()
                .position(|arg| arg == value)
                .map(|offset| position + offset + 1)
                .unwrap_or_else(|| panic!("missing argument {value:?} in {args:?}"));
        }
    }

    fn assert_arg_value(args: &[String], name: &str, expected: &Path) {
        assert_arg_literal(args, name, &expected.to_string_lossy());
    }

    fn assert_arg_literal(args: &[String], name: &str, expected: &str) {
        assert_eq!(arg_value(args, name), expected);
    }

    fn arg_value<'a>(args: &'a [String], name: &str) -> &'a str {
        let index = args
            .iter()
            .position(|arg| arg == name)
            .unwrap_or_else(|| panic!("missing argument {name:?} in {args:?}"));
        args.get(index + 1)
            .unwrap_or_else(|| panic!("missing value for {name:?} in {args:?}"))
    }
}
