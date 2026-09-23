# ADR-0015: Claude subscription coexistence, family mapping and metering

Date: 2026-09-23. Status: Accepted product and architecture direction. The local
authentication carrier was qualified with Claude Code 2.1.280 in #560; rendered
picker and explicit-model resume behavior remain part of combined candidate
evidence in #564.
Published specification: [#559](https://github.com/NOirBRight/CodexHub/issues/559).

Claude Code's saved subscription authentication remains active while its model
requests traverse CodexHub Gateway. Native IDs pass through unchanged to the
official Anthropic endpoint; external projected IDs use their own provider
credentials. This preserves subscription choices and lets the existing Usage
Statistics pipeline observe both routes. It replaces ADR-0014's Gateway-token
requirement and launcher-only coexistence recommendation for this connection mode.

Family mappings operate through the client's native alias resolution. A full
native ID explicitly supplied for the current invocation is never rewritten
by matching its family name. Claude Code 2.1.280 can re-resolve a resumed
conversation through the current default/family mapping when `--resume` is used
alone, even if it began with an explicit full ID. To preserve an old session's
native model, the user resumes with `claude --resume --model <original-full-id>`
(or supplies its session ID after `--resume`). CodexHub displays a copyable
command after the user enters that ID; it cannot safely infer the old choice
from the request arriving at Gateway. Connect preserves the default model;
editing a family mapping deliberately changes the effective target of defaults
using that alias, with the change disclosed in preview. Main-model selection and
Default subagent remain separate from family mapping.

The Gateway observes native usage while preserving request/response semantics.
New connected traffic is recorded in the existing Usage Statistics page with
actual Provider/model identity, token and cache counts and truthful completeness.
This decision does not import history or add a subscription quota dashboard.

## Credential boundary and consequences

- Claude Code 2.1.280 carries the local Gateway credential in
  `ANTHROPIC_CUSTOM_HEADERS` as `x-codexhub-gateway-key`, alongside its saved
  OAuth bearer and OAuth beta header. CodexHub must consume and validate that
  local header without replacing subscription authentication. The same probe
  showed OAuth-only model discovery is skipped, so the managed client projection
  is published additively through `modelPicker.options`; the built-in lineup is
  left in place.
- Subscription credentials remain client-carried and can reach only the official
  Anthropic destination. External provider dispatch strips incoming credentials.
  Tokens do not enter telemetry, configuration backups or persisted usage data.
- ADR-0005's persisted subscription credential seam is not extended to acquire
  or refresh Claude tokens. This narrowly authorizes pass-through of the
  authenticated Claude client's request, not reuse by unrelated clients.
- Failed/disabled mappings never silently substitute another model. Disconnected
  direct traffic is not observable by Gateway and is not claimed as recorded.
- ADR-0014's declared compatibility adaptation, explicit identity, conflict
  handling, rollback and evidence requirements otherwise remain in force.

See the [confirmed discussion](../research/2026-09-23-claude-coexistence-planning.md)
and [implementation plan](../research/2026-09-23-claude-coexistence-implementation-plan.md).
