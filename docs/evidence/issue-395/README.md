# Issue #395 — real CLI Collaboration V2 over Chat

This evidence qualifies candidate `549104696d9a86f308a9f5cc7d884237c1dc45bc` with real
`codex-cli 0.147.0`.

The client sends Responses requests to the Gateway. The protocol-controlled
fixture accepts only `POST /v1/chat/completions`; it never implements a
Responses endpoint. The Gateway therefore performs the production
Responses-to-Chat request conversion and Chat-to-Responses stream conversion.
The Codex client, not the Gateway or fixture, owns agent creation, messaging,
waiting, interruption, and result execution.

## Result

See `cli-chat/summary.json` (schema
`codexhub.issue395.cli-chat-v2-lifecycle.v1`). The bounded result records:

- all six V2 tools: `spawn_agent`, `send_message`, `followup_task`,
  `wait_agent`, `interrupt_agent`, and `list_agents`;
- child-result delivery and parent `turn.completed`;
- six Chat tool-call responses and nine progressively split Chat responses;
- eight root requests carrying all six request-scoped aliases plus one child
  request carrying a plaintext request-bound `agent_message` envelope;
- zero fallback, no V1 namespace/tool observation, and no Gateway error;
- same-Home Gateway restart followed by explicit parent-session resume, with
  task identity, `config.toml`, and user-owned `AGENTS.md` preserved;
- a second isolated Home cannot resume the parent session and produces zero
  Gateway/fixture requests or mutations.

## Identity and failure coverage

Production Handler coverage in `tests/test_routing.py` verifies that live
transparent Chat streaming reverses opaque aliases to the exact
`collaboration` namespace/name/call identity. It also verifies that an
unknown alias after streaming starts emits exactly one `response.failed`
with the same response identity and no later success.

`tests/test_issue_395_chat_v2_lifecycle_fixture.py` verifies Chat declaration
mapping, exact `tool_call.id` / `tool_call_id` replay, synthesized
`fc_<call_id>` item identity, request-bound child envelopes, split argument
deltas, and unknown-alias rejection.

The C4 cases in `tests/test_issue_283_v2_lifecycle_fixture.py` remain the
negative boundary for mixed V1/V2, cross-Home V1 state, orphan/ambiguous
results, incompatible pagination, malformed agent messages, and
missing/duplicate call identities. Every case asserts rejection before fixture
mutation, cross-Provider execution, or fallback. Encrypted V2 handoffs remain
unavailable on third-party Chat and fail before sampling.

## Sanitization

The committed summary contains no request/response bodies, prompts, headers,
credentials, opaque task/session/call identities, ciphertext, absolute paths,
or raw aliases. Ports, aggregate counts, candidate identity, CLI version, and a
hash of the isolated configuration are retained. Diagnostic captures used
during development stayed under `/tmp` and are not evidence inputs.

## Reproduce

```powershell
.\scripts\codexhub-python.cmd scripts/run_issue_395_cli_chat_v2_lifecycle.py `
  --output-dir docs/evidence/issue-395/cli-chat `
  --candidate-sha 549104696d9a86f308a9f5cc7d884237c1dc45bc
```
