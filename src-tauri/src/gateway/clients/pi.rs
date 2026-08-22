use super::super::{
    gateway_client_provider_groups, read_json_file_or_empty,
    remove_codexhub_client_provider_entries, GatewayClientProviderGroup,
    GatewayClientProviderModel,
};
use crate::{Provider, Settings};
use serde_json::{json, Value};
use std::path::Path;

pub(in crate::gateway) fn pi_settings_text(
    settings_path: &Path,
    _settings: &Settings,
    _providers: &[Provider],
    _model: &str,
) -> Result<String, String> {
    // Provider Injection (ADR-0004 Q1): settings.json is user-owned.
    // Never force defaultProvider/defaultModel and never strip enabledModels.
    let mut value = read_json_file_or_empty(settings_path, "Pi settings")?;
    if !value.is_object() {
        value = json!({});
    }
    serde_json::to_string_pretty(&value)
        .map(|text| format!("{text}\n"))
        .map_err(|error| format!("failed to serialize Pi settings: {error}"))
}

pub(in crate::gateway) fn pi_models_text(
    models_path: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<String, String> {
    let groups = gateway_client_provider_groups(settings, providers, model)?;
    let mut value = read_json_file_or_empty(models_path, "Pi models")?;
    if !value.is_object() {
        value = json!({});
    }
    let object = value
        .as_object_mut()
        .ok_or_else(|| "Pi models root must be a JSON object".to_string())?;
    let provider_root = object
        .entry("providers".to_string())
        .or_insert_with(|| json!({}));
    if !provider_root.is_object() {
        *provider_root = json!({});
    }
    let providers_object = provider_root
        .as_object_mut()
        .ok_or_else(|| "Pi providers root must be a JSON object".to_string())?;
    remove_codexhub_client_provider_entries(providers_object);
    for group in &groups.providers {
        providers_object.insert(
            group.client_provider_id.clone(),
            codexhub_pi_provider_value(settings, group),
        );
    }
    serde_json::to_string_pretty(&value)
        .map(|text| format!("{text}\n"))
        .map_err(|error| format!("failed to serialize Pi models: {error}"))
}

fn codexhub_pi_provider_value(settings: &Settings, group: &GatewayClientProviderGroup) -> Value {
    let models = group
        .models
        .iter()
        .map(codexhub_pi_model_value)
        .collect::<Vec<_>>();
    json!({
        "baseUrl": group.base_url.clone(),
        "api": group.endpoint_selection.pi_api(),
        "apiKey": settings.gateway_client_key,
        "authHeader": true,
        "compat": {
            "supportsDeveloperRole": group.supports_developer_role,
            "supportsReasoningEffort": true,
            "supportsUsageInStreaming": true,
        },
        "models": models,
    })
}

fn codexhub_pi_model_value(model: &GatewayClientProviderModel) -> Value {
    let mut value = json!({
        "id": model.id.clone(),
        "name": model.display_name.clone(),
        "reasoning": !model.supported_reasoning_levels.is_empty(),
        "input": model.input_modalities.clone(),
        "headers": {
            "x-codex-client-id": "pi",
        },
        "maxTokens": 32768,
        "cost": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
        },
    });
    if let (Some(object), Some(context_window)) = (value.as_object_mut(), model.context_window) {
        object.insert("contextWindow".to_string(), json!(context_window));
    }
    value
}
