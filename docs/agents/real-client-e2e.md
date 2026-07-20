# Real-client E2E gate

`scripts/Run-RealClientE2E.ps1` is the release-only Windows gate for proving
that the candidate Debug build routes six pinned, real clients through both
canonical routes. An HTTP request, configuration inspection, or client launch
without the required terminal evidence is not an E2E pass.

## Pinned VM

Run only in snapshot `codexhub-real-client-e2e-v1`. The snapshot has a
dedicated Windows account and these exact installed versions:

| Client | Version |
|---|---|
| Codex Desktop | `26.715.4045.0` |
| Codex CLI | `0.144.5` |
| ZCode | `3.3.6` |
| OpenCode | `1.18.3` |
| Pi | `0.80.6` |
| OMP | `17.0.3` |

Before taking the snapshot, verify each installed version in its native About
screen or `--version` output. Do not upgrade a client in place. A version
change requires a new named snapshot and a runner update reviewed in the same
PR.

The VM account is used only for this gate. It has a dedicated Codex account,
dedicated Volc credentials, no personal client history, and no mounted host
home/configuration directories. Never run the gate from a developer's normal
Desktop, Codex, ZCode, OpenCode, Pi, or OMP session.

## Candidate and isolation

Use a new output directory for every candidate SHA. The runner requires this
layout before it launches any process:

```text
<output>/
  isolated/
    account/profile.json
    credentials/volc.json
    config/
      client-versions.json
    work/
  manual-evidence.json
```

`profile.json` identifies the dedicated test account to the VM operator.
`volc.json` contains only the dedicated Volc configuration expected by the
candidate. Their contents are never copied into artifacts; only SHA-256 hashes
appear in `summary.json`.

After checking the native About/`--version` surfaces, write
`client-versions.json` as one JSON object whose six keys and values exactly
match the pinned-version table (`desktop`, `codex_cli`, `zcode`, `opencode`,
`pi`, and `omp`). Extra, missing, or mismatched entries fail before launch.

Build the Debug executable from the exact candidate SHA and place a sidecar
next to it named `<DebugBuild>.candidate-sha`. The sidecar contains only the
lowercase 40-character candidate SHA. A changed candidate invalidates the
Debug build and every automated and manual evidence file.

Every child gets a cleared environment with case-local `HOME`, `USERPROFILE`,
`APPDATA`, `LOCALAPPDATA`, `CODEX_HOME`, `XDG_CONFIG_HOME`, `TEMP`, and `TMP`.
The runner passes only the candidate's isolated account, credential, config,
and work paths to the Debug build. It never copies or reads host shared
sessions.

## Matrix

The runner executes this fixed order:

| Case | Client | Canonical model | Finalization |
|---|---|---|---|
| `desktop-luna` | Codex Desktop | `gpt-5.6-luna` | human GUI |
| `desktop-volc` | Codex Desktop | `volc/glm-5.2` | human GUI |
| `codex-cli-luna` | Codex CLI | `gpt-5.6-luna` | automated |
| `codex-cli-volc` | Codex CLI | `volc/glm-5.2` | automated |
| `opencode-luna` | OpenCode | `codexhub-openai/gpt-5.6-luna` | automated |
| `opencode-volc` | OpenCode | `codexhub-volc/glm-5.2` | automated |
| `zcode-luna` | ZCode | `codexhub-openai/gpt-5.6-luna` | human GUI |
| `zcode-volc` | ZCode | `codexhub-volc/glm-5.2` | human GUI |
| `pi-luna` | Pi | `codexhub-openai/gpt-5.6-luna` | automated |
| `pi-volc` | Pi | `codexhub-volc/glm-5.2` | automated |
| `omp-luna` | OMP | `codexhub-openai/gpt-5.6-luna` | automated |
| `omp-volc` | OMP | `codexhub-volc/glm-5.2` | automated |

For every case, the operator/client must read its disposable `sentinel.txt`
exactly once with a read-only tool, stream the named sentinel exactly once,
select the exact canonical model, and produce exactly one completed terminal
and one `request_complete` with HTTP `200`. Any fallback, duplicate terminal,
error event, or unclassified reconnect fails the case.

The automated clients must expose normalized JSON lines to the gate's capture
adapter with `model_selected`, `tool_call`, `stream_delta`,
`request_complete`, and `terminal` events. This is real client output plus
candidate diagnostics, not an HTTP/configuration preflight. Raw stdout and
stderr are kept only in bounded memory, reduced to SHA-256, and never written
to disk.

## Human GUI evidence

Codex Desktop and ZCode require a logged-in human at the VM console. Remote
automation may launch the isolated GUI, but may not claim the result. The human
selects both models, performs the same read-only sentinel flow, checks the
candidate diagnostics, and finalizes `manual-evidence.json`.

The file has this schema and exactly four unique cases:

```json
{
  "schema": "codexhub.real-client-manual-evidence.v1",
  "candidate_sha": "<40-hex candidate>",
  "cases": [
    {
      "case_id": "desktop-luna",
      "client": "desktop",
      "canonical_model": "gpt-5.6-luna",
      "human_finalized": true,
      "outcome": "passed",
      "terminal_classification": "completed",
      "reconnect_classification": "none",
      "request_complete_count": 1,
      "http_status": 200,
      "read_only_tool_call_count": 1,
      "sentinel_chunk_count": 1,
      "fallback_count": 0,
      "duplicate_terminal_count": 0
    }
  ]
}
```

Repeat the object for `desktop-volc`, `zcode-luna`, and `zcode-volc` using the
matrix values above. Input order does not matter; the merge order is fixed by
the matrix. Missing login, credentials, GUI access, a case, human finalization,
or any contradictory metric fails closed. Duplicate cases, malformed JSON,
and a stale candidate SHA also fail before client execution.

Do not put a person's name, username, account identifier, request/session/task
ID, prompt, model response, credential, or absolute path in manual evidence.

## Operator workflow

1. Restore `codexhub-real-client-e2e-v1` and verify the six pinned versions.
2. Log in locally with the dedicated VM account and verify the dedicated Codex
   login and Volc credential without opening a shared host session.
3. Check out the candidate SHA, produce its Debug build, and create the exact
   SHA sidecar.
4. Create a new output layout, populate the isolated account/credential files,
   and prepare the four-case manual evidence from the GUI observations.
5. Run the gate from Windows PowerShell:

```powershell
powershell -NoProfile -File scripts/Run-RealClientE2E.ps1 `
  -CandidateSha <sha> `
  -DebugBuild <path> `
  -LunaModel codexhub-openai/gpt-5.6-luna `
  -VolcModel codexhub-volc/glm-5.2 `
  -OutputDirectory <path>
```

6. Confirm exit code `0`, `summary.json` outcome `passed`, all twelve case
   outcomes `passed`, and the SHA matches the PR head. Upload only
   `summary.json` and the files named by its `artifacts` array as the human VM
   artifact for that SHA. Never upload `isolated/` or `manual-evidence.json`.
7. If the PR head changes, discard the result and repeat from the Debug build.

The runner attempts a case once. It permits exactly one retry only when the
first attempt exits with a structured provider-capacity `429` or `503` before
any output sentinel. Timeouts, process failures, malformed/error output,
post-output capacity errors, fallback, duplicates, and reconnect ambiguity are
never retried.

## Artifact contract

There is exactly one `summary.json` for a completed run. It contains only:

- candidate and artifact SHA-256 hashes;
- pinned client versions and canonical model identifiers;
- bounded durations and event/case counts;
- terminal, reconnect, and retry classifications;
- case/run outcomes;
- relative artifact names.

Per-case artifacts contain the candidate SHA, canonical model, outcome, and
hashes of bounded captures. Neither summary nor per-case artifacts contain
credentials, authorization headers, prompts, non-sentinel model output,
usernames, account identifiers, absolute paths, or private
request/session/task IDs.
