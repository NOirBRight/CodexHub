# Issue #65: runtime tool compatibility table

Status: accepted Beta2 design input for #58. This is a structural contract,
not a production implementation, model qualification matrix, or live-provider
result.

## Evidence and boundary

The table is derived from:

- [ADR-0002](../../adr/0002-runtime-derived-tool-compatibility.md), which
  fixes `ResolvedRoute` before the request-scoped `ToolCompatibilityPlan`.
- [Issue #62 source contract](../issue-62/codex-0.146-source-contract.json),
  CLI `0.146.0`, `rust-v0.146.0`, source commit
  `e363b08c9175ac1cbe5893615dd2cb9ddf95043b`. Its 0.146 shape is a structural
  source contract (`capture_status=not_observed`), not a live qualification.
- [Issue #64 inventory](../issue-64/collaboration-v1-v2-inventory.json),
  which selects Collaboration V1 versus V2 before exposure or repair and
  preserves the separate continuation identities.

The plan is computed after the user-selected Provider and exact model are
resolved and before upstream sampling. It is keyed only by declaration shape,
the selected protocol's capabilities, and the request's optional/required
condition. An absent model-name row never denies a request.

The selected Provider/model, credentials, and protocol remain fixed for the
whole request. There is no model or Provider fallback, cross-Provider hosted
proxy, alternate codec retry, or output repair. The Gateway only translates
wire shapes; it never executes tools, creates or schedules agents, or forges
results. Execution ownership in the table is therefore independent of the
Gateway adapter.

`optional` means the declaration is not required by `tool_choice` or the
request contract. `required` means that the request contract requires that
declaration; omission cannot satisfy it. Every row has exactly one disposition
for each of those two conditions.

## Selected-protocol capabilities

The following predicates describe the selected protocol, not a model name:

- `F`: a complete native plain-function declaration, call, result, history,
  identity, and streaming lifecycle is supported.
- `N`: a complete native namespace declaration and namespace-child function
  lifecycle is supported.
- `C`: a complete native custom/freeform declaration, call, result, history,
  identity, and streaming lifecycle is supported.
- `S`: the exact client-executed `tool_search` declaration, call, output,
  history, and client-execution marker are supported.
- `H(k)`: the already-selected Provider supports hosted kind `k` on this
  protocol with its complete native lifecycle. Provider support alone is not
  `H(k)` if this protocol cannot carry the lifecycle.
- `U(k)`: the protocol explicitly defines unknown tag `k`, its opaque payload,
  identity, owner, and complete lifecycle. An opaque payload by itself is not
  `U(k)`.
- `A_ns` and `A_custom`: the corresponding request-scoped Adapter below is
  accepted by the protocol and is proven injective and reversible for the
  complete declaration/call/result/history/SSE lifecycle. A function-shaped
  declaration without those guarantees is not an Adapter.

The event names below are the Issue #62 Responses names. A different protocol
may use equivalent names, but it is native only when the complete lifecycle
and semantics are equivalent. Native declarations are never put in a generic
envelope.

### Client-owned search and Adapter distinction

The `A_ns`, `A_custom`, and (in the later #277 implementation) `A_search`
predicates are semantic contracts, not aliases inferred from a spelling. A
request-scoped Adapter is allowed only after the declaration has already been
classified as client-owned and the selected protocol has a complete,
injective, reversible lifecycle mapping. In particular, a plain Provider
function whose name happens to be `tool_search`, or a `tool_search_call` whose
`execution` marker is missing or not exactly `client`, remains a Provider item
and is never rewritten as Codex client search. The Gateway must fail closed or
leave that item untouched; it may not claim ownership from the name alone.

The adapted representation used by #277 is therefore an upstream transport
shape only. It is not a generic function fallback, does not transfer tool
execution to the Gateway, and is inverse-mapped back to the native
client-owned search lifecycle only when the request-local registry proves the
mapping.

## Compatibility table

| Runtime family and selected-protocol condition | Execution owner; native shape; model-visible exposure | Request encoding | Inverse call/result/history | SSE assembly | Ambiguity boundary | Optional disposition | Required disposition |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **Plain function — `F`** | `codex_client`; `function`, `function_call`, `function_call_output`; expose the original declaration unchanged. | Copy `type`, `name`, and `parameters` unchanged. Keep `tool_choice` and all native IDs unchanged. | Identity mapping for call, result, `call_id`, `item_id`, and history links. | Assemble `response.output_item.added`, `response.function_call_arguments.delta`, `response.function_call_arguments.done`, `response.output_item.done`, then the terminal event, keyed by the original item/call identity. | Unknown or duplicate identity, missing/incomplete arguments, duplicate terminal/done event, or an event that cannot be associated with exactly one call is a compatibility failure. | `native` | `native` |
| **Plain function — `not F`** | `codex_client`; no safe native shape; do not expose it. | Emit no declaration. For a required declaration, fail before sampling; do not invent a wrapper. | None: no call, result, or history item is sent upstream. | None. | The selected protocol has no complete function lifecycle. Plain functions have no generic fallback adapter in this table. | `omit` | `required-but-unavailable` |
| **Namespace, including a pre-classified V1/V2 namespace — `N`** | `codex_client`; native `namespace` containing child `function` tools and the native namespace-qualified call/result; expose unchanged. | Copy the namespace and every child declaration unchanged. Classify `multi_agent_v1` versus `collaboration` before this step. | Preserve namespace, child name, `call_id`, `item_id`, result links, and history order unchanged. V1 keeps `agent_id` semantics; V2 keeps `task_path`, `continuation_id`, `task_name`, and `fork_turns`. | Use the function-call lifecycle above, with namespace and child identity retained on added/delta/done/history items. | Missing namespace, flattened alias without namespace, unknown child, mixed V1/V2 field/tool, unknown identity, or incomplete stream fails closed before repair or forwarding. | `native` | `native` |
| **Namespace — `not N` and `F` and `A_ns`** | `codex_client`; upstream native shape is one `function` declaration per child under a request-scoped alias; expose the aliases, not a namespace wrapper. The Gateway is only the adapter. | Allocate a collision-free alias for each namespace/child and send the child's parameters unchanged. The alias registry records namespace, child, version, and the original declaration. | Alias maps back to the original namespace and child on call and result; all `call_id`, `item_id`, and history links are unchanged. Preserve V1 `agent_id` or V2 `task_path`, `continuation_id`, `task_name`, and `fork_turns`; never reinterpret them. | Assemble upstream function deltas by mapped alias and item/call ID before inverse mapping. Emit the native namespace lifecycle in original order. | Alias collision or unknown alias, malformed call/result envelope, missing mapping, lost ID/order, mixed V1/V2 fields, or an unproven continuation field is a bounded compatibility failure. | `adapt` | `adapt` |
| **Namespace — `not N` and not (`F` and `A_ns`)** | `codex_client`; no safe namespace shape; do not expose it. | Emit no namespace or guessed function alias. Required failure occurs before sampling. | None. | None. | Namespace support is absent and the complete alias inverse is not proven. | `omit` | `required-but-unavailable` |
| **Custom/freeform — `C`** | `codex_client`; native `custom`, `custom_tool_call`, and `custom_tool_call_output`; expose unchanged. | Copy custom `name` and `format` unchanged. Keep the freeform input/output and all native IDs opaque. | Identity mapping for `input`, `output`, call/result IDs, and history links. | Assemble `response.output_item.added`, `response.custom_tool_call_input.delta`, `response.custom_tool_call_input.done`, `response.output_item.done`, and the terminal event without parsing or repairing freeform content. | Unknown or duplicate identity, incomplete input, malformed freeform content, or an event that cannot be tied to one call fails at that stream/history boundary. | `native` | `native` |
| **Custom/freeform — `not C` and `F` and `A_custom`** | `codex_client`; upstream shape is a function alias with an adapter envelope; expose the adapted function. The Gateway maps shape only. | Allocate a request-scoped custom alias. Use exactly one function argument key, `__codexhub_custom_input`, containing the exact native input value. Use exactly one function result key, `__codexhub_custom_output`, containing the exact native output value. The original custom name and format remain in the registry and native history. | Strictly unwrap the two keys back to `custom_tool_call` and `custom_tool_call_output`; preserve every call/item ID and history link. No freeform value is parsed, repaired, or normalized. | Assemble the upstream function argument deltas completely before validating the envelope, then inverse-map the completed call and result while preserving item/call order and IDs. | Unknown alias, alias collision, missing/extra envelope key, wrong envelope type, malformed/incomplete delta, missing/duplicate ID, or missing registry entry is a bounded compatibility failure. | `adapt` | `adapt` |
| **Custom/freeform — `not C` and not (`F` and `A_custom`)** | `codex_client`; no safe custom shape; do not expose it. | Emit no custom declaration or lossy function wrapper. Required failure occurs before sampling. | None. | None. | The protocol cannot carry the complete opaque input/output envelope and inverse. | `omit` | `required-but-unavailable` |
| **Client-executed `tool_search` — `S`** | `codex_client`; native `tool_search`, `tool_search_call`, and `tool_search_output` with `execution=client`; expose unchanged. The client remains the discovery executor. | Copy the declaration, client execution marker, query arguments, and IDs unchanged. | Identity mapping for search call/output/history, including the discovered tool declarations. The Gateway does not consume the search or synthesize its result. | Use the bounded native shape: `response.output_item.done` followed by the terminal event; no synthetic argument delta stream. | Missing or changed client-execution marker, unknown/duplicate IDs, an invented Provider result, or a search item that cannot be represented exactly fails closed. | `native` | `native` |
| **Client-executed `tool_search` — `not S`** | `codex_client`; no safe client-preserving shape; do not expose it. A plain function alias would incorrectly transfer discovery semantics. | Emit no search declaration. Required failure occurs before sampling. | None. | None. | No exact client-execution declaration/result exists; no generic function or hosted adapter is permitted. | `omit` | `required-but-unavailable` |
| **Provider-hosted kind `k` — `H(k)`** | `selected_provider`; the exact hosted native shape, such as `web_search` / `web_search_call`, is exposed unchanged. The Gateway is not the hosted executor. | Send the exact hosted declaration and Provider-required fields on the already-selected route only. | Preserve the Provider's call/result/history identities and status/action fields unchanged; no second Provider is consulted. | Assemble the Provider's documented added/delta/done/terminal lifecycle exactly. A provider-defined delta is native only when the complete lifecycle is known. | Missing Provider support, an unsupported event/field, unknown identity, or incomplete Provider lifecycle is not a native condition and fails the preflight/stream boundary. | `native` | `native` |
| **Provider-hosted kind `k` — not `H(k)`** | `selected_provider`; no Gateway or alternate-Provider executor; do not expose it. | Omit the hosted declaration. If required, return a stable bounded unavailable error before any upstream sampling. | None; no hosted history item is fabricated or proxied. | None. | The selected Provider or selected protocol cannot prove exact hosted support. Cross-Provider proxying is forbidden. | `omit` | `required-but-unavailable` |
| **Unknown/future kind `k` — `U(k)`** | The owner is only the explicit extension contract for `k`; the Gateway never infers or executes it. Preserve the opaque native tag/payload and expose it only when that exact lifecycle is declared. | Copy the exact opaque declaration, tag, payload, and identity without normalization. | Identity mapping for the declared opaque call/result/history and IDs; no guessed field names. | Preserve the extension's exact added/delta/done/terminal sequence and opaque fragments. | Any unknown tag, missing extension contract, malformed/ambiguous payload, or identity/lifecycle mismatch fails closed; opaque preservation is not semantic repair. | `native` | `native` |
| **Unknown/future kind `k` — not `U(k)`** | No execution owner is established; do not expose it. | Emit no declaration or generic wrapper. Required failure occurs before sampling. | None. | None. | The protocol cannot prove the exact tag and complete reversible lifecycle. | `omit` | `required-but-unavailable` |

The `required` cell in a native or adapted row is valid only when its
capability predicate is true. If a required declaration is not safely native
or adapted, the corresponding unavailable row applies; it is never silently
omitted.

## Adapter and lifecycle contract

The following rules make `A_ns` and `A_custom` implementable without adding
product policy:

1. Create one immutable alias registry per request. Allocate aliases from
   reserved family prefixes such as `__codexhub_ns_` and
   `__codexhub_custom_`, using an opaque request token and ordinal. Check each
   candidate against every native name and already allocated alias; on any
   collision allocate another candidate. The registry, not alias parsing,
   performs the reverse lookup. No alias state is shared across requests.
2. The registry is injective: distinct namespace/child or custom declarations
   receive distinct aliases, including when their original names collide with
   one another or with a reserved prefix. If the selected protocol cannot
   accept the allocated alias or envelope, the adapter predicate is false.
3. Encode all adapted declarations and request history with that registry.
   Decode upstream calls/results and client results with the same registry.
   History is the inverse of the same mapping: it preserves native names,
   namespace/version, continuation fields, call/item IDs, result links, and
   order. It is not a chance to rename or reorder a prior item.
4. For an adapted stream, assemble deltas per exact item/call identity before
   inverse mapping. An unknown alias, malformed or incomplete delta, missing or
   duplicate identity, duplicate terminal marker, or any other ambiguous
   inverse produces a visible bounded compatibility error at that boundary.
   Do not partially forward, retry with another codec, repair output, or
   reroute the request.
5. Collaboration V1/V2 classification happens before aliasing. A V2
   `collaboration` namespace is never sent through V1 repair and is never
   downgraded to V1. The client owns both lifecycles; the table establishes
   only representability.

## Deferred scope

`tool_search` receives only the representability rule above. Its complete
discovery/recovery workflow remains Beta3. Collaboration V2 receives only the
namespace representability rule; full spawn/message/follow-up/wait/interrupt/
list, restart, pagination, and cross-Home history qualification remain Beta4.
This document does not implement either workflow, qualify a Provider/model, or
authorize a release.
