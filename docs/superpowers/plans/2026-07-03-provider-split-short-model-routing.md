# Provider Split Short Model Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Export CodexHub client profiles as one client provider per upstream provider, with short model IDs inside each provider and deterministic Gateway routing for duplicate model names.

**Architecture:** Client configs will carry provider context primarily through provider-specific Gateway URLs: `/v1/providers/<provider-id>/chat/completions` and `/v1/providers/<provider-id>/responses`. Gateway will resolve a short inbound `model` such as `glm-5.2` against the provider path to the canonical route model `ollama-cloud/glm-5.2`, preserving exact model casing. Rust client export code will group models by provider and write split client provider blocks for OpenCode, ZCode, Pi, and OMP.

**Implementation Status:** Implemented. During review, provider-scoped requests with slash-bearing model IDs were changed to provider-relative routing, so `/v1/providers/openrouter/chat/completions` plus `model: "anthropic/claude-sonnet-4"` resolves as `openrouter/anthropic/claude-sonnet-4` instead of being rejected as a provider mismatch.

**Tech Stack:** Rust/Tauri backend in `src-tauri/src/gateway.rs`, Python Gateway proxy in `src-python/codex_proxy.py`, Python tests in `tests/test_routing.py` and `tests/test_chat_completions_gateway.py`, Rust unit tests in `src-tauri/src/gateway.rs`.

---

## Capability Findings

- Pi and OMP support custom provider `baseUrl`, provider/model `headers`, and short model IDs. Their OpenAI-compatible transport preserves `baseUrl` path and appends `/chat/completions`.
- OpenCode 1.17.13 supports multiple provider entries. The official schema allows provider `options.baseURL` and model-level `headers`; official docs show custom OpenAI-compatible providers using `@ai-sdk/openai-compatible`.
- ZCode v2 supports multiple providers in both `config.json` and `bots-model-cache.v2.json`. The active runtime config path is detected from `%USERPROFILE%\.zcode\v2\setting.json` `dataBaseDir`, then `<dataBaseDir>\.zcode\v2\config.json`.
- A provider-specific URL is the least fragile provider-context carrier because it works even if a client ignores custom headers.
- Exact casing matters. Provider prefix resolution must build `provider_id + "/" + short_model_id` without lowercasing the model segment.

## File Structure

- Modify `src-python/codex_proxy.py`: Parse provider-scoped Gateway paths, resolve short model IDs to canonical route IDs, and allow provider-scoped POST endpoints.
- Modify `tests/test_routing.py`: Add pure routing tests for provider-scoped short IDs, exact case, and mismatch rejection.
- Modify `tests/test_chat_completions_gateway.py`: Add handler-level POST tests for `/v1/providers/<provider>/chat/completions`.
- Modify `src-tauri/src/gateway.rs`: Replace the flat client export path with grouped client providers and write split OpenCode, ZCode, Pi, and OMP configs.
- Modify Rust tests inside `src-tauri/src/gateway.rs`: Update old single-provider assertions and add duplicate short-name coverage.

---

### Task 1: Gateway Provider-Scoped Path Parsing

**Files:**
- Modify: `src-python/codex_proxy.py`
- Test: `tests/test_routing.py`

- [ ] **Step 1: Write failing routing tests**

Add these tests near the existing `choose_upstream` tests in `tests/test_routing.py`:

```python
def test_provider_scoped_short_model_routes_to_external_provider(self):
    route_model = codex_proxy.provider_scoped_route_model("glm-5.2", "volc")

    self.assertEqual(route_model, "volc/glm-5.2")
    upstream = choose_upstream(route_model)
    self.assertEqual(upstream["name"], "volcengine")
    self.assertEqual(upstream["upstream_model"], "glm-5.2")


def test_provider_scoped_short_model_preserves_exact_case(self):
    route_model = codex_proxy.provider_scoped_route_model("MiniMax-M3", "minimax-cn")

    self.assertEqual(route_model, "minimax-cn/MiniMax-M3")
    upstream = choose_upstream(route_model)
    self.assertEqual(upstream["name"], "minimax_cn")
    self.assertEqual(upstream["upstream_model"], "MiniMax-M3")


def test_provider_scoped_model_rejects_mismatched_canonical_provider(self):
    with self.assertRaises(ValueError) as context:
        codex_proxy.provider_scoped_route_model("volc/glm-5.2", "ollama-cloud")

    self.assertIn("does not match provider path", str(context.exception))
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
python -m pytest tests/test_routing.py -k "provider_scoped_short_model" -q
```

Expected: FAIL because `provider_scoped_route_model` does not exist.

- [ ] **Step 3: Implement provider-scoped route helpers**

Add these helpers after `_is_websocket_upgrade` in `src-python/codex_proxy.py`:

```python
PROVIDER_SCOPED_PREFIX = "/v1/providers/"


def provider_scoped_path(path: str, endpoint: str) -> str | None:
    parsed = urlsplit(path)
    prefix = PROVIDER_SCOPED_PREFIX
    suffix = f"/{endpoint.lstrip('/')}"
    if not parsed.path.startswith(prefix) or not parsed.path.endswith(suffix):
        return None
    provider_id = parsed.path[len(prefix) : -len(suffix)]
    provider_id = unquote(provider_id).strip("/")
    return provider_id or None


def provider_scoped_route_model(model_id: str | None, provider_id: str | None) -> str | None:
    if not model_id or not provider_id:
        return model_id
    model = str(model_id).strip()
    provider = str(provider_id).strip()
    if not model or not provider:
        return model
    if "/" in model:
        actual_provider, _ = model.split("/", 1)
        if actual_provider != provider:
            raise ValueError(f"model provider {actual_provider} does not match provider path {provider}")
        return model
    return f"{provider}/{model}"
```

Also add the missing import at the top:

```python
from urllib.parse import unquote, urlsplit
```

If `urlsplit` is already imported from `urllib.parse`, extend that import instead of adding a duplicate.

- [ ] **Step 4: Run routing tests**

Run:

```powershell
python -m pytest tests/test_routing.py -k "provider_scoped_short_model" -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src-python/codex_proxy.py tests/test_routing.py
git commit -m "Add provider-scoped Gateway model routing"
```

---

### Task 2: Gateway Provider-Scoped POST Endpoints

**Files:**
- Modify: `src-python/codex_proxy.py`
- Test: `tests/test_chat_completions_gateway.py`

- [ ] **Step 1: Write failing handler test**

Add this test in `tests/test_chat_completions_gateway.py` near `test_post_chat_completions_routes_to_official_and_injects_subscription_token`:

```python
def test_provider_scoped_chat_completions_routes_short_model(self):
    external = {
        "alias": "volc/glm-5.2",
        "provider_alias": "volc",
        "upstream_name": "volcengine",
        "base_url": "https://ark.example.test/v1",
        "api_key": "volc-token",
        "upstream_model": "glm-5.2",
        "upstream_format": "chat_completions",
    }
    body = json.dumps({
        "model": "glm-5.2",
        "messages": [{"role": "user", "content": "Hello"}],
        "stream": False,
    }).encode("utf-8")
    handler = self._make_handler(body, path="/v1/providers/volc/chat/completions")
    upstream_body = json.dumps({
        "id": "chatcmpl_test",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    }).encode("utf-8")

    with patch("codex_proxy.resolve_external_model_alias", return_value=external), \
         patch("codex_proxy.urlopen", return_value=_FakeJsonResponse(upstream_body)) as mock_urlopen:
        CodexProxyHandler.do_POST(handler)

    request = mock_urlopen.call_args.args[0]
    self.assertTrue(request.full_url.endswith("/chat/completions"))
    self.assertEqual(json.loads(request.data)["model"], "glm-5.2")
    self.assertEqual(handler._fake.status, 200)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
python -m pytest tests/test_chat_completions_gateway.py -k "provider_scoped_chat_completions" -q
```

Expected: FAIL because `/v1/providers/volc/chat/completions` returns 404.

- [ ] **Step 3: Wire provider-scoped POST paths**

Change `CodexProxyHandler.do_POST` in `src-python/codex_proxy.py` to pass the provider hint:

```python
provider_responses = provider_scoped_path(self.path, "responses")
if provider_responses:
    self._proxy_post_request(inbound_format="responses", provider_hint=provider_responses)
    return

provider_chat = provider_scoped_path(self.path, "chat/completions")
if provider_chat:
    self._proxy_post_request(inbound_format="chat_completions", provider_hint=provider_chat)
    return

if parsed.path == "/v1/responses":
    self._proxy_post_request(inbound_format="responses")
    return

if parsed.path == "/v1/chat/completions":
    self._proxy_post_request(inbound_format="chat_completions")
    return
```

Update `_proxy_post_request` signature and model resolution:

```python
def _proxy_post_request(self, *, inbound_format: str, provider_hint: str | None = None) -> None:
    ...
    model_requested = try_extract_model(body)
    model = provider_scoped_route_model(model_requested, provider_hint)
    route_reason = "provider_path" if provider_hint and model else "model" if model else "official_control_fallback"
    upstream = choose_upstream(model) if model else official_upstream()
```

In telemetry fields, use:

```python
model_requested=model_requested,
model_canonical=canonical_model_id(model) if model else None,
provider_hint=provider_hint,
```

- [ ] **Step 4: Run handler and routing tests**

Run:

```powershell
python -m pytest tests/test_chat_completions_gateway.py -k "provider_scoped_chat_completions" -q
python -m pytest tests/test_routing.py -k "provider_scoped_short_model or provider_prefixed_model_routes_to_external_provider" -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src-python/codex_proxy.py tests/test_chat_completions_gateway.py tests/test_routing.py
git commit -m "Accept provider-scoped Gateway request paths"
```

---

### Task 3: Rust Client Export Group Model

**Files:**
- Modify: `src-tauri/src/gateway.rs`
- Test: `src-tauri/src/gateway.rs`

- [ ] **Step 1: Write failing Rust grouping test**

Add this test near `pi_and_omp_configs_keep_duplicate_glm_models_distinct`:

```rust
#[test]
fn gateway_client_provider_groups_keep_duplicate_short_models_distinct() {
    let settings = Settings::default();
    let providers = case_sensitive_client_export_test_providers();

    let groups = gateway_client_provider_groups(&settings, &providers, "ollama-cloud/glm-5.2").unwrap();
    let ollama = groups.providers.iter().find(|group| group.provider_id == "ollama-cloud").unwrap();
    let volc = groups.providers.iter().find(|group| group.provider_id == "volc").unwrap();

    assert_eq!(ollama.client_provider_id, "codexhub-ollama-cloud");
    assert_eq!(volc.client_provider_id, "codexhub-volc");
    assert!(ollama.models.iter().any(|model| model.short_id == "glm-5.2"));
    assert!(volc.models.iter().any(|model| model.short_id == "glm-5.2"));
    assert_eq!(groups.default_provider_id, "ollama-cloud");
    assert_eq!(groups.default_short_model_id, "glm-5.2");
}
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
cargo test --manifest-path src-tauri/Cargo.toml gateway::tests::gateway_client_provider_groups_keep_duplicate_short_models_distinct
```

Expected: FAIL because `gateway_client_provider_groups` does not exist.

- [ ] **Step 3: Add grouped export structs**

Add these structs near `GatewayModel`:

```rust
#[derive(Clone, Debug)]
struct GatewayClientModel {
    canonical_id: String,
    short_id: String,
    display_name: String,
    context_window: u64,
}

#[derive(Clone, Debug)]
struct GatewayClientProvider {
    provider_id: String,
    client_provider_id: String,
    display_name: String,
    base_url: String,
    models: Vec<GatewayClientModel>,
}

#[derive(Clone, Debug)]
struct GatewayClientProviderGroups {
    providers: Vec<GatewayClientProvider>,
    default_provider_id: String,
    default_client_provider_id: String,
    default_short_model_id: String,
    default_canonical_model_id: String,
}
```

- [ ] **Step 4: Implement grouping helpers**

Add helpers after `gateway_client_models`:

```rust
fn client_provider_id(provider_id: &str) -> String {
    format!(
        "codexhub-{}",
        provider_id
            .chars()
            .map(|ch| if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' { ch } else { '-' })
            .collect::<String>()
            .trim_matches('-')
    )
}

fn provider_scoped_base_url(settings: &Settings, provider_id: &str) -> String {
    let encoded = provider_id.replace('/', "%2F");
    format!("{}/providers/{encoded}", endpoints(settings.proxy_port).base_url)
}

fn split_canonical_model_id(model_id: &str) -> (String, String) {
    if let Some((provider, short)) = model_id.split_once('/') {
        (provider.to_string(), short.to_string())
    } else {
        ("ollama-cloud".to_string(), model_id.to_string())
    }
}
```

Add `gateway_client_provider_groups` that:

```rust
fn gateway_client_provider_groups(
    settings: &Settings,
    providers: &[Provider],
    default_model: &str,
) -> Result<GatewayClientProviderGroups, String> {
    let default_canonical = resolve_gateway_client_model_id(settings, providers, default_model)?;
    let (default_provider_id, default_short_model_id) = split_canonical_model_id(&default_canonical);
    let mut provider_names = HashMap::<String, String>::new();
    provider_names.insert("openai".to_string(), "OpenAI".to_string());
    provider_names.insert("ollama-cloud".to_string(), "Ollama Cloud".to_string());
    for provider in providers {
        provider_names.insert(provider.id.clone(), provider.name.clone());
    }

    let mut grouped = BTreeMap::<String, GatewayClientProvider>::new();
    for model in gateway_client_models(settings, providers, &default_canonical)? {
        let (provider_id, short_id) = split_canonical_model_id(&model.id);
        let entry = grouped.entry(provider_id.clone()).or_insert_with(|| GatewayClientProvider {
            provider_id: provider_id.clone(),
            client_provider_id: client_provider_id(&provider_id),
            display_name: provider_names.get(&provider_id).cloned().unwrap_or_else(|| provider_id.clone()),
            base_url: provider_scoped_base_url(settings, &provider_id),
            models: Vec::new(),
        });
        entry.models.push(GatewayClientModel {
            canonical_id: model.id,
            short_id,
            display_name: model.display_name,
            context_window: model.context_window,
        });
    }

    let default_client_provider_id = client_provider_id(&default_provider_id);
    Ok(GatewayClientProviderGroups {
        providers: grouped.into_values().collect(),
        default_provider_id,
        default_client_provider_id,
        default_short_model_id,
        default_canonical_model_id: default_canonical,
    })
}
```

If `BTreeMap` is not imported, add it to the existing `std::collections` import.

- [ ] **Step 5: Run grouping test**

Run:

```powershell
cargo test --manifest-path src-tauri/Cargo.toml gateway::tests::gateway_client_provider_groups_keep_duplicate_short_models_distinct
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add src-tauri/src/gateway.rs
git commit -m "Group Gateway client models by provider"
```

---

### Task 4: Split OpenCode, Pi, and OMP Provider Exports

**Files:**
- Modify: `src-tauri/src/gateway.rs`
- Test: `src-tauri/src/gateway.rs`

- [ ] **Step 1: Update failing Rust tests**

Replace the old single-provider assertions in these tests:

- `opencode_config_exports_all_active_gateway_models`
- `opencode_config_resolves_selected_alias_and_exports_only_canonical_models`
- `pi_and_omp_configs_keep_duplicate_glm_models_distinct`
- `pi_config_exports_all_active_gateway_models`
- `omp_models_export_all_active_gateway_models`

Use these core expectations:

```rust
assert_eq!(value["model"], "codexhub-openai/gpt-5.5");
assert!(value.pointer("/provider/codexhub-openai/models/gpt-5.5").is_some());
assert!(value.pointer("/provider/codexhub-minimax/models/minimax-m3").is_some());
assert_eq!(
    value.pointer("/provider/codexhub-openai/options/baseURL").and_then(serde_json::Value::as_str),
    Some("http://127.0.0.1:9099/v1/providers/openai")
);
```

For duplicate GLM models:

```rust
assert_eq!(pi_value["defaultProvider"], "codexhub-ollama-cloud");
assert_eq!(pi_value["defaultModel"], "glm-5.2");
assert!(pi_models_value.pointer("/providers/codexhub-ollama-cloud/models").is_some());
assert!(pi_models_value.pointer("/providers/codexhub-volc/models").is_some());
assert!(omp_text.contains("default: codexhub-ollama-cloud/glm-5.2"));
assert!(omp_text.contains("  codexhub-ollama-cloud:"));
assert!(omp_text.contains("  codexhub-volc:"));
assert!(omp_text.contains("      - id: glm-5.2"));
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
cargo test --manifest-path src-tauri/Cargo.toml gateway::tests::opencode_config_exports_all_active_gateway_models gateway::tests::pi_and_omp_configs_keep_duplicate_glm_models_distinct
```

Expected: FAIL because exports are still single-provider.

- [ ] **Step 3: Update OpenCode export**

Change `opencode_config_text` to iterate `gateway_client_provider_groups` and write one provider per group:

```rust
let groups = gateway_client_provider_groups(settings, providers, model)?;
let mut provider_map = Map::new();
for group in &groups.providers {
    let mut models = Map::new();
    for model in &group.models {
        models.insert(
            model.short_id.clone(),
            json!({
                "name": model.display_name,
                "headers": {
                    "X-CodexHub-Provider": group.provider_id,
                },
            }),
        );
    }
    provider_map.insert(group.client_provider_id.clone(), json!({
        "name": group.display_name,
        "npm": "@ai-sdk/openai-compatible",
        "options": {
            "baseURL": group.base_url,
            "apiKey": settings.gateway_client_key,
        },
        "models": Value::Object(models),
    }));
}
let body = json!({
    "$schema": "https://opencode.ai/config.json",
    "model": format!("{}/{}", groups.default_client_provider_id, groups.default_short_model_id),
    "small_model": format!("{}/{}", groups.default_client_provider_id, groups.default_short_model_id),
    "provider": Value::Object(provider_map),
});
```

- [ ] **Step 4: Update Pi export**

Change `pi_settings_text`:

```rust
let groups = gateway_client_provider_groups(settings, providers, model)?;
object.insert("defaultProvider".to_string(), json!(groups.default_client_provider_id));
object.insert("defaultModel".to_string(), json!(groups.default_short_model_id));
object.remove("enabledModels");
```

Change `pi_models_text` to insert every group:

```rust
let groups = gateway_client_provider_groups(settings, providers, model)?;
let providers_object = provider_root.as_object_mut().ok_or_else(|| "Pi providers root must be a JSON object".to_string())?;
for group in &groups.providers {
    providers_object.insert(group.client_provider_id.clone(), codexhub_pi_provider_value(settings, group));
}
```

Change `codexhub_pi_provider_value` signature to accept `&GatewayClientProvider` and write:

```rust
json!({
    "baseUrl": group.base_url,
    "api": "openai-completions",
    "apiKey": settings.gateway_client_key,
    "authHeader": true,
    "headers": {
        "X-CodexHub-Provider": group.provider_id,
    },
    "compat": {
        "supportsDeveloperRole": true,
        "supportsReasoningEffort": true,
        "supportsUsageInStreaming": true,
    },
    "models": models,
})
```

Update `codexhub_pi_model_value` to use `GatewayClientModel.short_id`.

- [ ] **Step 5: Update OMP export**

Change `omp_config_text` to receive provider and short model selector or add `omp_config_text_for_groups`:

```rust
let selector = format!("{}/{}", groups.default_client_provider_id, groups.default_short_model_id);
```

Change `omp_models_yml_text` to iterate groups:

```rust
let groups = gateway_client_provider_groups(settings, providers, model)?;
let mut output = "providers:\n".to_string();
for group in &groups.providers {
    output.push_str(&format!(
        "  {}:\n    baseUrl: {}\n    api: openai-completions\n    apiKey: {}\n    authHeader: true\n    headers:\n      X-CodexHub-Provider: {}\n    compat:\n      supportsDeveloperRole: true\n      supportsReasoningEffort: true\n      supportsUsageInStreaming: true\n    models:\n",
        yaml_scalar(&group.client_provider_id),
        yaml_scalar(&group.base_url),
        yaml_scalar(&settings.gateway_client_key),
        yaml_scalar(&group.provider_id),
    ));
    for model in &group.models {
        output.push_str(&format!(
            "      - id: {}\n        name: {}\n        reasoning: true\n        input:\n          - text\n          - image\n        contextWindow: {}\n        maxTokens: 32768\n        cost:\n          input: 0\n          output: 0\n          cacheRead: 0\n          cacheWrite: 0\n",
            yaml_scalar(&model.short_id),
            yaml_scalar(&model.display_name),
            model.context_window,
        ));
    }
}
```

- [ ] **Step 6: Run Rust client export tests**

Run:

```powershell
cargo test --manifest-path src-tauri/Cargo.toml gateway::tests::opencode_config_exports_all_active_gateway_models gateway::tests::pi_and_omp_configs_keep_duplicate_glm_models_distinct gateway::tests::pi_config_exports_all_active_gateway_models gateway::tests::omp_models_export_all_active_gateway_models
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add src-tauri/src/gateway.rs
git commit -m "Split Gateway client exports by provider"
```

---

### Task 5: Split ZCode Catalog and V2 Config

**Files:**
- Modify: `src-tauri/src/gateway.rs`
- Test: `src-tauri/src/gateway.rs`

- [ ] **Step 1: Update failing ZCode tests**

Update `zcode_catalog_exports_all_active_gateway_models` and `zcode_v2_config_replaces_active_config_with_codexhub_provider` to assert:

```rust
let providers = value.pointer("/providers").and_then(serde_json::Value::as_array).unwrap();
assert!(providers.iter().any(|provider| provider["id"] == "codexhub-openai"));
assert!(providers.iter().any(|provider| provider["id"] == "codexhub-minimax"));
let ollama = providers.iter().find(|provider| provider["id"] == "codexhub-ollama-cloud").unwrap();
assert_eq!(ollama.pointer("/endpoints/baseURL").and_then(serde_json::Value::as_str), Some("http://127.0.0.1:9099/v1/providers/ollama-cloud"));
assert!(ollama.pointer("/models/0/id").and_then(serde_json::Value::as_str) == Some("glm-5.2"));
```

For v2 config:

```rust
assert!(value.pointer("/provider/codexhub-ollama-cloud").is_some());
assert!(value.pointer("/provider/codexhub-volc").is_some());
assert_eq!(
    value.pointer("/provider/codexhub-ollama-cloud/options/baseURL").and_then(serde_json::Value::as_str),
    Some("http://127.0.0.1:9099/v1/providers/ollama-cloud")
);
assert!(value.pointer("/provider/codexhub-ollama-cloud/models/glm-5.2").is_some());
assert!(value.pointer("/provider/codexhub-volc/models/glm-5.2").is_some());
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
cargo test --manifest-path src-tauri/Cargo.toml gateway::tests::zcode_catalog_exports_all_active_gateway_models gateway::tests::zcode_v2_config_replaces_active_config_with_codexhub_provider
```

Expected: FAIL because ZCode still writes a single `codexhub` provider.

- [ ] **Step 3: Update `zcode_catalog_text`**

Build `providers` array from `gateway_client_provider_groups`:

```rust
let groups = gateway_client_provider_groups(settings, providers, model)?;
let providers = groups.providers.iter().map(|group| {
    let models = group.models.iter().map(zcode_model_value).collect::<Vec<_>>();
    json!({
        "id": group.client_provider_id,
        "name": group.display_name,
        "enabled": true,
        "source": "custom",
        "endpoints": {
            "baseURL": group.base_url,
            "paths": {
                "openai-compatible": "/chat/completions",
            },
        },
        "apiKeyRequired": true,
        "apiKey": settings.gateway_client_key,
        "defaultKind": "openai-compatible",
        "models": models,
        "createdAt": now,
        "updatedAt": now,
    })
}).collect::<Vec<_>>();
```

Update `zcode_model_value` to accept `&GatewayClientModel` and use `short_id`.

- [ ] **Step 4: Update `zcode_v2_config_text`**

Build provider object from groups:

```rust
let groups = gateway_client_provider_groups(settings, providers, model)?;
let mut provider_map = Map::new();
for group in &groups.providers {
    provider_map.insert(group.client_provider_id.clone(), zcode_v2_provider_value(settings, group));
}
let value = json!({ "provider": Value::Object(provider_map) });
```

Change `zcode_v2_provider_value` to accept `&GatewayClientProvider` and write:

```rust
json!({
    "name": group.display_name,
    "kind": "openai-compatible",
    "enabled": true,
    "source": "custom",
    "options": {
        "baseURL": group.base_url,
        "apiKey": settings.gateway_client_key,
        "apiKeyRequired": true,
    },
    "models": Value::Object(models),
})
```

The `models` map must be keyed by `GatewayClientModel.short_id`, not canonical ID.

- [ ] **Step 5: Run ZCode tests**

Run:

```powershell
cargo test --manifest-path src-tauri/Cargo.toml gateway::tests::zcode_catalog_exports_all_active_gateway_models gateway::tests::zcode_v2_config_replaces_active_config_with_codexhub_provider gateway::tests::zcode_v2_config_written_to_data_base_dir_when_present
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add src-tauri/src/gateway.rs
git commit -m "Split ZCode Gateway providers"
```

---

### Task 6: Full Verification and App Restart

**Files:**
- No source edits expected.

- [ ] **Step 1: Run Python tests**

Run:

```powershell
python -m pytest tests/test_routing.py tests/test_chat_completions_gateway.py tests/test_providers_config.py -q
```

Expected: PASS.

- [ ] **Step 2: Run Rust gateway tests**

Run:

```powershell
cargo test --manifest-path src-tauri/Cargo.toml gateway::tests::
```

Expected: PASS.

- [ ] **Step 3: Run Rust build**

Run:

```powershell
cargo build --manifest-path src-tauri/Cargo.toml
```

Expected: PASS.

- [ ] **Step 4: Restart local dev services**

Stop stale `web-bridge` or Tauri backend processes only if they are running from this workspace, then restart the app backend and keep Vite on `http://127.0.0.1:1420`.

Verification commands:

```powershell
Get-Process | Where-Object { $_.ProcessName -match 'web-bridge|CodexHub' } | Select-Object Id,ProcessName,Path
Invoke-RestMethod -Uri http://127.0.0.1:1421/health
```

Expected: backend health check succeeds and the browser app can refresh client statuses.

- [ ] **Step 5: Apply CodexHub profile to all four clients**

Use the app UI or backend command path to switch OpenCode, ZCode, Pi, and OMP to CodexHub. Then inspect sanitized config shape:

```powershell
$z = Get-Content -Raw 'D:\zcode\.zcode\v2\config.json' | ConvertFrom-Json
$z.provider.PSObject.Properties.Name

$p = Get-Content -Raw 'C:\Users\noirb\.pi\agent\models.json' | ConvertFrom-Json
$p.providers.PSObject.Properties.Name

Get-Content -Raw 'C:\Users\noirb\.omp\agent\models.yml' | Select-String 'codexhub-'
```

Expected: provider names include `codexhub-ollama-cloud`, `codexhub-volc`, `codexhub-minimax-cn`, and `codexhub-xunfei`; duplicate `glm-5.2` appears under separate providers as short IDs.

- [ ] **Step 6: Manual smoke tests**

Run:

```powershell
pi --model codexhub-ollama-cloud/glm-5.2 -p --no-session --no-tools "reply with ok"
omp --model codexhub-ollama-cloud/glm-5.2 -p --no-session --no-tools "reply with ok"
```

Expected: both commands return a short response, and Gateway logs show `model_canonical=ollama-cloud/glm-5.2`.

- [ ] **Step 7: Final commit**

```powershell
git status --short
git add src-python/codex_proxy.py tests/test_routing.py tests/test_chat_completions_gateway.py src-tauri/src/gateway.rs docs/superpowers/plans/2026-07-03-provider-split-short-model-routing.md
git commit -m "Plan provider-split short model routing"
```

Use a more specific final commit message if the implementation commits from prior tasks are squashed.
