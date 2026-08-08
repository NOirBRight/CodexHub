# Issue #251: bounded Code Mode evidence

Status: deterministic replay plus bounded Codex CLI evidence bound to the
reviewed Beta3 implementation candidate
`3faafe1ca8ed979cbaaa42aff21ffc78a7d6c1e8`. This artifact is the contract
evidence for #251 (with #279's codec); the four-case #278/#280 runner summary
records the corresponding native and adapted Code Mode workflows.

## Candidate binding

The fixture retains its implementation base
`2cc3b1ecd287b8ee15d14715d084d0eb18df12c8` and binds the evidence to
`candidate_sha: 3faafe1ca8ed979cbaaa42aff21ffc78a7d6c1e8`. Run the validator
with the same SHA during the candidate gate:

```powershell
python tests/validate_issue_251_evidence.py
python tests/validate_issue_251_evidence.py --candidate-sha <exact-reviewed-candidate-sha>
```

Both commands must pass before the candidate is reviewed.

## Scope and provenance

The replay is deterministic and in memory. It does not contact a third-party
Provider, read credentials, write a real workspace, or execute a tool. The
separate CLI runner owns a synthetic loopback upstream and records only
sanitized workflow/status digests. No model/provider name selects a codec.
The route fields in the fixture are generic sentinels; the validator records
each case's selected route digest while translating protocol shape.

The implementation seam under test is
[`runtime_tool_compatibility.py`](../../../src-python/runtime_tool_compatibility.py):
one request-scoped plan classifies each declaration as `native` or `adapt`, and
one stream ledger performs the inverse mapping. The governing shape/lifecycle
rules are in [Issue #65](../issue-65/README.md) and
[ADR-0002](../../adr/0002-runtime-derived-tool-compatibility.md).

## Coverage

`issue_251_code_mode_evidence.json` contains one sanitized declaration set and
two protocol cases:

| Case | Selected protocol | Custom/freeform result | Streaming | Replay/history |
| --- | --- | --- | --- | --- |
| `native_custom` | `responses_structured` | Native `custom` declaration and `custom_tool_call` lifecycle are unchanged. | Added, input delta/done, item done, and completed terminal remain identical. | Call/result items round-trip with the same IDs and order. |
| `adapted_custom` | `chat_tools` | One request-local function alias carries exactly one `__codexhub_custom_input` envelope. | Upstream function events are buffered until complete, then inverse-mapped to the native custom lifecycle. | The same alias registry unwraps calls/results back to native history; a different request token gets a different alias. |

Both cases carry the client-owned, target-scoped workflow
`read -> apply_patch -> verify`.
The fixture records only opaque input/output sentinels, not patch text, paths,
tool arguments, or results. The validator asserts that the Codex Client owns
execution and that the Gateway executes no tool. It also checks the exact
`item_*`/`call_*` links and history ordering.

## Fail-closed controls

The adapted stream is deliberately replayed through bounded negative controls.
The validator must observe these classifications without printing the alias,
IDs, or opaque values:

| Control | Expected classification |
| --- | --- |
| Unknown request-local alias | `unknown_alias` |
| Extra/malformed envelope key | `invalid_envelope` |
| Missing item/call identity | `invalid_custom_stream_identity` |
| Duplicate item/call identity | `invalid_custom_stream_identity` |
| Incomplete stream interrupted by a terminal event | `incomplete_stream` |
| Event after a completed terminal | `stream_after_terminal` |

The same plan is never allowed to borrow an identity from another request or
attempt. Native declarations are not wrapped, and adapted declarations are not
reported as direct/native passthrough. No retry, output repair, route fallback,
or cross-Provider execution is exercised or authorized by this artifact.

## Verification and boundary

Run from the repository root:

```powershell
python tests/validate_issue_251_evidence.py
pytest -q tests/test_runtime_tool_compatibility.py tests/test_runtime_tool_compatibility_boundaries.py tests/test_runtime_tool_compatibility_integration.py
```

The first command validates the fixture schema, sanitization, native/adapted
declaration and envelope shape, stream/replay identity, request-scoped alias
allocation, route identity digests, and all negative controls. The focused
pytest command covers the broader implementation boundaries. Validate the
bounded #278/#280 CLI summary with:

```powershell
python scripts/validate_issue_278_evidence.py `
  --summary docs/evidence/issue-278/summary.json `
  --candidate-sha 3faafe1ca8ed979cbaaa42aff21ffc78a7d6c1e8
```
