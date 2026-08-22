use super::clients::zcode::is_managed_zcode_catalog_provider_entry;
use super::{endpoints, GatewayModel};
use crate::app_flavor::RoutingOwner;
use crate::{config, models, official_refresh, safe_file, Provider, Settings, UpstreamFormat};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

pub(in crate::gateway) const DEFAULT_MODEL: &str = "gpt-5.5";
pub(in crate::gateway) const OPENAI_CONTEXT_GUARD_WINDOW: u32 = 272_000;

pub(in crate::gateway) const OFFICIAL_MODELS: &[(&str, &str, u32)] = &[
    ("gpt-5.5", "5.5", 258400),
    ("gpt-5.4", "5.4", 272000),
    ("gpt-5.4-mini", "5.4 Mini", 272000),
    ("gpt-5.3-codex-spark", "5.3 Codex Spark", 128000),
];

pub(in crate::gateway) const OFFICIAL_FAST_VARIANTS: &[(&str, &str, &str, u32)] = &[
    ("gpt-5.5", "gpt-5.5-fast", "5.5 Fast", 258400),
    ("gpt-5.4", "gpt-5.4-fast", "5.4 Fast", 272000),
];

pub(in crate::gateway) fn official_gateway_input_modalities() -> Vec<String> {
    vec!["text".to_string(), "image".to_string()]
}

pub(in crate::gateway) const OFFICIAL_REASONING_LEVELS: &[&str] =
    &["low", "medium", "high", "xhigh", "max"];
pub(in crate::gateway) const OFFICIAL_DEFAULT_REASONING_LEVEL: &str = "medium";

pub(in crate::gateway) fn official_gateway_reasoning_levels() -> Vec<String> {
    OFFICIAL_REASONING_LEVELS
        .iter()
        .map(|level| (*level).to_string())
        .collect()
}

#[derive(Debug, Clone)]
pub(in crate::gateway) struct GatewayClientProviderGroups {
    #[allow(dead_code)]
    pub(in crate::gateway) default_provider_id: String,
    #[allow(dead_code)]
    pub(in crate::gateway) default_model_id: String,
    pub(in crate::gateway) default_selector: String,
    pub(in crate::gateway) providers: Vec<GatewayClientProviderGroup>,
}

#[derive(Debug, Clone)]
pub(in crate::gateway) struct GatewayClientProviderGroup {
    pub(in crate::gateway) client_provider_id: String,
    pub(in crate::gateway) display_name: String,
    pub(in crate::gateway) base_url: String,
    pub(in crate::gateway) endpoint_selection: GatewayClientEndpointSelection,
    pub(in crate::gateway) responses_path: String,
    pub(in crate::gateway) chat_completions_path: String,
    pub(in crate::gateway) supports_developer_role: bool,
    pub(in crate::gateway) models: Vec<GatewayClientProviderModel>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(in crate::gateway) enum GatewayClientEndpointSelection {
    Responses,
    ChatCompletions,
    AnthropicMessages,
}

impl GatewayClientEndpointSelection {
    pub(in crate::gateway) fn opencode_npm(self) -> &'static str {
        match self.openai_compatible_selection() {
            GatewayClientEndpointSelection::Responses => "@ai-sdk/openai",
            GatewayClientEndpointSelection::ChatCompletions => "@ai-sdk/openai-compatible",
            GatewayClientEndpointSelection::AnthropicMessages => "@ai-sdk/openai-compatible",
        }
    }

    pub(in crate::gateway) fn pi_api(self) -> &'static str {
        match self.openai_compatible_selection() {
            GatewayClientEndpointSelection::Responses => "openai-responses",
            GatewayClientEndpointSelection::ChatCompletions => "openai-completions",
            GatewayClientEndpointSelection::AnthropicMessages => "openai-completions",
        }
    }

    pub(in crate::gateway) fn zcode_api_format(self) -> &'static str {
        match self.openai_compatible_selection() {
            GatewayClientEndpointSelection::Responses => "openai-responses",
            GatewayClientEndpointSelection::ChatCompletions => "openai-chat-completions",
            GatewayClientEndpointSelection::AnthropicMessages => "openai-chat-completions",
        }
    }

    pub(in crate::gateway) fn zcode_kind(self) -> &'static str {
        match self.openai_compatible_selection() {
            GatewayClientEndpointSelection::Responses => "openai",
            GatewayClientEndpointSelection::ChatCompletions => "openai-compatible",
            GatewayClientEndpointSelection::AnthropicMessages => "openai-compatible",
        }
    }

    pub(in crate::gateway) fn openai_compatible_selection(self) -> Self {
        match self {
            GatewayClientEndpointSelection::AnthropicMessages => {
                GatewayClientEndpointSelection::ChatCompletions
            }
            other => other,
        }
    }
}

#[derive(Debug, Clone)]
pub(in crate::gateway) struct GatewayClientProviderModel {
    pub(in crate::gateway) id: String,
    pub(in crate::gateway) display_name: String,
    pub(in crate::gateway) context_window: Option<u32>,
    pub(in crate::gateway) input_modalities: Vec<String>,
    pub(in crate::gateway) supported_reasoning_levels: Vec<String>,
    pub(in crate::gateway) default_reasoning_level: Option<String>,
}

pub(in crate::gateway) fn official_models(settings: &Settings) -> Vec<GatewayModel> {
    // Numeric limits are only trustworthy after the catalog publication fence
    // has resolved them. Subscription metadata remains useful for identity and
    // descriptive fields, but must never reintroduce a builtin fallback limit.
    let published_context_windows =
        official_refresh::published_official_context_windows().unwrap_or_default();
    let source_models = match models::list_cached_official_subscription_models_with_presence() {
        Ok(Some(models)) => Some(models),
        Ok(None) | Err(_) => models::list_models_with_presence().ok().flatten(),
    };
    official_models_from_metadata(settings, source_models, &published_context_windows)
}

pub(in crate::gateway) fn official_models_from_metadata(
    settings: &Settings,
    subscription_models: Option<Vec<crate::Model>>,
    published_context_windows: &BTreeMap<String, u32>,
) -> Vec<GatewayModel> {
    // A missing publication fence means no safe Official snapshot is available.
    // Official models must not be listed or routed until a safe snapshot has
    // been published, regardless of whether the optional cost guard is enabled.
    if published_context_windows.is_empty() {
        return Vec::new();
    }
    let mut models: Vec<GatewayModel> = match subscription_models {
        Some(source_models) => {
            let mut models = Vec::<GatewayModel>::new();
            let mut positions = HashMap::<String, usize>::new();
            let mut bare_sources = HashMap::<String, bool>::new();
            let mut enabled_by_id = HashMap::<String, bool>::new();
            for model in source_models {
                if !models::model_is_catalog_visible(&model) {
                    continue;
                }
                let source_is_bare = !model.id.trim().starts_with("openai/");
                let source_enabled = model.enabled;
                let Some(gateway_model) = official_gateway_model_from_metadata(
                    settings,
                    model,
                    published_context_windows,
                ) else {
                    continue;
                };
                if let Some(position) = positions.get(&gateway_model.id).copied() {
                    enabled_by_id
                        .entry(gateway_model.id.clone())
                        .and_modify(|enabled| *enabled = *enabled || source_enabled);
                    let existing_is_bare = bare_sources
                        .get(&gateway_model.id)
                        .copied()
                        .unwrap_or(false);
                    if source_is_bare || !existing_is_bare {
                        let id = gateway_model.id.clone();
                        models[position] = gateway_model;
                        bare_sources.insert(id, source_is_bare);
                    }
                    continue;
                }
                let id = gateway_model.id.clone();
                positions.insert(id.clone(), models.len());
                bare_sources.insert(id, source_is_bare);
                enabled_by_id.insert(gateway_model.id.clone(), source_enabled);
                models.push(gateway_model);
            }
            models.retain(|model| enabled_by_id.get(&model.id).copied().unwrap_or(true));
            models
        }
        None => fallback_official_gateway_models(settings, published_context_windows),
    };

    let base_ids = models
        .iter()
        .map(|model| model.id.clone())
        .collect::<HashSet<_>>();
    for (base_id, id, display_name, _) in OFFICIAL_FAST_VARIANTS {
        if !base_ids.contains(*base_id) || official_model_disabled(settings, base_id) {
            continue;
        }
        if settings
            .gateway_fast_model_variants
            .iter()
            .any(|value| value == base_id)
        {
            let context_window = models
                .iter()
                .find(|model| model.id == *base_id)
                .and_then(|model| model.context_window);
            models.push(GatewayModel {
                id: (*id).to_string(),
                display_name: (*display_name).to_string(),
                source: "Official Codex subscription".to_string(),
                source_kind: "official".to_string(),
                supports_responses: true,
                supports_chat_completions: true,
                context_window,
                input_modalities: Some(official_gateway_input_modalities()),
                supported_reasoning_levels: Some(official_gateway_reasoning_levels()),
                default_reasoning_level: Some(OFFICIAL_DEFAULT_REASONING_LEVEL.to_string()),
            });
        }
    }

    if settings.openai_context_guard_enabled {
        for model in &mut models {
            model.context_window = model
                .context_window
                .map(|context_window| context_window.min(OPENAI_CONTEXT_GUARD_WINDOW));
        }
    }

    models
}

pub(in crate::gateway) fn official_gateway_model_from_metadata(
    settings: &Settings,
    model: crate::Model,
    published_context_windows: &BTreeMap<String, u32>,
) -> Option<GatewayModel> {
    let id = official_gateway_model_id(&model.id)?;
    let context_window = published_context_windows.get(&id).copied();
    if settings.openai_context_guard_enabled && context_window.is_none() {
        return None;
    }
    if official_model_disabled(settings, &id) || is_gateway_fast_variant_id(&id) {
        return None;
    }
    Some(GatewayModel {
        id: id.clone(),
        display_name: model
            .display_name
            .as_deref()
            .map(models::official_short_display_name)
            .unwrap_or_else(|| models::official_short_display_name(&id)),
        source: "Official Codex subscription".to_string(),
        source_kind: "official".to_string(),
        supports_responses: true,
        supports_chat_completions: true,
        context_window,
        input_modalities: model
            .input_modalities
            .clone()
            .filter(|modalities| !modalities.is_empty())
            .or_else(|| Some(official_gateway_input_modalities())),
        supported_reasoning_levels: model
            .supported_reasoning_levels
            .clone()
            .filter(|levels| !levels.is_empty())
            .or_else(|| Some(official_gateway_reasoning_levels())),
        default_reasoning_level: model
            .default_reasoning_level
            .clone()
            .filter(|level| !level.is_empty())
            .or_else(|| Some(OFFICIAL_DEFAULT_REASONING_LEVEL.to_string())),
    })
}

pub(in crate::gateway) fn fallback_official_gateway_models(
    settings: &Settings,
    published_context_windows: &BTreeMap<String, u32>,
) -> Vec<GatewayModel> {
    OFFICIAL_MODELS
        .iter()
        .filter(|(id, _, _)| !official_model_disabled(settings, id))
        .map(|(id, display_name, _)| GatewayModel {
            id: (*id).to_string(),
            display_name: (*display_name).to_string(),
            source: "Official Codex subscription".to_string(),
            source_kind: "official".to_string(),
            supports_responses: true,
            supports_chat_completions: true,
            context_window: published_context_windows.get(*id).copied(),
            input_modalities: Some(official_gateway_input_modalities()),
            supported_reasoning_levels: Some(official_gateway_reasoning_levels()),
            default_reasoning_level: Some(OFFICIAL_DEFAULT_REASONING_LEVEL.to_string()),
        })
        .filter(|model| !settings.openai_context_guard_enabled || model.context_window.is_some())
        .collect()
}

pub(in crate::gateway) fn official_gateway_model_id(id: &str) -> Option<String> {
    let id = id.trim();
    let bare_id = id.strip_prefix("openai/").unwrap_or(id);
    bare_id.starts_with("gpt-").then(|| bare_id.to_string())
}

pub(in crate::gateway) fn is_gateway_fast_variant_id(id: &str) -> bool {
    matches!(
        id.strip_prefix("openai/").unwrap_or(id),
        "gpt-5.5-fast" | "gpt-5.4-fast"
    )
}

pub(in crate::gateway) fn official_model_disabled(settings: &Settings, id: &str) -> bool {
    let without_prefix = id.strip_prefix("openai/").unwrap_or(id);
    settings.official_disabled_models.iter().any(|value| {
        value == id
            || value == without_prefix
            || value.strip_prefix("openai/").unwrap_or(value) == without_prefix
    })
}

pub(in crate::gateway) fn gateway_models_from_config(
    settings: &Settings,
    providers: &[Provider],
) -> Vec<GatewayModel> {
    let official_source_models = if settings.include_official_models {
        official_models(settings)
    } else {
        Vec::new()
    };
    gateway_models_from_sources(settings, providers, official_source_models)
}

pub(in crate::gateway) fn gateway_models_from_sources(
    settings: &Settings,
    providers: &[Provider],
    official_source_models: Vec<GatewayModel>,
) -> Vec<GatewayModel> {
    let mut output = Vec::new();
    let mut exported_ids = HashSet::new();
    if settings.include_official_models {
        for model in official_source_models {
            if exported_ids.insert(model.id.to_ascii_lowercase()) {
                output.push(model);
            }
        }
    }
    for provider in providers {
        if !provider.enabled {
            continue;
        }
        for model in &provider.models {
            if !model.enabled || !model.gateway_exported {
                continue;
            }
            let model_id = provider_qualified_model_id(&provider.id, &model.id);
            if !exported_ids.insert(model_id.to_ascii_lowercase()) {
                continue;
            }
            let reasoning_levels = model
                .supported_reasoning_levels
                .clone()
                .filter(|levels| !levels.is_empty());
            output.push(GatewayModel {
                id: model_id.clone(),
                display_name: model
                    .display_name
                    .clone()
                    .unwrap_or_else(|| model_id.clone()),
                source: provider.name.clone(),
                source_kind: "external".to_string(),
                supports_responses: provider
                    .upstream_format
                    .as_ref()
                    .map(|format| {
                        matches!(format, UpstreamFormat::Auto | UpstreamFormat::Responses)
                    })
                    .unwrap_or(true),
                supports_chat_completions: true,
                context_window: model.context_window,
                input_modalities: model
                    .input_modalities
                    .clone()
                    .filter(|modalities| !modalities.is_empty()),
                default_reasoning_level: if reasoning_levels.is_some() {
                    model
                        .default_reasoning_level
                        .clone()
                        .filter(|level| !level.is_empty())
                } else {
                    None
                },
                supported_reasoning_levels: reasoning_levels,
            });
        }
    }
    output
}

pub(in crate::gateway) fn provider_qualified_model_id(provider_id: &str, model_id: &str) -> String {
    let provider_id = provider_id.trim();
    let model_id = model_id.trim();
    if provider_id.is_empty()
        || model_id.is_empty()
        || model_id.starts_with(&format!("{provider_id}/"))
    {
        return model_id.to_string();
    }
    format!("{provider_id}/{model_id}")
}

pub(in crate::gateway) fn gateway_model_alias_map(
    providers: &[Provider],
) -> HashMap<String, String> {
    let mut aliases = HashMap::new();
    for provider in providers {
        if !provider.enabled {
            continue;
        }
        for model in &provider.models {
            if !model.enabled || !model.gateway_exported {
                continue;
            }
            let canonical = provider_qualified_model_id(&provider.id, &model.id);
            for alias in &model.aliases {
                let alias = alias.trim();
                if alias.is_empty() {
                    continue;
                }
                let qualified_alias = if alias.contains('/') {
                    alias.to_string()
                } else {
                    provider_qualified_model_id(&provider.id, alias)
                };
                aliases
                    .entry(qualified_alias)
                    .or_insert_with(|| canonical.clone());
            }
        }
    }
    aliases
}

pub(in crate::gateway) fn resolve_gateway_client_model_id(
    settings: &Settings,
    providers: &[Provider],
    requested: &str,
) -> Result<String, String> {
    let exported = gateway_models_from_config(settings, providers);
    resolve_gateway_client_model_id_from_exported(&exported, providers, requested)
}

pub(in crate::gateway) fn resolve_gateway_client_model_id_from_exported(
    exported_models: &[GatewayModel],
    providers: &[Provider],
    requested: &str,
) -> Result<String, String> {
    let requested = requested.trim();
    if requested.is_empty() {
        return Err("Gateway model is required".to_string());
    }
    let exported = exported_models
        .iter()
        .map(|model| model.id.as_str())
        .collect::<HashSet<_>>();
    if exported.contains(requested) {
        return Ok(requested.to_string());
    }
    if let Some(canonical) = official_gateway_model_id(requested) {
        if exported.contains(canonical.as_str()) {
            return Ok(canonical);
        }
    }
    if let Some(canonical) = gateway_model_alias_map(providers).get(requested) {
        return Ok(canonical.clone());
    }
    Err(format!("Gateway model is not exported: {requested}"))
}

pub(in crate::gateway) fn gateway_client_models_from_exported(
    providers: &[Provider],
    default_model: &str,
    exported_models: Vec<GatewayModel>,
) -> Result<Vec<GatewayModel>, String> {
    let default_model =
        resolve_gateway_client_model_id_from_exported(&exported_models, providers, default_model)?;
    let mut seen = HashSet::new();
    let mut output = Vec::new();
    for model in exported_models {
        if seen.insert(model.id.clone()) {
            output.push(model);
        }
    }

    if let Some(position) = output.iter().position(|model| model.id == default_model) {
        if position > 0 {
            let selected = output.remove(position);
            output.insert(0, selected);
        }
    } else {
        return Err("Gateway model disappeared during client export".to_string());
    }
    Ok(output)
}

pub(in crate::gateway) fn split_gateway_model_id(model_id: &str) -> (String, String) {
    let model_id = model_id.trim();
    if let Some(official_id) = official_gateway_model_id(model_id) {
        return ("openai".to_string(), official_id);
    }
    if let Some((provider_id, short_id)) = model_id.split_once('/') {
        let provider_id = provider_id.trim();
        let short_id = short_id.trim();
        if !provider_id.is_empty() && !short_id.is_empty() {
            return (provider_id.to_string(), short_id.to_string());
        }
    }
    ("ollama-cloud".to_string(), model_id.to_string())
}

pub(in crate::gateway) fn codexhub_client_provider_id(provider_id: &str) -> String {
    let suffix = provider_id
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' {
                ch
            } else {
                '-'
            }
        })
        .collect::<String>()
        .trim_matches('-')
        .to_string();
    if suffix.is_empty() {
        "codexhub-provider".to_string()
    } else {
        format!("codexhub-{suffix}")
    }
}

pub(in crate::gateway) fn is_codexhub_client_provider_id(provider_id: &str) -> bool {
    provider_id == "codexhub" || provider_id.starts_with("codexhub-")
}

pub(in crate::gateway) fn is_builtin_codexhub_client_provider_id(provider_id: &str) -> bool {
    matches!(
        provider_id,
        "codexhub"
            | "codexhub-openai"
            | "codexhub-ollama-cloud"
            | "codexhub-minimax-cn"
            | "codexhub-volc"
            | "codexhub-xunfei"
    )
}

pub(in crate::gateway) fn is_recognized_codexhub_client_provider_id(provider_id: &str) -> bool {
    if is_builtin_codexhub_client_provider_id(provider_id) {
        return true;
    }
    config::get_providers().ok().is_some_and(|providers| {
        providers
            .iter()
            .any(|provider| codexhub_client_provider_id(&provider.id) == provider_id)
    })
}

pub(in crate::gateway) fn selector_provider_id(selector: &str) -> Option<&str> {
    selector.split_once('/').map(|(provider_id, _)| provider_id)
}

pub(in crate::gateway) fn is_codexhub_client_model_selector(model: &str) -> bool {
    selector_provider_id(model).is_some_and(is_recognized_codexhub_client_provider_id)
}

pub(in crate::gateway) fn is_local_gateway_url(url: &str) -> bool {
    let value = url.trim().trim_matches('"').trim_matches('\'');
    value.starts_with("http://127.0.0.1:") || value.starts_with("http://localhost:")
}

pub(in crate::gateway) fn routing_owner_from_gateway_url(url: &str) -> RoutingOwner {
    let trimmed = url.trim().trim_end_matches('/');
    if trimmed.starts_with("http://127.0.0.1:9099") || trimmed.starts_with("http://localhost:9099")
    {
        return RoutingOwner::Release;
    }
    if trimmed.starts_with("http://127.0.0.1:9109") || trimmed.starts_with("http://localhost:9109")
    {
        return RoutingOwner::Beta;
    }
    if trimmed.contains("127.0.0.1") || trimmed.contains("localhost") {
        return RoutingOwner::UnknownExternal;
    }
    RoutingOwner::UnknownExternal
}

pub(in crate::gateway) fn owner_label(owner: RoutingOwner) -> &'static str {
    match owner {
        RoutingOwner::Official => "Official",
        RoutingOwner::Release => "Release",
        RoutingOwner::Beta => "Beta",
        RoutingOwner::UnknownExternal => "Unknown external",
    }
}

pub(in crate::gateway) fn ensure_route_owner_mutation_allowed(
    current_app_owner: RoutingOwner,
    current_target_owner: RoutingOwner,
    force_takeover: bool,
) -> Result<(), String> {
    crate::routing_owner::permit(
        current_app_owner,
        Some(current_target_owner),
        crate::routing_owner::MutationKind::ManagedClient,
        force_takeover,
    )
    .map_err(|error| {
        format!(
            "{}: Managed by {}; explicit takeover is required before changing this target.",
            error.code(),
            owner_label(current_target_owner)
        )
    })
}

pub(in crate::gateway) fn provider_entry_base_url(entry: &Value) -> Option<&str> {
    entry
        .get("baseURL")
        .and_then(Value::as_str)
        .or_else(|| entry.get("baseUrl").and_then(Value::as_str))
        .or_else(|| entry.pointer("/options/baseURL").and_then(Value::as_str))
        .or_else(|| entry.pointer("/options/baseUrl").and_then(Value::as_str))
        .or_else(|| entry.pointer("/endpoints/baseURL").and_then(Value::as_str))
        .or_else(|| entry.pointer("/endpoints/baseUrl").and_then(Value::as_str))
}

pub(in crate::gateway) fn provider_entry_api_key(entry: &Value) -> Option<&str> {
    entry
        .get("apiKey")
        .and_then(Value::as_str)
        .or_else(|| entry.pointer("/options/apiKey").and_then(Value::as_str))
}

pub(in crate::gateway) fn provider_entry_has_gateway_path(entry: &Value) -> bool {
    provider_entry_base_url(entry).is_some_and(|url| {
        let url = url.trim_end_matches('/');
        url.contains("/v1/providers/") || url.ends_with("/v1")
    }) || entry
        .pointer("/endpoints/paths/openai-compatible")
        .and_then(Value::as_str)
        .is_some_and(|path| path == "/v1/chat/completions" || path.starts_with("/v1/providers/"))
}

pub(in crate::gateway) fn provider_entry_has_codexhub_name(entry: &Value) -> bool {
    entry
        .get("name")
        .and_then(Value::as_str)
        .is_some_and(|name| name == "CodexHub Gateway" || name.starts_with("CodexHub "))
}

pub(in crate::gateway) fn is_legacy_codexhub_chatgpt_sub_provider_entry(
    provider_id: &str,
    entry: &Value,
) -> bool {
    if provider_id != "openai-chatgpt-sub" {
        return false;
    }
    if !provider_entry_base_url(entry).is_some_and(is_local_gateway_url) {
        return false;
    }
    provider_entry_has_gateway_path(entry)
        || provider_entry_api_key(entry).is_some_and(|api_key| {
            matches!(
                api_key,
                "codexhub-proxy" | "__zcode_cached_api_key_present__"
            )
        })
}

pub(in crate::gateway) fn is_managed_codexhub_provider_entry(
    provider_id: &str,
    entry: &Value,
) -> bool {
    if is_legacy_codexhub_chatgpt_sub_provider_entry(provider_id, entry) {
        return true;
    }
    if !is_codexhub_client_provider_id(provider_id) {
        return false;
    }
    if provider_id == "codexhub" && provider_entry_has_codexhub_name(entry) {
        return true;
    }
    if is_builtin_codexhub_client_provider_id(provider_id)
        && provider_entry_has_codexhub_name(entry)
        && provider_entry_base_url(entry).is_none()
    {
        return true;
    }
    let local_gateway = provider_entry_base_url(entry).is_some_and(is_local_gateway_url);
    if !local_gateway {
        return false;
    }
    provider_entry_has_gateway_path(entry)
        || provider_entry_has_codexhub_name(entry)
        || entry
            .get("api")
            .and_then(Value::as_str)
            .is_some_and(|api| matches!(api, "openai-completions" | "openai-responses"))
        || entry
            .get("kind")
            .and_then(Value::as_str)
            .is_some_and(|kind| kind == "openai-compatible")
}

pub(in crate::gateway) fn remove_codexhub_client_provider_entries(
    providers: &mut Map<String, Value>,
) -> bool {
    let keys = providers
        .iter()
        .filter(|(key, value)| is_managed_codexhub_provider_entry(key, value))
        .map(|(key, _)| key.clone())
        .collect::<Vec<_>>();
    let removed = !keys.is_empty();
    for key in keys {
        providers.remove(&key);
    }
    removed
}

pub(in crate::gateway) fn gateway_provider_path_segment(provider_id: &str) -> String {
    let mut output = String::new();
    for byte in provider_id.as_bytes() {
        match *byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => {
                output.push(*byte as char);
            }
            _ => output.push_str(&format!("%{byte:02X}")),
        }
    }
    output
}

pub(in crate::gateway) fn gateway_client_provider_base_url(
    settings: &Settings,
    provider_id: &str,
) -> String {
    format!(
        "{}/providers/{}",
        endpoints(settings.proxy_port)
            .base_url
            .trim_end_matches('/'),
        gateway_provider_path_segment(provider_id)
    )
}

pub(in crate::gateway) fn gateway_client_provider_chat_path(provider_id: &str) -> String {
    format!(
        "/v1/providers/{}/chat/completions",
        gateway_provider_path_segment(provider_id)
    )
}

pub(in crate::gateway) fn gateway_client_provider_responses_path(provider_id: &str) -> String {
    format!(
        "/v1/providers/{}/responses",
        gateway_provider_path_segment(provider_id)
    )
}

pub(in crate::gateway) fn gateway_client_provider_endpoint_selection(
    provider_id: &str,
    providers: &[Provider],
) -> GatewayClientEndpointSelection {
    if provider_id == "openai" {
        return GatewayClientEndpointSelection::Responses;
    }
    let Some(provider) = providers.iter().find(|provider| provider.id == provider_id) else {
        return GatewayClientEndpointSelection::ChatCompletions;
    };
    match provider.upstream_format.as_ref() {
        Some(UpstreamFormat::Responses) => GatewayClientEndpointSelection::Responses,
        Some(UpstreamFormat::ChatCompletions) => GatewayClientEndpointSelection::ChatCompletions,
        Some(UpstreamFormat::AnthropicMessages) => {
            GatewayClientEndpointSelection::AnthropicMessages
        }
        Some(UpstreamFormat::Auto) | None => provider
            .available_upstream_formats
            .as_ref()
            .and_then(|formats| {
                if formats
                    .iter()
                    .any(|format| matches!(format, UpstreamFormat::Responses))
                {
                    Some(GatewayClientEndpointSelection::Responses)
                } else if formats
                    .iter()
                    .any(|format| matches!(format, UpstreamFormat::ChatCompletions))
                {
                    Some(GatewayClientEndpointSelection::ChatCompletions)
                } else if formats
                    .iter()
                    .any(|format| matches!(format, UpstreamFormat::AnthropicMessages))
                {
                    Some(GatewayClientEndpointSelection::AnthropicMessages)
                } else {
                    None
                }
            })
            .unwrap_or(GatewayClientEndpointSelection::ChatCompletions),
    }
}

pub(in crate::gateway) fn gateway_client_provider_supports_developer_role(
    provider_id: &str,
    providers: &[Provider],
) -> bool {
    providers
        .iter()
        .find(|provider| provider.id == provider_id)
        .and_then(|provider| provider.supports_developer_role)
        .unwrap_or(true)
}

pub(in crate::gateway) fn gateway_client_provider_label(
    provider_id: &str,
    providers: &[Provider],
) -> String {
    if provider_id == "openai" {
        return "OpenAI".to_string();
    }
    if let Some(provider) = providers.iter().find(|provider| provider.id == provider_id) {
        let name = provider.name.trim();
        if !name.is_empty() {
            return name.to_string();
        }
    }
    provider_id
        .split(['-', '_'])
        .filter(|part| !part.is_empty())
        .map(|part| {
            let mut chars = part.chars();
            match chars.next() {
                Some(first) => format!("{}{}", first.to_ascii_uppercase(), chars.as_str()),
                None => String::new(),
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

pub(in crate::gateway) fn gateway_client_provider_groups(
    settings: &Settings,
    providers: &[Provider],
    default_model: &str,
) -> Result<GatewayClientProviderGroups, String> {
    let exported_models = gateway_models_from_config(settings, providers);
    gateway_client_provider_groups_from_exported(
        settings,
        providers,
        default_model,
        exported_models,
    )
}

pub(in crate::gateway) fn gateway_client_provider_groups_from_exported(
    settings: &Settings,
    providers: &[Provider],
    default_model: &str,
    exported_models: Vec<GatewayModel>,
) -> Result<GatewayClientProviderGroups, String> {
    let default_model =
        resolve_gateway_client_model_id_from_exported(&exported_models, providers, default_model)?;
    let (default_provider_id, default_model_id) = split_gateway_model_id(&default_model);
    let default_client_provider_id = codexhub_client_provider_id(&default_provider_id);
    let default_selector = format!("{default_client_provider_id}/{default_model_id}");
    let mut groups = Vec::<GatewayClientProviderGroup>::new();
    let mut group_indices = HashMap::<String, usize>::new();

    for model in gateway_client_models_from_exported(providers, &default_model, exported_models)? {
        let (provider_id, short_id) = split_gateway_model_id(&model.id);
        let group_index = if let Some(index) = group_indices.get(&provider_id) {
            *index
        } else {
            let label = gateway_client_provider_label(&provider_id, providers);
            let endpoint_selection =
                gateway_client_provider_endpoint_selection(&provider_id, providers);
            let index = groups.len();
            group_indices.insert(provider_id.clone(), index);
            groups.push(GatewayClientProviderGroup {
                client_provider_id: codexhub_client_provider_id(&provider_id),
                display_name: format!("CodexHub {label}"),
                base_url: gateway_client_provider_base_url(settings, &provider_id),
                endpoint_selection,
                responses_path: gateway_client_provider_responses_path(&provider_id),
                chat_completions_path: gateway_client_provider_chat_path(&provider_id),
                supports_developer_role: gateway_client_provider_supports_developer_role(
                    &provider_id,
                    providers,
                ),
                models: Vec::new(),
            });
            index
        };
        let group = &mut groups[group_index];
        if group.models.iter().any(|existing| existing.id == short_id) {
            continue;
        }
        group.models.push(GatewayClientProviderModel {
            id: short_id,
            display_name: model.display_name,
            context_window: model.context_window,
            input_modalities: model
                .input_modalities
                .unwrap_or_else(|| vec!["text".to_string()]),
            supported_reasoning_levels: model.supported_reasoning_levels.unwrap_or_default(),
            default_reasoning_level: model.default_reasoning_level,
        });
    }

    Ok(GatewayClientProviderGroups {
        default_provider_id,
        default_model_id,
        default_selector,
        providers: groups,
    })
}

pub(in crate::gateway) fn gateway_client_model_selector(
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<String, String> {
    gateway_client_provider_groups(settings, providers, model).map(|groups| groups.default_selector)
}

pub(in crate::gateway) fn sanitize_text(text: &str) -> String {
    let mut output = text.chars().take(1400).collect::<String>();
    let lower = output.to_ascii_lowercase();
    if lower.contains("authorization")
        || lower.contains("access_token")
        || lower.contains("refresh_token")
        || lower.contains("api_key")
        || lower.contains("apikey")
        || lower.contains("bearer ")
    {
        output = "[redacted sensitive response detail]".to_string();
    }
    output
}

pub(in crate::gateway) fn normalize_client_id(client_id: &str) -> String {
    client_id.trim().to_ascii_lowercase().replace('_', "-")
}

pub(in crate::gateway) fn route_mode_from_text_file(
    path: &Path,
    is_hub: fn(&str) -> bool,
) -> &'static str {
    fs::read_to_string(path)
        .ok()
        .map(|text| if is_hub(&text) { "hub" } else { "official" })
        .unwrap_or("unknown")
}

pub(in crate::gateway) fn route_mode_for_owner(
    owner: RoutingOwner,
    current: RoutingOwner,
    stale: bool,
) -> &'static str {
    if stale {
        return "stale";
    }
    match owner {
        RoutingOwner::Official => "official",
        RoutingOwner::Release | RoutingOwner::Beta if owner == current => "hub",
        RoutingOwner::Release | RoutingOwner::Beta => "other_channel",
        RoutingOwner::UnknownExternal => "unknown",
    }
}

pub(in crate::gateway) fn pending_sync_is_stale(
    pending: bool,
    detected_owner: RoutingOwner,
    current_owner: RoutingOwner,
) -> bool {
    pending
        && !matches!(
            detected_owner,
            RoutingOwner::Release | RoutingOwner::Beta if detected_owner != current_owner
        )
}

pub(in crate::gateway) fn route_owner_from_endpoint(
    endpoint: Option<&str>,
    managed: bool,
    existing_config: bool,
    current_owner: RoutingOwner,
    current_port: u16,
) -> RoutingOwner {
    if !existing_config {
        return RoutingOwner::UnknownExternal;
    }
    let Some(endpoint) = endpoint else {
        return if managed {
            current_owner
        } else {
            RoutingOwner::Official
        };
    };
    let owner = routing_owner_from_gateway_url(endpoint);
    if owner != RoutingOwner::UnknownExternal {
        return owner;
    }
    let trimmed = endpoint.trim().trim_end_matches('/');
    if trimmed.starts_with(&format!("http://127.0.0.1:{current_port}"))
        || trimmed.starts_with(&format!("http://localhost:{current_port}"))
    {
        return current_owner;
    }
    if is_local_gateway_url(trimmed) {
        return if managed {
            current_owner
        } else {
            RoutingOwner::UnknownExternal
        };
    }
    if managed {
        current_owner
    } else {
        RoutingOwner::Official
    }
}

pub(in crate::gateway) fn first_provider_base_url_from_object(
    providers: &serde_json::Map<String, Value>,
) -> Option<String> {
    providers
        .values()
        .find_map(provider_entry_base_url)
        .map(ToOwned::to_owned)
}

pub(in crate::gateway) fn managed_provider_base_url_from_object(
    providers: &serde_json::Map<String, Value>,
) -> Option<Option<String>> {
    providers
        .iter()
        .find(|(provider_id, entry)| is_managed_codexhub_provider_entry(provider_id, entry))
        .map(|(_, entry)| provider_entry_base_url(entry).map(ToOwned::to_owned))
}

pub(in crate::gateway) fn first_provider_base_url_from_json_object_text(
    text: &str,
    pointer: &str,
) -> Option<String> {
    let value = serde_json::from_str::<Value>(text).ok()?;
    value
        .pointer(pointer)
        .and_then(Value::as_object)
        .and_then(first_provider_base_url_from_object)
}

pub(in crate::gateway) fn managed_provider_base_url_from_json_object_text(
    text: &str,
    pointer: &str,
) -> Option<Option<String>> {
    let value = serde_json::from_str::<Value>(text).ok()?;
    value
        .pointer(pointer)
        .and_then(Value::as_object)
        .and_then(managed_provider_base_url_from_object)
}

pub(in crate::gateway) fn first_provider_base_url_from_json_array_text(
    text: &str,
    pointer: &str,
) -> Option<String> {
    let value = serde_json::from_str::<Value>(text).ok()?;
    value
        .pointer(pointer)
        .and_then(Value::as_array)
        .and_then(|providers| providers.iter().find_map(provider_entry_base_url))
        .map(ToOwned::to_owned)
}

pub(in crate::gateway) fn managed_provider_base_url_from_json_array_text(
    text: &str,
    pointer: &str,
) -> Option<Option<String>> {
    let value = serde_json::from_str::<Value>(text).ok()?;
    value
        .pointer(pointer)
        .and_then(Value::as_array)
        .and_then(|providers| {
            providers
                .iter()
                .find(|provider| is_managed_zcode_catalog_provider_entry(provider))
                .map(|provider| provider_entry_base_url(provider).map(ToOwned::to_owned))
        })
}

pub(in crate::gateway) fn detect_route_details_from_json_provider_object(
    text: &str,
    pointer: &str,
    managed: bool,
    existing_config: bool,
    current_owner: RoutingOwner,
    current_port: u16,
) -> (RoutingOwner, Option<String>) {
    let route_endpoint = managed_provider_base_url_from_json_object_text(text, pointer)
        .unwrap_or_else(|| first_provider_base_url_from_json_object_text(text, pointer));
    let route_owner = route_owner_from_endpoint(
        route_endpoint.as_deref(),
        managed,
        existing_config,
        current_owner,
        current_port,
    );
    (route_owner, route_endpoint)
}

pub(in crate::gateway) fn detect_route_details_from_json_provider_array(
    text: &str,
    pointer: &str,
    managed: bool,
    existing_config: bool,
    current_owner: RoutingOwner,
    current_port: u16,
) -> (RoutingOwner, Option<String>) {
    let route_endpoint = managed_provider_base_url_from_json_array_text(text, pointer)
        .unwrap_or_else(|| first_provider_base_url_from_json_array_text(text, pointer));
    let route_owner = route_owner_from_endpoint(
        route_endpoint.as_deref(),
        managed,
        existing_config,
        current_owner,
        current_port,
    );
    (route_owner, route_endpoint)
}

pub(in crate::gateway) fn gateway_exported_model_supports_image(
    settings: &Settings,
    providers: &[Provider],
    resolved_model_id: &str,
) -> bool {
    gateway_models_from_config(settings, providers)
        .iter()
        .find(|model| model.id == resolved_model_id)
        .and_then(|model| model.input_modalities.as_ref())
        .is_some_and(|modalities| modalities.iter().any(|modality| modality == "image"))
}

pub(in crate::gateway) fn gateway_exported_model_default_reasoning_effort(
    settings: &Settings,
    providers: &[Provider],
    resolved_model_id: &str,
) -> Option<String> {
    gateway_models_from_config(settings, providers)
        .iter()
        .find(|model| model.id == resolved_model_id)
        .and_then(|model| {
            let levels = model.supported_reasoning_levels.as_ref()?;
            let default = model.default_reasoning_level.as_ref()?;
            levels
                .iter()
                .any(|level| level == default)
                .then(|| default.clone())
        })
}

pub(in crate::gateway) fn read_json_file_or_empty(
    path: &Path,
    label: &str,
) -> Result<Value, String> {
    if !path.exists() {
        return Ok(json!({}));
    }
    let text = fs::read_to_string(path)
        .map_err(|error| format!("failed to read {label} {}: {error}", path.display()))?;
    serde_json::from_str::<Value>(&text)
        .map_err(|error| format!("failed to parse {label} {}: {error}", path.display()))
}

pub(in crate::gateway) fn gateway_base_without_v1(settings: &Settings) -> String {
    let base_url = endpoints(settings.proxy_port).base_url;
    base_url
        .strip_suffix("/v1")
        .unwrap_or(base_url.as_str())
        .to_string()
}

pub(in crate::gateway) fn yaml_scalar(value: &str) -> String {
    // Always emit a double-quoted scalar. YAML double-quoted strings accept
    // JSON escaping, so every value round-trips as a string even when it
    // resembles a number, boolean, null, timestamp, anchor, alias, or
    // collection — a lexical allowlist cannot prove any of those.
    serde_json::to_string(value).unwrap_or_else(|_| "\"\"".to_string())
}

pub(in crate::gateway) fn is_top_level_yaml_key(line: &str, key: &str) -> bool {
    let trimmed = line.trim();
    !line.starts_with(' ')
        && !line.starts_with('\t')
        && trimmed
            .strip_suffix(':')
            .or_else(|| trimmed.split_once(':').map(|(name, _)| name))
            .is_some_and(|name| name == key)
}

pub(in crate::gateway) fn is_any_top_level_yaml_key(line: &str) -> bool {
    let trimmed = line.trim();
    !trimmed.is_empty()
        && !line.starts_with(' ')
        && !line.starts_with('\t')
        && trimmed.contains(':')
}

pub(in crate::gateway) fn write_text_replace(path: &Path, text: &str) -> Result<(), String> {
    safe_file::write_text_atomic(path, text)
}

pub(in crate::gateway) fn timestamp_millis() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or_default()
}
