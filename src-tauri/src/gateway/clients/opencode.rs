use super::super::gateway_client_provider_groups;
use crate::{Provider, Settings};
use serde_json::{json, Map, Value};

pub(in crate::gateway) fn opencode_reasoning_variants(
    supported_reasoning_levels: &[String],
) -> Map<String, serde_json::Value> {
    supported_reasoning_levels
        .iter()
        .map(|level| (level.clone(), json!({ "reasoningEffort": level.clone() })))
        .collect::<Map<_, _>>()
}

pub(in crate::gateway) fn opencode_config_text(
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<String, String> {
    let groups = gateway_client_provider_groups(settings, providers, model)?;
    let mut provider_map = Map::new();
    for group in &groups.providers {
        let mut models = Map::new();
        for gateway_model in &group.models {
            let mut entry = json!({
                "name": gateway_model.display_name,
                "modalities": {
                    "input": gateway_model.input_modalities,
                },
            });
            if !gateway_model.supported_reasoning_levels.is_empty() {
                let default_effort = gateway_model
                    .default_reasoning_level
                    .as_ref()
                    .filter(|default| {
                        gateway_model
                            .supported_reasoning_levels
                            .iter()
                            .any(|level| level == *default)
                    })
                    .cloned()
                    .unwrap_or_else(|| gateway_model.supported_reasoning_levels[0].clone());
                let variants =
                    opencode_reasoning_variants(&gateway_model.supported_reasoning_levels);
                if let Some(object) = entry.as_object_mut() {
                    object.insert(
                        "options".to_string(),
                        json!({ "reasoningEffort": default_effort }),
                    );
                    object.insert("variants".to_string(), Value::Object(variants));
                }
            }
            models.insert(gateway_model.id.clone(), entry);
        }
        provider_map.insert(
            group.client_provider_id.clone(),
            json!({
                "name": group.display_name.clone(),
                "npm": group.endpoint_selection.opencode_npm(),
                "options": {
                    "baseURL": group.base_url.clone(),
                    "apiKey": settings.gateway_client_key,
                },
                "models": Value::Object(models),
            }),
        );
    }
    let body = json!({
        "$schema": "https://opencode.ai/config.json",
        "model": groups.default_selector,
        "small_model": groups.default_selector,
        "provider": Value::Object(provider_map),
    });
    serde_json::to_string_pretty(&body)
        .map(|text| format!("{text}\n"))
        .map_err(|error| format!("failed to serialize OpenCode config: {error}"))
}
