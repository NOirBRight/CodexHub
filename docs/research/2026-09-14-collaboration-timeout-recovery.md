# Collaboration wait continuation recovery

## Acceptance

The user reports another subagent continuation failure on 0.2.9 and asks to
fix it and audit related omissions and payload/privacy leaks. Preserve user
history and original requested values, and keep tool execution owned by Codex.
Run Linux and Windows relevant regression/E2E checks before a follow-up release.

## Reproduction

The affected local parent rollout records a V2 `wait_agent` call with
`{"timeout_ms":1280}` followed by a successful JSON result explaining that
1280 ms was clamped to the minimum of 10000 ms. Gateway then rejects continuation
with `Tool compatibility failed at history: collaboration_timeout_out_of_range.`
No task IDs, task content, credentials or raw rollout are copied here.

The public ToolCompatibilityPlan regression is
`tests/test_collaboration_timeout_contract.py::test_client_wait_input_range_is_not_the_effective_wait_duration`.
It failed on 0.2.9 with the exact classification and now covers native/adapted
history plus response-body and SSE continuations.

## Runtime authority

Codex CLI rust-v0.153.4 source:

- https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/core/src/tools/handlers/multi_agents/wait.rs
- https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/core/src/tools/handlers/multi_agents_v2/wait.rs

Both deserialize Option<i64>. The wire domain is the full signed i64 range;
semantic execution errors are client-owned outcomes, not malformed JSON.
V1 rejects nonpositive integers and clamps positive
integers at both duration limits. V2 clamps all integers below its configured
minimum (including negative values), and reports a configured maximum violation
to the model. Null means the default timeout. Fixed 10000..3600000 validation
incorrectly confused execution duration with valid serialized arguments.

Preserve these values; retain exact-number handling, i64 overflow checks,
fraction/type/duplicate-key rejection, namespace isolation and encryption rules.
V2 range-error replay no longer needs a special argument-validation exemption:
its integer input is already valid, and the client owns its configured maximum.

## Adjacent audit fixes

- Preserve explicit null only for handler-proven Option fields on spawn_agent,
  send_input, list_agents and wait_agent. Required fields and defaulted bools
  still reject null. Keep original null fields even when another field is
  numerically normalized.
- Replay V1 argument-deserialization errors and the exact nonpositive wait
  execution error; do not convert them into success results.
- Replay the exact empty-message execution error for V2 spawn_agent as well
  as the existing send_message/followup_task paths.

## Verification status

Linux (CLI 0.154.0) and Windows real parent/child two-turn E2E pass on
7041f64, including two mandatory 1280 ms waits, client clamp evidence, encrypted
message delivery and completed status. Adjacent audit delta verification and
full suites are in progress. No production process or
user session has been modified.


## Error and diagnostics privacy audit

The public translation seam echoed unsupported user-controlled field names,
content/tool types, roles, finish reasons, indexes and statuses into exceptions
and downstream error details. The multimodal result adapter had the same issue.
Those errors now describe only the fixed failure surface. Request body-shape
telemetry now keeps known protocol keys/types/roles and unknown counts/categories;
it no longer persists arbitrary discriminator strings.

`tests/test_protocol_translation_privacy.py` reproduces and verifies both error
mapping and diagnostics through public seams with synthetic sentinels. No
actual credential disclosure was established from the user's affected task.

Runtime failure replay additionally covers source-proven resolver path/id errors
and the nonempty `collab tool failed: ` / `collab spawn failed: ` framing, scoped
to their owning tools. These remain unchanged tool failure outputs; they do not
waive argument validation, successful JSON schemas or encryption boundaries.
Unknown arbitrary text is not universally accepted as a successful result.
