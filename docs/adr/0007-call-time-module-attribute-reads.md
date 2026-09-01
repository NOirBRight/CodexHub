# ADR-0007: Call-time module attribute reads replace dynamic lookup

Date: 2026-08-23
Status: Accepted for campaign #467 (Deep Modules v4)

## Context

Deep Modules v3 deleted the `exec` facade, but three dynamic indirection
layers still hid the real call graph:

1. `gateway_compat.lookup()` / `api.py` / package `__getattr__` resolved
   names at call time through a dictionary of strings.
2. `RelaySymbols` packed 128 owning-module callables into a frozen dataclass
   rebuilt on every request.
3. Cross-module imports bound underscore-private names, so the public seam
   and the implementation name drifted.

Those layers made patches appear to work while the production path still went
through a bag of names. They also blocked acyclic `from X import name` edges
inside the `{request, response, sse, multi_agent, official_passthrough}` SCC.

## Decision

Callers import the **owning module object** and read the attribute at the
call site:

```python
from gateway_compat import official_passthrough as _official_passthrough

def compatible_sse_line(...):
    return _official_passthrough.compatible_sse_line(...)
```

Top-level `from owner import name` is still forbidden across that SCC
because it would recreate an import cycle. Attribute reads at call time keep
test patches on the owning submodule live without a lookup table.

`RelaySymbols` is gone. Relay entrypoints import owning modules at function
entry. Exchange-owned glue is a small `RelayGlue` on `RelayContext`, not a
128-field bag.

Cross-module imports use the owning module's public name. An AST gate in
`tests/test_module_boundaries.py` keeps the underscore-import allowlist empty.

`gateway_compat/__init__.py` is an explicit public surface
(`compatible_request_body`, `official_passthrough_request_body`, …). It does
not implement `lookup()`, `api`, or `__getattr__`.

## DM-5 amendment: ports are adapters, not bags

The Exchange ports introduced by campaign #491 DM-5 are typed protocols whose
production implementations live in gateway_exchange_adapters.py. Each adapter
method imports the owning module object and reads the attribute at call time —
the same discipline as this ADR. Tests patch the owning module (e.g.
gateway_transport.upstream_failure_class) after building the ports and assert
the new implementation is observed without rebuilding them.

## Consequences

New Gateway behavior is added on the owning module and called through a
module attribute (or a public re-export). Do not add a name registry, a
symbols dataclass, or a package `__getattr__` to paper over import cycles.
The v4 gates in `tests/test_deep_modules_v4_gates.py` fail closed if those
shapes return.
