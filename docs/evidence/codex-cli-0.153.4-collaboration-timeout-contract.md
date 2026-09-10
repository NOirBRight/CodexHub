# Codex CLI 0.153.4 Collaboration timeout contract

This is the source pin used by the Gateway's request-boundary integer
validation.  It is not a claim that every future CLI or a user-overridden V2
configuration has the same limits.

| Protocol | Wire type | Minimum used by the 0.153.4 default contract | Maximum |
|---|---|---:|---:|
| V1 `multi_agent_v1.wait_agent.timeout_ms` | JSON number, Rust integer handler | 10,000 ms | 3,600,000 ms |
| V2 `collaboration.wait_agent.timeout_ms` | JSON number, Rust integer handler | 10,000 ms default | 3,600,000 ms default |

## Source evidence

- Upstream repository: `https://github.com/openai/codex`
- Tag: `rust-v0.153.4`
- Tag commit: `042fb41b7c813ac7999105e886b2b7aa715b5081`
- `codex-rs/core/src/config/mod.rs` defines
  `DEFAULT_MULTI_AGENT_V2_MIN_WAIT_TIMEOUT_MS = 10_000` and
  `DEFAULT_MULTI_AGENT_V2_MAX_WAIT_TIMEOUT_MS = 3600 * 1000`.
- `codex-rs/core/src/tools/handlers/multi_agents_common.rs` uses those
  defaults for the V2 configurable options and defines the V1 handler's
  `MIN_WAIT_TIMEOUT_MS`/`MAX_WAIT_TIMEOUT_MS` from the same values.
- `codex-rs/core/src/tools/handlers/multi_agents_spec.rs` emits a JSON Schema
  `number` for both namespace declarations because the schema is shared with
  older clients; the handler deserializes an integer and clamps/rejects using
  the bounds above.

The Gateway therefore accepts only an exact integral JSON value in the
request-local Collaboration adapter.  Decimal or exponent spellings that are
mathematically integral may be canonically emitted as a JSON integer for an
adapted route; native namespace passthrough remains byte-for-byte unchanged.
V2 user configuration can lower its minimum to zero in the CLI, but the
isolated Codex 0.153.4 E2E configuration uses the shipped defaults and does
not override `multi_agent_v2`.
