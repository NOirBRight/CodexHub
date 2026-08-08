# Issue #369: Official Collaboration V1/V2 CLI evidence

This is a bounded, sanitized evidence index for the Beta3 Official-model
capability matrix. It records lifecycle phase outcomes only; it does not retain
session identifiers, prompts, reasoning, tool arguments/results, credentials,
or opaque agent/task identities.

## Candidate and runtime

- Candidate implementation revision: `c7f5f13949a767f62601d0b7d0e146cdb10c4098`
- Codex CLI: `0.146.1` (source-contract floor `0.146.0`)
- Gateway: isolated V1 and V2 loopback instances
- Capture Home: persistent isolated Homes (not `--ephemeral`, so restart/readback
  could be checked)
- Capture date: `2026-08-08`

The machine-readable result is
[`official-v1-v2-cli-matrix.json`](./official-v1-v2-cli-matrix.json). The
matrix validator is the authoritative check for required fields, candidate
binding, sanitization, and selector fail-closed behavior.

## Luna evidence

`gpt-5.6-luna` completed both model-level selections:

- V1: spawn, returned `agent_id` identity kind, send input, wait/result,
  close, SSE terminal, restart/readback, and a bounded terminal/error replay
  all observed.
- V2: spawn, returned `task_path` identity kind, list, send message,
  follow-up, wait/result, list, interrupt, SSE function-call and terminal
  events, restart/readback, and a bounded terminal/error replay all observed.

The evidence includes two V2 interrupt probes and negative validation probes
for an invalid task name. The error outcome was classified as terminal and
replayable without retaining the original request data. No duplicate execution
or reconnect storm was used as a qualification signal.

Accordingly, Luna is the only list-visible Official row with an accepted `GO`
verdict and an enabled model-scoped V1/V2 selector in this candidate.

## Terra and other Official rows

`gpt-5.6-terra` has bounded V2 and coarse V1 observations, but the required V1
stream/history and restart/readback phases were not captured. It remains
`PARTIAL` and selector-ineligible. Sol, 5.5, 5.3-codex-spark, 5.2, 5.4,
5.4-mini, and the hidden auto-review row remain `UNQUALIFIED` or `NO-GO` as
recorded in the matrix; no capability is inferred from a catalog value or a
simple text response.

## Qualification boundary

This evidence qualifies official model rows only. It does not claim that every
third-party model supports native Collaboration V2, does not enable a global
V1/V2 switch, and does not make the Gateway an agent executor. Generic
third-party Collaboration V2 lifecycle qualification remains a later Beta4
scope.
