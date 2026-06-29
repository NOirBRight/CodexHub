use crate::{autostart, catalog, config, history, models, proxy};
use serde::Serialize;

pub fn run(args: &[String]) -> i32 {
    match args.first().map(String::as_str) {
        Some("status") => print_result(proxy::status()),
        Some("switch") => match args.get(1).map(String::as_str) {
            Some(mode @ ("official" | "custom")) => print_result(config::switch_mode(mode)),
            _ => {
                eprintln!("usage: codexhub switch <official|custom>");
                2
            }
        },
        Some("start") => print_result(proxy::start()),
        Some("stop") => print_result(proxy::stop()),
        Some("restart") => print_result(proxy::restart()),
        Some("refresh-models") => print_result(models::refresh_official_models()),
        Some("sync-history") => print_result(history::sync_history(None)),
        Some("sync-catalog") => print_result(catalog::sync_catalog()),
        Some("list-providers") => print_result(config::get_providers()),
        Some("list-models") => print_result(models::list_models()),
        Some("set-autostart") => {
            let enabled = args.get(1).map(String::as_str).unwrap_or("true") != "false";
            print_result(autostart::set_autostart(enabled))
        }
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
    println!(
        "\
CodexHub CLI

Usage:
  codexhub app
  codexhub status
  codexhub switch <official|custom>
  codexhub start
  codexhub stop
  codexhub restart
  codexhub refresh-models
  codexhub sync-history
  codexhub sync-catalog
  codexhub list-providers
  codexhub list-models
  codexhub set-autostart [true|false]
  codexhub remove-autostart"
    );
}

#[cfg(test)]
mod tests {
    use super::run;

    #[test]
    fn help_command_succeeds() {
        let args = vec!["help".to_string()];

        assert_eq!(run(&args), 0);
    }

    #[test]
    fn unknown_command_returns_usage_error() {
        let args = vec!["nope".to_string()];

        assert_eq!(run(&args), 2);
    }
}
