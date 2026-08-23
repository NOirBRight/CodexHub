#[test]
fn pi_restore_ignores_malformed_settings_and_detaches_models() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-malformed");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    fs::create_dir_all(&root).unwrap();
    fs::write(&settings_path, "not json").unwrap();
    fs::write(
            &models_path,
            r#"{"providers":{"codexhub-openai":{"baseUrl":"http://127.0.0.1:9099/v1/providers/openai","api":"openai-responses","apiKey":"codexhub-proxy","models":[{"id":"gpt-5.5"}]}}}"#,
        )
        .unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    let result = super::restore_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[stable_root(root.join("backups"))],
    )
    .unwrap();

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.applied);
    assert_eq!(fs::read_to_string(&settings_path).unwrap(), "not json");
    assert!(!models_path.exists());
}

#[test]
fn opencode_apply_records_baseline_before_first_managed_write() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-first-apply-baseline");
    let config_path = root.join("opencode.json");
    let backup_root = root.join("backups");
    fs::create_dir_all(&root).unwrap();
    fs::write(&config_path, r#"{"model":"anthropic/claude-sonnet-4"}"#).unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));
    let settings = Settings::default();

    let result = super::apply_opencode_config_with_paths(
        &config_path,
        &[stable_root(backup_root.clone())],
        &settings,
        &[],
        "openai/gpt-5.5",
    )
    .unwrap();

    assert!(result.applied);
    let baseline = super::read_rollback_baseline("opencode").unwrap().unwrap();
    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(matches!(
        baseline.files.get("opencode.json"),
        Some(super::BaselineFile::Snapshot { .. })
    ));
    let written = fs::read_to_string(&config_path).unwrap();
    assert!(written.contains("codexhub"));
}

#[test]
fn pi_apply_records_absence_tombstone_before_first_managed_write() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-first-apply-tombstone");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    let backup_root = root.join("backups");
    fs::create_dir_all(&root).unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));
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
    let baseline = super::read_rollback_baseline("pi").unwrap().unwrap();
    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert_eq!(
        baseline.files.get("settings.json"),
        Some(&super::BaselineFile::Absent)
    );
    assert_eq!(
        baseline.files.get("models.json"),
        Some(&super::BaselineFile::Absent)
    );
    assert!(!settings_path.exists());
    assert!(models_path.exists());
}

#[test]
fn opencode_restore_rejects_corrupt_baseline() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-corrupt-baseline");
    let config_path = root.join("opencode.json");
    let provenance_dir = root.join("provenance");
    fs::create_dir_all(&root).unwrap();
    fs::write(&config_path, r#"{"model":"codexhub/openai/gpt-5.5"}"#).unwrap();
    fs::create_dir_all(provenance_dir.join("opencode")).unwrap();
    fs::write(
        provenance_dir.join("opencode").join("baseline.json"),
        "not json",
    )
    .unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", &provenance_dir);

    let result = super::restore_opencode_config_with_backup_roots(
        &config_path,
        &[stable_root(root.join("backups"))],
    );

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.is_err());
    let error = result.unwrap_err();
    assert!(
        error.contains("corrupt"),
        "expected corrupt baseline error, got: {error}"
    );
    assert!(!error.contains("\\") && !error.contains('/'));
    assert_eq!(
        fs::read_to_string(&config_path).unwrap(),
        r#"{"model":"codexhub/openai/gpt-5.5"}"#
    );
}

#[test]
fn opencode_restore_rejects_unsupported_baseline_version() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-baseline-version");
    let config_path = root.join("opencode.json");
    let provenance_dir = root.join("provenance");
    fs::create_dir_all(&root).unwrap();
    fs::write(&config_path, r#"{"model":"codexhub/openai/gpt-5.5"}"#).unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", &provenance_dir);
    fs::create_dir_all(provenance_dir.join("opencode")).unwrap();
    fs::write(
            provenance_dir.join("opencode").join("baseline.json"),
            r#"{"version":999,"recorded_at":1,"files":{"opencode.json":{"snapshot":{"content":"{\"model\":\"anthropic/claude-sonnet-4\"}"}}}}"#,
        )
        .unwrap();

    let result = super::restore_opencode_config_with_backup_roots(
        &config_path,
        &[stable_root(root.join("backups"))],
    );

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.is_err());
    let error = result.unwrap_err();
    assert!(
        error.contains("unsupported"),
        "expected unsupported version error, got: {error}"
    );
    assert!(!error.contains("\\") && !error.contains('/'));
    assert_eq!(
        fs::read_to_string(&config_path).unwrap(),
        r#"{"model":"codexhub/openai/gpt-5.5"}"#
    );
}

#[test]
fn pi_restore_rejects_corrupt_baseline() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-corrupt-baseline");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    let provenance_dir = root.join("provenance");
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
    fs::create_dir_all(provenance_dir.join("pi")).unwrap();
    fs::write(provenance_dir.join("pi").join("baseline.json"), "not json").unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", &provenance_dir);

    let result = super::restore_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[stable_root(root.join("backups"))],
    );

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.is_err());
    let error = result.unwrap_err();
    assert!(
        error.contains("corrupt"),
        "expected corrupt baseline error, got: {error}"
    );
    assert!(!error.contains("\\") && !error.contains('/'));
}

#[test]
fn pi_restore_rejects_unsupported_baseline_version() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-baseline-version");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    let provenance_dir = root.join("provenance");
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
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", &provenance_dir);
    fs::create_dir_all(provenance_dir.join("pi")).unwrap();
    fs::write(
            provenance_dir.join("pi").join("baseline.json"),
            r#"{"version":999,"recorded_at":1,"files":{"settings.json":{"snapshot":{"content":"{\"defaultProvider\":\"anthropic\",\"defaultModel\":\"claude-sonnet-4\"}"}},"models.json":{"snapshot":{"content":"{\"providers\":{\"anthropic\":{\"models\":[{\"id\":\"claude-sonnet-4\"}]}}}"}}}}"#,
        )
        .unwrap();

    let result = super::restore_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[stable_root(root.join("backups"))],
    );

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.is_err());
    let error = result.unwrap_err();
    assert!(
        error.contains("unsupported"),
        "expected unsupported version error, got: {error}"
    );
    assert!(!error.contains("\\") && !error.contains('/'));
}

#[test]
fn pi_restore_rejects_incomplete_baseline() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-incomplete-baseline");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    let provenance_dir = root.join("provenance");
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
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", &provenance_dir);
    let baseline = super::RollbackBaseline {
        version: super::ROLLBACK_BASELINE_VERSION,
        recorded_at: 1,
        files: [(
            "settings.json".to_string(),
            super::BaselineFile::Snapshot {
                content: r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4"}"#
                    .to_string(),
            },
        )]
        .into_iter()
        .collect(),
    };
    super::write_rollback_baseline_atomic("pi", &baseline).unwrap();

    let result = super::restore_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[stable_root(root.join("backups"))],
    );

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.is_err());
    let error = result.unwrap_err();
    assert!(
        error.contains("incomplete"),
        "expected incomplete baseline error, got: {error}"
    );
    assert!(!error.contains("\\") && !error.contains('/'));
}

#[test]
fn opencode_canonical_restore_preserves_exact_bytes() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-byte-for-byte");
    let config_path = root.join("opencode.json");
    let backup_root = root.join("backups");
    let provenance_dir = root.join("provenance");
    fs::create_dir_all(&root).unwrap();
    fs::create_dir_all(&backup_root).unwrap();
    fs::write(&config_path, r#"{"model":"codexhub/openai/gpt-5.5"}"#).unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", &provenance_dir);
    let original_snapshot =
        r#"{"model":"anthropic/claude-sonnet-4","codexhub_managed":false,"extra":1}"#;
    let baseline = super::RollbackBaseline {
        version: super::ROLLBACK_BASELINE_VERSION,
        recorded_at: 1,
        files: [(
            "opencode.json".to_string(),
            super::BaselineFile::Snapshot {
                content: original_snapshot.to_string(),
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
    assert_eq!(fs::read_to_string(&config_path).unwrap(), original_snapshot);
    assert_eq!(
        result.message,
        "OpenCode official config restored from canonical baseline."
    );
}

#[test]
fn pi_cleanup_preserves_unrelated_enabled_models() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-cleanup-enabled");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    fs::create_dir_all(&root).unwrap();
    fs::write(
            &settings_path,
            r#"{"defaultProvider":"codexhub-openai","defaultModel":"gpt-5.5","theme":"dark","enabledModels":["codexhub-openai/gpt-5.5","anthropic/claude-sonnet-4"]}"#,
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
        &[stable_root(root.join("backups"))],
    )
    .unwrap();

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.applied);
    let settings: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&settings_path).unwrap()).unwrap();
    assert_eq!(
        settings
            .get("defaultProvider")
            .and_then(serde_json::Value::as_str),
        Some("codexhub-openai")
    );
    assert_eq!(
        settings
            .get("defaultModel")
            .and_then(serde_json::Value::as_str),
        Some("gpt-5.5")
    );
    assert_eq!(
        settings.get("theme").and_then(serde_json::Value::as_str),
        Some("dark")
    );
    let enabled = settings
        .get("enabledModels")
        .and_then(serde_json::Value::as_array)
        .unwrap();
    assert_eq!(enabled.len(), 2);
    assert_eq!(enabled[0], "codexhub-openai/gpt-5.5");
    assert_eq!(enabled[1], "anthropic/claude-sonnet-4");
    assert!(!models_path.exists());
}

#[test]
fn pi_cleanup_detaches_models_without_requiring_managed_settings() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-cleanup-unmanaged-settings");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    fs::create_dir_all(&root).unwrap();
    let original_settings =
        r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4","theme":"dark"}"#;
    fs::write(&settings_path, original_settings).unwrap();
    fs::write(
            &models_path,
            r#"{"providers":{"codexhub-openai":{"baseUrl":"http://127.0.0.1:9099/v1/providers/openai","api":"openai-responses","apiKey":"codexhub-proxy","models":[{"id":"gpt-5.5"}]}}}"#,
        )
        .unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    let result = super::restore_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[stable_root(root.join("backups"))],
    )
    .unwrap();

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.applied);
    assert_eq!(
        fs::read_to_string(&settings_path).unwrap(),
        original_settings
    );
    assert!(!models_path.exists());
}

#[test]
fn pi_cleanup_ignores_wrong_shaped_enabled_models_and_detaches_models() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-cleanup-shape");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    fs::create_dir_all(&root).unwrap();
    let original_settings =
        r#"{"defaultProvider":"codexhub-openai","enabledModels":"not-an-array"}"#;
    fs::write(&settings_path, original_settings).unwrap();
    fs::write(
            &models_path,
            r#"{"providers":{"codexhub-openai":{"baseUrl":"http://127.0.0.1:9099/v1/providers/openai","api":"openai-responses","apiKey":"codexhub-proxy","models":[{"id":"gpt-5.5"}]}}}"#,
        )
        .unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    let result = super::restore_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[stable_root(root.join("backups"))],
    )
    .unwrap();

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.applied);
    assert_eq!(
        fs::read_to_string(&settings_path).unwrap(),
        original_settings
    );
    assert!(!models_path.exists());
}

#[test]
fn opencode_cleanup_rejects_malformed_provider_entries_without_mutation() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-cleanup-provider-entries");
    let config_path = root.join("opencode.json");
    fs::create_dir_all(&root).unwrap();
    let original = r#"{"model":"codexhub-openai/gpt-5.5","provider":{"codexhub-openai":{"baseUrl":"http://127.0.0.1:9099/v1/providers/openai","api":"openai-responses","apiKey":"codexhub-proxy"},"unrelated":"not-an-object"}}"#;
    fs::write(&config_path, original).unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    let result = super::opencode_ownership_bounded_cleanup(&config_path);

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.is_err());
    assert!(result.unwrap_err().contains("malformed entries"));
    assert_eq!(fs::read_to_string(&config_path).unwrap(), original);
}

#[test]
fn pi_cleanup_leaves_user_activation_even_when_enabled_models_are_heterogeneous() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-cleanup-heterogeneous-enabled");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    fs::create_dir_all(&root).unwrap();
    let original_settings =
        r#"{"defaultProvider":"codexhub-openai","enabledModels":["gpt-5.5",123]}"#;
    fs::write(&settings_path, original_settings).unwrap();
    fs::write(
            &models_path,
            r#"{"providers":{"codexhub-openai":{"baseUrl":"http://127.0.0.1:9099/v1/providers/openai","api":"openai-responses","apiKey":"codexhub-proxy","models":[{"id":"gpt-5.5"}]},"anthropic":{"models":[{"id":"claude"}]}}}"#,
        )
        .unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    let result = super::pi_ownership_bounded_cleanup(&settings_path, &models_path);

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.unwrap().applied);
    assert_eq!(
        fs::read_to_string(&settings_path).unwrap(),
        original_settings
    );
    let models: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&models_path).unwrap()).unwrap();
    assert!(models.pointer("/providers/codexhub-openai").is_none());
    assert!(models.pointer("/providers/anthropic").is_some());
}

#[test]
fn pi_cleanup_rejects_malformed_provider_entries_without_mutation() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-cleanup-provider-entries");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    fs::create_dir_all(&root).unwrap();
    let original_settings = r#"{"defaultProvider":"codexhub-openai","defaultModel":"gpt-5.5"}"#;
    fs::write(&settings_path, original_settings).unwrap();
    let original_models = r#"{"providers":{"codexhub-openai":{"models":[{"id":"gpt-5.5"}]},"unrelated":"not-an-object"}}"#;
    fs::write(&models_path, original_models).unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    let result = super::pi_ownership_bounded_cleanup(&settings_path, &models_path);

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.is_err());
    assert!(result.unwrap_err().contains("malformed entries"));
    assert_eq!(
        fs::read_to_string(&settings_path).unwrap(),
        original_settings
    );
    assert_eq!(fs::read_to_string(&models_path).unwrap(), original_models);
}

#[test]
fn opencode_cleanup_rejects_wrong_shaped_model_without_mutation() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-cleanup-model-shape");
    let config_path = root.join("opencode.json");
    fs::create_dir_all(&root).unwrap();
    let original = r#"{"model":["codexhub-openai","gpt-5.5"],"provider":{"codexhub-openai":{"baseUrl":"http://127.0.0.1:9099/v1/providers/openai","api":"openai-responses","apiKey":"codexhub-proxy"}}}"#;
    fs::write(&config_path, original).unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    let result = super::opencode_ownership_bounded_cleanup(&config_path);

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.is_err());
    assert!(result.unwrap_err().contains("unexpected shape"));
    assert_eq!(fs::read_to_string(&config_path).unwrap(), original);
}

#[test]
fn opencode_cleanup_rejects_wrong_shaped_provider_without_mutation() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-cleanup-provider-shape");
    let config_path = root.join("opencode.json");
    fs::create_dir_all(&root).unwrap();
    let original = r#"{"model":"codexhub-openai/gpt-5.5","provider":"not-an-object"}"#;
    fs::write(&config_path, original).unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    let result = super::opencode_ownership_bounded_cleanup(&config_path);

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.is_err());
    assert!(result.unwrap_err().contains("unexpected shape"));
    assert_eq!(fs::read_to_string(&config_path).unwrap(), original);
}

#[test]
fn pi_cleanup_ignores_wrong_shaped_default_provider_and_detaches_models() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-cleanup-default-provider-shape");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    fs::create_dir_all(&root).unwrap();
    let original_settings = r#"{"defaultProvider":["codexhub-openai"],"defaultModel":"gpt-5.5"}"#;
    fs::write(&settings_path, original_settings).unwrap();
    fs::write(
            &models_path,
            r#"{"providers":{"codexhub-openai":{"baseUrl":"http://127.0.0.1:9099/v1/providers/openai","api":"openai-responses","apiKey":"codexhub-proxy","models":[{"id":"gpt-5.5"}]}}}"#,
        )
        .unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    let result = super::pi_ownership_bounded_cleanup(&settings_path, &models_path);

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.unwrap().applied);
    assert_eq!(
        fs::read_to_string(&settings_path).unwrap(),
        original_settings
    );
    assert!(!models_path.exists());
}

#[test]
fn pi_cleanup_ignores_wrong_shaped_default_model_and_detaches_models() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-cleanup-default-model-shape");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    fs::create_dir_all(&root).unwrap();
    let original_settings =
        r#"{"defaultProvider":"codexhub-openai","defaultModel":{"id":"gpt-5.5"}}"#;
    fs::write(&settings_path, original_settings).unwrap();
    fs::write(
            &models_path,
            r#"{"providers":{"codexhub-openai":{"baseUrl":"http://127.0.0.1:9099/v1/providers/openai","api":"openai-responses","apiKey":"codexhub-proxy","models":[{"id":"gpt-5.5"}]}}}"#,
        )
        .unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    let result = super::pi_ownership_bounded_cleanup(&settings_path, &models_path);

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.unwrap().applied);
    assert_eq!(
        fs::read_to_string(&settings_path).unwrap(),
        original_settings
    );
    assert!(!models_path.exists());
}

#[test]
fn opencode_cleanup_write_failure_has_no_absolute_paths() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-cleanup-write-fail");
    let config_path = root.join("opencode.json");
    fs::create_dir_all(&root).unwrap();
    fs::write(
        &config_path,
        r#"{"model":"codexhub/openai/gpt-5.5","theme":"dark"}"#,
    )
    .unwrap();
    let _replacement_lock = lock_path_against_replacement(&config_path);
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    let result = super::opencode_ownership_bounded_cleanup(&config_path);

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.is_err());
    let error = result.unwrap_err();
    assert!(
        error.contains("failed to write cleaned OpenCode config"),
        "unexpected error: {error}"
    );
    assert!(!error.contains("\\") && !error.contains('/'));
}

#[test]
fn pi_cleanup_write_failure_has_no_absolute_paths() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-cleanup-write-fail");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    fs::create_dir_all(&root).unwrap();
    fs::write(
        &settings_path,
        r#"{"defaultProvider":"codexhub-openai","defaultModel":"gpt-5.5","apiKey":"user-key"}"#,
    )
    .unwrap();
    fs::write(
            &models_path,
            r#"{"providers":{"codexhub-openai":{"name":"CodexHub Gateway","models":[{"id":"gpt-5.5"}]},"anthropic":{"models":[{"id":"claude"}]}}}"#,
        )
        .unwrap();
    fs::remove_file(&models_path).unwrap();
    fs::create_dir(&models_path).unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    let result = super::pi_ownership_bounded_cleanup(&settings_path, &models_path);

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.is_err());
    let error = result.unwrap_err();
    assert!(
        error.contains("failed to read Pi models")
            || error.contains("failed to write cleaned Pi models"),
        "unexpected error: {error}"
    );
    assert!(!error.contains("\\") && !error.contains('/'));
}

#[test]
fn opencode_restore_rejects_malformed_legacy_snapshot() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-malformed-legacy");
    let config_path = root.join("opencode.json");
    let backup_root = root.join("backups");
    fs::create_dir_all(&root).unwrap();
    fs::create_dir_all(&backup_root).unwrap();
    fs::write(&config_path, r#"{"model":"codexhub/openai/gpt-5.5"}"#).unwrap();
    fs::write(backup_root.join("opencode-official.json"), "not json").unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    let result = super::restore_opencode_config_with_backup_roots(
        &config_path,
        &[stable_root(backup_root.clone())],
    );

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.is_err());
    let error = result.unwrap_err();
    assert!(
        error.contains("unexpected shape") || error.contains("malformed"),
        "unexpected error: {error}"
    );
    assert!(!error.contains("\\") && !error.contains('/'));
}

#[test]
fn pi_restore_rejects_incomplete_legacy_snapshot() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-incomplete-legacy");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    let backup_root = root.join("backups");
    let incomplete = backup_root.join("pi-official");
    fs::create_dir_all(&root).unwrap();
    fs::create_dir_all(&incomplete).unwrap();
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
        incomplete.join("settings.json"),
        r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4"}"#,
    )
    .unwrap();
    // models.json is intentionally missing.
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    let result = super::restore_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[stable_root(backup_root.clone())],
    );

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.is_err());
    let error = result.unwrap_err();
    assert!(
        error.contains("incomplete"),
        "expected incomplete snapshot error, got: {error}"
    );
    assert!(!error.contains("\\") && !error.contains('/'));
}

#[test]
fn opencode_restore_rejects_legacy_snapshot_with_malformed_provider_entries() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-malformed-provider-legacy");
    let config_path = root.join("opencode.json");
    let backup_root = root.join("backups");
    fs::create_dir_all(&root).unwrap();
    fs::create_dir_all(&backup_root).unwrap();
    fs::write(&config_path, r#"{"model":"codexhub/openai/gpt-5.5"}"#).unwrap();
    fs::write(
        backup_root.join("opencode-official.json"),
        r#"{"model":"anthropic/claude-sonnet-4","provider":{"unrelated":"not-an-object"}}"#,
    )
    .unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    let result = super::restore_opencode_config_with_backup_roots(
        &config_path,
        &[stable_root(backup_root.clone())],
    );

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.is_err());
    assert!(result.unwrap_err().contains("unexpected shape"));
    assert!(config_path.exists());
}

#[test]
fn pi_restore_rejects_legacy_snapshot_with_heterogeneous_enabled_models() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-heterogeneous-enabled-legacy");
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
            r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4","enabledModels":["claude-sonnet-4",123]}"#,
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
    );

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.is_err());
    assert!(result.unwrap_err().contains("unexpected shape"));
    assert!(settings_path.exists());
}

#[test]
fn pi_restore_rejects_legacy_snapshot_with_malformed_provider_entries() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-malformed-provider-legacy");
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
            r#"{"providers":{"anthropic":{"models":[{"id":"claude-sonnet-4"}]},"unrelated":"not-an-object"}}"#,
        )
        .unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", root.join("provenance"));

    let result = super::restore_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[stable_root(backup_root.clone())],
    );

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.is_err());
    assert!(result.unwrap_err().contains("unexpected shape"));
    assert!(settings_path.exists());
}

#[test]
fn opencode_restore_result_has_no_absolute_paths_or_contents() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-sanitized-restore");
    let config_path = root.join("opencode.json");
    let backup_root = root.join("backups");
    let provenance_dir = root.join("provenance");
    fs::create_dir_all(&root).unwrap();
    fs::create_dir_all(&backup_root).unwrap();
    fs::write(&config_path, r#"{"model":"codexhub/openai/gpt-5.5"}"#).unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", &provenance_dir);

    let result = super::restore_opencode_config_with_backup_roots(
        &config_path,
        &[stable_root(backup_root.clone())],
    )
    .unwrap();

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    let json = serde_json::to_string(&result).unwrap();
    assert!(
        !json.contains(&root.to_string_lossy().to_string()),
        "absolute path leaked: {json}"
    );
    assert!(
        !json.contains("codexhub/openai/gpt-5.5"),
        "config content leaked: {json}"
    );
    assert!(json.contains("OpenCode"));
}

#[test]
fn opencode_concurrent_legacy_adoption_yields_one_baseline() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-concurrent-legacy-adoption");
    let config_path = root.join("opencode.json");
    let stable_backups = root.join("stable-backups");
    let beta_backups = root.join("beta-backups");
    let provenance_dir = root.join("provenance");
    fs::create_dir_all(&root).unwrap();
    fs::create_dir_all(&stable_backups).unwrap();
    fs::create_dir_all(&beta_backups).unwrap();
    fs::write(&config_path, r#"{"model":"codexhub/openai/gpt-5.5"}"#).unwrap();
    let stable_file = stable_backups.join("opencode-stable.json");
    let beta_file = beta_backups.join("opencode-beta.json");
    // Baseline A from Stable, baseline B from Beta; both are complete and eligible.
    fs::write(
        &stable_file,
        r#"{"model":"anthropic/claude-sonnet-4-stable"}"#,
    )
    .unwrap();
    fs::write(&beta_file, r#"{"model":"anthropic/claude-sonnet-4-beta"}"#).unwrap();
    // Beta candidate is strictly newer, but the first publisher still wins.
    let newer = std::time::SystemTime::now() + std::time::Duration::from_secs(60);
    std::fs::OpenOptions::new()
        .write(true)
        .open(&beta_file)
        .unwrap()
        .set_times(std::fs::FileTimes::new().set_modified(newer))
        .unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", &provenance_dir);

    let (a_result, b_result) = run_overlapping_first_baseline(
        {
            let config_path = config_path.clone();
            let stable_roots = vec![stable_root(stable_backups.clone())];
            move || super::restore_opencode_config_with_backup_roots(&config_path, &stable_roots)
        },
        {
            let config_path = config_path.clone();
            let beta_roots = vec![beta_root(beta_backups.clone())];
            move || super::restore_opencode_config_with_backup_roots(&config_path, &beta_roots)
        },
    );

    let baseline = super::read_rollback_baseline("opencode").unwrap().unwrap();
    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(
        a_result.is_ok(),
        "Stable contender A must succeed: {:?}",
        a_result.err()
    );
    assert!(
        b_result.is_ok(),
        "Beta contender B must succeed: {:?}",
        b_result.err()
    );
    assert_eq!(
        baseline.files.get("opencode.json"),
        Some(&super::BaselineFile::Snapshot {
            content: r#"{"model":"anthropic/claude-sonnet-4-stable"}"#.to_string()
        }),
        "Stable baseline A must remain immutable while Beta contender B overlaps at the lock seam"
    );
}

#[test]
fn pi_concurrent_legacy_adoption_yields_one_baseline() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-concurrent-legacy-adoption");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    let stable_backups = root.join("stable-backups");
    let beta_backups = root.join("beta-backups");
    let provenance_dir = root.join("provenance");
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
    // Baseline A from Stable, baseline B from Beta; both are complete and eligible.
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
    // Beta candidate is strictly newer, but the first publisher still wins.
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
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", &provenance_dir);

    let (a_result, b_result) = run_overlapping_first_baseline(
        {
            let settings_path = settings_path.clone();
            let models_path = models_path.clone();
            let stable_roots = vec![stable_root(stable_backups.clone())];
            move || super::restore_pi_config_with_paths(&settings_path, &models_path, &stable_roots)
        },
        {
            let settings_path = settings_path.clone();
            let models_path = models_path.clone();
            let beta_roots = vec![beta_root(beta_backups.clone())];
            move || super::restore_pi_config_with_paths(&settings_path, &models_path, &beta_roots)
        },
    );

    let baseline = super::read_rollback_baseline("pi").unwrap().unwrap();
    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(
        a_result.is_ok(),
        "Stable contender A must succeed: {:?}",
        a_result.err()
    );
    assert!(
        b_result.is_ok(),
        "Beta contender B must succeed: {:?}",
        b_result.err()
    );
    assert!(
            matches!(
                baseline.files.get("settings.json"),
                Some(super::BaselineFile::Snapshot { content, .. }) if content.contains("claude-sonnet-4-stable")
            ),
            "Stable settings baseline A must remain immutable while Beta contender B overlaps at the lock seam"
        );
    assert!(
            matches!(
                baseline.files.get("models.json"),
                Some(super::BaselineFile::Snapshot { content, .. }) if content.contains("claude-sonnet-4-stable")
            ),
            "Stable models baseline A must remain immutable while Beta contender B overlaps at the lock seam"
        );
}

#[test]
fn opencode_concurrent_legacy_adoption_preserves_first_winner() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-immutability-race");
    let config_path = root.join("opencode.json");
    let stable_backups = root.join("stable-backups");
    let beta_backups = root.join("beta-backups");
    let provenance_dir = root.join("provenance");
    fs::create_dir_all(&root).unwrap();
    fs::create_dir_all(&stable_backups).unwrap();
    fs::create_dir_all(&beta_backups).unwrap();
    fs::write(&config_path, r#"{"model":"codexhub/openai/gpt-5.5"}"#).unwrap();
    let stable_file = stable_backups.join("opencode-stable.json");
    let beta_file = beta_backups.join("opencode-beta.json");
    // Baseline A from Stable, baseline B from Beta; equal mtimes so channel breaks the tie.
    fs::write(
        &stable_file,
        r#"{"model":"anthropic/claude-sonnet-4-stable"}"#,
    )
    .unwrap();
    fs::write(&beta_file, r#"{"model":"anthropic/claude-sonnet-4-beta"}"#).unwrap();
    let shared_mtime = std::time::SystemTime::now() + std::time::Duration::from_secs(60);
    for path in [&stable_file, &beta_file] {
        std::fs::OpenOptions::new()
            .write(true)
            .open(path)
            .unwrap()
            .set_times(std::fs::FileTimes::new().set_modified(shared_mtime))
            .unwrap();
    }
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", &provenance_dir);

    let (a_result, b_result) = run_overlapping_first_baseline(
        {
            let config_path = config_path.clone();
            let stable_roots = vec![stable_root(stable_backups.clone())];
            move || super::restore_opencode_config_with_backup_roots(&config_path, &stable_roots)
        },
        {
            let config_path = config_path.clone();
            let beta_roots = vec![beta_root(beta_backups.clone())];
            move || super::restore_opencode_config_with_backup_roots(&config_path, &beta_roots)
        },
    );

    let baseline = super::read_rollback_baseline("opencode").unwrap().unwrap();
    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(
        a_result.is_ok(),
        "Stable contender A must succeed: {:?}",
        a_result.err()
    );
    assert!(
        b_result.is_ok(),
        "Beta contender B must succeed: {:?}",
        b_result.err()
    );
    assert_eq!(
        baseline.files.get("opencode.json"),
        Some(&super::BaselineFile::Snapshot {
            content: r#"{"model":"anthropic/claude-sonnet-4-stable"}"#.to_string()
        }),
        "Stable baseline A must remain immutable while Beta contender B overlaps at the lock seam"
    );
}

#[test]
fn pi_concurrent_legacy_adoption_preserves_first_winner() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-immutability-race");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    let stable_backups = root.join("stable-backups");
    let beta_backups = root.join("beta-backups");
    let provenance_dir = root.join("provenance");
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
    // Baseline A from Stable, baseline B from Beta; equal mtimes so channel breaks the tie.
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
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", &provenance_dir);

    let (a_result, b_result) = run_overlapping_first_baseline(
        {
            let settings_path = settings_path.clone();
            let models_path = models_path.clone();
            let stable_roots = vec![stable_root(stable_backups.clone())];
            move || super::restore_pi_config_with_paths(&settings_path, &models_path, &stable_roots)
        },
        {
            let settings_path = settings_path.clone();
            let models_path = models_path.clone();
            let beta_roots = vec![beta_root(beta_backups.clone())];
            move || super::restore_pi_config_with_paths(&settings_path, &models_path, &beta_roots)
        },
    );

    let baseline = super::read_rollback_baseline("pi").unwrap().unwrap();
    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(
        a_result.is_ok(),
        "Stable contender A must succeed: {:?}",
        a_result.err()
    );
    assert!(
        b_result.is_ok(),
        "Beta contender B must succeed: {:?}",
        b_result.err()
    );
    assert!(
            matches!(
                baseline.files.get("settings.json"),
                Some(super::BaselineFile::Snapshot { content, .. }) if content.contains("claude-sonnet-4-stable")
            ),
            "Stable settings baseline A must remain immutable while Beta contender B overlaps at the lock seam"
        );
    assert!(
            matches!(
                baseline.files.get("models.json"),
                Some(super::BaselineFile::Snapshot { content, .. }) if content.contains("claude-sonnet-4-stable")
            ),
            "Stable models baseline A must remain immutable while Beta contender B overlaps at the lock seam"
        );
}

#[test]
fn isolated_apply_uses_default_isolated_provenance_root() {
    use super::{
        apply_gateway_client_config_isolated, validate_isolated_root, IsolatedClientApplyInput,
    };
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let root = unique_temp_dir("isolated-default-provenance");
    let isolated = validate_isolated_root(&root).unwrap();
    let settings = Settings {
        proxy_port: 9099,
        gateway_client_key: "isolated-key".to_string(),
        include_official_models: true,
        ..Settings::default()
    };
    let providers = case_sensitive_client_export_test_providers();
    let inp = IsolatedClientApplyInput {
        client_id: "opencode".to_string(),
        model: Some("volc/glm-5.2".to_string()),
        settings,
        providers,
        catalog_path: None,
        backup_subdir: None,
    };

    let result = apply_gateway_client_config_isolated(&isolated, &inp).unwrap();
    assert!(result.applied);

    let default_provenance = root
        .join("rollback-provenance")
        .join("opencode")
        .join("baseline.json");
    assert!(
        default_provenance.exists(),
        "default isolated apply must write provenance beneath the isolated root"
    );
}

#[test]
fn opencode_first_baseline_creation_is_process_atomic() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-concurrent-baseline");
    let config_path = root.join("opencode.json");
    let backup_root = root.join("backups");
    let provenance_dir = root.join("provenance");
    fs::create_dir_all(&root).unwrap();
    fs::create_dir_all(&backup_root).unwrap();
    let clean = r#"{"model":"anthropic/claude-sonnet-4"}"#;
    fs::write(&config_path, clean).unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", &provenance_dir);
    let settings = Settings::default();

    let barrier = std::sync::Arc::new(std::sync::Barrier::new(2));
    let mut handles = Vec::new();
    for model in ["openai/gpt-5.5", "openai/gpt-5.4"] {
        let config_path = config_path.clone();
        let backup_root = backup_root.clone();
        let settings = settings.clone();
        let barrier = barrier.clone();
        handles.push(std::thread::spawn(move || {
            barrier.wait();
            super::apply_opencode_config_with_paths(
                &config_path,
                &[stable_root(backup_root.clone())],
                &settings,
                &[],
                model,
            )
        }));
    }
    for handle in handles {
        handle.join().unwrap().unwrap();
    }

    let baseline = super::read_rollback_baseline("opencode").unwrap().unwrap();
    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert_eq!(
        baseline.files.get("opencode.json"),
        Some(&super::BaselineFile::Snapshot {
            content: clean.to_string()
        })
    );
    let written = fs::read_to_string(&config_path).unwrap();
    assert!(written.contains("codexhub"));
}

#[test]
fn pi_first_baseline_creation_is_process_atomic() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-concurrent-baseline");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    let backup_root = root.join("backups");
    let provenance_dir = root.join("provenance");
    fs::create_dir_all(&root).unwrap();
    fs::create_dir_all(&backup_root).unwrap();
    let clean_settings =
        r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4","theme":"dark"}"#;
    let clean_models = r#"{"providers":{"anthropic":{"models":[{"id":"claude-sonnet-4"}]}}}"#;
    fs::write(&settings_path, clean_settings).unwrap();
    fs::write(&models_path, clean_models).unwrap();
    std::env::set_var("CODEXHUB_ROLLBACK_PROVENANCE_DIR", &provenance_dir);
    let settings = Settings::default();

    let barrier = std::sync::Arc::new(std::sync::Barrier::new(2));
    let mut handles = Vec::new();
    for model in ["openai/gpt-5.5", "openai/gpt-5.4"] {
        let settings_path = settings_path.clone();
        let models_path = models_path.clone();
        let backup_root = backup_root.clone();
        let settings = settings.clone();
        let barrier = barrier.clone();
        handles.push(std::thread::spawn(move || {
            barrier.wait();
            super::apply_pi_config_with_paths(
                &settings_path,
                &models_path,
                &[stable_root(backup_root.clone())],
                &settings,
                &[],
                model,
            )
        }));
    }
    for handle in handles {
        handle.join().unwrap().unwrap();
    }

    let baseline = super::read_rollback_baseline("pi").unwrap().unwrap();
    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert_eq!(
        baseline.files.get("settings.json"),
        Some(&super::BaselineFile::Snapshot {
            content: clean_settings.to_string()
        })
    );
    assert_eq!(
        baseline.files.get("models.json"),
        Some(&super::BaselineFile::Snapshot {
            content: clean_models.to_string()
        })
    );
}

#[test]
fn opencode_baseline_write_failure_leaves_targets_intact() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-baseline-failure");
    let config_path = root.join("opencode.json");
    let backup_root = root.join("backups");
    fs::create_dir_all(&root).unwrap();
    let original = r#"{"model":"anthropic/claude-sonnet-4"}"#;
    fs::write(&config_path, original).unwrap();
    let blocker = root.join("provenance-blocker");
    fs::write(&blocker, "not a directory").unwrap();
    std::env::set_var(
        "CODEXHUB_ROLLBACK_PROVENANCE_DIR",
        blocker.join("provenance"),
    );
    let settings = Settings::default();

    let result = super::apply_opencode_config_with_paths(
        &config_path,
        &[stable_root(backup_root.clone())],
        &settings,
        &[],
        "openai/gpt-5.5",
    );

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.is_err());
    let error = result.unwrap_err();
    assert!(
        !error.contains(&root.to_string_lossy().to_string()),
        "absolute path leaked in error: {error}"
    );
    assert_eq!(fs::read_to_string(&config_path).unwrap(), original);
    assert!(!blocker.join("provenance").exists());
}

#[test]
fn pi_baseline_write_failure_leaves_targets_intact() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-pi-baseline-failure");
    let settings_path = root.join("settings.json");
    let models_path = root.join("models.json");
    let backup_root = root.join("backups");
    fs::create_dir_all(&root).unwrap();
    let original_settings = r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4"}"#;
    let original_models = r#"{"providers":{"anthropic":{"models":[{"id":"claude-sonnet-4"}]}}}"#;
    fs::write(&settings_path, original_settings).unwrap();
    fs::write(&models_path, original_models).unwrap();
    let blocker = root.join("provenance-blocker");
    fs::write(&blocker, "not a directory").unwrap();
    std::env::set_var(
        "CODEXHUB_ROLLBACK_PROVENANCE_DIR",
        blocker.join("provenance"),
    );
    let settings = Settings::default();

    let result = super::apply_pi_config_with_paths(
        &settings_path,
        &models_path,
        &[stable_root(backup_root.clone())],
        &settings,
        &[],
        "openai/gpt-5.5",
    );

    restore_env("CODEXHUB_ROLLBACK_PROVENANCE_DIR", previous_provenance);
    assert!(result.is_err());
    let error = result.unwrap_err();
    assert!(
        !error.contains(&root.to_string_lossy().to_string()),
        "absolute path leaked in error: {error}"
    );
    assert_eq!(
        fs::read_to_string(&settings_path).unwrap(),
        original_settings
    );
    assert_eq!(fs::read_to_string(&models_path).unwrap(), original_models);
    assert!(!blocker.join("provenance").exists());
}

#[test]
fn opencode_restore_empty_legacy_roots_classifies_bounded_cleanup() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous_provenance = std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR");
    let root = unique_temp_dir("codexhub-opencode-cleanup-classification");
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
    assert_eq!(result.message, "OpenCode CodexHub config removed.");
    assert!(!result.message.contains("\\") && !result.message.contains('/'));
    assert!(!result.message.contains("codexhub-openai"));
}

#[test]
fn omp_apply_writes_models_yml_and_model_roles_with_backup() {
    let root = unique_temp_dir("codexhub-omp");
    let config_path = root.join("config.yml");
    let models_path = root.join("models.yml");
    let backup_root = root.join("backups");
    fs::create_dir_all(root.as_path()).unwrap();
    fs::write(
            &config_path,
            "symbolPreset: unicode\ntheme:\n  dark: titanium\n  light: light\nmodelRoles:\n  default: ollama/qwen\n  vision: ollama/qwen-vision\n",
        )
        .unwrap();
    fs::write(
            &models_path,
            "providers:\n  ollama:\n    baseUrl: http://localhost:11434/v1\n    api: openai-completions\n    apiKey: ollama\n    models:\n      - id: qwen\n",
        )
        .unwrap();
    let settings = Settings::default();

    let result = super::apply_omp_config_with_paths(
        &config_path,
        &models_path,
        &backup_root,
        &settings,
        &[],
        "openai/gpt-5.5",
    )
    .unwrap();

    assert!(result.applied);
    let backup_path = result.backup_path.unwrap();
    assert!(backup_path.join("config.yml").exists());
    assert!(backup_path.join("models.yml").exists());
    let config = fs::read_to_string(&config_path).unwrap();
    assert!(config.contains("symbolPreset: unicode"));
    assert!(config.contains("modelRoles:\n  default: codexhub-openai/gpt-5.5"));
    assert!(config.contains("  vision: codexhub-openai/gpt-5.5"));
    let models = fs::read_to_string(&models_path).unwrap();
    assert!(models.contains("providers:\n  codexhub-openai:"));
    assert!(models.contains("baseUrl: \"http://127.0.0.1:9099/v1/providers/openai\""));
    assert!(models.contains("api: openai-responses"));
    assert!(models.contains("apiKey: \"codexhub-proxy\""));
    assert!(models.contains("id: \"gpt-5.5\""));
    assert!(models.contains("id: \"gpt-5.5-fast\""));
}

#[test]
fn omp_apply_rejects_invalid_model_before_backup_side_effects() {
    let root = unique_temp_dir("codexhub-omp-invalid-model");
    let config_path = root.join("config.yml");
    let models_path = root.join("models.yml");
    let backup_root = root.join("backups");
    fs::create_dir_all(root.as_path()).unwrap();
    fs::write(
        &config_path,
        "modelRoles:\n  default: anthropic/claude-sonnet-4\n",
    )
    .unwrap();
    fs::write(
        &models_path,
        "providers:\n  anthropic:\n    models:\n      - id: claude-sonnet-4\n",
    )
    .unwrap();
    let original_config = fs::read_to_string(&config_path).unwrap();
    let original_models = fs::read_to_string(&models_path).unwrap();
    let settings = Settings::default();
    let providers = case_sensitive_client_export_test_providers();

    let error = super::apply_omp_config_with_paths(
        &config_path,
        &models_path,
        &backup_root,
        &settings,
        &providers,
        "minimax-cn/MINIMAX-M3",
    )
    .unwrap_err();

    assert!(error.contains("Gateway model is not exported: minimax-cn/MINIMAX-M3"));
    assert!(!backup_root.exists());
    assert_eq!(fs::read_to_string(&config_path).unwrap(), original_config);
    assert_eq!(fs::read_to_string(&models_path).unwrap(), original_models);
}

#[test]
fn omp_route_mode_detects_split_provider_from_config_without_models_file() {
    let root = unique_temp_dir("codexhub-omp-route-mode");
    let config_path = root.join("config.yml");
    let models_path = root.join("models.yml");
    fs::create_dir_all(root.as_path()).unwrap();
    fs::write(
        &config_path,
        "modelRoles:\n  default: codexhub-openai/gpt-5.5\n  vision: codexhub-openai/gpt-5.5\n",
    )
    .unwrap();
    let paths = super::OmpConfigPaths {
        config_path,
        models_path,
    };

    assert_eq!(super::omp_route_mode(&paths), "hub");
}

#[test]
fn zcode_apply_writes_user_catalog_with_schema_safe_provider() {
    let root = unique_temp_dir("codexhub-zcode");
    let catalog_path = root.join("model-providers").join("codexhub.json");
    let v2_config_path = root.join("v2").join("config.json");
    let v2_cache_path = root.join("v2").join("bots-model-cache.v2.json");
    let targets = super::ZcodeConfigTargets {
        catalog_path: catalog_path.clone(),
        v2_config_path: v2_config_path.clone(),
        v2_cache_path: v2_cache_path.clone(),
    };
    let backup_root = root.join("backups");
    let settings = Settings::default();

    let result = super::apply_zcode_config_with_targets(
        &targets,
        &backup_root,
        &settings,
        &[],
        "openai/gpt-5.5",
    )
    .unwrap();

    assert!(result.applied);
    assert!(result.backup_path.is_none());
    assert_eq!(
        result.config_path.as_deref(),
        Some(v2_config_path.as_path())
    );
    let catalog: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&catalog_path).unwrap()).unwrap();
    assert_eq!(
        catalog
            .get("schemaVersion")
            .and_then(serde_json::Value::as_str),
        Some("zcode.model-providers.v2")
    );
    let provider = catalog.pointer("/providers/0").unwrap();
    assert_eq!(
        provider.get("id").and_then(serde_json::Value::as_str),
        Some("codexhub-openai")
    );
    assert_eq!(
        provider.get("source").and_then(serde_json::Value::as_str),
        Some("custom")
    );
    assert_eq!(
        provider.get("apiKey").and_then(serde_json::Value::as_str),
        Some("codexhub-proxy")
    );
    assert_eq!(
        provider
            .pointer("/endpoints/baseURL")
            .and_then(serde_json::Value::as_str),
        Some("http://127.0.0.1:9099")
    );
    assert_eq!(
        provider
            .get("apiFormat")
            .and_then(serde_json::Value::as_str),
        Some("openai-responses")
    );
    assert_eq!(
        provider
            .pointer("/endpoints/paths/openai")
            .and_then(serde_json::Value::as_str),
        Some("/v1/providers/openai/responses")
    );
    assert_eq!(
        provider
            .pointer("/models/0/id")
            .and_then(serde_json::Value::as_str),
        Some("gpt-5.5")
    );
    assert_eq!(
        provider
            .pointer("/models/0/defaultKind")
            .and_then(serde_json::Value::as_str),
        Some("openai")
    );
    assert!(!fs::read_to_string(&catalog_path)
        .unwrap()
        .contains("codexhub_managed"));
    let v2_config: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&v2_config_path).unwrap()).unwrap();
    assert_eq!(
        v2_config
            .pointer("/provider/codexhub-openai/options/baseURL")
            .and_then(serde_json::Value::as_str),
        Some("http://127.0.0.1:9099/v1/providers/openai")
    );
    assert_eq!(
        v2_config
            .pointer("/provider/codexhub-openai/kind")
            .and_then(serde_json::Value::as_str),
        Some("openai")
    );
    assert_eq!(
        v2_config
            .pointer("/provider/codexhub-openai/apiFormat")
            .and_then(serde_json::Value::as_str),
        Some("openai-responses")
    );
    assert_eq!(
        v2_config
            .pointer("/provider/codexhub-openai/endpoints/baseURL")
            .and_then(serde_json::Value::as_str),
        Some("http://127.0.0.1:9099/v1/providers/openai")
    );
    assert_eq!(
        v2_config
            .pointer("/provider/codexhub-openai/endpoints/paths/openai")
            .and_then(serde_json::Value::as_str),
        Some("/responses")
    );
    assert!(v2_config
        .pointer("/provider/codexhub-openai/models/gpt-5.5")
        .is_some());
    assert!(v2_config
        .pointer("/provider/codexhub-openai/models/gpt-5.5-fast")
        .is_some());
    assert!(v2_config
        .pointer("/provider/codexhub-openai/models/gpt-5.4-fast")
        .is_some());
    let v2_cache: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&v2_cache_path).unwrap()).unwrap();
    let cache_provider = v2_cache.pointer("/providers/0").unwrap();
    let cache_models = cache_provider["models"].as_array().unwrap();
    assert!(cache_models
        .iter()
        .any(|model| model["id"] == "gpt-5.5-fast"));
    assert!(cache_models
        .iter()
        .any(|model| model["id"] == "gpt-5.4-fast"));
    assert_eq!(
        cache_provider
            .pointer("/endpoints/baseURL")
            .and_then(serde_json::Value::as_str),
        Some("http://127.0.0.1:9099/v1/providers/openai")
    );
    assert_eq!(
        cache_provider
            .pointer("/endpoints/paths/openai")
            .and_then(serde_json::Value::as_str),
        Some("/responses")
    );
}

#[test]
fn zcode_v2_config_preserves_active_config_with_codexhub_provider() {
    let root = unique_temp_dir("codexhub-zcode-v2-config");
    let config_path = root.join("config.json");
    fs::create_dir_all(root.as_path()).unwrap();
    fs::write(
            &config_path,
            r#"{"provider":{"builtin:test":{"name":"Existing","kind":"openai-compatible","options":{"baseURL":"https://example.test"},"models":{}},"codexhub-old":{"name":"CodexHub Gateway","kind":"openai-compatible","options":{"baseURL":"http://127.0.0.1:9099/v1"},"models":{}}}}"#,
        )
        .unwrap();
    let settings = Settings::default();
    let mut providers = case_sensitive_client_export_test_providers();
    providers[0].upstream_format = Some(UpstreamFormat::Responses);

    let text =
        super::zcode_v2_config_text(&config_path, &settings, &providers, "ollama-cloud/glm-5.2")
            .unwrap();
    let value: serde_json::Value = serde_json::from_str(&text).unwrap();
    let provider = value.pointer("/provider/codexhub-ollama-cloud").unwrap();

    assert!(value.pointer("/provider/builtin:test").is_some());
    assert!(value.pointer("/provider/codexhub-old").is_none());
    assert_eq!(
        provider.get("name").and_then(serde_json::Value::as_str),
        Some("CodexHub Ollama Cloud")
    );
    assert_eq!(
        provider.get("kind").and_then(serde_json::Value::as_str),
        Some("openai")
    );
    assert_eq!(
        provider
            .pointer("/options/baseURL")
            .and_then(serde_json::Value::as_str),
        Some("http://127.0.0.1:9099/v1/providers/ollama-cloud")
    );
    assert_eq!(
        provider
            .get("apiFormat")
            .and_then(serde_json::Value::as_str),
        Some("openai-responses")
    );
    assert_eq!(
        provider
            .pointer("/endpoints/baseURL")
            .and_then(serde_json::Value::as_str),
        Some("http://127.0.0.1:9099/v1/providers/ollama-cloud")
    );
    assert_eq!(
        provider
            .pointer("/endpoints/paths/openai")
            .and_then(serde_json::Value::as_str),
        Some("/responses")
    );
    assert_eq!(
        provider
            .pointer("/models/glm-5.2/limit/context")
            .and_then(serde_json::Value::as_u64),
        Some(131_072)
    );
    assert_eq!(
        value
            .pointer("/provider/codexhub-volc/models/glm-5.2/limit/context")
            .and_then(serde_json::Value::as_u64),
        Some(1_024_000)
    );
}

#[test]
fn zcode_apply_preserves_existing_official_v2_providers() {
    let root = unique_temp_dir("codexhub-zcode-preserve-official");
    let catalog_path = root.join("model-providers").join("codexhub.json");
    let v2_config_path = root.join("v2").join("config.json");
    let v2_cache_path = root.join("v2").join("bots-model-cache.v2.json");
    let targets = super::ZcodeConfigTargets {
        catalog_path,
        v2_config_path: v2_config_path.clone(),
        v2_cache_path,
    };
    let backup_root = root.join("backups");
    fs::create_dir_all(v2_config_path.parent().unwrap()).unwrap();
    fs::write(
            &v2_config_path,
            r#"{"provider":{"builtin:bigmodel-coding-plan":{"name":"Bigmodel - Coding Plan","kind":"anthropic","source":"custom","models":{"GLM-5.2":{"name":"GLM-5.2"}}}}}"#,
        )
        .unwrap();
    let settings = Settings::default();
    let providers = case_sensitive_client_export_test_providers();

    let result = super::apply_zcode_config_with_targets(
        &targets,
        &backup_root,
        &settings,
        &providers,
        "ollama-cloud/glm-5.2",
    )
    .unwrap();

    assert!(result.applied);
    assert!(result.backup_path.is_some());
    let value: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&v2_config_path).unwrap()).unwrap();
    assert!(value
        .pointer("/provider/builtin:bigmodel-coding-plan")
        .is_some());
    assert!(value.pointer("/provider/codexhub-ollama-cloud").is_some());
}

#[test]
fn provider_scoped_gateway_urls_percent_encode_path_segments() {
    let settings = Settings::default();
    let provider_id = "odd/provider?x#frag %";

    assert_eq!(
        super::gateway_client_provider_base_url(&settings, provider_id),
        "http://127.0.0.1:9099/v1/providers/odd%2Fprovider%3Fx%23frag%20%25"
    );
    assert_eq!(
        super::gateway_client_provider_chat_path(provider_id),
        "/v1/providers/odd%2Fprovider%3Fx%23frag%20%25/chat/completions"
    );
}

#[test]
fn local_gateway_owner_detects_release_and_beta_ports() {
    assert_eq!(
        super::routing_owner_from_gateway_url("http://127.0.0.1:9099/v1"),
        crate::app_flavor::RoutingOwner::Release
    );
    assert_eq!(
        super::routing_owner_from_gateway_url("http://127.0.0.1:9109/v1"),
        crate::app_flavor::RoutingOwner::Beta
    );
    assert_eq!(
        super::routing_owner_from_gateway_url("https://api.openai.com/v1"),
        crate::app_flavor::RoutingOwner::UnknownExternal
    );
}

#[test]
fn public_apply_rejects_other_channel_managed_config_without_takeover() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous = std::env::var_os("CODEXHUB_OPENCODE_CONFIG");
    let root = unique_temp_dir("codexhub-opencode-public-apply-owner");
    let config_path = root.join("opencode.json");
    write_beta_owned_opencode_config(&config_path);
    std::env::set_var("CODEXHUB_OPENCODE_CONFIG", &config_path);

    let error = super::apply_gateway_client_config(
        "opencode".to_string(),
        Some("openai/gpt-5.5".to_string()),
    )
    .expect_err("public apply must not overwrite beta-owned config");

    restore_env("CODEXHUB_OPENCODE_CONFIG", previous);
    assert!(error.contains("Managed by Beta"));
}

#[test]
fn public_restore_rejects_other_channel_managed_config_without_takeover() {
    let _guard = TEST_ENV_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let previous = std::env::var_os("CODEXHUB_OPENCODE_CONFIG");
    let root = unique_temp_dir("codexhub-opencode-public-restore-owner");
    let config_path = root.join("opencode.json");
    write_beta_owned_opencode_config(&config_path);
    std::env::set_var("CODEXHUB_OPENCODE_CONFIG", &config_path);

    let error = super::restore_gateway_client_config("opencode".to_string())
        .expect_err("public restore must not disconnect beta-owned config");

    restore_env("CODEXHUB_OPENCODE_CONFIG", previous);
    assert!(error.contains("Managed by Beta"));
}

#[test]
fn foreign_owner_restore_uses_the_owning_channel_backup_root() {
    let user_home = dirs::home_dir().expect("user home");
    let foreign_owner = match crate::app_flavor::current().routing_owner() {
        crate::app_flavor::RoutingOwner::Release => crate::app_flavor::RoutingOwner::Beta,
        _ => crate::app_flavor::RoutingOwner::Release,
    };
    let foreign_flavor = match foreign_owner {
        crate::app_flavor::RoutingOwner::Release => crate::app_flavor::RuntimeFlavor::Stable,
        _ => crate::app_flavor::RuntimeFlavor::Beta,
    };
    let expected_runtime =
        crate::runtime_paths::homes_for_flavor(&user_home, foreign_flavor).runtime;

    assert_eq!(
        super::client_backup_root_for_owner("opencode", foreign_owner),
        expected_runtime
            .join("proxy")
            .join("client-backups")
            .join("opencode")
    );
}

#[test]
fn managed_route_owner_scans_later_provider_entries() {
    let text = r#"{
  "provider": {
    "aaa-provider": {
      "options": {
        "baseURL": "https://api.openai.com/v1"
      }
    },
    "codexhub-openai": {
      "name": "CodexHub OpenAI",
      "options": {
        "baseURL": "http://127.0.0.1:9109/v1/providers/openai"
      }
    }
  }
}"#;

    let (owner, endpoint) = super::detect_route_details_from_json_provider_object(
        text,
        "/provider",
        true,
        true,
        crate::app_flavor::RoutingOwner::Release,
        9099,
    );

    assert_eq!(owner, crate::app_flavor::RoutingOwner::Beta);
    assert_eq!(
        endpoint.as_deref(),
        Some("http://127.0.0.1:9109/v1/providers/openai")
    );
}

#[test]
fn zcode_route_mode_prefers_v2_config_over_stale_catalog() {
    let root = unique_temp_dir("codexhub-zcode-route-mode");
    let catalog_path = root.join("model-providers").join("codexhub.json");
    let v2_config_path = root.join("v2").join("config.json");
    let targets = super::ZcodeConfigTargets {
        catalog_path: catalog_path.clone(),
        v2_config_path: v2_config_path.clone(),
        v2_cache_path: root.join("v2").join("bots-model-cache.v2.json"),
    };
    fs::create_dir_all(catalog_path.parent().unwrap()).unwrap();
    fs::create_dir_all(v2_config_path.parent().unwrap()).unwrap();
    fs::write(
        &catalog_path,
        r#"{"schemaVersion":"zcode.model-providers.v2","providers":[{"id":"codexhub-openai"}]}"#,
    )
    .unwrap();
    fs::write(
        &v2_config_path,
        r#"{"provider":{"builtin:test":{"name":"Existing","models":{}}}}"#,
    )
    .unwrap();

    assert_eq!(super::zcode_route_mode(&targets), "official");

    fs::write(
        &v2_config_path,
        r#"{"provider":{"codexhub-openai":{"name":"CodexHub OpenAI","models":{}}}}"#,
    )
    .unwrap();

    assert_eq!(super::zcode_route_mode(&targets), "hub");
}

#[test]
fn zcode_route_mode_marks_protocol_mismatch_as_stale() {
    let root = unique_temp_dir("codexhub-zcode-stale-protocol");
    let catalog_path = root.join("model-providers").join("codexhub.json");
    let v2_config_path = root.join("v2").join("config.json");
    let v2_cache_path = root.join("v2").join("bots-model-cache.v2.json");
    let targets = super::ZcodeConfigTargets {
        catalog_path: catalog_path.clone(),
        v2_config_path: v2_config_path.clone(),
        v2_cache_path: v2_cache_path.clone(),
    };
    fs::create_dir_all(catalog_path.parent().unwrap()).unwrap();
    fs::create_dir_all(v2_config_path.parent().unwrap()).unwrap();
    let settings = Settings::default();
    let mut providers = case_sensitive_client_export_test_providers();
    providers[0].upstream_format = Some(UpstreamFormat::Responses);
    let model = "ollama-cloud/glm-5.2";
    let expected_config =
        super::zcode_v2_config_text(&v2_config_path, &settings, &providers, model).unwrap();
    let expected_catalog = super::zcode_catalog_text(&settings, &providers, model).unwrap();
    let expected_cache = super::zcode_v2_cache_text(&settings, &providers, model).unwrap();

    fs::write(&catalog_path, &expected_catalog).unwrap();
    fs::write(&v2_config_path, &expected_config).unwrap();
    fs::write(&v2_cache_path, &expected_cache).unwrap();
    assert_eq!(
        super::zcode_route_mode_with_expected(&targets, &settings, &providers, model),
        "hub"
    );

    fs::write(
        &v2_config_path,
        expected_config.replacen("\"kind\": \"openai\"", "\"kind\": \"openai-compatible\"", 1),
    )
    .unwrap();
    assert_eq!(
        super::zcode_route_mode_with_expected(&targets, &settings, &providers, model),
        "stale"
    );

    fs::write(&v2_config_path, &expected_config).unwrap();
    fs::write(
        &v2_cache_path,
        expected_cache.replacen(
            "\"apiFormat\": \"openai-responses\"",
            "\"apiFormat\": \"openai-chat-completions\"",
            1,
        ),
    )
    .unwrap();
    assert_eq!(
        super::zcode_route_mode_with_expected(&targets, &settings, &providers, model),
        "stale"
    );
}

#[test]
fn zcode_route_mode_accepts_zcode_normalized_v2_provider_config() {
    let root = unique_temp_dir("codexhub-zcode-normalized-v2");
    let catalog_path = root.join("model-providers").join("codexhub.json");
    let v2_config_path = root.join("v2").join("config.json");
    let v2_cache_path = root.join("v2").join("bots-model-cache.v2.json");
    let targets = super::ZcodeConfigTargets {
        catalog_path: catalog_path.clone(),
        v2_config_path: v2_config_path.clone(),
        v2_cache_path: v2_cache_path.clone(),
    };
    fs::create_dir_all(catalog_path.parent().unwrap()).unwrap();
    fs::create_dir_all(v2_config_path.parent().unwrap()).unwrap();
    let settings = Settings::default();
    let providers = case_sensitive_client_export_test_providers();
    let model = "ollama-cloud/glm-5.2";
    let expected_config =
        super::zcode_v2_config_text(&v2_config_path, &settings, &providers, model).unwrap();
    let expected_catalog = super::zcode_catalog_text(&settings, &providers, model).unwrap();
    let expected_cache = super::zcode_v2_cache_text(&settings, &providers, model).unwrap();
    let mut normalized_config: serde_json::Value = serde_json::from_str(&expected_config).unwrap();
    for provider in normalized_config
        .get_mut("provider")
        .and_then(serde_json::Value::as_object_mut)
        .unwrap()
        .values_mut()
    {
        provider.as_object_mut().unwrap().remove("apiFormat");
        provider.as_object_mut().unwrap().remove("endpoints");
    }

    fs::write(&catalog_path, expected_catalog).unwrap();
    fs::write(
        &v2_config_path,
        serde_json::to_string_pretty(&normalized_config).unwrap(),
    )
    .unwrap();
    fs::write(&v2_cache_path, expected_cache).unwrap();

    assert_eq!(
        super::zcode_route_mode_with_expected(&targets, &settings, &providers, model),
        "hub"
    );
}

#[test]
fn zcode_catalog_override_derives_v2_root_from_same_profile() {
    let root = unique_temp_dir("codexhub-zcode-profile-root");
    let catalog_path = root.join("model-providers").join("codexhub.json");

    assert_eq!(
        super::zcode_v2_root_from_catalog_path(&catalog_path),
        Some(root.join("v2"))
    );
}

#[test]
fn zcode_data_base_dir_derives_active_v2_root() {
    let root = unique_temp_dir("codexhub-zcode-data-root");
    let settings_path = root.join(".zcode").join("v2").join("setting.json");
    let data_base_dir = root.join("external-data");
    fs::create_dir_all(settings_path.parent().unwrap()).unwrap();
    fs::write(
        &settings_path,
        json!({ "dataBaseDir": data_base_dir.to_string_lossy() }).to_string(),
    )
    .unwrap();

    assert_eq!(
        super::zcode_v2_root_from_settings_path(&settings_path),
        Some(data_base_dir.join(".zcode").join("v2"))
    );
}

#[test]
fn zcode_apply_rejects_invalid_model_before_backup_side_effects() {
    let root = unique_temp_dir("codexhub-zcode-invalid-model");
    let catalog_path = root.join("model-providers").join("codexhub.json");
    let v2_config_path = root.join("v2").join("config.json");
    let v2_cache_path = root.join("v2").join("bots-model-cache.v2.json");
    let targets = super::ZcodeConfigTargets {
        catalog_path: catalog_path.clone(),
        v2_config_path: v2_config_path.clone(),
        v2_cache_path: v2_cache_path.clone(),
    };
    let backup_root = root.join("backups");
    fs::create_dir_all(catalog_path.parent().unwrap()).unwrap();
    fs::create_dir_all(v2_config_path.parent().unwrap()).unwrap();
    fs::write(
        &catalog_path,
        r#"{"schemaVersion":"zcode.model-providers.v2","providers":[]}"#,
    )
    .unwrap();
    fs::write(
        &v2_config_path,
        r#"{"provider":{"builtin:test":{"name":"Existing","models":{}}}}"#,
    )
    .unwrap();
    let original = fs::read_to_string(&catalog_path).unwrap();
    let original_v2_config = fs::read_to_string(&v2_config_path).unwrap();
    let settings = Settings::default();
    let providers = case_sensitive_client_export_test_providers();

    let error = super::apply_zcode_config_with_targets(
        &targets,
        &backup_root,
        &settings,
        &providers,
        "minimax-cn/MINIMAX-M3",
    )
    .unwrap_err();

    assert!(error.contains("Gateway model is not exported: minimax-cn/MINIMAX-M3"));
    assert!(!backup_root.exists());
    assert_eq!(fs::read_to_string(&catalog_path).unwrap(), original);
    assert_eq!(
        fs::read_to_string(&v2_config_path).unwrap(),
        original_v2_config
    );
    assert!(!v2_cache_path.exists());
}

#[test]
fn zcode_restore_without_backup_removes_managed_v2_provider_only() {
    let root = unique_temp_dir("codexhub-zcode-restore-managed");
    let catalog_path = root.join("model-providers").join("codexhub.json");
    let v2_config_path = root.join("v2").join("config.json");
    let v2_cache_path = root.join("v2").join("bots-model-cache.v2.json");
    let targets = super::ZcodeConfigTargets {
        catalog_path: catalog_path.clone(),
        v2_config_path: v2_config_path.clone(),
        v2_cache_path: v2_cache_path.clone(),
    };
    fs::create_dir_all(catalog_path.parent().unwrap()).unwrap();
    fs::create_dir_all(v2_config_path.parent().unwrap()).unwrap();
    fs::write(
        &catalog_path,
        r#"{"schemaVersion":"zcode.model-providers.v2","providers":[{"id":"codexhub-openai"}]}"#,
    )
    .unwrap();
    fs::write(
            &v2_config_path,
            r#"{"provider":{"builtin:test":{"name":"Existing","models":{}},"codexhub-labs":{"name":"CodexHub Labs","options":{"baseURL":"https://labs.example.test/v1"},"models":{}},"codexhub-openai":{"name":"CodexHub OpenAI","options":{"baseURL":"http://127.0.0.1:9099/v1/providers/openai"},"models":{}},"codexhub-volc":{"name":"CodexHub Volcengine","options":{"baseURL":"http://127.0.0.1:9099/v1/providers/volc"},"models":{}}}}"#,
        )
        .unwrap();
    fs::write(
        &v2_cache_path,
        r#"{"schemaVersion":"zcode.model-providers.v2","providers":[{"id":"codexhub-openai"}]}"#,
    )
    .unwrap();

    let result = super::restore_zcode_config_with_targets(&targets, &root.join("backups")).unwrap();

    assert!(result.applied);
    assert!(result.backup_path.is_none());
    assert!(!catalog_path.exists());
    assert!(!v2_cache_path.exists());
    let value: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&v2_config_path).unwrap()).unwrap();
    assert!(value.pointer("/provider/builtin:test").is_some());
    assert!(value.pointer("/provider/codexhub-labs").is_some());
    assert!(value.pointer("/provider/codexhub-openai").is_none());
    assert!(value.pointer("/provider/codexhub-volc").is_none());
}
