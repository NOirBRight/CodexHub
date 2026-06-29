use crate::Model;
use reqwest::blocking::Client;
use reqwest::header::{ACCEPT, AUTHORIZATION};
use serde_json::Value;
use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

const OFFICIAL_MODELS_URL: &str = "https://api.openai.com/v1/models";
const DISCOVERY_TIMEOUT: Duration = Duration::from_secs(20);
const GENERATED_CATALOG_FILE: &str = "codex-proxy-official-ollama.json";

pub fn refresh_official_models() -> Result<Vec<Model>, String> {
    let api_key = std::env::var("OPENAI_API_KEY")
        .map_err(|_| "OPENAI_API_KEY is required to refresh official OpenAI models".to_string())?;
    let api_key = api_key.trim();
    if api_key.is_empty() {
        return Err("OPENAI_API_KEY is required to refresh official OpenAI models".to_string());
    }

    refresh_official_models_from_endpoint(OFFICIAL_MODELS_URL, api_key, DISCOVERY_TIMEOUT)
}

pub fn discover_provider_models(base_url: &str, api_key: &str) -> Result<Vec<Model>, String> {
    discover_provider_models_with_timeout(base_url, api_key, DISCOVERY_TIMEOUT)
}

pub fn generate_catalog() -> Result<Vec<Model>, String> {
    let paths = ModelPaths::runtime()?;
    let python = find_python();
    let runner = ProcessCatalogSyncRunner;

    generate_catalog_with_runner(&paths, &python, &runner)
}

pub fn list_models() -> Result<Vec<Model>, String> {
    let paths = ModelPaths::runtime()?;
    let catalog_path = paths.generated_catalog_path();
    if !catalog_path.exists() {
        return Ok(Vec::new());
    }

    read_catalog_models(&catalog_path)
}

fn refresh_official_models_from_endpoint(
    endpoint: &str,
    api_key: &str,
    timeout: Duration,
) -> Result<Vec<Model>, String> {
    discover_models_http(
        endpoint,
        api_key,
        timeout,
        DiscoveryKind::Official,
        "official OpenAI models",
    )
}

fn discover_provider_models_with_timeout(
    base_url: &str,
    api_key: &str,
    timeout: Duration,
) -> Result<Vec<Model>, String> {
    let endpoint = provider_models_endpoint(base_url)?;
    discover_models_http(
        &endpoint,
        api_key,
        timeout,
        DiscoveryKind::Provider,
        "provider models",
    )
}

fn discover_models_http(
    endpoint: &str,
    api_key: &str,
    timeout: Duration,
    kind: DiscoveryKind,
    label: &str,
) -> Result<Vec<Model>, String> {
    let client = Client::builder()
        .timeout(timeout)
        .build()
        .map_err(|error| format!("failed to build HTTP client for {label}: {error}"))?;
    let mut request = client.get(endpoint).header(ACCEPT, "application/json");
    let api_key = api_key.trim();
    if !api_key.is_empty() {
        request = request.header(AUTHORIZATION, format!("Bearer {api_key}"));
    }

    let response = request.send().map_err(|error| {
        format!(
            "{label} discovery request failed: {}",
            safe_http_error(error)
        )
    })?;
    let status = response.status();
    if !status.is_success() {
        return Err(format!(
            "{label} discovery request failed with HTTP status {status}"
        ));
    }

    let payload = response.json::<Value>().map_err(|error| {
        format!(
            "{label} discovery response was not valid JSON: {}",
            safe_http_error(error)
        )
    })?;

    Ok(parse_discovered_models(&payload, kind))
}

fn provider_models_endpoint(base_url: &str) -> Result<String, String> {
    let base_url = base_url.trim();
    if base_url.is_empty() {
        return Err("provider base_url is required for model discovery".to_string());
    }

    let base_url = base_url.trim_end_matches('/');
    if base_url.ends_with("/v1") {
        Ok(format!("{base_url}/models"))
    } else {
        Ok(format!("{base_url}/v1/models"))
    }
}

fn safe_http_error(error: reqwest::Error) -> String {
    error.without_url().to_string()
}

#[derive(Debug, Clone, Copy)]
enum DiscoveryKind {
    Official,
    Provider,
}

fn parse_discovered_models(payload: &Value, kind: DiscoveryKind) -> Vec<Model> {
    let mut models = Vec::new();
    let mut seen = HashSet::new();

    for item in payload_model_items(payload) {
        let Some(id) = discovered_model_id(item) else {
            continue;
        };
        if matches!(kind, DiscoveryKind::Official) && !id.starts_with("gpt-") {
            continue;
        }
        if !seen.insert(id.clone()) {
            continue;
        }

        models.push(model_from_discovered_item(id, item));
    }

    if matches!(kind, DiscoveryKind::Official) {
        models.sort_by(|left, right| left.id.cmp(&right.id));
    }

    models
}

fn payload_model_items(payload: &Value) -> Vec<&Value> {
    if let Some(items) = payload.as_array() {
        return items.iter().collect();
    }

    if let Some(document) = payload.as_object() {
        if let Some(items) = document.get("data").and_then(Value::as_array) {
            return items.iter().collect();
        }
        if let Some(items) = document.get("models").and_then(Value::as_array) {
            return items.iter().collect();
        }
    }

    Vec::new()
}

fn discovered_model_id(value: &Value) -> Option<String> {
    if let Some(text) = value.as_str() {
        return nonblank(text);
    }

    let object = value.as_object()?;
    for key in ["id", "model", "name", "slug"] {
        if let Some(text) = object.get(key).and_then(Value::as_str) {
            if let Some(model_id) = nonblank(text) {
                return Some(model_id);
            }
        }
    }

    None
}

fn model_from_discovered_item(id: String, item: &Value) -> Model {
    Model {
        id,
        display_name: None,
        upstream_model: None,
        context_window: numeric_limit(
            item,
            &["context_window", "max_context_window", "context_length"],
            "context",
        ),
        max_output_tokens: numeric_limit(item, &["max_output_tokens", "output_tokens"], "output"),
        sort_order: None,
        enabled: true,
    }
}

fn numeric_limit(item: &Value, keys: &[&str], nested_limit_key: &str) -> Option<u32> {
    let object = item.as_object()?;
    for key in keys {
        if let Some(value) = object.get(*key).and_then(optional_u32) {
            return Some(value);
        }
    }

    object
        .get("limit")
        .and_then(Value::as_object)
        .and_then(|limit| limit.get(nested_limit_key))
        .and_then(optional_u32)
}

fn optional_u32(value: &Value) -> Option<u32> {
    if let Some(value) = value.as_u64() {
        return u32::try_from(value).ok();
    }
    value.as_str().and_then(|text| text.trim().parse().ok())
}

fn optional_i32(value: &Value) -> Option<i32> {
    if let Some(value) = value.as_i64() {
        return i32::try_from(value).ok();
    }
    value.as_str().and_then(|text| text.trim().parse().ok())
}

fn nonblank(value: &str) -> Option<String> {
    let value = value.trim();
    if value.is_empty() {
        None
    } else {
        Some(value.to_string())
    }
}

#[derive(Debug, Clone)]
struct ModelPaths {
    codex_dir: PathBuf,
    repo_root: PathBuf,
}

impl ModelPaths {
    fn runtime() -> Result<Self, String> {
        let codex_dir = match std::env::var_os("CODEX_HOME").filter(|value| !value.is_empty()) {
            Some(value) => PathBuf::from(value),
            None => dirs::home_dir()
                .ok_or_else(|| "failed to resolve user home directory".to_string())?
                .join(".codex"),
        };
        let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .ok_or_else(|| "failed to resolve CodexHub repo root".to_string())?
            .to_path_buf();

        Ok(Self::new(codex_dir, repo_root))
    }

    fn new(codex_dir: impl Into<PathBuf>, repo_root: impl Into<PathBuf>) -> Self {
        Self {
            codex_dir: codex_dir.into(),
            repo_root: repo_root.into(),
        }
    }

    fn catalog_sync_script(&self) -> PathBuf {
        self.repo_root.join("src-python").join("catalog_sync.py")
    }

    fn generated_catalog_path(&self) -> PathBuf {
        self.codex_dir
            .join("model-catalogs")
            .join(GENERATED_CATALOG_FILE)
    }
}

#[derive(Debug, Clone)]
struct CatalogCommandOutcome {
    code: Option<i32>,
    stdout: String,
    stderr: String,
}

trait CatalogSyncRunner {
    fn run_sync(
        &self,
        python: &Path,
        script: &Path,
        codex_dir: &Path,
    ) -> Result<CatalogCommandOutcome, String>;
}

struct ProcessCatalogSyncRunner;

impl CatalogSyncRunner for ProcessCatalogSyncRunner {
    fn run_sync(
        &self,
        python: &Path,
        script: &Path,
        codex_dir: &Path,
    ) -> Result<CatalogCommandOutcome, String> {
        let output = Command::new(python)
            .arg(script)
            .arg("--sync")
            .env("CODEX_HOME", codex_dir)
            .output()
            .map_err(|error| format!("failed to start catalog sync: {error}"))?;

        Ok(CatalogCommandOutcome {
            code: output.status.code(),
            stdout: String::from_utf8_lossy(&output.stdout).to_string(),
            stderr: String::from_utf8_lossy(&output.stderr).to_string(),
        })
    }
}

fn generate_catalog_with_runner(
    paths: &ModelPaths,
    python: &Path,
    runner: &dyn CatalogSyncRunner,
) -> Result<Vec<Model>, String> {
    let script = paths.catalog_sync_script();
    if !script.exists() {
        return Err(format!(
            "catalog sync script not found: {}",
            script.display()
        ));
    }

    let catalog_path = paths.generated_catalog_path();
    if let Some(parent) = catalog_path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            format!(
                "failed to create catalog output directory {}: {error}",
                parent.display()
            )
        })?;
    }

    let outcome = runner.run_sync(python, &script, &paths.codex_dir)?;
    if outcome.code != Some(0) {
        return Err(format!(
            "catalog sync failed with {}\nstdout:\n{}\nstderr:\n{}",
            format_exit_code(outcome.code),
            outcome.stdout.trim_end(),
            outcome.stderr.trim_end()
        ));
    }

    read_catalog_models(&catalog_path)
}

fn read_catalog_models(path: &Path) -> Result<Vec<Model>, String> {
    let text = fs::read_to_string(path)
        .map_err(|error| format!("failed to read catalog JSON {}: {error}", path.display()))?;
    let payload: Value = serde_json::from_str(&text)
        .map_err(|error| format!("failed to parse catalog JSON {}: {error}", path.display()))?;
    let items = payload
        .get("models")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            format!(
                "catalog JSON {} does not contain a models array",
                path.display()
            )
        })?;

    let mut seen = HashSet::new();
    let mut models = Vec::new();
    for item in items {
        let Some(model) = catalog_model_from_item(item) else {
            continue;
        };
        if seen.insert(model.id.clone()) {
            models.push(model);
        }
    }

    Ok(models)
}

fn catalog_model_from_item(item: &Value) -> Option<Model> {
    let object = item.as_object()?;
    let id = object
        .get("slug")
        .or_else(|| object.get("id"))
        .or_else(|| object.get("model"))
        .or_else(|| object.get("name"))
        .and_then(Value::as_str)
        .and_then(nonblank)?;

    Some(Model {
        id,
        display_name: object
            .get("display_name")
            .and_then(Value::as_str)
            .and_then(nonblank),
        upstream_model: object
            .get("upstream_model")
            .and_then(Value::as_str)
            .and_then(nonblank)
            .or_else(|| {
                object
                    .get("codex_proxy_metadata")
                    .and_then(Value::as_object)
                    .and_then(|metadata| metadata.get("upstream_model"))
                    .and_then(Value::as_str)
                    .and_then(nonblank)
            }),
        context_window: numeric_limit(
            item,
            &["context_window", "max_context_window", "context_length"],
            "context",
        ),
        max_output_tokens: numeric_limit(item, &["max_output_tokens", "output_tokens"], "output"),
        sort_order: object.get("priority").and_then(optional_i32),
        enabled: true,
    })
}

fn format_exit_code(code: Option<i32>) -> String {
    code.map_or_else(
        || "no exit code".to_string(),
        |code| format!("exit code {code}"),
    )
}

fn find_python() -> PathBuf {
    which::which("python")
        .or_else(|_| which::which("python3"))
        .unwrap_or_else(|_| PathBuf::from("python"))
}

#[cfg(test)]
mod tests {
    use super::{
        discover_provider_models_with_timeout, generate_catalog_with_runner, list_models,
        refresh_official_models_from_endpoint, CatalogCommandOutcome, CatalogSyncRunner,
        ModelPaths,
    };
    use crate::Model;
    use std::cell::RefCell;
    use std::fs;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::path::{Path, PathBuf};
    use std::sync::mpsc::{self, Receiver};
    use std::sync::Mutex;
    use std::thread::{self, JoinHandle};
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn official_discovery_uses_expected_url_headers_and_timeout() {
        let body = r#"{"data":[{"id":"gpt-live","context_window":128000}]}"#;
        let server = MockServer::json(body, Duration::ZERO);
        let endpoint = format!("{}/v1/models", server.base_url());

        let models = refresh_official_models_from_endpoint(
            &endpoint,
            " test-secret ",
            Duration::from_secs(2),
        )
        .expect("official discovery");

        assert_eq!(model_ids(&models), ["gpt-live"]);
        let request = server.request();
        assert!(request.starts_with("GET /v1/models "));
        let lowered = request.to_ascii_lowercase();
        assert!(lowered.contains("authorization: bearer test-secret"));
        assert!(lowered.contains("accept: application/json"));
        server.join();

        let slow_server = MockServer::json(body, Duration::from_millis(250));
        let slow_endpoint = format!("{}/v1/models", slow_server.base_url());
        let error = refresh_official_models_from_endpoint(
            &slow_endpoint,
            "test-secret",
            Duration::from_millis(30),
        )
        .expect_err("slow response should time out");

        assert!(error.contains("official OpenAI models"));
        assert!(!error.contains("test-secret"));
        slow_server.join();
    }

    #[test]
    fn official_discovery_filters_dedupes_sorts_and_parses_limits() {
        let body = r#"
        {
          "data": [
            {"id":" gpt-4.1-mini ","context_window":128000,"max_output_tokens":32768},
            {"id":"gpt-4.1","context_length":"1047576","output_tokens":"32768"},
            {"model":"gpt-4o","limit":{"context":128000,"output":16384}},
            {"id":"gpt-4.1","context_window":1,"max_output_tokens":1},
            {"id":"chatgpt-4o-latest"},
            {"id":"o3"},
            {"id":"  "},
            {"id":123}
          ]
        }
        "#;
        let server = MockServer::json(body, Duration::ZERO);
        let endpoint = format!("{}/v1/models", server.base_url());

        let models =
            refresh_official_models_from_endpoint(&endpoint, "test-secret", Duration::from_secs(2))
                .expect("official discovery");

        assert_eq!(
            compact_models(&models),
            vec![
                ("gpt-4.1", Some(1_047_576), Some(32_768)),
                ("gpt-4.1-mini", Some(128_000), Some(32_768)),
                ("gpt-4o", Some(128_000), Some(16_384)),
            ]
        );
        server.join();
    }

    #[test]
    fn provider_discovery_handles_payload_shapes_dedupes_and_preserves_order() {
        let cases = [
            (
                r#"{"data":[{"id":"alpha","context_window":128000,"max_output_tokens":8192},{"model":"beta","max_context_window":64000,"output_tokens":4096},{"name":"nested","limit":{"context":32000,"output":2048}},"string-model",{"slug":"alpha","context_length":1},{"id":"  "}]}"#,
                vec![
                    ("alpha", Some(128_000), Some(8_192)),
                    ("beta", Some(64_000), Some(4_096)),
                    ("nested", Some(32_000), Some(2_048)),
                    ("string-model", None, None),
                ],
            ),
            (
                r#"{"models":[{"slug":"from-models","context_length":1024}]}"#,
                vec![("from-models", Some(1_024), None)],
            ),
            (
                r#"[{"id":"from-list","max_output_tokens":"256"}]"#,
                vec![("from-list", None, Some(256))],
            ),
        ];

        for (body, expected) in cases {
            let server = MockServer::json(body, Duration::ZERO);

            let models = discover_provider_models_with_timeout(
                &server.base_url(),
                " provider-secret ",
                Duration::from_secs(2),
            )
            .expect("provider discovery");

            assert_eq!(compact_models(&models), expected);
            let request = server.request();
            assert!(request.starts_with("GET /v1/models "));
            assert!(request
                .to_ascii_lowercase()
                .contains("authorization: bearer provider-secret"));
            server.join();
        }
    }

    #[test]
    fn provider_discovery_accepts_blank_api_key_without_authorization_header() {
        let server = MockServer::json(r#"{"models":["public-model"]}"#, Duration::ZERO);

        let models =
            discover_provider_models_with_timeout(&server.base_url(), "  ", Duration::from_secs(2))
                .expect("provider discovery");

        assert_eq!(model_ids(&models), ["public-model"]);
        let request = server.request();
        assert!(request.starts_with("GET /v1/models "));
        assert!(!request.to_ascii_lowercase().contains("authorization:"));
        server.join();
    }

    #[test]
    fn provider_discovery_does_not_duplicate_v1_suffix() {
        let server = MockServer::json(r#"{"models":["v1-model"]}"#, Duration::ZERO);
        let base_url = format!("{}/v1/", server.base_url());

        let models =
            discover_provider_models_with_timeout(&base_url, "test-secret", Duration::from_secs(2))
                .expect("provider discovery");

        assert_eq!(model_ids(&models), ["v1-model"]);
        let request = server.request();
        assert!(request.starts_with("GET /v1/models "));
        server.join();
    }

    #[test]
    fn refresh_official_models_requires_openai_api_key_without_leaking_values() {
        let _guard = ENV_LOCK.lock().unwrap();
        let previous = std::env::var_os("OPENAI_API_KEY");
        std::env::remove_var("OPENAI_API_KEY");

        let error = super::refresh_official_models().expect_err("missing key should fail");

        restore_env("OPENAI_API_KEY", previous);
        assert!(error.contains("OPENAI_API_KEY"));
        assert!(!error.contains("sk-"));
    }

    #[test]
    fn generate_catalog_runs_sync_and_reads_generated_catalog_from_codex_home() {
        let root = temp_root("generate-catalog");
        let paths = test_paths(&root);
        fs::create_dir_all(paths.catalog_sync_script().parent().unwrap()).unwrap();
        fs::write(paths.catalog_sync_script(), "# fake catalog sync").unwrap();
        let catalog_path = paths.generated_catalog_path();
        let runner = WritingCatalogRunner::new(
            catalog_path.clone(),
            r#"
            {
              "models": [
                {
                  "slug": "openai/gpt-5.5",
                  "display_name": "OpenAI GPT-5.5",
                  "context_window": 128000,
                  "max_output_tokens": "32768",
                  "priority": 3,
                  "codex_proxy_metadata": {"upstream_model": "gpt-5.5"}
                },
                {
                  "slug": "glm-5.2",
                  "display_name": "GLM-5.2",
                  "max_context_window": 1000000,
                  "limit": {"output": 131072}
                },
                {"slug": "  "}
              ]
            }
            "#,
            CatalogCommandOutcome {
                code: Some(0),
                stdout: "visible_models=2\n".to_string(),
                stderr: String::new(),
            },
        );

        let models = generate_catalog_with_runner(&paths, Path::new("python-test"), &runner)
            .expect("catalog");

        assert!(catalog_path.exists());
        assert_eq!(models.len(), 2);
        assert_model(
            &models[0],
            "openai/gpt-5.5",
            Some("OpenAI GPT-5.5"),
            Some("gpt-5.5"),
            Some(128_000),
            Some(32_768),
            Some(3),
        );
        assert_model(
            &models[1],
            "glm-5.2",
            Some("GLM-5.2"),
            None,
            Some(1_000_000),
            Some(131_072),
            None,
        );

        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 1);
        assert_eq!(commands[0].python, PathBuf::from("python-test"));
        assert_eq!(commands[0].script, paths.catalog_sync_script());
        assert_eq!(commands[0].codex_dir, root.join("codex-home"));
    }

    #[test]
    fn generate_catalog_reads_codex_home_even_if_sync_prints_another_catalog_path() {
        let root = temp_root("generate-catalog-stdout-path");
        let paths = test_paths(&root);
        fs::create_dir_all(paths.catalog_sync_script().parent().unwrap()).unwrap();
        fs::write(paths.catalog_sync_script(), "# fake catalog sync").unwrap();
        let printed_catalog = root.join("printed").join("catalog.json");
        fs::create_dir_all(printed_catalog.parent().unwrap()).unwrap();
        fs::write(
            &printed_catalog,
            r#"{"models":[{"slug":"printed-model","display_name":"Printed Model"}]}"#,
        )
        .unwrap();
        let runner = WritingCatalogRunner::new(
            paths.generated_catalog_path(),
            r#"{"models":[{"slug":"codex-home-model","display_name":"Codex Home Model"}]}"#,
            CatalogCommandOutcome {
                code: Some(0),
                stdout: format!("catalog={}\n", printed_catalog.display()),
                stderr: String::new(),
            },
        );

        let models = generate_catalog_with_runner(&paths, Path::new("python-test"), &runner)
            .expect("catalog");

        assert_eq!(model_ids(&models), ["codex-home-model"]);
    }

    #[test]
    fn list_models_reads_generated_catalog_from_codex_home() {
        let _guard = ENV_LOCK.lock().unwrap();
        let previous = std::env::var_os("CODEX_HOME");
        let root = temp_root("list-models-codex-home");
        let codex_home = root.join("codex-home");
        let catalog_path = codex_home
            .join("model-catalogs")
            .join(super::GENERATED_CATALOG_FILE);
        fs::create_dir_all(catalog_path.parent().unwrap()).unwrap();
        fs::write(
            &catalog_path,
            r#"{"models":[{"slug":"codex-home-list-model","display_name":"Codex Home List Model"}]}"#,
        )
        .unwrap();
        std::env::set_var("CODEX_HOME", &codex_home);

        let models = list_models();

        restore_env("CODEX_HOME", previous);
        assert_eq!(
            model_ids(&models.expect("list models")),
            ["codex-home-list-model"]
        );
    }

    fn compact_models(models: &[Model]) -> Vec<(&str, Option<u32>, Option<u32>)> {
        models
            .iter()
            .map(|model| {
                (
                    model.id.as_str(),
                    model.context_window,
                    model.max_output_tokens,
                )
            })
            .collect()
    }

    fn model_ids(models: &[Model]) -> Vec<&str> {
        models.iter().map(|model| model.id.as_str()).collect()
    }

    #[allow(clippy::too_many_arguments)]
    fn assert_model(
        model: &Model,
        id: &str,
        display_name: Option<&str>,
        upstream_model: Option<&str>,
        context_window: Option<u32>,
        max_output_tokens: Option<u32>,
        sort_order: Option<i32>,
    ) {
        assert_eq!(model.id, id);
        assert_eq!(model.display_name.as_deref(), display_name);
        assert_eq!(model.upstream_model.as_deref(), upstream_model);
        assert_eq!(model.context_window, context_window);
        assert_eq!(model.max_output_tokens, max_output_tokens);
        assert_eq!(model.sort_order, sort_order);
        assert!(model.enabled);
    }

    fn restore_env(name: &str, value: Option<std::ffi::OsString>) {
        match value {
            Some(value) => std::env::set_var(name, value),
            None => std::env::remove_var(name),
        }
    }

    #[derive(Debug)]
    struct MockServer {
        base_url: String,
        request: Receiver<String>,
        handle: JoinHandle<()>,
    }

    impl MockServer {
        fn json(body: &str, delay: Duration) -> Self {
            let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind mock server");
            let base_url = format!("http://{}", listener.local_addr().unwrap());
            let body = body.to_string();
            let (request_tx, request_rx) = mpsc::channel();
            let handle = thread::spawn(move || {
                let (mut stream, _) = listener.accept().expect("accept request");
                let mut buffer = [0; 8192];
                let count = stream.read(&mut buffer).expect("read request");
                let request = String::from_utf8_lossy(&buffer[..count]).to_string();
                request_tx.send(request).expect("send request");
                if !delay.is_zero() {
                    thread::sleep(delay);
                }
                let response = format!(
                    "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
                    body.len(),
                    body
                );
                let _ = stream.write_all(response.as_bytes());
            });

            Self {
                base_url,
                request: request_rx,
                handle,
            }
        }

        fn base_url(&self) -> String {
            self.base_url.clone()
        }

        fn request(&self) -> String {
            self.request
                .recv_timeout(Duration::from_secs(2))
                .expect("mock server received request")
        }

        fn join(self) {
            self.handle.join().expect("mock server thread");
        }
    }

    #[derive(Debug, Clone)]
    struct RecordedCatalogCommand {
        python: PathBuf,
        script: PathBuf,
        codex_dir: PathBuf,
    }

    struct WritingCatalogRunner {
        commands: RefCell<Vec<RecordedCatalogCommand>>,
        catalog_path: PathBuf,
        catalog_body: String,
        outcome: CatalogCommandOutcome,
    }

    impl WritingCatalogRunner {
        fn new(catalog_path: PathBuf, catalog_body: &str, outcome: CatalogCommandOutcome) -> Self {
            Self {
                commands: RefCell::new(Vec::new()),
                catalog_path,
                catalog_body: catalog_body.to_string(),
                outcome,
            }
        }
    }

    impl CatalogSyncRunner for WritingCatalogRunner {
        fn run_sync(
            &self,
            python: &Path,
            script: &Path,
            codex_dir: &Path,
        ) -> Result<CatalogCommandOutcome, String> {
            let catalog_parent = self
                .catalog_path
                .parent()
                .ok_or_else(|| "catalog path must have a parent".to_string())?;
            assert!(
                catalog_parent.is_dir(),
                "catalog output directory should exist before sync runs"
            );
            fs::write(&self.catalog_path, &self.catalog_body)
                .map_err(|error| format!("failed to write test catalog: {error}"))?;
            self.commands.borrow_mut().push(RecordedCatalogCommand {
                python: python.to_path_buf(),
                script: script.to_path_buf(),
                codex_dir: codex_dir.to_path_buf(),
            });
            Ok(self.outcome.clone())
        }
    }

    fn test_paths(root: &Path) -> ModelPaths {
        ModelPaths::new(root.join("codex-home"), root.join("repo-root"))
    }

    fn temp_root(name: &str) -> PathBuf {
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "codexhub-models-{name}-{}-{suffix}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&path);
        fs::create_dir_all(&path).unwrap();
        path
    }
}
