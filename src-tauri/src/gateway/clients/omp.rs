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
