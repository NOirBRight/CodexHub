# CodexHub

CodexHub is the model-access console for Codex desktop users: official subscription and third-party models side by side, stable, visible, and reversible. Product positioning, non-goals, and version themes live in [ADR-0002](docs/adr/0002-product-positioning-model-access-console.md). This glossary keeps user-facing product terms consistent across documentation and UI.

## Language

**Gateway**:
The user-facing local OpenAI-compatible HTTP service that exposes official Codex subscription models and configured third-party models through one endpoint.
_Avoid_: Proxy, runtime proxy, local proxy

**Vision Proxy**:
The Gateway feature that lets a non-vision target model handle image requests by using a configured image-capable model to produce text visual context.
_Avoid_: Image conversion, image workaround

## Operations

If ChatGPT/Codex cannot start or native Windows sandbox commands hang, follow
[`docs/runbooks/codex-windows-sandbox-recovery-handoff.md`](docs/runbooks/codex-windows-sandbox-recovery-handoff.md).
Do not reset ACLs, reinstall the AppX package, or delete `.codex` as a first-line response.
