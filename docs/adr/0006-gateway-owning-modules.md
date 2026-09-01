# ADR-0006: Gateway owning modules and a thin process entry

Date: 2026-08-23
Status: Accepted for campaign #460 (Deep Modules v3)

## Context

The Gateway HTTP process used to load `gateway_runtime.py` and
`gateway_handler_impl.py` into `codex_proxy.py` with `exec`. Tests patched
`codex_proxy.<name>` and a `_live` helper copied those patches into adapters.
That made the entry a second copy of every Gateway name. Campaign #460 deletes
that copy.

## Decision

The Gateway process entry (`codex_proxy.py`) owns only HTTP wiring:
`CodexProxyHandler`, `run_server`, and `main`. It stays under 500 lines and
does not `exec` sibling sources or `setattr` handler methods.

Campaign #491 DM-5 replaces the ExchangeHooks/ExchangeFailureTypes callback
bags with four real ports owned by gateway_exchange: ExchangeTransport,
DownstreamPort, ExecutionControl, and ExchangeObserver. Production adapters
live in gateway_exchange_adapters.py and read owning-module attributes at call
time (ADR-0007); scripted adapters are used in tests. Fixed request policy
(mutation order, retry classification, protocol fallback, elapsed limits,
event payload construction) stays inside the owning module.

Each seam lives in one owning module. Tests patch and import that module:

| Seam | Owning module |
|---|---|
| Route Plan | `route_plan` / `route_primitives` |
| Catalog / model identity | `gateway_catalog_runtime`, `catalog`, `catalog_sync` |
| Transport / retry / Official HTTP | `gateway_transport` |
| Events / diagnostics | `gateway_events` |
| Compatibility application | `gateway_compat/` |
| Stream semantics / terminal detection | `gateway_stream_semantics`, `gateway_sse` |
| Request decode / local auth / Vision Proxy factory | `gateway_request`, `vision_proxy` |
| Error payloads | `gateway_errors` |
| Relay | `gateway_relay`, `gateway_relay_passthrough` |
| Handler method bodies | `gateway_handler_impl.GatewayHandlerMixin` |
| Admission / shutdown | `gateway_admission` |

`GATEWAY_EVENT_WRITER` is a process-wide singleton in `gateway_events`. No
extracted module imports `codex_proxy` or a deleted `gateway_runtime`.

File-size gates keep top-level `gateway_*.py` under 3000 lines and
`tool_compatibility/` / `gateway_compat/` files under 2000 lines.
`tests/test_entry_discipline.py` and `tests/test_seam_discipline.py` pin this.

## Consequences

Adding a Gateway behavior means adding or extending an owning module, then
importing it from the handler mixin or the entry. It does not mean growing
`codex_proxy.py` as a dump of names. Test patches target the owning module so
request-time factories see them without a facade `_live` hook.
