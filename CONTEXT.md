# CodexHub

CodexHub is a local model-routing utility for the Codex ecosystem. This glossary keeps user-facing product terms consistent across documentation and UI.

## Language

**Gateway**:
The user-facing local OpenAI-compatible HTTP service that exposes official Codex subscription models and configured third-party models through one endpoint.
_Avoid_: Proxy, runtime proxy, local proxy

**Vision Proxy**:
The Gateway feature that lets a non-vision target model handle image requests by using a configured image-capable model to produce text visual context.
_Avoid_: Image conversion, image workaround

**Route Plan**:
The immutable per-request decision snapshot the Gateway computes after the user selects a Provider/model: ordered upstream protocol attempts, retry/streaming/mutation policies, and the Vision Proxy decision. Fixed for the life of the request; its identity core is the ResolvedRoute of ADR-0002.

**Maintained Catalog**:
The repository-owned table of official model identity for Maintained Providers: Display Name, family, thinking mode, reasoning levels, default level, and per-endpoint context/output limits (ADR-0009). `catalog_sync` consumes it; it is not a Gateway owning module.
_Avoid_: five-level fill, guessed effort grades

**Display Name**:
The provider-independent label stored on a model (for example `GLM-5.3`). The Provider page and grouped Client pickers show this string.
_Avoid_: prefixed name, long name, catalog title

**Display Prefix**:
The Provider brand token used only to compose a Flat Label (`Ollama`, `Volc`, `Kimi CN`). It is not part of Display Name.
_Avoid_: provider alias

**Flat Label**:
Display Name with Display Prefix composed, used only on lists that mix more than one Provider (Gateway `/v1/models`). Official models keep their own short-name rule and do not compose a Prefix.
_Avoid_: qualified display name, long name, prefixed display name

**Owning module**:
The single Gateway Python module that holds one seam (catalog, transport, events, compatibility, stream semantics, request boundary, relay, or handler methods). The process entry only wires HTTP. Tests patch the owning module, not the entry. Cross-SCC calls import the module object and read the attribute at call time (ADR-0007); they do not go through `lookup()`, `api`, or `RelaySymbols`.
_Avoid_: facade, runtime proxy module, exec dump

## Operations

If ChatGPT/Codex cannot start or native Windows sandbox commands hang, follow
[`docs/runbooks/codex-windows-sandbox-recovery-handoff.md`](docs/runbooks/codex-windows-sandbox-recovery-handoff.md).
Do not reset ACLs, reinstall the AppX package, or delete `.codex` as a first-line response.

## Client integration

**Provider Injection**:
The integration mode where the Gateway joins a client configuration as one additional provider entry among the user's own, preserving every user-owned provider and setting. The standard mode for all managed clients from 0.1.9 (ADR-0004).
_Avoid_: incremental access, partial takeover

**Managed Takeover**:
The legacy integration mode where CodexHub rewrote a client configuration so the Gateway was the only usable route, forcing the client's default model selection. Superseded by Provider Injection (ADR-0004); still in force for clients not yet migrated in campaign 0.1.9.
_Avoid_: full management, ownership mode

**Injected Block**:
The exact set of entries CodexHub owns inside a client configuration under Provider Injection: DSH's one `codexhub` provider entry, or the Client Provider Groups in a client that has a provider map, plus one credential reference. Detach removes precisely this; readback validates only this.
_Avoid_: owned fields, managed section

**Client Provider Group**:
One `codexhub-{provider}` entry in a Client that has a provider map, corresponding to one CodexHub Provider. The group label is `CodexHub {Provider name}`. DSH does not use groups.
_Avoid_: injected provider, managed provider

**Client Projection**:
The derived model entries written into the Injected Block on Connect or republish. Identity and capabilities come from the Provider; the Client adapter only supplies that Client's file shape. Republish replaces the projection; the Client does not keep a second editable copy.
_Avoid_: client catalog, client-owned models, synced copy

**History Bucket**:
The per-provider-ID session history partitioning in Codex CLI: sessions belong to whichever provider ID was active when they ran, and switching provider IDs strands them. The reason Codex keeps one stable provider bucket and expresses direct-vs-Gateway inside it (ADR-0004).
_Avoid_: session loss, conversation reset

**Activation**:
Pointing a client's global default model selection at the Injected Block. Always the user's own action, never a side effect of Apply.
_Avoid_: enabling, switching on
