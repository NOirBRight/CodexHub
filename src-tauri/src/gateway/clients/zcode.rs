use super::super::{
    gateway_base_without_v1, gateway_client_provider_groups, read_json_file_or_empty,
    remove_codexhub_client_provider_entries, timestamp_millis, GatewayClientEndpointSelection,
    GatewayClientProviderGroup, GatewayClientProviderModel,
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

use super::super::{
    combined_current_preview, combined_named_text, create_snapshot_backup,
    detect_route_details_from_json_provider_array, detect_route_details_from_json_provider_object,
    is_builtin_codexhub_client_provider_id, is_exact_semver_like, is_local_gateway_url,
    is_managed_codexhub_provider_entry, latest_clean_snapshot_backup, provider_entry_base_url,
    provider_entry_has_codexhub_name, provider_entry_has_gateway_path,
    resolve_gateway_client_model_id, route_mode_from_text_file, sanitize_text, windows_app_path,
    write_text_replace, GatewayClientApplyResult, GatewayClientConfigPreview,
    GatewayClientProviderGroups, IsolatedClientApplyTargets,
};
use crate::app_flavor::RoutingOwner;
use reqwest::blocking::Client;
use std::path::PathBuf;
use std::time::Duration;

#[derive(Debug, Clone)]
pub(in crate::gateway) struct ZcodeConfigTargets {
    pub(in crate::gateway) catalog_path: PathBuf,
    pub(in crate::gateway) v2_config_path: PathBuf,
    pub(in crate::gateway) v2_cache_path: PathBuf,
}

#[derive(Debug, Clone, Copy)]
pub(in crate::gateway) enum ZcodeProviderFileKind {
    Catalog,
    V2Cache,
}

pub(in crate::gateway) fn detect_zcode_route_details(
    targets: &ZcodeConfigTargets,
    current_owner: RoutingOwner,
    current_port: u16,
) -> (RoutingOwner, Option<String>) {
    let v2_text = fs::read_to_string(&targets.v2_config_path).ok();
    if let Some(text) = v2_text.as_deref() {
        return detect_route_details_from_json_provider_object(
            text,
            "/provider",
            is_zcode_v2_codexhub_config(text),
            true,
            current_owner,
            current_port,
        );
    }
    if let Ok(text) = fs::read_to_string(&targets.catalog_path) {
        return detect_route_details_from_json_provider_array(
            &text,
            "/providers",
            is_zcode_codexhub_config(&text),
            true,
            current_owner,
            current_port,
        );
    }
    if let Ok(text) = fs::read_to_string(&targets.v2_cache_path) {
        return detect_route_details_from_json_provider_array(
            &text,
            "/providers",
            is_zcode_codexhub_config(&text),
            true,
            current_owner,
            current_port,
        );
    }
    (RoutingOwner::UnknownExternal, None)
}

pub(in crate::gateway) fn zcode_route_mode(targets: &ZcodeConfigTargets) -> &'static str {
    if targets.v2_config_path.exists() {
        return route_mode_from_text_file(&targets.v2_config_path, is_zcode_v2_codexhub_config);
    }
    route_mode_from_text_file(&targets.catalog_path, is_zcode_codexhub_config)
}

pub(in crate::gateway) fn zcode_route_mode_with_expected(
    targets: &ZcodeConfigTargets,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> &'static str {
    let route_mode = zcode_route_mode(targets);
    if route_mode != "hub" {
        return route_mode;
    }
    match zcode_targets_match_expected(targets, settings, providers, model) {
        Ok(true) => "hub",
        Ok(false) | Err(_) => "stale",
    }
}

pub(in crate::gateway) fn zcode_targets_match_expected(
    targets: &ZcodeConfigTargets,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<bool, String> {
    let groups = gateway_client_provider_groups(settings, providers, model)?;
    Ok(
        zcode_v2_config_matches_expected(&targets.v2_config_path, &groups)
            && zcode_catalog_matches_expected(
                &targets.catalog_path,
                settings,
                &groups,
                ZcodeProviderFileKind::Catalog,
            )
            && zcode_catalog_matches_expected(
                &targets.v2_cache_path,
                settings,
                &groups,
                ZcodeProviderFileKind::V2Cache,
            ),
    )
}

pub(in crate::gateway) fn zcode_v2_config_matches_expected(
    path: &Path,
    groups: &GatewayClientProviderGroups,
) -> bool {
    let Ok(text) = fs::read_to_string(path) else {
        return false;
    };
    let Ok(value) = serde_json::from_str::<Value>(&text) else {
        return false;
    };
    let Some(providers) = value.get("provider").and_then(Value::as_object) else {
        return false;
    };
    groups.providers.iter().all(|group| {
        providers
            .get(&group.client_provider_id)
            .is_some_and(|provider| zcode_v2_provider_matches_expected(provider, group))
    })
}

pub(in crate::gateway) fn zcode_v2_provider_matches_expected(
    provider: &Value,
    group: &GatewayClientProviderGroup,
) -> bool {
    let kind = group.endpoint_selection.zcode_kind();
    let (expected_base_url, expected_path) =
        zcode_provider_endpoint(&Settings::default(), group, ZcodeProviderFileKind::V2Cache);
    let api_format_matches = provider
        .get("apiFormat")
        .and_then(Value::as_str)
        .map(|value| value == group.endpoint_selection.zcode_api_format())
        .unwrap_or(true);
    let endpoints_match = if provider.get("endpoints").is_some() {
        provider
            .pointer("/endpoints/baseURL")
            .and_then(Value::as_str)
            .is_some_and(|value| value == expected_base_url)
            && provider
                .pointer(&format!("/endpoints/paths/{kind}"))
                .and_then(Value::as_str)
                .is_some_and(|value| value == expected_path)
    } else {
        true
    };
    provider
        .get("kind")
        .and_then(Value::as_str)
        .is_some_and(|value| value == kind)
        && api_format_matches
        && endpoints_match
        && provider
            .pointer("/options/baseURL")
            .and_then(Value::as_str)
            .is_some_and(|value| value == group.base_url.as_str())
}

pub(in crate::gateway) fn zcode_catalog_matches_expected(
    path: &Path,
    settings: &Settings,
    groups: &GatewayClientProviderGroups,
    file_kind: ZcodeProviderFileKind,
) -> bool {
    let Ok(text) = fs::read_to_string(path) else {
        return false;
    };
    let Ok(value) = serde_json::from_str::<Value>(&text) else {
        return false;
    };
    let Some(providers) = value.get("providers").and_then(Value::as_array) else {
        return false;
    };
    groups.providers.iter().all(|group| {
        providers
            .iter()
            .find(|provider| {
                provider
                    .get("id")
                    .and_then(Value::as_str)
                    .is_some_and(|id| id == group.client_provider_id)
            })
            .is_some_and(|provider| {
                zcode_catalog_provider_matches_expected(provider, settings, group, file_kind)
            })
    })
}

pub(in crate::gateway) fn zcode_catalog_provider_matches_expected(
    provider: &Value,
    settings: &Settings,
    group: &GatewayClientProviderGroup,
    file_kind: ZcodeProviderFileKind,
) -> bool {
    let (expected_base_url, expected_path) = zcode_provider_endpoint(settings, group, file_kind);
    let kind = group.endpoint_selection.zcode_kind();
    let path_pointer = format!("/endpoints/paths/{kind}");
    provider
        .get("apiFormat")
        .and_then(Value::as_str)
        .is_some_and(|value| value == group.endpoint_selection.zcode_api_format())
        && provider
            .get("defaultKind")
            .and_then(Value::as_str)
            .is_some_and(|value| value == kind)
        && provider
            .pointer("/endpoints/baseURL")
            .and_then(Value::as_str)
            .is_some_and(|value| value == expected_base_url)
        && provider
            .pointer(&path_pointer)
            .and_then(Value::as_str)
            .is_some_and(|value| value == expected_path)
}

pub(in crate::gateway) fn zcode_app_data_root() -> PathBuf {
    #[cfg(windows)]
    {
        return std::env::var_os("APPDATA")
            .filter(|value| !value.is_empty())
            .map(PathBuf::from)
            .map(|path| path.join("ZCode"))
            .unwrap_or_else(|| PathBuf::from("%APPDATA%/ZCode"));
    }

    dirs::home_dir()
        .map(|home| home.join(".zcode"))
        .unwrap_or_else(|| PathBuf::from("~/.zcode"))
}

pub(in crate::gateway) fn detect_zcode_config_path() -> PathBuf {
    if let Some(path) = std::env::var_os("CODEXHUB_ZCODE_CONFIG")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
    {
        return path;
    }
    zcode_app_data_root()
        .join("model-providers")
        .join("codexhub.json")
}

pub(in crate::gateway) fn detect_zcode_config_targets() -> ZcodeConfigTargets {
    let catalog_override = std::env::var_os("CODEXHUB_ZCODE_CONFIG")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from);
    let catalog_path = catalog_override
        .clone()
        .unwrap_or_else(detect_zcode_config_path);
    let v2_root = std::env::var_os("CODEXHUB_ZCODE_V2_DIR")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .or_else(|| {
            catalog_override
                .as_ref()
                .and_then(|path| zcode_v2_root_from_catalog_path(path))
        })
        .or_else(|| zcode_v2_root_from_settings_path(&default_zcode_settings_path()))
        .unwrap_or_else(default_zcode_v2_root);
    ZcodeConfigTargets {
        catalog_path,
        v2_config_path: v2_root.join("config.json"),
        v2_cache_path: v2_root.join("bots-model-cache.v2.json"),
    }
}

pub(in crate::gateway) fn default_zcode_v2_root() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("~"))
        .join(".zcode")
        .join("v2")
}

pub(in crate::gateway) fn default_zcode_settings_path() -> PathBuf {
    default_zcode_v2_root().join("setting.json")
}

pub(in crate::gateway) fn zcode_v2_root_from_catalog_path(catalog_path: &Path) -> Option<PathBuf> {
    catalog_path.parent()?.parent().map(|root| root.join("v2"))
}

pub(in crate::gateway) fn zcode_v2_root_from_settings_path(
    settings_path: &Path,
) -> Option<PathBuf> {
    let text = fs::read_to_string(settings_path).ok()?;
    let value = serde_json::from_str::<Value>(&text).ok()?;
    let data_base_dir = value
        .get("dataBaseDir")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())?;
    Some(zcode_v2_root_from_data_base_dir(&PathBuf::from(
        data_base_dir,
    )))
}

pub(in crate::gateway) fn zcode_v2_root_from_data_base_dir(data_base_dir: &Path) -> PathBuf {
    if data_base_dir
        .file_name()
        .and_then(|value| value.to_str())
        .is_some_and(|value| value.eq_ignore_ascii_case("v2"))
    {
        return data_base_dir.to_path_buf();
    }
    if data_base_dir
        .file_name()
        .and_then(|value| value.to_str())
        .is_some_and(|value| value.eq_ignore_ascii_case(".zcode"))
    {
        return data_base_dir.join("v2");
    }
    data_base_dir.join(".zcode").join("v2")
}

pub(in crate::gateway) fn detect_zcode_store_path() -> PathBuf {
    zcode_app_data_root()
        .join("rum-electron-store")
        .join("ZGVmYXVsdA.json")
}

pub(in crate::gateway) fn detect_zcode_executable_path() -> Option<PathBuf> {
    let mut candidates = Vec::new();
    if let Some(path) = std::env::var_os("CODEXHUB_ZCODE_EXE")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
    {
        candidates.push(path);
    }
    if let Ok(path) = which::which("zcode") {
        candidates.push(path);
    }
    if let Ok(path) = which::which("ZCode") {
        candidates.push(path);
    }
    if let Ok(path) = which::which("ZCode.exe") {
        candidates.push(path);
    }
    candidates.push(std::path::PathBuf::from("/opt/ZCode/zcode"));
    if let Some(path) = windows_app_path("ZCode.exe") {
        candidates.push(path);
    }
    if let Some(program_files) = std::env::var_os("ProgramFiles").map(PathBuf::from) {
        candidates.push(program_files.join("ZCode").join("ZCode.exe"));
    }
    if let Some(program_files_x86) = std::env::var_os("ProgramFiles(x86)").map(PathBuf::from) {
        candidates.push(program_files_x86.join("ZCode").join("ZCode.exe"));
    }
    if let Some(system_drive) = std::env::var_os("SystemDrive").map(PathBuf::from) {
        candidates.push(
            system_drive
                .join("Program Files")
                .join("ZCode")
                .join("ZCode.exe"),
        );
    }
    if let Some(local_appdata) = std::env::var_os("LOCALAPPDATA").map(PathBuf::from) {
        candidates.push(
            local_appdata
                .join("Programs")
                .join("ZCode")
                .join("ZCode.exe"),
        );
    }
    candidates.into_iter().find(|path| path.exists())
}

pub(in crate::gateway) fn zcode_latest_version() -> Option<String> {
    let client = Client::builder()
        .timeout(Duration::from_secs(8))
        .user_agent("Mozilla/5.0 CodexHub/0.1")
        .build()
        .ok()?;
    let text = client
        .get("https://zcode.z.ai/en/changelog")
        .send()
        .ok()?
        .error_for_status()
        .ok()?
        .text()
        .ok()?;
    zcode_changelog_release_version(&text)
}

pub(in crate::gateway) fn zcode_changelog_release_version(text: &str) -> Option<String> {
    let marker = "font-mono text-sm\">";
    let mut offset = 0;
    while let Some(relative_start) = text[offset..].find(marker) {
        let start = offset + relative_start + marker.len();
        let end = start + text[start..].find('<')?;
        let candidate = text[start..end].trim();
        if is_exact_semver_like(candidate) {
            return Some(candidate.to_string());
        }
        offset = end;
    }
    None
}

pub(in crate::gateway) fn preview_zcode_config_with_targets(
    targets: &ZcodeConfigTargets,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<GatewayClientConfigPreview, String> {
    let model = resolve_gateway_client_model_id(settings, providers, model)?;
    let current = combined_current_preview(&[
        ("config.json", &targets.v2_config_path),
        ("codexhub.json", &targets.catalog_path),
        ("bots-model-cache.v2.json", &targets.v2_cache_path),
    ])
    .map(|text| sanitize_text(&text));
    let next_config = zcode_v2_config_text(&targets.v2_config_path, settings, providers, &model)?;
    let next_catalog = zcode_catalog_text(settings, providers, &model)?;
    let next_cache = zcode_v2_cache_text(settings, providers, &model)?;
    Ok(GatewayClientConfigPreview {
        client_id: "zcode".to_string(),
        can_apply: true,
        strategy: "managed_native_config".to_string(),
        config_path: Some(targets.v2_config_path.clone()),
        current_redacted: current,
        next_redacted: sanitize_text(&combined_named_text(&[
            ("config.json", &next_config),
            ("codexhub.json", &next_catalog),
            ("bots-model-cache.v2.json", &next_cache),
        ])),
        backup_required: targets.v2_config_path.exists()
            || targets.catalog_path.exists()
            || targets.v2_cache_path.exists(),
        message:
            "Apply will snapshot ZCode v2 config/cache/catalog, then route ZCode through CodexHub Gateway."
                .to_string(),
    })
}

pub(in crate::gateway) fn apply_zcode_config_with_targets(
    targets: &ZcodeConfigTargets,
    backup_root: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<GatewayClientApplyResult, String> {
    let model = resolve_gateway_client_model_id(settings, providers, model)?;
    let backup_path = create_snapshot_backup(
        "zcode",
        backup_root,
        &zcode_target_files(targets),
        zcode_targets_current_is_managed(targets),
    )?;
    let next_catalog = zcode_catalog_text(settings, providers, &model)?;
    let next_cache = zcode_v2_cache_text(settings, providers, &model)?;
    let next_config = zcode_v2_config_text(&targets.v2_config_path, settings, providers, &model)?;
    write_text_replace(&targets.catalog_path, &next_catalog)?;
    write_text_replace(&targets.v2_config_path, &next_config)?;
    write_text_replace(&targets.v2_cache_path, &next_cache)?;
    Ok(GatewayClientApplyResult {
        client_id: "zcode".to_string(),
        applied: true,
        config_path: Some(targets.v2_config_path.clone()),
        backup_path,
        message: "ZCode now routes through CodexHub Gateway.".to_string(),
    })
}

pub(in crate::gateway) fn restore_zcode_config_with_targets(
    targets: &ZcodeConfigTargets,
    backup_root: &Path,
) -> Result<GatewayClientApplyResult, String> {
    let latest = latest_clean_snapshot_backup("zcode", backup_root, |path| {
        zcode_snapshot_contains_managed(path)
    });
    match latest {
        Ok(path) => {
            restore_zcode_sanitized_snapshot_files(&path, targets)?;
            Ok(GatewayClientApplyResult {
                client_id: "zcode".to_string(),
                applied: true,
                config_path: Some(targets.v2_config_path.clone()),
                backup_path: Some(path),
                message: "ZCode official config restored.".to_string(),
            })
        }
        Err(clean_error) => {
            if let Ok(path) = latest_zcode_official_config_snapshot(backup_root) {
                restore_zcode_sanitized_snapshot_files(&path, targets)?;
                return Ok(GatewayClientApplyResult {
                    client_id: "zcode".to_string(),
                    applied: true,
                    config_path: Some(targets.v2_config_path.clone()),
                    backup_path: Some(path),
                    message: "ZCode official config restored.".to_string(),
                });
            }
            if !zcode_targets_contain_managed(targets) {
                return Err(clean_error);
            }
            let mut removed_any = false;
            if targets.catalog_path.exists()
                && is_zcode_codexhub_config(
                    &fs::read_to_string(&targets.catalog_path).unwrap_or_default(),
                )
            {
                fs::remove_file(&targets.catalog_path).map_err(|error| {
                    format!(
                        "failed to remove ZCode CodexHub catalog {}: {error}",
                        targets.catalog_path.display()
                    )
                })?;
                removed_any = true;
            }
            if targets.v2_cache_path.exists()
                && is_zcode_codexhub_config(
                    &fs::read_to_string(&targets.v2_cache_path).unwrap_or_default(),
                )
            {
                fs::remove_file(&targets.v2_cache_path).map_err(|error| {
                    format!(
                        "failed to remove ZCode CodexHub cache {}: {error}",
                        targets.v2_cache_path.display()
                    )
                })?;
                removed_any = true;
            }
            removed_any |= remove_zcode_v2_codexhub_provider(&targets.v2_config_path)?;
            removed_any |= remove_zcode_coding_plan_cache(targets)?;
            Ok(GatewayClientApplyResult {
                client_id: "zcode".to_string(),
                applied: true,
                config_path: Some(targets.v2_config_path.clone()),
                backup_path: None,
                message: if removed_any {
                    "ZCode CodexHub config removed.".to_string()
                } else {
                    "ZCode CodexHub config was already absent.".to_string()
                },
            })
        }
    }
}

pub(in crate::gateway) fn zcode_target_files(
    targets: &ZcodeConfigTargets,
) -> [(&'static str, &Path); 3] {
    [
        ("codexhub.json", targets.catalog_path.as_path()),
        ("config.json", targets.v2_config_path.as_path()),
        ("bots-model-cache.v2.json", targets.v2_cache_path.as_path()),
    ]
}

pub(in crate::gateway) fn zcode_targets_current_is_managed(targets: &ZcodeConfigTargets) -> bool {
    let mut saw_existing = false;
    let mut all_managed = true;
    if targets.catalog_path.exists() {
        saw_existing = true;
        all_managed &= is_zcode_codexhub_config(
            &fs::read_to_string(&targets.catalog_path).unwrap_or_default(),
        );
    }
    if targets.v2_config_path.exists() {
        saw_existing = true;
        all_managed &= is_zcode_v2_codexhub_config(
            &fs::read_to_string(&targets.v2_config_path).unwrap_or_default(),
        );
    }
    if targets.v2_cache_path.exists() {
        saw_existing = true;
        all_managed &= is_zcode_codexhub_config(
            &fs::read_to_string(&targets.v2_cache_path).unwrap_or_default(),
        );
    }
    saw_existing && all_managed
}

pub(in crate::gateway) fn zcode_targets_contain_managed(targets: &ZcodeConfigTargets) -> bool {
    (targets.catalog_path.exists()
        && is_zcode_codexhub_config(&fs::read_to_string(&targets.catalog_path).unwrap_or_default()))
        || (targets.v2_config_path.exists()
            && is_zcode_v2_codexhub_config(
                &fs::read_to_string(&targets.v2_config_path).unwrap_or_default(),
            ))
        || (targets.v2_cache_path.exists()
            && is_zcode_codexhub_config(
                &fs::read_to_string(&targets.v2_cache_path).unwrap_or_default(),
            ))
}

pub(in crate::gateway) fn zcode_snapshot_contains_managed(snapshot_path: &Path) -> bool {
    let catalog_path = snapshot_path.join("codexhub.json");
    let v2_config_path = snapshot_path.join("config.json");
    let v2_cache_path = snapshot_path.join("bots-model-cache.v2.json");
    let targets = ZcodeConfigTargets {
        catalog_path,
        v2_config_path,
        v2_cache_path,
    };
    zcode_targets_contain_managed(&targets)
}

pub(in crate::gateway) fn latest_zcode_official_config_snapshot(
    backup_root: &Path,
) -> Result<PathBuf, String> {
    fs::read_dir(backup_root)
        .map_err(|error| {
            format!(
                "failed to read backup directory {}: {error}",
                backup_root.display()
            )
        })?
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let metadata = entry.metadata().ok()?;
            if !metadata.is_dir() {
                return None;
            }
            let modified = metadata.modified().ok()?;
            let path = entry.path();
            zcode_cleaned_v2_config_snapshot_text(&path)
                .ok()
                .flatten()
                .map(|_| (modified, path))
        })
        .max_by_key(|(modified, _)| *modified)
        .map(|(_, path)| path)
        .ok_or_else(|| "no ZCode snapshot with official v2 config is available".to_string())
}

pub(in crate::gateway) fn restore_zcode_sanitized_snapshot_files(
    snapshot_path: &Path,
    targets: &ZcodeConfigTargets,
) -> Result<(), String> {
    if let Some(text) = zcode_cleaned_v2_config_snapshot_text(snapshot_path)? {
        write_text_replace(&targets.v2_config_path, &text)?;
    } else if targets.v2_config_path.exists() {
        fs::remove_file(&targets.v2_config_path).map_err(|error| {
            format!(
                "failed to remove restored-absent config {}: {error}",
                targets.v2_config_path.display()
            )
        })?;
    }

    restore_zcode_sanitized_provider_collection_file(
        &snapshot_path.join("codexhub.json"),
        &targets.catalog_path,
    )?;
    restore_zcode_sanitized_provider_collection_file(
        &snapshot_path.join("bots-model-cache.v2.json"),
        &targets.v2_cache_path,
    )?;
    remove_zcode_coding_plan_cache(targets)?;
    Ok(())
}

pub(in crate::gateway) fn zcode_cleaned_v2_config_snapshot_text(
    snapshot_path: &Path,
) -> Result<Option<String>, String> {
    let path = snapshot_path.join("config.json");
    if !path.exists() {
        return Ok(None);
    }
    let text = fs::read_to_string(&path)
        .map_err(|error| format!("failed to read ZCode snapshot {}: {error}", path.display()))?;
    sanitize_zcode_v2_config_text(&text)
}

pub(in crate::gateway) fn restore_zcode_sanitized_provider_collection_file(
    source: &Path,
    target: &Path,
) -> Result<(), String> {
    let clean_text = if source.exists() {
        let text = fs::read_to_string(source).map_err(|error| {
            format!(
                "failed to read ZCode snapshot {}: {error}",
                source.display()
            )
        })?;
        sanitize_zcode_provider_collection_text(&text)?
    } else {
        None
    };
    if let Some(text) = clean_text {
        write_text_replace(target, &text)?;
    } else if target.exists() {
        fs::remove_file(target).map_err(|error| {
            format!(
                "failed to remove restored-absent config {}: {error}",
                target.display()
            )
        })?;
    }
    Ok(())
}

pub(in crate::gateway) fn zcode_coding_plan_cache_path(targets: &ZcodeConfigTargets) -> PathBuf {
    targets
        .v2_cache_path
        .with_file_name("coding-plan-cache.json")
}

pub(in crate::gateway) fn remove_zcode_coding_plan_cache(
    targets: &ZcodeConfigTargets,
) -> Result<bool, String> {
    let path = zcode_coding_plan_cache_path(targets);
    if !path.exists() {
        return Ok(false);
    }
    fs::remove_file(&path).map_err(|error| {
        format!(
            "failed to remove ZCode coding plan cache {}: {error}",
            path.display()
        )
    })?;
    Ok(true)
}

pub(in crate::gateway) fn sanitize_zcode_v2_provider_entry(provider: &mut Value) {
    if let Some(provider) = provider.as_object_mut() {
        provider.remove("systemDisabledReason");
    }
}

pub(in crate::gateway) fn remove_zcode_v2_codexhub_provider(
    config_path: &Path,
) -> Result<bool, String> {
    if !config_path.exists() {
        return Ok(false);
    }
    let text = fs::read_to_string(config_path).map_err(|error| {
        format!(
            "failed to read ZCode v2 config {}: {error}",
            config_path.display()
        )
    })?;
    if !is_zcode_v2_codexhub_config(&text) {
        return Ok(false);
    }
    let mut value = serde_json::from_str::<Value>(&text).map_err(|error| {
        format!(
            "failed to parse ZCode v2 config {}: {error}",
            config_path.display()
        )
    })?;
    let removed = value
        .get_mut("provider")
        .and_then(Value::as_object_mut)
        .is_some_and(remove_codexhub_client_provider_entries);
    if removed {
        let next = serde_json::to_string_pretty(&value)
            .map(|text| format!("{text}\n"))
            .map_err(|error| format!("failed to serialize ZCode v2 config: {error}"))?;
        write_text_replace(config_path, &next)?;
    }
    Ok(removed)
}

pub(in crate::gateway) fn is_zcode_codexhub_config(text: &str) -> bool {
    let Ok(value) = serde_json::from_str::<Value>(text) else {
        return text.contains("\"codexhub\"")
            || text.contains("\"codexhub-")
            || text.contains("CodexHub");
    };
    value
        .get("providers")
        .and_then(Value::as_array)
        .is_some_and(|providers| {
            providers
                .iter()
                .any(is_managed_zcode_catalog_provider_entry)
        })
}

pub(in crate::gateway) fn is_zcode_v2_codexhub_config(text: &str) -> bool {
    let Ok(value) = serde_json::from_str::<Value>(text) else {
        return text.contains("\"codexhub\"")
            || text.contains("\"codexhub-")
            || text.contains("CodexHub");
    };
    value
        .get("provider")
        .and_then(Value::as_object)
        .is_some_and(|providers| {
            providers
                .iter()
                .any(|(key, value)| is_managed_codexhub_provider_entry(key, value))
        })
}

pub(in crate::gateway) fn is_managed_zcode_catalog_provider_entry(provider: &Value) -> bool {
    provider
        .get("id")
        .and_then(Value::as_str)
        .is_some_and(|id| {
            is_managed_codexhub_provider_entry(id, provider)
                || (is_builtin_codexhub_client_provider_id(id)
                    && provider_entry_base_url(provider).is_none())
        })
        || (provider_entry_has_codexhub_name(provider)
            && provider_entry_base_url(provider).is_some_and(is_local_gateway_url)
            && provider_entry_has_gateway_path(provider))
}

pub(in crate::gateway) fn sanitize_zcode_v2_config_text(
    text: &str,
) -> Result<Option<String>, String> {
    let mut value = serde_json::from_str::<Value>(text)
        .map_err(|error| format!("failed to parse ZCode v2 config backup: {error}"))?;
    let Some(providers) = value.get_mut("provider").and_then(Value::as_object_mut) else {
        return Ok(None);
    };
    remove_codexhub_client_provider_entries(providers);
    for provider in providers.values_mut() {
        sanitize_zcode_v2_provider_entry(provider);
    }
    if providers.is_empty() {
        return Ok(None);
    }
    serde_json::to_string_pretty(&value)
        .map(|text| Some(format!("{text}\n")))
        .map_err(|error| format!("failed to serialize cleaned ZCode v2 config: {error}"))
}

pub(in crate::gateway) fn sanitize_zcode_provider_collection_text(
    text: &str,
) -> Result<Option<String>, String> {
    let mut value = serde_json::from_str::<Value>(text)
        .map_err(|error| format!("failed to parse ZCode provider collection backup: {error}"))?;
    let Some(providers) = value.get_mut("providers").and_then(Value::as_array_mut) else {
        return Ok(None);
    };
    providers.retain(|provider| !is_managed_zcode_catalog_provider_entry(provider));
    if providers.is_empty() {
        return Ok(None);
    }
    serde_json::to_string_pretty(&value)
        .map(|text| Some(format!("{text}\n")))
        .map_err(|error| format!("failed to serialize cleaned ZCode provider collection: {error}"))
}

pub(in crate::gateway) fn zcode_targets_from_writable(
    targets: &IsolatedClientApplyTargets,
) -> Result<ZcodeConfigTargets, String> {
    let writable = targets.writable_paths();
    if writable.len() < 3 {
        return Err("zcode isolated targets are missing files".to_string());
    }
    Ok(ZcodeConfigTargets {
        catalog_path: writable[0].clone(),
        v2_config_path: writable[1].clone(),
        v2_cache_path: writable[2].clone(),
    })
}
