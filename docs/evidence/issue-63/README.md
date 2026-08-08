# Issue #63 generic client-executed `tool_search` evidence

This directory is an offline, sanitized evidence scaffold for Beta3 Issues
[#63](https://github.com/NOirBRight/CodexHub/issues/63),
[#277](https://github.com/NOirBRight/CodexHub/issues/277), and
[#278](https://github.com/NOirBRight/CodexHub/issues/278). It is not a live
Provider capture, a release gate, or an Issue-closure claim. The fixture never
opens a network connection and contains no credentials, headers, prompts, raw
tool results, or real wire identifiers.

## Candidate binding

The fixture currently binds to the `codex/0.1.8-beta3-reasoning` lineage with
`candidate.revision=REPLACE_WITH_FINAL_BETA3_SHA`. The Beta3 candidate is still
being assembled. Before a qualification or release decision, replace that
placeholder with the final 40-character candidate SHA, set
`candidate.revision_status` to `final_candidate`, and rerun:

```powershell
python scripts/validate_issue_63_evidence.py `
  --fixture docs/evidence/issue-63/tool-search-lifecycle.json `
  --require-final-candidate
```

The fixture records CLI `0.146.0` only as the current source-contract floor;
the source contract remains `not_observed`. No real Codex CLI or Provider
request was run for this commit.

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

The same implementation does not currently contain a generic adapted
`tool_search` alias/envelope path. The `adapted_explicit_hint` case in
`tool-search-lifecycle.json` is therefore a contract-only, protocol-controlled
fixture: it checks the required injective envelope, inverse mapping, IDs,
ordering, streaming assembly, discovered declaration, subsequent call/result,
and replay history without claiming that production routing already supports
it. This is the remaining #277 implementation boundary.

## Fixture cases

`tool-search-lifecycle.json` has four bounded cases:

| Case | Protocol disposition | Selection | Evidence meaning |
| --- | --- | --- | --- |
| `native_explicit_hint` | `native` | explicit hint selected | Existing native shape and complete canonical lifecycle contract; fixture-only until an authorized CLI capture binds it. |
| `adapted_explicit_hint` | `adapt` | explicit hint selected | Reversible adapter contract for #277, including function `added`/`delta`/`done` ordering; not a production qualification. |
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

Run the standard-library validator from the repository root:

```powershell
python scripts/validate_issue_63_evidence.py
```

Success prints `ISSUE_63_EVIDENCE_FIXTURE_OK` and one status for each case.
`--require-final-candidate` intentionally fails while the candidate SHA is
the documented placeholder. The validator checks the schema, redaction
allow-list, route ownership, native identity preservation, adapted envelope
round-trip, SSE event ordering, discovered declaration, follow-up links,
history order, and no-hint classification. It has no HTTP, subprocess, CLI,
credential, or Provider integration.

## Qualification boundary

The fixture is necessary but not sufficient for #63/#278. A separately
authorized Beta3 evidence window must later bind the final candidate SHA, the
exact CLI/runtime build, and a sanitized real-CLI explicit-hint run through a
protocol-controlled upstream for both native and adapted variants. That run
must capture planner eligibility, model-visible client `tool_search`, search
call/output, discovered declaration, subsequent call/result, SSE, and replay
history. A no-hint run must retain the same route and classify model
non-selection separately from infrastructure failures. Hosted search cannot
stand in for client-executed `tool_search`, and no alternate Provider may be
contacted.
