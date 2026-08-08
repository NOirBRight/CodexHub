# Issue #63 generic client-executed `tool_search` evidence

This directory is a sanitized contract and bounded CLI evidence record for Beta3 Issues
[#63](https://github.com/NOirBRight/CodexHub/issues/63),
[#277](https://github.com/NOirBRight/CodexHub/issues/277), and
[#278](https://github.com/NOirBRight/CodexHub/issues/278). The checked-in
fixture is offline; the companion #278 runner separately exercises the same
four cases through Codex CLI 0.146.1 and a request-owned synthetic upstream.
Neither artifact is an authenticated third-party Provider qualification. No
credential, header, prompt, raw tool result, or real wire identifier is
retained.

## Candidate binding

The fixture binds to the reviewed Beta3 implementation candidate
`3faafe1ca8ed979cbaaa42aff21ffc78a7d6c1e8` and has
`candidate.revision_status=final_candidate`. Rerun the bounded validator
before the release gate:

```powershell
python scripts/validate_issue_63_evidence.py `
  --fixture docs/evidence/issue-63/tool-search-lifecycle.json `
  --require-final-candidate
```

The fixture records CLI `0.146.0` only as the source-contract floor. The
bounded CLI result is recorded in
`docs/evidence/issue-278/summary.json`, bound to the same implementation
candidate, and reports Codex CLI `0.146.1` with native and adapted explicit
cases completed and no-hint cases classified as `model_not_selected`.

## What the current implementation proves

`src-python/runtime_tool_compatibility.py` derives `tool_search` handling from
the declaration and selected protocol capabilities. A native path requires
the exact client-owned declaration (`type=tool_search`, `execution=client`)
and an explicit `tool_search_lifecycle` capability. Existing deterministic
tests cover the native declaration/body/history identity, the client execution
marker, rejection of function-argument SSE fragments, stream marker
revalidation, duplicate/missing identities, and conservative omission when
the lifecycle fact is absent. The Responses integration fixture also covers
the explicit-fact native disposition.

For a protocol without native namespace support, the implementation derives a
request-scoped reversible function envelope. The `adapted_explicit_hint` case
and the runner's `adapted_explicit` case check the injective alias, inverse
mapping, IDs, ordering, streaming assembly, discovered declaration,
subsequent call/result, and replay history. This does not claim that hosted
Provider search is proxied or that an authenticated Provider is universally
compatible; it proves only the generic wire boundary owned by CodexHub.

## Fixture cases

`tool-search-lifecycle.json` has four bounded cases:

| Case | Protocol disposition | Selection | Evidence meaning |
| --- | --- | --- | --- |
| `native_explicit_hint` | `native` | explicit hint selected | Native contract plus the bounded CLI `native_explicit` run. |
| `adapted_explicit_hint` | `adapt` | explicit hint selected | Reversible adapter contract plus the bounded CLI `adapted_explicit` run. |
| `native_no_hint` | `native` | not selected | `model_not_selected` classification; no search lifecycle is treated as a Gateway failure. |
| `adapted_no_hint` | `adapt` | not selected | The same `model_not_selected` classification for the adapted protocol variant. |

Both explicit-hint cases preserve one selected route, keep hosted search
separate, and record zero cross-Provider requests. The canonical lifecycle is:

```text
client tool_search declaration
  -> search call/output (client execution marker)
  -> discovered declaration
  -> subsequent call/result
  -> SSE lifecycle
  -> replay/history in the same order and identity
```

The no-hint cases require planner eligibility and model-visible
`tool_search`, then classify non-selection as `model_not_selected`. They do
not infer planner, upstream, runtime, or Gateway failure from the absence of a
search call.

## Offline validation

Run the standard-library validators from the repository root:

```powershell
python scripts/validate_issue_63_evidence.py
python scripts/validate_issue_278_evidence.py `
  --summary docs/evidence/issue-278/summary.json `
  --candidate-sha 3faafe1ca8ed979cbaaa42aff21ffc78a7d6c1e8
```

Success prints `ISSUE_63_EVIDENCE_FIXTURE_OK` and one status for each case.
`--require-final-candidate` passes for the checked-in bound fixture; the
validator checks the schema, redaction allow-list, route ownership, native
identity preservation, adapted envelope round-trip, SSE event ordering,
discovered declaration, follow-up links, history order, and no-hint
classification. It has no HTTP, subprocess, CLI, credential, or Provider
integration.

## Qualification boundary

The fixture is necessary but not sufficient for #63/#278. The checked-in
summary is the bounded real-CLI evidence window: it binds the candidate SHA
and exact CLI/runtime, and records planner eligibility, model-visible client
`tool_search`, search/discovery/workflow completion, SSE/history digests, and
no-hint classification. Hosted search cannot stand in for client-executed
`tool_search`, and no alternate Provider may be contacted.
