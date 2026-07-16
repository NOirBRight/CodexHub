# Issue #161 implementer report

## Status

DONE

## Files and design

- `src-python/codex_semantic_adapter.py`
  - Preserves explicit `spawn_agent.agent_type=worker` instead of deleting it.
  - Defines the exact internal normalized envelope `codexhub.worker-binding.v1` at `result.effective_binding`.
  - Validates selector, support/status, effective agent type, third-party model, and reasoning with fail-closed classifications.
  - The envelope is a CodexHub-internal adapter contract, not a guessed Host wire schema. A future/live #156 Host shape must cross a separate explicit normalization seam before entering this validator; aliases and unversioned/extended shapes are rejected.
- `src-python/codex_proxy.py`
  - Rejects missing/unsupported Worker selectors before external spawn execution once the request advertised the spawn surface.
  - Preserves explicit Worker through body/SSE response normalization and existing exact/required-spawn coercion paths without synthesizing it.
  - Validates supported effective binding readback before forwarding history to an edit-capable external turn; requested model/reasoning are only comparison inputs and never treated as effective evidence.
  - Emits only stable outcome/classification/surface fields for Worker contract telemetry; private request state remains underscore-prefixed, preserving #159 telemetry projection.
- `tests/test_codex_semantic_adapter.py`
  - Covers selector preservation, missing/unsupported selector rejection, exact matching readback, missing/unknown/aliased/extended/contradictory/rejected/unsupported/GPT-substituted failures.
- `tests/test_routing.py`
  - Covers declaration → response/body → structured history replay, pre-execution selector rejection, pre-edit binding rejection, SSE argument repair preservation, and unchanged legacy `general` behavior.
- `tests/fixtures/worker_effective_binding.json`
  - Synthetic enums/shapes only; explicitly marked `synthetic_codexhub_internal_normalized_adapter_contract`.

## Exact RED evidence

1. Initial semantic adapter RED:
   - Command: `python -m pytest -q tests/test_codex_semantic_adapter.py`
   - Result: `10 failed, 5 passed`
   - Expected failures: Worker selector was deleted (`KeyError: agent_type`) and validation APIs were absent.
2. Initial routing RED:
   - Command: `python -m pytest -q tests/test_routing.py -k "external_worker_selector or external_worker_binding"`
   - Result: `4 failed, 1 passed, 433 deselected`
   - Expected failures: no declaration constraint/assertion, no selector rejection, no binding mismatch rejection.
3. SSE preservation RED:
   - Command: `python -m pytest -q tests/test_routing.py::RoutingTests::test_responses_sse_coerces_exact_child_spawn_prompts_across_lines`
   - Result: `1 failed`
   - Expected failure: existing exact-prompt coercion removed explicit Worker from `response.function_call_arguments.done`.
4. Versioned exact-envelope RED:
   - Command: `python -m pytest -q tests/test_codex_semantic_adapter.py`
   - Result: `2 failed, 15 passed`
   - Expected failures: an added extension was accepted and aliased effective fields were not given the stable unknown-readback classification.

## GREEN and targeted verification

- Command: `python -m pytest -q tests/test_codex_semantic_adapter.py`
  - Result: `17 passed in 0.34s`
- Command: `python -m pytest -q tests/test_routing.py -k "agent_type or binding"`
  - Result: `3 passed, 433 deselected, 2 subtests passed in 0.48s`
- Existing supported non-Worker checks:
  - Command: `python -m pytest -q tests/test_routing.py -k "spawn_alias_arguments or strict_mode_still_repairs_multi_agent_argument_shape or raw_provider_probe_skips_request_injection"`
  - Result: `3 passed, 433 deselected`

## Full Python suite

- First non-final candidate run exposed a related deterministic evidence regression:
  - `1 failed, 1163 passed, 1 skipped, 307 subtests passed`
  - Failure: `test_issue_108_tool_surface_evidence_replay_has_semantic_three_case_ab` / `evidence_fixture_invalid`.
  - Root cause: constraining the long-standing discovery schema changed the #108 prepared-surface digest.
  - Disposition: restored the existing declaration shape and kept enforcement at the selector execution boundary; isolated replay then passed (`1 passed`). No #108 fixture was edited.
- Final candidate command: `python -m pytest -q`
  - Result: `1164 passed, 1 skipped, 307 subtests passed in 40.37s`
- The baseline `WinError 5` atomic-replacement flake did not recur.

## Report-only quality gates

- Command: `python scripts/report_quality_gates.py`
- Exit: `0` (report-only)
- Findings: `python_unused_imports: 3`, `python_dead_functions: 79`, `duplicate_function_names: 132`, `parse_errors: 0`.
- The scanner lists `validate_effective_worker_binding` as dead because proxy consumption is through an imported alias; runtime tests cover that seam. No allowlist changes were made.

## Diff check

- Command: `git diff --check`
- Exit: `0` (only configured LF→CRLF working-copy warnings).

## Acceptance self-review

- [x] Worker survives declaration presence, normalization, body/SSE call execution, response repair, and structured history replay unchanged.
- [x] Missing/unsupported selector rejects with sanitized terminal classification before execution when the external spawn surface was advertised.
- [x] Only exact `codexhub.worker-binding.v1` supported readback proves effective agent type/model/reasoning.
- [x] Missing, unknown, aliased, extended, contradictory, rejected, unsupported, and GPT-substituted readbacks fail closed before an edit-capable request.
- [x] Legacy supported `general` normalization and raw-provider behavior remain unchanged.
- [x] Telemetry contains only stable classifications/outcomes/surface; no prompts, credentials, private Task/callback IDs, rollout data, paths, model values, or reasoning values.
- [x] #159 query-bound behavior and underscore-private telemetry state remain unchanged.
- [x] No Host callback/runtime/result-delivery implementation and no GitHub mutation.

## Commit

`8bcb0c1121c6fb5f387bf5f209e55feed7eece81` — `fix(gateway): validate external worker binding`

## Concerns

- Live Host compatibility remains gated by #156. This change deliberately does not claim the current Host wire schema matches the synthetic internal envelope; any different Host shape requires an explicit, separately tested normalization seam and must not be alias-guessed.



---

## Review-fix delta (supersedes conflicting candidate statements above)

### Status

DONE — all five findings in `.superpowers/sdd/issue-161-review.md` were addressed in the local review-fix commit. The earlier statements that the declaration was restored, that the #108 fixture was untouched, and that generated Worker spawn repair survives are superseded by this section.

### Review fixes

1. Unknown selectors are preserved by normalization. Only explicit legacy `agent_type="general"` is removed compatibly. Every replayed spawn selector is validated before history rewrite; missing/unsupported values reject terminally with `surface="history"`.
2. Worker responses receive a versioned, exact-field, server-owned `_codexhub_worker_requested_binding` sidecar. Its HMAC binds the original requested Worker/model/reasoning binding to the spawn `call_id`. Replay compares effective readback only with that verified original-call binding, never with next-turn top-level model/reasoning.
3. Effective readback now crosses a strict full-consumption JSON decoder. Trailing bytes, concatenated JSON, and malformed nonempty strings reject as `malformed_readback`.
4. The advertised selector is required and declared as `enum: ["worker", "general"]`. Only the three #108 `prepared_surface_sha256` values changed; the source payload digests, counts, ordering semantics, and eager/deferred behavior remain unchanged.
5. Requested and effective validation now emits precise stable classifications, including missing versus unsupported requested model/reasoning and recognized contradictions versus unknown effective enum values.

Generated semantic repair remains fail-closed: a required spawn spec without an explicit selector is rejected instead of synthesizing Worker. Historical routing fixtures unrelated to Worker binding now declare explicit legacy `general`; production validation was not weakened.

### Review-fix RED evidence

- Unknown-selector normalization: `1 failed, 1 passed` before preserving unknown values.
- Missing/unsupported replay bypass: `2 subfailures` before validating every historical spawn.
- Original-call correlation/model-change cases: `2 failed` before the signed requested-binding sidecar.
- Precise classifications and strict decoder cases: `8 failed, 8 passed` before the classification/decoder changes.
- Generated repair without selector: `1 failed` before terminal rejection was enforced.
- Declaration enum/required assertion: `1 failed` before constraining the schema.
- #108 prepared-surface replay after the intentional declaration change: `1 failed` with `evidence_fixture_invalid` before updating only the three prepared-surface digests.
- Complete routing-module compatibility run initially found 37 historical fixtures without a selector. After marking those historical calls as explicit legacy `general`, three remaining tests exposed two Worker requests without original reasoning and one outdated generated-repair expectation; these were corrected without weakening the production boundary.

### Final targeted verification

- `python -m pytest -q tests/test_codex_semantic_adapter.py`
  - `26 passed in 0.34s`
- `python -m pytest -q tests/test_routing.py -k "agent_type or binding"`
  - `9 passed, 432 deselected, 9 subtests passed in 0.63s`
- `python -m pytest -q tests/test_smoke_scripts.py::test_issue_108_tool_surface_evidence_replay_has_semantic_three_case_ab`
  - `1 passed in 1.38s`
- `python -m pytest -q tests/test_routing.py -k "deferred_tool_search or bounded_tool_search or external_tool_surface_preparation_telemetry_uses_sanitized_structural_counts"`
  - `8 passed, 433 deselected in 0.54s`
- `python -m pytest -q tests/test_routing.py`
  - `441 passed, 118 subtests passed in 8.08s`
- Focused requested/effective telemetry-classification test:
  - `1 passed, 440 deselected, 2 subtests passed in 0.64s`

Per the repository review-fix policy and controller instruction, the full Python suite was not repeated. Retained candidate evidence remains `1164 passed, 1 skipped, 307 subtests passed in 40.37s`; the review delta did not cross a new verification-matrix row.

### #108 prepared-surface digest update

The required/enum selector declaration deterministically changes the prepared tool surface, so the following three prepared-surface digests were intentionally refreshed:

- `minimal_core`: `sha256:5dc7c2fdc99cadc1dd8dcb2175e72ff41b83c0d209560ea589685ca74329b899`
- `namespace_200_eager`: `sha256:18c4d4bf1571f0dc7c3ca33b1fac816303a815db45a7ce99d872a0546ef60f75`
- `namespace_200_deferred_core`: `sha256:a5232e63e034bdca95f8f733c129cab51be155bf822ab6c5aea679483f37c108`

No source digest, tool count, namespace count, ordering expectation, or visibility semantic changed.

### Report-only quality and diff hygiene

- `python scripts/report_quality_gates.py`: exit `0` (report-only); `python_unused_imports: 3`, `python_dead_functions: 80`, `duplicate_function_names: 132`, `parse_errors: 0`. The scanner reports the new strict decoder as dead because proxy use is through an imported alias; routing tests exercise the seam. No allowlist changes were made.
- `git diff --check`: exit `0` before commit; only configured LF-to-CRLF warnings were printed.

### Review-fix commit

`6678800cd8272ae1c82f8b629382f3e1232d0a7d` — `fix(gateway): bind worker replay to original request`

### Remaining concern

Live Host/runtime Worker materialization and wire-shape normalization remain owned by #156. This change carries a CodexHub-internal sidecar and does not guess aliases, infer effective evidence from requests, or claim live Host compatibility. No GitHub mutation was performed.

---

## Second re-review delta (supersedes conflicting legacy/general and carrier statements above)

### Status

DONE — all Critical/Important findings and the requested Minor classification refinement in `.superpowers/sdd/issue-161-rereview.md` are addressed by the third local commit.

### Contract changes

1. Explicit `agent_type="general"` is preserved through declaration, body normalization, native Responses SSE normalization, replay history rewriting, and Chat output repair. It never receives or requires a Worker requested-binding sidecar/readback. Missing selectors still reject.
2. `_codexhub_worker_requested_binding` is a Gateway-to-Responses-client history carrier only. Replay ingress verifies it before mutation; provider-bound request normalization then removes the sidecar plus response-output-only `id/status`, retaining `call_id/name/arguments` and strict request semantics. The strict Chat/Responses translator was not weakened.
3. Chat callers receive a spawn declaration whose selector enum contains only `general`. An explicit Worker body or SSE call is rejected before caller conversion with sanitized `unsupported_caller_carrier`; requested binding is never inserted into executable arguments.
4. Responses callers using Chat upstream conversion receive Worker sidecars after Chat-to-Responses body/SSE conversion. On the next turn the sidecar validates, is stripped, and the remaining history passes the existing strict Responses-to-Chat converter. Ordinary non-Worker function-call replay remains intact.
5. Worker identity is one-to-one: duplicate Worker `call_id`, duplicate effective output, and valid-output-then-duplicate-call all reject. Swapped-call and field-tampered sidecars reject before forwarding.
6. Signing no longer reuses the telemetry secret. `src-python/worker_binding_signing.py` owns a dedicated private atomic `worker-binding-signing-secret-v1`. Restart reuses the file; deletion/rotation intentionally invalidates existing signed histories so they fail closed and must start a fresh delegation. All routing tests patch the dedicated root to a per-test temporary directory; no test touches the real runtime Codex directory.
7. Effective `agent_type`, `model`, and `reasoning` now classify missing/empty separately from present wrong types/unknown enums. Proxy telemetry tests assert stable event/classification fields only and exclude values/call IDs.

### TDD RED evidence

- General end-to-end preservation:
  - `python -m pytest -q` over the semantic general case plus body/SSE normalize-to-replay cases.
  - RED: `3 failed`; explicit `general` had been erased.
  - GREEN after preservation: `3 passed`.
- Carrier-safe Chat caller and replay stripping:
  - Initial focused carrier tests RED: `3 failed, 441 deselected`; Worker was advertised to Chat callers and replay retained the sidecar.
  - After sidecar stripping, Responses-caller/Chat-upstream body+SSE tests exposed the next strict request-shape boundary: `2 failed, 451 deselected` because response-only `id/status` remained.
  - GREEN after ingress request-shape normalization: `2 passed, 451 deselected`; ordinary non-Worker replay also passed (`1 passed, 453 deselected`).
- One-to-one identity:
  - RED: `3 failed, 3 passed, 444 deselected`; duplicate Worker calls/outputs were accepted while swapped/tampered/rotated signatures already rejected.
  - GREEN: `6 passed, 444 deselected`.
- Dedicated signing lifecycle:
  - RED: test collection failed with `ModuleNotFoundError: worker_binding_signing`.
  - GREEN: `2 passed`; stable restart reuse, private file mode where supported, rotation invalidation, and new-key verification are covered.
- Effective-field classifications:
  - Semantic RED: `9 failed, 26 deselected`; all cases were collapsed to `missing_readback`.
  - GREEN: `9 passed, 26 deselected` and proxy seam `1 passed, 450 deselected, 6 subtests passed`.
- The first complete routing compatibility run after preservation found 11 outdated expectations/context-field collisions. Those tests were updated to the new explicit-general contract and the caller format was moved to underscore-private adapter context. No production validation was weakened.

### Final verification

- `python -m pytest -q tests/test_routing.py`
  - `454 passed, 124 subtests passed in 8.81s`
- `python -m pytest -q tests/test_codex_semantic_adapter.py`
  - `35 passed in 0.45s`
- `python -m pytest -q tests/test_worker_binding_signing.py`
  - `2 passed in 0.46s`
- Focused strict Chat/Responses protocol translation:
  - `7 passed, 49 deselected, 3 subtests passed in 0.37s`
- `python -m pytest -q tests/test_smoke_scripts.py::test_issue_108_tool_surface_evidence_replay_has_semantic_three_case_ab`
  - `1 passed in 1.50s`
- #159 query-bound/private telemetry focus:
  - `8 passed, 446 deselected in 0.64s`
- Worker/general/carrier/identity focus during final verification:
  - `22 passed, 432 deselected, 15 subtests passed in 1.07s`

Per controller instruction and `docs/agents/verification-policy.md`, the repository-wide Python suite was not repeated for the same Python boundary. Retained candidate evidence remains `1164 passed, 1 skipped, 307 subtests passed in 40.37s`.

### Quality and diff hygiene

- `git diff --check`: exit `0`; configured LF-to-CRLF warnings only.
- `python scripts/report_quality_gates.py --json`: exit `0` (report-only); `python_unused_imports: 3`, `python_dead_functions: 81`, `duplicate_function_names: 132`, `parse_errors: 0`.
- The scanner reports imported-alias/module entrypoints such as the strict decoder and signing verifier as dead; routing and dedicated tests execute those seams. No allowlist changes were made.

### Commit

`06684ccb1b19f5f414ac2eeb43338d2de1b2f23b` — `fix(gateway): harden worker history carrier`

### Remaining concern

Live Host Worker materialization/readback production remains #156 scope. This delta adds no Host aliases, does not infer effective state from request values, and does not place private binding data in executable arguments or telemetry. A Chat caller intentionally cannot request Worker until a safe opaque carrier is designed; its supported legacy `general` surface remains available.

---

## End-to-end Chat-stream relay contract fix delta

### Status

DONE — the production Responses-caller/Chat-upstream streaming relay now applies the same Worker response contract as body and native Responses SSE adapters, and the duplicated signature payload construction is centralized.

### Root cause and design

`CodexProxyHandler._relay_upstream_response()` buffered Chat Completions chunks, converted and reconciled the Responses event list, then wrote it directly. That parallel path never crossed the Worker selector validation / requested-binding carrier boundary used by `compatible_response_body()` and `compatible_sse_line()`.

The fix adds `_apply_external_worker_response_contract()` as the shared event-level seam around selector validation and sidecar attachment:

- body and native Responses SSE retain their fail-fast pre-normalization selector check and post-repair sidecar attachment by invoking the same seam in its two required phases;
- the converted Chat-stream event list invokes the complete seam once after `_reconcile_function_call_argument_events()` and the final required-call repair, before headers/event bytes are written;
- valid Worker calls therefore receive the signed carrier on replayable `response.output_item.done` and `response.completed.response.output` call items;
- missing and unsupported selectors raise before any downstream executable event is written;
- strict Chat/Responses conversion remains unchanged.

Signing and verification now both use `_worker_requested_binding_signature_payload()`, which selects the four signed contract fields, canonicalizes them once, and applies the shared `call_id + NUL + canonical JSON` framing.

### Files changed

- `src-python/codex_proxy.py`
  - added the shared Worker response-contract seam;
  - applied it to body, native Responses SSE, and the reconciled Chat-stream-to-Responses relay path;
  - centralized signed binding payload canonicalization/framing.
- `tests/test_routing.py`
  - replaced the manually adapted Chat-stream carrier test with a production `_relay_upstream_response()` regression;
  - added production relay regressions for missing and unsupported selectors and asserted zero downstream event writes.
- `.superpowers/sdd/issue-161-report.md`
  - appended this review-fix evidence.

### Strict TDD RED evidence

Command:

```powershell
python -m pytest -q tests/test_routing.py -k "responses_caller_chat_upstream_sse_relay"
```

Observed before production changes:

```text
FFF                                                                      [100%]
...
E       AssertionError: UpstreamProtocolTranslationError not raised
...
E       AssertionError: UpstreamProtocolTranslationError not raised
...
E       AssertionError: '_codexhub_worker_requested_binding' not found in {'id': 'fc_chat-worker-stream-call', 'type': 'function_call', 'status': 'completed', 'call_id': 'chat-worker-stream-call', 'name': 'spawn_agent', 'arguments': '{"agent_type":"worker","message":"delegate","fork_context":false}', 'namespace': 'multi_agent_v1'}
...
3 failed, 453 deselected in 1.49s
```

This was the expected RED: the production relay neither rejected the two invalid selectors nor attached the valid Worker carrier.

### GREEN and compatibility verification

- Production relay regressions:
  - `python -m pytest -q tests/test_routing.py -k "responses_caller_chat_upstream_sse_relay"`
  - `3 passed, 453 deselected in 0.66s`
- Worker/binding/converted-path focus after preserving the prior validation order:
  - `python -m pytest -q tests/test_routing.py -k "agent_type or binding or responses_caller_chat_upstream"`
  - `21 passed, 435 deselected, 15 subtests passed in 1.06s`
- Complete routing compatibility module:
  - `python -m pytest -q tests/test_routing.py`
  - `456 passed, 124 subtests passed in 9.46s`
- Semantic adapter:
  - `python -m pytest -q tests/test_codex_semantic_adapter.py`
  - `35 passed in 0.33s`
- Dedicated signing lifecycle:
  - `python -m pytest -q tests/test_worker_binding_signing.py`
  - `2 passed in 0.36s`
- Report-only quality gates:
  - `python scripts/report_quality_gates.py --json`
  - exit `0`; `python_unused_imports: 3`, `python_dead_functions: 81`, `duplicate_function_names: 132`, `parse_errors: 0`
- Diff hygiene:
  - `git diff --check`
  - exit `0`; only configured LF-to-CRLF warnings were printed.

The first broader Worker focus exposed that moving validation after alias normalization added an alias telemetry event before terminal rejection (`2 failed, 21 passed, 435 deselected, 13 subtests passed`). The implementation was corrected to preserve the established fail-fast validation phase while routing both phases through the shared contract seam; no test expectation or production validation was weakened.

Per `docs/agents/verification-policy.md` and the controller instruction, the repository-wide Python suite was not repeated: this review fix stays within the already-covered Python Gateway/routing row. Retained candidate evidence remains `1164 passed, 1 skipped, 307 subtests passed in 40.37s`.

### Acceptance self-review

- The tests call the production `_relay_upstream_response()` path; they do not manually feed converted events through `compatible_sse_line()`.
- A valid Worker call reaches the Responses caller with the same signed carrier on both replayable terminal representations, then successfully verifies/strips on replay and passes the strict Responses-to-Chat request converter.
- Missing and unsupported selectors terminate with `external_worker_selector_rejected`, sanitized classifications, and no downstream event writes.
- Body/native SSE keep the prior fail-fast validation ordering and late carrier attachment; the converted Chat stream applies both together only after final argument reconciliation.
- The private carrier remains outside executable arguments and telemetry; signature verification still fails closed for field or call-ID changes.
- No GitHub mutation, push, Host/runtime implementation, fixture digest change, or unrelated worktree edit was made.

### Remaining concern

Live Host/runtime Worker materialization and effective readback production remain owned by #156. This delta only closes the CodexHub production relay bypass and does not claim live Host compatibility.
