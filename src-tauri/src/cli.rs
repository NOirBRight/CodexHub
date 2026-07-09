use crate::{autostart, catalog, config, history, models, proxy, AppStatus, Settings};
use serde::Serialize;

pub fn run(args: &[String]) -> i32 {
    match args.first().map(String::as_str) {
        Some("status") => print_result(proxy::status()),
        Some("switch") => {
            run_switch_command(&args[1..], config::get_settings, |mode, auto_sync| {
                config::switch_mode(mode, auto_sync)
            })
        }
        Some("start") => print_result(proxy::start()),
        Some("stop") => print_result(proxy::stop()),
        Some("restart") => print_result(proxy::restart()),
        Some("refresh-models") => print_result(models::refresh_official_models()),
        Some("sync-history") => print_result(history::sync_history(None)),
        Some("sync-catalog") => print_result(catalog::sync_catalog()),
        Some("list-providers") => print_result(config::get_providers()),
        Some("list-models") => print_result(models::list_models()),
        Some("set-autostart") => match parse_set_autostart_enabled(&args[1..]) {
            Ok(enabled) => print_result(autostart::set_autostart(enabled)),
            Err(()) => {
                print_set_autostart_usage();
                2
            }
        },
        Some("remove-autostart") => print_result(autostart::remove_autostart()),
        Some("app") | None => 0,
        Some("-h" | "--help" | "help") => {
            print_help();
            0
        }
        Some(command) => {
            eprintln!("unknown command: {command}");
            print_help();
            2
        }
    }
}

fn parse_set_autostart_enabled(values: &[String]) -> Result<bool, ()> {
    match values {
        [] => Ok(true),
        [value] => match value.as_str() {
            "true" => Ok(true),
            "false" => Ok(false),
            _ => Err(()),
        },
        _ => Err(()),
    }
}

struct SwitchRequest<'a> {
    mode: &'a str,
    auto_sync: Option<bool>,
}

fn parse_switch_args(values: &[String]) -> Result<SwitchRequest<'_>, ()> {
    let Some((mode, flags)) = values.split_first() else {
        return Err(());
    };
    if mode != "official" && mode != "custom" {
        return Err(());
    }

    let mut auto_sync = None;
    for flag in flags {
        let value = match flag.as_str() {
            "--auto-sync" => true,
            "--no-auto-sync" => false,
            _ => return Err(()),
        };
        if auto_sync.replace(value).is_some() {
            return Err(());
        }
    }

    Ok(SwitchRequest { mode, auto_sync })
}

fn run_switch_command<GetSettings, SwitchMode>(
    values: &[String],
    _get_settings: GetSettings,
    switch_mode: SwitchMode,
) -> i32
where
    GetSettings: FnOnce() -> Result<Settings, String>,
    SwitchMode: FnOnce(&str, bool) -> Result<AppStatus, String>,
{
    let request = match parse_switch_args(values) {
        Ok(request) => request,
        Err(()) => {
            print_switch_usage();
            return 2;
        }
    };

    let auto_sync = request.auto_sync.unwrap_or(false);

    print_result(switch_mode(request.mode, auto_sync))
}

fn print_set_autostart_usage() {
    eprintln!("usage: codexhub set-autostart [true|false]");
}

fn print_switch_usage() {
    eprintln!("usage: codexhub switch <official|custom> [--auto-sync|--no-auto-sync]");
}

fn print_result<T: Serialize>(result: Result<T, String>) -> i32 {
    match result {
        Ok(value) => match serde_json::to_string_pretty(&value) {
            Ok(json) => {
                println!("{json}");
                0
            }
            Err(error) => {
                eprintln!("failed to serialize command output: {error}");
                1
            }
        },
        Err(error) => {
            eprintln!("{error}");
            1
        }
    }
}

fn print_help() {
    println!("{}", help_text());
}

fn help_text() -> String {
    format!(
        "\
CodexHub CLI

Usage:
  codexhub app
  codexhub status
  codexhub switch <official|custom> [--auto-sync|--no-auto-sync]
  codexhub start
  codexhub stop
  codexhub restart
  codexhub refresh-models
  codexhub sync-history
  codexhub sync-catalog
  codexhub list-providers
  codexhub list-models
  codexhub set-autostart [true|false]
  codexhub remove-autostart
  codexhub web-bridge [--port {bridge_port}] [--addr HOST:PORT]",
        bridge_port = crate::app_flavor::current().bridge_port()
    )
}

#[cfg(test)]
mod tests {
    use super::{help_text, parse_set_autostart_enabled, run, run_switch_command};
    use crate::config::{self, CommandOutcome, CommandRunner, ConfigPaths};
    use std::cell::RefCell;
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn help_command_succeeds() {
        let args = vec!["help".to_string()];

        assert_eq!(run(&args), 0);
    }

    #[test]
    fn help_text_uses_current_flavor_bridge_port() {
        let help = help_text();

        assert!(help.contains(&format!(
            "codexhub web-bridge [--port {}] [--addr HOST:PORT]",
            crate::app_flavor::current().bridge_port()
        )));
    }

    #[test]
    fn unknown_command_returns_usage_error() {
        let args = vec!["nope".to_string()];

        assert_eq!(run(&args), 2);
    }

    #[test]
    fn set_autostart_accepts_no_value_as_true() {
        let args: Vec<String> = Vec::new();

        assert_eq!(parse_set_autostart_enabled(&args), Ok(true));
    }

    #[test]
    fn set_autostart_accepts_true_and_false_values() {
        let true_args = vec!["true".to_string()];
        let false_args = vec!["false".to_string()];

        assert_eq!(parse_set_autostart_enabled(&true_args), Ok(true));
        assert_eq!(parse_set_autostart_enabled(&false_args), Ok(false));
    }

    #[test]
    fn set_autostart_rejects_unknown_value() {
        let args = vec!["yes".to_string()];

        assert_eq!(parse_set_autostart_enabled(&args), Err(()));
    }

    #[test]
    fn set_autostart_rejects_extra_values() {
        let args = vec!["true".to_string(), "extra".to_string()];

        assert_eq!(parse_set_autostart_enabled(&args), Err(()));
    }

    #[test]
    fn set_autostart_unknown_value_returns_usage_error() {
        let args = vec!["set-autostart".to_string(), "yes".to_string()];

        assert_eq!(run(&args), 2);
    }

    #[test]
    fn switch_no_auto_sync_flag_skips_history_overlay() {
        let root = temp_root("cli-switch-no-auto-sync");
        let paths = test_paths(&root);
        let runner = RecordingRunner::successful();
        let args = vec!["custom".to_string(), "--no-auto-sync".to_string()];

        let exit = run_switch_command(
            &args,
            || panic!("explicit flag should not read settings"),
            |mode, auto_sync| {
                config::switch_mode_with_paths(
                    mode,
                    auto_sync,
                    &paths,
                    Path::new("python-test"),
                    &runner,
                )
            },
        );

        assert_eq!(exit, 0);
        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 1);
        assert!(commands[0].args.iter().any(|arg| arg == "apply"));
        assert!(!commands[0].args.iter().any(|arg| arg == "normalize-fast"));
    }

    #[test]
    fn switch_auto_sync_flag_is_accepted_but_ignored() {
        let root = temp_root("cli-switch-auto-sync");
        let paths = test_paths(&root);
        let runner = RecordingRunner::successful();
        let args = vec!["custom".to_string(), "--auto-sync".to_string()];

        let exit = run_switch_command(
            &args,
            || panic!("explicit flag should not read settings"),
            |mode, auto_sync| {
                config::switch_mode_with_paths(
                    mode,
                    auto_sync,
                    &paths,
                    Path::new("python-test"),
                    &runner,
                )
            },
        );

        assert_eq!(exit, 0);
        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 1);
        assert!(commands[0].args.iter().any(|arg| arg == "apply"));
        assert!(!commands[0].args.iter().any(|arg| arg == "normalize-fast"));
    }

    #[test]
    fn switch_without_flag_does_not_read_settings() {
        let root = temp_root("cli-switch-settings-default");
        let paths = test_paths(&root);
        let runner = RecordingRunner::successful();
        let args = vec!["custom".to_string()];

        let exit = run_switch_command(
            &args,
            || panic!("switch default should not read settings"),
            |mode, auto_sync| {
                config::switch_mode_with_paths(
                    mode,
                    auto_sync,
                    &paths,
                    Path::new("python-test"),
                    &runner,
                )
            },
        );

        assert_eq!(exit, 0);
        assert_eq!(runner.commands.borrow().len(), 1);
    }

    #[derive(Debug, Clone)]
    struct RecordedCommand {
        args: Vec<String>,
    }

    struct RecordingRunner {
        commands: RefCell<Vec<RecordedCommand>>,
    }

    impl RecordingRunner {
        fn successful() -> Self {
            Self {
                commands: RefCell::new(Vec::new()),
            }
        }
    }

    impl CommandRunner for RecordingRunner {
        fn run(&self, _program: &Path, args: &[String]) -> Result<CommandOutcome, String> {
            self.commands.borrow_mut().push(RecordedCommand {
                args: args.to_vec(),
            });
            Ok(CommandOutcome {
                code: Some(0),
                stdout: "ok".to_string(),
                stderr: String::new(),
            })
        }
    }

    fn test_paths(root: &Path) -> ConfigPaths {
        ConfigPaths::new(root.join("codex-home"), root.join("repo-root"))
    }

    fn temp_root(name: &str) -> PathBuf {
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "codexhub-cli-{name}-{}-{suffix}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&path);
        fs::create_dir_all(&path).unwrap();
        path
    }
}
