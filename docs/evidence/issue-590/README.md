# Issue #590 runtime readiness checks

Readiness and provider-connection checks use the authenticated, versioned
`GET /admin/status` endpoint on the managed runtime's loopback port. Contract
version 1 reports whether the runtime accepts turns, active HTTP and browser
turn counts, account capabilities, and the account-visible Web models with
their effort and image-input metadata. The consumer validates the full shape
and fails closed for a missing, unsupported, or malformed contract; it does
not fall back to `/v1/models`.

`/admin/status` is a control endpoint authenticated with the managed runtime
control token. Native OpenAI-compatible `/v1/models` remains a separate
passthrough for client requests and does not establish readiness or expose the
control contract. The control token is rejected by that native passthrough.

An explicit readiness check compares the browser-observed account capabilities
with the active runtime's capabilities. A mismatch clears the cached model
routes and leaves readiness blocked until the runtime is reconciled. Only the
observed `not_authenticated` and `session_expired` results mark the account
signed out; other check failures remain unknown. Ordinary status reads consume
saved evidence only. The cache is bound to the active config, account state,
and runtime process identity; a binding change makes it stale. Checks refuse
to inspect the browser while the runtime is not accepting turns or has active
turns, and browser-helper operations use a temporary profile and isolated CDP
context.

## Runtime patch and smoke evidence

The companion runtime patch is
[`codex-chatgpt-web-control-contract.patch`](codex-chatgpt-web-control-contract.patch),
SHA-256
`50577ded0e5b012ec7ea893f98dcd1635705470c0f093e4cb192c9cadd638c35`. It
applies the control contract to upstream `a13cd09950969f43e3b7e25c71fa43efaf5446c5`
through `9c2892af646f36752cc131dedd90af6586e6e4ce`; `git apply --check`
passed at the base revision.

The extracted Linux runtime archive smoke passed with SHA-256
`ab118da7d08baae8d1cd8496a2951e6e613a8827ccdc411a11ba52ba36a62c1d`:
health returned 200, unauthenticated admin status returned 401, authenticated
admin status returned 200 with contract version 1 and five Web models, and the
control token was rejected by `/v1/models`. The paired Windows x64 runtime
archive has SHA-256
`780bbb9b63888379cc41c77ba5dc293a30d93d375a4d0c98af659629bb04ec0e`.

The focused readiness, connection, recovery, route, and runtime tests passed:
62 passed, 1 skipped. These tests use synthetic account and runtime data; no
real account state or managed runtime was used. The paired payloads are
prepared artifacts, not yet published by #588 or selected by the released
runtime pin. Until publication and pin update, a runtime missing
`/admin/status` remains blocked and readiness does not use the legacy model
endpoint as a fallback.
