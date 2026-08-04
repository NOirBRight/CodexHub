# ADR-0002: Runtime-derived tool compatibility on an immutable selected route

Date: 2026-08-04
Status: Accepted for 0.1.8 Beta2; implementation remains a follow-up

## Context

The user selects the Provider and model before a request is sent. The
installed Codex runtime then supplies a request-scoped set of tool
declarations. Those declarations include client-executed tools, hosted tools,
namespaces, and custom/freeform shapes whose wire representation depends on
the selected upstream protocol. A protocol mismatch must not silently change
the user's model, invoke another Provider, or make the Gateway the owner of a
tool or agent lifecycle.

Beta2 therefore needs one architecture contract for the seam between route
selection and protocol representation. The contract is structural: it is
about declaration type, lifecycle, and selected protocol capabilities, not a
catalogue of model names. It applies to maintained Providers and unknown
custom endpoints alike.

## Decision

### Two non-overlapping request stages

Route selection and compatibility planning are separate stages with separate
inputs and authority.

**`ResolvedRoute`** is the immutable result of the user's Provider/model
selection. It binds exactly one Provider and exact model ID to its endpoint,
auth reference, and upstream protocol. Resolving the route may validate the
configured binding, but it never consults tool compatibility to choose a
different model or Provider. The selected binding remains the identity for the
whole request, including any permitted reconnect or transport retry.
The exact user-selected Provider/model identity is preserved end to end.

**`ToolCompatibilityPlan`** is an immutable, request-scoped result computed
after route resolution. It classifies each runtime declaration using the
declaration's structure, the already-selected protocol's representational
capabilities, and request requirements such as `tool_choice`. It records the
disposition and any structural Adapter needed to encode and decode that
declaration. It cannot mutate `ResolvedRoute`, select a model, select a
Provider, or add execution/scheduling authority.

The resulting flow is:

```text
user Provider/model selection
        -> ResolvedRoute (one exact binding)
        -> ToolCompatibilityPlan (per declaration, per request)
        -> selected upstream wire request/response
```

### Four deterministic dispositions

`tool_choice` and the request contract determine whether a declaration is
optional or required. The plan must resolve every declaration to exactly one
of these dispositions; it must not leave an implicit fallback path.

| Disposition | When it is valid | Optional declaration | Required declaration |
| --- | --- | --- | --- |
| `native` | The selected protocol carries the declaration and its lifecycle with the same semantics. | Include it unchanged. | Include it unchanged. |
| `adapt` | The selected protocol cannot carry the native shape directly, but a structural Adapter is injective, request-scoped, and invertible for the complete lifecycle. | Apply that Adapter. | Apply that Adapter; if any required lifecycle mapping is not provably reversible, use `required-but-unavailable`. |
| `omit` | The optional declaration is either provider-hosted without native support from the selected Provider, or not safely representable on the selected protocol. | Leave it out of the model-visible plan and emit only a bounded, sanitized classification diagnostic. | Not valid; a required declaration becomes `required-but-unavailable`. |
| `required-but-unavailable` | The required declaration is not native or safely adaptable on the selected protocol, or is provider-hosted without support from the selected Provider. | Not valid. | Fail visibly before upstream sampling, with a stable bounded error; do not fall back or proxy. |

An optional hosted tool that the selected Provider does not natively support is
`omit`. If `tool_choice` requires that hosted tool, it is
`required-but-unavailable` and the request fails before upstream sampling.
There is no second Provider for search, compaction, or any other hosted
operation. Client-executed tools remain owned and executed by the Codex
client; lack of Provider execution support alone does not omit one when the
selected protocol safely represents it. The same optional/required rules apply
to client-executed, namespace, custom/freeform, and future declaration kinds.

### Native passthrough and Adapter boundaries

Native-compatible declarations and wire items pass through unchanged. An
Adapter exists only for a reversible structural mismatch; it does not add
semantics or choose execution behavior. Every Adapter is:

- request-scoped, with no alias or lifecycle state shared across requests;
- injective and collision-free for the declaration names, envelopes, and IDs it
  encodes; and
- invertible across the entire request/response lifecycle.

For an adapted declaration, the inverse must preserve the original meaning,
exact tool-call IDs, and ordering through declaration, call, result, streaming
`added`/`delta`/`done` (or the selected protocol's equivalent), and replay or
history. Before sending, a declaration whose complete lifecycle cannot be
proven is `omit` when optional or `required-but-unavailable` when required.
After an `adapt` declaration is sent, an unknown alias, malformed or
incomplete delta, missing or duplicate ID, or any other ambiguous inverse is a
visible bounded compatibility failure at that stream/history boundary. It is
not partially forwarded, silently repaired, retried with another codec, or
rerouted. History is the inverse of the same request-scoped mapping; it is not
an opportunity to rename, reorder, or reinterpret a prior call/result.

An Adapter may make a Collaboration V2 namespace representable on a selected
protocol, but it may not downgrade V2 to V1 or run V1 repair logic. V1 and V2
selection is decided before any schema repair or adaptation, and each version's
declaration, call, result, stream, and history fields remain isolated.

### Ownership and Provider identity

CodexHub adapts protocol shapes only. The Gateway does not execute tools,
create agents, schedule subagents, forge tool results, or become a second
Collaboration scheduler. The Codex client remains the owner and executor for
client tools and agent lifecycles; representability of a declaration is not
evidence that the Gateway can execute it.

The selected `ResolvedRoute` is authoritative for all model inference and
hosted capabilities. Explicit and implicit cross-Provider tool proxying are
forbidden. There is no model/provider fallback: no compatibility failure,
unsupported hosted tool, alias conflict, or upstream error may select or
contact another Provider/model. Beta2 also does not retry with an alternate
codec or repair a model's output format. A visible failure or optional omission
is the terminal compatibility outcome; any transport retry that exists outside
this ADR must keep the same route and codec.

### Capability and endpoint policy

Tool behavior is derived from the runtime declaration type and selected
upstream protocol, with hosted support taken only from the already-selected
Provider. There is no model-name allowlist, model qualification gate, or
production dispatch branch for GLM/K2.7 (or any other model name). A model
slug is never a codec selector.

Unknown custom endpoints use these same conservative type/protocol rules. A
maintained-Provider regression, fixture, or release qualification result is
evidence for release confidence; it is not runtime eligibility and does not
create an endpoint-specific dispatch exception. If the endpoint's protocol
cannot prove native or reversible representation, the plan uses `omit` for an
optional declaration or `required-but-unavailable` for a required one.

## Consequences

The plan is predictable and auditable: route identity is fixed before
compatibility, optional unsupported tools disappear explicitly, and required
unsupported tools fail before network work. Native paths remain semantically
unchanged, while the only permitted adaptations are bounded, reversible
request-local mappings that preserve IDs, order, streaming, and history.

The trade-off is intentional loss of unsupported optional capabilities on
unknown or conservative endpoints. Beta2 does not hide that loss behind a
model fallback, a second Provider, an alternate codec retry, or output repair.

## Alternatives considered and superseded

### Choose a model or Provider from a compatibility matrix

Rejected. The user has already selected the Provider/model, and a
`ToolCompatibilityPlan` is not a routing authority. Model-name allowlists,
qualification gates, and GLM/K2.7-specific production branches would make
compatibility dispatch policy rather than structural adaptation.

### Proxy a hosted tool through another Provider

Rejected. Both explicit delegation and implicit “helpful” proxying violate
the selected route's identity, credentials, and semantics. Unsupported hosted
tools are omitted or fail visibly on that same route.

### Retry with another codec or repair model output in Beta2

Rejected. An alternate-codec retry changes the contract after route planning,
and output repair cannot establish a reversible wire lifecycle. Neither is a
Beta2 compatibility outcome.

### Wrap every declaration in a generic envelope

Rejected. Wrapping a native declaration adds mutation and can break exact
stream/history semantics. Native declarations pass through; an Adapter is
introduced only when a documented structural mismatch has a complete inverse.

### Let the Gateway execute tools or schedule agents

Rejected. Gateway-owned execution, agent creation, and Collaboration
scheduling would make protocol adaptation an unauthorized second runtime.
The Codex client owns those actions; V1 repair and V2 adaptation remain
isolated.

## Scope and follow-up boundaries

- **#62** owns the bounded Codex CLI 0.146 runtime and Responses lifecycle
  inventory. Its fixtures are structural evidence, not a model qualification
  gate or a reason to proxy through another Provider.
- **#64** owns the Collaboration V1/V2 schema boundary and the signal selected
  before repair. This ADR consumes that boundary and does not qualify the full
  V2 workflow.
- **#65** owns the model-agnostic compatibility table that applies these four
  dispositions to each observed declaration family. This ADR defines the
  contract; it is not the table or a production allowlist.
- **#198** owns the implementation isolation that makes V1 repair structurally
  unable to run on V2. V2 namespace adaptation may establish representability,
  never Gateway-owned execution or a V2-to-V1 downgrade.
- **#58** owns the runtime compatibility implementation, tests, diagnostics,
  and network/manual evidence. This ADR adds no product implementation and
  does not authorize behavior outside the contract above.

The ADR is intentionally limited to the Beta2 architecture contract. Runtime
inventory, compatibility-table details, V1/V2 hardening, and the product
implementation remain in their respective issues.
