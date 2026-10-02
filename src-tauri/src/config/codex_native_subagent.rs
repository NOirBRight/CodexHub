use super::{ConfigPaths, ProcessCommandRunner};
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct CodexNativeSubagentOption {
    pub id: String,
    pub label: String,
    pub efforts: Vec<String>,
    #[serde(rename = "defaultEffort")]
    pub default_effort: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct CodexNativeSubagent {
    pub model: String,
    pub effort: String,
    pub models: Vec<CodexNativeSubagentOption>,
    pub native_catalog: bool,
}

fn baseline_paths(paths: &ConfigPaths) -> Vec<PathBuf> {
    let current = crate::app_flavor::current().routing_owner();
    [
        crate::app_flavor::RoutingOwner::Release,
        crate::app_flavor::RoutingOwner::Beta,
    ]
    .into_iter()
    .map(|owner| paths.config_backup_path_for_target_owner(current, owner))
    .collect()
}

fn native_subagent_command(
    paths: &ConfigPaths,
    selection: Option<(&str, &str)>,
) -> Result<CodexNativeSubagent, String> {
    let mut args = vec![
        if selection.is_some() {
            "save-native-subagent"
        } else {
            "inspect-native-subagent"
        }
        .to_string(),
        "--config".to_string(),
        paths.codex_config_path().to_string_lossy().into_owned(),
    ];
    for backup in baseline_paths(paths) {
        args.extend([
            "--backup".to_string(),
            backup.to_string_lossy().into_owned(),
        ]);
    }
    if let Some((model, effort)) = selection {
        args.extend([
            "--model".to_string(),
            model.to_string(),
            "--effort".to_string(),
            effort.to_string(),
        ]);
    }
    let outcome = super::run_python_script(
        "Codex native Default subagent",
        &super::find_python()?,
        paths.config_overlay_script(),
        args,
        &ProcessCommandRunner,
    )?;
    serde_json::from_str(&outcome.stdout)
        .map_err(|error| format!("invalid Codex native Default subagent readback: {error}"))
}

fn with_official_options(mut state: CodexNativeSubagent) -> Result<CodexNativeSubagent, String> {
    // A user catalog is authoritative for that native client. Otherwise reuse
    // native subscription discovery, which does not filter by Gateway export.
    if !state.native_catalog {
        for model in crate::models::list_official_models()? {
            state.models.push(CodexNativeSubagentOption {
                id: model.id.clone(),
                label: model.display_name.unwrap_or(model.id),
                efforts: model.supported_reasoning_levels.unwrap_or_default(),
                default_effort: model
                    .default_reasoning_level
                    .unwrap_or_default(),
            });
        }
    }
    Ok(state)
}

pub fn get_codex_native_subagent() -> Result<CodexNativeSubagent, String> {
    crate::codex_desktop::serialize_config_writer(|| {
        with_official_options(native_subagent_command(&ConfigPaths::runtime()?, None)?)
    })
}

pub fn save_codex_native_subagent(
    model: String,
    effort: String,
) -> Result<CodexNativeSubagent, String> {
    crate::codex_desktop::serialize_config_writer(|| {
        save_with_paths(&ConfigPaths::runtime()?, &model, &effort)
    })
}

fn save_with_paths(
    paths: &ConfigPaths,
    model: &str,
    effort: &str,
) -> Result<CodexNativeSubagent, String> {
    let mut targets = baseline_paths(paths);
    targets.extend([paths.codex_config_path(), paths.settings_path()]);
    crate::file_transaction::with_text_file_rollback(&targets, || {
        let state = native_subagent_command(paths, Some((model, effort)))?;
        clear_gateway_pin(paths)?;
        with_official_options(state)
    })
    .map_err(|error| error.to_string())
}

fn clear_gateway_pin(paths: &ConfigPaths) -> Result<(), String> {
    let path = paths.settings_path();
    if !path.exists() {
        return Ok(());
    }
    let text = fs::read_to_string(&path).map_err(|error| error.to_string())?;
    let mut settings: serde_json::Value =
        serde_json::from_str(&text).map_err(|error| error.to_string())?;
    let object = settings.as_object_mut().ok_or("invalid settings JSON")?;
    object.insert("codex_default_subagent_model".to_string(), "".into());
    object.insert(
        "codex_default_subagent_reasoning_effort".to_string(),
        "".into(),
    );
    crate::safe_file::write_text_atomic(
        &path,
        &format!(
            "{}\n",
            serde_json::to_string_pretty(&settings).map_err(|error| error.to_string())?
        ),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_save_readback_and_failed_preference_update_are_transactional() {
        let root =
            std::env::temp_dir().join(format!("codex-native-transaction-{}", std::process::id()));
        let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .to_path_buf();
        let paths = ConfigPaths::new(&root, repo);
        fs::create_dir_all(paths.proxy_dir()).unwrap();
        let catalog = root.join("native.json");
        fs::write(
            &catalog,
            r#"{"models":[{"slug":"native-only","supported_reasoning_levels":["high"]}]}"#,
        )
        .unwrap();
        let original = format!(
            "model = 'parent'\nmodel_catalog_json = {}\n[agents]\nmax_depth = 3\n",
            serde_json::to_string(&catalog.to_string_lossy()).unwrap()
        );
        fs::write(paths.codex_config_path(), &original).unwrap();
        let backup = paths.config_backup_path();
        fs::write(&backup, &original).unwrap();
        fs::write(
            paths.settings_path(),
            r#"{"codex_default_subagent_model":"stale/model","unknown":true}"#,
        )
        .unwrap();
        let saved = save_with_paths(&paths, "native-only", "high").unwrap();
        assert_eq!(saved.model, "native-only");
        assert_eq!(saved.effort, "high");
        assert_eq!(saved.models[0].id, "native-only");
        let readback = native_subagent_command(&paths, None).unwrap();
        assert_eq!(readback.model, saved.model);
        let baseline: toml::Value = toml::from_str(&fs::read_to_string(&backup).unwrap()).unwrap();
        assert_eq!(
            baseline["agents"]["default_subagent_model"].as_str(),
            Some("native-only")
        );
        assert_eq!(baseline["model"].as_str(), Some("parent"));
        let before_config = fs::read_to_string(paths.codex_config_path()).unwrap();
        let before_backup = fs::read_to_string(&backup).unwrap();
        fs::write(paths.settings_path(), "invalid-json").unwrap();
        assert!(save_with_paths(&paths, "replacement", "low").is_err());
        assert_eq!(
            fs::read_to_string(paths.codex_config_path()).unwrap(),
            before_config
        );
        assert_eq!(fs::read_to_string(&backup).unwrap(), before_backup);
        assert_eq!(
            fs::read_to_string(paths.settings_path()).unwrap(),
            "invalid-json"
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn native_save_clears_stale_gateway_pin_without_normalizing_other_settings() {
        let root = std::env::temp_dir().join(format!("codex-native-pin-{}", std::process::id()));
        let paths = ConfigPaths::new(&root, &root);
        fs::create_dir_all(paths.proxy_dir()).unwrap();
        fs::write(paths.settings_path(), r#"{"codex_default_subagent_model":"stale/model","codex_default_subagent_reasoning_effort":"max","include_official_models":false,"unknown_user_field":{"keep":true}}"#).unwrap();
        clear_gateway_pin(&paths).unwrap();
        let value: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(paths.settings_path()).unwrap()).unwrap();
        assert_eq!(value["codex_default_subagent_model"], "");
        assert_eq!(value["codex_default_subagent_reasoning_effort"], "");
        assert_eq!(value["include_official_models"], false);
        assert_eq!(value["unknown_user_field"]["keep"], true);
        fs::remove_dir_all(root).unwrap();
    }
}
