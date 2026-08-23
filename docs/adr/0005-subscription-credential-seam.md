# ADR-0005: SubscriptionCredential seam for upstream auth

Date: 2026-08-23
Status: Accepted for campaign #448 Stage 3B

## Context

Upstream authentication is implemented as enum hard-branches. `AuthenticationStrategy`
has `codex_auth`, `api_key`, `ollama_api_key`, `incoming`, and `unknown`.
`gateway_transport.build_upstream_headers()` switches on the auth-mode string.
`codex_auth.py` is a ChatGPT-only adapter (auth.json load, refresh, account
headers). Adding SuperGrok/XAI OAuth would otherwise add another enum value and
another transport branch. `ollama_api_key` is already a provider-special-case
precedent.

Campaign #448 requires: adding a Provider/credential type = adding one adapter;
no enum-branch edits in transport. XAI (#454) is the second adapter that proves
the seam.

## Decision

Introduce a `SubscriptionCredential` protocol as the sole seam for
refreshable, persisted subscription tokens:

- `access_token() -> str`
- `account_headers() -> Mapping[str, str]` (empty when the provider has none)
- `refresh() -> str`
- persistence is owned by the adapter (0600 atomic writes, file is the
  process-shared source of truth)
- errors use a small taxonomy: `auth-required`, `refresh-failed`, `not-eligible`
- provider binding is part of the seam: `register_provider_auth(provider_id,
  auth_mode, has_session)`. Catalog selects `auth_mode` only through
  `provider_auth_mode(provider_id)` and must not name a provider. A third
  subscription type is one adapter `register(...)` plus one
  `register_provider_auth(...)` — zero catalog or transport edits.

`codex_auth` becomes adapter `#1` behind this protocol. Its headers remain
byte-identical (`Authorization`, `Chatgpt-account-id`, plus the existing
session/thread/window/request identity materialization in transport).

`build_upstream_headers()` dispatches through the credential registry for
registered subscription modes. Unregistered simple strategies (`api_key`,
`ollama_api_key`, `incoming`) stay on the same dispatch function as explicit
non-subscription adapters, not sibling `elif` chains that grow per provider.

## Consequences

- Official-route request headers must remain byte-identical (characterization
  evidence).
- A new subscription type is one adapter module + one `register(...)` call.
- Non-subscription API keys are not forced through OAuth refresh machinery.
- Anthropic consumer OAuth remains out of product scope (ToS); this seam does
  not authorize it.
