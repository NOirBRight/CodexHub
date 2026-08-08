# Issue #278/#280 CLI protocol-controlled evidence

`scripts/qualify_beta3_protocol_cli.py` is a bounded, CLI-only runner for the
Beta3 tool-search and Code Mode gates. Each case creates a fresh temporary
`CODEX_HOME`, runtime `providers.toml`, model catalog, loopback upstream fixture,
Gateway process, and `codex exec --json --ephemeral` process. All endpoints are
loopback and the provider key is a fixed fixture sentinel.

The fixture returns a deterministic sequence. Explicit cases cover
client-owned `tool_search`, a discovered declaration and result, then the
Code Mode `shell_command -> apply_patch -> shell_command` workflow. No-hint
cases return ordinary text without a search call and classify the outcome as
`model_not_selected`. Native Responses and adapted Chat-tool routes each have
independent case state and request-scoped alias history.

Run with an exact reviewed Codex CLI binary:

```powershell
py -3.13 scripts/qualify_beta3_protocol_cli.py `
  --codex C:\path\to\codex.exe `
  --candidate-sha <40-hex-candidate-sha> `
  --output <new-isolated-output> `
  --case native_explicit,native_no_hint,adapted_explicit,adapted_no_hint `
  --timeout-seconds 180
```

The runner writes only `summary.json` using schema
`codexhub.issue278.cli-tool-search.v1`. A missing CLI, timeout, malformed
catalog, or route failure produces `qualification_status: not_run`/`failed`
with a stable classification; it never falls back to an authenticated
Provider run. Raw request/response bodies, prompts, headers, credentials,
workspace paths, and real IDs are not retained. `evidence_status` therefore
remains `observed_synthetic_upstream`, not authenticated-provider evidence.

Each observed case also carries a shape-only `provenance` ledger. Its trace
records the planner declaration, search/result, discovered declaration and
follow-up call/result, then the Code Mode
`shell_command -> apply_patch -> shell_command` call/result pairs. The history
section records request/response counts, wire protocol sequence, response shape
markers, and recomputable order/identity digests. The validator checks these
stages and recomputes every digest, so planner booleans alone cannot qualify a
forged summary.

Focused checks:

```powershell
py -3.13 -m pytest -q tests/test_beta3_protocol_cli_runner.py
```

The separate Issue #63 and #251 replay fixtures remain offline contract
checks. They do not upgrade this runner's result or claim authenticated
Provider qualification.
