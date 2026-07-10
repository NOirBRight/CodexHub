# Task 1 Report: Model identity, metadata, display, dedupe, slider, and editing

## Status

DONE_WITH_CONCERNS

The requested model contract and editing regression work is implemented. All required Python, Rust, and frontend verification passed. The only concern is a pre-existing repository-wide `cargo fmt --check` failure described below; it does not touch the required verification gate and the task changes themselves pass `git diff --check`.

## Commits

- `16ea4b3929e3f160251699617d67f42c4b3d9d37` — `fix model catalog identity contracts`
- `c33d2388beceb096381c76a7dc9acc92dd847018` — `preserve official app model contracts`
- `0c100c35a6f483fd063bce5435f161eb7eaaa0de` — `restore official model draft editing`
- `e039a604b51d3d2f25d82d0b8758c9abda61885d` — `canonicalize gateway model output`

## Behavior implemented

- Official models use bare `gpt-*` canonical IDs.
- A legacy `openai/gpt-*` ID is normalized only when the bare model exists in the static official policy or current App CLI/runtime catalog; unknown official aliases are rejected.
- Third-party IDs such as `acme/gpt-5.6-sol` and `ollama-cloud/glm-5.2` remain provider-qualified and distinct.
- The App-facing catalog and CodexHub model rows use short official names such as `5.6 Sol`, with the internal ID shown separately as `gpt-5.6-sol`.
- Gateway client groups remain `CodexHub OpenAI`, selectors remain `codexhub-openai/gpt-*`, and exported model labels are short names.
- Gateway `/v1/models` canonicalizes and deduplicates legacy official aliases while preserving third-party IDs.
- Raw App CLI model records are retained as the cache base. Unknown/current metadata fields are preserved, including context, reasoning efforts, `multi_agent_version`, tool mode, model messages, skills instructions, web search mode, responses-lite, availability, upgrade metadata, and compatibility hash.
- Python catalog generation no longer injects Ollama generic defaults into real official App records. Static official fallback records still receive only their official fallback metadata.
- Alias dedupe preserves the earliest position, lets the fresh bare/App record win metadata, and ORs enabled state.
- The official Terra/Sol reasoning metadata preserves the six simple-slider combinations: Terra Light; Sol Light; Sol Medium; Sol High; Sol Extra High; Sol Ultra. Full official effort lists remain available for advanced mode.
- Official model rows are sortable again. Row clicks no longer toggle official models; only the Toggle changes draft enabled state.
- Toggle and reorder changes stay in a dedicated draft. The bottom Save action performs one settings save and one catalog regenerate/client-sync path.

## Shared contract fixture

Added `tests/fixtures/model_identity_vectors.json` and consumed it from:

- Python catalog contract tests.
- Rust config contract tests via `include_str!`.
- TypeScript contract tests by transpiling and executing `normalizeOfficialModelId` in the Node UI contract runner.

The fixture covers official bare ID, accepted official legacy alias, rejected unknown official alias, `acme/gpt-*`, and `ollama-cloud/glm-5.2`.

## TDD evidence

### Python catalog RED

Command:

```powershell
python -m pytest tests/test_catalog_sync.py -q -k "shared_model_identity_vectors or official_catalog_preserves_app_cli_metadata or official_alias_duplicates or official_fast_metadata or build_catalog_uses_subscription"
```

Observed expected failures:

- `5 failed, 1 passed, 31 deselected, 4 subtests passed`.
- Unknown `openai/gpt-9.9-unknown` was incorrectly normalized to a bare ID.
- Official labels were emitted as `OpenAI GPT-*` instead of short names.
- `openai/gpt-5.6-sol` and `gpt-5.6-sol` remained duplicate records.
- Official records received generic fields such as shell/base-instruction defaults.

### Python catalog GREEN

Focused command result:

```text
5 passed, 31 deselected, 5 subtests passed
```

Broader catalog result:

```text
44 passed, 5 subtests passed
```

### Rust test-harness correction before valid RED

The first Rust RED command compiled the new tests with two missing `super::` qualifications. Cargo reported `sanitize_model_ids` and `official_subscription_seed_model` not found in test scope. The test references were corrected without changing production behavior, then the RED commands were rerun to obtain behavioral assertion failures.

### Rust behavioral RED

Commands:

```powershell
cargo test shared_model_identity_vectors_reject_only_unknown_official_aliases -- --nocapture
cargo test settings_accept_current_catalog_alias_and_reject_unknown_official_alias -- --nocapture
cargo test subscription_seed_preserves_app_cli_metadata_and_simple_slider_presets -- --nocapture
cargo test official_gateway_models_ -- --nocapture
```

Observed expected failures:

- Shared fixture retained unknown official alias as `gpt-9.9-unknown`.
- Settings retained the same unknown alias even when a current runtime catalog was present.
- Subscription seed displayed `GPT-5.6-Terra` instead of `5.6 Terra` and had not retained the required raw fields.
- Gateway returned two alias records and returned `GPT-5.6-Sol` instead of `5.6 Sol`.

### Rust GREEN

Focused results:

```text
shared fixture: 1 passed
current-catalog settings alias: 1 passed
App metadata and slider contract: 1 passed
Gateway display and dedupe: 2 passed
```

### Frontend RED

Command:

```powershell
npm run test:ui-contract -- --test-name-pattern "official model rows only|official OpenAI model edits|shared identity vectors|frontend official merge|official model list exposes"
```

The runner executed the full contract file. Result: `107 passed, 5 failed`.

Expected failures showed that:

- Official row clicks still toggled enabled state.
- Official changes saved immediately instead of remaining in a draft.
- TypeScript accepted the unknown official alias.
- Frontend merge keyed by raw IDs and let stale catalog values override fresh metadata.
- Official sorting was explicitly disabled.

### Frontend GREEN

After implementation the five new contracts passed. The first full run also exposed four existing source-extraction tests that depended on `reorderOfficialModels` retaining its `async` declaration. The declaration was preserved without reintroducing persistence, then verification passed:

```text
npm run test:ui-contract: 112 passed, 0 failed
npm run build: TypeScript and Vite build succeeded
```

### Gateway `/v1/models` RED

Command:

```powershell
python -m pytest tests/test_routing.py -q -k "current_catalog_data"
```

Result: `2 failed, 356 deselected`.

- Legacy and bare official IDs were both returned.
- Fast pseudo-model names still used the `OpenAI GPT-*` prefix.

### Gateway `/v1/models` GREEN

```text
2 passed, 356 deselected
```

## Final verification

- Focused/broader Python catalog, config, and routing:

  ```text
  424 passed, 72 subtests passed
  ```

- Full Python suite:

  ```text
  735 passed, 1 skipped, 86 subtests passed
  ```

- Full Rust suite:

  ```text
  232 passed, 0 failed
  ```

- Full frontend UI contract suite:

  ```text
  112 passed, 0 failed
  ```

- Frontend production build: passed.
- `git diff --check`: passed after each implementation slice and before commits.

## Files changed

- `tests/fixtures/model_identity_vectors.json`
- `src-python/catalog_sync.py`
- `src-python/codex_proxy.py`
- `tests/test_catalog_sync.py`
- `tests/test_routing.py`
- `src-tauri/src/config.rs`
- `src-tauri/src/models.rs`
- `src-tauri/src/gateway.rs`
- `frontend/src/lib/settings.ts`
- `frontend/src/pages/ProvidersPage.tsx`
- `frontend/src/i18n/locales/en-US.ts`
- `frontend/src/i18n/locales/zh-CN.ts`
- `frontend/scripts/ui-contract.test.mjs`
- `.superpowers/sdd/task-1-report.md`

## Self-review

- Confirmed canonicalization is conditional on a trusted known-official set; no global provider-prefix stripping was introduced.
- Confirmed custom provider IDs and provider-qualified third-party models are preserved.
- Confirmed raw App CLI records are cloned before normalized cache fields are overlaid, so newly added App metadata is not silently lost.
- Confirmed real official App records do not receive generic Ollama defaults.
- Confirmed Gateway client group/selector behavior remains provider-qualified while model IDs on `/v1/models` are bare.
- Confirmed frontend metadata merge processes catalog first and fresh App metadata second, preserves canonical insertion position, and ORs enabled state.
- Confirmed official Toggle and reorder handlers contain no save/catalog/sync calls; the Save handler owns the single persisted path.
- Confirmed titlebar and unrelated current UI improvements were not modified.
- Confirmed no TLS, keepalive, FlClash, v0.2, version, publishing, merge, or release work was included.

## Concerns

- `cargo fmt --check` remains nonzero because the branch already contains rustfmt differences outside this task, including `autostart.rs`, `proxy.rs`, and unrelated existing sections of `gateway.rs`/`models.rs`. Those files were not bulk-formatted to avoid unrelated churn. Required Rust tests and `git diff --check` pass.
