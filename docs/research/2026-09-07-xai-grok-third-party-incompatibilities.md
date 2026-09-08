# CodexHub × xAI Grok: third-party request/tool/schema incompatibilities

Date: 2026-09-07  
Scope: What Codex App / Codex CLI emit through CodexHub Gateway to xAI Grok (Chat Completions and Responses), which shapes xAI documents as rejected, which shapes CodexHub rewrites, and which other third-party providers share the same constraints. The incompatibility table is the evidence snapshot; **Implementation status** records what Gateway now does.

Live observation used as a pin, not as the whole catalog:

```text
HTTP 400 from source=xai
"__codexhub_ns_a5e9029afd_33: tool parameter root must be an object type (root schema is an anyOf/oneOf union with a non-object branch)"
```

## Evidence boundary

Claims below are limited to:

| Class | Sources used |
| --- | --- |
| What CodexHub sends | CodexHub Python Gateway code and tests cited by path |
| What xAI documents as accepted/rejected | [xAI Function Calling](https://docs.x.ai/developers/tools/function-calling), [Structured Outputs](https://docs.x.ai/developers/model-capabilities/text/structured-outputs), [Reasoning](https://docs.x.ai/developers/model-capabilities/text/reasoning), [Chat REST](https://docs.x.ai/developers/rest-api-reference/inference/chat), [WebSocket mode](https://docs.x.ai/developers/advanced-api-usage/websocket-mode), [Tool usage details](https://docs.x.ai/developers/tools/tool-usage-details), [Advanced usage / images](https://docs.x.ai/developers/tools/advanced-usage) (fetched 2026-09-07) |
| OpenAI tool wrapping | OpenAI OpenAPI `FunctionObject` / `ChatCompletionTool` / `FunctionParameters` in [api-definition.yaml](https://platform.openai.com/docs/static/api-definition.yaml) (fetched 2026-09-07), plus CodexHub `protocol_translation.py` which encodes the Responses ↔ Chat mapping the Gateway actually uses |
| Same error string elsewhere | GitHub issues that quote the identical xAI 400 text; labeled as third-party reproductions, not xAI-owned docs |
| OpenCode Go | CodexHub comments/tests that name OpenCode Go and the recursive-schema error string |

Not used as xAI constraints: SuperGrok-only stream bugs, unverified “defaults `parallel_tool_calls` off” claims, or any schema keyword xAI docs do not mention as rejected.

## Executive summary

Codex App talks to CodexHub on the local Responses wire (`config_overlay.build_provider_section` sets `wire_api = "responses"`). The bundled xAI provider is Responses-only (`config/providers.toml` `upstream_format = "responses"`). Namespace tools are flattened to opaque `__codexhub_ns_<token>_<n>` function tools; child `parameters` are copied unchanged. After that, Gateway runs a JSON Schema walk that rewrites boolean subschemas, inlines/`$ref`-breaks recursive refs, and (as of the 2026-09-07 follow-up) coerces **parameters roots** that are not `type: object` or an all-object union. The live xAI 400 (`__codexhub_ns_*` + root `anyOf`/`oneOf` with a non-object branch) is that documented constraint.

`PROXY_FEATURES` advertises `third-party-json-schema-type-array-guard`. No implementation of a type-array rewrite exists in Gateway Python. xAI structured-output docs **accept** `{"type": ["string", "null"]}` for nullable fields, so that flag is not an xAI requirement and must not be implemented as a global xAI sanitizer.

OpenCode Go is the provider CodexHub already sanitizes for boolean schemas and recursive `$ref`. Real-client E2E’s third-party leg remains OpenCode Go Muse Spark. Grok tool-root coverage is a separate always-on sanitizer plus optional live probes; it is not one of the eight CLI cases.

## Path: Codex App → Gateway → xAI

1. Codex App overlay forces the local provider off websockets and onto HTTP Responses (`src-python/config_overlay.py` `supports_websockets = false`, `wire_api = "responses"`).
2. Gateway compatibility (`gateway_compat.request.compatible_request_body`) builds a runtime tool plan. Namespaces without a native lifecycle become `ADAPT` / `namespace_function_adapter` (`tool_compatibility/dispositions.py`, `tool_compatibility/plan.py`).
3. Each child is re-emitted as a plain function named `__codexhub_ns_<10-hex>_<ordinal>` (`tool_compatibility/registry.py`). `provider_function_declaration` copies the child mapping, sets `type`/`name`, and pops `namespace` — **parameters and `strict` stay as the client sent them** (`tool_compatibility/contracts.py`).
4. Third-party `tool_choice` other than `auto` is forced to `auto` (`gateway_compat/request.py`).
5. Unless a native V2 namespace is kept, Gateway runs `_normalize_transparent_tool_schema_booleans` (`gateway_compat/official_passthrough.py`, also from `gateway_exchange.py` on TRANSPARENT and GATEWAY_COMPATIBILITY).
6. If the inbound payload has Chat `messages`, Responses-shaped tools are wrapped as Chat Completions `{type, function:{...}}` (`_wrap_chat_function_tools`). The bundled xAI route does not use Chat Completions (`available_upstream_formats = ["responses"]`).
7. Encrypted reasoning is stripped for third-party (`gateway_request.sanitize_third_party_reasoning_items`; xAI fixture in `tests/test_third_party_reasoning_request.py`).

The live error names `__codexhub_ns_a5e9029afd_33`: that is Gateway’s namespace alias (10-character token + ordinal 33), not a Codex native tool name. xAI is rejecting the **flattened child schema**, and it names the alias in the 400.

Independent reproduction of the same xAI sentence, with a concrete schema, is [CLIProxyAPI #4343](https://github.com/router-for-me/CLIProxyAPI/issues/4343): Codex Desktop `codex_app` / `automation_update` flattened to a function whose `parameters` root is `oneOf: [ {type: object, ...}, {type: null} ]`. That matches xAI’s documented warning and the live CodexHub wording. This note does **not** claim the live alias `_33` is that specific child; it claims the rejected shape class is the same.

## OpenAI vs xAI tool envelopes (what Codex App emits vs what xAI examples use)

OpenAI Chat Completions tools are `{type: "function", function: FunctionObject}` (`ChatCompletionTool` in the OpenAPI spec). `FunctionObject.parameters` is `FunctionParameters`, described as “a JSON Schema object”; `strict` is optional and, when true, restricts the schema subset.

OpenAI Responses function tools, as CodexHub translates them, are flat: `{type, name, description, parameters, strict}` (`protocol_translation.responses_tools_to_chat_tools` / `chat_tools_to_responses_tools`). Named `tool_choice` is `{type, name}` on Responses and `{type, function: {name}}` on Chat.

xAI Function Calling examples for `/v1/responses` use the **flat** Responses shape (`type` / `name` / `parameters` on the tool). xAI’s `tool_choice` table also documents the **Chat** named form `{"type": "function", "function": {"name": "..."}}`. Chat REST says tools are functions (and, in some Chat objects, functions plus web search), max **128** tools. Function Calling’s schema table says max **200** tools per request. CodexHub does not currently cap either.

xAI does not document a `namespace` tool type. CodexHub therefore must flatten namespaces before xAI, which is what produces `__codexhub_ns_*`.

## Table of incompatibilities

Legend for “Who rejects”: **xAI-doc** = official xAI docs; **xAI-live** = CodexHub live 400; **xAI-gh** = GitHub quote of the same xAI error; **OpenCode Go** = CodexHub comment/test; **OpenAI-spec** = OpenAPI / CodexHub translator. Empty “Who rejects” means no primary source found that this provider 400s the shape.

| Shape | Who rejects it | CodexHub current behavior | Recommended Gateway rewrite |
| --- | --- | --- | --- |
| Tool `parameters` root is scalar, array, or not `type: object` | **xAI-doc**: root must be `"type": "object"`; nest other types under `properties`. 400 names the tool. | **Implemented** in `coerce_tool_parameter_root`. Missing `type` becomes `"object"`; scalar/array roots and impossible roots (`false`, `{not:{}}`) are rejected with `unsupported_tool_parameter_root`; unconstrained roots become objects. Outer nullable type arrays are restricted to object before union handling. No argument wrapper is introduced. | Keep. |
| Root `anyOf` / `oneOf` whose branches are all objects | **xAI-doc**: allowed (“union of objects”). | Left intact (normalizer only walks into the list; it does not flatten unions). | Keep. |
| Root `anyOf` / `oneOf` with a **non-object** branch (`null`, scalar, array, typeless `{required: [...]}`) | **xAI-doc** (warning quotes this); **xAI-live** (exact sentence); **xAI-gh** CLIProxyAPI `oneOf: [object, {type: null}]`; **xAI-gh** oh-my-pi exclusive-required `anyOf` of `{required: …}` only | **Implemented** at the parameters **root only**. Non-object branches are dropped; a single remaining object becomes the root; several remaining objects stay a union. Exclusive-required `{required: [...]}` branches are kept and annotated `"type": "object"`. Nested unions are not flattened. | Keep. |
| Nested `anyOf` / `oneOf` (property-level) | xAI structured outputs **support** `anyOf` / `oneOf` (oneOf ≡ anyOf). No doc that nested unions 400. | Preserved (test keeps `anyOf: [{type: number}]`). | Do not flatten nested unions. |
| `type: ["string", "null"]` and other type arrays | **xAI-doc: accepted** for nullable fields. No CodexHub test that OpenCode Go rejects type arrays. | **Advertised, not implemented.** `PROXY_FEATURES` lists `third-party-json-schema-type-array-guard`; grep of `src-python/` finds only that string. Collaboration V2 contracts still emit `NULLABLE_STRING = {"type": ["string", "null"]}`. | Do **not** rewrite type arrays for xAI. If a guard is still needed for another vendor, implement it behind a provider predicate, not as a global xAI sanitizer. |
| JSON Schema boolean `true` / `false` as a **property schema** | **xAI-doc**: rejected schemas include “Properties with a schema of `true` or `false`” (400). **OpenCode Go**: CodexHub tests/comments treat boolean schemas as third-party-unsafe. | **Implemented.** `true` → `{}`, `false` → `{"not": {}}` in `properties`, `$defs`, combinators, `not`, `contains`, etc. Boolean applicators (`additionalProperties`, `items`, …) stay booleans. Event `tool_schema_boolean_normalized`. | Keep. Root `parameters: true`/`false` now become `{type: object, properties: {}}` after the root coerce. |
| `$ref` / `$dynamicRef` (local, non-circular) | **xAI-doc**: `$ref` / `$defs` supported, non-circular only. | Inlined via JSON pointer. Leftover `$ref` stripped; empty leftover becomes `{type: object}`. | Keep inline for xAI (circular would be rejected). |
| Recursive / circular `$ref` | **xAI-doc**: “non-circular references only”. **OpenCode Go**: comment + test: `"Recursive JSON schemas are not currently supported"`. | Recursive ref replaced with leftover or `{type: object}`. Test `test_normalize_tool_schema_breaks_recursive_refs_for_opencode`. | Keep. Shared xAI + OpenCode Go rewrite. |
| `additionalProperties: false` / `true` (boolean keyword) | **xAI-doc**: `additionalProperties` defaults to `false` and must be set `true` explicitly. Not listed under Rejected schemas. **OpenCode Go live (OMP 17.0.3 `strict` read)**: `{not: {}}` in this position 400s; boolean `false`/`true` is accepted. | Preserved as booleans. Object-valued `additionalProperties: {type: string}` still walks as a schema. Same for `items` / `unevaluatedProperties` / `additionalItems` / `unevaluatedItems`. | Keep booleans here. Continue rewriting boolean **property** schemas. |
| `strict` on tools | **xAI-doc**: tool arguments “strict flag is implicitly always `true`”. OpenAI: optional `strict`; strict mode uses a JSON Schema subset. | Copied on Chat wrap and on Responses ↔ Chat translation. Namespace flatten copies child `strict`. No strip for xAI. | No rewrite required for presence of `strict`. Do not set `strict: true` on a schema xAI cannot compile (root union with `null`). If a rewrite of a union drops `null`, leave `strict` as the client sent it. |
| Responses-native tools vs Chat `function` wrapping | **OpenAI-spec**: Chat requires nested `function`. **xAI-doc**: Responses examples are flat; Chat REST is function tools; `tool_choice` table uses Chat nested named choice. | xAI bundled route is Responses-only — no wrap. Wrap runs only when payload has `messages`. Translation cannot map `namespace` / `web_search` / `tool_search` to Chat (`Cannot translate Responses tool type …`). | For xAI Responses: keep flat tools. For any future xAI Chat Completions attempt: run `_wrap_chat_function_tools` after flatten. Do not send `type: namespace` upstream. |
| Namespaced `__codexhub_ns_*` tools | xAI has no `namespace` type. Alias names themselves are not documented as rejected. **xAI-live** names the alias in the 400 because **that function’s parameters** failed compilation. | Flatten + inverse-map. Max alias length 128. Ordinal is unbounded per request (not capped by `max_alias_attempts`). | Keep flatten. Apply root-schema rewrite **after** flatten so the alias xAI names is a compilable object schema. |
| `reasoning` / `effort` | **xAI-doc** (`grok-4.6`): `reasoning.effort` `low`/`medium`/`high` (default `high`)/`xhigh`; cannot disable. `presencePenalty` / `frequencyPenalty` / `stop` error on reasoning models. Encrypted reasoning via `include: ["reasoning.encrypted_content"]`. Chat REST also documents `reasoning.effort` (page text still mentions `grok-4.3` in one field blurb). | Grok family keeps `reasoning_effort` (`maintained_catalog.thinking_payload`). Encrypted blobs stripped for third-party. Official-style `include` dropped in the xAI history fixture. No CodexHub strip of `presence_penalty` / `frequency_penalty` / `stop` specifically for xAI. | Keep effort. Keep stripping encrypted content (xAI can return it; Codex App history is not xAI-encrypted). If Codex ever sends `presence_penalty`/`stop` on Grok, strip those — documented xAI 400, not yet seen in CodexHub tests. |
| `parallel_tool_calls` | **xAI-doc**: parallel calling enabled by default; disable with `parallel_tool_calls: false`. Copied through protocol translation. | No xAI-specific rewrite. | No rewrite from official docs. Do not treat third-party “SuperGrok defaults off” reports as an xAI constraint. |
| `tool_choice` | **xAI-doc**: `auto` / `required` / `none` / named function. | Third-party: anything other than `None`/`auto` becomes `auto` (except native V2 namespace). Tests assert this. | Policy is CodexHub’s, not an xAI 400. Named `tool_choice` never reaches xAI on this route. |
| Image / vision | **xAI-doc**: images in tool-enabled conversations (Advanced usage). Grok family `input_modalities` includes `image`. | Translator maps Responses `input_image` + `image_url` string ↔ Chat `image_url`. `file_id` images fail closed. | No xAI-specific image rewrite found. Unverified: Codex App image parts through Gateway to Grok. |
| WebSocket `/v1/responses` | **xAI-doc**: first-class WS mode, same create body, 25-minute cap. | Overlay sets Gateway `supports_websockets = false`. GET `/v1/responses` + Upgrade is local-probe rejected. Codex App → Gateway is HTTP. | Not an xAI schema 400. A Grok E2E over WS is out of scope until overlay allows it. |

## What the schema walk does and does not do

`_normalize_tool_json_schema` (`official_passthrough.py`):

- Boolean **property / combinator** schema nodes → `{}` / `{"not": {}}`
- Boolean JSON Schema applicators (`additionalProperties`, `items`, …) stay `true` / `false`
- Local `#…` `$ref` / `$dynamicRef` inline; recursion → open object
- Walks `properties`, `$defs`/`definitions`, combinators, schema-valued `additionalProperties` / `items`, …

`_coerce_tool_parameter_root` is now `coerce_tool_parameter_root` in
`gateway_compat/tool_parameter_root.py` (called from the boolean/`$ref` walk,
**root only**):

- Parameters root becomes `type: object`, or an all-object `anyOf`/`oneOf`
- Non-object union branches are dropped; exclusive-required branches get `"type": "object"`

Still not implemented (despite the health-feature name):

- Coercing nested JSON Schema **type arrays** (intentionally: xAI allows them)
- Changing `strict`
- Unwrapping Chat vs Responses tool envelopes (that is `_wrap_chat_function_tools` / `protocol_translation`)

`_wrap_chat_function_tools` only copies `parameters` when it is already a `dict`. A boolean parameters root is still coerced on the Responses/flat path before Chat wrap.

## xAI JSON Schema subset (tools share structured-output rules)

From Structured Outputs (tools “follow the same JSON Schema support rules”):

**Supported:** `string`, `number`, `integer`, `boolean`, `null`, `enum`, `const`, `array`, `object`, `anyOf`, `oneOf` (same as `anyOf`), `allOf` (single subschema guaranteed), `$ref`/`$defs` (non-circular), type arrays for nullability.

**Rejected (400):** `enum`/`anyOf` with zero variants; **properties whose schema is `true`/`false`**; `maxContains`/`minContains`; `items` as an array (use `prefixItems`).

**Best-effort (accepted, not structurally enforced):** `not`, `if`/`then`/`else`, multi-`allOf`, extra `format` values.

CodexHub’s property-schema `false` → `{"not": {}}` lands in a keyword xAI accepts as best-effort. Boolean applicators such as `additionalProperties` stay booleans so OpenCode Go does not 400 OMP `strict` tools.

## Other third-party overlap

| Provider | Shared with xAI | CodexHub already handles | Distinct |
| --- | --- | --- | --- |
| OpenCode Go | Boolean property schemas; recursive `$ref` | Both, in the same normalizer; recursive test names OpenCode; boolean `additionalProperties`/`items` preserved | Recursive error string is OpenCode Go’s; not found in xAI docs. OpenCode Go E2E is Muse Spark, not Grok. OpenCode Go `upstream_format = auto` (Chat + Responses). OMP 17 `strict` read 400s `{not: {}}` in `additionalProperties`. |
| Ollama Cloud | Not schema-root unions | Reasoning effort aliases; Chat wrap | Different thinking controls |
| Command Code | Not schema-root unions | Optional live E2E with DeepSeek V4 Flash | Not xAI |
| OpenAI Chat/Responses | Accepts object-root exclusive-required `anyOf` (oh-my-pi contrast; not re-tested here) | Official passthrough does not run the third-party schema walk | Official route is not this sanitizer |

## E2E gap

Current coverage:

| Gate | What it proves | What it does not prove |
| --- | --- | --- |
| `docs/agents/real-client-e2e.md` | Eight CLI combinations: Codex CLI, OpenCode, Pi, and OMP × Official Luna and **OpenCode Go** `muse-spark-1.2-contributor`. Third-party credential is `opencode-go.json`. Windows preflight also runs `scripts/e2e_xai_grok_tools.py` with live xAI unset. | No xAI / Grok 4.6 **CLI client** case. Ollama prohibited. Desktop/ZCode GUI retired on Windows. |
| `tests/test_live_opencode_commandcode_e2e.py` | Live Gateway + Muse Spark / Command Code DeepSeek; collaboration V2 children use **object** parameter schemas from `EXPECTED_PARAMETER_SCHEMAS`. | No root `oneOf`+`null`. Not Grok. |
| `tests/test_third_party_reasoning_request.py` `test_live_gateway_accepts_sanitized_xai_codex_app_history` | Optional `CODEXHUB_LIVE_GATEWAY_CONFIG` probe: sanitized Codex App **history** (compaction, encrypted reasoning, apply_patch, V2 namespace) returns HTTP 200. Skip via `CODEXHUB_SKIP_LIVE_XAI_E2E`. Documented in `docs/agents/ci.md`. | Tools in the fixture are V2 children (object schemas) plus `shell`. **Does not send** a `oneOf`/`null` parameters root. Does not prove a full Codex App editor tool list (including `codex_app` children). |
| Unit tests in `tests/test_chat_completions_gateway.py` | Boolean/`$ref` rewrite; root `anyOf`/`oneOf`+null coerced to object; exclusive-required branches annotated; nested unions and type arrays left in place; xAI Responses fixture for `__codexhub_ns_*`; HTTP inverse-map of a Codex App `codex_app` namespace `function_call`. | Full Codex App editor tool list in a live Desktop session. |
| `tests/test_third_party_reasoning_request.py` `test_compatible_request_rewrites_xai_root_union_and_keeps_nested_unions` | Adapter path: illegal root union becomes `type: object`; nested `anyOf` and `["string","null"]` survive. | Live HTTP. |
| `test_live_gateway_accepts_xai_root_union_tools` | Optional `CODEXHUB_LIVE_GATEWAY_CONFIG`: same illegal root union through a running Gateway. | Skip without the explicit config. |
| `scripts/e2e_xai_grok_tools.py` | Always-on sanitizer plus flatten/inverse-map of a `codex_app` namespace child. `CODEXHUB_E2E_XAI=1` POSTs the sanitized Responses body to `https://api.x.ai/v1/responses`. | Not part of the eight-case CLI gate. Live POST does not prove inverse-map (that is the always-on adapter/HTTP tests). |

A Grok E2E that would close the live 400 must prove, through Gateway to `https://api.x.ai/v1` Responses:

1. ~~Codex App or a fixture that includes at least one flattened namespace child whose **raw** parameters root is `oneOf`/`anyOf` with a `null` (or other non-object) branch.~~ Covered by Gateway fixtures and `scripts/e2e_xai_grok_tools.py`.
2. ~~Upstream request after Gateway: that tool’s `parameters` root is `type: object` or a union of objects only.~~ Covered.
3. ~~HTTP 200 from xAI (not `invalid_client_tool_schema` / “tool parameter root must be an object type”).~~ Optional live script; observed 200 against this machine’s xAI session on 2026-09-07.
4. Inverse-map: a model `function_call` to that alias still round-trips to the original namespace/child for Codex App. Covered by `test_xai_codex_app_namespace_alias_inverse_maps_function_call`, SSE, HTTP (`test_xai_codex_app_http_inverse_maps_namespace_function_call`), and `scripts/e2e_xai_grok_tools.py`.
5. ~~Nested property `anyOf` and `{"type": ["string", "null"]}` still present.~~ Covered by always-on tests.
6. ~~Exclusive-required root `anyOf` annotated as object branches.~~ Covered by always-on tests.

The remaining editor-path gap is a full Codex App tool list in a live Desktop session, not the schema 400 or alias inverse-map.

## Implementation status (2026-09-07)

Gateway now rewrites tool `parameters` / `function.parameters` / `input_schema`
**roots** in `gateway_compat/tool_parameter_root.py` `coerce_tool_parameter_root`,
on the same walk as the boolean / `$ref` sanitizer:

- Root `anyOf`/`oneOf` with a non-object branch: drop those branches; a single
  remaining object becomes the root; several remaining objects stay a union.
- Exclusive-required `anyOf` of `{required: [...]}`: keep the union and set
  each branch `"type": "object"`.
- Object-only unions and nested `anyOf` / `{"type": ["string", "null"]}`: left
  intact.
- Boolean parameter roots become `{type: object, properties: {}}`.

Always-on tests: `tests/test_chat_completions_gateway.py` (Chat + xAI
Responses + HTTP inverse-map), `test_xai_codex_app_namespace_alias_inverse_maps_*`,
and `test_compatible_request_rewrites_xai_root_union_and_keeps_nested_unions`.
Optional live: `test_live_gateway_accepts_xai_root_union_tools` and
`scripts/e2e_xai_grok_tools.py` (`CODEXHUB_E2E_XAI=1`). The eight-case
Official+OpenCode Go CLI gate is unchanged.

## Highest-priority Gateway rewrites

Items 1–3, 5–8 are implemented; item 4 remains “do not implement” on purpose.

1. **Root union sanitizer (done):** `coerce_tool_parameter_root` after flatten, on the boolean/`$ref` walk.
2. **Root type object (done):** scalar/array and impossible roots fail explicitly; unconstrained roots become objects. Object-union projection preserves outer constraints and normalizes outer nullable types.
3. **Keep the existing boolean + recursive `$ref` walk (kept).**
4. **Do not implement type-array stripping for xAI (still a no-op, intentionally).**
5. **Do not recursively flatten nested unions (tests assert this).**
6. **Same path as today’s schema walk, including `__codexhub_ns_*` (done).**
7. **Leave `strict`, `parallel_tool_calls`, and `reasoning.effort` alone for Grok 4.6 (unchanged).**
8. **E2E (partial):** always-on sanitizer + inverse-map + optional live Responses POST. Eight-case CLI gate unchanged. A full Desktop editor tool list is still a manual/live gap.

## Source pins (file + line / URL)

- Live alias shape: `src-python/tool_compatibility/registry.py` `_NAMESPACE_ALIAS_PREFIX`, `_allocate` (`__codexhub_ns_{token}_{ordinal}`).
- Flatten copies child schema: `src-python/tool_compatibility/contracts.py` `provider_function_declaration`.
- Namespace ADAPT: `src-python/tool_compatibility/dispositions.py` `namespace_function_adapter`; `src-python/tool_compatibility/plan.py` encode loop.
- Schema walk: `src-python/gateway_compat/official_passthrough.py` `_normalize_tool_json_schema` (boolean, `$ref`, combinator walk) and `src-python/gateway_compat/tool_parameter_root.py` `coerce_tool_parameter_root` (object / all-object union at the tool root).
- When it runs: `src-python/gateway_compat/request.py` (third-party, non-native-V2); `src-python/gateway_exchange.py` TRANSPARENT and GATEWAY_COMPATIBILITY.
- Chat wrap: `src-python/gateway_compat/request.py` `_wrap_chat_function_tools`.
- `tool_choice` forced `auto`: `src-python/gateway_compat/request.py`.
- Feature flag only: `src-python/codex_proxy.py` `PROXY_FEATURES` `third-party-json-schema-type-array-guard`.
- Type-array still emitted: `src-python/collaboration_runtime_contract.py` `NULLABLE_STRING`.
- Tests: `tests/test_chat_completions_gateway.py` boolean + recursive `$ref` + root union; `tests/test_third_party_reasoning_request.py` xAI history and root-union adapter/live probes; `scripts/e2e_xai_grok_tools.py`; `tests/test_runtime_tool_compatibility_integration.py` `tool_choice == auto`.
- xAI provider Responses-only: `config/providers.toml` `[providers]` `id = "xai"`.
- Overlay no WS: `src-python/config_overlay.py` `supports_websockets = false`.
- Local WS reject: `src-python/codex_proxy.py` GET `/v1/responses`.
- xAI Function Calling (root object / union of objects / 400 warning): https://docs.x.ai/developers/tools/function-calling
- xAI Structured Outputs (type arrays allowed; boolean property schemas rejected; `$ref` non-circular; `strict` implicit): https://docs.x.ai/developers/model-capabilities/text/structured-outputs
- xAI Reasoning (`grok-4.6` effort including `xhigh`): https://docs.x.ai/developers/model-capabilities/text/reasoning
- Same 400 + `oneOf`+`null` schema: https://github.com/router-for-me/CLIProxyAPI/issues/4343
- Same error family + exclusive-required `anyOf`: https://github.com/can1357/oh-my-pi/issues/8698
- Real-client E2E third-party = OpenCode Go: `docs/agents/real-client-e2e.md`
