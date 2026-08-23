use super::super::{
    gateway_client_provider_groups, is_any_top_level_yaml_key, is_top_level_yaml_key, yaml_scalar,
};
use crate::{Provider, Settings};

pub(in crate::gateway) fn omp_config_text(
    current: Option<&str>,
    selector: &str,
    vision_selector: Option<&str>,
    default_reasoning_effort: Option<&str>,
) -> String {
    let default_selector = match default_reasoning_effort {
        Some(effort) if !effort.is_empty() => format!("{selector}:{effort}"),
        _ => selector.to_string(),
    };
    let mut block = vec![
        "modelRoles:".to_string(),
        format!("  default: {default_selector}"),
    ];
    if let Some(vision_selector) = vision_selector {
        block.push(format!("  vision: {vision_selector}"));
    }
    let mut output = Vec::new();
    let mut inserted = false;
    let lines = current.unwrap_or_default().lines().collect::<Vec<_>>();
    let mut index = 0;
    while index < lines.len() {
        let line = lines[index];
        if is_top_level_yaml_key(line, "modelRoles") {
            output.extend(block.iter().cloned());
            inserted = true;
            index += 1;
            while index < lines.len() && !is_any_top_level_yaml_key(lines[index]) {
                index += 1;
            }
            continue;
        }
        output.push(line.to_string());
        index += 1;
    }
    if !inserted {
        if !output.is_empty() && output.last().is_some_and(|line| !line.trim().is_empty()) {
            output.push(String::new());
        }
        output.extend(block);
    }
    format!("{}\n", output.join("\n"))
}

pub(in crate::gateway) fn omp_models_yml_text(
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<String, String> {
    let groups = gateway_client_provider_groups(settings, providers, model)?;
    let api_key = yaml_scalar(&settings.gateway_client_key);
    let mut output = "providers:\n".to_string();
    for group in &groups.providers {
        let base_url = yaml_scalar(&group.base_url);
        let api = group.endpoint_selection.pi_api();
        let supports_developer_role = group.supports_developer_role;
        output.push_str(&format!(
            "  {}:\n    baseUrl: {base_url}\n    api: {api}\n    apiKey: {api_key}\n    authHeader: true\n    compat:\n      supportsDeveloperRole: {supports_developer_role}\n      supportsReasoningEffort: true\n      supportsUsageInStreaming: true\n    models:\n",
            group.client_provider_id
        ));
        for gateway_model in &group.models {
            let model_id = yaml_scalar(&gateway_model.id);
            let model_name = yaml_scalar(&gateway_model.display_name);
            let reasoning = !gateway_model.supported_reasoning_levels.is_empty();
            let input_list = gateway_model
                .input_modalities
                .iter()
                .map(|modality| format!("          - {modality}\n"))
                .collect::<String>();
            let context_window = gateway_model
                .context_window
                .map(|value| format!("        contextWindow: {value}\n"))
                .unwrap_or_default();
            output.push_str(&format!(
            "      - id: {model_id}\n        name: {model_name}\n        reasoning: {reasoning}\n        input:\n{input_list}        headers:\n          x-codex-client-id: omp\n{context_window}        maxTokens: 32768\n        cost:\n          input: 0\n          output: 0\n          cacheRead: 0\n          cacheWrite: 0\n"
        ));
        }
    }
    Ok(output)
}

use super::super::{
    combined_current_preview, combined_named_text, create_snapshot_backup,
    gateway_client_model_selector, gateway_exported_model_default_reasoning_effort,
    gateway_exported_model_supports_image, is_codexhub_client_model_selector,
    is_codexhub_client_provider_id, is_local_gateway_url, resolve_gateway_client_model_id,
    restore_latest_snapshot_backup, route_owner_from_endpoint, sanitize_text, write_text_replace,
    GatewayClientApplyResult, GatewayClientConfigPreview,
};
use crate::app_flavor::RoutingOwner;
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone)]
pub(in crate::gateway) struct OmpConfigPaths {
    pub(in crate::gateway) config_path: PathBuf,
    pub(in crate::gateway) models_path: PathBuf,
}

pub(in crate::gateway) fn detect_omp_config_paths() -> OmpConfigPaths {
    let agent_dir = if let Some(path) = std::env::var_os("CODEXHUB_OMP_AGENT_DIR")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
    {
        path
    } else if let Some(path) = std::env::var_os("CODEXHUB_OMP_CONFIG")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
    {
        return OmpConfigPaths {
            models_path: path
                .parent()
                .map(|parent| parent.join("models.yml"))
                .unwrap_or_else(|| PathBuf::from("models.yml")),
            config_path: path,
        };
    } else {
        dirs::home_dir()
            .map(|home| home.join(".omp").join("agent"))
            .unwrap_or_else(|| PathBuf::from("~/.omp/agent"))
    };
    OmpConfigPaths {
        config_path: agent_dir.join("config.yml"),
        models_path: agent_dir.join("models.yml"),
    }
}

pub(in crate::gateway) fn detect_omp_route_details(
    paths: &OmpConfigPaths,
    current_owner: RoutingOwner,
    current_port: u16,
) -> (RoutingOwner, Option<String>) {
    let config_text = fs::read_to_string(&paths.config_path).ok();
    let models_text = fs::read_to_string(&paths.models_path).ok();
    let managed = omp_route_mode(paths) == "hub";
    let route_endpoint = models_text.as_deref().and_then(|text| {
        managed_omp_provider_base_url(text).unwrap_or_else(|| first_omp_provider_base_url(text))
    });
    let existing_config = config_text.is_some() || models_text.is_some();
    (
        route_owner_from_endpoint(
            route_endpoint.as_deref(),
            managed,
            existing_config,
            current_owner,
            current_port,
        ),
        route_endpoint,
    )
}

pub(in crate::gateway) fn first_omp_provider_base_url(text: &str) -> Option<String> {
    text.lines().find_map(|line| {
        line.trim()
            .strip_prefix("baseUrl:")
            .map(str::trim)
            .map(ToOwned::to_owned)
    })
}

pub(in crate::gateway) fn managed_omp_provider_base_url(text: &str) -> Option<Option<String>> {
    let mut current_provider_id: Option<String> = None;
    let mut current_base_url: Option<String> = None;
    let mut current_has_local_gateway_url = false;
    let mut current_has_codexhub_name = false;

    let finalize = |provider_id: &Option<String>,
                    base_url: &Option<String>,
                    has_local_gateway_url: bool,
                    has_codexhub_name: bool|
     -> Option<Option<String>> {
        provider_id
            .as_deref()
            .filter(|provider_id| is_codexhub_client_provider_id(provider_id))
            .filter(|_| has_local_gateway_url || has_codexhub_name)
            .map(|_| base_url.clone())
    };

    for line in text.lines() {
        let starts_provider_entry =
            line.starts_with("  ") && !line.starts_with("    ") && line.trim_end().ends_with(':');
        if starts_provider_entry {
            if let Some(endpoint) = finalize(
                &current_provider_id,
                &current_base_url,
                current_has_local_gateway_url,
                current_has_codexhub_name,
            ) {
                return Some(endpoint);
            }
            current_provider_id = Some(line.trim().trim_end_matches(':').to_string());
            current_base_url = None;
            current_has_local_gateway_url = false;
            current_has_codexhub_name = false;
            continue;
        }

        if current_provider_id.is_some() {
            let trimmed = line.trim();
            if let Some(url) = trimmed.strip_prefix("baseUrl:") {
                let url = url.trim().to_string();
                current_has_local_gateway_url = is_local_gateway_url(&url)
                    && (url.contains("/v1/providers/")
                        || url.trim().trim_end_matches('/').ends_with("/v1"));
                current_base_url = Some(url);
            }
            if trimmed.contains("CodexHub Gateway") || trimmed.contains("CodexHub ") {
                current_has_codexhub_name = true;
            }
        }
    }

    finalize(
        &current_provider_id,
        &current_base_url,
        current_has_local_gateway_url,
        current_has_codexhub_name,
    )
}

pub(in crate::gateway) fn omp_route_mode(paths: &OmpConfigPaths) -> &'static str {
    let config = fs::read_to_string(&paths.config_path).ok();
    let models = fs::read_to_string(&paths.models_path).ok();
    match (config.as_deref(), models.as_deref()) {
        (Some(config), Some(models)) => {
            if is_omp_codexhub_config(config, models) {
                "hub"
            } else {
                "official"
            }
        }
        (Some(config), None) => {
            if is_omp_codexhub_config(config, "") {
                "hub"
            } else {
                "official"
            }
        }
        (None, Some(models)) => {
            if is_omp_models_codexhub_config(models) {
                "hub"
            } else {
                "official"
            }
        }
        (None, None) => "unknown",
    }
}

pub(in crate::gateway) fn preview_omp_config_with_paths(
    config_path: &Path,
    models_path: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<GatewayClientConfigPreview, String> {
    let current =
        combined_current_preview(&[("config.yml", config_path), ("models.yml", models_path)]);
    let current_config = fs::read_to_string(config_path).ok();
    let model = resolve_gateway_client_model_id(settings, providers, model)?;
    let selector = gateway_client_model_selector(settings, providers, &model)?;
    let vision_selector = if gateway_exported_model_supports_image(settings, providers, &model) {
        Some(selector.as_str())
    } else {
        None
    };
    let default_reasoning_effort =
        gateway_exported_model_default_reasoning_effort(settings, providers, &model);
    let next_config = omp_config_text(
        current_config.as_deref(),
        &selector,
        vision_selector,
        default_reasoning_effort.as_deref(),
    );
    let next_models = omp_models_yml_text(settings, providers, &model)?;
    let mut message =
        "Apply will snapshot OMP config/models, then route OMP through CodexHub Gateway."
            .to_string();
    if vision_selector.is_none() {
        message.push_str(
            " OMP modelRoles.vision is omitted because the selected model is exported text-only.",
        );
    }
    Ok(GatewayClientConfigPreview {
        client_id: "omp".to_string(),
        can_apply: true,
        strategy: "managed_native_config".to_string(),
        config_path: Some(config_path.to_path_buf()),
        current_redacted: current.map(|text| sanitize_text(&text)),
        next_redacted: sanitize_text(&combined_named_text(&[
            ("config.yml", &next_config),
            ("models.yml", &next_models),
        ])),
        backup_required: config_path.exists() || models_path.exists(),
        message,
    })
}

pub(in crate::gateway) fn apply_omp_config_with_paths(
    config_path: &Path,
    models_path: &Path,
    backup_root: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<GatewayClientApplyResult, String> {
    let model = resolve_gateway_client_model_id(settings, providers, model)?;
    let current_config = fs::read_to_string(config_path).unwrap_or_default();
    let current_models = fs::read_to_string(models_path).unwrap_or_default();
    let backup_path = create_snapshot_backup(
        "omp",
        backup_root,
        &[("config.yml", config_path), ("models.yml", models_path)],
        is_omp_codexhub_config(&current_config, &current_models),
    )?;
    let selector = gateway_client_model_selector(settings, providers, &model)?;
    let vision_selector = if gateway_exported_model_supports_image(settings, providers, &model) {
        Some(selector.as_str())
    } else {
        None
    };
    let default_reasoning_effort =
        gateway_exported_model_default_reasoning_effort(settings, providers, &model);
    let next_config = omp_config_text(
        Some(&current_config),
        &selector,
        vision_selector,
        default_reasoning_effort.as_deref(),
    );
    let next_models = omp_models_yml_text(settings, providers, &model)?;
    write_text_replace(config_path, &next_config)?;
    write_text_replace(models_path, &next_models)?;
    let mut message = "OMP now routes through CodexHub Gateway.".to_string();
    if vision_selector.is_none() {
        message.push_str(
            " OMP modelRoles.vision is omitted because the selected model is exported text-only.",
        );
    }
    Ok(GatewayClientApplyResult {
        client_id: "omp".to_string(),
        applied: true,
        config_path: Some(config_path.to_path_buf()),
        backup_path,
        message,
    })
}

pub(in crate::gateway) fn restore_omp_config_with_paths(
    config_path: &Path,
    models_path: &Path,
    backup_root: &Path,
) -> Result<GatewayClientApplyResult, String> {
    let latest = restore_latest_snapshot_backup(
        "omp",
        backup_root,
        &[("config.yml", config_path), ("models.yml", models_path)],
        |path| {
            let config = fs::read_to_string(path.join("config.yml")).unwrap_or_default();
            let models = fs::read_to_string(path.join("models.yml")).unwrap_or_default();
            is_omp_codexhub_config(&config, &models)
        },
    )?;
    Ok(GatewayClientApplyResult {
        client_id: "omp".to_string(),
        applied: true,
        config_path: Some(config_path.to_path_buf()),
        backup_path: Some(latest),
        message: "OMP official config restored.".to_string(),
    })
}

pub(in crate::gateway) fn is_omp_codexhub_config(config_text: &str, models_text: &str) -> bool {
    config_text
        .lines()
        .filter_map(|line| line.split_once(':').map(|(_, value)| value.trim()))
        .any(is_codexhub_client_model_selector)
        || is_omp_models_codexhub_config(models_text)
}

pub(in crate::gateway) fn is_omp_models_codexhub_config(text: &str) -> bool {
    let mut in_candidate = false;
    let mut has_local_gateway_url = false;
    for line in text.lines() {
        let starts_provider_entry =
            line.starts_with("  ") && !line.starts_with("    ") && line.trim_end().ends_with(':');
        if starts_provider_entry {
            if in_candidate && has_local_gateway_url {
                return true;
            }
            let provider_id = line.trim().trim_end_matches(':');
            in_candidate = is_codexhub_client_provider_id(provider_id);
            has_local_gateway_url = false;
            continue;
        }
        if in_candidate {
            let trimmed = line.trim();
            if let Some(url) = trimmed.strip_prefix("baseUrl:") {
                has_local_gateway_url = is_local_gateway_url(url.trim())
                    && (url.contains("/v1/providers/")
                        || url.trim().trim_end_matches('/').ends_with("/v1"));
            }
            if trimmed.contains("CodexHub Gateway") || trimmed.contains("CodexHub ") {
                return true;
            }
        }
    }
    in_candidate && has_local_gateway_url
}
