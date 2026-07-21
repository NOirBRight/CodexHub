# Real-client E2E gate

`scripts/Run-RealClientE2E.ps1` is the release-only Windows gate for proving
that one candidate Debug build routes six pinned real clients through both
canonical routes. HTTP/configuration preflight alone is never an E2E pass.

## Pinned host environment and clients

Run on the dedicated Windows host environment `codexhub-real-client-e2e` with
a new output root, dedicated Codex login input, dedicated Volc credential, and
no reused host user session or client configuration. A VM or named snapshot is
not required. The runner verifies these native installed versions before
launching the candidate or a client:

| Client | Version | Version source |
|---|---:|---|
| Codex Desktop | `26.715.4045.0` | `OpenAI.Codex` AppX package identity and install location |
| Codex CLI | `0.144.5` | `--version` |
| ZCode | `3.3.6` | Authoritative Windows uninstall identity and install root |
| OpenCode | `1.18.3` | `--version` |
| Pi | `0.80.6` | `--version` |
| OMP | `17.0.3` | `--version` |

Codex CLI, OpenCode, Pi, and OMP must each emit exactly one normalized
three-part version token equal to the pin. Suffixes, four-part forms, and
mixed or repeated version output fail. Only ZCode permits the documented
executable build suffix.

Do not upgrade a client in place. In particular, OpenCode remains `1.18.3`;
issue #191 owns the future stable release containing upstream fix #37770. A
pin change requires a runner and host-environment review.

Desktop's passed executable must reside beneath the matching `OpenAI.Codex`
AppX `InstallLocation`. Its Chromium `ProductVersion` is not the Desktop
version authority. ZCode requires an authoritative HKLM uninstall entry whose
publisher is exactly `ZCode`, whose `DisplayVersion` is exactly `3.3.6`, and
whose display name is either `ZCode` or `ZCode 3.3.6`. A version suffix must
agree exactly with `DisplayVersion`. The runner prefers a valid absolute
`InstallLocation`. When it is absent, the runner derives the install root from
the authoritative absolute `DisplayIcon` and quoted `UninstallString` paths.
Every available source must resolve to the same existing root, and the passed
ZCode executable must reside beneath it. Relative, missing, ambiguous,
conflicting, or unbound metadata fails closed. The executable build suffix,
including `3.3.6.3198`, is accepted only when `DisplayVersion` remains exactly
`3.3.6`. Real E2E reads the installed metadata directly; operators must not
mutate the registry or supply the test-only metadata fixture.

The host-environment manifest is bound to the Windows MachineGuid without
recording the MachineGuid, machine name, username, account, or credential. It
has exactly this shape:

```json
{
  "schema": "codexhub.real-client-host-environment.v1",
  "environment": "codexhub-real-client-e2e",
  "machine_binding_sha256": "sha256:<hash of windows-machine-guid-v1:<lowercase MachineGuid>>"
}
```

## Candidate and isolated inputs

Use a new output directory for every invocation. Before launch it contains:

```text
<output>/
  isolated/
    account/
      profile.json
      auth.json
    credentials/
      volc.json
    config/
      gateway.json
      host-environment.json
```

`isolated/work` must not exist before invocation. The runner verifies the
output/isolation ancestors are canonical and non-reparse, rejects any stale or
linked work root, creates the directory once without `-Force`, and verifies it
is empty before any executable probe or launch.

`profile.json` contains only readiness assertions and no account identifier:

```json
{
  "schema": "codexhub.real-client-account.v1",
  "dedicated_account": true,
  "codex_login_ready": true,
  "gui_ready": true,
  "host_session_reused": false
}
```

`auth.json` is freshly materialized directly in this invocation's isolated
root; it is never discovered or copied from the current user's Codex home. It
must use `chatgpt` mode and contain non-empty access and refresh tokens.
`volc.json` has schema
`codexhub.real-client-volc.v1` and one non-empty `api_key`. `gateway.json` has
schema `codexhub.real-client-gateway.v1`, a loopback `listen_port`, and a
dedicated `gateway_client_key`. These secret-bearing inputs remain under
`isolated/` and are never uploaded.

Build the runnable, release-optimized Debug portable from the exact candidate
SHA. A plain `cargo build` or `src-tauri/target/debug/codexhub.exe` is not a
valid candidate: it uses the Tauri development URL and can display
`localhost` connection failure instead of the bundled frontend. From any
working directory, use an explicit repository root:

```powershell
powershell -NoProfile -File <repo>\scripts\build-windows-portable.ps1 `
  -Flavor debug `
  -RepoRoot <absolute-repo-root>
```

The executable is written beneath
`<repo>/output/portable/CodexHub_<version>_debug_portable_<sha8>/CodexHub.exe`
with adjacent `config`, `src-python`, and embedded `python` resources. Pass
that executable and create `<DebugBuild>.candidate-sha` containing only the
full lowercase candidate SHA. The runner rejects a resource-incomplete or
development-only build before GUI launch. Pass the machine-bound host manifest
with `-HostEnvironmentManifest`. A new SHA invalidates the build sidecar, run
binding, automated evidence, GUI evidence, review, and Actions result.

The runner materializes, rather than assumes, the actual consumed configs:

- candidate `CODEXHUB_RUNTIME_HOME/proxy/settings.json` and
  `proxy/config/providers.toml`;
- candidate `CODEXHUB_CODEX_TARGET_HOME/auth.json` and `config.toml`;
- case-local Codex `config.toml`/`auth.json`;
- OpenCode `XDG_CONFIG_HOME/opencode/opencode.json`;
- Pi `.pi/agent/settings.json` and `models.json`;
- OMP `.omp/agent/config.yml` and `models.yml`;
- ZCode catalog at the launched process's consumed
  `APPDATA/ZCode/model-providers/codexhub.json` path (isolated as
  `appdata/roaming/ZCode/model-providers/codexhub.json`), plus
  `.zcode/v2/bots-model-cache.v2.json` and `.zcode/v2/config.json`. The
  catalog and cache use production provider arrays, array-shaped models,
  `defaultKind`, and `source`; their endpoint roots differ. The v2 config uses
  its separate `provider.<id>.options` shape and object-shaped models.

Before launching Desktop or ZCode, the runner also requires the configured
loopback port to be unused, starts the candidate in a kill-on-close Windows Job
Object, and waits for a successful bounded `/health` response plus the isolated
diagnostics path. The startup wait is capped at 30 seconds (or the smaller
`-TimeoutSeconds` value). A missing Python lifecycle, listener, usable health
response, or diagnostics path fails before any GUI launch with a stable
`candidate_gateway_startup_failed_*` classification. The accompanying
`candidate-startup.json` contains only fixed booleans, a bounded duration, and
the classification—never raw process output, paths, PIDs, credentials, or
account data.

Provider protocol selection mirrors the production Gateway exports. Luna uses
the Responses endpoint (`@ai-sdk/openai`, `openai-responses`, and
`/responses`). The current Volc provider has no explicit or available upstream
format declaration, so it uses Chat Completions (`@ai-sdk/openai-compatible`,
`openai-completions`, ZCode `openai-chat-completions`, and
`/chat/completions`).

Every child receives a cleared environment with case-local `HOME`,
`USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `CODEX_HOME`, `XDG_CONFIG_HOME`,
`TEMP`, and `TMP`. The candidate receives the production-consumed
`CODEXHUB_RUNTIME_HOME`, `CODEXHUB_CODEX_TARGET_HOME`, Gateway key, and Volc
environment values. The runner never discovers, copies, or modifies host
shared sessions. Isolated inputs must be regular files under the invocation's
`isolated/` root; reparse points and hard links fail as host-session reuse.

## Matrix and measurement

The fixed case order and selectors are:

| Case | Client | Client selector | Gateway canonical route | Finalization |
|---|---|---|---|---|
| `desktop-luna` | Codex Desktop | `gpt-5.6-luna` | `gpt-5.6-luna` | human GUI |
| `desktop-volc` | Codex Desktop | `volc/glm-5.2` | `volc/glm-5.2` | human GUI |
| `codex-cli-luna` | Codex CLI | `gpt-5.6-luna` | `gpt-5.6-luna` | automated |
| `codex-cli-volc` | Codex CLI | `volc/glm-5.2` | `volc/glm-5.2` | automated |
| `opencode-luna` | OpenCode | `codexhub-openai/gpt-5.6-luna` | `openai/gpt-5.6-luna` | automated |
| `opencode-volc` | OpenCode | `codexhub-volc/glm-5.2` | `volc/glm-5.2` | automated |
| `zcode-luna` | ZCode | `codexhub-openai/gpt-5.6-luna` | `openai/gpt-5.6-luna` | human GUI |
| `zcode-volc` | ZCode | `codexhub-volc/glm-5.2` | `volc/glm-5.2` | human GUI |
| `pi-luna` | Pi | `codexhub-openai/gpt-5.6-luna` | `openai/gpt-5.6-luna` | automated |
| `pi-volc` | Pi | `codexhub-volc/glm-5.2` | `volc/glm-5.2` | automated |
| `omp-luna` | OMP | `codexhub-openai/gpt-5.6-luna` | `openai/gpt-5.6-luna` | automated |
| `omp-volc` | OMP | `codexhub-volc/glm-5.2` | `volc/glm-5.2` | automated |

Each case creates one disposable `sentinel.txt`. The client must use exactly
one successful read-only read tool, emit the named sentinel once, and finish
once. The pinned client parsers consume their real JSONL contracts:

- Codex CLI `0.144.5`: `thread.started`, `item.completed` command/agent
  items, and `turn.completed`; the read command must explicitly report
  `status = completed` and integer `exit_code = 0`;
- OpenCode `1.18.3`: `step_start`, completed `tool_use`, `text`, and
  the final `step_finish` whose reason is `stop`; the intermediate
  `tool-calls` finish is not a terminal;
- Pi `0.80.6` and OMP `17.0.3`: `tool_execution_end`, assistant
  `message_end`, and `agent_end`. The final assistant message must have
  `stopReason = stop`, no `errorMessage`, and exactly one later `agent_end`.
  `error`, `aborted`, `length`, missing/unknown reasons, error messages,
  contradictory ordering, and duplicate/missing agent ends fail closed.

OMP `17.0.3` is launched through its one-shot JSON interface as
`omp --print --mode json --model <selector> <prompt>`. It has no `run`
subcommand, and `--format` is not a supported launch flag.

Completed/final assistant messages prove the exact sentinel content but are
not relabeled as stream deltas. Streaming is proven separately from correlated
production Gateway `request_complete.is_stream = true` evidence for both the
tool request and final continuation; the sanitized case records
`streaming_request_count = 2`.

Client output does not prove routing. For each attempt, the runner reads only
new lines from the isolated Debug Gateway's
`proxy/codex-proxy-events.jsonl`, correlates all new client events and their
request-ID-linked metadata, and validates every observed model. Events are
never discarded for disagreeing with the expected model, and the selected
model comes from the actual final completion. One read tool normally causes
one tool-call request and one final continuation request. The summary therefore
records `gateway_request_count = 2` but counts exactly one final
`request_complete` with HTTP `200`. Any additional request start is an
unclassified reconnect. `upstream_protocol_fallback`, a missing or mismatched
model, missing/duplicate Gateway request completion, duplicate client terminal,
error, or malformed output fails the case. Actual contradictory models and
private request IDs are not copied into uploadable artifacts.

Raw client output and diagnostics remain in bounded memory. Per-case files
contain only capture hashes and approved fields.

## Human GUI phase

Completed GUI evidence must not exist when the runner starts. After all
preflight checks, the runner emits `manual-evidence.template.json`, starts the
isolated candidate, launches Codex Desktop and ZCode, and waits up to
`-ManualEvidenceTimeoutSeconds` for a new `manual-evidence.json`. A native GUI
that exits before finalization fails immediately.

The template uses schema `codexhub.real-client-manual-evidence.v2` and contains
the candidate SHA plus a random `run_binding_sha256`. At the host console, the
human confirms the dedicated login and GUI, performs both model cases in each
launched GUI, verifies the same tool/sentinel/Gateway diagnostics, then copies
the template to `manual-evidence.json` and changes only the observed fields:

```json
{
  "schema": "codexhub.real-client-manual-evidence.v2",
  "candidate_sha": "<candidate SHA>",
  "run_binding_sha256": "<unchanged template hash>",
  "login_confirmed": true,
  "gui_confirmed": true,
  "cases": [
    {
      "case_id": "desktop-luna",
      "client": "desktop",
      "canonical_model": "gpt-5.6-luna",
      "sentinel_relative_path": "isolated/work/gui-desktop/desktop-luna/sentinel.txt",
      "human_finalized": true,
      "outcome": "passed",
      "terminal_classification": "completed",
      "reconnect_classification": "none",
      "request_complete_count": 1,
      "http_status": 200,
      "read_only_tool_call_count": 1,
      "sentinel_chunk_count": 1,
      "streaming_request_count": 2,
      "fallback_count": 0,
      "duplicate_terminal_count": 0
    }
  ]
}
```

The file must contain exactly the four Desktop/ZCode cases. Preexisting,
missing, malformed, duplicate, contradictory, login-unconfirmed,
GUI-unconfirmed, stale-SHA, or stale-run-binding evidence fails closed. Do not
add a name, username, account identifier, prompt, model response, credential,
absolute path, or request/session/task identifier.

## Operator workflow

1. Use the dedicated `codexhub-real-client-e2e` Windows host environment. Do
   not open, inspect, copy, or modify any current user's Codex, ZCode,
   OpenCode, Pi, OMP, or provider session/configuration.
2. Create a fresh output root and directly materialize its machine-bound host
   manifest and dedicated Codex/Volc inputs. Do not use links or copy an
   existing host session, and do not create `isolated/work`.
3. Check out the candidate. Run the exact `build-windows-portable.ps1 -Flavor
   debug -RepoRoot <absolute-repo-root>` command above, select the resulting
   `_debug_portable_<sha8>/CodexHub.exe`, and write its full-SHA sidecar. Do not
   substitute a plain Cargo Debug executable.
4. Create the isolated input layout above. Do not create manual evidence yet.
5. From the host console, start the blocking runner:

```powershell
powershell -NoProfile -File scripts/Run-RealClientE2E.ps1 `
  -CandidateSha <sha> `
  -DebugBuild <path> `
  -LunaModel codexhub-openai/gpt-5.6-luna `
  -VolcModel codexhub-volc/glm-5.2 `
  -OutputDirectory <path> `
  -HostEnvironmentManifest <path-to-host-environment.json>
```

6. Wait for the template and both launched GUIs. Complete and finalize the four
   GUI cases as described above while the runner is waiting.
7. Confirm exit `0`, summary outcome `passed`, all twelve cases passed, and the
   SHA/run binding match. Upload only `summary.json` and the relative files in
   its `artifacts` list. Never upload `isolated/`, the template, or manual
   evidence.
8. Repeat the Debug build and entire run after any candidate SHA change.

The runner permits one retry only when the first attempt has a correlated
Gateway `request_error` status `429` or `503`, the client exited nonzero, and no
output of any kind occurred. Tool emission/execution, stream or terminal
events, malformed client output, or any prior completed Gateway request makes
the attempt ineligible. Every other failure is also ineligible.

## Sanitized artifact contract

Every invocation that reaches the runner body ends with exactly one
`summary.json`, including preflight, candidate startup, GUI, manual, and
unexpected automated failures. A thrown-path summary contains only a bounded
`failure_classification`, zero case counts, and no artifacts except the fixed
sanitized `candidate-startup.json` when portable-build or candidate-startup
diagnosis applies. Scoped partial case artifacts are removed before that
summary is written. Candidate and GUI processes share one kill-on-close Job
Object, followed by a five-second bounded fallback cleanup, so resistant or
expanding descendants cannot prevent failure-summary completion. A complete
matrix keeps one sanitized artifact per case and uses `failure_classification`
`none` or `case_failure`.

The success-summary top-level schema is exactly `schema`, `candidate_sha`,
`run_binding_sha256`, `outcome`, `failure_classification`, `hashes`,
`pinned_versions`, `canonical_models`, `counts`, `cases`, and `artifacts`.
Its `hashes` object contains exactly `debug_build`; fingerprints of the host
manifest, account profile/auth, Volc credential, Gateway configuration, and
manual evidence are forbidden. Failure summaries omit `run_binding_sha256`
and `hashes`. Summary/per-case content is otherwise limited to verified pins,
canonical model IDs, bounded timings and counts, classifications, outcomes,
and relative artifact names. It never contains credentials,
authorization headers, prompts, non-sentinel model output, usernames, account
identifiers, absolute paths, or private request/session/task IDs.
