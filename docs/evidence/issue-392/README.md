# Issue #392 frozen Collaboration runtime contract

This directory records the request-visible Collaboration V1/V2 contract used
by CodexHub 0.1.8 Beta4. It supersedes the source-only shape assumption in the
historical #64 inventory.

## Frozen inputs

| Client | Version | Binary SHA-256 | Source tag / commit |
| --- | --- | --- | --- |
| Codex CLI | 0.146.1 | `ae9d865f3d346a1a2a60c4e84775622d74e3e7ef53e0dede9c68b81eab306cca` | `rust-v0.146.1` / `79b4f03d35962b005b007a015113b38930711665` |
| Codex Desktop | 26.803.5235.0 (runtime 0.147.0-alpha.6.5) | `fb5c760e14cf8fe86e12e49e8a3e7f237af06082d6b9fe1e411e463b7229c916` | `rust-v0.147.0-alpha.6.5` / `618b8e9111da9f57fe380b09d0f6516e3f343536` |

The Beta4 implementation base is
`be10f62f44b22fa8c84510238250ae11fb3ecab4`. That revision includes the
Beta4 Kimi thinking-model Chat tool-probe fix and no #392 production routing
change.

## Capture controls

- Every CLI, Desktop runtime, and app-server run used a newly created isolated
  Codex Home and workspace.
- A loopback protocol-controlled Responses server supplied deterministic V1,
  V2 spawn, wait, child completion, and final response streams.
- The V2 run observed declaration, call, result, `agent_message`, rollout
  version metadata, same-Home process restart/readback, and Desktop
  app-server item notifications.
- No existing user Home or task was opened. The known crash task was not read.
- Prompts, credentials, filesystem paths, thread/task IDs, call/item IDs, and
  message content are not retained in the JSON artifact.
- User-configured role and tool descriptions are dynamic and are excluded from
  classification. The normalized structural schema retains types, nesting,
  encryption flags, required fields, strictness, and additional-properties
  behavior.

## Observed decision

- V1 is one `multi_agent_v1` Responses namespace.
- V2 is one `collaboration` Responses namespace containing the six V2
  function children; it is not six top-level Responses functions.
- Both clients send `tool_choice: "auto"`.
- Neither frozen request carries `multi_agent_version`; the complete namespace
  and child schema is the request-visible discriminator.
- The client dispatch recipient, Responses namespace, function call/result,
  model-input `agent_message`, rollout metadata, and Desktop
  `agentMessage`/`collabAgentToolCall`/`subAgentActivity` items are distinct
  layers.

## Reproduction checks

Regenerate and validate the bounded artifacts with Python 3.13:

```powershell
python scripts/build_issue_392_collaboration_contract.py
python scripts/build_issue_392_collaboration_contract.py --check
python scripts/build_issue_64_collaboration_inventory.py
python scripts/build_issue_64_collaboration_inventory.py --check
python -m pytest -q tests/test_issue_392_collaboration_contract.py tests/test_issue_64_collaboration_inventory.py
```

The builder binds every cited frozen source file to its Git blob. Re-running
runtime capture requires the exact two binaries above and the same isolated
loopback controls; a client, source, or binary change invalidates this evidence
and requires a fresh capture rather than editing the artifact in place.
