# ADR-0014: Claude Code Gateway compatibility and explicit client activation

Status: Accepted product direction; implementation and live verification pending.

## Context

The user confirmed Claude Code as a downstream CodexHub Gateway client, not an
ACP plugin or AgentProvider. The canonical product specification is
[issue #73](https://github.com/NOirBRight/CodexHub/issues/73); implementation and
evidence remain in issues #74–#78. This decision revises the strict compatibility
policy of ADR-0001 and adds a narrow activation exception to ADR-0004. It does
not claim the retired spike or its scoped PARTIAL evidence proves readiness.

## Protocol compatibility

Claude Code sends Anthropic Messages to the Gateway. All three upstream formats
are required: Anthropic Messages, OpenAI Responses, and Chat Completions.
Native Anthropic routes preserve requests, semantic headers, JSON responses,
and incremental SSE where possible rather than round-tripping through a lossy
OpenAI representation. Gateway authentication, upstream credentials, model
resolution, transport safety, and redaction remain Gateway-owned.

Prefer equivalent conversion; where unavailable, attempt best-effort adaptation
without a separate opt-in. Every non-equivalent adaptation has a named policy
and a diagnostic explaining which field was transformed or omitted and why.
Do not silently remove user text, images, or tool results; break Call identity;
fabricate success or usage; or silently substitute a different model. If no safe
adaptation can carry essential content, return an actionable error. Best effort
does not authorize blind retry after partial output or possible side effects.
Unrepresentable usage detail must not be folded into invented base counts;
report only supported truthful values and disclose unavailable detail.

ADR-0001's pure representation seam, ordered content, opaque-field preservation,
credential separation, and declared-adaptation outcomes remain required. Its
blanket rejection of non-equivalent options and unmapped usage detail is replaced
by the policy above. The native path must not lose fields merely because a
cross-protocol adapter cannot represent them. Thinking, beta/body pairs, caching,
images, compaction, server tools, token counting, and cancellation require an
explicit evidence-backed handling policy, not an unsupported-equals-hidden rule.

Request details show sanitized adaptation diagnostics; the Claude Code card shows
a concise compatibility state. Neither inserts diagnostic prose into model
answers nor records credentials or full prompts as diagnostic payloads.

## Model selection

Every model in the existing enabled, Gateway-exported catalog must be selectable
inside Claude Code, not merely visible in CodexHub or usable as one custom default.
Do not introduce capability-based filtering or a second editable model list.
Claude-compatible model IDs, when required by the verified CLI, must resolve
unambiguously to the original Provider/model identity; display labels are not
routing identities. Mixed-Provider lists retain ADR-0011's derived Flat Label;
Claude-compatible aliases do not rename Provider models or invent capabilities.
Existing clients' model discovery must remain compatible.

The default model and mappings from Claude Code's fixed names/role aliases are
separate user choices. Explicit model selection must not be overridden by a role
mapping. Catalog changes automatically update the Client Projection; removed or
disabled mapping targets become visibly invalid and never silently fall back.
Exact supported role names, discovery format, and refresh/restart behavior are
facts to establish against the pinned CLI in #74/#77, not assumptions here.

## Claude Code activation exception

Unlike provider-map clients, Claude Code's selected integration uses current-user
settings to point its default route at the Gateway. An explicit Connect action
may change the necessary route/default-model fields after disclosing this scope.
This supersedes #76's launcher-first default; a generated launcher is not a
required deliverable. Automatic republish is not permission to activate an
otherwise disconnected client or overwrite a user's conflicting edits.

Use the existing managed-client coordinator with a Claude-specific adapter.
The Injected Block is the exact set of managed settings/environment keys, not
an invented provider entry. Preserve unrelated configuration; back up previous
values, publish atomically, detect concurrent edits, verify the owned fields,
and restore only those fields on Disconnect. Preserve subsequent foreign edits
and surface conflicting managed-field edits rather than replacing the whole
file. Never modify project configuration or Claude login credentials as a side
effect. Write only the local Gateway credential via ANTHROPIC_AUTH_TOKEN, never
an upstream API key or subscription credential; mask secrets in previews and
protect backups. Inspect environment/project overrides and report ineffective
configuration without rewriting those overrides. The initial credential contract
requires ANTHROPIC_AUTH_TOKEN/Bearer; an effective conflicting API-key or helper
configuration is reported as a connection conflict, not silently cleared. #74
must verify actual carrier precedence; accepting x-api-key is not authorized by
this decision and would require the same Gateway-key validation if later added.

The Claude Code card keeps connection/status controls and adds a dedicated
settings dialog: read-only searchable exported models, default model, searchable
fixed-name/role mappings, pending changes, conflicts, and accurate restart
requirements. Other Clients receive no Claude-specific controls. Reuse the
existing dialog, persistence, and Toast lifecycle rather than building a generic
client-settings framework.

## Evidence and release gates

#74 must re-establish the wire contract with a pinned real Claude Code and
approved real upstream access for all three protocols before production routing
is authorized. #75 implements the verified protocol contract; #77 implements
complete selection/mapping; #76 integrates safe configuration and UI; #78 proves
the combined release. No client configuration UI ships ahead of working Messages
text, tools, and streaming. Historical PARTIAL evidence remains historical.

Each upstream path must demonstrate real CLI multi-turn text, incremental
streaming, tool call/result round trips, cancellation, and error handling. The
native Anthropic path additionally proves semantic-field and SSE preservation.
Deterministic tests cover malformed input, adaptation policies, credentials,
alias collisions, disabled/removed models, configuration rollback and conflicts.
UI evidence covers model selection, mappings, connect/disconnect, synchronization,
and restart feedback without regressing other Clients. Publish verified CLI
versions and limitations in English and Chinese; do not claim Anthropic endorses
non-Claude model compatibility. Protocol/configuration implementation is strict
work under the repository verification policy; this documentation change is fast.

## Alternatives not selected

- Strict rejection of every non-equivalent option prevents useful compatibility;
  unconstrained field dropping conceals data loss. Declared best effort retains
  usability without pretending semantic equivalence.
- Launcher-only setup and a single custom model do not meet the requested Clients
  experience or full model picker. Current-user activation is explicit and
  reversible rather than silently extending activation to every managed client.
- ACP/session hosting is a different product and is not part of this campaign.
