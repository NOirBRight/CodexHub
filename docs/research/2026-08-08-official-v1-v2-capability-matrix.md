# Official Collaboration V1/V2 capability matrix

Issue: #369<br>
Candidate: `3faafe1ca8ed979cbaaa42aff21ffc78a7d6c1e8`<br>
Codex CLI/Desktop: `0.146.1` (source-contract floor `0.146.0`)<br>
Captured: `2026-08-08`

The machine-readable, sanitized evidence is
[`docs/evidence/issue-369/official-v1-v2-cli-matrix.json`](../evidence/issue-369/official-v1-v2-cli-matrix.json).
The bounded CLI evidence index is
[`docs/evidence/issue-369/README.md`](../evidence/issue-369/README.md).
Validate it with:

```powershell
py -3.13 scripts/validate_issue_369_matrix.py --candidate-sha 3faafe1ca8ed979cbaaa42aff21ffc78a7d6c1e8
```

Each `v1`/`v2` object retains the coarse lifecycle and terminal status, plus the
sanitized required phase fields `spawn_identity_kind`, `message_agent_message`,
`follow_up`, `wait_result`, `list_interrupt`, `stream_history`,
`restart_readback`, and `terminal_error_replayable`. Phase values are bounded to
`observed`, `not_observed`, or `not_applicable`; an unobserved phase is never
represented by an empty value or a guessed success. When an identity kind is
observed, it is limited to the protocol kinds `agent_id` and `task_path`; the
No returned identity, call ID, item ID, or task path is retained. V1's
follow-up and list/interrupt fields are `not_applicable` by protocol; V2's
list/interrupt field is `observed` only for the Luna evidence window, where
both operations were exercised.

## Decision rule

The selector is enabled only when the exact visible Official row has an
accepted `GO` verdict for both the requested V1 and V2 paths. A catalog value,
a native cache marker, or a model-name guess is not capability evidence. Hidden
and unknown rows are recorded but never become picker-visible. The selector is
model-scoped; it does not create a global switch and it never changes a
third-party row.

## Matrix

| Canonical model | Visibility | Managed baseline | V1 probe | V2 probe | Verdict | Selector |
| --- | --- | --- | --- | --- | --- | --- |
| `gpt-5.6-sol` | list | `v2` | CLI 0.146.1 did not expose the V1 native surface, terminal 200 | complete native spawn/list/follow-up/wait/list, terminal 200 | `UNQUALIFIED` | no |
| `gpt-5.6-terra` | list | `v2` | coarse spawn/send/wait/close observed; stream/history and restart/readback not observed | coarse spawn/list/send/follow-up/wait/list observed; interrupt, stream/history, restart/readback not observed | `PARTIAL` | no |
| `gpt-5.6-luna` | list | `v1` | complete spawn/send/wait/close, stream/history, restart/readback, and bounded terminal/error replay | complete spawn/list/send/follow-up/wait/list/interrupt, stream/history, restart/readback, and bounded terminal/error replay | `GO` | yes |
| `gpt-5.5` | list | `null` | no accepted baseline selection | full probe reported no native Collaboration surface | `UNQUALIFIED` | no |
| `gpt-5.3-codex-spark` | list | `null` | no accepted baseline selection | simple response only; no complete lifecycle evidence | `UNQUALIFIED` | no |
| `gpt-5.2` | absent/unknown | absent | not testable | not testable | `UNQUALIFIED` | no |
| `gpt-5.4` | list | `null` | no native Collaboration surface | no native Collaboration surface | `NO-GO` | no |
| `gpt-5.4-mini` | list | `null` | no native Collaboration surface | no native Collaboration surface | `NO-GO` | no |
| `codex-auto-review` | hidden/internal | internal | never exposed | never exposed | `UNQUALIFIED` | no |

The V1 and V2 probes used the exact selected model and an isolated Beta3
Gateway. Evidence retains only lifecycle phase names, terminal status, and
bounded event counts; it retains no prompt, reasoning, tool payload, opaque
identity, credential, or session data. A successful child result alone is not
accepted for `GO`: every required phase, including stream/history,
restart/readback, terminal/error, and replayability, must be observed. Luna
meets that bar; Terra remains `PARTIAL` and picker-ineligible.

## Required CLI reruns before `GO`

Run each missing phase against a fresh isolated catalog/runtime and retain only
the bounded fields listed above. The command shape is intentionally explicit;
the catalog used for each invocation must select the named version. Luna's
required phases are already captured in the linked evidence; these commands
remain the rerun shape for any future row promotion:

```powershell
codex exec --json --ephemeral --model gpt-5.6-terra `
  "Using the isolated V1 catalog selection, run spawn/send/wait/close, capture stream/history, restart, and read back the sanitized lifecycle; report only bounded phase statuses."
codex exec --json --ephemeral --model gpt-5.6-terra `
  "Using the isolated V2 catalog selection, run spawn/list/send/follow-up/wait/interrupt/list, capture stream/history, restart, and read back the sanitized lifecycle; report only bounded phase statuses."
codex exec --json --ephemeral --model gpt-5.6-luna `
  "Using the isolated V1 catalog selection, run spawn/send/wait/close, capture stream/history, restart, and read back the sanitized lifecycle; report only bounded phase statuses."
codex exec --json --ephemeral --model gpt-5.6-luna `
  "Using the isolated V2 catalog selection, run spawn/list/send/follow-up/wait/interrupt/list, capture stream/history, restart, and read back the sanitized lifecycle; report only bounded phase statuses."
```

Do not promote a row to `GO` or expose its selector until those reruns produce
`observed` for every applicable required field and the matrix is revalidated
against the exact candidate SHA.

## UI and persistence consequence

`frontend/src/lib/officialModels.ts` must consume only exact `GO` rows. Sol and
Terra retain their catalog V2 baselines; Luna retains its catalog V1 baseline.
Only Luna exposes the model-scoped selector in this candidate. Any override
remains keyed by the exact Official identity and must survive refresh, restart,
catalog regeneration, and session overlay writes; clearing it removes only
that model's override.

No selector is exposed for `UNQUALIFIED`, `NO-GO`, hidden, or stale rows. This
matrix does not authorize third-party Collaboration V2, fallback, or
cross-Provider execution; those remain later generic qualification work.
