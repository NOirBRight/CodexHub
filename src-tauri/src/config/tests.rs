use super::{
    apply_history_sync_result, codex_overlay_owner, get_bundled_providers_with_paths,
    get_codex_context_guard_status_with_paths, get_providers_with_paths, get_settings_with_paths,
    merge_post_switch_gateway_status, migrate_legacy_context_guard_with_paths,
    managed_codex_projection_transaction_paths_with_paths,
    republish_managed_codex_context_budget_with_paths, save_providers_with_paths,
    save_settings_with_paths, set_codex_context_guard_with_paths, switch_mode_with_paths,
    switch_mode_with_paths_takeover_as_owner, takeover_metadata_path, top_level_model_is_official,
    CommandOutcome, CommandRunner, ConfigPaths, ProcessCommandRunner,
};
use crate::{Model, Provider, Settings, ToolProtocol, ToolSurfaceStrategy, UpstreamFormat};
use std::cell::RefCell;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

#[test]
fn projection_transaction_restores_both_channel_backups_and_takeover_metadata() {
    let root = temp_root("projection-cross-channel-rollback");
    let paths = test_paths(&root);
    let transaction_paths = managed_codex_projection_transaction_paths_with_paths(
        &paths,
        crate::app_flavor::RoutingOwner::Release,
    );
    let release_backup = paths.config_backup_path_for_target_owner(
        crate::app_flavor::RoutingOwner::Release,
        crate::app_flavor::RoutingOwner::Release,
    );
    let beta_backup = paths.config_backup_path_for_target_owner(
        crate::app_flavor::RoutingOwner::Release,
        crate::app_flavor::RoutingOwner::Beta,
    );
    let release_takeover = takeover_metadata_path(&release_backup);
    let beta_takeover = takeover_metadata_path(&beta_backup);
    for (path, text) in [
        (&release_backup, "old-release-backup"),
        (&beta_backup, "old-beta-backup"),
        (&release_takeover, "old-release-takeover"),
        (&beta_takeover, "old-beta-takeover"),
    ] {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, text).unwrap();
        assert!(transaction_paths.contains(path));
    }

    let result = crate::file_transaction::with_text_file_rollback(&transaction_paths, || {
        fs::write(&release_backup, "new-release-backup").unwrap();
        fs::write(&beta_backup, "new-beta-backup").unwrap();
        fs::write(&release_takeover, "new-release-takeover").unwrap();
        fs::remove_file(&beta_takeover).unwrap();
        Err::<(), _>("injected post-projection failure".to_string())
    });

    assert!(result.is_err());
    assert_eq!(fs::read_to_string(release_backup).unwrap(), "old-release-backup");
    assert_eq!(fs::read_to_string(beta_backup).unwrap(), "old-beta-backup");
    assert_eq!(
        fs::read_to_string(release_takeover).unwrap(),
        "old-release-takeover"
    );
    assert_eq!(
        fs::read_to_string(beta_takeover).unwrap(),
        "old-beta-takeover"
    );
    let _ = fs::remove_dir_all(root);
}

#[test]
fn committed_route_survives_gateway_status_readback_failure() {
    let mut status = crate::AppStatus::scaffold("Switched to custom mode");

    merge_post_switch_gateway_status(&mut status, Err("health unavailable".to_string()));

    assert_eq!(status.mode, "unknown");
    assert!(status.message.contains("route committed"));
    assert!(status.message.contains("health unavailable"));
}

#[test]
fn providers_toml_roundtrip_preserves_all_provider_and_model_fields() {
    let root = temp_root("providers-roundtrip");
    let paths = test_paths(&root);
    let providers = vec![Provider {
        id: "volc-roundtrip".to_string(),
        name: "Volcengine".to_string(),
        base_url: "https://ark.cn-beijing.volces.com/api/coding/v3".to_string(),
        api_key: Some("{env:VOLCENGINE_API_KEY}".to_string()),
        upstream_format: Some(UpstreamFormat::ChatCompletions),
        available_upstream_formats: Some(vec![
            UpstreamFormat::Responses,
            UpstreamFormat::ChatCompletions,
        ]),
        tool_protocol: Some(ToolProtocol::ChatTools),
        tool_surface_strategy: Some(ToolSurfaceStrategy::Eager),
        reports_cached_input_tokens: Some(true),
        supports_developer_role: None,
        display_prefix: Some("Volc".to_string()),
        auth_capabilities: None,
        onboarding_hint: Some("providers.catalogProviderSubscriptionHint".to_string()),
        discovery_policy: None,
        sort_order: Some(2),
        enabled: true,
        locked: false,
        models: vec![
            Model {
                id: "glm-5.2".to_string(),
                display_name: Some("Volc GLM-5.2".to_string()),
                upstream_model: Some("ep-20260629".to_string()),
                aliases: vec!["GLM-5.2".to_string(), "legacy-glm52".to_string()],
                context_window: Some(1_024_000),
                max_output_tokens: Some(8_192),
                input_modalities: Some(vec!["text".to_string(), "image".to_string()]),
                supported_reasoning_levels: Some(vec![
                    "low".to_string(),
                    "medium".to_string(),
                    "high".to_string(),
                    "xhigh".to_string(),
                ]),
                default_reasoning_level: Some("high".to_string()),
                tool_surface_strategy: Some(ToolSurfaceStrategy::DeferredCore),
                sort_order: Some(1),
                enabled: true,
                ..Model::default()
            },
            Model {
                id: "minimax-m3".to_string(),
                display_name: None,
                upstream_model: None,
                context_window: None,
                max_output_tokens: Some(8_192),
                input_modalities: None,
                supported_reasoning_levels: None,
                default_reasoning_level: None,
                sort_order: Some(2),
                enabled: false,
                ..Model::default()
            },
        ],
    }];

    let saved = save_providers_with_paths(providers.clone(), &paths).expect("providers save");
    let loaded = get_providers_with_paths(&paths).expect("providers load");

    assert_json_eq(&saved, &providers);
    assert_json_eq(&loaded, &providers);
    let written = fs::read_to_string(paths.runtime_providers_path()).expect("providers text");
    assert!(written.contains("[[providers]]"));
    assert!(written.contains("[[providers.models]]"));
    assert!(written.contains("upstream_format = \"chat_completions\""));
    assert!(written.contains("available_upstream_formats"));
    assert!(written.contains("tool_protocol = \"chat_tools\""));
    assert!(written.contains("tool_surface_strategy = \"eager\""));
    assert!(written.contains("reports_cached_input_tokens = true"));
    assert!(written.contains("onboarding_hint = \"providers.catalogProviderSubscriptionHint\""));
    assert!(written.contains("\"responses\""));
    assert!(written.contains("upstream_model = \"ep-20260629\""));
    assert!(written.contains("aliases"));
    assert!(!written.contains("aliases = []"));
    assert!(written.contains("\"GLM-5.2\""));
    assert_eq!(
        loaded[0].models[0].aliases,
        vec!["GLM-5.2".to_string(), "legacy-glm52".to_string()]
    );
    assert!(written.contains("input_modalities"));
    assert!(written.contains("\"image\""));
    assert!(written.contains("supported_reasoning_levels"));
    assert!(written.contains("\"xhigh\""));
    assert!(written.contains("default_reasoning_level = \"high\""));
    assert!(written.contains("tool_surface_strategy = \"deferred_core\""));
}

#[test]
fn providers_toml_roundtrip_preserves_anthropic_endpoint_selection() {
    let root = temp_root("providers-anthropic-format");
    let paths = test_paths(&root);
    let providers = vec![Provider {
        id: "anthropic-direct".to_string(),
        name: "Anthropic Direct".to_string(),
        base_url: "https://api.anthropic.com".to_string(),
        api_key: Some("{env:ANTHROPIC_API_KEY}".to_string()),
        upstream_format: Some(UpstreamFormat::AnthropicMessages),
        available_upstream_formats: Some(vec![UpstreamFormat::AnthropicMessages]),
        tool_protocol: Some(ToolProtocol::None),
        tool_surface_strategy: None,
        reports_cached_input_tokens: None,
        supports_developer_role: None,
        display_prefix: Some("anthropic/".to_string()),
        auth_capabilities: None,
        onboarding_hint: None,
        discovery_policy: None,
        sort_order: Some(3),
        enabled: true,
        locked: false,
        models: vec![Model {
            id: "claude-sonnet-4-20250514".to_string(),
            enabled: true,
            ..Model::default()
        }],
    }];

    save_providers_with_paths(providers.clone(), &paths).expect("providers save");
    let loaded = get_providers_with_paths(&paths).expect("providers load");
    let written = fs::read_to_string(paths.runtime_providers_path()).expect("providers text");

    assert_json_eq(&loaded, &providers);
    assert!(written.contains("upstream_format = \"anthropic_messages\""));
    assert!(written.contains("available_upstream_formats = [\"anthropic_messages\"]"));
    assert!(written.contains("tool_protocol = \"none\""));
}

#[test]
fn get_providers_falls_back_to_bundled_config_when_runtime_config_is_missing() {
    let root = temp_root("providers-fallback");
    let paths = test_paths(&root);
    fs::create_dir_all(paths.bundled_providers_path().parent().unwrap()).unwrap();
    fs::write(
        paths.bundled_providers_path(),
        r#"
[[providers]]
id = "bundled"
name = "Bundled Provider"
base_url = "https://example.test/v1"
api_key = "{env:BUNDLED_API_KEY}"
sort_order = 7

  [[providers.models]]
  id = "model-a"
  context_window = 123
"#,
    )
    .unwrap();

    let loaded = get_providers_with_paths(&paths).expect("fallback providers");

    assert_eq!(loaded.len(), 1);
    assert_eq!(loaded[0].id, "bundled");
    assert_eq!(loaded[0].models[0].id, "model-a");
    assert!(loaded[0].enabled);
    assert!(loaded[0].models[0].enabled);
}

#[test]
fn get_bundled_providers_reads_bundled_file_even_when_runtime_config_exists() {
    let root = temp_root("providers-bundled-catalog");
    let paths = test_paths(&root);
    fs::create_dir_all(paths.bundled_providers_path().parent().unwrap()).unwrap();
    fs::create_dir_all(paths.runtime_providers_path().parent().unwrap()).unwrap();
    fs::write(
        paths.bundled_providers_path(),
        r#"
[[providers]]
id = "xai"
name = "xAI"
base_url = "https://api.x.ai/v1"

  [[providers.models]]
  id = "grok-4"
"#,
    )
    .unwrap();
    fs::write(
        paths.runtime_providers_path(),
        r#"
[[providers]]
id = "ollama-cloud"
name = "Ollama Cloud"
base_url = "https://ollama.com/v1"
"#,
    )
    .unwrap();

    let runtime = get_providers_with_paths(&paths).expect("runtime providers");
    let bundled = get_bundled_providers_with_paths(&paths).expect("bundled providers");

    assert_eq!(runtime.len(), 1);
    assert_eq!(runtime[0].id, "ollama-cloud");
    assert_eq!(bundled.len(), 1);
    assert_eq!(bundled[0].id, "xai");
    assert_eq!(bundled[0].models[0].id, "grok-4");
}

#[test]
fn get_providers_applies_resolved_limits_used_by_the_gateway() {
    let root = temp_root("providers-resolved-limits");
    let paths = test_paths(&root);
    fs::create_dir_all(paths.bundled_providers_path().parent().unwrap()).unwrap();
    fs::write(
        paths.bundled_providers_path(),
        r#"
[[providers]]
id = "ollama-cloud"
name = "Ollama Cloud"
base_url = "https://ollama.com/v1"

  [[providers.models]]
  id = "glm-5.2"

[[providers]]
id = "volc"
name = "Volcengine"
base_url = "https://ark.cn-beijing.volces.com/api/coding/v3"

  [[providers.models]]
  id = "minimax-m3"
"#,
    )
    .unwrap();

    let loaded = get_providers_with_paths(&paths).expect("providers with resolved limits");

    assert_eq!(loaded[0].models[0].context_window, Some(1_000_000));
    assert_eq!(loaded[0].models[0].max_context_window, Some(1_000_000));
    assert_eq!(loaded[1].models[0].context_window, Some(1_000_000));
    assert_eq!(loaded[1].models[0].max_context_window, Some(1_000_000));
    assert_eq!(
        loaded[0].models[0].effective_source.as_deref(),
        Some("provider_spec")
    );
    assert_eq!(
        loaded[1].models[0].max_source.as_deref(),
        Some("https://www.volcengine.com/docs/82379")
    );
}

#[test]
fn settings_missing_file_returns_defaults_and_roundtrips_saved_values() {
    let root = temp_root("settings-roundtrip");
    let paths = test_paths(&root);

    let defaults = get_settings_with_paths(&paths).expect("default settings");
    assert_settings_eq(&defaults, &Settings::default());
    assert_eq!(
        defaults.proxy_port,
        crate::app_flavor::default_gateway_port()
    );

    let custom = Settings {
        locale: "zh-CN".to_string(),
        auto_sync_history: false,
        unified_codex_history: false,
        auto_start_software: false,
        auto_start_gateway: false,
        include_official_models: false,
        auto_sync_catalog: false,
        auto_sync_clients: false,
        default_codex_route: "official".to_string(),
        gateway_bind_address: "127.0.0.1".to_string(),
        gateway_client_key: "local-test-key".to_string(),
        gateway_enable_models: false,
        gateway_enable_responses: true,
        gateway_enable_chat_completions: false,
        gateway_request_timeout_seconds: 90,
        gateway_auto_retry_enabled: false,
        gateway_auto_retry_max_attempts: 7,
        gateway_image_proxy_enabled: true,
        gateway_image_proxy_model: "minimax-cn/MiniMax-M3".to_string(),
        openai_context_guard_enabled: true,
        gateway_fast_model_variants: vec!["gpt-5.5".to_string()],
        official_disabled_models: vec!["gpt-5.4-mini".to_string()],
        official_model_sort_order: vec!["gpt-5.4".to_string(), "gpt-5.5".to_string()],
        official_provider_sort_order: 3,
        proxy_port: 4555,
    };
    let saved = save_settings_with_paths(custom.clone(), &paths).expect("settings save");
    let loaded = get_settings_with_paths(&paths).expect("settings load");

    assert_settings_eq(&saved, &custom);
    assert_settings_eq(&loaded, &custom);
    let written = fs::read_to_string(paths.settings_path()).expect("settings text");
    assert!(written.contains("\"proxy_port\": 4555"));
    assert!(written.contains("\"gateway_request_timeout_seconds\": 90"));
    assert!(written.contains("\"gateway_auto_retry_enabled\": false"));
    assert!(written.contains("\"gateway_auto_retry_max_attempts\": 7"));
    assert!(written.contains("\"gateway_image_proxy_enabled\": true"));
    assert!(written.contains("\"gateway_image_proxy_model\": \"minimax-cn/MiniMax-M3\""));
    assert!(written.contains("\"openai_context_guard_enabled\": true"));
    assert!(written.contains("\"gateway_fast_model_variants\""));
    assert!(written.contains("\"official_disabled_models\""));
    assert!(written.contains("\"official_model_sort_order\""));
    assert!(written.contains("\"official_provider_sort_order\": 3"));
    assert!(written.contains("\"auto_sync_clients\": false"));
    assert!(written.contains("\"auto_start_software\": false"));
    assert!(written.contains("\"auto_start_gateway\": false"));
    assert!(written.contains("\"unified_codex_history\": false"));
    assert!(written.contains("\"locale\": \"zh-CN\""));
}

#[test]
fn legacy_official_model_ids_are_normalized_on_load_and_save() {
    let root = temp_root("legacy-official-model-ids");
    let paths = test_paths(&root);
    fs::create_dir_all(paths.settings_path().parent().unwrap()).unwrap();
    fs::write(
        paths.settings_path(),
        r#"{
          "gateway_fast_model_variants": [
            " openai/gpt-5.5 ",
            "gpt-5.5",
            "openai/gpt-5.4",
            "ollama-cloud/glm-5.2"
          ],
          "official_disabled_models": [
            " openai/gpt-5.4-mini ",
            "gpt-5.4-mini",
            "ollama-cloud/glm-5.2"
          ],
          "official_model_sort_order": [
            "openai/gpt-5.5",
            " gpt-5.5 ",
            "ollama-cloud/glm-5.2"
          ]
        }"#,
    )
    .unwrap();

    let loaded = get_settings_with_paths(&paths).expect("legacy settings load");

    assert_eq!(
        loaded.gateway_fast_model_variants,
        vec!["gpt-5.5".to_string(), "gpt-5.4".to_string()]
    );
    assert_eq!(
        loaded.official_disabled_models,
        vec![
            "gpt-5.4-mini".to_string(),
            "ollama-cloud/glm-5.2".to_string()
        ]
    );
    assert_eq!(
        loaded.official_model_sort_order,
        vec!["gpt-5.5".to_string(), "ollama-cloud/glm-5.2".to_string()]
    );

    let saved = save_settings_with_paths(
        Settings {
            gateway_fast_model_variants: vec![
                "openai/gpt-5.5".to_string(),
                " gpt-5.4 ".to_string(),
            ],
            official_disabled_models: vec!["openai/gpt-5.4".to_string(), " gpt-5.4 ".to_string()],
            official_model_sort_order: vec!["openai/gpt-5.5".to_string(), " gpt-5.5 ".to_string()],
            ..Settings::default()
        },
        &paths,
    )
    .expect("legacy settings save");

    assert_eq!(
        saved.gateway_fast_model_variants,
        vec!["gpt-5.5".to_string(), "gpt-5.4".to_string()]
    );
    assert_eq!(saved.official_disabled_models, vec!["gpt-5.4".to_string()]);
    assert_eq!(saved.official_model_sort_order, vec!["gpt-5.5".to_string()]);
    let written = fs::read_to_string(paths.settings_path()).expect("normalized settings text");
    assert!(!written.contains("openai/gpt-"));
}

#[test]
fn shared_model_identity_vectors_reject_only_unknown_official_aliases() {
    let fixture: serde_json::Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/model_identity_vectors.json"
    ))
    .expect("identity fixture");
    let inputs = fixture["vectors"]
        .as_array()
        .unwrap()
        .iter()
        .map(|vector| vector["input"].as_str().unwrap().to_string())
        .collect();
    let mut expected = Vec::<String>::new();
    for value in fixture["vectors"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|vector| vector["expected"].as_str())
    {
        if !expected.iter().any(|existing| existing == value) {
            expected.push(value.to_string());
        }
    }

    assert_eq!(super::sanitize_model_ids(inputs), expected);
}

#[test]
fn settings_accept_current_catalog_alias_and_reject_unknown_official_alias() {
    let root = temp_root("current-official-alias");
    let paths = test_paths(&root);
    let catalog_path = root
        .join("codex-home")
        .join("model-catalogs")
        .join("openai-plus-ollama-cloud.json");
    fs::create_dir_all(catalog_path.parent().unwrap()).unwrap();
    fs::write(
        &catalog_path,
        r#"{"models":[{"slug":"gpt-5.6-sol","display_name":"GPT-5.6-Sol"}]}"#,
    )
    .unwrap();
    fs::create_dir_all(paths.settings_path().parent().unwrap()).unwrap();
    fs::write(
        paths.settings_path(),
        r#"{
          "official_disabled_models": [
            "openai/gpt-5.6-sol",
            "openai/gpt-9.9-unknown",
            "acme/gpt-5.6-sol"
          ]
        }"#,
    )
    .unwrap();

    let loaded = get_settings_with_paths(&paths).expect("settings load");

    assert_eq!(
        loaded.official_disabled_models,
        vec!["gpt-5.6-sol".to_string(), "acme/gpt-5.6-sol".to_string()]
    );
    let saved = save_settings_with_paths(loaded, &paths).expect("settings save");
    assert_eq!(
        saved.official_disabled_models,
        vec!["gpt-5.6-sol".to_string(), "acme/gpt-5.6-sol".to_string()]
    );
    let written = fs::read_to_string(paths.settings_path()).unwrap();
    assert!(!written.contains("openai/gpt-"));
    let _ = fs::remove_dir_all(root);
}

#[test]
fn generated_and_bundled_catalogs_do_not_authorize_legacy_aliases() {
    let root = temp_root("untrusted-official-alias-catalogs");
    let paths = test_paths(&root);
    let generated_path = paths.generated_catalog_path();
    fs::create_dir_all(generated_path.parent().unwrap()).unwrap();
    fs::write(
        generated_path,
        r#"{"models":[{"slug":"gpt-forged-generated"}]}"#,
    )
    .unwrap();
    let bundled_path = root
        .join("repo-root")
        .join("model-catalogs")
        .join("openai-plus-ollama-cloud.json");
    fs::create_dir_all(bundled_path.parent().unwrap()).unwrap();
    fs::write(
        bundled_path,
        r#"{"models":[{"slug":"gpt-forged-bundled"}]}"#,
    )
    .unwrap();
    fs::create_dir_all(paths.settings_path().parent().unwrap()).unwrap();
    fs::write(
        paths.settings_path(),
        r#"{
          "official_disabled_models": [
            "openai/gpt-forged-generated",
            "openai/gpt-forged-bundled"
          ]
        }"#,
    )
    .unwrap();

    let loaded = get_settings_with_paths(&paths).expect("settings load");

    assert!(loaded.official_disabled_models.is_empty());
    let _ = fs::remove_dir_all(root);
}

#[test]
fn legacy_auto_start_proxy_migrates_to_software_autostart_only() {
    let root = temp_root("legacy-autostart-split");
    let paths = test_paths(&root);
    fs::create_dir_all(paths.settings_path().parent().unwrap()).unwrap();
    fs::write(
        paths.settings_path(),
        r#"{
          "auto_start_proxy": false,
          "proxy_port": 4555
        }"#,
    )
    .unwrap();

    let loaded = get_settings_with_paths(&paths).expect("settings load");

    assert!(!loaded.auto_start_software);
    assert!(loaded.auto_start_gateway);
}

#[test]
fn gateway_retry_and_image_proxy_settings_default_and_clamp() {
    let root = temp_root("gateway-runtime-settings");
    let paths = test_paths(&root);
    fs::create_dir_all(paths.settings_path().parent().unwrap()).unwrap();
    fs::write(
        paths.settings_path(),
        r#"{
          "gateway_auto_retry_max_attempts": 99,
          "gateway_image_proxy_enabled": true,
          "gateway_image_proxy_model": "  minimax-cn/MiniMax-M3  ",
          "proxy_port": 4555
        }"#,
    )
    .unwrap();

    let loaded = get_settings_with_paths(&paths).expect("settings load");

    assert!(loaded.gateway_auto_retry_enabled);
    assert_eq!(loaded.gateway_auto_retry_max_attempts, 30);
    assert!(loaded.gateway_image_proxy_enabled);
    assert_eq!(loaded.gateway_image_proxy_model, "minimax-cn/MiniMax-M3");
}

#[test]
fn gateway_retry_attempts_clamp_to_minimum() {
    let root = temp_root("gateway-runtime-settings-min");
    let paths = test_paths(&root);
    fs::create_dir_all(paths.settings_path().parent().unwrap()).unwrap();
    fs::write(
        paths.settings_path(),
        r#"{
          "gateway_auto_retry_max_attempts": 0,
          "proxy_port": 4555
        }"#,
    )
    .unwrap();

    let loaded = get_settings_with_paths(&paths).expect("settings load");

    assert_eq!(loaded.gateway_auto_retry_max_attempts, 1);
}

#[test]
fn missing_locale_loads_as_frontend_resolved_default_marker() {
    let root = temp_root("settings-missing-locale");
    let paths = test_paths(&root);
    fs::create_dir_all(paths.settings_path().parent().unwrap()).unwrap();
    fs::write(
        paths.settings_path(),
        r#"{
          "proxy_port": 4555
        }"#,
    )
    .unwrap();

    let loaded = get_settings_with_paths(&paths).expect("settings load");

    assert_eq!(loaded.locale, "");
    assert_eq!(loaded.proxy_port, 4555);
}

#[test]
fn invalid_locale_saves_as_english_default() {
    let root = temp_root("settings-invalid-locale");
    let paths = test_paths(&root);

    let saved = save_settings_with_paths(
        Settings {
            locale: "fr-FR".to_string(),
            ..Settings::default()
        },
        &paths,
    )
    .expect("settings save");
    let written = fs::read_to_string(paths.settings_path()).expect("settings text");

    assert_eq!(saved.locale, "en-US");
    assert!(written.contains("\"locale\": \"en-US\""));
}

#[test]
fn legacy_auto_sync_catalog_loads_as_auto_sync_clients() {
    let root = temp_root("legacy-auto-sync-catalog");
    let paths = test_paths(&root);
    fs::create_dir_all(paths.settings_path().parent().unwrap()).unwrap();
    fs::write(
        paths.settings_path(),
        r#"{
          "auto_sync_catalog": false,
          "proxy_port": 4555
        }"#,
    )
    .unwrap();

    let loaded = get_settings_with_paths(&paths).expect("legacy settings load");

    assert!(!loaded.auto_sync_catalog);
    assert!(!loaded.auto_sync_clients);
    assert_eq!(loaded.proxy_port, 4555);
}

#[test]
fn unified_history_setting_false_is_preserved() {
    let root = temp_root("unified-history-disabled");
    let paths = test_paths(&root);
    fs::create_dir_all(paths.settings_path().parent().unwrap()).unwrap();
    fs::write(
        paths.settings_path(),
        r#"{
          "unified_codex_history": false,
          "proxy_port": 4555
        }"#,
    )
    .unwrap();

    let loaded = get_settings_with_paths(&paths).expect("legacy settings load");

    assert!(!loaded.unified_codex_history);
    assert_eq!(loaded.proxy_port, 4555);
}

#[test]
fn missing_unified_history_defaults_true_and_serializes() {
    let root = temp_root("unified-history-default-true");
    let paths = test_paths(&root);
    fs::create_dir_all(paths.settings_path().parent().unwrap()).unwrap();
    fs::write(
        paths.settings_path(),
        r#"{
          "proxy_port": 4555
        }"#,
    )
    .unwrap();

    let loaded = get_settings_with_paths(&paths).expect("settings load");
    let value = serde_json::to_value(&loaded).expect("settings serialize");

    assert!(loaded.unified_codex_history);
    assert_eq!(value["unified_codex_history"], serde_json::json!(true));
}

#[test]
fn switch_mode_custom_applies_config_overlay_without_history_sync() {
    let root = temp_root("switch-custom");
    let paths = test_paths(&root);
    save_settings_with_paths(
        Settings {
            proxy_port: 4555,
            ..Settings::default()
        },
        &paths,
    )
    .expect("settings save");
    let runner = RecordingRunner::successful();

    let status = switch_mode_with_paths("custom", true, &paths, Path::new("python-test"), &runner)
        .expect("switch custom");

    assert_eq!(status.mode, "custom");
    assert_eq!(status.proxy_port, 4555);
    assert!(!status.proxy_running);
    assert_eq!(
        status.gateway_lifecycle,
        crate::gateway_transaction::GatewayLifecyclePhase::Unavailable
    );
    assert!(status.message.contains("custom"));

    let commands = runner.commands.borrow();
    assert_eq!(commands.len(), 1);
    assert_eq!(
        commands[0].args[0],
        paths.config_overlay_script().to_string_lossy()
    );
    assert_contains_sequence(&commands[0].args, &["apply"]);
    assert_arg_value(&commands[0].args, "--config", &paths.codex_config_path());
    assert_arg_value(&commands[0].args, "--backup", &paths.config_backup_path());
    assert_arg_value(
        &commands[0].args,
        "--context-guard-state",
        &paths.context_guard_state_path(),
    );
    assert_arg_value(
        &commands[0].args,
        "--catalog",
        &paths.generated_catalog_path(),
    );
    assert_arg_literal(&commands[0].args, "--base-url", "http://127.0.0.1:4555");
    assert_arg_literal(&commands[0].args, "--gateway-key", "codexhub-proxy");
    assert_arg_literal(&commands[0].args, "--owner", "release");
    assert_eq!(
        paths
            .config_backup_path()
            .file_name()
            .and_then(|name| name.to_str()),
        Some("config.toml.release.backup")
    );
    assert!(!commands[0].args.iter().any(|arg| arg == "normalize-fast"));
    assert_eq!(status.history_sync_status, None);
    assert_eq!(status.history_sync_message, None);
}

#[test]
fn route_switch_history_failure_is_returned_to_the_user() {
    let mut status = crate::AppStatus::scaffold("route switched");

    apply_history_sync_result(&mut status, Err("history database is busy".to_string()));

    assert_eq!(status.history_sync_status.as_deref(), Some("conflict"));
    assert_eq!(
        status.history_sync_message.as_deref(),
        Some("history database is busy")
    );
}

#[test]
fn isolated_paths_keep_beta_runtime_artifacts_out_of_codex_target() {
    let root = temp_root("isolated-beta-paths");
    let runtime = root.join(".codexhub-beta");
    let target = root.join(".codex");
    let paths = ConfigPaths::new_isolated(&runtime, &target, root.join("repo"));

    assert_eq!(paths.settings_path(), runtime.join("proxy/settings.json"));
    assert_eq!(
        paths.config_backup_path(),
        runtime.join("proxy/config.toml.release.backup")
    );
    assert_eq!(paths.codex_config_path(), target.join("config.toml"));
    assert_eq!(
        paths.generated_catalog_path(),
        runtime.join("model-catalogs/codexhub-model-catalog.json")
    );
}

#[test]
fn beta_can_explicitly_switch_stable_owned_codex_to_unified_official() {
    let root = temp_root("beta-force-stable-to-official");
    let target = root.join(".codex");
    let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .to_path_buf();
    let stable_paths = ConfigPaths::new_isolated(&target, &target, &repo);
    let beta_paths = ConfigPaths::new_isolated(root.join(".codexhub-beta"), &target, repo);
    fs::create_dir_all(&target).unwrap();
    fs::write(
        stable_paths.codex_config_path(),
        b"model = \"gpt-5.4\"\nmodel_reasoning_effort = \"high\"\n",
    )
    .unwrap();
    let catalog_path = stable_paths.generated_catalog_path();
    fs::create_dir_all(catalog_path.parent().unwrap()).unwrap();
    fs::write(
        catalog_path,
        r#"{
  "models": [
{
  "slug": "gpt-5.4",
  "codex_proxy_metadata": {
    "provider": "openai",
    "upstream_name": "official",
    "official_context_budget": {
      "source": "current_direct_official",
      "freshness": "fresh",
      "model_context_window": 300000,
      "effective_context_window_percent": 100,
      "effective_context_window": 300000,
      "model_auto_compact_token_limit": 270000
    }
  }
}
  ]
}"#,
    )
    .unwrap();
    save_settings_with_paths(Settings::default(), &stable_paths).unwrap();
    save_settings_with_paths(Settings::default(), &beta_paths).unwrap();
    let python = super::find_python().expect("repository Python interpreter");
    let runner = ProcessCommandRunner;

    switch_mode_with_paths_takeover_as_owner(
        crate::app_flavor::RoutingOwner::Release,
        "custom",
        false,
        &stable_paths,
        &python,
        &runner,
    )
    .unwrap();

    switch_mode_with_paths_takeover_as_owner(
        crate::app_flavor::RoutingOwner::Beta,
        "official",
        true,
        &beta_paths,
        &python,
        &runner,
    )
    .unwrap();

    let restored = fs::read_to_string(beta_paths.codex_config_path()).unwrap();
    assert!(restored.contains("model_provider = \"custom\""));
    assert!(restored.contains("name = \"OpenAI\""));
    assert!(restored.contains("model = \"gpt-5.4\""));
    assert!(restored.contains("model_reasoning_effort = \"high\""));
    assert!(!restored.contains("# owner = release"));
    assert!(!restored.contains("base_url"));
}

#[test]
fn beta_backend_takeover_chain_with_default_unified_history_preserves_the_custom_bucket() {
    for (name, original) in [
        ("unowned", b"model_reasoning_effort = \"high\"\r\n".as_slice()),
        (
            "official",
            b"model_provider = \"openai\"\nmodel_reasoning_effort = \"medium\"\n".as_slice(),
        ),
        (
            "stable",
            b"# BEGIN CODEX PROXY SESSION CONFIG\n# owner = release\n# END CODEX PROXY SESSION CONFIG\nmodel_reasoning_effort = \"high\"\n".as_slice(),
        ),
    ] {
        let root = temp_root(name);
        let runtime = root.join(".codexhub-beta");
        let target = root.join(".codex");
        let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .to_path_buf();
        let paths = ConfigPaths::new_isolated(&runtime, &target, repo);
        fs::create_dir_all(&target).unwrap();
        fs::write(paths.codex_config_path(), original).unwrap();
        save_settings_with_paths(
            Settings {
                proxy_port: 9109,
                ..Settings::default()
            },
            &paths,
        )
        .unwrap();
        let python = super::find_python().expect("repository Python interpreter");
        let runner = ProcessCommandRunner;

        let rejected = switch_mode_with_paths_takeover_as_owner(
            crate::app_flavor::RoutingOwner::Beta,
            "custom",
            false,
            &paths,
            &python,
            &runner,
        )
        .expect_err("normal Beta connect must be rejected");
        assert!(rejected.contains("route.takeover_required"));
        assert_eq!(fs::read(paths.codex_config_path()).unwrap(), original);
        assert!(!paths.config_backup_path_for_owner(crate::app_flavor::RoutingOwner::Beta).exists());

        switch_mode_with_paths_takeover_as_owner(
            crate::app_flavor::RoutingOwner::Beta,
            "custom",
            true,
            &paths,
            &python,
            &runner,
        )
        .unwrap();
        switch_mode_with_paths_takeover_as_owner(
            crate::app_flavor::RoutingOwner::Beta,
            "custom",
            false,
            &paths,
            &python,
            &runner,
        )
        .unwrap();
        switch_mode_with_paths_takeover_as_owner(
            crate::app_flavor::RoutingOwner::Beta,
            "official",
            false,
            &paths,
            &python,
            &runner,
        )
        .unwrap();

        let restored = fs::read_to_string(paths.codex_config_path()).unwrap();
        if name == "stable" {
            assert_eq!(restored.as_bytes(), original);
        } else {
            assert!(restored.contains("model_provider = \"custom\""));
            assert!(restored.contains("[model_providers.custom]"));
            assert!(restored.contains("name = \"OpenAI\""));
            assert!(!restored.contains("base_url"));
            assert!(restored.contains("model_reasoning_effort"));
        }
        assert!(!paths.config_backup_path_for_owner(crate::app_flavor::RoutingOwner::Beta).exists());
    }
}

#[test]
fn stable_normal_connect_then_official_reconciles_unified_history() {
    let root = temp_root("stable-normal-unified-restore");
    let runtime = root.join(".codexhub");
    let target = root.join(".codex");
    let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .to_path_buf();
    let paths = ConfigPaths::new_isolated(&runtime, &target, repo);
    fs::create_dir_all(&target).unwrap();
    fs::write(
        paths.codex_config_path(),
        b"model_reasoning_effort = \"high\"\r\n",
    )
    .unwrap();
    save_settings_with_paths(Settings::default(), &paths).unwrap();
    let python = super::find_python().expect("repository Python interpreter");
    let runner = ProcessCommandRunner;

    switch_mode_with_paths_takeover_as_owner(
        crate::app_flavor::RoutingOwner::Release,
        "custom",
        false,
        &paths,
        &python,
        &runner,
    )
    .unwrap();
    switch_mode_with_paths_takeover_as_owner(
        crate::app_flavor::RoutingOwner::Release,
        "official",
        false,
        &paths,
        &python,
        &runner,
    )
    .unwrap();

    let restored = fs::read_to_string(paths.codex_config_path()).unwrap();
    assert!(restored.contains("model_provider = \"custom\""));
    assert!(restored.contains("[model_providers.custom]"));
    assert!(restored.contains("name = \"OpenAI\""));
    assert!(restored.contains("requires_openai_auth = true"));
    assert!(!paths.config_backup_path().exists());
}

#[test]
fn stable_same_owner_force_with_missing_backup_disconnects_to_unified_official() {
    let root = temp_root("stable-same-owner-force");
    let runtime = root.join(".codexhub");
    let target = root.join(".codex");
    let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .to_path_buf();
    let paths = ConfigPaths::new_isolated(&runtime, &target, repo);
    fs::create_dir_all(&target).unwrap();
    save_settings_with_paths(Settings::default(), &paths).unwrap();
    let python = super::find_python().expect("repository Python interpreter");
    let runner = ProcessCommandRunner;

    switch_mode_with_paths_takeover_as_owner(
        crate::app_flavor::RoutingOwner::Release,
        "custom",
        false,
        &paths,
        &python,
        &runner,
    )
    .unwrap();
    fs::remove_file(paths.config_backup_path()).unwrap();
    switch_mode_with_paths_takeover_as_owner(
        crate::app_flavor::RoutingOwner::Release,
        "custom",
        true,
        &paths,
        &python,
        &runner,
    )
    .unwrap();

    let metadata = paths
        .config_backup_path()
        .with_file_name("config.toml.release.backup.takeover.json");
    assert!(!metadata.exists());

    switch_mode_with_paths_takeover_as_owner(
        crate::app_flavor::RoutingOwner::Release,
        "official",
        false,
        &paths,
        &python,
        &runner,
    )
    .unwrap();

    let restored = fs::read_to_string(paths.codex_config_path()).unwrap();
    assert!(restored.contains("name = \"OpenAI\""));
    assert!(!restored.contains("name = \"Codex Proxy\""));
    assert!(!restored.contains("base_url"));
}

#[test]
fn codex_overlay_owner_is_detected_from_managed_marker() {
    let text =
        "# BEGIN CODEX PROXY SESSION CONFIG\n# owner = beta\n# END CODEX PROXY SESSION CONFIG\n";
    assert_eq!(
        codex_overlay_owner(text),
        Some(crate::app_flavor::RoutingOwner::Beta)
    );
}

#[test]
fn switch_mode_official_uses_unified_history_bucket_by_default() {
    let root = temp_root("switch-official");
    let paths = test_paths(&root);
    let runner = RecordingRunner::successful();

    let status =
        switch_mode_with_paths("official", false, &paths, Path::new("python-test"), &runner)
            .expect("switch official");

    assert_eq!(status.mode, "official");
    assert_eq!(status.proxy_port, Settings::default().proxy_port);

    let commands = runner.commands.borrow();
    assert_eq!(commands.len(), 1);
    assert_eq!(
        commands[0].args[0],
        paths.config_overlay_script().to_string_lossy()
    );
    assert_contains_sequence(&commands[0].args, &["restore"]);
    assert_arg_value(&commands[0].args, "--config", &paths.codex_config_path());
    assert_arg_value(&commands[0].args, "--backup", &paths.config_backup_path());
    assert_arg_value(
        &commands[0].args,
        "--context-guard-state",
        &paths.context_guard_state_path(),
    );
    assert_eq!(
        paths
            .config_backup_path()
            .file_name()
            .and_then(|name| name.to_str()),
        Some("config.toml.release.backup")
    );
    assert!(commands[0]
        .args
        .iter()
        .any(|arg| arg == "--unified-history"));
}

#[test]
fn switch_mode_official_skips_unified_history_when_setting_is_disabled() {
    let root = temp_root("switch-official-unified-history");
    let paths = test_paths(&root);
    save_settings_with_paths(
        Settings {
            unified_codex_history: false,
            proxy_port: 4555,
            ..Settings::default()
        },
        &paths,
    )
    .expect("settings save");
    let runner = RecordingRunner::successful();

    let status =
        switch_mode_with_paths("official", true, &paths, Path::new("python-test"), &runner)
            .expect("switch official");

    assert_eq!(status.mode, "official");
    assert_eq!(status.proxy_port, 4555);
    let commands = runner.commands.borrow();
    assert_eq!(commands.len(), 1);
    assert_contains_sequence(&commands[0].args, &["restore"]);
    assert!(!commands[0]
        .args
        .iter()
        .any(|arg| arg == "--unified-history"));
    assert!(!commands[0].args.iter().any(|arg| arg == "normalize-fast"));
}

#[test]
fn switch_mode_official_without_history_ignores_corrupt_settings() {
    let root = temp_root("switch-official-corrupt-settings");
    let paths = test_paths(&root);
    fs::create_dir_all(paths.settings_path().parent().unwrap()).unwrap();
    fs::write(paths.settings_path(), "{not json").unwrap();
    let runner = RecordingRunner::successful();

    let status =
        switch_mode_with_paths("official", false, &paths, Path::new("python-test"), &runner)
            .expect("switch official");

    assert_eq!(status.mode, "official");
    assert_eq!(status.proxy_port, Settings::default().proxy_port);
    let commands = runner.commands.borrow();
    assert_eq!(commands.len(), 1);
    assert_contains_sequence(&commands[0].args, &["restore"]);
}

#[test]
fn switch_mode_does_not_run_history_when_overlay_fails() {
    let root = temp_root("switch-overlay-fails-after-history");
    let paths = test_paths(&root);
    save_settings_with_paths(
        Settings {
            proxy_port: 4555,
            ..Settings::default()
        },
        &paths,
    )
    .expect("settings save");
    let runner = RecordingRunner::failed(23, "overlay stdout", "overlay stderr");

    let error = switch_mode_with_paths("custom", true, &paths, Path::new("python-test"), &runner)
        .expect_err("overlay should fail");

    let commands = runner.commands.borrow();
    assert_eq!(commands.len(), 1);
    assert_contains_sequence(&commands[0].args, &["apply"]);
    assert!(!commands[0].args.iter().any(|arg| arg == "normalize-fast"));
    assert!(error.contains("config overlay apply failed"));
    assert!(!error.contains("history backup root"));
    assert!(error.contains("overlay stderr"));
}

#[test]
fn switch_mode_returns_stdout_stderr_context_when_python_fails() {
    let root = temp_root("switch-failure");
    let paths = test_paths(&root);
    let runner = RecordingRunner::failed(17, "printed stdout", "printed stderr");

    let error =
        switch_mode_with_paths("official", false, &paths, Path::new("python-test"), &runner)
            .expect_err("switch should fail");

    assert!(error.contains("config overlay restore failed"));
    assert!(error.contains("exit code 17"));
    assert!(error.contains("printed stdout"));
    assert!(error.contains("printed stderr"));
}

#[test]
fn committed_restore_cleanup_warning_is_exposed_in_switch_status() {
    let root = temp_root("switch-committed-cleanup-warning");
    let paths = test_paths(&root);
    let runner = RecordingRunner::sequence(vec![CommandOutcome {
        code: Some(0),
        stdout: "restored".to_string(),
        stderr: "warning: route committed; backup cleanup deferred (PermissionError)\n"
            .to_string(),
    }]);

    let status =
        switch_mode_with_paths("official", false, &paths, Path::new("python-test"), &runner)
            .expect("the committed route switch remains successful");

    assert!(status.message.contains("Switched to official mode"));
    assert!(status.message.contains("route committed"));
    assert!(status.message.contains("cleanup deferred"));
    assert!(status.message.contains("PermissionError"));
}

#[test]
fn context_guard_command_keeps_codex_and_gateway_state_in_sync() {
    let root = temp_root("context-guard-command");
    let paths = test_paths(&root);
    let status_json =
        r#"{"enabled":true,"model_context_window":272000,"model_auto_compact_token_limit":240000}"#;
    let set_runner = RecordingRunner::sequence(vec![CommandOutcome {
        code: Some(0),
        stdout: status_json.to_string(),
        stderr: String::new(),
    }]);

    let status =
        set_codex_context_guard_with_paths(true, &paths, Path::new("python-test"), &set_runner)
            .expect("context guard enabled");

    assert!(status.enabled);
    assert!(status.codex_enabled);
    assert!(status.gateway_enabled);
    assert_eq!(status.model_context_window, Some(272_000));
    assert_eq!(status.model_auto_compact_token_limit, Some(240_000));
    assert!(!status.global_override_conflict);
    assert!(
        get_settings_with_paths(&paths)
            .expect("saved settings")
            .openai_context_guard_enabled
    );
    let set_commands = set_runner.commands.borrow();
    assert_eq!(set_commands.len(), 1);
    assert_contains_sequence(
        &set_commands[0].args,
        &[
            "context-guard-set",
            "--config",
            "--backup",
            "--state",
            "--enabled",
            "true",
        ],
    );
    drop(set_commands);

    let get_runner = RecordingRunner::sequence(vec![CommandOutcome {
        code: Some(0),
        stdout: status_json.to_string(),
        stderr: String::new(),
    }]);
    let refreshed =
        get_codex_context_guard_status_with_paths(&paths, Path::new("python-test"), &get_runner)
            .expect("context guard status");
    assert!(refreshed.enabled);
    assert_contains_sequence(
        &get_runner.commands.borrow()[0].args,
        &["context-guard-status", "--config"],
    );
}

#[test]
fn runtime_projection_recognizes_only_an_explicit_official_top_level_model() {
    assert!(top_level_model_is_official("model = \"gpt-5.6-terra\"\n"));
    assert!(top_level_model_is_official(
        "model = 'openai/gpt-5.6-terra'\n"
    ));
    assert!(!top_level_model_is_official("model = \"volc/glm-5.2\"\n"));
    assert!(!top_level_model_is_official(
        "[profiles.work]\nmodel = \"gpt-5.6-terra\"\n"
    ));
}

#[test]
fn refreshed_budget_reapplies_the_owned_codex_overlay_from_the_published_catalog() {
    let root = temp_root("republish-owned-context-budget");
    let paths = test_paths(&root);
    fs::create_dir_all(paths.codex_config_path().parent().unwrap()).unwrap();
    fs::write(
        paths.codex_config_path(),
        concat!(
            "# BEGIN CODEX PROXY SESSION CONFIG\n",
            "# owner = release\n",
            "# END CODEX PROXY SESSION CONFIG\n",
            "model = \"gpt-5.6-terra\"\n",
        ),
    )
    .unwrap();
    save_settings_with_paths(Settings::default(), &paths).expect("settings save");
    let runner = RecordingRunner::successful();

    let changed = republish_managed_codex_context_budget_with_paths(
        &paths,
        Path::new("python-test"),
        &runner,
    )
    .expect("owned runtime projection");

    assert!(
        !changed,
        "the recording runner leaves the config text unchanged"
    );
    let commands = runner.commands.borrow();
    assert_eq!(commands.len(), 1);
    assert_contains_sequence(
        &commands[0].args,
        &[
            "apply",
            "--config",
            "--backup",
            "--catalog",
            "--owner",
            "release",
        ],
    );
    assert_arg_value(
        &commands[0].args,
        "--catalog",
        &paths.generated_catalog_path(),
    );
}

#[test]
fn startup_context_migration_targets_a_foreign_channel_backup() {
    let root = temp_root("startup-context-migration-foreign-channel");
    let paths = test_paths(&root);
    fs::create_dir_all(paths.codex_config_path().parent().unwrap()).unwrap();
    fs::write(
        paths.codex_config_path(),
        "# BEGIN CODEX PROXY SESSION CONFIG\n# owner = beta\n# END CODEX PROXY SESSION CONFIG\nmodel = \"volc/glm-5.2\"\n",
    )
    .unwrap();
    let current_owner = crate::app_flavor::current().routing_owner();
    let backup_path = paths
        .config_backup_path_for_target_owner(current_owner, crate::app_flavor::RoutingOwner::Beta);
    let other_backup_path = paths.config_backup_path_for_target_owner(
        current_owner,
        crate::app_flavor::RoutingOwner::Release,
    );
    for path in [&backup_path, &other_backup_path] {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, "legacy backup").unwrap();
    }
    let runner = RecordingRunner::successful();

    migrate_legacy_context_guard_with_paths(&paths, Path::new("python-test"), &runner)
        .expect("startup context migration");

    let commands = runner.commands.borrow();
    assert_eq!(commands.len(), 1);
    assert_contains_sequence(
        &commands[0].args,
        &[
            "migrate-context-guard",
            "--config",
            "--backup",
            "--context-guard-state",
        ],
    );
    assert_eq!(
        commands[0]
            .args
            .iter()
            .filter(|argument| argument.as_str() == "--backup")
            .count(),
        2
    );
    assert!(commands[0]
        .args
        .iter()
        .any(|argument| argument == &backup_path.to_string_lossy()));
    assert!(commands[0]
        .args
        .iter()
        .any(|argument| argument == &other_backup_path.to_string_lossy()));
    assert_arg_value(
        &commands[0].args,
        "--context-guard-state",
        &paths.context_guard_state_path(),
    );
}

#[derive(Debug, Clone)]
struct RecordedCommand {
    args: Vec<String>,
}

struct RecordingRunner {
    commands: RefCell<Vec<RecordedCommand>>,
    outcomes: RefCell<Vec<CommandOutcome>>,
}

impl RecordingRunner {
    fn successful() -> Self {
        Self::sequence(vec![CommandOutcome {
            code: Some(0),
            stdout: "ok".to_string(),
            stderr: String::new(),
        }])
    }

    fn failed(code: i32, stdout: &str, stderr: &str) -> Self {
        Self::sequence(vec![CommandOutcome {
            code: Some(code),
            stdout: stdout.to_string(),
            stderr: stderr.to_string(),
        }])
    }

    fn sequence(outcomes: Vec<CommandOutcome>) -> Self {
        Self {
            commands: RefCell::new(Vec::new()),
            outcomes: RefCell::new(outcomes),
        }
    }
}

impl CommandRunner for RecordingRunner {
    fn run(&self, _program: &Path, args: &[String]) -> Result<CommandOutcome, String> {
        self.commands.borrow_mut().push(RecordedCommand {
            args: args.to_vec(),
        });
        let mut outcomes = self.outcomes.borrow_mut();
        let outcome = if outcomes.len() > 1 {
            outcomes.remove(0)
        } else {
            outcomes
                .first()
                .cloned()
                .expect("recording runner requires at least one outcome")
        };
        Ok(outcome)
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
        "codexhub-config-{name}-{}-{suffix}",
        std::process::id()
    ));
    let _ = fs::remove_dir_all(&path);
    fs::create_dir_all(&path).unwrap();
    path
}

fn assert_json_eq<T: serde::Serialize>(left: &T, right: &T) {
    assert_eq!(
        serde_json::to_value(left).unwrap(),
        serde_json::to_value(right).unwrap()
    );
}

fn assert_settings_eq(left: &Settings, right: &Settings) {
    assert_eq!(left.locale, right.locale);
    assert_eq!(left.auto_sync_history, right.auto_sync_history);
    assert_eq!(left.unified_codex_history, right.unified_codex_history);
    assert_eq!(left.auto_start_software, right.auto_start_software);
    assert_eq!(left.auto_start_gateway, right.auto_start_gateway);
    assert_eq!(left.include_official_models, right.include_official_models);
    assert_eq!(left.auto_sync_catalog, right.auto_sync_catalog);
    assert_eq!(left.auto_sync_clients, right.auto_sync_clients);
    assert_eq!(left.default_codex_route, right.default_codex_route);
    assert_eq!(left.gateway_bind_address, right.gateway_bind_address);
    assert_eq!(left.gateway_client_key, right.gateway_client_key);
    assert_eq!(left.gateway_enable_models, right.gateway_enable_models);
    assert_eq!(
        left.gateway_enable_responses,
        right.gateway_enable_responses
    );
    assert_eq!(
        left.gateway_enable_chat_completions,
        right.gateway_enable_chat_completions
    );
    assert_eq!(
        left.gateway_request_timeout_seconds,
        right.gateway_request_timeout_seconds
    );
    assert_eq!(
        left.gateway_auto_retry_enabled,
        right.gateway_auto_retry_enabled
    );
    assert_eq!(
        left.gateway_auto_retry_max_attempts,
        right.gateway_auto_retry_max_attempts
    );
    assert_eq!(
        left.gateway_image_proxy_enabled,
        right.gateway_image_proxy_enabled
    );
    assert_eq!(
        left.gateway_image_proxy_model,
        right.gateway_image_proxy_model
    );
    assert_eq!(
        left.openai_context_guard_enabled,
        right.openai_context_guard_enabled
    );
    assert_eq!(
        left.gateway_fast_model_variants,
        right.gateway_fast_model_variants
    );
    assert_eq!(
        left.official_disabled_models,
        right.official_disabled_models
    );
    assert_eq!(
        left.official_model_sort_order,
        right.official_model_sort_order
    );
    assert_eq!(
        left.official_provider_sort_order,
        right.official_provider_sort_order
    );
    assert_eq!(left.proxy_port, right.proxy_port);
}

fn assert_contains_sequence(args: &[String], values: &[&str]) {
    let mut position = 0;
    for value in values {
        position = args[position..]
            .iter()
            .position(|arg| arg == value)
            .map(|offset| position + offset + 1)
            .unwrap_or_else(|| panic!("missing argument {value:?} in {args:?}"));
    }
}

fn assert_arg_value(args: &[String], name: &str, expected: &Path) {
    assert_arg_literal(args, name, &expected.to_string_lossy());
}

fn assert_arg_literal(args: &[String], name: &str, expected: &str) {
    assert_eq!(arg_value(args, name), expected);
}

fn arg_value<'a>(args: &'a [String], name: &str) -> &'a str {
    let index = args
        .iter()
        .position(|arg| arg == name)
        .unwrap_or_else(|| panic!("missing argument {name:?} in {args:?}"));
    args.get(index + 1)
        .unwrap_or_else(|| panic!("missing value for {name:?} in {args:?}"))
}

mod isolated_codex_managed_config {
    use super::super::{
        apply_codex_config_isolated, populate_isolated_repo_resources,
        preview_codex_config_isolated, readback_codex_config_isolated,
    };
    use super::{
        assert_arg_literal, assert_arg_value, assert_contains_sequence, save_settings_with_paths,
        temp_root, CommandOutcome, CommandRunner, ConfigPaths, RecordedCommand, Settings,
    };
    use std::cell::RefCell;
    use std::fs;
    use std::path::Path;

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
                stdout: String::new(),
                stderr: String::new(),
            })
        }
    }

    fn isolated_paths(root: &Path) -> ConfigPaths {
        ConfigPaths::new_isolated(
            root.join("runtime"),
            root.join("codex-target"),
            root.join("repo"),
        )
    }

    /// A config.toml that mirrors the production overlay output: the
    /// `# owner = release|beta` marker, `model_provider = "custom"`, and
    /// `[model_providers.custom]` with `wire_api = "responses"`. Used by
    /// readback tests that need a fully-bound, overlay-produced file.
    fn overlay_managed_config(owner: &str, model: &str) -> String {
        format!(
            "# owner = {owner}\n\
             model = \"{model}\"\n\
             model_provider = \"custom\"\n\
             openai_base_url = \"http://127.0.0.1:9099/v1\"\n\
             [model_providers.custom]\n\
             name = \"Codex Proxy\"\n\
             requires_openai_auth = true\n\
             supports_websockets = true\n\
             wire_api = \"responses\"\n",
        )
    }

    #[test]
    fn codex_preview_under_isolated_root_reports_relative_target_and_no_secret() {
        let root = temp_root("codex-preview-isolated");
        let paths = isolated_paths(&root);
        let catalog_path = paths.generated_catalog_path();
        let preview =
            preview_codex_config_isolated(&paths, "custom", "gpt-5.6-luna", Some(&catalog_path))
                .unwrap();

        assert_eq!(preview.client_id, "codex");
        // F4: the Codex preview now reports the real overlay provider/route
        // binding (model_provider = "custom", wire_api = "responses").
        assert_eq!(preview.selector, "custom/gpt-5.6-luna");
        assert_eq!(preview.model, "gpt-5.6-luna");
        assert_eq!(preview.route_protocol, "responses");
        assert!(preview.target_names.iter().all(|name| {
            !name.contains(':') && !name.starts_with('/') && !name.starts_with('\\')
        }));
        let json = serde_json::to_string(&preview).unwrap();
        assert!(
            !json.contains(&root.to_string_lossy().to_string()),
            "absolute path leaked: {json}"
        );
    }

    #[test]
    fn codex_apply_under_isolated_root_invokes_overlay_with_isolated_paths() {
        let root = temp_root("codex-apply-isolated");
        let paths = isolated_paths(&root);
        fs::create_dir_all(paths.proxy_dir()).unwrap();
        // Settings required by switch_mode to build base-url and gateway-key.
        save_settings_with_paths(
            Settings {
                proxy_port: 9099,
                gateway_client_key: "isolated-key".to_string(),
                ..Settings::default()
            },
            &paths,
        )
        .unwrap();
        let runner = RecordingRunner::successful();

        let catalog_path = paths.generated_catalog_path();
        let result = apply_codex_config_isolated(
            &paths,
            "custom",
            false,
            "gpt-5.6-luna",
            Some(&catalog_path),
            Path::new("python-test"),
            &runner,
        )
        .unwrap();
        assert_eq!(result.mode, "custom");

        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 1);
        assert_contains_sequence(&commands[0].args, &["apply"]);
        assert_arg_value(&commands[0].args, "--config", &paths.codex_config_path());
        assert_arg_value(&commands[0].args, "--backup", &paths.config_backup_path());
        assert_arg_value(
            &commands[0].args,
            "--context-guard-state",
            &paths.context_guard_state_path(),
        );
        assert_arg_value(
            &commands[0].args,
            "--catalog",
            &paths.generated_catalog_path(),
        );
        assert_arg_literal(&commands[0].args, "--base-url", "http://127.0.0.1:9099");
        assert_arg_literal(&commands[0].args, "--gateway-key", "isolated-key");
        // All config/backup/catalog paths stay beneath the isolated root.
        assert!(paths.codex_config_path().starts_with(&root));
        assert!(paths.config_backup_path().starts_with(&root));
        assert!(paths.generated_catalog_path().starts_with(&root));
    }

    // F3: the isolated Codex apply path must populate the isolated repo
    // with the real production overlay resources (src-python modules and
    // the bundled providers.toml) so `config_overlay.py` and its sibling
    // imports resolve without host discovery. The apply step invokes the
    // production Python overlay by absolute script path; without this
    // population the script and its imports would not exist under the
    // isolated root.
    #[test]
    fn populate_isolated_repo_resources_copies_production_overlay_modules() {
        let root = temp_root("codex-populate-repo");
        let paths = isolated_paths(&root);
        populate_isolated_repo_resources(&paths).unwrap();

        // The overlay script itself must exist under the isolated repo.
        let overlay = paths.config_overlay_script();
        assert!(
            overlay.is_file(),
            "config_overlay.py must be copied to isolated repo: {}",
            overlay.display()
        );
        // The sibling modules the overlay imports must also be present.
        for module in ["atomic_io.py", "model_limits.py"] {
            let module_path = paths.repo_root.join("src-python").join(module);
            assert!(
                module_path.is_file(),
                "overlay sibling module {module} must be copied: {}",
                module_path.display()
            );
        }
        let compatibility_init = paths
            .repo_root
            .join("src-python")
            .join("tool_compatibility")
            .join("__init__.py");
        assert!(
            compatibility_init.is_file(),
            "tool_compatibility package must be copied: {}",
            compatibility_init.display()
        );
        let gateway_compat_init = paths
            .repo_root
            .join("src-python")
            .join("gateway_compat")
            .join("__init__.py");
        assert!(
            gateway_compat_init.is_file(),
            "gateway_compat package must be copied: {}",
            gateway_compat_init.display()
        );
        // The bundled providers.toml referenced by providers_config must
        // exist beneath the isolated repo so no host config/ discovery
        // leaks into the isolated apply path.
        assert!(
            paths.bundled_providers_path().is_file(),
            "bundled providers.toml must be copied to isolated repo"
        );
        // Everything copied stays beneath the isolated root.
        assert!(overlay.starts_with(&root));
        assert!(paths.bundled_providers_path().starts_with(&root));
    }

    #[test]
    fn codex_readback_under_isolated_root_verifies_overlay_owner_marker() {
        let root = temp_root("codex-readback-isolated");
        let paths = isolated_paths(&root);
        fs::create_dir_all(paths.codex_config_path().parent().unwrap()).unwrap();
        // No config.toml present -> readback fails closed (missing).
        let error = readback_codex_config_isolated(&paths, "gpt-5.6-luna").unwrap_err();
        assert!(
            error.contains("missing") || error.contains("absent"),
            "unexpected error: {error}"
        );

        // Write a fully-bound overlay-produced config.toml; readback must
        // confirm owner, model_provider, and wire_api all match.
        let owner_marker = match crate::app_flavor::current().routing_owner() {
            crate::app_flavor::RoutingOwner::Beta => "beta",
            _ => "release",
        };
        fs::write(
            paths.codex_config_path(),
            overlay_managed_config(owner_marker, "gpt-5.6-luna"),
        )
        .unwrap();
        let readback = readback_codex_config_isolated(&paths, "gpt-5.6-luna").unwrap();
        assert_eq!(readback.client_id, "codex");
        assert!(readback.ok);
        // F4: readback surfaces the real overlay route binding.
        assert_eq!(readback.selector, "custom/gpt-5.6-luna");
        assert_eq!(readback.route_protocol, "responses");
    }

    #[test]
    fn codex_readback_fails_closed_on_mismatched_stale_owner_marker() {
        let root = temp_root("codex-readback-stale-owner");
        let paths = isolated_paths(&root);
        fs::create_dir_all(paths.codex_config_path().parent().unwrap()).unwrap();
        // The current app flavor is Stable (RoutingOwner::Release); a beta
        // owner marker is a stale, cross-channel owner that readback must
        // reject without mutating the file. The provider binding is
        // production-shaped so the failure is owner-only, not provider.
        fs::write(
            paths.codex_config_path(),
            overlay_managed_config("beta", "gpt-5.6-luna"),
        )
        .unwrap();
        let error = readback_codex_config_isolated(&paths, "gpt-5.6-luna").unwrap_err();
        assert!(
            error.contains("stale") || error.contains("owner"),
            "unexpected error: {error}"
        );
        // Readback must fail closed without altering the file.
        assert_eq!(
            fs::read_to_string(paths.codex_config_path()).unwrap(),
            overlay_managed_config("beta", "gpt-5.6-luna"),
        );
    }

    #[test]
    fn codex_readback_fails_closed_on_absent_owner_marker() {
        let root = temp_root("codex-readback-absent-owner");
        let paths = isolated_paths(&root);
        fs::create_dir_all(paths.codex_config_path().parent().unwrap()).unwrap();
        // A config.toml with no `# owner = ...` marker was not produced
        // by this app's overlay; readback must fail closed. (The provider
        // here is intentionally "openai", not "custom", so a future
        // relaxation of the owner check would still fail on provider.)
        fs::write(
            paths.codex_config_path(),
            "model = \"gpt-5.6-luna\"\nmodel_provider = \"openai\"\n",
        )
        .unwrap();
        let error = readback_codex_config_isolated(&paths, "gpt-5.6-luna").unwrap_err();
        assert!(
            error.contains("stale") || error.contains("absent") || error.contains("owner"),
            "unexpected error: {error}"
        );
        assert_eq!(
            fs::read_to_string(paths.codex_config_path()).unwrap(),
            "model = \"gpt-5.6-luna\"\nmodel_provider = \"openai\"\n",
        );
    }

    #[test]
    fn codex_readback_fails_closed_when_provider_binding_is_absent() {
        let root = temp_root("codex-readback-no-provider");
        let paths = isolated_paths(&root);
        fs::create_dir_all(paths.codex_config_path().parent().unwrap()).unwrap();
        // Owner marker is valid but model_provider is missing — this was
        // not produced by the overlay; readback must fail closed.
        let owner_marker = match crate::app_flavor::current().routing_owner() {
            crate::app_flavor::RoutingOwner::Beta => "beta",
            _ => "release",
        };
        fs::write(
            paths.codex_config_path(),
            format!("# owner = {owner_marker}\nmodel = \"gpt-5.6-luna\"\n"),
        )
        .unwrap();
        let error = readback_codex_config_isolated(&paths, "gpt-5.6-luna").unwrap_err();
        assert!(
            error.contains("model_provider") || error.contains("provider"),
            "unexpected error: {error}"
        );
    }

    #[test]
    fn codex_readback_fails_closed_when_wire_api_is_not_responses() {
        let root = temp_root("codex-readback-wrong-wire-api");
        let paths = isolated_paths(&root);
        fs::create_dir_all(paths.codex_config_path().parent().unwrap()).unwrap();
        // Owner + provider are valid but wire_api is "chat_completions"
        // — the overlay always writes "responses", so this is a stale or
        // hand-edited config; readback must fail closed.
        let owner_marker = match crate::app_flavor::current().routing_owner() {
            crate::app_flavor::RoutingOwner::Beta => "beta",
            _ => "release",
        };
        fs::write(
            paths.codex_config_path(),
            format!(
                "# owner = {owner_marker}\n\
                 model = \"gpt-5.6-luna\"\n\
                 model_provider = \"custom\"\n\
                 [model_providers.custom]\n\
                 name = \"Codex Proxy\"\n\
                 wire_api = \"chat_completions\"\n",
            ),
        )
        .unwrap();
        let error = readback_codex_config_isolated(&paths, "gpt-5.6-luna").unwrap_err();
        assert!(
            error.contains("wire_api") || error.contains("responses"),
            "unexpected error: {error}"
        );
    }
}
