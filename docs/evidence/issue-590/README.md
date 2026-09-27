# Issue #590 runtime readiness checks

This work adds explicit browser, login, connector, and model evidence for the
managed ChatGPT Web runtime. The `status --home <path>` action reads saved
evidence only; `check --home <path>` performs the browser checks and is meant
for an explicit user action. When #585 exposes these results in settings, call
`cached_checks(home)` from ordinary status reads and `check_runtime(home)` only
from the explicit check action. A settings save must not trigger a browser
check or wait for one.

Saved evidence is bound to hashes of the active config and account files and to
the runtime process identity. A binding change makes the cached evidence
unchecked. A later explicit failed or blocked check replaces the prior result
when the binding is available. If the runtime identity disappears, ordinary
reads report the cache as stale.
`checked_at` is informational; evidence has no arbitrary time-to-live, so a
long-running session does not require a periodic re-check. The explicit check
fails closed when the runtime cannot report active turn counts or either count
is non-zero, and it does not open or take over the runtime's existing browser
tab. It uses a temporary headless Chromium profile and an isolated CDP context
for the browser-helper operations.

The returned model list uses the same normalized shape as the runtime doctor:
`id`, `display_name`, `efforts`, and `image_input`. Only IDs prefixed with
`chatgpt-web/` are retained from `/v1/models`; the runtime's shared
`normalize_runtime_model` handles string or object reasoning levels and image
capability fields. This keeps readiness checks compatible with the existing
route and model picker.

## Verification and smoke evidence

The deterministic tests use synthetic config and account files. They cover
model normalization, cached-evidence binding, explicit busy-runtime behavior,
and the guarantee that saved evidence remains current while its account,
configuration, and process binding remain unchanged.

The pinned-helper negative control was run against the code-only Linux runtime
archive with SHA-256
`4c5fc30299820e078a9df4d1dc6bef725855d29c0981770335d421e1bd70f075` and
`/usr/bin/chromium`. Its isolated synthetic profile contained no cookies or
origins and an explicitly unauthenticated verification marker. The pinned
helper reported that the composer was unavailable; the sanitized result was
`browser_check_failed`. It did not report authentication success. This proves
only that the empty-profile negative control fails closed. It is not a
successful browser, login, connector, model, or real-account acceptance run.

No real account state was read or copied for this smoke, and no running
ChatGPT Web component was changed. Real-account acceptance remains separate
and must use the active managed account through an explicit check when that
environment is available.
