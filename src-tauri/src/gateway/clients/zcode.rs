use super::super::{
    gateway_base_without_v1, gateway_client_provider_groups, read_json_file_or_empty,
    remove_codexhub_client_provider_entries, timestamp_millis, GatewayClientEndpointSelection,
    GatewayClientProviderGroup, GatewayClientProviderModel, ZcodeProviderFileKind,
};
use crate::{Provider, Settings};
use serde_json::{json, Map, Value};
use std::fs;
use std::path::Path;

pub(in crate::gateway) fn zcode_catalog_text(
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<String, String> {
    zcode_provider_collection_text(settings, providers, model, ZcodeProviderFileKind::Catalog)
}

pub(in crate::gateway) fn zcode_v2_cache_text(
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<String, String> {
    zcode_provider_collection_text(settings, providers, model, ZcodeProviderFileKind::V2Cache)
}

fn zcode_provider_collection_text(
    settings: &Settings,
    providers: &[Provider],
    model: &str,
    file_kind: ZcodeProviderFileKind,
) -> Result<String, String> {
    zcode_provider_collection_text_with_now(
        settings,
        providers,
        model,
        file_kind,
        timestamp_millis() as u64,
    )
}

/// Deterministic ZCode collection serializer used by the readback verifier.
/// The `now` argument is the timestamp to stamp onto `createdAt`/`updatedAt`;
/// the readback path reuses the timestamps already persisted in the written
/// file so the round-trip comparison is stable across wall-clock time instead
/// of regenerating a fresh `timestamp_millis()` that would always contradict
/// the persisted output.
pub(in crate::gateway) fn zcode_provider_collection_text_with_now(
    settings: &Settings,
    providers: &[Provider],
    model: &str,
    file_kind: ZcodeProviderFileKind,
    now: u64,
) -> Result<String, String> {
    let groups = gateway_client_provider_groups(settings, providers, model)?;
    let catalog_providers = groups
        .providers
        .iter()
        .map(|group| zcode_catalog_provider_value(settings, group, now, file_kind))
        .collect::<Vec<_>>();
    let body = json!({
        "schemaVersion": "zcode.model-providers.v2",
        "providers": catalog_providers,
    });
    serde_json::to_string_pretty(&body)
        .map(|text| format!("{text}\n"))
        .map_err(|error| format!("failed to serialize ZCode catalog: {error}"))
}

/// Read the persisted `createdAt` (or `updatedAt`) timestamp from a written
/// ZCode collection file (`codexhub.json` / `bots-model-cache.v2.json`). The
/// overlay stamps both fields with the same `now` value, so reading the first
/// provider's `createdAt` is sufficient. Returns `None` when the file is
/// absent or malformed; callers must have already validated structure.
pub(in crate::gateway) fn persisted_zcode_collection_timestamp(path: &Path) -> Option<u64> {
    let text = fs::read_to_string(path).ok()?;
    let value: Value = serde_json::from_str(&text).ok()?;
    let providers = value.get("providers")?.as_array()?;
    let first = providers.first()?;
    first.get("createdAt")?.as_u64()
}

fn zcode_catalog_provider_value(
    settings: &Settings,
    group: &GatewayClientProviderGroup,
    now: u64,
    file_kind: ZcodeProviderFileKind,
) -> Value {
    let kind = group.endpoint_selection.zcode_kind();
    let models = group
        .models
        .iter()
        .map(|model| zcode_model_value(model, kind))
        .collect::<Vec<_>>();
    let (base_url, endpoint_path) = zcode_provider_endpoint(settings, group, file_kind);
    let mut paths = Map::new();
    paths.insert(kind.to_string(), Value::String(endpoint_path.to_string()));
    json!({
        "id": group.client_provider_id.clone(),
        "name": group.display_name.clone(),
        "enabled": true,
        "source": "custom",
        "apiFormat": group.endpoint_selection.zcode_api_format(),
        "endpoints": {
            "baseURL": base_url,
            "paths": Value::Object(paths),
        },
        "apiKeyRequired": true,
        "apiKey": settings.gateway_client_key,
        "defaultKind": kind,
        "models": models,
        "createdAt": now,
        "updatedAt": now,
    })
}

pub(in crate::gateway) fn zcode_provider_endpoint(
    settings: &Settings,
    group: &GatewayClientProviderGroup,
    file_kind: ZcodeProviderFileKind,
) -> (String, String) {
    match file_kind {
        ZcodeProviderFileKind::Catalog => {
            let endpoint_path = match group.endpoint_selection.openai_compatible_selection() {
                GatewayClientEndpointSelection::Responses => group.responses_path.clone(),
                GatewayClientEndpointSelection::ChatCompletions
                | GatewayClientEndpointSelection::AnthropicMessages => {
                    group.chat_completions_path.clone()
                }
            };
            (gateway_base_without_v1(settings), endpoint_path)
        }
        ZcodeProviderFileKind::V2Cache => {
            let endpoint_path = match group.endpoint_selection.openai_compatible_selection() {
                GatewayClientEndpointSelection::Responses => "/responses".to_string(),
                GatewayClientEndpointSelection::ChatCompletions
                | GatewayClientEndpointSelection::AnthropicMessages => {
                    "/chat/completions".to_string()
                }
            };
            (group.base_url.clone(), endpoint_path)
        }
    }
}

fn zcode_reasoning_value(model: &GatewayClientProviderModel) -> Option<Value> {
    if model.supported_reasoning_levels.is_empty() {
        return None;
    }
    let mut variants = model.supported_reasoning_levels.clone();
    if !variants.iter().any(|variant| variant == "off") {
        variants.push("off".to_string());
    }
    let default_variant = model
        .default_reasoning_level
        .as_ref()
        .filter(|default| variants.iter().any(|variant| variant == *default))
        .cloned()
        .unwrap_or_else(|| variants[0].clone());
    Some(json!({
        "enabled": true,
        "variants": variants,
        "defaultVariant": default_variant,
    }))
}

fn zcode_model_value(model: &GatewayClientProviderModel, kind: &str) -> Value {
    let mut value = json!({
        "id": model.id.clone(),
        "name": model.display_name.clone(),
        "kinds": [kind],
        "defaultKind": kind,
        "modalities": {
            "input": model.input_modalities.clone(),
            "output": ["text"],
        },
        "maxOutputTokens": 32768,
    });
    if let (Some(object), Some(context_window)) = (value.as_object_mut(), model.context_window) {
        object.insert("contextWindow".to_string(), json!(context_window));
    }
    if let (Some(object), Some(reasoning)) = (value.as_object_mut(), zcode_reasoning_value(model)) {
        object.insert("reasoning".to_string(), reasoning);
    }
    value
}

pub(in crate::gateway) fn zcode_v2_config_text(
    config_path: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<String, String> {
    let groups = gateway_client_provider_groups(settings, providers, model)?;
    let mut value = read_json_file_or_empty(config_path, "ZCode v2 config")?;
    if !value.is_object() {
        value = json!({});
    }
    let root = value
        .as_object_mut()
        .ok_or_else(|| "ZCode v2 config root must be a JSON object".to_string())?;
    let provider_root = root
        .entry("provider".to_string())
        .or_insert_with(|| json!({}));
    if !provider_root.is_object() {
        *provider_root = json!({});
    }
    let provider_map = provider_root
        .as_object_mut()
        .ok_or_else(|| "ZCode v2 provider root must be a JSON object".to_string())?;
    remove_codexhub_client_provider_entries(provider_map);
    for group in &groups.providers {
        provider_map.insert(
            group.client_provider_id.clone(),
            zcode_v2_provider_value(settings, group),
        );
    }
    serde_json::to_string_pretty(&value)
        .map(|text| format!("{text}\n"))
        .map_err(|error| format!("failed to serialize ZCode v2 config: {error}"))
}

fn zcode_v2_provider_value(settings: &Settings, group: &GatewayClientProviderGroup) -> Value {
    let kind = group.endpoint_selection.zcode_kind();
    let models = group
        .models
        .iter()
        .map(|model| {
            (model.id.clone(), {
                let mut value = json!({
                "name": model.display_name.clone(),
                "limit": {
                    "output": 32768,
                },
                "modalities": {
                    "input": model.input_modalities.clone(),
                    "output": ["text"],
                },
                });
                if let (Some(limit), Some(context_window)) = (
                    value.get_mut("limit").and_then(Value::as_object_mut),
                    model.context_window,
                ) {
                    limit.insert("context".to_string(), json!(context_window));
                }
                if let (Some(object), Some(reasoning)) =
                    (value.as_object_mut(), zcode_reasoning_value(model))
                {
                    object.insert("reasoning".to_string(), reasoning);
                }
                value
            })
        })
        .collect::<Map<_, _>>();
    let (base_url, endpoint_path) =
        zcode_provider_endpoint(settings, group, ZcodeProviderFileKind::V2Cache);
    let mut paths = Map::new();
    paths.insert(kind.to_string(), Value::String(endpoint_path));
    json!({
        "name": group.display_name.clone(),
        "kind": kind,
        "enabled": true,
        "source": "custom",
        "apiFormat": group.endpoint_selection.zcode_api_format(),
        "endpoints": {
            "baseURL": base_url,
            "paths": Value::Object(paths),
        },
        "options": {
            "baseURL": group.base_url.clone(),
            "apiKey": settings.gateway_client_key,
            "apiKeyRequired": true,
        },
        "models": Value::Object(models),
    })
}
