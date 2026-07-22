# Real-client E2E gate

`scripts/Run-RealClientE2E.ps1` is the release-only Windows gate for proving
that one candidate Debug build routes six compatible real clients through both
canonical routes. HTTP/configuration preflight alone is never an E2E pass.

## Authoritative host and compatibility baselines

Run on the authoritative machine-bound local dedicated Windows host
environment `codexhub-real-client-e2e` with a new output root, dedicated Codex
login input, dedicated Volc credential, and no reused host user session or
client configuration. A VM or named snapshot is not required. The runner
verifies each native installed version against these compatibility floors
before launching the candidate or a client:

| Client | Minimum stable version | Version source |
|---|---:|---|
| Codex Desktop | `26.715.8383.0` | `OpenAI.Codex` AppX package identity and install location |
| Codex CLI | `0.144.5` | `--version` |
| ZCode | `3.3.6` | Authoritative Windows uninstall identity and install root |
| OpenCode | `1.18.4` | `--version` |
| Pi | `0.80.6` | `--version` |
| OMP | `17.0.3` | `--version` |

Codex CLI, OpenCode, Pi, and OMP must each emit exactly one normalized stable
three-part version token at or above the table's floor. Codex CLI `0.145.0` is accepted.
Suffixes, prereleases, four-part forms, unparseable output, and mixed
or repeated version tokens fail. Only ZCode permits its separately verified
numeric executable build suffix.

Do not install or upgrade a client during a qualification run. OpenCode
`1.18.4` is the minimum stable release because it contains the upstream
response-header-timeout fix from commit
`67caf894e0843ee370e72839e8265e483233479b`. The old `1.18.3` release and any
prerelease or ambiguous suffix fail closed; newer stable releases remain
subject to the complete parser, routing, terminal, streaming, retry, and
evidence matrix.

Desktop's passed executable must reside beneath the matching `OpenAI.Codex`
AppX `InstallLocation`. Its Chromium `ProductVersion` is not the Desktop
version authority. The four-part AppX version must be stable and at least
`26.715.8383.0`. ZCode requires an authoritative HKLM uninstall entry whose
publisher is exactly `ZCode`, whose stable three-part `DisplayVersion` is at
least `3.3.6`, and whose display name is either `ZCode` or `ZCode <actual
DisplayVersion>`. A display-name version must agree exactly with
`DisplayVersion`. The runner prefers a valid absolute
`InstallLocation`. When it is absent, the runner derives the install root from
the authoritative absolute `DisplayIcon` and quoted `UninstallString` paths.
Every available source must resolve to the same existing root, and the passed
ZCode executable must reside beneath it. Relative, missing, ambiguous,
conflicting, or unbound metadata fails closed. The executable build suffix,
including `3.3.6.3198`, is accepted only when its three-part prefix agrees with
the actual authoritative `DisplayVersion`. Real E2E reads the installed
metadata directly; operators must not mutate the registry or supply the
test-only metadata fixture.

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

Client configuration requires a second explicit executable parameter,
`-ManagedClientConfigBuild`, with an adjacent candidate-SHA sidecar matching
`-ManagedClientConfigSha`. This build must contain the merged Issue #194
`managed-client-config` CLI. For every fresh case root, the runner invokes all
three production verbs in order: `preview` in a fresh preview root, then
`apply` and `readback` in a separate fresh apply root. It accepts only the
bounded client ID, production selector, canonical model, route protocol,
relative target names, and successful apply/readback state. A malformed,
secret-bearing, absolute-path-bearing, contradictory, or non-zero response
fails closed before that client launches.

The candidate owns each opaque relative `target_names` value. The runner first
uses an exact safe path beneath the fresh apply root. If the candidate reports
only a basename and no exact file exists, the runner performs a bounded,
non-reparse traversal and accepts exactly one regular, single-link file with
that basename. Zero or multiple matches, traversal/rooted names, canonical
escape, reparse points or junctions, hard links, and an over-bound tree fail
closed. This lookup is generic: operators and the runner must not infer a
client-specific candidate source directory from the target name.

The runner first invokes the candidate's production `refresh-models` command
with the isolated `CODEXHUB_RUNTIME_HOME`, isolated
`CODEXHUB_CODEX_TARGET_HOME`, dedicated auth input, and the exact
version-verified Codex CLI path. This publishes the candidate-managed Official
catalog and resolved context budget without discovering or copying a host
catalog, session, or configuration. Operators must not seed this state by hand
or hard-code a context limit.

After `refresh-models` succeeds, the runner contract-probes the actual passed
`-ManagedClientConfigBuild` for Codex, OpenCode, ZCode, Pi, and OMP across both
Official and Volc selections. Each probe performs `preview`/`apply`/`readback`
in the final case-local root, passing the candidate-published Official catalog
via `--catalog-path` for any `openai/gpt-5.6-luna` selection. The verified roots
are then reused for the corresponding client launch. Thus the probe detects
candidate #194 CLI schema drift and verifies that the candidate-managed catalog
drives Official model resolution without a second materialization or host-state
fallback. Codex apply requires the six production fields
`gateway_lifecycle`, `message`, `mode`, `proxy_build`, `proxy_port`, and
`proxy_running`. `history_sync_status` and `history_sync_message` are the only
optional keys and may be omitted, null, or bounded safe strings; all other
unknown or missing-required keys fail closed.

The runner does not construct or parse Codex TOML, OpenCode JSON, Pi JSON, OMP
YAML, or ZCode catalog/cache/config schemas. It copies the production-applied,
readback-verified target bytes named by the seam into the same case-local paths
consumed by the launched client: Codex `.codex`, OpenCode
`XDG_CONFIG_HOME/opencode`, Pi `.pi/agent`, OMP `.omp/agent`, and ZCode's
isolated `APPDATA/ZCode/model-providers` plus `.zcode/v2`. Desktop and Codex
CLI share the Codex managed-client adapter. ZCode GUI processes therefore
consume only files originating from that case's #194 apply root; host ZCode
state is never read or reused. The runner owns only this path publication and
copies opaque verified bytes without reconstructing selectors, endpoints,
protocols, provider objects, or ownership markers.

The release `0.1.6` calibration build at
`cc9df197a709fb4c7548021819ecb8fa716ed664` predates #194. For that run,
`-DebugBuild` and its sidecar remain bound to `cc9df197...`, while
`-ManagedClientConfigBuild` must be built from the exact current Issue #190
candidate and `-ManagedClientConfigSha` must name that candidate. The baseline
never falls back to handwritten configuration. For the final candidate run,
the same exact portable candidate executable and SHA may be supplied for both
roles. The summary and human template record both bindings.

Before launching Desktop or ZCode, the runner also requires the configured
loopback port to be unused.

Pass the real Codex CLI executable to `-CodexCliPath`. Do not pass an
OpenCodex-style shim that locates another executable relative to the current
user's `%APPDATA%`: the runner intentionally replaces `%APPDATA%` with the
fresh case-local directory, so such host-state indirection fails closed rather
than weakening isolation.

After the `refresh-models` bootstrap, the runner starts the candidate in a
kill-on-close Windows Job Object and waits for a successful `/health` response
plus the isolated diagnostics path. Bootstrap and readiness share one 30-second
budget (or the smaller `-TimeoutSeconds` value); bootstrap does not receive a
second timeout window. A missing or stale Official catalog after `refresh-models`
is classified as `candidate_gateway_bootstrap_failed_context_budget`; other
bootstrap failures and timeouts use `candidate_gateway_bootstrap_failed` and
`candidate_gateway_bootstrap_timeout`. A missing Python lifecycle, listener,
usable health response, or diagnostics path after bootstrap fails before any
GUI launch with a stable `candidate_gateway_startup_failed_*` classification.
The accompanying `candidate-startup.json` contains only fixed booleans, a
bounded duration, and the classification—never raw process output, paths,
PIDs, credentials, or account data.

Provider protocol selection comes only from #194 preview/apply/readback. The
runner verifies those three results agree, then verifies the real Gateway
diagnostics agree with the returned canonical route. Under the current
production providers this yields Responses for Luna and Chat Completions for
Volc; the runner contains no endpoint root, SDK, or protocol-format generator.

Every child receives a cleared environment with case-local `HOME`,
`USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `CODEX_HOME`, `XDG_CONFIG_HOME`,
`TEMP`, and `TMP`. The candidate receives the production-consumed
`CODEXHUB_RUNTIME_HOME`, `CODEXHUB_CODEX_TARGET_HOME`, exact verified
`CODEXHUB_CODEX_PATH`, Gateway key, and Volc environment values. The runner
never discovers, copies, or modifies host shared sessions. Isolated inputs
must be regular files under the invocation's `isolated/` root; reparse points
and hard links fail as host-session reuse.

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
once. The compatibility-baseline client parsers consume their real JSONL
contracts; accepting a newer version never relaxes these shapes:

- Codex CLI `0.144.5`: `thread.started`, `item.completed` command/agent
  items, and `turn.completed`; the read command must explicitly report
  `status = completed` and integer `exit_code = 0`;
- OpenCode `1.18.4`: `step_start`, completed `tool_use`, `text`, and
  the final `step_finish` whose reason is `stop`; the intermediate
  `tool-calls` finish is not a terminal;
- Pi `0.80.6` and OMP `17.0.3`: `tool_execution_end`, assistant
  `message_end`, and `agent_end`. The final assistant message must have
  `stopReason = stop`, no `errorMessage`, and exactly one later `agent_end`.
  `error`, `aborted`, `length`, missing/unknown reasons, error messages,
  contradictory ordering, and duplicate/missing agent ends fail closed.

OMP's `17.0.3` compatibility baseline is launched through its one-shot JSON interface as
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

## Wall-clock supervision

Every runner invocation has two process levels. The parent supervisor assigns
the complete worker tree to a Windows kill-on-close Job Object before the
worker can reach client launch, redirects worker stdout/stderr to bounded files
instead of pipes, and enforces `-OverallTimeoutSeconds` (default `5400`, maximum
`7200`). Closing the Job terminates descendants even after an intermediate
parent exits. An outer timeout returns nonzero and replaces any partial summary
with one sanitized `automated_outer_timeout` summary plus
`runner-timeout.json`. That diagnostic contains only the bounded phase,
duration, total-process count, and active-process count.

Unattended pytest commands must also use the checked-in external watchdog. It
owns the pytest process tree with the same kill-on-close behavior, never
captures through inherited pipes, returns `124` on timeout, and emits only a
bounded phase/process-count message. For the Issue module use:

```powershell
python tests/fixtures/real_client_e2e/run-with-windows-watchdog.py --timeout-seconds 3600 -- `
  python -m pytest -q tests/test_real_client_e2e.py
```

For a required full Python run, use the same command with an explicitly
recorded bound appropriate to that suite, for example `--timeout-seconds 5400
-- python -m pytest -q`. Targeted unattended invocations use the same wrapper
with a smaller stated bound. Do not invoke unattended E2E pytest without this
outer watchdog.

## Human GUI phase

Completed GUI evidence must not exist when the runner starts. After all
preflight checks, the runner emits `manual-evidence.template.json`, starts the
isolated candidate, launches Codex Desktop and ZCode, and waits up to
`-ManualEvidenceTimeoutSeconds` for a new `manual-evidence.json`. A native GUI
that exits before finalization fails immediately.

The `-ManualEvidenceTimeoutSeconds` manual window is finite (default `900`,
maximum `3600`) and remains distinct from automated per-process timeouts. It is
still contained by the overall runner deadline; it never disables or extends
that outer deadline.

The template uses schema `codexhub.real-client-manual-evidence.v2` and contains
the candidate SHA, managed-client-config candidate SHA, and a random
`run_binding_sha256`. At the host console, the
human confirms the dedicated login and GUI, performs both model cases in each
launched GUI, verifies the same tool/sentinel/Gateway diagnostics, then copies
the template to `manual-evidence.json` and changes only the observed fields:

```json
{
  "schema": "codexhub.real-client-manual-evidence.v2",
  "candidate_sha": "<candidate SHA>",
  "managed_client_config_sha": "<materializer candidate SHA>",
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

1. Use the authoritative machine-bound local dedicated Windows host
   environment `codexhub-real-client-e2e`. Do not open, inspect, copy, or
   modify any current user's Codex, ZCode, OpenCode, Pi, OMP, or provider
   session/configuration.
2. Create a fresh output root and directly materialize its machine-bound host
   manifest and dedicated Codex/Volc inputs. Do not use links or copy an
   existing host session, and do not create `isolated/work`.
3. Check out the candidate. Run the exact `build-windows-portable.ps1 -Flavor
   debug -RepoRoot <absolute-repo-root>` command above, select the resulting
   `_debug_portable_<sha8>/CodexHub.exe`, and write its full-SHA sidecar. Use
   that exact candidate as `-ManagedClientConfigBuild`. For final-candidate
   qualification it may also be `-DebugBuild`; for the `0.1.6` calibration,
   keep the baseline Gateway build as `-DebugBuild` and pass the current
   candidate only as the materializer. A plain Cargo Debug executable is not
   valid for either role.
4. Create the isolated input layout above. Do not create manual evidence yet.
5. From the host console, start the blocking runner:

```powershell
powershell -NoProfile -File scripts/Run-RealClientE2E.ps1 `
  -CandidateSha <sha> `
  -DebugBuild <path> `
  -ManagedClientConfigBuild <candidate-portable-path> `
  -ManagedClientConfigSha <candidate-materializer-sha> `
  -LunaModel codexhub-openai/gpt-5.6-luna `
  -VolcModel codexhub-volc/glm-5.2 `
  -OutputDirectory <path> `
  -HostEnvironmentManifest <path-to-host-environment.json> `
  -OverallTimeoutSeconds 5400 `
  -ManualEvidenceTimeoutSeconds 900
```

6. Wait for the template and four case-local GUI launches (Desktop Luna/Volc
   and ZCode Luna/Volc). Each launch consumes its own #194-applied root.
   Complete and finalize the four GUI cases as described above while the
   runner is waiting.
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
diagnosis applies. Scoped partial case artifacts are excluded before that
summary is written. The outer worker Job, candidate/GUI Job, and bounded
fallback cleanup prevent resistant, expanding, detached, or missing-parent
descendants and inherited stdout/stderr handles from delaying failure-summary
completion. An outer timeout references only the fixed sanitized
`runner-timeout.json` diagnostic. A complete matrix keeps one sanitized
artifact per case and uses `failure_classification` `none` or `case_failure`.

The success-summary top-level schema is exactly `schema`, `candidate_sha`,
`managed_client_config_sha`, `run_binding_sha256`, `outcome`, `failure_classification`, `hashes`,
`pinned_versions`, `canonical_models`, `counts`, `cases`, and `artifacts`.
The legacy-named `pinned_versions` object contains the actual normalized versions
verified for this run, not the compatibility floors.
Its `hashes` object contains exactly `debug_build` and
`managed_client_config_build`; fingerprints of the host
manifest, account profile/auth, Volc credential, Gateway configuration, and
manual evidence are forbidden. Failure summaries omit `run_binding_sha256`
and `hashes`. Summary/per-case content is otherwise limited to verified versions,
canonical model IDs, bounded timings and counts, classifications, outcomes,
and relative artifact names. It never contains credentials,
authorization headers, prompts, non-sentinel model output, usernames, account
identifiers, absolute paths, or private request/session/task IDs.
