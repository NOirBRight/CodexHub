# Issue #283: Protocol fixture verification

This directory contains sanitized evidence for the Issue #283 V2 lifecycle
protocol-fixture track. It verifies the gateway's handling of Collaboration V2
namespaces, alias adaptation for Responses endpoints that do not support
`namespace_lifecycle`, streaming event reconciliation, and negative-case
boundary failures.

## Candidate revision

- Branch: `codex/issue-283-fixture`
- Candidate SHA: `5bb5ce50fc736d79d5ca1b37fd75720c9bc5b12b`
- Evidence captured: `2026-08-10T15:48:05Z`

## Scope

This is a **protocol-controlled fixture** test:

- The upstream is a synthetic Responses endpoint; no model, agent, or provider
  is invoked.
- The fixture exercises the same `codex_proxy.compatible_request_body`,
  `compatible_response_body`, and `compatible_sse_line` surface used in
  production.
- No credentials, prompts, call IDs, item IDs, task paths, or opaque agent
  identities are retained in this evidence.

The test file is `tests/test_issue_283_v2_lifecycle_fixture.py` and covers:

- **C1 native Responses**: V2 namespace declarations and history round-trip
  unchanged when `namespace_lifecycle=True`.
- **C2 adapted (chat_tools)**: `responses_structured` with
  `namespace_lifecycle=False` and `accepts_namespace_adapter=True` produces
  request-scoped, injective aliases that decode back to
  `namespace=collaboration` and the original tool name.
- **C3 streaming**: fragmented `function_call_arguments.delta` events assemble,
  SSE order is preserved, and terminal `response.completed` reconciles against
  the alias wire.
- **C4 negative cases**: mixed V1/V2 tools/history, malformed V2 parameters,
  missing namespace, duplicate/missing `call_id`, and malformed
  `agent_message` are all rejected before upstream sampling.
- **Gateway invariants**: the gateway never fabricates `function_call_output`
  or completion items, and V2 contexts do not initialize V1 scheduler/repair
  state.

## Results

- New fixture test: **18 passed, 0 failed**.
- Targeted regression suite (9 files): **403 passed, 0 failed**.
- Full Python core suite: **2347 passed, 2 failed (known, unrelated), 1
  skipped**.
  - `tests/test_release_channel_scripts.py::test_portable_rejects_invalid_flavor_before_building`
    fails on a locale-dependent PowerShell error message assertion (zh-CN
    Windows).
  - `tests/test_smoke_scripts.py::test_issue_108_tool_surface_evidence_replay_has_semantic_three_case_ab`
    fails because an external PowerShell evidence replay subprocess reports
    `evidence_fixture_invalid`.

## Static invariant checks

- `codex_proxy.py` Collaboration V2 branch sets `include_spawn_agent=False` and
  keeps `_subagent_state=None`.
- Worker stream validation, response contract, and V1 suppression functions all
  early-return under V2 contexts.
- `runtime_tool_compatibility.py` `_V2_NAMES` contains exactly the six Issue
  #392 tools: `spawn_agent`, `send_message`, `followup_task`, `wait_agent`,
  `interrupt_agent`, `list_agents`.

## Matrix and quality gates

- `scripts/validate_issue_369_matrix.py` reports `ISSUE_369_MATRIX_OK` when not
  pinned to a specific SHA. When pinned to this branch HEAD, it reports
  `candidate_sha_mismatch` because the matrix artifact currently records
  `candidate_revision=7006542a773fc20c10e4bbcadd593393a259ceb2`.
- `scripts/report_quality_gates.py` completed in report-only mode with zero
  parse errors.

## Artifact

The machine-readable result is
[`protocol-fixture-evidence.json`](./protocol-fixture-evidence.json).
