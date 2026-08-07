# Official Collaboration V1/V2 capability matrix

Issue: #369  
Candidate: `3e518330`  
Codex runtime: Codex CLI/Desktop `0.146.1` (native binary SHA prefix `AE9D865F…`)  
Runtime cache snapshot: `C:\Users\noirb\.codex\models_cache.json`, `client_version=0.146.0`, fetched `2026-08-02`  
Catalog metadata source revision: `b24aa20107f365a1d0f06de9e0b28df5c516c7dd`  
Captured: `2026-08-08`

## Scope and decision rule

This is an evidence report, not a model allowlist and not a default/catalog change. A catalog `null`, a missing runtime row, or a partial lifecycle observation is `UNQUALIFIED`; a spawn-only observation is never `GO`. A V1/V2 selector may be enabled only for a list-visible row with a complete accepted `GO` verdict. Internal/hidden rows remain recorded but are never picker-visible.

The report separates three sources:

1. CodexHub's managed baseline in `config/official_model_catalog_metadata.json` and `src-tauri/src/models.rs`.
2. The native Codex 0.146 runtime cache snapshot.
3. Complete Collaboration lifecycle evidence. No complete lifecycle capture was available for this candidate, so no row is promoted to `GO`.

## Matrix

| Canonical model | CodexHub baseline | Runtime cache | Catalog visibility | Explicit V1/V2 probe | Lifecycle evidence | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| `gpt-5.6-sol` | `v2` | `v2` | list | Selection is the model-level `multi_agent_version` field; no selector enabled | No complete spawn/message/follow-up/wait/list/interrupt/restart capture bound to `3e518330` | `UNQUALIFIED` |
| `gpt-5.6-terra` | `v2` | `v2` | list | Same model-level field; no selector enabled | No complete lifecycle capture | `UNQUALIFIED` |
| `gpt-5.6-luna` | `v1` | `v2` | list | Baseline/override conflict is recorded; no default change and no selector enabled | No complete lifecycle capture | `UNQUALIFIED` |
| `gpt-5.5` | `null` | `null` | list | No inferred capability; no selector enabled | No complete lifecycle capture | `UNQUALIFIED` |
| `gpt-5.2` | absent | absent | absent/unknown | No selection method; not picker-visible | Not testable from the installed catalog/cache | `UNQUALIFIED` |
| `gpt-5.4` | `null` | `null` | list in managed builtin metadata | No inferred capability; no selector enabled | No complete lifecycle capture | `UNQUALIFIED` |
| `gpt-5.4-mini` | `null` | `null` | list in managed builtin metadata | No inferred capability; no selector enabled | No complete lifecycle capture | `UNQUALIFIED` |
| `codex-auto-review` | internal row | `v2` in cache | hidden from picker by internal-identity policy | Never expose a selector | No lifecycle qualification; internal row is recorded only | `UNQUALIFIED` |

The native cache's `v2` values for Luna and `codex-auto-review` are runtime observations, not permission to rewrite CodexHub's managed baseline. The Luna conflict is the input to #368's model-level override work.

## Lifecycle contract

The required lifecycle for a future `GO` row is: explicit V1 and V2 selection, spawn, returned task identity/path, `message`/`agent_message`, follow-up, wait/result, list/interrupt where supported, streaming/history, restart/readback, and terminal/error shape. The candidate evidence set contains no complete, replayable capture satisfying that contract. In particular, the existing #64 source-contract inventory records `capture_status=not_observed`; it cannot be upgraded to `GO` by inference from the cache or a spawn-only result.

## Reproduction and verification

```powershell
codex --version
Get-Content $env:USERPROFILE\.codex\models_cache.json
py -3.13 -m pytest -q tests/test_catalog_sync.py
```

The first two commands were run against the installed native runtime and the third is the catalog contract regression. No credentials, prompts, reasoning text, task IDs, tool arguments/results, or raw client output are retained in this report.

## Decision for #368

Do not expose a V1/V2 selector from this matrix yet. #368 may implement persistence and UI plumbing behind an accepted matrix verdict, but the effective value must remain the managed baseline until a later exact-runtime lifecycle capture records `GO` for the specific Official model. The matrix does not change defaults, add a global switch, or authorize fallback.
