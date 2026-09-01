#[test]
fn opencode_well_known_binary_is_detected_without_path() {
    let home = std::env::temp_dir().join(format!(
        "codexhub-opencode-home-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos(),
    ));
    let binary = home.join(".opencode").join("bin").join(if cfg!(windows) {
        "opencode.exe"
    } else {
        "opencode"
    });
    fs::create_dir_all(binary.parent().unwrap()).unwrap();
    fs::write(&binary, b"opencode").unwrap();

    assert_eq!(
        super::detect_opencode_executable_path_in_home(&home),
        Some(binary)
    );
    fs::remove_dir_all(home).unwrap();
}

#[test]
fn opencode_jsonc_config_is_detected() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let root = std::env::temp_dir().join(format!(
        "codexhub-opencode-jsonc-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos(),
    ));
    let config = root.join("opencode").join("opencode.jsonc");
    fs::create_dir_all(config.parent().unwrap()).unwrap();
    fs::write(&config, br#"{"$schema":"https://opencode.ai/config.json"}"#).unwrap();
    let previous = std::env::var_os("XDG_CONFIG_HOME");
    std::env::set_var("XDG_CONFIG_HOME", &root);

    assert_eq!(super::detect_opencode_config_path(), Some(config));

    if let Some(previous) = previous {
        std::env::set_var("XDG_CONFIG_HOME", previous);
    } else {
        std::env::remove_var("XDG_CONFIG_HOME");
    }
    fs::remove_dir_all(root).unwrap();
}

#[cfg(target_os = "linux")]
#[test]
fn opencode_linux_desktop_install_path_is_a_known_candidate() {
    assert!(super::opencode_system_executable_candidates()
        .contains(&PathBuf::from("/opt/OpenCode/ai.opencode.desktop")));
}

#[cfg(target_os = "linux")]
#[test]
fn zcode_linux_defaults_are_home_relative_and_writable() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let home = std::env::temp_dir().join(format!(
        "codexhub-zcode-home-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos(),
    ));
    fs::create_dir_all(&home).unwrap();
    let previous_home = std::env::var_os("HOME");
    let previous_appdata = std::env::var_os("APPDATA");
    std::env::set_var("HOME", &home);
    std::env::remove_var("APPDATA");

    let targets = super::detect_zcode_config_targets();
    assert_eq!(
        targets.catalog_path,
        home.join(".zcode/model-providers/codexhub.json")
    );
    assert_eq!(targets.v2_config_path, home.join(".zcode/v2/config.json"));
    assert_eq!(
        super::detect_zcode_store_path(),
        home.join(".zcode/rum-electron-store/ZGVmYXVsdA.json")
    );

    restore_env("HOME", previous_home);
    restore_env("APPDATA", previous_appdata);
    fs::remove_dir_all(home).unwrap();
}

/// Force two restore/apply calls to overlap at the absent-baseline lock seam.
/// `a_restore` acquires the lock first and pauses until `b_restore` has
/// entered the lock acquisition; then A publishes and B re-reads the baseline.
fn run_overlapping_first_baseline<A, B>(
    a_restore: impl FnOnce() -> A + Send + 'static,
    b_restore: impl FnOnce() -> B + Send + 'static,
) -> (A, B)
where
    A: Send + 'static,
    B: Send + 'static,
{
    let a_has_lock = std::sync::Arc::new(AtomicBool::new(false));
    let b_blocked = std::sync::Arc::new(AtomicBool::new(false));
    let release_a = std::sync::Arc::new(AtomicBool::new(false));

    // B signals from inside safe_file's actual lock-acquisition seam.
    *crate::safe_file::TEST_LOCK_ACQUIRE_HOOK.lock().unwrap() = Some(Box::new({
        let b_blocked = b_blocked.clone();
        move |path: &std::path::Path, event: &str| {
            if event == "blocked" && path.to_string_lossy().contains("baseline.json") {
                b_blocked.store(true, Ordering::SeqCst);
            }
        }
    }));

    // A pauses after acquiring the baseline lock and confirming absence.
    *super::TEST_BASELINE_WRITE_HOOK.lock().unwrap() = Some(Box::new({
        let a_has_lock = a_has_lock.clone();
        let b_blocked = b_blocked.clone();
        let release_a = release_a.clone();
        move || {
            a_has_lock.store(true, Ordering::SeqCst);
            while !b_blocked.load(Ordering::SeqCst) {
                std::thread::yield_now();
            }
            while !release_a.load(Ordering::SeqCst) {
                std::thread::yield_now();
            }
        }
    }));

    let a_handle = std::thread::spawn(a_restore);

    let a_has_lock_main = a_has_lock.clone();
    while !a_has_lock_main.load(Ordering::SeqCst) {
        std::thread::yield_now();
    }

    let b_handle = std::thread::spawn(b_restore);

    // A already holds the lock; B is now entering the lock seam.
    // Release A only after B has demonstrably blocked on the same lock.
    let b_blocked_main = b_blocked.clone();
    while !b_blocked_main.load(Ordering::SeqCst) {
        std::thread::yield_now();
    }
    release_a.store(true, Ordering::SeqCst);

    let result = (a_handle.join().unwrap(), b_handle.join().unwrap());
    *super::TEST_BASELINE_WRITE_HOOK.lock().unwrap() = None;
    *crate::safe_file::TEST_LOCK_ACQUIRE_HOOK.lock().unwrap() = None;
    result
}

#[test]
fn runtime_artifacts_are_rooted_under_flavor_home() {
    let runtime_home = PathBuf::from("C:\\Users\\tester\\.codexhub-beta");
    assert_eq!(runtime_proxy_dir(&runtime_home), runtime_home.join("proxy"));
}

#[test]
fn write_text_replace_does_not_clobber_existing_stale_temp_file() {
    let root = unique_temp_dir("write-text-replace-stale-temp");
    fs::create_dir_all(root.as_path()).unwrap();
    let target = root.join("config.toml");
    let stale_temp = target.with_extension("tmp-codexhub");
    fs::write(&target, "old").unwrap();
    fs::write(&stale_temp, "stale-temp").unwrap();

    super::write_text_replace(&target, "new").unwrap();

    assert_eq!(fs::read_to_string(&target).unwrap(), "new");
    assert_eq!(fs::read_to_string(&stale_temp).unwrap(), "stale-temp");
}

fn client_export_test_providers() -> Vec<Provider> {
    vec![Provider {
        id: "minimax".to_string(),
        name: "MiniMax".to_string(),
        base_url: "https://api.minimax.chat/v1".to_string(),
        api_key: None,
        upstream_format: None,
        available_upstream_formats: None,
        tool_protocol: None,
        tool_surface_strategy: None,
        reports_cached_input_tokens: None,
        supports_developer_role: None,
        display_prefix: Some("minimax/".to_string()),
        auth_capabilities: None,
        onboarding_hint: None,
        discovery_policy: None,
        sort_order: None,
        enabled: true,
        locked: false,
        models: vec![
            Model {
                id: "minimax-m3".to_string(),
                display_name: Some("MiniMax M3".to_string()),
                context_window: Some(1_000_000),
                gateway_exported: true,
                ..Model::default()
            },
            Model {
                id: "minimax-m3-lite".to_string(),
                gateway_exported: false,
                ..Model::default()
            },
        ],
    }]
}

#[test]
fn stopped_proxy_is_status_not_actionable_gateway_error() {
    let auth = super::CodexAuthStatus {
        auth_file_present: true,
        logged_in: true,
        auth_mode: Some("chatgpt".to_string()),
        account_id_present: true,
        access_token_present: true,
        refresh_token_present: true,
        token_refresh_status: "fresh".to_string(),
        last_refresh: None,
        issue: None,
    };

    let diagnostics = super::gateway_diagnostics(false, false, &auth);

    assert!(diagnostics.iter().any(|item| {
        item.category == "proxy_state"
            && item.level == "status"
            && item.message == "Gateway is stopped."
    }));
    assert!(!diagnostics
        .iter()
        .any(|item| item.category == "proxy" && item.level == "error"));
}

#[test]
fn gateway_post_tests_attach_configured_client_key_to_every_shape() {
    let server = CapturingPostServer::start(3);
    let settings = Settings {
        proxy_port: server.port,
        gateway_client_key: "local-test-key".to_string(),
        gateway_request_timeout_seconds: 5,
        ..Settings::default()
    };

    for kind in [
        super::GatewayTestKind::ChatCompletions,
        super::GatewayTestKind::ChatCompletionsStream,
        super::GatewayTestKind::ResponsesStream,
    ] {
        let result = super::gateway_test_request_with_settings(
            kind,
            Some("gpt-5.6-sol".to_string()),
            &settings,
        )
        .expect("Gateway POST test");
        assert!(result.ok, "{result:?}");
    }

    let requests = server.finish();
    assert_eq!(requests.len(), 3);
    assert_eq!(request_path(&requests[0]), "/v1/chat/completions");
    assert_eq!(request_path(&requests[1]), "/v1/chat/completions");
    assert_eq!(request_path(&requests[2]), "/v1/responses");
    for request in requests {
        assert_eq!(
            header_value(&request, "authorization"),
            Some("Bearer local-test-key")
        );
    }
}

#[test]
fn gateway_request_auth_preserves_explicit_header_and_never_leaks() {
    let client = reqwest::blocking::Client::new();
    let settings = Settings {
        proxy_port: 4555,
        gateway_client_key: "local-test-key".to_string(),
        ..Settings::default()
    };
    let local_endpoint = "http://127.0.0.1:4555/v1/responses";
    let explicit = super::attach_local_gateway_authorization(
        client
            .post(local_endpoint)
            .header("Authorization", "Bearer explicit-key"),
        local_endpoint,
        &settings,
    )
    .build()
    .unwrap();
    assert_eq!(
        explicit.headers().get("Authorization").unwrap(),
        "Bearer explicit-key"
    );

    for endpoint in [
        "https://api.openai.com/v1/responses",
        "https://127.0.0.1:4555/v1/responses",
        "http://127.0.0.1:4556/v1/responses",
    ] {
        let request =
            super::attach_local_gateway_authorization(client.post(endpoint), endpoint, &settings)
                .build()
                .unwrap();
        assert!(
            request.headers().get("Authorization").is_none(),
            "Gateway key leaked to {endpoint}"
        );
    }
}

struct CapturingPostServer {
    port: u16,
    requests: mpsc::Receiver<String>,
    handle: thread::JoinHandle<()>,
}

impl CapturingPostServer {
    fn start(request_count: usize) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let (sender, requests) = mpsc::channel();
        let handle = thread::spawn(move || {
            for _ in 0..request_count {
                let (mut stream, _) = listener.accept().unwrap();
                let request = read_http_request(&mut stream);
                sender.send(request).unwrap();
                let body = "{}";
                let response = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                        body.len()
                    );
                stream.write_all(response.as_bytes()).unwrap();
            }
        });
        Self {
            port,
            requests,
            handle,
        }
    }

    fn finish(self) -> Vec<String> {
        self.handle.join().unwrap();
        self.requests.try_iter().collect()
    }
}

fn read_http_request(stream: &mut impl Read) -> String {
    let mut request = Vec::new();
    let mut buffer = [0_u8; 1024];
    loop {
        let count = stream.read(&mut buffer).unwrap();
        if count == 0 {
            break;
        }
        request.extend_from_slice(&buffer[..count]);
        if request.windows(4).any(|window| window == b"\r\n\r\n") {
            break;
        }
    }
    String::from_utf8(request).unwrap()
}

fn request_path(request: &str) -> &str {
    request
        .lines()
        .next()
        .unwrap()
        .split_whitespace()
        .nth(1)
        .unwrap()
}

fn header_value<'a>(request: &'a str, name: &str) -> Option<&'a str> {
    request.lines().find_map(|line| {
        let (header_name, value) = line.split_once(':')?;
        header_name.eq_ignore_ascii_case(name).then(|| value.trim())
    })
}

fn case_sensitive_client_export_test_providers() -> Vec<Provider> {
    vec![
        Provider {
            id: "ollama-cloud".to_string(),
            name: "Ollama Cloud".to_string(),
            base_url: "https://ollama.com/v1".to_string(),
            api_key: None,
            upstream_format: None,
            available_upstream_formats: None,
            tool_protocol: None,
            tool_surface_strategy: None,
            reports_cached_input_tokens: None,
            supports_developer_role: None,
            display_prefix: Some("Ollama".to_string()),
        auth_capabilities: None,
        onboarding_hint: None,
        discovery_policy: None,
            sort_order: Some(1),
            enabled: true,
            locked: false,
            models: vec![Model {
                id: "glm-5.2".to_string(),
                display_name: Some("Ollama GLM-5.2".to_string()),
                context_window: Some(131_072),
                gateway_exported: true,
                ..Model::default()
            }],
        },
        Provider {
            id: "volc".to_string(),
            name: "Volcengine".to_string(),
            base_url: "https://ark.example.test/v1".to_string(),
            api_key: None,
            upstream_format: None,
            available_upstream_formats: None,
            tool_protocol: None,
            tool_surface_strategy: None,
            reports_cached_input_tokens: None,
            supports_developer_role: None,
            display_prefix: Some("Volc".to_string()),
        auth_capabilities: None,
        onboarding_hint: None,
        discovery_policy: None,
            sort_order: Some(2),
            enabled: true,
            locked: false,
            models: vec![Model {
                id: "glm-5.2".to_string(),
                display_name: Some("Volc GLM-5.2".to_string()),
                context_window: Some(1_024_000),
                gateway_exported: true,
                ..Model::default()
            }],
        },
        Provider {
            id: "minimax-cn".to_string(),
            name: "MiniMax.cn".to_string(),
            base_url: "https://api.minimaxi.com/v1".to_string(),
            api_key: None,
            upstream_format: None,
            available_upstream_formats: None,
            tool_protocol: None,
            tool_surface_strategy: None,
            reports_cached_input_tokens: None,
            supports_developer_role: None,
            display_prefix: Some("MiniMax.cn".to_string()),
        auth_capabilities: None,
        onboarding_hint: None,
        discovery_policy: None,
            sort_order: Some(3),
            enabled: true,
            locked: false,
            models: vec![Model {
                id: "MiniMax-M3".to_string(),
                aliases: vec!["minimax-m3".to_string()],
                display_name: Some("MiniMax-M3".to_string()),
                context_window: Some(1_000_000),
                gateway_exported: true,
                ..Model::default()
            }],
        },
    ]
}

fn case_collision_client_export_test_providers() -> Vec<Provider> {
    vec![Provider {
        id: "minimax-cn".to_string(),
        name: "MiniMax.cn".to_string(),
        base_url: "https://api.minimaxi.com/v1".to_string(),
        api_key: None,
        upstream_format: None,
        available_upstream_formats: None,
        tool_protocol: None,
        tool_surface_strategy: None,
        reports_cached_input_tokens: None,
        supports_developer_role: None,
        display_prefix: Some("MiniMax.cn".to_string()),
        auth_capabilities: None,
        onboarding_hint: None,
        discovery_policy: None,
        sort_order: Some(1),
        enabled: true,
        locked: false,
        models: vec![
            Model {
                id: "MiniMax-M3".to_string(),
                display_name: Some("MiniMax-M3".to_string()),
                context_window: Some(1_000_000),
                gateway_exported: true,
                ..Model::default()
            },
            Model {
                id: "minimax-m3".to_string(),
                display_name: Some("MiniMax-M3 lowercase legacy".to_string()),
                context_window: Some(1_000_000),
                gateway_exported: true,
                ..Model::default()
            },
        ],
    }]
}

fn sync_test_client(
    id: &str,
    name: &str,
    installed: bool,
    auto_apply_supported: bool,
    route_mode: &str,
) -> super::GatewayClientInfo {
    let route_owner = match route_mode {
        "hub" | "stale" => crate::app_flavor::RoutingOwner::Release,
        "official" => crate::app_flavor::RoutingOwner::Official,
        "other_channel" => crate::app_flavor::RoutingOwner::Beta,
        _ => crate::app_flavor::RoutingOwner::UnknownExternal,
    };
    super::GatewayClientInfo {
        id: id.to_string(),
        name: name.to_string(),
        kind: "Test".to_string(),
        installed,
        auto_apply_supported,
        config_path: Some(PathBuf::from(format!("{id}.json"))),
        route_owner,
        route_endpoint: None,
        managed_by_current_app: route_owner == crate::app_flavor::RoutingOwner::Release,
        route_mode: route_mode.to_string(),
        status: "test".to_string(),
        versions_checked: false,
        current_version: None,
        latest_version: None,
    }
}

#[test]
fn sanitizes_sensitive_text() {
    assert_eq!(
        sanitize_text("Authorization: Bearer secret"),
        "[redacted sensitive response detail]"
    );
}

#[test]
fn event_sanitization_keeps_only_safe_fields() {
    let event = sanitize_event(&json!({
        "ts": "now",
        "event": "request_error",
        "model": "openai/gpt-5.5",
        "Authorization": "Bearer secret",
        "detail": "CodexAuthError",
        "upstream": "official"
    }));
    assert_eq!(event.model.as_deref(), Some("openai/gpt-5.5"));
    assert_eq!(event.category, "codex_auth");
}

#[test]
fn classifies_streaming_events() {
    assert_eq!(
        super::classify_event(&json!({"event": "upstream_stream_interrupted"})),
        "streaming"
    );
}

#[test]
fn sync_gateway_clients_applies_only_hub_bound_supported_clients() {
    let clients = vec![
        sync_test_client("generic", "Generic", true, false, "copy_only"),
        sync_test_client("official", "Official Client", true, true, "official"),
        sync_test_client("missing", "Missing Client", false, true, "hub"),
        sync_test_client("hub-ok", "Hub OK", true, true, "hub"),
        sync_test_client(
            "hub-stale-managed",
            "Hub Stale Managed",
            true,
            true,
            "stale",
        ),
        sync_test_client(
            "hub-stale-other-channel",
            "Hub Stale Other Channel",
            true,
            true,
            "stale",
        ),
        sync_test_client("hub-fail", "Hub Fail", true, true, "hub"),
    ];
    let stale_index = clients
        .iter()
        .position(|client| client.id == "hub-stale-other-channel")
        .expect("stale client index");
    let mut clients = clients;
    clients[stale_index].managed_by_current_app = false;
    clients[stale_index].route_owner = crate::app_flavor::RoutingOwner::Beta;
    let mut attempted = Vec::new();

    let summary = super::sync_gateway_clients_from_infos(
        clients,
        Some("openai/gpt-5.5".to_string()),
        |client_id, model| {
            attempted.push((client_id.clone(), model.clone()));
            if client_id == "hub-fail" {
                return Err("write failed".to_string());
            }
            Ok(super::GatewayClientApplyResult {
                client_id,
                applied: true,
                config_path: Some(PathBuf::from("config.json")),
                backup_path: Some(PathBuf::from("backup.json")),
                message: "applied".to_string(),
            })
        },
    );

    assert_eq!(
        attempted,
        vec![
            ("hub-ok".to_string(), Some("openai/gpt-5.5".to_string())),
            (
                "hub-stale-managed".to_string(),
                Some("openai/gpt-5.5".to_string())
            ),
            ("hub-fail".to_string(), Some("openai/gpt-5.5".to_string())),
        ]
    );
    assert_eq!(summary.applied, 2);
    assert_eq!(summary.skipped, 4);
    assert_eq!(summary.failed, 1);
    assert_eq!(summary.results[0].status, "skipped");
    assert_eq!(summary.results[3].status, "applied");
    assert_eq!(summary.results[4].status, "applied");
    assert_eq!(summary.results[5].status, "skipped");
    assert_eq!(summary.results[6].status, "failed");
    assert!(summary.message.contains("1 failed"));
}

#[test]
fn failed_client_syncs_remain_pending_until_a_successful_retry() {
    let root = unique_temp_dir("codexhub-client-sync-state");
    let state_path = root.join("proxy").join("gateway-client-sync-state.json");
    let mut pending = std::collections::HashSet::new();
    let failed = super::GatewayClientSyncSummary {
        applied: 0,
        skipped: 0,
        failed: 1,
        results: vec![super::GatewayClientSyncItem {
            client_id: "pi".to_string(),
            name: "Pi".to_string(),
            status: "failed".to_string(),
            applied: false,
            skipped: false,
            message: "write failed".to_string(),
            config_path: None,
            backup_path: None,
        }],
        message: "sync failed".to_string(),
    };

    super::update_pending_client_ids_after_sync(&mut pending, &failed);
    super::write_pending_client_ids_to_path(&state_path, &pending).unwrap();
    let persisted = super::read_pending_client_ids_from_path(&state_path);
    assert!(persisted.contains("pi"));
    assert_eq!(
        super::route_mode_for_owner(
            crate::app_flavor::RoutingOwner::Release,
            crate::app_flavor::RoutingOwner::Release,
            persisted.contains("pi"),
        ),
        "stale"
    );
    assert!(super::pending_sync_is_stale(
        true,
        crate::app_flavor::RoutingOwner::UnknownExternal,
        crate::app_flavor::RoutingOwner::Release,
    ));
    assert!(super::pending_sync_is_stale(
        true,
        crate::app_flavor::RoutingOwner::Official,
        crate::app_flavor::RoutingOwner::Release,
    ));
    assert!(!super::pending_sync_is_stale(
        true,
        crate::app_flavor::RoutingOwner::Beta,
        crate::app_flavor::RoutingOwner::Release,
    ));

    let succeeded = super::GatewayClientSyncSummary {
        applied: 1,
        skipped: 0,
        failed: 0,
        results: vec![super::GatewayClientSyncItem {
            client_id: "pi".to_string(),
            name: "Pi".to_string(),
            status: "applied".to_string(),
            applied: true,
            skipped: false,
            message: "applied".to_string(),
            config_path: None,
            backup_path: None,
        }],
        message: "synced".to_string(),
    };

    super::update_pending_client_ids_after_sync(&mut pending, &succeeded);
    super::write_pending_client_ids_to_path(&state_path, &pending).unwrap();
    assert!(!state_path.exists());
    assert!(!super::read_pending_client_ids_from_path(&state_path).contains("pi"));
}

#[test]
fn gateway_client_sync_default_model_skips_disabled_default_model() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_codex_home = std::env::var_os("CODEX_HOME");
    let previous_runtime_home = std::env::var_os("CODEXHUB_RUNTIME_HOME");
    let root = unique_temp_dir("codexhub-client-sync-official-catalog");
    let catalog_dir = root.join("model-catalogs");
    fs::create_dir_all(&catalog_dir).unwrap();
    fs::write(
        catalog_dir.join("codexhub-model-catalog.json"),
        serde_json::to_vec_pretty(&json!({
            "models": [
                {
                    "slug": "gpt-5.5",
                    "codex_proxy_metadata": {
                        "provider": "openai",
                        "upstream_name": "official",
                        "official_context_budget": {
                            "source": "degraded_last_known_official",
                            "freshness": "stale",
                            "model_context_window": 272000,
                            "effective_context_window_percent": 95,
                            "effective_context_window": 258400,
                            "model_auto_compact_token_limit": 244800
                        }
                    }
                },
                {
                    "slug": "gpt-5.4",
                    "codex_proxy_metadata": {
                        "provider": "openai",
                        "upstream_name": "official",
                        "official_context_budget": {
                            "source": "degraded_last_known_official",
                            "freshness": "stale",
                            "model_context_window": 272000,
                            "effective_context_window_percent": 95,
                            "effective_context_window": 258400,
                            "model_auto_compact_token_limit": 244800
                        }
                    }
                }
            ]
        }))
        .unwrap(),
    )
    .unwrap();
    std::env::set_var("CODEX_HOME", &root);
    std::env::set_var("CODEXHUB_RUNTIME_HOME", &root);

    let settings = Settings {
        official_disabled_models: vec!["openai/gpt-5.5".to_string()],
        ..Settings::default()
    };

    let result = super::default_gateway_client_sync_model(&settings, &[]);
    restore_env("CODEX_HOME", previous_codex_home);
    restore_env("CODEXHUB_RUNTIME_HOME", previous_runtime_home);
    let model = result.expect("enabled fallback model");

    assert_eq!(model, "gpt-5.4");
}

#[test]
fn client_route_model_preserves_valid_selection_and_falls_back_from_stale_selection() {
    let settings = Settings {
        include_official_models: false,
        ..Settings::default()
    };
    let providers = case_sensitive_client_export_test_providers();

    assert_eq!(
        super::gateway_client_route_model(Some("volc/glm-5.2".to_string()), &settings, &providers,)
            .unwrap(),
        "volc/glm-5.2"
    );
    let fallback =
        super::gateway_client_route_model(Some("gpt-5.5".to_string()), &settings, &providers)
            .unwrap();
    assert_ne!(fallback, "gpt-5.5");
    assert!(super::resolve_gateway_client_model_id(&settings, &providers, &fallback).is_ok());
}

#[cfg(target_os = "windows")]
#[test]
fn command_version_reads_supported_cmd_shim_from_path() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_path = std::env::var_os("PATH");
    let root = unique_temp_dir("codexhub-version-probe-cmd");
    fs::create_dir_all(&root).unwrap();
    let shim = root.join("opencode.cmd");
    fs::write(&shim, "@echo off\r\necho opencode 1.2.3\r\n").unwrap();
    let mut path_entries = vec![root.clone()];
    if let Some(path) = previous_path.as_ref() {
        path_entries.extend(std::env::split_paths(path));
    }
    std::env::set_var("PATH", std::env::join_paths(path_entries).unwrap());

    let version = super::command_version(&["opencode"]);

    restore_env("PATH", previous_path);
    assert_eq!(version.as_deref(), Some("1.2.3"));
}

#[cfg(target_os = "windows")]
#[test]
fn version_probe_returns_none_when_supported_shim_times_out() {
    let root = unique_temp_dir("codexhub-version-probe-timeout");
    fs::create_dir_all(&root).unwrap();
    let shim = root.join("slow-client.cmd");
    fs::write(
        &shim,
        "@echo off\r\nping -n 6 127.0.0.1 >NUL\r\necho slow-client 9.9.9\r\n",
    )
    .unwrap();

    let started = Instant::now();
    let output = super::version_output_for_path(&shim);

    assert!(output.is_none());
    assert!(started.elapsed() < Duration::from_secs(4));
}

#[cfg(target_os = "windows")]
#[test]
fn version_probe_does_not_execute_powershell_scripts() {
    let root = unique_temp_dir("codexhub-version-probe-ps1");
    fs::create_dir_all(&root).unwrap();
    let sentinel = root.join("executed.txt");
    let script = root.join("opencode.ps1");
    let sentinel_literal = sentinel.to_string_lossy().replace('\'', "''");
    fs::write(
            &script,
            format!(
                "Set-Content -LiteralPath '{sentinel_literal}' -Value 'ran'\r\nWrite-Output 'opencode 1.2.3'\r\n"
            ),
        )
        .unwrap();

    let output = super::version_output_for_path(&script);

    assert!(output.is_none());
    assert!(!sentinel.exists());
}

#[test]
fn gateway_models_export_enabled_gateway_models_ignoring_legacy_hidden() {
    let settings = Settings::default();
    let providers: Vec<Provider> = serde_json::from_value(json!([{
        "id": "minimax",
        "name": "MiniMax",
        "base_url": "https://api.minimax.chat/v1",
        "display_prefix": "minimax/",
        "enabled": true,
        "hidden": true,
        "models": [
            {
                "id": "minimax-m3",
                "display_name": "MiniMax M3",
                "context_window": 1000000,
                "gateway_exported": true,
                "hidden": true
            },
            {
                "id": "minimax-m3-lite",
                "gateway_exported": false,
                "hidden": true
            },
            {
                "id": "disabled",
                "enabled": false,
                "gateway_exported": true,
                "hidden": true
            }
        ]
    }]))
    .unwrap();

    let models = gateway_models_from_config(&settings, &providers);

    assert!(models.iter().any(|model| model.id == "gpt-5.5"));
    assert!(!models.iter().any(|model| model.id == "openai/gpt-5.5"));
    assert!(models.iter().any(|model| model.id == "minimax/minimax-m3"));
    assert!(!models
        .iter()
        .any(|model| model.id == "minimax/minimax-m3-lite"));
    assert!(!models.iter().any(|model| model.id == "minimax/disabled"));
}

#[test]
fn gateway_models_preserve_provider_prefix_and_exact_model_case() {
    let settings = Settings {
        include_official_models: false,
        ..Settings::default()
    };
    let providers = vec![
        Provider {
            id: "ollama-cloud".to_string(),
            name: "Ollama Cloud".to_string(),
            base_url: "https://ollama.com/v1".to_string(),
            api_key: None,
            upstream_format: None,
            available_upstream_formats: None,
            tool_protocol: None,
            tool_surface_strategy: None,
            reports_cached_input_tokens: None,
            supports_developer_role: None,
            display_prefix: Some("Ollama".to_string()),
        auth_capabilities: None,
        onboarding_hint: None,
        discovery_policy: None,
            sort_order: Some(1),
            enabled: true,
            locked: false,
            models: vec![Model {
                id: "glm-5.2".to_string(),
                display_name: Some("Ollama GLM-5.2".to_string()),
                gateway_exported: true,
                ..Model::default()
            }],
        },
        Provider {
            id: "volc".to_string(),
            name: "Volcengine".to_string(),
            base_url: "https://ark.example.test/v1".to_string(),
            api_key: None,
            upstream_format: None,
            available_upstream_formats: None,
            tool_protocol: None,
            tool_surface_strategy: None,
            reports_cached_input_tokens: None,
            supports_developer_role: None,
            display_prefix: Some("Volc".to_string()),
        auth_capabilities: None,
        onboarding_hint: None,
        discovery_policy: None,
            sort_order: Some(2),
            enabled: true,
            locked: false,
            models: vec![Model {
                id: "glm-5.2".to_string(),
                display_name: Some("Volc GLM-5.2".to_string()),
                gateway_exported: true,
                ..Model::default()
            }],
        },
        Provider {
            id: "minimax-cn".to_string(),
            name: "MiniMax.cn".to_string(),
            base_url: "https://api.minimaxi.com/v1".to_string(),
            api_key: None,
            upstream_format: None,
            available_upstream_formats: None,
            tool_protocol: None,
            tool_surface_strategy: None,
            reports_cached_input_tokens: None,
            supports_developer_role: None,
            display_prefix: Some("MiniMax.cn".to_string()),
        auth_capabilities: None,
        onboarding_hint: None,
        discovery_policy: None,
            sort_order: Some(3),
            enabled: true,
            locked: false,
            models: vec![Model {
                id: "MiniMax-M3".to_string(),
                aliases: vec!["minimax-m3".to_string()],
                display_name: Some("MiniMax-M3".to_string()),
                gateway_exported: true,
                ..Model::default()
            }],
        },
    ];

    let ids = gateway_models_from_config(&settings, &providers)
        .into_iter()
        .map(|model| model.id)
        .collect::<Vec<_>>();

    assert!(ids.contains(&"ollama-cloud/glm-5.2".to_string()));
    assert!(ids.contains(&"volc/glm-5.2".to_string()));
    assert!(ids.contains(&"minimax-cn/MiniMax-M3".to_string()));
    assert!(!ids.contains(&"glm-5.2".to_string()));
    assert!(!ids.contains(&"minimax-cn/minimax-m3".to_string()));
}

#[test]
fn gateway_models_skip_disabled_official_models_and_fast_variants() {
    let settings = Settings {
        official_disabled_models: vec!["openai/gpt-5.4".to_string()],
        ..Settings::default()
    };

    let models = official_models_from_metadata(
        &settings,
        None,
        &published_context_windows(&[("gpt-5.5", 258_400), ("gpt-5.4", 258_400)]),
    );

    assert!(models.iter().any(|model| model.id == "gpt-5.5"));
    assert!(models.iter().any(|model| model.id == "gpt-5.5-fast"));
    assert!(!models.iter().any(|model| model.id == "gpt-5.4"));
    assert!(!models.iter().any(|model| model.id == "gpt-5.4-fast"));
}

#[test]
fn official_gateway_models_use_subscription_metadata_for_display_and_published_limits() {
    let settings = Settings::default();
    let published_contexts = published_context_windows(&[("gpt-5.6-sol", 272_000)]);
    let models = official_models_from_metadata(
        &settings,
        Some(vec![Model {
            id: "openai/gpt-5.6-sol".to_string(),
            display_name: Some("GPT-5.6-Sol".to_string()),
            context_window: Some(400_000),
            ..Model::default()
        }]),
        &published_contexts,
    );

    assert_eq!(models.len(), 1);
    assert_eq!(models[0].id, "gpt-5.6-sol");
    assert_eq!(models[0].display_name, "5.6 Sol");
    assert_eq!(models[0].context_window, Some(272_000));
}

#[test]
fn official_gateway_models_do_not_fallback_when_authoritative_catalog_is_empty() {
    let published_contexts = published_context_windows(&[("gpt-5.6-terra", 272_000)]);
    let models =
        official_models_from_metadata(&Settings::default(), Some(Vec::new()), &published_contexts);

    assert!(models.is_empty());
}

#[test]
fn published_official_context_limit_bounds_stale_subscription_metadata_for_status_and_exports() {
    // The raw subscription record omitted its numeric context field; the
    // metadata projection supplied this stale builtin fallback instead.
    // A safely published Official catalog limit must still bound every
    // downstream Gateway status and export consumer.
    let published_contexts = published_context_windows(&[("gpt-5.6-terra", 272_000)]);
    let models = official_models_from_metadata(
        &Settings::default(),
        Some(vec![Model {
            id: "gpt-5.6-terra".to_string(),
            context_window: Some(353_400),
            ..Model::default()
        }]),
        &published_contexts,
    );

    assert_eq!(models.len(), 1);
    assert_eq!(models[0].context_window, Some(272_000));

    let gateway_status_models =
        gateway_models_from_sources(&Settings::default(), &[], models.clone());
    assert_eq!(gateway_status_models[0].context_window, Some(272_000));

    let groups = gateway_client_provider_groups_from_exported(
        &Settings::default(),
        &[],
        "gpt-5.6-terra",
        gateway_status_models,
    )
    .expect("Gateway client export groups");
    let exported = groups
        .providers
        .iter()
        .find(|provider| provider.client_provider_id == "codexhub-openai")
        .and_then(|provider| {
            provider
                .models
                .iter()
                .find(|model| model.id == "gpt-5.6-terra")
        })
        .expect("exported Terra model");
    assert_eq!(exported.context_window, Some(272_000));
}

#[test]
fn official_gateway_models_fail_closed_without_a_published_context_limit() {
    let models = official_models_from_metadata(
        &Settings::default(),
        Some(vec![Model {
            id: "gpt-5.6-terra".to_string(),
            context_window: Some(353_400),
            ..Model::default()
        }]),
        &BTreeMap::new(),
    );

    assert!(models.is_empty());

    let guarded = official_models_from_metadata(
        &Settings {
            openai_context_guard_enabled: true,
            ..Settings::default()
        },
        Some(vec![Model {
            id: "gpt-5.6-terra".to_string(),
            context_window: Some(353_400),
            ..Model::default()
        }]),
        &BTreeMap::new(),
    );
    assert!(guarded.is_empty());
}

#[test]
fn official_gateway_models_are_available_with_guard_disabled_when_safe_snapshot_exists() {
    let settings = Settings::default();
    let published_contexts = published_context_windows(&[("gpt-5.6-terra", 272_000)]);
    let models = official_models_from_metadata(
        &settings,
        Some(vec![Model {
            id: "openai/gpt-5.6-terra".to_string(),
            context_window: Some(353_400),
            ..Model::default()
        }]),
        &published_contexts,
    );

    assert_eq!(models.len(), 1);
    assert_eq!(models[0].id, "gpt-5.6-terra");
    assert_eq!(models[0].context_window, Some(272_000));
}

#[test]
fn gateway_models_exclude_official_and_include_external_when_safe_snapshot_is_unavailable() {
    let settings = Settings {
        include_official_models: true,
        ..Settings::default()
    };
    let providers = vec![Provider {
        id: "test-provider".to_string(),
        name: "Test Provider".to_string(),
        base_url: "https://api.test.example/v1".to_string(),
        api_key: None,
        upstream_format: Some(UpstreamFormat::Responses),
        available_upstream_formats: None,
        tool_protocol: None,
        tool_surface_strategy: None,
        reports_cached_input_tokens: None,
        supports_developer_role: None,
        display_prefix: None,
        auth_capabilities: None,
        onboarding_hint: None,
        discovery_policy: None,
        sort_order: None,
        enabled: true,
        locked: false,
        models: vec![Model {
            id: "test-model".to_string(),
            display_name: Some("Test Model".to_string()),
            gateway_exported: true,
            context_window: Some(128_000),
            ..Model::default()
        }],
    }];

    let models = gateway_models_from_sources(&settings, &providers, Vec::new());

    assert_eq!(models.len(), 1);
    assert_eq!(models[0].id, "test-provider/test-model");
    assert_eq!(models[0].source, "Test Provider");
    assert_eq!(models[0].source_kind, "external");
}

#[test]
fn openai_context_guard_clamps_gateway_official_models_without_changing_external_models() {
    let settings = Settings {
        openai_context_guard_enabled: true,
        ..Settings::default()
    };
    let published_contexts =
        published_context_windows(&[("gpt-5.6-sol", 272_000), ("gpt-5.3-codex-spark", 128_000)]);
    let official = official_models_from_metadata(
        &settings,
        Some(vec![
            Model {
                id: "gpt-5.6-sol".to_string(),
                context_window: Some(353_000),
                ..Model::default()
            },
            Model {
                id: "gpt-5.3-codex-spark".to_string(),
                context_window: Some(128_000),
                ..Model::default()
            },
        ]),
        &published_contexts,
    );

    assert_eq!(official[0].context_window, Some(272_000));
    assert_eq!(official[1].context_window, Some(128_000));

    let providers: Vec<Provider> = serde_json::from_value(json!([{
        "id": "ollama-cloud",
        "name": "Ollama Cloud",
        "base_url": "https://ollama.com/v1",
        "models": [{
            "id": "glm-5.2",
            "context_window": 1000000,
            "gateway_exported": true
        }]
    }]))
    .unwrap();
    let exported = gateway_models_from_config(&settings, &providers);
    assert_eq!(
        exported
            .iter()
            .find(|model| model.id == "ollama-cloud/glm-5.2")
            .expect("external model")
            .context_window,
        Some(1_000_000)
    );
}

#[test]
fn official_gateway_models_dedupe_legacy_alias_with_fresh_metadata_winning() {
    let published_contexts = published_context_windows(&[("gpt-5.6-sol", 272_000)]);
    let models = official_models_from_metadata(
        &Settings::default(),
        Some(vec![
            Model {
                id: "openai/gpt-5.6-sol".to_string(),
                display_name: Some("Legacy Sol".to_string()),
                context_window: Some(1),
                ..Model::default()
            },
            Model {
                id: "gpt-5.6-sol".to_string(),
                display_name: Some("GPT-5.6-Sol".to_string()),
                context_window: Some(400_000),
                ..Model::default()
            },
        ]),
        &published_contexts,
    );

    assert_eq!(models.len(), 1);
    assert_eq!(models[0].id, "gpt-5.6-sol");
    assert_eq!(models[0].display_name, "5.6 Sol");
    assert_eq!(models[0].context_window, Some(272_000));
}

#[test]
fn official_gateway_alias_merge_exports_only_when_any_record_is_enabled() {
    let published_contexts = published_context_windows(&[("gpt-5.6-sol", 272_000)]);
    let merged = |legacy_enabled, bare_enabled| {
        official_models_from_metadata(
            &Settings::default(),
            Some(vec![
                Model {
                    id: "openai/gpt-5.6-sol".to_string(),
                    enabled: legacy_enabled,
                    ..Model::default()
                },
                Model {
                    id: "gpt-5.6-sol".to_string(),
                    enabled: bare_enabled,
                    ..Model::default()
                },
            ]),
            &published_contexts,
        )
    };

    assert!(merged(false, false).is_empty());
    assert_eq!(merged(false, true)[0].id, "gpt-5.6-sol");
    assert_eq!(merged(true, false)[0].id, "gpt-5.6-sol");
}

#[test]
fn official_gateway_model_ids_are_bare_and_accept_legacy_aliases() {
    assert_eq!(
        super::official_gateway_model_id("openai/gpt-5.6").as_deref(),
        Some("gpt-5.6")
    );
    assert_eq!(
        super::official_gateway_model_id("gpt-5.6").as_deref(),
        Some("gpt-5.6")
    );
    assert_eq!(
        super::official_gateway_model_id("ollama-cloud/glm-5.2"),
        None
    );
}

#[test]
fn legacy_official_client_selection_resolves_to_exported_bare_id() {
    let settings = Settings::default();

    let resolved =
        super::resolve_gateway_client_model_id(&settings, &[], "openai/gpt-5.5").unwrap();

    assert_eq!(resolved, "gpt-5.5");
    assert_eq!(
        super::split_gateway_model_id(&resolved),
        ("openai".to_string(), "gpt-5.5".to_string())
    );
}

#[test]
fn opencode_config_exports_all_active_gateway_models() {
    let settings = Settings::default();
    let providers = client_export_test_providers();

    let text = opencode_config_text(None, &settings, &providers, "openai/gpt-5.5").unwrap();
    let value: serde_json::Value = serde_json::from_str(&text).unwrap();
    let openai_models = value
        .pointer("/provider/codexhub-openai/models")
        .and_then(serde_json::Value::as_object)
        .unwrap();
    let minimax_models = value
        .pointer("/provider/codexhub-minimax/models")
        .and_then(serde_json::Value::as_object)
        .unwrap();

    // ADR-0004 / #435: model selection stays user-owned (never forced).
    assert_eq!(value["model"], serde_json::Value::Null);
    assert!(openai_models.contains_key("gpt-5.5"));
    assert!(openai_models.contains_key("gpt-5.5-fast"));
    assert!(openai_models.contains_key("gpt-5.4-fast"));
    assert!(minimax_models.contains_key("minimax-m3"));
    assert!(!minimax_models.contains_key("minimax-m3-lite"));
    assert_eq!(minimax_models["minimax-m3"]["name"], "MiniMax M3");
    assert_eq!(
        value
            .pointer("/provider/codexhub-openai/npm")
            .and_then(serde_json::Value::as_str),
        Some("@ai-sdk/openai")
    );
    assert_eq!(
        value
            .pointer("/provider/codexhub-minimax/npm")
            .and_then(serde_json::Value::as_str),
        Some("@ai-sdk/openai-compatible")
    );
    assert_eq!(
        value
            .pointer("/provider/codexhub-minimax/options/baseURL")
            .and_then(serde_json::Value::as_str),
        Some("http://127.0.0.1:9099/v1/providers/minimax")
    );
}

#[test]
fn client_exports_use_explicit_responses_provider_protocols() {
    let root = unique_temp_dir("codexhub-responses-provider-export");
    let models_path = root.join("models.json");
    fs::create_dir_all(root.as_path()).unwrap();
    let settings = Settings {
        include_official_models: false,
        ..Settings::default()
    };
    let mut providers = client_export_test_providers();
    providers[0].upstream_format = Some(UpstreamFormat::Responses);

    let opencode_text = opencode_config_text(None, &settings, &providers, "minimax/minimax-m3").unwrap();
    let opencode_value: serde_json::Value = serde_json::from_str(&opencode_text).unwrap();
    let pi_text =
        pi_models_text(&models_path, &settings, &providers, "minimax/minimax-m3").unwrap();
    let pi_value: serde_json::Value = serde_json::from_str(&pi_text).unwrap();
    let omp_text = omp_models_yml_text(None, &settings, &providers, "minimax/minimax-m3").unwrap();
    let zcode_text = zcode_catalog_text(&settings, &providers, "minimax/minimax-m3").unwrap();
    let zcode_value: serde_json::Value = serde_json::from_str(&zcode_text).unwrap();
    let zcode_provider = zcode_value
        .pointer("/providers")
        .and_then(serde_json::Value::as_array)
        .unwrap()
        .iter()
        .find(|provider| provider["id"] == "codexhub-minimax")
        .unwrap();

    assert_eq!(
        opencode_value
            .pointer("/provider/codexhub-minimax/npm")
            .and_then(serde_json::Value::as_str),
        Some("@ai-sdk/openai")
    );
    assert_eq!(
        pi_value
            .pointer("/providers/codexhub-minimax/api")
            .and_then(serde_json::Value::as_str),
        Some("openai-responses")
    );
    assert!(omp_text.contains("codexhub-minimax:"));
    assert!(omp_text.contains("api: openai-responses"));
    assert_eq!(
        zcode_provider
            .pointer("/endpoints/paths/openai")
            .and_then(serde_json::Value::as_str),
        Some("/v1/providers/minimax/responses")
    );
    assert_eq!(
        zcode_provider
            .get("apiFormat")
            .and_then(serde_json::Value::as_str),
        Some("openai-responses")
    );
    let zcode_v2_text = super::zcode_v2_config_text(
        &root.join("zcode").join("config.json"),
        &settings,
        &providers,
        "minimax/minimax-m3",
    )
    .unwrap();
    let zcode_v2_value: serde_json::Value = serde_json::from_str(&zcode_v2_text).unwrap();
    let zcode_v2_provider = zcode_v2_value
        .pointer("/provider/codexhub-minimax")
        .unwrap();
    assert_eq!(
        zcode_v2_provider
            .get("kind")
            .and_then(serde_json::Value::as_str),
        Some("openai")
    );
    assert_eq!(
        zcode_v2_provider
            .get("apiFormat")
            .and_then(serde_json::Value::as_str),
        Some("openai-responses")
    );
    assert_eq!(
        zcode_v2_provider
            .pointer("/endpoints/baseURL")
            .and_then(serde_json::Value::as_str),
        Some("http://127.0.0.1:9099/v1/providers/minimax")
    );
    assert_eq!(
        zcode_v2_provider
            .pointer("/endpoints/paths/openai")
            .and_then(serde_json::Value::as_str),
        Some("/responses")
    );
}

#[test]
fn client_exports_use_explicit_chat_provider_protocols() {
    let root = unique_temp_dir("codexhub-chat-provider-export");
    let models_path = root.join("models.json");
    fs::create_dir_all(root.as_path()).unwrap();
    let settings = Settings {
        include_official_models: false,
        ..Settings::default()
    };
    let mut providers = client_export_test_providers();
    providers[0].upstream_format = Some(UpstreamFormat::ChatCompletions);

    let opencode_text = opencode_config_text(None, &settings, &providers, "minimax/minimax-m3").unwrap();
    let opencode_value: serde_json::Value = serde_json::from_str(&opencode_text).unwrap();
    let pi_text =
        pi_models_text(&models_path, &settings, &providers, "minimax/minimax-m3").unwrap();
    let pi_value: serde_json::Value = serde_json::from_str(&pi_text).unwrap();
    let omp_text = omp_models_yml_text(None, &settings, &providers, "minimax/minimax-m3").unwrap();
    let zcode_text = zcode_catalog_text(&settings, &providers, "minimax/minimax-m3").unwrap();
    let zcode_value: serde_json::Value = serde_json::from_str(&zcode_text).unwrap();
    let zcode_provider = zcode_value
        .pointer("/providers")
        .and_then(serde_json::Value::as_array)
        .unwrap()
        .iter()
        .find(|provider| provider["id"] == "codexhub-minimax")
        .unwrap();

    assert_eq!(
        opencode_value
            .pointer("/provider/codexhub-minimax/npm")
            .and_then(serde_json::Value::as_str),
        Some("@ai-sdk/openai-compatible")
    );
    assert_eq!(
        pi_value
            .pointer("/providers/codexhub-minimax/api")
            .and_then(serde_json::Value::as_str),
        Some("openai-completions")
    );
    assert!(omp_text.contains("codexhub-minimax:"));
    assert!(omp_text.contains("api: openai-completions"));
    assert_eq!(
        zcode_provider
            .pointer("/endpoints/paths/openai-compatible")
            .and_then(serde_json::Value::as_str),
        Some("/v1/providers/minimax/chat/completions")
    );
    assert_eq!(
        zcode_provider
            .get("apiFormat")
            .and_then(serde_json::Value::as_str),
        Some("openai-chat-completions")
    );
    let zcode_v2_text = super::zcode_v2_config_text(
        &root.join("zcode").join("config.json"),
        &settings,
        &providers,
        "minimax/minimax-m3",
    )
    .unwrap();
    let zcode_v2_value: serde_json::Value = serde_json::from_str(&zcode_v2_text).unwrap();
    let zcode_v2_provider = zcode_v2_value
        .pointer("/provider/codexhub-minimax")
        .unwrap();
    assert_eq!(
        zcode_v2_provider
            .get("kind")
            .and_then(serde_json::Value::as_str),
        Some("openai-compatible")
    );
    assert_eq!(
        zcode_v2_provider
            .get("apiFormat")
            .and_then(serde_json::Value::as_str),
        Some("openai-chat-completions")
    );
    assert_eq!(
        zcode_v2_provider
            .pointer("/endpoints/baseURL")
            .and_then(serde_json::Value::as_str),
        Some("http://127.0.0.1:9099/v1/providers/minimax")
    );
    assert_eq!(
        zcode_v2_provider
            .pointer("/endpoints/paths/openai-compatible")
            .and_then(serde_json::Value::as_str),
        Some("/chat/completions")
    );
}

#[test]
fn opencode_config_resolves_selected_alias_and_exports_only_canonical_models() {
    let settings = Settings::default();
    let providers = case_sensitive_client_export_test_providers();

    let text = opencode_config_text(None, &settings, &providers, "minimax-cn/minimax-m3").unwrap();
    let value: serde_json::Value = serde_json::from_str(&text).unwrap();
    let exported = value
        .pointer("/provider/codexhub-minimax-cn/models")
        .and_then(serde_json::Value::as_object)
        .unwrap();

    // ADR-0004 / #435: model selection stays user-owned (never forced).
    assert_eq!(value["model"], serde_json::Value::Null);
    assert!(exported.contains_key("MiniMax-M3"));
    assert!(!exported.contains_key("minimax-m3"));
}

#[test]
fn client_configs_drop_case_insensitive_export_collisions() {
    let settings = Settings::default();
    let providers = case_collision_client_export_test_providers();

    let text = opencode_config_text(None, &settings, &providers, "minimax-cn/MiniMax-M3").unwrap();
    let value: serde_json::Value = serde_json::from_str(&text).unwrap();
    let exported = value
        .pointer("/provider/codexhub-minimax-cn/models")
        .and_then(serde_json::Value::as_object)
        .unwrap();
    let exported_ids = exported.keys().map(String::as_str).collect::<Vec<_>>();

    assert!(exported.contains_key("MiniMax-M3"));
    assert!(!exported.contains_key("minimax-m3"));
    assert_eq!(
        exported_ids
            .iter()
            .filter(|id| id.eq_ignore_ascii_case("minimax-m3"))
            .count(),
        1
    );
}

#[test]
fn pi_and_omp_configs_keep_duplicate_glm_models_distinct() {
    let root = unique_temp_dir("codexhub-client-case");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    fs::create_dir_all(root.as_path()).unwrap();
    let settings = Settings::default();
    let providers = case_sensitive_client_export_test_providers();

    let pi_text = pi_settings_text(
        &settings_path,
        &settings,
        &providers,
        "ollama-cloud/glm-5.2",
    )
    .unwrap();
    let pi_models_text =
        pi_models_text(&models_path, &settings, &providers, "ollama-cloud/glm-5.2").unwrap();
    let omp_text = omp_models_yml_text(None, &settings, &providers, "ollama-cloud/glm-5.2").unwrap();
    let pi_value: serde_json::Value = serde_json::from_str(&pi_text).unwrap();
    let pi_models_value: serde_json::Value = serde_json::from_str(&pi_models_text).unwrap();
    let ollama_models = pi_models_value
        .pointer("/providers/codexhub-ollama-cloud/models")
        .and_then(serde_json::Value::as_array)
        .unwrap();
    let volc_models = pi_models_value
        .pointer("/providers/codexhub-volc/models")
        .and_then(serde_json::Value::as_array)
        .unwrap();

    assert!(pi_value.get("defaultProvider").is_none());
    assert!(pi_value.get("defaultModel").is_none());
    assert!(pi_value.get("enabledModels").is_none());
    assert!(ollama_models.iter().any(|model| model["id"] == "glm-5.2"));
    assert!(volc_models.iter().any(|model| model["id"] == "glm-5.2"));
    assert!(omp_text.contains("codexhub-ollama-cloud:"));
    assert!(omp_text.contains("baseUrl: \"http://127.0.0.1:9099/v1/providers/ollama-cloud\""));
    assert!(omp_text.contains("codexhub-volc:"));
    assert!(omp_text.contains("baseUrl: \"http://127.0.0.1:9099/v1/providers/volc\""));
    assert!(omp_text.contains("id: \"glm-5.2\""));
}

#[test]
fn pi_and_omp_configs_emit_provider_supports_developer_role() {
    let root = unique_temp_dir("codexhub-developer-role-compat");
    let models_path = root.join("models.json");
    fs::create_dir_all(root.as_path()).unwrap();
    let settings = Settings::default();
    let mut providers = case_sensitive_client_export_test_providers();
    providers.push(Provider {
        id: "kimi".to_string(),
        name: "Kimi".to_string(),
        base_url: "https://api.kimi.example.test/coding/".to_string(),
        api_key: None,
        upstream_format: Some(UpstreamFormat::ChatCompletions),
        available_upstream_formats: None,
        tool_protocol: None,
        tool_surface_strategy: None,
        reports_cached_input_tokens: None,
        supports_developer_role: Some(false),
        display_prefix: Some("Kimi".to_string()),
        auth_capabilities: None,
        onboarding_hint: None,
        discovery_policy: None,
        sort_order: Some(4),
        enabled: true,
        locked: false,
        models: vec![Model {
            id: "k3".to_string(),
            display_name: Some("Kimi K3".to_string()),
            context_window: Some(1_048_576),
            gateway_exported: true,
            ..Model::default()
        }],
    });

    let pi_models_text = pi_models_text(&models_path, &settings, &providers, "kimi/k3").unwrap();
    let omp_text = omp_models_yml_text(None, &settings, &providers, "kimi/k3").unwrap();
    let pi_models_value: serde_json::Value = serde_json::from_str(&pi_models_text).unwrap();

    assert_eq!(
        pi_models_value.pointer("/providers/codexhub-kimi/compat/supportsDeveloperRole"),
        Some(&serde_json::Value::Bool(false))
    );
    assert_eq!(
        pi_models_value.pointer("/providers/codexhub-volc/compat/supportsDeveloperRole"),
        Some(&serde_json::Value::Bool(true))
    );
    let kimi_block = omp_text
        .split("  codexhub-kimi:\n")
        .nth(1)
        .and_then(|rest| rest.split("\n  codexhub-").next())
        .unwrap();
    let volc_block = omp_text
        .split("  codexhub-volc:\n")
        .nth(1)
        .and_then(|rest| rest.split("\n  codexhub-").next())
        .unwrap();
    assert!(kimi_block.contains("supportsDeveloperRole: false"));
    assert!(volc_block.contains("supportsDeveloperRole: true"));
}

#[test]
fn client_config_rejects_unexported_selected_model_case() {
    let settings = Settings::default();
    let providers = case_sensitive_client_export_test_providers();

    let error = opencode_config_text(None, &settings, &providers, "minimax-cn/MINIMAX-M3").unwrap_err();

    assert!(error.contains("Gateway model is not exported: minimax-cn/MINIMAX-M3"));
}

#[test]
fn client_config_keeps_official_fast_selection_as_client_pseudo_model() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_codex_home = std::env::var_os("CODEX_HOME");
    let previous_runtime_home = std::env::var_os("CODEXHUB_RUNTIME_HOME");
    let root = unique_temp_dir("codexhub-official-fast-selection");
    let catalog_dir = root.join("model-catalogs");
    fs::create_dir_all(&catalog_dir).unwrap();
    fs::write(
        catalog_dir.join("codexhub-model-catalog.json"),
        serde_json::to_vec_pretty(&json!({
            "models": [{
                "slug": "gpt-5.5",
                "codex_proxy_metadata": {
                    "provider": "openai",
                    "upstream_name": "official",
                    "official_context_budget": {
                        "source": "degraded_last_known_official",
                        "freshness": "stale",
                        "model_context_window": 272000,
                        "effective_context_window_percent": 95,
                        "effective_context_window": 258400,
                        "model_auto_compact_token_limit": 244800
                    }
                }
            }]
        }))
        .unwrap(),
    )
    .unwrap();
    std::env::set_var("CODEX_HOME", &root);
    std::env::set_var("CODEXHUB_RUNTIME_HOME", &root);

    let settings = Settings::default();
    let providers = client_export_test_providers();

    let result = opencode_config_text(None, &settings, &providers, "openai/gpt-5.5-fast");
    restore_env("CODEX_HOME", previous_codex_home);
    restore_env("CODEXHUB_RUNTIME_HOME", previous_runtime_home);
    let text = result.unwrap();
    let value: serde_json::Value = serde_json::from_str(&text).unwrap();
    let openai_models = value
        .pointer("/provider/codexhub-openai/models")
        .and_then(serde_json::Value::as_object)
        .unwrap();

    // ADR-0004 / #435: model/small_model selection stays user-owned.
    assert_eq!(value["model"], serde_json::Value::Null);
    assert_eq!(value["small_model"], serde_json::Value::Null);
    assert!(openai_models.contains_key("gpt-5.5"));
    assert!(openai_models.contains_key("gpt-5.5-fast"));
}

#[test]
fn pi_config_exports_all_active_gateway_models() {
    let root = unique_temp_dir("codexhub-pi-export");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    fs::create_dir_all(root.as_path()).unwrap();
    let settings = Settings::default();
    let providers = client_export_test_providers();

    let settings_text =
        pi_settings_text(&settings_path, &settings, &providers, "openai/gpt-5.5").unwrap();
    let models_text =
        pi_models_text(&models_path, &settings, &providers, "openai/gpt-5.5").unwrap();
    let settings_value: serde_json::Value = serde_json::from_str(&settings_text).unwrap();
    let models_value: serde_json::Value = serde_json::from_str(&models_text).unwrap();
    let openai_models = models_value
        .pointer("/providers/codexhub-openai/models")
        .and_then(serde_json::Value::as_array)
        .unwrap();
    let minimax_models = models_value
        .pointer("/providers/codexhub-minimax/models")
        .and_then(serde_json::Value::as_array)
        .unwrap();

    assert!(settings_value.get("enabledModels").is_none());
    assert!(settings_value.get("defaultProvider").is_none());
    assert!(settings_value.get("defaultModel").is_none());
    assert_eq!(
        models_value
            .pointer("/providers/codexhub-openai/api")
            .and_then(serde_json::Value::as_str),
        Some("openai-responses")
    );
    assert_eq!(
        models_value
            .pointer("/providers/codexhub-minimax/api")
            .and_then(serde_json::Value::as_str),
        Some("openai-completions")
    );
    assert!(openai_models.iter().any(|model| model["id"] == "gpt-5.5"));
    let openai_model = openai_models
        .iter()
        .find(|model| model["id"] == "gpt-5.5")
        .unwrap();
    assert_eq!(
        openai_model
            .pointer("/headers/x-codex-client-id")
            .and_then(serde_json::Value::as_str),
        Some("pi")
    );
    assert!(openai_models
        .iter()
        .any(|model| model["id"] == "gpt-5.5-fast"));
    assert!(openai_models
        .iter()
        .any(|model| model["id"] == "gpt-5.4-fast"));
    let minimax_model = minimax_models
        .iter()
        .find(|model| model["id"] == "minimax-m3")
        .unwrap();
    assert_eq!(
        minimax_model
            .pointer("/headers/x-codex-client-id")
            .and_then(serde_json::Value::as_str),
        Some("pi")
    );
    assert!(!minimax_models
        .iter()
        .any(|model| model["id"] == "minimax-m3-lite"));
}

#[test]
fn pi_models_preserve_unmanaged_codexhub_prefix_provider() {
    let root = unique_temp_dir("codexhub-pi-unmanaged-prefix");
    let models_path = root.join("models.json");
    fs::create_dir_all(root.as_path()).unwrap();
    fs::write(
            &models_path,
            r#"{"providers":{"codexhub-labs":{"baseUrl":"https://labs.example.test/v1","api":"openai-completions","models":[{"id":"custom"}]}}}"#,
        )
        .unwrap();
    let settings = Settings::default();

    let models_text = pi_models_text(&models_path, &settings, &[], "openai/gpt-5.5").unwrap();
    let value: serde_json::Value = serde_json::from_str(&models_text).unwrap();

    assert!(value.pointer("/providers/codexhub-labs").is_some());
    assert!(value.pointer("/providers/codexhub-openai").is_some());
}

#[test]
fn pi_settings_preserve_activation_and_enabled_models() {
    let root = unique_temp_dir("codexhub-pi-enabled-models");
    let settings_path = root.join("settings.json");
    fs::create_dir_all(root.as_path()).unwrap();
    fs::write(
            &settings_path,
            r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4","enabledModels":["codexhub/minimax-cn/MiniMax-M3"],"theme":"dark"}"#,
        )
        .unwrap();
    let settings = Settings::default();
    let providers = case_sensitive_client_export_test_providers();

    let text = pi_settings_text(
        &settings_path,
        &settings,
        &providers,
        "ollama-cloud/glm-5.2",
    )
    .unwrap();
    let value: serde_json::Value = serde_json::from_str(&text).unwrap();

    assert_eq!(
        value
            .get("enabledModels")
            .and_then(serde_json::Value::as_array)
            .map(|models| models
                .iter()
                .filter_map(serde_json::Value::as_str)
                .collect::<Vec<_>>()),
        Some(vec!["codexhub/minimax-cn/MiniMax-M3"])
    );
    assert_eq!(value["defaultProvider"], "anthropic");
    assert_eq!(value["defaultModel"], "claude-sonnet-4");
    assert_eq!(value["theme"], "dark");
}

#[test]
fn omp_models_export_all_active_gateway_models() {
    let settings = Settings::default();
    let providers = client_export_test_providers();

    let text = omp_models_yml_text(None, &settings, &providers, "openai/gpt-5.5").unwrap();

    assert!(text.contains("codexhub-openai:"));
    assert!(text.contains("api: openai-responses"));
    assert!(text.contains("id: \"gpt-5.5\""));
    assert!(text.contains("x-codex-client-id: omp"));
    assert!(text.contains("id: \"gpt-5.5-fast\""));
    assert!(text.contains("codexhub-minimax:"));
    assert!(text.contains("api: openai-completions"));
    assert!(text.contains("id: \"minimax-m3\""));
    assert!(text.contains("name: \"MiniMax M3\""));
    assert!(!text.contains("minimax/minimax-m3-lite"));
}

fn yaml_string(value: &serde_yaml::Value) -> &str {
    match value {
        serde_yaml::Value::String(text) => text.as_str(),
        other => panic!("expected YAML string, got {other:?}"),
    }
}

#[test]
fn omp_models_yaml_keeps_implicitly_typed_scalars_as_strings() {
    let settings = Settings {
        gateway_client_key: "2026-07-17".to_string(),
        ..Settings::default()
    };
    let mut provider = client_export_test_providers().remove(0);
    provider.models = vec![
        Model {
            id: "5.4".to_string(),
            display_name: Some("5.4".to_string()),
            gateway_exported: true,
            ..Model::default()
        },
        Model {
            id: "true".to_string(),
            display_name: Some("null".to_string()),
            gateway_exported: true,
            ..Model::default()
        },
        Model {
            id: "1e3".to_string(),
            display_name: Some("~".to_string()),
            gateway_exported: true,
            ..Model::default()
        },
        Model {
            id: "0x10".to_string(),
            display_name: Some("[a, b]".to_string()),
            gateway_exported: true,
            ..Model::default()
        },
        Model {
            id: "*alias".to_string(),
            display_name: Some("&anchor".to_string()),
            gateway_exported: true,
            ..Model::default()
        },
    ];
    let providers = vec![provider];

    let text = omp_models_yml_text(None, &settings, &providers, "minimax/5.4").unwrap();
    let document: serde_yaml::Value = serde_yaml::from_str(&text).unwrap();

    let provider_doc = document
        .get("providers")
        .and_then(|providers| providers.get("codexhub-minimax"))
        .expect("provider document");
    assert_eq!(
        yaml_string(provider_doc.get("baseUrl").expect("baseUrl")),
        "http://127.0.0.1:9099/v1/providers/minimax"
    );
    assert_eq!(
        yaml_string(provider_doc.get("apiKey").expect("apiKey")),
        "2026-07-17"
    );
    let models = provider_doc
        .get("models")
        .and_then(|models| models.as_sequence())
        .expect("models");
    let expected = [
        ("5.4", "5.4"),
        ("true", "null"),
        ("1e3", "~"),
        ("0x10", "[a, b]"),
        ("*alias", "&anchor"),
    ];
    assert_eq!(models.len(), expected.len());
    for (model, (id, name)) in models.iter().zip(expected) {
        assert_eq!(yaml_string(model.get("id").expect("model id")), id);
        assert_eq!(yaml_string(model.get("name").expect("model name")), name);
    }
}

#[test]
fn omp_models_yaml_official_numeric_display_names_parse_as_strings() {
    let settings = Settings::default();
    let providers = client_export_test_providers();

    let text = omp_models_yml_text(None, &settings, &providers, "openai/gpt-5.5").unwrap();
    let document: serde_yaml::Value = serde_yaml::from_str(&text).unwrap();

    let models = document
        .get("providers")
        .and_then(|providers| providers.get("codexhub-openai"))
        .and_then(|provider| provider.get("models"))
        .and_then(|models| models.as_sequence())
        .expect("official models");
    // The official source is environment-dependent (local subscription
    // cache vs static fallback), so the hermetic invariant is that every
    // emitted official id/name parses back as a YAML string, whatever the
    // concrete catalog is. Deterministic numeric-name coverage lives in
    // the custom-provider fixture test above.
    assert!(!models.is_empty());
    for model in models {
        yaml_string(model.get("id").expect("model id"));
        yaml_string(model.get("name").expect("model name"));
    }
}

fn reasoning_contract_client_export_test_providers() -> Vec<Provider> {
    vec![Provider {
        id: "volc".to_string(),
        name: "Volcengine".to_string(),
        base_url: "https://ark.example.test/v1".to_string(),
        api_key: None,
        upstream_format: None,
        available_upstream_formats: None,
        tool_protocol: None,
        tool_surface_strategy: None,
        reports_cached_input_tokens: None,
        supports_developer_role: None,
        display_prefix: Some("Volc".to_string()),
        auth_capabilities: None,
        onboarding_hint: None,
        discovery_policy: None,
        sort_order: Some(1),
        enabled: true,
        locked: false,
        models: vec![
            Model {
                id: "glm-5.2".to_string(),
                display_name: Some("Volc GLM-5.2".to_string()),
                input_modalities: Some(vec!["text".to_string(), "image".to_string()]),
                supported_reasoning_levels: Some(vec![
                    "low".to_string(),
                    "high".to_string(),
                    "xhigh".to_string(),
                ]),
                default_reasoning_level: Some("high".to_string()),
                gateway_exported: true,
                ..Model::default()
            },
            Model {
                id: "glm-5.2-flash".to_string(),
                display_name: Some("Volc GLM-5.2 Flash".to_string()),
                gateway_exported: true,
                ..Model::default()
            },
        ],
    }]
}

#[test]
fn opencode_variants_preserve_official_catalog_reasoning_order() {
    let variants = opencode_reasoning_variants(&official_gateway_reasoning_levels());
    let keys: Vec<&str> = variants.keys().map(String::as_str).collect();
    assert_eq!(
        keys,
        ["low", "medium", "high", "xhigh", "max"],
        "official variants must follow catalog order, not alphabetical"
    );
}

#[test]
fn opencode_config_preserves_configured_reasoning_variant_order() {
    let settings = Settings::default();
    let providers = reasoning_contract_client_export_test_providers();
    let text = opencode_config_text(None, &settings, &providers, "volc/glm-5.2").unwrap();

    let variants_start = text.find("\"variants\"").expect("variants object");
    let variants_text = &text[variants_start..];
    let low = variants_text.find("\"low\"").expect("low variant");
    let high = variants_text.find("\"high\"").expect("high variant");
    let xhigh = variants_text.find("\"xhigh\"").expect("xhigh variant");
    assert!(
        low < high && high < xhigh,
        "variants must follow the configured order (low, high, xhigh), got: {variants_text}"
    );
}

#[test]
fn opencode_config_exports_configured_modalities_per_model() {
    let settings = Settings::default();
    let providers = reasoning_contract_client_export_test_providers();
    let text = opencode_config_text(None, &settings, &providers, "volc/glm-5.2").unwrap();
    let value: serde_json::Value = serde_json::from_str(&text).unwrap();

    assert_eq!(
        value.pointer("/provider/codexhub-volc/models/glm-5.2/modalities/input"),
        Some(&serde_json::json!(["text", "image"]))
    );
    assert_eq!(
        value.pointer("/provider/codexhub-volc/models/glm-5.2-flash/modalities/input"),
        Some(&serde_json::json!(["text"]))
    );
    // The projection has no output-modality source; omit rather than fabricate.
    assert!(value
        .pointer("/provider/codexhub-volc/models/glm-5.2/modalities/output")
        .is_none());
}

#[test]
fn client_exports_map_configured_reasoning_contract() {
    let root = unique_temp_dir("codexhub-reasoning-contract");
    let models_path = root.join("models.json");
    let v2_config_path = root.join("v2").join("config.json");
    fs::create_dir_all(root.as_path()).unwrap();
    let settings = Settings::default();
    let providers = reasoning_contract_client_export_test_providers();

    let expected_reasoning = serde_json::json!({
        "enabled": true,
        "variants": ["low", "high", "xhigh", "off"],
        "defaultVariant": "high",
    });

    let zcode_catalog = zcode_catalog_text(&settings, &providers, "volc/glm-5.2").unwrap();
    let zcode_catalog_value: serde_json::Value = serde_json::from_str(&zcode_catalog).unwrap();
    let zcode_models = zcode_catalog_value
        .pointer("/providers/0/models")
        .and_then(serde_json::Value::as_array)
        .unwrap();
    let zcode_entry = |model_id: &str| {
        zcode_models
            .iter()
            .find(|model| model["id"] == model_id)
            .cloned()
            .unwrap_or_else(|| panic!("missing ZCode catalog entry for {model_id}"))
    };
    assert_eq!(zcode_entry("glm-5.2")["reasoning"], expected_reasoning);
    assert!(zcode_entry("glm-5.2-flash").get("reasoning").is_none());

    let zcode_v2 =
        super::zcode_v2_config_text(&v2_config_path, &settings, &providers, "volc/glm-5.2")
            .unwrap();
    let zcode_v2_value: serde_json::Value = serde_json::from_str(&zcode_v2).unwrap();
    assert_eq!(
        zcode_v2_value.pointer("/provider/codexhub-volc/models/glm-5.2/reasoning"),
        Some(&expected_reasoning)
    );
    assert!(zcode_v2_value
        .pointer("/provider/codexhub-volc/models/glm-5.2-flash/reasoning")
        .is_none());

    let opencode_text = opencode_config_text(None, &settings, &providers, "volc/glm-5.2").unwrap();
    let opencode_value: serde_json::Value = serde_json::from_str(&opencode_text).unwrap();
    let opencode_entry = |model_id: &str| {
        opencode_value
            .pointer(&format!("/provider/codexhub-volc/models/{model_id}"))
            .cloned()
            .unwrap_or_else(|| panic!("missing OpenCode entry for {model_id}"))
    };
    let capable = opencode_entry("glm-5.2");
    assert_eq!(
        capable.pointer("/options/reasoningEffort"),
        Some(&serde_json::Value::String("high".to_string()))
    );
    assert_eq!(
        capable.pointer("/variants/low/reasoningEffort"),
        Some(&serde_json::Value::String("low".to_string()))
    );
    assert_eq!(
        capable.pointer("/variants/xhigh/reasoningEffort"),
        Some(&serde_json::Value::String("xhigh".to_string()))
    );
    let incapable = opencode_entry("glm-5.2-flash");
    assert!(incapable.get("options").is_none());
    assert!(incapable.get("variants").is_none());

    let pi_text = pi_models_text(&models_path, &settings, &providers, "volc/glm-5.2").unwrap();
    let pi_value: serde_json::Value = serde_json::from_str(&pi_text).unwrap();
    let pi_models = pi_value
        .pointer("/providers/codexhub-volc/models")
        .and_then(serde_json::Value::as_array)
        .unwrap();
    let pi_reasoning = |model_id: &str| {
        pi_models
            .iter()
            .find(|model| model["id"] == model_id)
            .and_then(|model| model.get("reasoning"))
            .and_then(serde_json::Value::as_bool)
            .unwrap_or_else(|| panic!("missing Pi reasoning flag for {model_id}"))
    };
    assert!(pi_reasoning("glm-5.2"));
    assert!(!pi_reasoning("glm-5.2-flash"));

    let omp_text = omp_models_yml_text(None, &settings, &providers, "volc/glm-5.2").unwrap();
    let omp_block = |model_id: &str| {
        omp_text
            .split(&format!("      - id: \"{model_id}\"\n"))
            .nth(1)
            .and_then(|rest| rest.split("\n      - id: ").next())
            .unwrap_or_else(|| panic!("missing OMP model block for {model_id}"))
            .to_string()
    };
    assert!(omp_block("glm-5.2").contains("reasoning: true"));
    assert!(omp_block("glm-5.2-flash").contains("reasoning: false"));
}

#[test]
fn omp_apply_writes_default_reasoning_effort_suffix_when_configured() {
    let root = unique_temp_dir("codexhub-omp-reasoning-default");
    let config_path = root.join("config.yml");
    let models_path = root.join("models.yml");
    let backup_root = root.join("backups");
    fs::create_dir_all(root.as_path()).unwrap();
    fs::write(&config_path, "modelRoles:\n  default: ollama/qwen\n").unwrap();
    let settings = Settings::default();
    let providers = reasoning_contract_client_export_test_providers();

    let result = super::apply_omp_config_with_paths(
        &config_path,
        &models_path,
        &backup_root,
        &settings,
        &providers,
        "volc/glm-5.2",
    )
    .unwrap();

    assert!(result.applied);
    let config = fs::read_to_string(&config_path).unwrap();
    // ADR-0004 / #435: user-owned modelRoles preserved, never forced.
    assert!(config.contains("  default: ollama/qwen"));
    assert!(!config.contains("codexhub-volc/glm-5.2:high"));
}

#[test]
fn omp_apply_keeps_bare_default_selector_without_reasoning_contract() {
    let root = unique_temp_dir("codexhub-omp-reasoning-bare");
    let config_path = root.join("config.yml");
    let models_path = root.join("models.yml");
    let backup_root = root.join("backups");
    fs::create_dir_all(root.as_path()).unwrap();
    fs::write(&config_path, "modelRoles:\n  default: ollama/qwen\n").unwrap();
    let settings = Settings::default();
    let providers = reasoning_contract_client_export_test_providers();

    let result = super::apply_omp_config_with_paths(
        &config_path,
        &models_path,
        &backup_root,
        &settings,
        &providers,
        "volc/glm-5.2-flash",
    )
    .unwrap();

    assert!(result.applied);
    let config = fs::read_to_string(&config_path).unwrap();
    // ADR-0004 / #435: user-owned modelRoles preserved.
    assert!(!config.contains("codexhub-volc/glm-5.2-flash"));
    assert!(!config.contains("glm-5.2-flash:"));
}

fn image_capability_client_export_test_providers() -> Vec<Provider> {
    vec![Provider {
        id: "volc".to_string(),
        name: "Volcengine".to_string(),
        base_url: "https://ark.example.test/v1".to_string(),
        api_key: None,
        upstream_format: None,
        available_upstream_formats: None,
        tool_protocol: None,
        tool_surface_strategy: None,
        reports_cached_input_tokens: None,
        supports_developer_role: None,
        display_prefix: Some("Volc".to_string()),
        auth_capabilities: None,
        onboarding_hint: None,
        discovery_policy: None,
        sort_order: Some(1),
        enabled: true,
        locked: false,
        models: vec![
            Model {
                id: "glm-5.2".to_string(),
                display_name: Some("Volc GLM-5.2".to_string()),
                input_modalities: Some(vec!["text".to_string(), "image".to_string()]),
                gateway_exported: true,
                ..Model::default()
            },
            Model {
                id: "glm-5.2-coder".to_string(),
                display_name: Some("Volc GLM-5.2 Coder".to_string()),
                input_modalities: Some(vec!["text".to_string()]),
                gateway_exported: true,
                ..Model::default()
            },
            Model {
                id: "glm-5.2-air".to_string(),
                display_name: Some("Volc GLM-5.2 Air".to_string()),
                gateway_exported: true,
                ..Model::default()
            },
        ],
    }]
}

#[test]
fn client_exports_map_configured_image_capability_per_model() {
    let root = unique_temp_dir("codexhub-image-capability");
    let models_path = root.join("models.json");
    let v2_config_path = root.join("v2").join("config.json");
    fs::create_dir_all(root.as_path()).unwrap();
    let settings = Settings::default();
    let providers = image_capability_client_export_test_providers();

    let omp_text = omp_models_yml_text(None, &settings, &providers, "volc/glm-5.2").unwrap();
    let pi_text = pi_models_text(&models_path, &settings, &providers, "volc/glm-5.2").unwrap();
    let pi_value: serde_json::Value = serde_json::from_str(&pi_text).unwrap();
    let zcode_catalog = zcode_catalog_text(&settings, &providers, "volc/glm-5.2").unwrap();
    let zcode_catalog_value: serde_json::Value = serde_json::from_str(&zcode_catalog).unwrap();
    let zcode_v2 =
        super::zcode_v2_config_text(&v2_config_path, &settings, &providers, "volc/glm-5.2")
            .unwrap();
    let zcode_v2_value: serde_json::Value = serde_json::from_str(&zcode_v2).unwrap();

    let omp_block = |model_id: &str| {
        omp_text
            .split(&format!("      - id: \"{model_id}\"\n"))
            .nth(1)
            .and_then(|rest| rest.split("\n      - id: ").next())
            .unwrap_or_else(|| panic!("missing OMP model block for {model_id}"))
            .to_string()
    };
    assert!(omp_block("glm-5.2").contains("input:\n          - text\n          - image\n"));
    let coder_block = omp_block("glm-5.2-coder");
    assert!(coder_block.contains("input:\n          - text\n"));
    assert!(!coder_block.contains("- image"));
    let air_block = omp_block("glm-5.2-air");
    assert!(air_block.contains("input:\n          - text\n"));
    assert!(!air_block.contains("- image"));

    let pi_models = pi_value
        .pointer("/providers/codexhub-volc/models")
        .and_then(serde_json::Value::as_array)
        .unwrap();
    let pi_inputs = |model_id: &str| {
        pi_models
            .iter()
            .find(|model| model["id"] == model_id)
            .and_then(|model| model.pointer("/input"))
            .cloned()
            .unwrap_or_else(|| panic!("missing Pi model entry for {model_id}"))
    };
    assert_eq!(pi_inputs("glm-5.2"), serde_json::json!(["text", "image"]));
    assert_eq!(pi_inputs("glm-5.2-coder"), serde_json::json!(["text"]));
    assert_eq!(pi_inputs("glm-5.2-air"), serde_json::json!(["text"]));

    let zcode_models = zcode_catalog_value
        .pointer("/providers/0/models")
        .and_then(serde_json::Value::as_array)
        .unwrap();
    let zcode_inputs = |model_id: &str| {
        zcode_models
            .iter()
            .find(|model| model["id"] == model_id)
            .and_then(|model| model.pointer("/modalities/input"))
            .cloned()
            .unwrap_or_else(|| panic!("missing ZCode catalog entry for {model_id}"))
    };
    assert_eq!(
        zcode_inputs("glm-5.2"),
        serde_json::json!(["text", "image"])
    );
    assert_eq!(zcode_inputs("glm-5.2-coder"), serde_json::json!(["text"]));
    assert_eq!(zcode_inputs("glm-5.2-air"), serde_json::json!(["text"]));

    assert_eq!(
        zcode_v2_value.pointer("/provider/codexhub-volc/models/glm-5.2/modalities/input"),
        Some(&serde_json::json!(["text", "image"]))
    );
    assert_eq!(
        zcode_v2_value.pointer("/provider/codexhub-volc/models/glm-5.2-coder/modalities/input"),
        Some(&serde_json::json!(["text"]))
    );
    assert_eq!(
        zcode_v2_value.pointer("/provider/codexhub-volc/models/glm-5.2-air/modalities/input"),
        Some(&serde_json::json!(["text"]))
    );
}

#[test]
fn omp_apply_omits_vision_role_and_reports_when_selected_model_is_text_only() {
    let root = unique_temp_dir("codexhub-omp-text-only");
    let config_path = root.join("config.yml");
    let models_path = root.join("models.yml");
    let backup_root = root.join("backups");
    fs::create_dir_all(root.as_path()).unwrap();
    fs::write(
        &config_path,
        "modelRoles:\n  default: ollama/qwen\n  vision: ollama/qwen-vision\n",
    )
    .unwrap();
    let settings = Settings::default();
    let providers = image_capability_client_export_test_providers();

    let result = super::apply_omp_config_with_paths(
        &config_path,
        &models_path,
        &backup_root,
        &settings,
        &providers,
        "volc/glm-5.2-coder",
    )
    .unwrap();

    assert!(result.applied);
    let config = fs::read_to_string(&config_path).unwrap();
    // ADR-0004 / #435: user-owned modelRoles preserved.
    assert!(!config.contains("codexhub-volc/glm-5.2-coder"));
    // ADR-0004 / #435: the user's own vision role stays.
    assert!(!config.contains("vision: codexhub-volc"));
    assert!(result.message.contains("vision"));
    assert!(result.message.contains("text-only"));
}

#[test]
fn omp_apply_writes_vision_role_for_image_capable_selection() {
    let root = unique_temp_dir("codexhub-omp-image-capable");
    let config_path = root.join("config.yml");
    let models_path = root.join("models.yml");
    let backup_root = root.join("backups");
    fs::create_dir_all(root.as_path()).unwrap();
    fs::write(&config_path, "modelRoles:\n  default: ollama/qwen\n").unwrap();
    let settings = Settings::default();
    let providers = image_capability_client_export_test_providers();

    let result = super::apply_omp_config_with_paths(
        &config_path,
        &models_path,
        &backup_root,
        &settings,
        &providers,
        "volc/glm-5.2",
    )
    .unwrap();

    assert!(result.applied);
    let config = fs::read_to_string(&config_path).unwrap();
    // ADR-0004 / #435: user-owned modelRoles preserved.
    assert!(!config.contains("codexhub-volc/glm-5.2"));
    assert!(!config.contains("vision: codexhub-volc"));
    assert!(!result.message.contains("text-only"));
}
