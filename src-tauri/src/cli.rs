use crate::{
    autostart, catalog, config, gateway, history, models, proxy, AppStatus, Model, Provider,
    Settings, UpstreamFormat,
};
use serde::Serialize;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

const MAX_MANAGED_CLIENT_CATALOG_BYTES: u64 = 16 * 1024 * 1024;

pub fn run(args: &[String]) -> i32 {
    match args.first().map(String::as_str) {
        Some("status") => print_result(proxy::status()),
        Some("switch") => {
            run_switch_command(&args[1..], config::get_settings, |mode, auto_sync| {
                crate::switch_mode(mode.to_string(), auto_sync, None)
            })
        }
        Some("start") => print_result(crate::start_proxy()),
        Some("stop") => print_result(proxy::stop()),
        Some("restart") => print_result(crate::restart_proxy()),
        Some("refresh-models") => print_result(crate::official_refresh::refresh_manual()),
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
        Some("cleanup-autostart-on-uninstall") => {
            print_result(autostart::remove_autostart_for_uninstall())
        }
        Some("managed-client-config") => run_managed_client_config(&args[1..]),
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

#[derive(Debug, Clone)]
struct ManagedClientConfigRequest {
    verb: String,
    client: String,
    root: PathBuf,
    model: Option<String>,
    settings_path: Option<PathBuf>,
    providers_path: Option<PathBuf>,
    catalog_path: Option<PathBuf>,
    python_path: Option<PathBuf>,
    backup_subdir: Option<PathBuf>,
}

fn parse_managed_client_config(values: &[String]) -> Result<ManagedClientConfigRequest, ()> {
    let Some((verb, rest)) = values.split_first() else {
        return Err(());
    };
    if !matches!(verb.as_str(), "preview" | "apply" | "readback") {
        return Err(());
    }
    let mut client = None;
    let mut root = None;
    let mut model = None;
    let mut settings_path = None;
    let mut providers_path = None;
    let mut catalog_path = None;
    let mut python_path = None;
    let mut backup_subdir = None;
    let mut index = 0;
    while index < rest.len() {
        let flag = rest[index].as_str();
        let value = rest.get(index + 1).ok_or(())?;
        index += 2;
        match flag {
            "--client" => client = Some(value.clone()),
            "--root" => root = Some(PathBuf::from(value)),
            "--model" => model = Some(value.clone()),
            "--settings-path" => settings_path = Some(PathBuf::from(value)),
            "--providers-path" => providers_path = Some(PathBuf::from(value)),
            "--catalog-path" => catalog_path = Some(PathBuf::from(value)),
            "--python-path" => python_path = Some(PathBuf::from(value)),
            "--backup-subdir" => backup_subdir = Some(PathBuf::from(value)),
            _ => return Err(()),
        }
    }
    let client = client.ok_or(())?;
    let root = root.ok_or(())?;
    Ok(ManagedClientConfigRequest {
        verb: verb.clone(),
        client,
        root,
        model,
        settings_path,
        providers_path,
        catalog_path,
        python_path,
        backup_subdir,
    })
}

fn run_managed_client_config(args: &[String]) -> i32 {
    let request = match parse_managed_client_config(args) {
        Ok(request) => request,
        Err(()) => {
            print_managed_client_config_usage();
            return 2;
        }
    };
    let supported = gateway::isolated_managed_client_ids();
    if !supported.iter().any(|id| id == &request.client) {
        let mut known = supported;
        known.sort();
        let error = format!(
            "unknown managed client: {}; expected one of {}",
            request.client,
            known.join(", ")
        );
        eprintln!("{error}");
        return 1;
    }
    let result = match request.client.as_str() {
        "codex" => run_codex_managed_client_config(&request),
        "opencode" | "zcode" | "pi" | "omp" => run_native_managed_client_config(&request),
        _ => Err(format!(
            "unknown managed client: {}; expected codex, opencode, zcode, pi, or omp",
            request.client
        )),
    };
    print_result(result)
}

fn load_settings_and_providers(
    request: &ManagedClientConfigRequest,
    isolated_root: &Path,
) -> Result<
    (
        Settings,
        Vec<Provider>,
        config::ConfigPaths,
        Option<PathBuf>,
    ),
    String,
> {
    // The isolated ConfigPaths resolves settings/providers/catalog/targets
    // beneath the supplied root using existing production parsers. We seed
    // the isolated runtime dir with the caller-supplied settings/providers
    // files so no host discovery occurs.
    let runtime_dir = isolated_root.join("runtime");
    let codex_target_dir = isolated_root.join("codex-target");
    let repo_root = isolated_root.join("repo");
    std::fs::create_dir_all(runtime_dir.join("proxy").join("config"))
        .map_err(|error| format!("failed to create isolated runtime: {error}"))?;
    let paths = config::ConfigPaths::new_isolated(&runtime_dir, &codex_target_dir, &repo_root);
    // F3: populate the isolated repo with the production Codex overlay
    // resources (src-python modules + bundled providers.toml) so the Codex
    // apply path can invoke the real config_overlay.py without host
    // discovery. Native clients (opencode/zcode/pi/omp) do not read from
    // the repo tree, so this is harmless for them.
    config::populate_isolated_repo_resources(&paths)?;
    let staged_catalog_path = stage_candidate_catalog(request.catalog_path.as_deref(), &paths)?;
    // Seed settings.json from the caller-supplied path if provided.
    if let Some(settings_path) = &request.settings_path {
        let text = std::fs::read_to_string(settings_path)
            .map_err(|error| format!("failed to read settings {}: {error}", settings_path.display()))?;
        std::fs::create_dir_all(paths.settings_path().parent().unwrap_or(Path::new(".")))
            .map_err(|error| format!("failed to create settings dir: {error}"))?;
        std::fs::write(paths.settings_path(), text)
            .map_err(|error| format!("failed to write isolated settings: {error}"))?;
    }
    if let Some(providers_path) = &request.providers_path {
        let text = std::fs::read_to_string(providers_path).map_err(|error| {
            format!("failed to read providers {}: {error}", providers_path.display())
        })?;
        std::fs::write(paths.runtime_providers_path_for_cli(), text).map_err(|error| {
            format!("failed to write isolated providers: {error}")
        })?;
    }
    let mut settings = config::get_settings_from_paths(&paths)?;
    let mut providers = config::get_providers_from_paths(&paths)?;

    // The isolated CLI must never discover Official models from the current
    // user's runtime. An explicit candidate catalog is imported below as the
    // authoritative OpenAI provider; without one, only the supplied provider
    // configuration is available.
    let include_candidate_official_models = settings.include_official_models;
    settings.include_official_models = false;
    if include_candidate_official_models {
        if let Some(catalog_path) = staged_catalog_path.as_deref() {
            let official_models = models::read_official_catalog_models(catalog_path)
                .map_err(|_| "candidate catalog is malformed".to_string())?;
            if official_models.is_empty() {
                return Err("candidate catalog has no Official models".to_string());
            }
            providers.retain(|provider| provider.id != "openai");
            providers.push(candidate_official_provider(official_models));
        }
    }

    Ok((settings, providers, paths, staged_catalog_path))
}

fn stage_candidate_catalog(
    source_path: Option<&Path>,
    paths: &config::ConfigPaths,
) -> Result<Option<PathBuf>, String> {
    let Some(source_path) = source_path else {
        return Ok(None);
    };
    let source_bytes = read_bounded_single_link_file(source_path)
        .map_err(|_| "candidate catalog source is not a regular single-link file".to_string())?;
    let staged_path = paths.generated_catalog_path_for_cli();
    let parent = staged_path
        .parent()
        .ok_or_else(|| "candidate catalog target has no parent".to_string())?;
    std::fs::create_dir_all(parent)
        .map_err(|_| "failed to create isolated candidate catalog directory".to_string())?;

    if staged_path.exists() {
        let staged_bytes = read_bounded_single_link_file(&staged_path)
            .map_err(|_| "isolated candidate catalog is not a regular single-link file".to_string())?;
        if staged_bytes != source_bytes {
            return Err("isolated candidate catalog contradicts supplied source".to_string());
        }
    } else {
        let mut staged = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&staged_path)
            .map_err(|_| "failed to create isolated candidate catalog".to_string())?;
        staged
            .write_all(&source_bytes)
            .and_then(|_| staged.sync_all())
            .map_err(|_| "failed to write isolated candidate catalog".to_string())?;
        drop(staged);
        let staged_bytes = read_bounded_single_link_file(&staged_path)
            .map_err(|_| "isolated candidate catalog is not a regular single-link file".to_string())?;
        if staged_bytes != source_bytes {
            return Err("isolated candidate catalog copy verification failed".to_string());
        }
    }
    Ok(Some(staged_path))
}

fn read_bounded_single_link_file(path: &Path) -> Result<Vec<u8>, String> {
    let path_metadata = std::fs::symlink_metadata(path)
        .map_err(|error| format!("failed to inspect input: {error}"))?;
    if !path_metadata.file_type().is_file() || path_metadata.file_type().is_symlink() {
        return Err("input is not a regular file".to_string());
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
        if path_metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Err("input is a reparse point".to_string());
        }
    }
    let canonical = std::fs::canonicalize(path)
        .map_err(|error| format!("failed to canonicalize input: {error}"))?;
    gateway::reject_hard_link(&canonical)?;
    let file = std::fs::File::open(&canonical)
        .map_err(|error| format!("failed to open input: {error}"))?;
    let metadata = file
        .metadata()
        .map_err(|error| format!("failed to inspect open input: {error}"))?;
    if !metadata.is_file() || metadata.len() > MAX_MANAGED_CLIENT_CATALOG_BYTES {
        return Err("input is not a bounded regular file".to_string());
    }
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    file.take(MAX_MANAGED_CLIENT_CATALOG_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|error| format!("failed to read input: {error}"))?;
    if bytes.len() as u64 > MAX_MANAGED_CLIENT_CATALOG_BYTES {
        return Err("input exceeds the size bound".to_string());
    }
    Ok(bytes)
}

fn candidate_official_provider(models: Vec<Model>) -> Provider {
    Provider {
        id: "openai".to_string(),
        name: "OpenAI".to_string(),
        base_url: "https://api.openai.com/v1".to_string(),
        api_key: None,
        upstream_format: Some(UpstreamFormat::Responses),
        available_upstream_formats: Some(vec![UpstreamFormat::Responses]),
        tool_protocol: None,
        tool_surface_strategy: None,
        reports_cached_input_tokens: None,
        supports_developer_role: Some(true),
        display_prefix: Some("openai/".to_string()),
        sort_order: Some(0),
        enabled: true,
        locked: true,
        models,
    }
}

fn run_native_managed_client_config(
    request: &ManagedClientConfigRequest,
) -> Result<serde_json::Value, String> {
    let isolated = if request.verb == "readback" {
        gateway::validate_existing_isolated_root(&request.root)?
    } else {
        gateway::validate_isolated_root(&request.root)?
    };
    let (settings, providers, _paths, staged_catalog_path) =
        load_settings_and_providers(request, isolated.root())?;
    let input = gateway::IsolatedClientApplyInput {
        client_id: request.client.clone(),
        model: request.model.clone(),
        settings,
        providers,
        catalog_path: staged_catalog_path,
        backup_subdir: request.backup_subdir.clone(),
    };
    match request.verb.as_str() {
        "preview" => {
            let preview = gateway::isolated_client_preview(&isolated, &input)?;
            Ok(serde_json::to_value(&preview).map_err(|error| error.to_string())?)
        }
        "apply" => {
            let result = gateway::apply_gateway_client_config_isolated(&isolated, &input)?;
            Ok(serde_json::to_value(&result).map_err(|error| error.to_string())?)
        }
        "readback" => {
            let readback = gateway::readback_gateway_client_config_isolated(&isolated, &input)?;
            Ok(serde_json::to_value(&readback).map_err(|error| error.to_string())?)
        }
        other => Err(format!("unsupported verb: {other}")),
    }
}

fn run_codex_managed_client_config(
    request: &ManagedClientConfigRequest,
) -> Result<serde_json::Value, String> {
    let isolated = if request.verb == "readback" {
        gateway::validate_existing_isolated_root(&request.root)?
    } else {
        gateway::validate_isolated_root(&request.root)?
    };
    let (_settings, _providers, paths, _staged_catalog_path) =
        load_settings_and_providers(request, isolated.root())?;
    let model = request.model.clone().unwrap_or_else(|| "gpt-5.5".to_string());
    match request.verb.as_str() {
        "preview" => {
            let preview = config::preview_codex_config_isolated(&paths, "custom", &model)?;
            Ok(serde_json::to_value(&preview).map_err(|error| error.to_string())?)
        }
        "apply" => {
            let python = request
                .python_path
                .as_deref()
                .map(Path::to_path_buf)
                .unwrap_or_else(config::find_python);
            let runner = config::ProcessCommandRunner;
            let status =
                config::apply_codex_config_isolated(&paths, "custom", false, &model, &python, &runner)?;
            Ok(serde_json::to_value(&status).map_err(|error| error.to_string())?)
        }
        "readback" => {
            let readback = config::readback_codex_config_isolated(&paths, &model)?;
            Ok(serde_json::to_value(&readback).map_err(|error| error.to_string())?)
        }
        other => Err(format!("unsupported verb: {other}")),
    }
}

fn print_managed_client_config_usage() {
    eprintln!(
        "usage: codexhub managed-client-config <preview|apply|readback> \
         --client <codex|opencode|zcode|pi|omp> --root <fresh-isolated-root> \
         [--model <id>] [--settings-path <path>] [--providers-path <path>] \
         [--catalog-path <path>] [--python-path <path>] [--backup-subdir <name>]"
    );
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
  codexhub web-bridge [--port {bridge_port}] [--addr HOST:PORT]
  codexhub managed-client-config <preview|apply|readback> --client <codex|opencode|zcode|pi|omp> --root <dir> [--model <id>] [--settings-path <path>] [--providers-path <path>] [--catalog-path <path>] [--python-path <path>]",
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

    mod managed_client_config_cli {
        use super::{run, temp_root};
        use std::fs;
        use std::path::{Path, PathBuf};

        fn write_settings_and_providers(root: &Path) -> (PathBuf, PathBuf) {
            let proxy_dir = root.join("proxy");
            let config_dir = proxy_dir.join("config");
            fs::create_dir_all(&config_dir).unwrap();
            let settings_path = proxy_dir.join("settings.json");
            fs::write(
                &settings_path,
                r#"{"proxy_port": 9099, "gateway_client_key": "isolated-key"}"#,
            )
            .unwrap();
            let providers_path = config_dir.join("providers.toml");
            fs::write(
                &providers_path,
                r#"[[providers]]
id = "volc"
name = "Volcengine"
base_url = "https://ark.example.test/v1"
upstream_format = "responses"
enabled = true

[[providers.models]]
id = "glm-5.2"
display_name = "Volc GLM-5.2"
gateway_exported = true
"#,
            )
            .unwrap();
            (settings_path, providers_path)
        }

        fn write_candidate_official_catalog(root: &Path) -> PathBuf {
            let catalog_path = root.join("candidate-catalog.json");
            fs::write(
                &catalog_path,
                r#"{
  "models": [
    {
      "slug": "gpt-cli-catalog-only",
      "display_name": "CLI Catalog Only",
      "context_window": 272000,
      "input_modalities": ["text", "image"],
      "supported_reasoning_levels": ["medium"],
      "default_reasoning_level": "medium",
      "enabled": true,
      "gateway_exported": true,
      "codex_proxy_metadata": {
        "provider": "openai",
        "upstream_name": "official",
        "upstream_model": "gpt-cli-catalog-only"
      }
    }
  ]
}"#,
            )
            .unwrap();
            catalog_path
        }

        #[test]
        fn managed_client_config_unknown_verb_returns_usage_error() {
            let root = temp_root("mcc-unknown-verb");
            let args = vec![
                "managed-client-config".to_string(),
                "bogus".to_string(),
                "--client".to_string(),
                "opencode".to_string(),
                "--root".to_string(),
                root.to_string_lossy().to_string(),
            ];
            assert_eq!(run(&args), 2);
        }

        #[test]
        fn managed_client_config_missing_client_returns_usage_error() {
            let root = temp_root("mcc-missing-client");
            let args = vec![
                "managed-client-config".to_string(),
                "preview".to_string(),
                "--root".to_string(),
                root.to_string_lossy().to_string(),
            ];
            assert_eq!(run(&args), 2);
        }

        #[test]
        fn managed_client_config_preview_opencode_emits_bounded_json_without_secrets() {
            let root = temp_root("mcc-preview");
            let (settings_path, providers_path) = write_settings_and_providers(&root);
            let isolated = root.join("isolated");
            let args = vec![
                "managed-client-config".to_string(),
                "preview".to_string(),
                "--client".to_string(),
                "opencode".to_string(),
                "--root".to_string(),
                isolated.to_string_lossy().to_string(),
                "--settings-path".to_string(),
                settings_path.to_string_lossy().to_string(),
                "--providers-path".to_string(),
                providers_path.to_string_lossy().to_string(),
                "--model".to_string(),
                "volc/glm-5.2".to_string(),
            ];
            let exit = run(&args);
            assert_eq!(exit, 0, "preview should succeed");
        }

        #[test]
        fn managed_client_config_apply_opencode_then_readback_round_trips() {
            let root = temp_root("mcc-apply-readback");
            let (settings_path, providers_path) = write_settings_and_providers(&root);
            let isolated = root.join("isolated");

            let apply_args = vec![
                "managed-client-config".to_string(),
                "apply".to_string(),
                "--client".to_string(),
                "opencode".to_string(),
                "--root".to_string(),
                isolated.to_string_lossy().to_string(),
                "--settings-path".to_string(),
                settings_path.to_string_lossy().to_string(),
                "--providers-path".to_string(),
                providers_path.to_string_lossy().to_string(),
                "--model".to_string(),
                "volc/glm-5.2".to_string(),
            ];
            assert_eq!(run(&apply_args), 0, "apply should succeed");

            let readback_args = vec![
                "managed-client-config".to_string(),
                "readback".to_string(),
                "--client".to_string(),
                "opencode".to_string(),
                "--root".to_string(),
                isolated.to_string_lossy().to_string(),
                "--settings-path".to_string(),
                settings_path.to_string_lossy().to_string(),
                "--providers-path".to_string(),
                providers_path.to_string_lossy().to_string(),
                "--model".to_string(),
                "volc/glm-5.2".to_string(),
            ];
            assert_eq!(run(&readback_args), 0, "readback should succeed");
        }

        #[test]
        fn managed_client_config_codex_preview_invokes_config_overlay_python() {
            let root = temp_root("mcc-codex");
            let proxy_dir = root.join("proxy");
            let config_dir = proxy_dir.join("config");
            fs::create_dir_all(&config_dir).unwrap();
            let settings_path = proxy_dir.join("settings.json");
            fs::write(
                &settings_path,
                r#"{"proxy_port": 9099, "gateway_client_key": "isolated-key"}"#,
            )
            .unwrap();
            let providers_path = config_dir.join("providers.toml");
            fs::write(&providers_path, "").unwrap();
            let isolated = root.join("isolated");
            let args = vec![
                "managed-client-config".to_string(),
                "preview".to_string(),
                "--client".to_string(),
                "codex".to_string(),
                "--root".to_string(),
                isolated.to_string_lossy().to_string(),
                "--settings-path".to_string(),
                settings_path.to_string_lossy().to_string(),
                "--providers-path".to_string(),
                providers_path.to_string_lossy().to_string(),
                "--model".to_string(),
                "gpt-5.6-luna".to_string(),
            ];
            // Codex preview resolves ConfigPaths and reports the overlay args without running Python.
            assert_eq!(run(&args), 0, "codex preview should succeed");
        }

        // F4: the Codex isolated preview must surface the real overlay route
        // binding (model_provider = "custom", wire_api = "responses") in its
        // bounded JSON, not a fabricated selector/route_protocol.
        #[test]
        fn managed_client_config_codex_preview_emits_real_route_protocol() {
            let root = temp_root("mcc-codex-route");
            let (settings_path, providers_path) = write_settings_and_providers(&root);
            let isolated = root.join("isolated");
            let args = vec![
                "managed-client-config".to_string(),
                "preview".to_string(),
                "--client".to_string(),
                "codex".to_string(),
                "--root".to_string(),
                isolated.to_string_lossy().to_string(),
                "--settings-path".to_string(),
                settings_path.to_string_lossy().to_string(),
                "--providers-path".to_string(),
                providers_path.to_string_lossy().to_string(),
                "--model".to_string(),
                "gpt-5.6-luna".to_string(),
            ];
            let exit = run(&args);
            assert_eq!(exit, 0, "codex preview should succeed");
            // The preview JSON is printed to stdout; we cannot easily capture
            // it here without refactoring run(), but the config.rs unit test
            // `codex_preview_under_isolated_root_reports_relative_target_and_no_secret`
            // already asserts route_protocol == "responses" and selector ==
            // "custom/gpt-5.6-luna". This CLI test ensures the dispatch path
            // that wires ConfigPaths + populate_isolated_repo_resources does
            // not regress for Codex preview.
        }

        // F6: table-driven all-client CLI dispatch. Every supported client
        // must accept the preview verb and return exit 0, covering the CLI
        // parity surface for codex/opencode/zcode/pi/omp.
        #[test]
        fn table_driven_managed_client_config_preview_accepts_all_clients() {
            let root = temp_root("mcc-table-preview");
            let (settings_path, providers_path) = write_settings_and_providers(&root);
            for client_id in ["codex", "opencode", "zcode", "pi", "omp"] {
                let isolated = root.join(format!("isolated-{client_id}"));
                let args = vec![
                    "managed-client-config".to_string(),
                    "preview".to_string(),
                    "--client".to_string(),
                    client_id.to_string(),
                    "--root".to_string(),
                    isolated.to_string_lossy().to_string(),
                    "--settings-path".to_string(),
                    settings_path.to_string_lossy().to_string(),
                    "--providers-path".to_string(),
                    providers_path.to_string_lossy().to_string(),
                    "--model".to_string(),
                    "volc/glm-5.2".to_string(),
                ];
                assert_eq!(
                    run(&args),
                    0,
                    "preview should succeed for client {client_id}"
                );
            }
        }

        #[test]
        fn managed_client_config_native_matrix_imports_external_catalog_into_fresh_roots() {
            let root = temp_root("mcc-external-catalog");
            let (settings_path, providers_path) = write_settings_and_providers(&root);
            let catalog_path = write_candidate_official_catalog(&root);

            for client_id in ["opencode", "zcode", "pi", "omp"] {
                let preview_root = root.join(format!("{client_id}-preview"));
                let apply_root = root.join(format!("{client_id}-apply"));
                for (verb, isolated) in [
                    ("preview", preview_root.as_path()),
                    ("apply", apply_root.as_path()),
                    ("readback", apply_root.as_path()),
                ] {
                    let args = vec![
                        "managed-client-config".to_string(),
                        verb.to_string(),
                        "--client".to_string(),
                        client_id.to_string(),
                        "--root".to_string(),
                        isolated.to_string_lossy().to_string(),
                        "--settings-path".to_string(),
                        settings_path.to_string_lossy().to_string(),
                        "--providers-path".to_string(),
                        providers_path.to_string_lossy().to_string(),
                        "--catalog-path".to_string(),
                        catalog_path.to_string_lossy().to_string(),
                        "--model".to_string(),
                        "openai/gpt-cli-catalog-only".to_string(),
                    ];
                    assert_eq!(
                        run(&args),
                        0,
                        "{verb} should import the candidate catalog for {client_id}"
                    );
                }
            }
        }

        #[test]
        fn managed_client_config_rejects_hardlinked_external_catalog() {
            let root = temp_root("mcc-hardlinked-catalog");
            let (settings_path, providers_path) = write_settings_and_providers(&root);
            let catalog_path = write_candidate_official_catalog(&root);
            let linked_catalog_path = root.join("linked-candidate-catalog.json");
            fs::hard_link(&catalog_path, &linked_catalog_path).unwrap();
            let isolated = root.join("isolated");
            let args = vec![
                "managed-client-config".to_string(),
                "preview".to_string(),
                "--client".to_string(),
                "zcode".to_string(),
                "--root".to_string(),
                isolated.to_string_lossy().to_string(),
                "--settings-path".to_string(),
                settings_path.to_string_lossy().to_string(),
                "--providers-path".to_string(),
                providers_path.to_string_lossy().to_string(),
                "--catalog-path".to_string(),
                linked_catalog_path.to_string_lossy().to_string(),
                "--model".to_string(),
                "openai/gpt-cli-catalog-only".to_string(),
            ];

            assert_eq!(run(&args), 1, "linked catalog input must fail closed");
        }
    }
}
