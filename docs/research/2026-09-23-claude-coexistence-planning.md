# Claude subscription and Gateway coexistence: discussion map

Status: product direction confirmed; remaining factual checks are recorded in the
[implementation plan](2026-09-23-claude-coexistence-implementation-plan.md).
[ADR-0015](../adr/0015-claude-subscription-coexistence-and-metering.md) records the
architecture direction. Implementation and candidate delivery are not complete.
The synthesized specification is published as
[#559](https://github.com/NOirBRight/CodexHub/issues/559), marked ready-for-agent.

## Accepted product direction

1. Preserve access to all native subscription models alongside Gateway models. Distinguish actual source/payment route in model choices; use richer grouping in CodexHub and supported picker configuration in Claude Code.
2. Prefer family mapping over task-purpose routing. A family alias such as `opus` may map to a Gateway model, while an explicitly selected complete native ID such as `claude-opus-5-5` keeps its subscription route. Never rewrite a complete native ID merely because it contains a family name.
3. Add Claude native request usage to the existing CodexHub Usage Statistics page: tokens, requests, cache usage/hit rates and existing relevant dimensions. Subscription allowance percentages and a quota dashboard are not this task.
4. No automatic cross-source fallback by default. Explicit fallback must respect restrictions on retrying after partial output or effects.
5. Connect preserves the default model. The user can change it separately.
6. All runtime verification uses isolated configurations, ports and synthetic conversations. Do not modify or restart the user's active sessions.
7. Editing a family mapping deliberately changes the effective target of a default that uses that alias. Preview this change; do not silently pin the previous native version. Connect alone still preserves the default.
8. Do not import historic usage. All new model requests in the connected coexistence route, including native subscription models, must enter the existing Usage Statistics pipeline.

## Evidence and implementation boundaries

- [Live feasibility evidence](2026-09-23-claude-coexistence-live-verification.md) covers one CLI version, native Haiku and external DeepSeek, same-process switching, resume and manual compaction. Family precedence, automatic compaction and native usage metering still need verification.
- [Official documentation findings](2026-09-23-claude-subscription-gateway-coexistence-docs.md) distinguish subscription authentication, model selection and routing.
- `src-python/claude_code_projection.py:22` and `src-tauri/src/gateway/clients/claude.rs:17` bind families to native `ANTHROPIC_DEFAULT_*_MODEL` variables. Main-model and default-subagent settings remain separate. Family mapping can affect alias-based internal calls and manual alias choices; it is not internal-task-only routing.
- Apply mappings at client alias resolution; Gateway resolves the resulting exact identity. A second Gateway family-name rewrite would lose the distinction between explicit and alias-derived native IDs. Do not add `modelOverrides` replacements for the exact native IDs we promise to preserve.
- A mapped family can change its built-in picker row. Preserving explicit native versions may require additional exact-ID rows. Validate rendering, duplicate suppression and subscription-version discovery before committing to a menu layout.
- The five documented request classes (main/subagent/workflow/compaction/auxiliary) may serve statistical attribution. They are not selected as the primary routing policy; absent classifications remain unknown.
- `gateway_events.py` reads generic JSON input/output usage but lacks native Messages stream merging and cache normalization. First-usage capture is insufficient for initial/final stream usage and upstream retry attempts.
- `gateway/telemetry.rs` summarizes Gateway request records. `StackedUsageChartShell.tsx` calculates cached-input/input ratios. Native input and cache counters require a consistent denominator to avoid double-counting or ratios over 100%.
- No Claude transcript import was found. Historic/direct sessions cannot appear in Gateway statistics without a separate source.

## Family behavior contract to verify

| Selection | Intended behavior |
| --- | --- |
| Unmapped family | Native CLI family behavior |
| Mapped family | Configured Provider/model |
| Complete native version chosen manually or retained by an old session | Same subscription model, no family rewrite |
| Explicit projected Gateway model | That exact exported Provider/model |
| Internal call governed by a family default | Native client mapping applies |
| Internal call pinning a complete native ID | Preserve identity; do not assume every hardcoded call honors family settings |
| Missing/disabled mapping target | Visible invalid mapping/error, no silent substitute |

This is the target contract, not a claim that every row passed a live test.

## Proposed usage measurement contract

- Reuse the existing database, aggregation and Usage Statistics page. Attribute native traffic to Claude Code / Claude subscription and the actual model; attribute external traffic to its actual Provider/model, not to the mapped family.
- Normalize reported input/output tokens, cache-read and cache-write tokens, retaining enough native usage detail to verify totals. Do not invent reasoning counts or add reasoning twice when already included in output.
- For native Anthropic accounting, total input includes uncached input, cache creation and cache reads. Token cache-hit rate is cache-read tokens divided by total input. Aggregate known token counts before calculating the ratio; do not average request percentages. Keep request-hit ratio separate if exposed.
- Unknown/partial usage is not zero. Stream events update one attempt and are not separate requests; cumulative output counters must not be repeatedly added.
- Downstream request count and upstream attempt count are distinct. Preserve known consumed usage across attempts without duplicating telemetry. Align the visible request-count definition with the existing page.
- Existing API-value estimates remain distinct from actual subscription charges. This feature does not require quota polling or a status-line bridge.

## Native request measurement path

`Claude Code -> local Gateway -> official Anthropic endpoint`, with the original
complete native model ID and client-supplied subscription authentication. Native
means the model and subscription are preserved; the model request still traverses
the local Gateway while connected. External selections take a separate provider
route with separate upstream credentials.

The native relay forwards the response and observes usage without rewriting
model content. Merge Messages stream start/delta usage into one attempt result,
normalize input/cache/output counts, and persist to the existing telemetry store.
Main turns, subagents, compression and auxiliary model requests through this
route are included; missing purpose labels do not exclude requests from totals.
Failures/cancellations retain request status and observed usage completeness.

This coverage requires the configured route to be effective. Disconnected or
environment/project-overridden direct calls bypass Gateway and are not observed;
do not describe them as recorded. No content/history import is required for this
measurement path, and prompts or credentials are not usage-statistics fields.

This is the planned product path. The prior relay experiment established native
subscription inference but did not implement or verify product usage collection.

## Remaining verification backlog

| Area | Boundary |
| --- | --- |
| Selection precedence | Explicit IDs, family aliases, inherited children, custom agents, `opusplan`, invocation-specific models and native fallback |
| Picker lifecycle | Native versions, mapped-family labels, duplicate native/API models, stable external IDs, refresh, removal and stale selections |
| Capabilities | Thinking, effort, tools, images, signed reasoning/history, cache TTLs, smaller context and count_tokens |
| Compaction | Manual, automatic, context-error recovery and cross-model histories |
| Credentials | Client-owned OAuth refresh, non-overriding local Gateway auth, fixed destinations, redirects, redaction and no cross-client subscription reuse |
| Metering | Messages JSON/SSE, retries, cancellation, partial data, request/attempt counts, cache ratios and model attribution |
| Configuration | Project/environment/managed overrides, concurrent edits, owned-key restoration and restarting/resuming existing sessions |
| Compatibility | Pinned CLI versions, Linux/Windows, subscription model variants and live evidence |

## Architecture decisions to reconcile

- Revise ADR-0014's token requirement and launcher addendum for subscription-preserving coexistence, while preserving explicit model identity.
- Distinguish ADR-0005's persisted Anthropic credential-adapter exclusion from client-carried pass-through. This plan does not acquire or refresh login tokens.
- Reuse ADR-0011 Display Name, Flat Label and Client Projection; do not invent another editable catalog.
- Reconcile ADR-0013 default-subagent ownership and invalid-target behavior with the Claude mapping contract.

## Planning sequence

Resolve remaining decisions, isolate-test unsettled facts, record consequential decisions in an ADR, and confirm shared understanding. Then produce the implementation spec, dependency-ordered issues and strict auth/routing/metering acceptance matrix.
