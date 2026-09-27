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

The account binding uses the server-confirmed ChatGPT identity from the
same-origin `/api/auth/session` response. Only a SHA-256 `accountKey` derived
from its user and account IDs is returned by the browser helper; IDs, cookies,
and session tokens remain in the page. An `.identity.json` sidecar binds that
key to the SHA-256 of the exact saved storage-state bytes. A successful managed
turn refreshes both files after checking the current account, so cookie
rotation for the same account preserves readiness. A different account key,
missing attestation, or storage-state bytes that do not match the attested
digest invalidate cached readiness. The first explicit readiness check
establishes an attestation for existing login files.

## Runtime patch and smoke evidence

The companion runtime patch is
[`codex-chatgpt-web-control-contract.patch`](codex-chatgpt-web-control-contract.patch),
SHA-256
`6e89c190cd6d01126a3d64d52bf09cb6a5fde8f24361f37e3056f8d1729104e1`. It
applies the model repair, control contract, and account-identity attestation to
upstream `a13cd09950969f43e3b7e25c71fa43efaf5446c5` through
`93b8e6fc3eda8a81176964be87f8c7b8fc637a7f`, producing tree
`641caaf875fcf908dfaa919c242f24fdede26a69`.

The previous control-contract candidate's extracted Linux runtime archive
smoke passed with SHA-256
`ab118da7d08baae8d1cd8496a2951e6e613a8827ccdc411a11ba52ba36a62c1d`:
health returned 200, unauthenticated admin status returned 401, authenticated
admin status returned 200 with contract version 1 and five Web models, and the
control token was rejected by `/v1/models`. The paired Windows x64 runtime
archive has SHA-256
`780bbb9b63888379cc41c77ba5dc293a30d93d375a4d0c98af659629bb04ec0e`.
Both hashes belong to source revision `9c2892af646f36752cc131dedd90af6586e6e4ce`
and are superseded by the account-identity attestation change above. They are
not qualified artifacts for revision `93b8e6fc3eda8a81176964be87f8c7b8fc637a7f`;
paired source builds and their smoke checks are pending.

The focused readiness, connection, recovery, route, and runtime tests passed:
62 passed, 1 skipped. These tests use synthetic account and runtime data; no
real account state or managed runtime was used. The paired payloads are
prepared artifacts, not yet published by #588 or selected by the released
runtime pin. Until publication and pin update, a runtime missing
`/admin/status` remains blocked and readiness does not use the legacy model
endpoint as a fallback.

## Isolated account inspection — 2026-09-28

A separate read-only inspection reused the saved login state already copied
into the isolated runtime. The active runtime setting was `headed=true`, but
the readiness browser launcher always forced `--headless=new`. That launch
reached `chatgpt.com` on the temporary-chat path and remained on a
`Just a moment...` challenge page with no visible composer for 30 seconds; this
was not evidence of an expired login. With the browser opened in the configured
headed mode, a first attempt still found no composer, then the next inspection
confirmed the account and temporary-chat surface. After the launcher was
changed to honor the active `headed` setting, two consecutive inspections
confirmed the account and exposed the expected capability metadata.

These checks only inspected the logged-in page. They did not verify the
connector, submit a message, invoke a tool, or establish text/tool readiness.
The source login files and the separately edited settings page were not changed
or read by this diagnostic.
