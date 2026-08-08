# Official Collaboration V1/V2 capability matrix

Issue: #369  
Candidate: `c115551a2246e141a0a6e33c41c9ae40bd02be73`  
Codex CLI/Desktop: `0.146.1` (source-contract floor `0.146.0`)  
Captured: `2026-08-08`

The machine-readable, sanitized evidence is
[`docs/evidence/issue-369/official-v1-v2-cli-matrix.json`](../evidence/issue-369/official-v1-v2-cli-matrix.json).
Validate it with:

```powershell
py -3.13 scripts/validate_issue_369_matrix.py
```

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
| `gpt-5.6-sol` | list | `v2` | complete native spawn/send/wait/close, terminal 200 | complete native spawn/list/send/follow-up/wait/list, terminal 200 | `GO` | yes |
| `gpt-5.6-terra` | list | `v2` | complete native spawn/send/wait/close, terminal 200 | complete native spawn/list/send/follow-up/wait/list, terminal 200 | `GO` | yes |
| `gpt-5.6-luna` | list | `v1` | complete native spawn/send/wait/close, terminal 200 | complete native spawn/list/send/follow-up/wait/list, terminal 200 | `GO` | yes |
| `gpt-5.5` | list | `null` | no accepted baseline selection | full probe reported no native Collaboration surface | `UNQUALIFIED` | no |
| `gpt-5.3-codex-spark` | list | `null` | no accepted baseline selection | simple response only; no complete lifecycle evidence | `UNQUALIFIED` | no |
| `gpt-5.2` | absent/unknown | absent | not testable | not testable | `UNQUALIFIED` | no |
| `gpt-5.4` | list | `null` | no native Collaboration surface | no native Collaboration surface | `NO-GO` | no |
| `gpt-5.4-mini` | list | `null` | no native Collaboration surface | no native Collaboration surface | `NO-GO` | no |
| `codex-auto-review` | hidden/internal | internal | never exposed | never exposed | `UNQUALIFIED` | no |

The V1 and V2 probes used the exact selected model and an isolated Beta3
Gateway. Evidence retains only lifecycle phase names, terminal status, and
bounded event counts; it retains no prompt, reasoning, tool payload, opaque
identity, credential, or session data. A successful child result alone was not
accepted for rows marked `GO`: the accepted rows also have the full lifecycle
probe and a terminal `200` response.

## UI and persistence consequence

`frontend/src/lib/officialModels.ts` consumes the three exact `GO` rows above.
Sol and Terra retain their catalog V2 baselines. Luna retains its catalog V1
baseline and may receive an explicit model-level V2 override. The override is
keyed by the exact Official identity and is persisted by the managed catalog
sidecar; refresh, restart, catalog regeneration, and session overlay writes do
not erase it. Clearing the selector removes only that model's override.

No selector is exposed for `UNQUALIFIED`, `NO-GO`, hidden, or stale rows. This
matrix does not authorize third-party Collaboration V2, fallback, or
cross-Provider execution; those remain later generic qualification work.
