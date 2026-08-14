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

## Operations

If ChatGPT/Codex cannot start or native Windows sandbox commands hang, follow
[`docs/runbooks/codex-windows-sandbox-recovery-handoff.md`](docs/runbooks/codex-windows-sandbox-recovery-handoff.md).
Do not reset ACLs, reinstall the AppX package, or delete `.codex` as a first-line response.
