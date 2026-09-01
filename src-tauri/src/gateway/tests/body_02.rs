#[test]
fn omp_models_merge_preserves_foreign_providers_through_apply() {
    // #435: surgical merge — user-owned providers survive apply.
    let root = unique_temp_dir("codexhub-omp-merge");
    let config_path = root.join("config.yml");
    let models_path = root.join("models.yml");
    fs::create_dir_all(root.as_path()).unwrap();
    fs::write(&config_path, "modelRoles:
  default: some-model
").unwrap();
    fs::write(
        &models_path,
        "providers:
  ollama:
    baseUrl: http://localhost:11434/v1
    api: openai-completions
    models:
      - id: qwen2.5-coder:7b
",
    )
    .unwrap();
    let settings = Settings::default();
    let providers = vec![];

    let result = super::apply_omp_config_with_paths(
        &config_path,
        &models_path,
        &root.join("backups"),
        &settings,
        &providers,
        "openai/gpt-5.5",
    )
    .unwrap();
    assert!(result.applied);
    let written = fs::read_to_string(&models_path).unwrap();
    // Foreign provider preserved, codexhub providers added.
    assert!(written.contains("ollama:"), "foreign provider preserved: {written}");
    assert!(written.contains("codexhub"), "codexhub provider present: {written}");
    // User-owned modelRoles untouched.
    let config = fs::read_to_string(&config_path).unwrap();
    assert!(config.contains("some-model"), "user modelRoles preserved: {config}");
}

#[test]
fn omp_models_omit_unknown_context_window_instead_of_inventing_a_default() {
    let settings = Settings {
        include_official_models: false,
        ..Settings::default()
    };
    let providers = vec![Provider {
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
        sort_order: None,
        enabled: true,
        locked: false,
        models: vec![Model {
            id: "nemotron-3-nano:30b".to_string(),
            gateway_exported: true,
            context_window: None,
            ..Model::default()
        }],
    }];

    let text =
        omp_models_yml_text(None, &settings, &providers, "ollama-cloud/nemotron-3-nano:30b").unwrap();

    assert!(text.contains("codexhub-ollama-cloud:"));
    assert!(text.contains("id: \"nemotron-3-nano:30b\""));
    assert!(!text.contains("contextWindow:"));
}

#[test]
fn zcode_catalog_exports_all_active_gateway_models() {
    let settings = Settings::default();
    let providers = client_export_test_providers();

    let text = zcode_catalog_text(&settings, &providers, "openai/gpt-5.5").unwrap();
    let value: serde_json::Value = serde_json::from_str(&text).unwrap();
    let providers = value
        .pointer("/providers")
        .and_then(serde_json::Value::as_array)
        .unwrap();
    let openai = providers
        .iter()
        .find(|provider| provider["id"] == "codexhub-openai")
        .unwrap();
    let minimax = providers
        .iter()
        .find(|provider| provider["id"] == "codexhub-minimax")
        .unwrap();
    let openai_models = openai["models"].as_array().unwrap();
    let minimax_models = minimax["models"].as_array().unwrap();

    assert!(openai_models.iter().any(|model| model["id"] == "gpt-5.5"));
    assert!(minimax_models
        .iter()
        .any(|model| model["id"] == "minimax-m3"));
    assert!(!minimax_models
        .iter()
        .any(|model| model["id"] == "minimax-m3-lite"));
    assert_eq!(
        openai.get("apiFormat").and_then(serde_json::Value::as_str),
        Some("openai-responses")
    );
    assert_eq!(
        openai
            .pointer("/endpoints/paths/openai")
            .and_then(serde_json::Value::as_str),
        Some("/v1/providers/openai/responses")
    );
    assert_eq!(
        minimax.get("apiFormat").and_then(serde_json::Value::as_str),
        Some("openai-chat-completions")
    );
    assert_eq!(
        minimax
            .pointer("/endpoints/paths/openai-compatible")
            .and_then(serde_json::Value::as_str),
        Some("/v1/providers/minimax/chat/completions")
    );
}

#[test]
fn zcode_export_prefers_responses_when_provider_advertises_both_formats() {
    let root = unique_temp_dir("codexhub-zcode-responses-preferred");
    let settings = Settings::default();
    let mut providers = case_sensitive_client_export_test_providers();
    let volc = providers
        .iter_mut()
        .find(|provider| provider.id == "volc")
        .unwrap();
    volc.upstream_format = None;
    volc.available_upstream_formats = Some(vec![
        UpstreamFormat::Responses,
        UpstreamFormat::ChatCompletions,
    ]);

    let catalog_text = zcode_catalog_text(&settings, &providers, "volc/glm-5.2").unwrap();
    let catalog: serde_json::Value = serde_json::from_str(&catalog_text).unwrap();
    let catalog_provider = catalog
        .pointer("/providers")
        .and_then(serde_json::Value::as_array)
        .unwrap()
        .iter()
        .find(|provider| provider["id"] == "codexhub-volc")
        .unwrap();
    assert_eq!(
        catalog_provider
            .get("apiFormat")
            .and_then(serde_json::Value::as_str),
        Some("openai-responses")
    );
    assert_eq!(
        catalog_provider
            .pointer("/endpoints/paths/openai")
            .and_then(serde_json::Value::as_str),
        Some("/v1/providers/volc/responses")
    );

    let v2_text = super::zcode_v2_config_text(
        &root.join("config.json"),
        &settings,
        &providers,
        "volc/glm-5.2",
    )
    .unwrap();
    let v2: serde_json::Value = serde_json::from_str(&v2_text).unwrap();
    let v2_provider = v2.pointer("/provider/codexhub-volc").unwrap();
    assert_eq!(
        v2_provider.get("kind").and_then(serde_json::Value::as_str),
        Some("openai")
    );
    assert_eq!(
        v2_provider
            .get("apiFormat")
            .and_then(serde_json::Value::as_str),
        Some("openai-responses")
    );
    assert_eq!(
        v2_provider
            .pointer("/endpoints/paths/openai")
            .and_then(serde_json::Value::as_str),
        Some("/responses")
    );
}

#[test]
fn usage_summary_counts_missing_usage_without_estimating_tokens() {
    let text = [
            r#"{"event":"request_complete","model":"openai/gpt-5.5","status":200,"duration_ms":120,"usage_source":"upstream","usage_input_tokens":10,"usage_output_tokens":4,"usage_cached_input_tokens":3}"#,
            r#"{"event":"request_complete","upstream":"ollama_cloud","model":"ollama-cloud/glm-5.2","reports_cached_input_tokens":false,"status":200,"duration_ms":80,"usage_source":"upstream","usage_input_tokens":100,"usage_output_tokens":2,"usage_cached_input_tokens":0}"#,
            r#"{"event":"request_complete","model":"ollama/glm-5.2","status":200,"duration_ms":80,"usage_source":"upstream","usage_input_tokens":5,"usage_output_tokens":2}"#,
            r#"{"event":"request_complete","model":"ollama/glm-5.2","status":200,"duration_ms":90,"usage_source":"missing","usage_missing_reason":"upstream_missing_usage"}"#,
            r#"{"event":"request_complete","method":"GET","model":null,"upstream":"local","route_reason":"local_responses_probe","status":204,"duration_ms":1}"#,
        ]
        .join("\n");

    let summary = read_usage_summary_from_text(&text);
    let events = read_usage_events_from_text(&text, usize::MAX);

    assert_eq!(summary.requests, 4);
    assert_eq!(summary.total_tokens, Some(123));
    assert_eq!(summary.cached_input_tokens, Some(3));
    assert_eq!(summary.cache_hit_rate, Some(30.0));
    assert_eq!(summary.missing_usage_requests, 1);
    assert_eq!(events.len(), 4);
}

#[test]
fn usage_summary_counts_cache_for_kimi_but_not_unflagged_external_providers() {
    let text = [
            r#"{"event":"request_complete","upstream":"kimi","model":"kimi/k3","status":200,"usage_source":"upstream_async","usage_input_tokens":100,"usage_cached_input_tokens":80,"usage_output_tokens":5}"#,
            r#"{"event":"request_complete","upstream":"external","model":"external/k3","status":200,"usage_source":"upstream_async","usage_input_tokens":100,"usage_cached_input_tokens":90,"usage_output_tokens":5}"#,
        ]
        .join("\n");

    let summary = read_usage_summary_from_text(&text);

    assert_eq!(summary.cached_input_tokens, Some(80));
    assert_eq!(summary.cache_hit_rate, Some(80.0));
}

#[test]
fn usage_summary_estimates_cost_from_priced_token_usage() {
    let text = [
            r#"{"event":"request_complete","model":"openai/example","status":200,"duration_ms":120,"usage_source":"upstream","usage_input_tokens":10,"usage_output_tokens":4,"usage_cached_input_tokens":3}"#,
            r#"{"event":"request_complete","model":"fallback","reports_cached_input_tokens":true,"status":200,"duration_ms":80,"usage_source":"upstream","usage_input_tokens":10,"usage_output_tokens":1,"usage_cached_input_tokens":5}"#,
            r#"{"event":"request_complete","model":"estimated-cache","reports_cached_input_tokens":false,"status":200,"duration_ms":70,"usage_source":"upstream","usage_input_tokens":10,"usage_output_tokens":1,"usage_cached_input_tokens":0}"#,
            r#"{"event":"request_complete","model":"missing-price","status":200,"duration_ms":70,"usage_source":"upstream","usage_input_tokens":9,"usage_output_tokens":1}"#,
            r#"{"event":"request_complete","model":"openai/example","status":200,"duration_ms":90,"usage_source":"missing","usage_missing_reason":"upstream_missing_usage"}"#,
        ]
        .join("\n");
    let pricing = HashMap::from([
        (
            "openai/example".to_string(),
            UsagePricing {
                input_per_million: 2.0,
                cached_input_per_million: Some(0.2),
                output_per_million: 8.0,
            },
        ),
        (
            "fallback".to_string(),
            UsagePricing {
                input_per_million: 1.0,
                cached_input_per_million: None,
                output_per_million: 3.0,
            },
        ),
        (
            "estimated-cache".to_string(),
            UsagePricing {
                input_per_million: 1.0,
                cached_input_per_million: Some(0.2),
                output_per_million: 3.0,
            },
        ),
    ]);

    let summary = read_usage_summary_from_text_with_pricing(&text, &pricing);

    let expected = ((7.0 * 2.0 + 3.0 * 0.2 + 4.0 * 8.0)
        + (10.0 * 1.0 + 1.0 * 3.0)
        + (6.0 * 1.0 + 4.0 * 0.2 + 1.0 * 3.0))
        / 1_000_000.0;
    let actual = summary
        .estimated_cost_usd
        .expect("priced requests should produce an estimate");
    assert!((actual - expected).abs() < f64::EPSILON);
    assert!(summary
        .cost_label
        .contains("1 requests used input pricing for cached tokens"));
    assert!(summary
        .cost_label
        .contains("1 requests estimated cached input at 40.0% average hit rate"));
    assert!(summary
        .cost_label
        .contains("1 requests missing model pricing"));
    assert!(summary
        .cost_label
        .contains("1 requests missing token usage"));
}

#[test]
fn usage_summary_reads_sqlite_requests_as_source_of_truth() {
    let root = unique_temp_dir("codexhub-usage-sqlite");
    fs::create_dir_all(&root).unwrap();
    let db_path = root.join("codex-proxy-telemetry.sqlite");
    let connection = rusqlite::Connection::open(&db_path).unwrap();
    connection
            .execute_batch(
                r#"
                CREATE TABLE gateway_requests (
                    request_id TEXT PRIMARY KEY,
                    completed_ts TEXT,
                    method TEXT,
                    path TEXT,
                    route_reason TEXT,
                    model TEXT,
                    model_requested TEXT,
                    model_canonical TEXT,
                    upstream TEXT,
                    provider_id TEXT,
                    reports_cached_input_tokens INTEGER,
                    status INTEGER,
                    duration_ms INTEGER,
                    usage_source TEXT,
                    usage_missing_reason TEXT,
                    usage_input_tokens INTEGER,
                    usage_cached_input_tokens INTEGER,
                    usage_output_tokens INTEGER,
                    usage_total_tokens INTEGER,
                    usage_reasoning_tokens INTEGER
                );
                INSERT INTO gateway_requests (
                    request_id, completed_ts, method, path, route_reason, model_canonical, upstream, provider_id,
                    reports_cached_input_tokens, status,
                    duration_ms, usage_source, usage_input_tokens, usage_cached_input_tokens,
                    usage_output_tokens, usage_total_tokens
                ) VALUES
                    ('req-a', '2026-07-03T01:00:00Z', 'POST', '/v1/responses', 'model', 'openai/example', 'official', 'official', 1, 200,
                     120, 'upstream', 10, 3, 4, 14),
                    ('req-b', '2026-07-03T01:00:01Z', 'POST', '/v1/chat/completions', 'model', 'fallback', 'external', 'external', 1, 200,
                     80, 'upstream', 10, 5, 1, 11),
                    ('req-missing', '2026-07-03T01:00:02Z', 'POST', '/v1/responses', 'model', 'openai/example', 'official', 'official', 1, 200,
                     90, 'missing', NULL, NULL, NULL, NULL),
                    ('req-failed', '2026-07-03T01:00:03Z', 'POST', '/v1/responses', 'model', 'openai/example', 'official', 'official', 1, 502,
                     40, 'missing', NULL, NULL, NULL, NULL),
                    ('req-control', '2026-07-03T01:00:04Z', 'GET', '/v1/models', 'official_control', NULL, 'official', 'official', 1, 200,
                     20, 'missing', NULL, NULL, NULL, NULL),
                    ('req-local', '2026-07-03T01:00:05Z', 'GET', '/v1/responses', 'local_responses_probe', NULL, 'local', 'local', 0, 204,
                     1, NULL, NULL, NULL, NULL, NULL);
                "#,
            )
            .unwrap();

    let pricing = HashMap::from([
        (
            "openai/example".to_string(),
            UsagePricing {
                input_per_million: 2.0,
                cached_input_per_million: Some(0.2),
                output_per_million: 8.0,
            },
        ),
        (
            "fallback".to_string(),
            UsagePricing {
                input_per_million: 1.0,
                cached_input_per_million: None,
                output_per_million: 3.0,
            },
        ),
    ]);

    let events = read_usage_events_from_sqlite_path(&db_path, usize::MAX).unwrap();
    let summary = read_usage_summary_from_sqlite_path_with_pricing(&db_path, &pricing).unwrap();

    assert_eq!(events.len(), 3);
    assert_eq!(events[0].request_id.as_deref(), Some("req-a"));
    assert_eq!(summary.requests, 3);
    assert_eq!(summary.successful_requests, 3);
    assert_eq!(summary.total_tokens, Some(25));
    assert_eq!(summary.cache_hit_rate, Some(40.0));
    assert_eq!(summary.missing_usage_requests, 1);
    assert!(summary.estimated_cost_usd.is_some());

    let window = super::UsageTimeWindow::new(
        Some("2026-07-03T01:00:01Z".to_string()),
        Some("2026-07-03T01:00:02Z".to_string()),
    );
    let windowed_events =
        super::read_usage_events_from_sqlite_path_with_window(&db_path, usize::MAX, &window)
            .unwrap();
    let windowed_summary = super::read_usage_summary_from_sqlite_path_with_pricing_and_window(
        &db_path, &pricing, &window,
    )
    .unwrap();

    assert_eq!(windowed_events.len(), 2);
    assert_eq!(windowed_events[0].request_id.as_deref(), Some("req-b"));
    assert_eq!(
        windowed_events[1].request_id.as_deref(),
        Some("req-missing")
    );
    assert_eq!(windowed_summary.requests, windowed_events.len() as u64);
    assert_eq!(windowed_summary.total_tokens, Some(11));
    assert_eq!(windowed_summary.cache_hit_rate, Some(50.0));
    assert_eq!(windowed_summary.missing_usage_requests, 1);
}

#[test]
fn usage_snapshot_caps_visible_events_without_truncating_summary() {
    let root = unique_temp_dir("codexhub-usage-snapshot-cap");
    fs::create_dir_all(&root).unwrap();
    let event_path = root.join("codex-proxy-events.jsonl");
    let db_path = root.join("codex-proxy-telemetry.sqlite");
    fs::write(&event_path, "").unwrap();
    let mut connection = rusqlite::Connection::open(&db_path).unwrap();
    super::initialize_telemetry_db(&connection).unwrap();
    let transaction = connection.transaction().unwrap();
    for index in 0..501 {
        transaction
            .execute(
                r#"
                INSERT INTO gateway_requests (
                    request_id, completed_ts, method, path, route_reason,
                    model_canonical, upstream, provider_id, status,
                    usage_source, usage_input_tokens, usage_output_tokens,
                    usage_total_tokens, created_at, updated_at
                ) VALUES (?1, ?2, 'POST', '/v1/responses', 'model',
                    'openai/gpt-5.5', 'official', 'official', 200,
                    'upstream', 1, 1, 2, 'test', 'test')
                "#,
                rusqlite::params![
                    format!("req-{index:03}"),
                    format!("2026-07-03T01:{:02}:{:02}Z", index / 60, index % 60),
                ],
            )
            .unwrap();
    }
    transaction.commit().unwrap();
    drop(connection);

    let snapshot = super::gateway_usage_snapshot_for_paths(
        &event_path,
        &db_path,
        None,
        None,
        None,
    )
    .unwrap();

    assert_eq!(snapshot.events.len(), 500);
    assert_eq!(snapshot.summary.requests, 501);
    assert_eq!(snapshot.summary.total_tokens, Some(1_002));
}

#[test]
fn usage_events_normalize_official_bare_model_names() {
    let root = unique_temp_dir("codexhub-usage-official-models");
    fs::create_dir_all(&root).unwrap();
    let db_path = root.join("codex-proxy-telemetry.sqlite");
    let connection = rusqlite::Connection::open(&db_path).unwrap();
    super::initialize_telemetry_db(&connection).unwrap();
    connection
            .execute(
                r#"
                INSERT INTO gateway_requests (
                    request_id, completed_ts, method, path, route_reason, model_canonical,
                    upstream, provider_id, status, usage_source, usage_input_tokens, created_at, updated_at
                ) VALUES
                    ('req-bare', '2026-07-03T01:00:00Z', 'POST', '/v1/responses', 'model', 'gpt-5.5',
                     'official', 'official', 200, 'upstream', 10, 'test', 'test'),
                    ('req-prefixed', '2026-07-03T01:00:01Z', 'POST', '/v1/responses', 'model', 'openai/gpt-5.5',
                     'official', 'official', 200, 'upstream', 10, 'test', 'test')
                "#,
                [],
            )
            .unwrap();

    let events = read_usage_events_from_sqlite_path(&db_path, usize::MAX).unwrap();

    assert_eq!(events.len(), 2);
    assert!(events
        .iter()
        .all(|event| event.model.as_deref() == Some("openai/gpt-5.5")));
}

#[test]
fn telemetry_backfill_imports_jsonl_to_sqlite_idempotently() {
    let root = unique_temp_dir("codexhub-usage-backfill");
    fs::create_dir_all(&root).unwrap();
    let log_path = root.join("codex-proxy-events.jsonl");
    let db_path = root.join("codex-proxy-telemetry.sqlite");
    fs::write(
            &log_path,
            [
                r#"{"ts":"2026-07-03T01:00:00Z","event":"request_start","request_id":"req-backfill","method":"POST","path":"/v1/responses","upstream":"official","model":"openai/gpt-5.5"}"#,
                r#"{"ts":"2026-07-03T01:00:03Z","event":"request_complete","request_id":"req-backfill","status":200,"duration_ms":3000,"usage_source":"upstream","usage_input_tokens":7,"usage_output_tokens":3,"upstream":"official","model":"openai/gpt-5.5"}"#,
            ]
            .join("\n"),
        )
        .unwrap();

    super::backfill_event_log_to_sqlite_path(&log_path, &db_path).unwrap();
    super::backfill_event_log_to_sqlite_path(&log_path, &db_path).unwrap();

    let events = read_usage_events_from_sqlite_path(&db_path, usize::MAX).unwrap();
    let connection = rusqlite::Connection::open(&db_path).unwrap();
    let event_count: i64 = connection
        .query_row("SELECT COUNT(*) FROM gateway_events", [], |row| row.get(0))
        .unwrap();
    let request_count: i64 = connection
        .query_row("SELECT COUNT(*) FROM gateway_requests", [], |row| {
            row.get(0)
        })
        .unwrap();
    let backfill_size: String = connection
        .query_row(
            "SELECT value FROM telemetry_meta WHERE key = 'last_backfill_size'",
            [],
            |row| row.get(0),
        )
        .unwrap();

    assert_eq!(events.len(), 1);
    assert_eq!(events[0].input_tokens, Some(7));
    assert_eq!(event_count, 2);
    assert_eq!(request_count, 1);
    assert_eq!(
        backfill_size,
        fs::metadata(&log_path).unwrap().len().to_string()
    );
}

#[test]
fn telemetry_backfill_projects_usage_observed_into_request_usage() {
    let root = unique_temp_dir("codexhub-usage-observed-backfill");
    fs::create_dir_all(&root).unwrap();
    let log_path = root.join("codex-proxy-events.jsonl");
    let db_path = root.join("codex-proxy-telemetry.sqlite");
    fs::write(
            &log_path,
            [
                r#"{"ts":"2026-07-07T01:00:00Z","event":"request_complete","request_id":"req-usage-observed-rust","status":200,"usage_source":"missing","usage_missing_reason":"async_usage_pending","upstream":"official","model":"openai/gpt-5.5"}"#,
                r#"{"ts":"2026-07-07T01:00:01Z","event":"usage_observed","request_id":"req-usage-observed-rust","usage_source":"upstream_async","usage_input_tokens":11,"usage_cached_input_tokens":3,"usage_output_tokens":5,"usage_total_tokens":16,"upstream":"official","model":"openai/gpt-5.5"}"#,
            ]
            .join("\n"),
        )
        .unwrap();

    super::backfill_event_log_to_sqlite_path(&log_path, &db_path).unwrap();

    let connection = rusqlite::Connection::open(&db_path).unwrap();
    let row: (String, i64, i64, i64, i64) = connection
            .query_row(
                "SELECT usage_source, usage_input_tokens, usage_cached_input_tokens, usage_output_tokens, usage_total_tokens FROM gateway_requests WHERE request_id = 'req-usage-observed-rust'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?)),
            )
            .unwrap();

    assert_eq!(row, ("upstream_async".to_string(), 11, 3, 5, 16));
}

#[test]
fn telemetry_backfill_does_not_downgrade_prior_usage_observed() {
    let root = unique_temp_dir("codexhub-usage-observed-before-complete");
    fs::create_dir_all(&root).unwrap();
    let log_path = root.join("codex-proxy-events.jsonl");
    let db_path = root.join("codex-proxy-telemetry.sqlite");
    fs::write(
            &log_path,
            [
                r#"{"ts":"2026-07-07T01:00:00Z","event":"usage_observed","request_id":"req-usage-before-complete-rust","usage_source":"upstream_async","usage_input_tokens":11,"usage_cached_input_tokens":3,"usage_output_tokens":5,"usage_total_tokens":16,"upstream":"official","model":"openai/gpt-5.5"}"#,
                r#"{"ts":"2026-07-07T01:00:01Z","event":"request_complete","request_id":"req-usage-before-complete-rust","status":200,"usage_source":"missing","usage_missing_reason":"async_usage_pending","upstream":"official","model":"openai/gpt-5.5"}"#,
            ]
            .join("\n"),
        )
        .unwrap();

    super::backfill_event_log_to_sqlite_path(&log_path, &db_path).unwrap();

    let connection = rusqlite::Connection::open(&db_path).unwrap();
    let row: (String, Option<String>, i64, i64, i64, i64) = connection
            .query_row(
                "SELECT usage_source, usage_missing_reason, usage_input_tokens, usage_cached_input_tokens, usage_output_tokens, usage_total_tokens FROM gateway_requests WHERE request_id = 'req-usage-before-complete-rust'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?, row.get(5)?)),
            )
            .unwrap();

    assert_eq!(row, ("upstream_async".to_string(), None, 11, 3, 5, 16));
}

#[test]
fn telemetry_backfill_preserves_distinct_events_for_same_request_id() {
    let root = unique_temp_dir("codexhub-usage-backfill-duplicate-request");
    fs::create_dir_all(&root).unwrap();
    let log_path = root.join("codex-proxy-events.jsonl");
    let db_path = root.join("codex-proxy-telemetry.sqlite");
    fs::write(
            &log_path,
            [
                r#"{"ts":"2026-07-03T01:00:00Z","event":"request_error","request_id":"req-retry","status":502,"duration_ms":100,"usage_missing_reason":"upstream_error"}"#,
                r#"{"ts":"2026-07-03T01:00:01Z","event":"request_error","request_id":"req-retry","status":504,"duration_ms":200,"usage_missing_reason":"upstream_timeout"}"#,
                r#"{"duration_ms":200,"request_id":"req-retry","event":"request_error","usage_missing_reason":"upstream_timeout","status":504,"ts":"2026-07-03T01:00:01Z"}"#,
            ]
            .join("\n"),
        )
        .unwrap();

    super::backfill_event_log_to_sqlite_path(&log_path, &db_path).unwrap();
    super::backfill_event_log_to_sqlite_path(&log_path, &db_path).unwrap();

    let connection = rusqlite::Connection::open(&db_path).unwrap();
    let event_count: i64 = connection
        .query_row("SELECT COUNT(*) FROM gateway_events", [], |row| row.get(0))
        .unwrap();
    let request_count: i64 = connection
        .query_row("SELECT COUNT(*) FROM gateway_requests", [], |row| {
            row.get(0)
        })
        .unwrap();
    let request: (String, i64, i64, String) = connection
            .query_row(
                "SELECT completed_ts, status, duration_ms, usage_missing_reason FROM gateway_requests WHERE request_id = 'req-retry'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();

    assert_eq!(event_count, 2);
    assert_eq!(request_count, 1);
    assert_eq!(
        request,
        (
            "2026-07-03T01:00:01Z".to_string(),
            504,
            200,
            "upstream_timeout".to_string()
        )
    );
}

#[test]
fn telemetry_incremental_ingest_processes_only_complete_lines() {
    let root = unique_temp_dir("codexhub-usage-incremental-partial");
    fs::create_dir_all(&root).unwrap();
    let log_path = root.join("codex-proxy-events.jsonl");
    let db_path = root.join("codex-proxy-telemetry.sqlite");
    let first = r#"{"ts":"2026-07-03T01:00:00Z","event":"request_complete","request_id":"req-a","status":200,"duration_ms":10,"usage_source":"upstream","usage_input_tokens":1,"usage_output_tokens":2,"upstream":"official","model":"openai/gpt-5.5"}"#;
    let partial = r#"{"ts":"2026-07-03T01:00:01Z","event":"request_complete","request_id":"req-b""#;
    fs::write(&log_path, format!("{first}\n{partial}")).unwrap();

    let status = super::ingest_telemetry_once_for_paths(&log_path, &db_path).unwrap();
    let events = read_usage_events_from_sqlite_path(&db_path, usize::MAX).unwrap();

    assert_eq!(events.len(), 1);
    assert_eq!(events[0].request_id.as_deref(), Some("req-a"));
    assert_eq!(status.indexed_offset, first.len() as u64 + 1);
    assert_eq!(status.lag_bytes, partial.len() as u64);
    assert!(status.backfill_pending);

    let mut file = fs::OpenOptions::new().append(true).open(&log_path).unwrap();
    file.write_all(
            br#","status":200,"duration_ms":11,"usage_source":"upstream","usage_input_tokens":3,"usage_output_tokens":4,"upstream":"official","model":"openai/gpt-5.5"}"#,
        )
        .unwrap();
    file.write_all(b"\n").unwrap();

    let status = super::ingest_telemetry_once_for_paths(&log_path, &db_path).unwrap();
    let events = read_usage_events_from_sqlite_path(&db_path, usize::MAX).unwrap();

    assert_eq!(events.len(), 2);
    assert_eq!(events[1].request_id.as_deref(), Some("req-b"));
    assert_eq!(
        status.indexed_offset,
        fs::metadata(&log_path).unwrap().len()
    );
    assert_eq!(status.lag_bytes, 0);
    assert!(!status.backfill_pending);
}

#[test]
fn telemetry_incremental_ingest_resets_after_log_truncation_and_dedupes() {
    let root = unique_temp_dir("codexhub-usage-incremental-truncate");
    fs::create_dir_all(&root).unwrap();
    let log_path = root.join("codex-proxy-events.jsonl");
    let db_path = root.join("codex-proxy-telemetry.sqlite");
    let first = r#"{"ts":"2026-07-03T01:00:00Z","event":"request_complete","request_id":"req-a","status":200,"duration_ms":10,"usage_source":"upstream","usage_input_tokens":1,"usage_output_tokens":2,"upstream":"official","model":"openai/gpt-5.5"}"#;
    let new_second = r#"{"ts":"2026-07-03T01:00:02Z","event":"request_complete","request_id":"req-new","status":200,"duration_ms":12,"usage_source":"upstream","usage_input_tokens":5,"usage_output_tokens":6,"upstream":"official","model":"openai/gpt-5.5"}"#;
    fs::write(&log_path, format!("{first}\n{}\n", "x".repeat(512))).unwrap();
    super::ingest_telemetry_once_for_paths(&log_path, &db_path).unwrap();

    fs::write(&log_path, format!("{first}\n{new_second}\n")).unwrap();
    let status = super::ingest_telemetry_once_for_paths(&log_path, &db_path).unwrap();
    let connection = rusqlite::Connection::open(&db_path).unwrap();
    let event_count: i64 = connection
        .query_row("SELECT COUNT(*) FROM gateway_events", [], |row| row.get(0))
        .unwrap();
    let new_request_count: i64 = connection
        .query_row(
            "SELECT COUNT(*) FROM gateway_requests WHERE request_id = 'req-new'",
            [],
            |row| row.get(0),
        )
        .unwrap();

    assert_eq!(event_count, 2);
    assert_eq!(new_request_count, 1);
    assert_eq!(
        status.indexed_offset,
        fs::metadata(&log_path).unwrap().len()
    );
    assert_eq!(status.lag_bytes, 0);
}

#[test]
fn telemetry_ingest_skips_sqlite_when_jsonl_size_is_unchanged() {
    let root = unique_temp_dir("codexhub-usage-ingest-skip-unchanged");
    fs::create_dir_all(&root).unwrap();
    let log_path = root.join("codex-proxy-events.jsonl");
    let db_path = root.join("codex-proxy-telemetry.sqlite");
    let line = r#"{"ts":"2026-07-03T01:00:00Z","event":"request_complete","request_id":"req-skip","status":200,"duration_ms":10,"usage_source":"upstream","usage_input_tokens":1,"usage_output_tokens":2,"upstream":"official","model":"openai/gpt-5.5"}"#;
    fs::write(&log_path, format!("{line}\n")).unwrap();

    super::reset_telemetry_sqlite_ready_calls();
    let first = super::ingest_telemetry_once_for_paths(&log_path, &db_path).unwrap();
    let after_first = super::telemetry_sqlite_ready_calls();
    assert!(after_first > 0);
    assert_eq!(first.lag_bytes, 0);

    let second = super::ingest_telemetry_once_for_paths(&log_path, &db_path).unwrap();
    assert_eq!(super::telemetry_sqlite_ready_calls(), after_first);
    assert_eq!(second.indexed_offset, first.indexed_offset);
    assert_eq!(second.event_log_size, first.event_log_size);
    assert_eq!(second.lag_bytes, 0);
}

#[test]
fn telemetry_ingest_rebuilds_deleted_sqlite_when_jsonl_is_unchanged() {
    let root = unique_temp_dir("codexhub-usage-ingest-rebuild-deleted-db");
    fs::create_dir_all(&root).unwrap();
    let log_path = root.join("codex-proxy-events.jsonl");
    let db_path = root.join("codex-proxy-telemetry.sqlite");
    let line = r#"{"ts":"2026-07-03T01:00:00Z","event":"request_complete","request_id":"req-rebuild","status":200,"duration_ms":10,"usage_source":"upstream","usage_input_tokens":1,"usage_output_tokens":2,"upstream":"official","model":"openai/gpt-5.5"}"#;
    fs::write(&log_path, format!("{line}\n")).unwrap();

    super::ingest_telemetry_once_for_paths(&log_path, &db_path).unwrap();
    fs::remove_file(&db_path).unwrap();

    let rebuilt = super::ingest_telemetry_once_for_paths(&log_path, &db_path).unwrap();
    let events = read_usage_events_from_sqlite_path(&db_path, usize::MAX).unwrap();

    assert_eq!(rebuilt.lag_bytes, 0);
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].request_id.as_deref(), Some("req-rebuild"));
}

#[test]
fn usage_snapshot_reports_lag_without_inline_backfill() {
    let root = unique_temp_dir("codexhub-usage-snapshot-lag");
    fs::create_dir_all(&root).unwrap();
    let log_path = root.join("codex-proxy-events.jsonl");
    let db_path = root.join("codex-proxy-telemetry.sqlite");
    fs::write(
            &log_path,
            r#"{"ts":"2026-07-03T01:00:00Z","event":"request_complete","request_id":"req-lag","status":200,"duration_ms":10,"usage_source":"upstream","usage_input_tokens":1,"usage_output_tokens":2,"upstream":"official","model":"openai/gpt-5.5"}"#,
        )
        .unwrap();

    let snapshot =
        super::gateway_usage_snapshot_for_paths(&log_path, &db_path, None, None, None).unwrap();
    let connection = rusqlite::Connection::open(&db_path).unwrap();
    let event_count: i64 = connection
        .query_row("SELECT COUNT(*) FROM gateway_events", [], |row| row.get(0))
        .unwrap();

    assert_eq!(snapshot.summary.requests, 0);
    assert!(snapshot.events.is_empty());
    assert_eq!(event_count, 0);
    assert_eq!(snapshot.telemetry_status.indexed_offset, 0);
    assert_eq!(
        snapshot.telemetry_status.lag_bytes,
        fs::metadata(&log_path).unwrap().len()
    );
    assert!(snapshot.telemetry_status.backfill_pending);
}

#[test]
fn usage_pricing_includes_openai_aliases_and_priority_fast_rates() {
    let pricing = usage_pricing_by_model();
    let base = super::lookup_usage_pricing(&pricing, "gpt-5.5").expect("gpt-5.5 pricing");
    let namespaced = super::lookup_usage_pricing(&pricing, "openai/gpt-5.5").expect("openai alias");
    let fast = super::lookup_usage_pricing(&pricing, "openai/gpt-5.5-fast").expect("fast pricing");

    assert_eq!(base.input_per_million, namespaced.input_per_million);
    assert_eq!(fast.input_per_million, 12.50);
    assert_eq!(fast.cached_input_per_million, Some(1.25));
    assert_eq!(fast.output_per_million, 75.00);
}

#[test]
fn usage_pricing_includes_official_cached_input_rates() {
    let pricing = usage_pricing_by_model();

    assert_eq!(
        super::lookup_usage_pricing(&pricing, "gpt-5.5")
            .and_then(|pricing| pricing.cached_input_per_million),
        Some(0.50)
    );
    assert_eq!(
        super::lookup_usage_pricing(&pricing, "gpt-5.4")
            .and_then(|pricing| pricing.cached_input_per_million),
        Some(0.25)
    );
    assert_eq!(
        super::lookup_usage_pricing(&pricing, "gpt-5.4-mini")
            .and_then(|pricing| pricing.cached_input_per_million),
        Some(0.0375)
    );
}

#[test]
fn plan_opencode_apply_does_not_write_or_backup() {
    let root = unique_temp_dir("codexhub-opencode-plan");
    let config_path = root.join("opencode.json");
    let original = r#"{"model":"anthropic/claude-sonnet-4"}"#;
    fs::create_dir_all(root.as_path()).unwrap();
    fs::write(&config_path, original).unwrap();
    let settings = Settings::default();

    let decision = super::plan_opencode_apply(&config_path, &settings, &[], "openai/gpt-5.5")
        .unwrap();
    match decision {
        super::OpenCodeApplyDecision::Apply(plan) => {
            assert_eq!(fs::read_to_string(&config_path).unwrap(), original);
            assert!(plan.next.contains("codexhub"));
            assert!(!plan.skip_snapshot);
        }
        super::OpenCodeApplyDecision::NotApplied(_) => panic!("expected apply plan"),
    }
}

#[test]
fn opencode_apply_creates_backup_before_managed_overwrite() {
    let root = unique_temp_dir("codexhub-opencode");
    let config_path = root.join("opencode.json");
    let backup_root = root.join("backups");
    fs::create_dir_all(root.as_path()).unwrap();
    fs::write(&config_path, r#"{"model":"anthropic/claude-sonnet-4"}"#).unwrap();
    let settings = Settings::default();

    let result = apply_opencode_config_with_paths(
        &config_path,
        &[stable_root(backup_root.clone())],
        &settings,
        &[],
        "openai/gpt-5.5",
    )
    .unwrap();

    assert!(result.applied);
    assert!(result.backup_path.unwrap().exists());
    let written = fs::read_to_string(&config_path).unwrap();
    assert!(written.contains("codexhub"));
    assert!(!written.contains("codexhub_managed"));
    // ADR-0004 / #435: the user's model selection stays untouched.
    assert!(written.contains("anthropic/claude-sonnet-4"));
    assert!(written.contains("codexhub-proxy"));
}

#[test]
fn opencode_apply_does_not_back_up_managed_config() {
    let root = unique_temp_dir("codexhub-opencode-managed");
    let config_path = root.join("opencode.json");
    let backup_root = root.join("backups");
    fs::create_dir_all(root.as_path()).unwrap();
    fs::write(
        &config_path,
        r#"{"model":"codexhub/openai/gpt-5.5","provider":{"codexhub":{"name":"CodexHub"}}}"#,
    )
    .unwrap();
    let settings = Settings::default();

    let result = apply_opencode_config_with_paths(
        &config_path,
        &[stable_root(backup_root.clone())],
        &settings,
        &[],
        "openai/gpt-5.4",
    )
    .unwrap();

    assert!(result.applied);
    assert!(result.backup_path.is_none());
    assert!(!backup_root
        .read_dir()
        .map(|mut entries| entries.next().is_some())
        .unwrap_or(false));
    let written = fs::read_to_string(&config_path).unwrap();
    assert!(!written.contains("codexhub_managed"));
    // ADR-0004 / #435: the existing model selection stays user-owned.
    assert!(written.contains("codexhub/openai/gpt-5.5"));
}

#[test]
fn opencode_apply_rejects_invalid_model_before_backup_side_effects() {
    let root = unique_temp_dir("codexhub-opencode-invalid-model");
    let config_path = root.join("opencode.json");
    let backup_root = root.join("backups");
    fs::create_dir_all(root.as_path()).unwrap();
    fs::write(&config_path, r#"{"model":"anthropic/claude-sonnet-4"}"#).unwrap();
    let original = fs::read_to_string(&config_path).unwrap();
    let settings = Settings::default();
    let providers = case_sensitive_client_export_test_providers();

    let error = apply_opencode_config_with_paths(
        &config_path,
        &[stable_root(backup_root.clone())],
        &settings,
        &providers,
        "minimax-cn/MINIMAX-M3",
    )
    .unwrap_err();

    assert!(error.contains("Gateway model is not exported: minimax-cn/MINIMAX-M3"));
    assert!(!backup_root.exists());
    assert_eq!(fs::read_to_string(&config_path).unwrap(), original);
}

#[test]
fn opencode_apply_backs_up_unmanaged_codexhub_prefix_provider() {
    let root = unique_temp_dir("codexhub-opencode-unmanaged-prefix");
    let config_path = root.join("opencode.json");
    let backup_root = root.join("backups");
    fs::create_dir_all(root.as_path()).unwrap();
    fs::write(
            &config_path,
            r#"{"model":"codexhub-labs/custom","provider":{"codexhub-labs":{"name":"CodexHub Labs","options":{"baseURL":"https://labs.example.test/v1","apiKey":"labs-key"},"models":{"custom":{"name":"Custom"}}}}}"#,
        )
        .unwrap();
    let settings = Settings::default();

    let result = apply_opencode_config_with_paths(
        &config_path,
        &[stable_root(backup_root.clone())],
        &settings,
        &[],
        "openai/gpt-5.5",
    )
    .unwrap();

    assert!(result.backup_path.is_some());
    let backup_path = result.backup_path.unwrap();
    assert!(backup_path.exists());
    assert!(fs::read_to_string(backup_path)
        .unwrap()
        .contains("codexhub-labs"));
}

#[test]
fn opencode_restore_skips_managed_backups_and_strips_invalid_keys() {
    let root = unique_temp_dir("codexhub-opencode-restore");
    let config_path = root.join("opencode.json");
    let backup_root = root.join("backups");
    fs::create_dir_all(backup_root.as_path()).unwrap();
    fs::write(&config_path, r#"{"model":"codexhub/openai/gpt-5.5"}"#).unwrap();
    let official_backup = backup_root.join("opencode-official.json");
    fs::write(
        &official_backup,
        r#"{"model":"anthropic/claude-sonnet-4","codexhub_managed":false}"#,
    )
    .unwrap();
    std::thread::sleep(std::time::Duration::from_millis(2));
    fs::write(
        backup_root.join("opencode-managed.json"),
        r#"{"model":"codexhub/openai/gpt-5.5","provider":{"codexhub":{"name":"CodexHub"}}}"#,
    )
    .unwrap();

    let result = restore_latest_backup("opencode", &config_path, &backup_root).unwrap();

    assert!(result.applied);
    assert_eq!(
        result.backup_path.as_deref(),
        Some(official_backup.as_path())
    );
    let written = fs::read_to_string(&config_path).unwrap();
    assert!(written.contains("anthropic/claude-sonnet-4"));
    assert!(!written.contains("codexhub_managed"));
    assert!(!written.contains("provider"));
}

#[test]
fn opencode_official_restore_survives_stable_then_beta_takeover() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-cross-channel-restore");
    let config_path = root.join("opencode.json");
    let stable_backups = root.join("stable-backups");
    let beta_backups = root.join("beta-backups");
    fs::create_dir_all(&root).unwrap();
    fs::write(&config_path, r#"{"model":"anthropic/claude-sonnet-4"}"#).unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));
    let settings = Settings::default();

    apply_opencode_config_with_paths(
        &config_path,
        &[stable_root(stable_backups.clone())],
        &settings,
        &[],
        "openai/gpt-5.5",
    )
    .unwrap();
    apply_opencode_config_with_paths(
        &config_path,
        &[beta_root(beta_backups.clone())],
        &settings,
        &[],
        "openai/gpt-5.4",
    )
    .unwrap();

    let result = super::restore_opencode_config_with_backup_roots(
        &config_path,
        &[
            beta_root(beta_backups.clone()),
            stable_root(stable_backups.clone()),
        ],
    )
    .unwrap();

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.applied);
    assert!(fs::read_to_string(&config_path)
        .unwrap()
        .contains("anthropic/claude-sonnet-4"));
}

#[test]
fn opencode_stable_takeover_adopts_beta_legacy_baseline() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-stable-adopts-beta");
    let config_path = root.join("opencode.json");
    let stable_backups = root.join("stable-backups");
    let beta_backups = root.join("beta-backups");
    fs::create_dir_all(&root).unwrap();
    fs::create_dir_all(&stable_backups).unwrap();
    fs::create_dir_all(&beta_backups).unwrap();
    fs::write(&config_path, r#"{"model":"codexhub/openai/gpt-5.5"}"#).unwrap();
    let beta_file = beta_backups.join("opencode-beta.json");
    fs::write(&beta_file, r#"{"model":"anthropic/claude-sonnet-4-beta"}"#).unwrap();
    let newer = std::time::SystemTime::now() + std::time::Duration::from_secs(60);
    std::fs::OpenOptions::new()
        .write(true)
        .open(&beta_file)
        .unwrap()
        .set_times(std::fs::FileTimes::new().set_modified(newer))
        .unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));
    let settings = Settings::default();

    apply_opencode_config_with_paths(
        &config_path,
        &[
            stable_root(stable_backups.clone()),
            beta_root(beta_backups.clone()),
        ],
        &settings,
        &[],
        "openai/gpt-5.5",
    )
    .unwrap();

    let baseline = super::read_rollback_baseline("opencode").unwrap().unwrap();
    assert_eq!(
        baseline.files.get("opencode.json"),
        Some(&super::BaselineFile::Snapshot {
            content: r#"{"model":"anthropic/claude-sonnet-4-beta"}"#.to_string()
        })
    );

    let result = super::restore_opencode_config_with_backup_roots(
        &config_path,
        &[
            stable_root(stable_backups.clone()),
            beta_root(beta_backups.clone()),
        ],
    )
    .unwrap();

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.applied);
    assert!(fs::read_to_string(&config_path)
        .unwrap()
        .contains("anthropic/claude-sonnet-4-beta"));
}

#[test]
fn opencode_beta_takeover_adopts_stable_legacy_baseline() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-beta-adopts-stable");
    let config_path = root.join("opencode.json");
    let stable_backups = root.join("stable-backups");
    let beta_backups = root.join("beta-backups");
    fs::create_dir_all(&root).unwrap();
    fs::create_dir_all(&stable_backups).unwrap();
    fs::create_dir_all(&beta_backups).unwrap();
    fs::write(&config_path, r#"{"model":"codexhub/openai/gpt-5.5"}"#).unwrap();
    let stable_file = stable_backups.join("opencode-stable.json");
    fs::write(
        &stable_file,
        r#"{"model":"anthropic/claude-sonnet-4-stable"}"#,
    )
    .unwrap();
    let newer = std::time::SystemTime::now() + std::time::Duration::from_secs(60);
    std::fs::OpenOptions::new()
        .write(true)
        .open(&stable_file)
        .unwrap()
        .set_times(std::fs::FileTimes::new().set_modified(newer))
        .unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));
    let settings = Settings::default();

    apply_opencode_config_with_paths(
        &config_path,
        &[
            beta_root(beta_backups.clone()),
            stable_root(stable_backups.clone()),
        ],
        &settings,
        &[],
        "openai/gpt-5.5",
    )
    .unwrap();

    let baseline = super::read_rollback_baseline("opencode").unwrap().unwrap();
    assert_eq!(
        baseline.files.get("opencode.json"),
        Some(&super::BaselineFile::Snapshot {
            content: r#"{"model":"anthropic/claude-sonnet-4-stable"}"#.to_string()
        })
    );

    let result = super::restore_opencode_config_with_backup_roots(
        &config_path,
        &[
            beta_root(beta_backups.clone()),
            stable_root(stable_backups.clone()),
        ],
    )
    .unwrap();

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.applied);
    assert!(fs::read_to_string(&config_path)
        .unwrap()
        .contains("anthropic/claude-sonnet-4-stable"));
}

#[test]
fn plan_pi_apply_does_not_write_or_backup() {
    let root = unique_temp_dir("codexhub-pi-plan");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    let original_settings =
        r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4"}"#;
    let original_models = r#"{"providers":{}}"#;
    fs::create_dir_all(root.as_path()).unwrap();
    fs::write(&settings_path, original_settings).unwrap();
    fs::write(&models_path, original_models).unwrap();
    let settings = Settings::default();

    let plan = super::plan_pi_apply(
        &settings_path,
        &models_path,
        &settings,
        &[],
        "openai/gpt-5.5",
    )
    .unwrap();
    assert_eq!(fs::read_to_string(&settings_path).unwrap(), original_settings);
    assert_eq!(fs::read_to_string(&models_path).unwrap(), original_models);
    assert!(plan.next_models.contains("codexhub"));
    assert!(!plan.skip_snapshot);
}

#[test]
fn pi_apply_writes_models_and_settings_with_backup() {
    let root = unique_temp_dir("codexhub-pi");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    let backup_root = root.join("backups");
    fs::create_dir_all(root.as_path()).unwrap();
    fs::write(
            &settings_path,
            r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4","enabledModels":["anthropic/*"],"theme":"dark"}"#,
        )
        .unwrap();
    fs::write(
            &models_path,
            r#"{"providers":{"ollama":{"baseUrl":"http://localhost:11434/v1","api":"openai-completions","apiKey":"ollama","models":[{"id":"qwen2.5-coder:7b"}]}}}"#,
        )
        .unwrap();
    let settings = Settings::default();

    let result = super::apply_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[stable_root(backup_root.clone())],
        &settings,
        &[],
        "openai/gpt-5.5",
    )
    .unwrap();

    assert!(result.applied);
    let backup_path = result.backup_path.unwrap();
    assert!(backup_path.join("settings.json").exists());
    assert!(backup_path.join("models.json").exists());
    let written_settings: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&settings_path).unwrap()).unwrap();
    assert_eq!(
        written_settings
            .get("defaultProvider")
            .and_then(serde_json::Value::as_str),
        Some("anthropic")
    );
    assert_eq!(
        written_settings
            .get("defaultModel")
            .and_then(serde_json::Value::as_str),
        Some("claude-sonnet-4")
    );
    assert_eq!(
        written_settings
            .get("theme")
            .and_then(serde_json::Value::as_str),
        Some("dark")
    );
    assert_eq!(
        written_settings
            .get("enabledModels")
            .and_then(serde_json::Value::as_array)
            .map(|models| models
                .iter()
                .filter_map(serde_json::Value::as_str)
                .collect::<Vec<_>>()),
        Some(vec!["anthropic/*"])
    );

    let written_models: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&models_path).unwrap()).unwrap();
    assert!(written_models.pointer("/providers/ollama").is_some());
    let provider = written_models
        .pointer("/providers/codexhub-openai")
        .unwrap();
    assert_eq!(
        provider.get("baseUrl").and_then(serde_json::Value::as_str),
        Some("http://127.0.0.1:9099/v1/providers/openai")
    );
    assert_eq!(
        provider.get("api").and_then(serde_json::Value::as_str),
        Some("openai-responses")
    );
    assert_eq!(
        provider.get("apiKey").and_then(serde_json::Value::as_str),
        Some("codexhub-proxy")
    );
    assert_eq!(
        provider
            .pointer("/models/0/id")
            .and_then(serde_json::Value::as_str),
        Some("gpt-5.5")
    );
}

#[test]
fn pi_apply_and_detach_preserve_foreign_providers_and_activation() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-apply-detach-cycle");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    let backup_root = root.join("backups");
    fs::create_dir_all(root.as_path()).unwrap();
    let original_settings = r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4","enabledModels":["anthropic/*"],"theme":"dark"}"#;
    fs::write(&settings_path, original_settings).unwrap();
    fs::write(
            &models_path,
            r#"{"providers":{"ollama":{"baseUrl":"http://localhost:11434/v1","api":"openai-completions","apiKey":"ollama","models":[{"id":"qwen2.5-coder:7b"}]}}}"#,
        )
        .unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));
    let settings = Settings::default();

    super::apply_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[stable_root(backup_root.clone())],
        &settings,
        &[],
        "openai/gpt-5.5",
    )
    .unwrap();

    let connected_settings: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&settings_path).unwrap()).unwrap();
    let connected_models: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&models_path).unwrap()).unwrap();
    assert_eq!(
        connected_settings
            .get("defaultProvider")
            .and_then(serde_json::Value::as_str),
        Some("anthropic")
    );
    assert_eq!(
        connected_settings
            .get("enabledModels")
            .and_then(serde_json::Value::as_array)
            .map(|models| models
                .iter()
                .filter_map(serde_json::Value::as_str)
                .collect::<Vec<_>>()),
        Some(vec!["anthropic/*"])
    );
    assert!(connected_models.pointer("/providers/ollama").is_some());
    assert!(connected_models
        .pointer("/providers/codexhub-openai")
        .is_some());

    let detach = super::pi_ownership_bounded_cleanup(&settings_path, &models_path).unwrap();
    assert!(detach.applied);

    assert_eq!(
        fs::read_to_string(&settings_path).unwrap(),
        original_settings
    );
    let detached_models: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&models_path).unwrap()).unwrap();
    assert!(detached_models.pointer("/providers/ollama").is_some());
    assert!(detached_models
        .pointer("/providers/codexhub-openai")
        .is_none());
    assert_eq!(
        detached_models
            .pointer("/providers/ollama/models/0/id")
            .and_then(serde_json::Value::as_str),
        Some("qwen2.5-coder:7b")
    );
    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
}

#[test]
fn pi_apply_on_takeover_state_leaves_user_owned_activation() {
    let root = unique_temp_dir("codexhub-pi-takeover-upgrade");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    let backup_root = root.join("backups");
    fs::create_dir_all(root.as_path()).unwrap();
    fs::write(
        &settings_path,
        r#"{"defaultProvider":"codexhub-openai","defaultModel":"gpt-5.5"}"#,
    )
    .unwrap();
    fs::write(
            &models_path,
            r#"{"providers":{"codexhub-openai":{"baseUrl":"http://127.0.0.1:9099/v1/providers/openai","api":"openai-responses","apiKey":"codexhub-proxy","models":[{"id":"gpt-5.5"}]},"ollama":{"baseUrl":"http://localhost:11434/v1","api":"openai-completions","models":[{"id":"qwen2.5-coder:7b"}]}}}"#,
        )
        .unwrap();
    let settings = Settings::default();

    super::apply_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[stable_root(backup_root.clone())],
        &settings,
        &[],
        "openai/gpt-5.5",
    )
    .unwrap();

    let written_settings: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&settings_path).unwrap()).unwrap();
    let written_models: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&models_path).unwrap()).unwrap();
    assert_eq!(
        written_settings
            .get("defaultProvider")
            .and_then(serde_json::Value::as_str),
        Some("codexhub-openai"),
        "takeover leftovers stay user-owned; apply must not rewrite activation"
    );
    assert_eq!(
        written_settings
            .get("defaultModel")
            .and_then(serde_json::Value::as_str),
        Some("gpt-5.5")
    );
    assert!(written_models.pointer("/providers/ollama").is_some());
    assert!(written_models
        .pointer("/providers/codexhub-openai")
        .is_some());
}

#[test]
fn pi_apply_rejects_invalid_model_before_backup_side_effects() {
    let root = unique_temp_dir("codexhub-pi-invalid-model");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    let backup_root = root.join("backups");
    fs::create_dir_all(root.as_path()).unwrap();
    fs::write(
        &settings_path,
        r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4"}"#,
    )
    .unwrap();
    fs::write(
        &models_path,
        r#"{"providers":{"anthropic":{"models":[{"id":"claude-sonnet-4"}]}}}"#,
    )
    .unwrap();
    let original_settings = fs::read_to_string(&settings_path).unwrap();
    let original_models = fs::read_to_string(&models_path).unwrap();
    let settings = Settings::default();
    let providers = case_sensitive_client_export_test_providers();

    let error = super::apply_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[stable_root(backup_root.clone())],
        &settings,
        &providers,
        "minimax-cn/MINIMAX-M3",
    )
    .unwrap_err();

    assert!(error.contains("Gateway model is not exported: minimax-cn/MINIMAX-M3"));
    assert!(!backup_root.exists());
    assert_eq!(
        fs::read_to_string(&settings_path).unwrap(),
        original_settings
    );
    assert_eq!(fs::read_to_string(&models_path).unwrap(), original_models);
}

#[test]
fn pi_restore_skips_managed_snapshot_and_restores_clean_pair() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-restore");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    let backup_root = root.join("backups");
    let official_backup = backup_root.join("pi-official");
    let managed_backup = backup_root.join("pi-managed");
    fs::create_dir_all(official_backup.as_path()).unwrap();
    fs::create_dir_all(managed_backup.as_path()).unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));
    fs::write(
        &settings_path,
        r#"{"defaultProvider":"codexhub","defaultModel":"openai/gpt-5.5"}"#,
    )
    .unwrap();
    fs::write(
        &models_path,
        r#"{"providers":{"codexhub":{"models":[{"id":"openai/gpt-5.5"}]}}}"#,
    )
    .unwrap();
    fs::write(
        official_backup.join("settings.json"),
        r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4"}"#,
    )
    .unwrap();
    fs::write(
            official_backup.join("models.json"),
            r#"{"providers":{"anthropic":{"baseUrl":"https://api.anthropic.com","api":"anthropic-messages","apiKey":"key","models":[{"id":"claude-sonnet-4"}]}}}"#,
        )
        .unwrap();
    std::thread::sleep(std::time::Duration::from_millis(2));
    fs::write(
        managed_backup.join("settings.json"),
        r#"{"defaultProvider":"codexhub","defaultModel":"openai/gpt-5.4"}"#,
    )
    .unwrap();
    fs::write(
        managed_backup.join("models.json"),
        r#"{"providers":{"codexhub":{"models":[{"id":"openai/gpt-5.4"}]}}}"#,
    )
    .unwrap();

    let result = super::restore_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[stable_root(backup_root.clone())],
    )
    .unwrap();

    assert!(result.applied);
    // Restore results are sanitized: no absolute legacy backup path is exposed.
    assert_eq!(result.backup_path, None);
    let settings = fs::read_to_string(&settings_path).unwrap();
    let models = fs::read_to_string(&models_path).unwrap();
    assert!(settings.contains("anthropic"));
    assert!(models.contains("claude-sonnet-4"));
    assert!(!settings.contains("codexhub"));
    assert!(!models.contains("codexhub"));
    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
}

#[test]
fn opencode_restore_empty_legacy_roots_removes_managed_config() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-empty-legacy-cleanup");
    let config_path = root.join("opencode.json");
    let backup_root = root.join("backups");
    fs::create_dir_all(&root).unwrap();
    fs::create_dir_all(&backup_root).unwrap();
    fs::write(
        &config_path,
        r#"{
  "provider": {
    "codexhub-openai": {
      "options": {
        "baseURL": "http://127.0.0.1:9099/v1"
      },
      "models": {
        "gpt-5.5": {}
      }
    }
  },
  "model": "codexhub-openai/gpt-5.5"
}
"#,
    )
    .unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    let result = super::restore_opencode_config_with_backup_roots(
        &config_path,
        &[stable_root(backup_root.clone())],
    )
    .unwrap();

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.applied);
    assert!(!config_path.exists());
    assert!(
        !result.message.contains("\\\\") && !result.message.contains('/'),
        "message must not leak absolute paths"
    );
    assert!(
        !result.message.contains("codexhub-openai"),
        "message must not leak config contents"
    );
}

#[test]
fn pi_restore_missing_legacy_roots_removes_managed_config() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-missing-legacy-cleanup");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    fs::create_dir_all(&root).unwrap();
    fs::write(
        &settings_path,
        r#"{"defaultProvider":"codexhub-openai","defaultModel":"gpt-5.5"}"#,
    )
    .unwrap();
    fs::write(
            &models_path,
            r#"{"providers":{"codexhub-openai":{"baseUrl":"http://127.0.0.1:9099/v1/providers/openai","api":"openai-responses","apiKey":"codexhub-proxy","models":[{"id":"gpt-5.5"}]}}}"#,
        )
        .unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    let result = super::restore_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[stable_root(root.join("nonexistent-backups"))],
    )
    .unwrap();

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.applied);
    assert!(settings_path.exists());
    assert_eq!(
        fs::read_to_string(&settings_path).unwrap(),
        r#"{"defaultProvider":"codexhub-openai","defaultModel":"gpt-5.5"}"#
    );
    assert!(!models_path.exists());
    assert!(
        !result.message.contains("\\\\") && !result.message.contains('/'),
        "message must not leak absolute paths"
    );
    assert!(
        !result.message.contains("codexhub-proxy"),
        "message must not leak secrets"
    );
}

#[test]
fn opencode_restore_prefers_canonical_baseline_over_empty_legacy_roots() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-baseline-restore");
    let config_path = root.join("opencode.json");
    let backup_root = root.join("backups");
    fs::create_dir_all(&root).unwrap();
    fs::create_dir_all(&backup_root).unwrap();
    fs::write(
        &config_path,
        r#"{
  "provider": {
    "codexhub-openai": {
      "options": {
        "baseURL": "http://127.0.0.1:9099/v1"
      },
      "models": {
        "gpt-5.5": {}
      }
    }
  },
  "model": "codexhub-openai/gpt-5.5"
}
"#,
    )
    .unwrap();
    let provenance_dir = root.join("provenance");
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", &provenance_dir);
    let baseline = super::RollbackBaseline {
        version: super::ROLLBACK_BASELINE_VERSION,
        recorded_at: 1,
        files: [(
            "opencode.json".to_string(),
            super::BaselineFile::Snapshot {
                content: r#"{"model":"anthropic/claude-sonnet-4"}"#.to_string(),
            },
        )]
        .into_iter()
        .collect(),
    };
    super::write_rollback_baseline_atomic("opencode", &baseline).unwrap();

    let result = super::restore_opencode_config_with_backup_roots(
        &config_path,
        &[stable_root(backup_root.clone())],
    )
    .unwrap();

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.applied);
    assert_eq!(
        fs::read_to_string(&config_path).unwrap(),
        r#"{"model":"anthropic/claude-sonnet-4"}"#
    );
    assert_eq!(result.backup_path, None);
    assert_eq!(
        result.message,
        "OpenCode official config restored from canonical baseline."
    );
}

#[test]
fn pi_restore_prefers_canonical_baseline_over_missing_legacy_roots() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-baseline-restore");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    fs::create_dir_all(&root).unwrap();
    fs::write(
        &settings_path,
        r#"{"defaultProvider":"codexhub-openai","defaultModel":"gpt-5.5"}"#,
    )
    .unwrap();
    fs::write(
            &models_path,
            r#"{"providers":{"codexhub-openai":{"baseUrl":"http://127.0.0.1:9099/v1/providers/openai","api":"openai-responses","apiKey":"codexhub-proxy","models":[{"id":"gpt-5.5"}]}}}"#,
        )
        .unwrap();
    let provenance_dir = root.join("provenance");
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", &provenance_dir);
    let baseline = super::RollbackBaseline {
            version: super::ROLLBACK_BASELINE_VERSION,
            recorded_at: 1,
            files: [
                (
                    "settings.json".to_string(),
                    super::BaselineFile::Snapshot {
                        content: r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4","theme":"dark"}"#.to_string(),
                    },
                ),
                (
                    "models.json".to_string(),
                    super::BaselineFile::Snapshot {
                        content: r#"{"providers":{"anthropic":{"models":[{"id":"claude-sonnet-4"}]}}}"#.to_string(),
                    },
                ),
            ]
            .into_iter()
            .collect(),
        };
    super::write_rollback_baseline_atomic("pi", &baseline).unwrap();

    let result = super::restore_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[stable_root(root.join("nonexistent-backups"))],
    )
    .unwrap();

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.applied);
    let settings: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&settings_path).unwrap()).unwrap();
    assert_eq!(
        settings.get("theme").and_then(serde_json::Value::as_str),
        Some("dark")
    );
    assert!(!fs::read_to_string(&models_path)
        .unwrap()
        .contains("codexhub"));
    assert_eq!(result.backup_path, None);
    assert_eq!(
        result.message,
        "Pi official config restored from canonical baseline."
    );
}

#[test]
fn opencode_reapply_preserves_original_canonical_baseline() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-reapply-baseline");
    let config_path = root.join("opencode.json");
    let backup_root = root.join("backups");
    fs::create_dir_all(&root).unwrap();
    fs::write(&config_path, r#"{"model":"anthropic/claude-sonnet-4"}"#).unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));
    let settings = Settings::default();

    super::apply_opencode_config_with_paths(
        &config_path,
        &[stable_root(backup_root.clone())],
        &settings,
        &[],
        "openai/gpt-5.5",
    )
    .unwrap();
    super::apply_opencode_config_with_paths(
        &config_path,
        &[stable_root(backup_root.clone())],
        &settings,
        &[],
        "openai/gpt-5.4",
    )
    .unwrap();

    let result = super::restore_opencode_config_with_backup_roots(
        &config_path,
        &[stable_root(backup_root.clone())],
    )
    .unwrap();

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.applied);
    let written = fs::read_to_string(&config_path).unwrap();
    assert!(written.contains("anthropic/claude-sonnet-4"));
    assert!(!written.contains("codexhub"));
    assert_eq!(
        result.message,
        "OpenCode official config restored from canonical baseline."
    );
}

#[test]
fn pi_reapply_preserves_original_canonical_baseline() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-reapply-baseline");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    let backup_root = root.join("backups");
    fs::create_dir_all(&root).unwrap();
    fs::write(
        &settings_path,
        r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4","theme":"dark"}"#,
    )
    .unwrap();
    fs::write(
        &models_path,
        r#"{"providers":{"anthropic":{"models":[{"id":"claude-sonnet-4"}]}}}"#,
    )
    .unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));
    let settings = Settings::default();

    super::apply_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[stable_root(backup_root.clone())],
        &settings,
        &[],
        "openai/gpt-5.5",
    )
    .unwrap();
    super::apply_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[stable_root(backup_root.clone())],
        &settings,
        &[],
        "openai/gpt-5.4",
    )
    .unwrap();

    let result = super::restore_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[stable_root(backup_root.clone())],
    )
    .unwrap();

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.applied);
    let settings: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&settings_path).unwrap()).unwrap();
    assert_eq!(
        settings.get("theme").and_then(serde_json::Value::as_str),
        Some("dark")
    );
    assert_eq!(
        settings
            .get("defaultProvider")
            .and_then(serde_json::Value::as_str),
        Some("anthropic")
    );
    assert_eq!(
        result.message,
        "Pi official config restored from canonical baseline."
    );
}

#[test]
fn opencode_restore_adopts_eligible_legacy_snapshot_into_baseline() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-legacy-adoption");
    let config_path = root.join("opencode.json");
    let backup_root = root.join("backups");
    let official_backup = backup_root.join("opencode-official.json");
    fs::create_dir_all(&root).unwrap();
    fs::create_dir_all(&backup_root).unwrap();
    fs::write(&config_path, r#"{"model":"codexhub/openai/gpt-5.5"}"#).unwrap();
    fs::write(&official_backup, r#"{"model":"anthropic/claude-sonnet-4"}"#).unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    let result = super::restore_opencode_config_with_backup_roots(
        &config_path,
        &[stable_root(backup_root.clone())],
    )
    .unwrap();

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.applied);
    let written = fs::read_to_string(&config_path).unwrap();
    assert!(written.contains("anthropic/claude-sonnet-4"));
    assert!(!written.contains("codexhub"));
    assert_eq!(
        result.message,
        "OpenCode official config restored from legacy-adopted baseline."
    );
    // Legacy source remains intact.
    assert!(official_backup.exists());
}

#[test]
fn pi_restore_adopts_eligible_legacy_snapshot_into_baseline() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-legacy-adoption");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    let backup_root = root.join("backups");
    let official_backup = backup_root.join("pi-official");
    fs::create_dir_all(&root).unwrap();
    fs::create_dir_all(&official_backup).unwrap();
    fs::write(
        &settings_path,
        r#"{"defaultProvider":"codexhub-openai","defaultModel":"gpt-5.5"}"#,
    )
    .unwrap();
    fs::write(
            &models_path,
            r#"{"providers":{"codexhub-openai":{"baseUrl":"http://127.0.0.1:9099/v1/providers/openai","api":"openai-responses","apiKey":"codexhub-proxy","models":[{"id":"gpt-5.5"}]}}}"#,
        )
        .unwrap();
    fs::write(
        official_backup.join("settings.json"),
        r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4"}"#,
    )
    .unwrap();
    fs::write(
        official_backup.join("models.json"),
        r#"{"providers":{"anthropic":{"models":[{"id":"claude-sonnet-4"}]}}}"#,
    )
    .unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    let result = super::restore_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[stable_root(backup_root.clone())],
    )
    .unwrap();

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.applied);
    assert!(fs::read_to_string(&settings_path)
        .unwrap()
        .contains("anthropic"));
    assert!(!fs::read_to_string(&models_path)
        .unwrap()
        .contains("codexhub"));
    assert_eq!(
        result.message,
        "Pi official config restored from legacy-adopted baseline."
    );
    assert!(official_backup.exists());
}

#[test]
fn pi_stable_takeover_adopts_beta_legacy_baseline() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-stable-adopts-beta");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    let stable_backups = root.join("stable-backups");
    let beta_backups = root.join("beta-backups");
    let _stable_backup = stable_backups.join("pi-stable");
    let beta_backup = beta_backups.join("pi-beta");
    fs::create_dir_all(&root).unwrap();
    fs::create_dir_all(&beta_backup).unwrap();
    fs::write(
        &settings_path,
        r#"{"defaultProvider":"codexhub-openai","defaultModel":"gpt-5.5"}"#,
    )
    .unwrap();
    fs::write(
            &models_path,
            r#"{"providers":{"codexhub-openai":{"baseUrl":"http://127.0.0.1:9099/v1/providers/openai","api":"openai-responses","apiKey":"codexhub-proxy","models":[{"id":"gpt-5.5"}]}}}"#,
        )
        .unwrap();
    fs::write(
        beta_backup.join("settings.json"),
        r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4-beta"}"#,
    )
    .unwrap();
    fs::write(
        beta_backup.join("models.json"),
        r#"{"providers":{"anthropic":{"models":[{"id":"claude-sonnet-4-beta"}]}}}"#,
    )
    .unwrap();
    let newer = std::time::SystemTime::now() + std::time::Duration::from_secs(60);
    std::fs::OpenOptions::new()
        .write(true)
        .open(beta_backup.join("settings.json"))
        .unwrap()
        .set_times(std::fs::FileTimes::new().set_modified(newer))
        .unwrap();
    std::fs::OpenOptions::new()
        .write(true)
        .open(beta_backup.join("models.json"))
        .unwrap()
        .set_times(std::fs::FileTimes::new().set_modified(newer))
        .unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));
    let settings = Settings::default();

    super::apply_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[
            stable_root(stable_backups.clone()),
            beta_root(beta_backups.clone()),
        ],
        &settings,
        &client_export_test_providers(),
        "minimax/minimax-m3",
    )
    .unwrap();

    let baseline = super::read_rollback_baseline("pi").unwrap().unwrap();
    assert!(
        matches!(
            baseline.files.get("settings.json"),
            Some(super::BaselineFile::Snapshot { content, .. }) if content.contains("claude-sonnet-4-beta")
        ),
        "Beta legacy baseline must be adopted into canonical provenance"
    );

    let result = super::restore_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[
            stable_root(stable_backups.clone()),
            beta_root(beta_backups.clone()),
        ],
    )
    .unwrap();

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.applied);
    assert!(fs::read_to_string(&settings_path)
        .unwrap()
        .contains("claude-sonnet-4-beta"));
}

#[test]
fn pi_beta_takeover_adopts_stable_legacy_baseline() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-beta-adopts-stable");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    let stable_backups = root.join("stable-backups");
    let beta_backups = root.join("beta-backups");
    let stable_backup = stable_backups.join("pi-stable");
    fs::create_dir_all(&root).unwrap();
    fs::create_dir_all(&stable_backup).unwrap();
    fs::write(
        &settings_path,
        r#"{"defaultProvider":"codexhub-openai","defaultModel":"gpt-5.5"}"#,
    )
    .unwrap();
    fs::write(
            &models_path,
            r#"{"providers":{"codexhub-openai":{"baseUrl":"http://127.0.0.1:9099/v1/providers/openai","api":"openai-responses","apiKey":"codexhub-proxy","models":[{"id":"gpt-5.5"}]}}}"#,
        )
        .unwrap();
    fs::write(
        stable_backup.join("settings.json"),
        r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4-stable"}"#,
    )
    .unwrap();
    fs::write(
        stable_backup.join("models.json"),
        r#"{"providers":{"anthropic":{"models":[{"id":"claude-sonnet-4-stable"}]}}}"#,
    )
    .unwrap();
    let newer = std::time::SystemTime::now() + std::time::Duration::from_secs(60);
    std::fs::OpenOptions::new()
        .write(true)
        .open(stable_backup.join("settings.json"))
        .unwrap()
        .set_times(std::fs::FileTimes::new().set_modified(newer))
        .unwrap();
    std::fs::OpenOptions::new()
        .write(true)
        .open(stable_backup.join("models.json"))
        .unwrap()
        .set_times(std::fs::FileTimes::new().set_modified(newer))
        .unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));
    let settings = Settings::default();

    super::apply_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[
            beta_root(beta_backups.clone()),
            stable_root(stable_backups.clone()),
        ],
        &settings,
        &[],
        "openai/gpt-5.5",
    )
    .unwrap();

    let baseline = super::read_rollback_baseline("pi").unwrap().unwrap();
    assert!(
        matches!(
            baseline.files.get("settings.json"),
            Some(super::BaselineFile::Snapshot { content, .. }) if content.contains("claude-sonnet-4-stable")
        ),
        "Stable legacy baseline must be adopted into canonical provenance"
    );

    let result = super::restore_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[
            beta_root(beta_backups.clone()),
            stable_root(stable_backups.clone()),
        ],
    )
    .unwrap();

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.applied);
    assert!(fs::read_to_string(&settings_path)
        .unwrap()
        .contains("claude-sonnet-4-stable"));
}

#[test]
fn opencode_equal_mtime_adoption_is_independent_of_caller() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-equal-mtime");
    let config_path = root.join("opencode.json");
    let stable_backups = root.join("stable-backups");
    let beta_backups = root.join("beta-backups");
    fs::create_dir_all(&root).unwrap();
    fs::create_dir_all(&stable_backups).unwrap();
    fs::create_dir_all(&beta_backups).unwrap();
    fs::write(&config_path, r#"{"model":"codexhub/openai/gpt-5.5"}"#).unwrap();
    fs::write(
        stable_backups.join("opencode-stable.json"),
        r#"{"model":"anthropic/claude-sonnet-4-stable"}"#,
    )
    .unwrap();
    fs::write(
        beta_backups.join("opencode-beta.json"),
        r#"{"model":"anthropic/claude-sonnet-4-beta"}"#,
    )
    .unwrap();
    // Deliberately equal mtimes: channel/name must break the tie, not caller order.
    let shared_mtime = std::time::SystemTime::now() + std::time::Duration::from_secs(60);
    for path in [
        stable_backups.join("opencode-stable.json"),
        beta_backups.join("opencode-beta.json"),
    ] {
        std::fs::OpenOptions::new()
            .write(true)
            .open(&path)
            .unwrap()
            .set_times(std::fs::FileTimes::new().set_modified(shared_mtime))
            .unwrap();
    }
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    for caller_roots in [
        vec![
            stable_root(stable_backups.clone()),
            beta_root(beta_backups.clone()),
        ],
        vec![
            beta_root(beta_backups.clone()),
            stable_root(stable_backups.clone()),
        ],
    ] {
        let provenance_dir = root.join(format!(
            "provenance-{}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_millis(),
            TEMP_DIR_COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", &provenance_dir);
        let result = super::apply_opencode_config_with_paths(
            &config_path,
            &caller_roots,
            &Settings::default(),
            &[],
            "openai/gpt-5.5",
        )
        .unwrap();
        assert!(result.applied);
        let baseline = super::read_rollback_baseline("opencode").unwrap().unwrap();
        assert_eq!(
            baseline.files.get("opencode.json"),
            Some(&super::BaselineFile::Snapshot {
                content: r#"{"model":"anthropic/claude-sonnet-4-stable"}"#.to_string()
            }),
            "Stable must win for equal mtimes regardless of caller order"
        );
    }

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
}

#[test]
fn pi_equal_mtime_adoption_is_independent_of_caller() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-equal-mtime");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    let stable_backups = root.join("stable-backups");
    let beta_backups = root.join("beta-backups");
    let stable_backup = stable_backups.join("pi-stable");
    let beta_backup = beta_backups.join("pi-beta");
    fs::create_dir_all(&root).unwrap();
    fs::create_dir_all(&stable_backup).unwrap();
    fs::create_dir_all(&beta_backup).unwrap();
    fs::write(
        &settings_path,
        r#"{"defaultProvider":"codexhub-openai","defaultModel":"gpt-5.5"}"#,
    )
    .unwrap();
    fs::write(
            &models_path,
            r#"{"providers":{"codexhub-openai":{"baseUrl":"http://127.0.0.1:9099/v1/providers/openai","api":"openai-responses","apiKey":"codexhub-proxy","models":[{"id":"gpt-5.5"}]}}}"#,
        )
        .unwrap();
    fs::write(
        stable_backup.join("settings.json"),
        r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4-stable"}"#,
    )
    .unwrap();
    fs::write(
        stable_backup.join("models.json"),
        r#"{"providers":{"anthropic":{"models":[{"id":"claude-sonnet-4-stable"}]}}}"#,
    )
    .unwrap();
    fs::write(
        beta_backup.join("settings.json"),
        r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4-beta"}"#,
    )
    .unwrap();
    fs::write(
        beta_backup.join("models.json"),
        r#"{"providers":{"anthropic":{"models":[{"id":"claude-sonnet-4-beta"}]}}}"#,
    )
    .unwrap();
    let shared_mtime = std::time::SystemTime::now() + std::time::Duration::from_secs(60);
    for backup in [&stable_backup, &beta_backup] {
        for file in ["settings.json", "models.json"] {
            std::fs::OpenOptions::new()
                .write(true)
                .open(backup.join(file))
                .unwrap()
                .set_times(std::fs::FileTimes::new().set_modified(shared_mtime))
                .unwrap();
        }
    }
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    for caller_roots in [
        vec![
            stable_root(stable_backups.clone()),
            beta_root(beta_backups.clone()),
        ],
        vec![
            beta_root(beta_backups.clone()),
            stable_root(stable_backups.clone()),
        ],
    ] {
        let provenance_dir = root.join(format!(
            "provenance-{}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_millis(),
            TEMP_DIR_COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", &provenance_dir);
        let result = super::apply_pi_config_with_paths(
            &settings_path,
            &models_path,
            &caller_roots,
            &Settings::default(),
            &[],
            "openai/gpt-5.5",
        )
        .unwrap();
        assert!(result.applied);
        let baseline = super::read_rollback_baseline("pi").unwrap().unwrap();
        assert!(
            matches!(
                baseline.files.get("settings.json"),
                Some(super::BaselineFile::Snapshot { content, .. }) if content.contains("claude-sonnet-4-stable")
            ),
            "Stable must win for equal mtimes regardless of caller order"
        );
    }

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
}

#[test]
fn opencode_equal_mtime_adoption_ignores_parent_path_channel_names() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-misleading-parent");
    let config_path = root.join("opencode.json");
    // Stable root nested under a path containing "beta"; Beta root nested under a path containing "stable".
    let stable_backups = root.join("beta-channel").join("stable-snapshots");
    let beta_backups = root.join("stable-channel").join("beta-snapshots");
    fs::create_dir_all(&stable_backups).unwrap();
    fs::create_dir_all(&beta_backups).unwrap();
    fs::write(&config_path, r#"{"model":"codexhub/openai/gpt-5.5"}"#).unwrap();
    // Identical snapshot names so name cannot break the tie.
    fs::write(
        stable_backups.join("opencode.json"),
        r#"{"model":"anthropic/claude-sonnet-4-stable"}"#,
    )
    .unwrap();
    fs::write(
        beta_backups.join("opencode.json"),
        r#"{"model":"anthropic/claude-sonnet-4-beta"}"#,
    )
    .unwrap();
    let shared_mtime = std::time::SystemTime::now() + std::time::Duration::from_secs(60);
    for path in [
        stable_backups.join("opencode.json"),
        beta_backups.join("opencode.json"),
    ] {
        std::fs::OpenOptions::new()
            .write(true)
            .open(&path)
            .unwrap()
            .set_times(std::fs::FileTimes::new().set_modified(shared_mtime))
            .unwrap();
    }

    for caller_roots in [
        vec![
            stable_root(stable_backups.clone()),
            beta_root(beta_backups.clone()),
        ],
        vec![
            beta_root(beta_backups.clone()),
            stable_root(stable_backups.clone()),
        ],
    ] {
        let provenance_dir = root.join(format!(
            "provenance-{}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_millis(),
            TEMP_DIR_COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", &provenance_dir);
        let result = super::apply_opencode_config_with_paths(
            &config_path,
            &caller_roots,
            &Settings::default(),
            &[],
            "openai/gpt-5.5",
        )
        .unwrap();
        assert!(result.applied);
        let baseline = super::read_rollback_baseline("opencode").unwrap().unwrap();
        assert_eq!(
                baseline.files.get("opencode.json"),
                Some(&super::BaselineFile::Snapshot {
                    content: r#"{"model":"anthropic/claude-sonnet-4-stable"}"#.to_string()
                }),
                "Stable must win for equal mtimes/snapshot names regardless of parent path or caller order"
            );
    }

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
}

#[test]
fn pi_equal_mtime_adoption_ignores_parent_path_channel_names() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-misleading-parent");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    // Stable root nested under a path containing "beta"; Beta root nested under a path containing "stable".
    let stable_backups = root.join("beta-channel").join("stable-snapshots");
    let beta_backups = root.join("stable-channel").join("beta-snapshots");
    let stable_backup = stable_backups.join("pi");
    let beta_backup = beta_backups.join("pi");
    fs::create_dir_all(&stable_backup).unwrap();
    fs::create_dir_all(&beta_backup).unwrap();
    fs::write(
        &settings_path,
        r#"{"defaultProvider":"codexhub-openai","defaultModel":"gpt-5.5"}"#,
    )
    .unwrap();
    fs::write(
            &models_path,
            r#"{"providers":{"codexhub-openai":{"baseUrl":"http://127.0.0.1:9099/v1/providers/openai","api":"openai-responses","apiKey":"codexhub-proxy","models":[{"id":"gpt-5.5"}]}}}"#,
        )
        .unwrap();
    // Identical snapshot names so name cannot break the tie.
    fs::write(
        stable_backup.join("settings.json"),
        r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4-stable"}"#,
    )
    .unwrap();
    fs::write(
        stable_backup.join("models.json"),
        r#"{"providers":{"anthropic":{"models":[{"id":"claude-sonnet-4-stable"}]}}}"#,
    )
    .unwrap();
    fs::write(
        beta_backup.join("settings.json"),
        r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4-beta"}"#,
    )
    .unwrap();
    fs::write(
        beta_backup.join("models.json"),
        r#"{"providers":{"anthropic":{"models":[{"id":"claude-sonnet-4-beta"}]}}}"#,
    )
    .unwrap();
    let shared_mtime = std::time::SystemTime::now() + std::time::Duration::from_secs(60);
    for backup in [&stable_backup, &beta_backup] {
        for file in ["settings.json", "models.json"] {
            std::fs::OpenOptions::new()
                .write(true)
                .open(backup.join(file))
                .unwrap()
                .set_times(std::fs::FileTimes::new().set_modified(shared_mtime))
                .unwrap();
        }
    }

    for caller_roots in [
        vec![
            stable_root(stable_backups.clone()),
            beta_root(beta_backups.clone()),
        ],
        vec![
            beta_root(beta_backups.clone()),
            stable_root(stable_backups.clone()),
        ],
    ] {
        let provenance_dir = root.join(format!(
            "provenance-{}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_millis(),
            TEMP_DIR_COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", &provenance_dir);
        let result = super::apply_pi_config_with_paths(
            &settings_path,
            &models_path,
            &caller_roots,
            &Settings::default(),
            &[],
            "openai/gpt-5.5",
        )
        .unwrap();
        assert!(result.applied);
        let baseline = super::read_rollback_baseline("pi").unwrap().unwrap();
        assert!(
                matches!(
                    baseline.files.get("settings.json"),
                    Some(super::BaselineFile::Snapshot { content, .. }) if content.contains("claude-sonnet-4-stable")
                ),
                "Stable must win for equal mtimes/snapshot names regardless of parent path or caller order"
            );
    }

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
}

#[test]
fn pi_restore_absent_tombstone_removes_only_owned_targets() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-absent-tombstone");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    fs::create_dir_all(&root).unwrap();
    fs::write(
        &settings_path,
        r#"{"defaultProvider":"codexhub-openai","defaultModel":"gpt-5.5"}"#,
    )
    .unwrap();
    fs::write(
            &models_path,
            r#"{"providers":{"codexhub-openai":{"baseUrl":"http://127.0.0.1:9099/v1/providers/openai","api":"openai-responses","apiKey":"codexhub-proxy","models":[{"id":"gpt-5.5"}]}}}"#,
        )
        .unwrap();
    let provenance_dir = root.join("provenance");
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", &provenance_dir);
    let baseline = super::RollbackBaseline {
        version: super::ROLLBACK_BASELINE_VERSION,
        recorded_at: 1,
        files: [
            ("settings.json".to_string(), super::BaselineFile::Absent),
            ("models.json".to_string(), super::BaselineFile::Absent),
        ]
        .into_iter()
        .collect(),
    };
    super::write_rollback_baseline_atomic("pi", &baseline).unwrap();

    let result = super::restore_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[stable_root(root.join("backups"))],
    )
    .unwrap();

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.applied);
    assert!(!settings_path.exists());
    assert!(!models_path.exists());
    assert_eq!(
        result.message,
        "Pi config removed; original baseline recorded targets as absent."
    );
}

#[test]
fn opencode_restore_malformed_config_fails_without_mutation() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-malformed");
    let config_path = root.join("opencode.json");
    fs::create_dir_all(&root).unwrap();
    fs::write(&config_path, "not json").unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    let result = super::restore_opencode_config_with_backup_roots(
        &config_path,
        &[stable_root(root.join("backups"))],
    );

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.is_err());
    assert_eq!(fs::read_to_string(&config_path).unwrap(), "not json");
}
