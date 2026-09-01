# ADR-0004: Coexistence Provider Injection over Managed Takeover for client integration

Date: 2026-08-15
Status: Accepted. Campaign 0.1.9 (#436); DM-4 implementation complete in the
current architecture candidate. DSH (#430) is the first client built on these
semantics. Recorded during 0.1.8-beta.4.2 wind-down.

## Context

CodexHub integrated its first five clients (codex, opencode, pi, omp, zcode)
with **Managed Takeover** semantics: Apply rewrote the client configuration so
the local Gateway was effectively the only route. Four of the five clients
overwrite whole files or strip foreign provider sections
(`clients::opencode::opencode_config_text`, `clients::omp::omp_models_yml_text`,
`clients::zcode::zcode_catalog_text`, codex overlay
`config_overlay.STALE_PROXY_PROVIDER_SECTIONS`); pi alone edits
surgically but still forces `defaultProvider`/`defaultModel`. Readback
verifies by byte-comparing a re-serialized expectation against the whole file
(`readback::verify_apply_readback`).

Consequences that motivated the change:

- A user cannot keep their own providers/models configured alongside the
  Gateway; CodexHub-managed use and direct multi-provider use are exclusive.
- Any legitimate user edit to their own config reads as drift at the next
  byte-compare readback.
- Restore needs full-file snapshots because removal is not reliably surgical
  under overwrite semantics.

DSH (#430) was the forcing case: its `settings.yaml` holds a provider map
(`llm-pi-ai.providers`) designed for coexistence, hot-reloads on change, and
stores credentials as env-var references in a separate `.credentials.yaml`.
Reproducing takeover semantics there would have meant fighting the client's
own model.

## Decision

Adopt **Provider Injection** as the integration semantics for all managed
clients, landing in phases over campaign 0.1.9 (DSH first, then pi + codex,
then opencode + omp + zcode):

1. **Inject, never rewrite.** CodexHub adds exactly one provider entry (the
   Injected Block, fixed route key `codexhub`) into the client's existing
   config, preserving all user-owned providers, settings, and formatting that
   the file-format strategy permits.
2. **Never activate.** Apply never touches the client's global default-model
   selection (e.g. DSH `agent-default-model`, codex `model_provider`).
   Pointing the client at the Gateway is always the user's own action; the
   pointing state is surfaced read-only, never as drift. The one migration
   exception is a selector that already names a recognized CodexHub provider
   from the legacy managed-takeover format: it may be rewritten to the
   current injected provider during repair, or removed during detach, so a
   stale app-owned selector cannot strand the client on a deleted provider.
3. **Adopt, don't conflict.** A pre-existing same-named entry pointing at the
   local Gateway is adopted as the Injected Block.
4. **Project the full enabled model set** into the injected entry, re-projected
   on catalog change through the republish channel (#428).
5. **Credentials are surgical**: a single key in the client's credential store
   (DSH: `~/.dsh/.credentials.yaml`), backup + atomic write, masked in all
   evidence. Key rotation rewrites that one key only.
6. **Detach is surgical**: remove exactly the Injected Block plus the
   credential key. Full-file snapshots degrade to disaster-recovery-only.
7. **Readback is block-fingerprint**, not byte-compare (#433): presence and
   fingerprint of the Injected Block; foreign content is never validated.

## Codex exception: stable history bucket

Codex CLI buckets session history by `model_provider` ID; switching providers
strands existing sessions in the old bucket. Multi-provider coexistence would
reintroduce that regression, so codex is the sole exception to decision 2:
`model_provider` stays pinned to the fixed bucket ID at all times, and
direct-vs-gateway is expressed by the presence of `base_url` inside that one
bucket (the existing unified-history machinery,
`config_overlay.unified_config_state` / `history_overlay`). Foreign
`model_providers.*` sections are no longer stripped, but switching to them
changes the history bucket by design of the client itself.

## DM-4 amendment: managed-client adapter seam

Campaign #491 DM-4 completes #8 with a unified coordinator and per-client
adapters. Adapters expose three internal capabilities — metadata(), inspect(ctx) -> ClientSnapshot, and plan(intent, ctx) -> ClientMutationPlan (Connect | Disconnect | Republish) — and never publish files directly. The
coordinator (src-tauri/src/gateway/managed_clients.rs) owns the routing-owner
gate, global write lock, baseline/legacy backup adoption, file_transaction
atomic publish/rollback, block-fingerprint readback, pending-sync cleanup, and
result mapping. InjectionDescriptor plus the JSON/YAML/TOML strategies are
internal plan machinery. The Codex stable-bucket exception is expressed by a
dedicated adapter, not by distorting the generic descriptor.

The adapter interface stays internal to the Rust crate: metadata(), inspect(ctx) -> ClientSnapshot, and plan(intent, ctx) -> ClientMutationPlan. Public Tauri/bridge commands only ever see the coordinator, never an adapter.

## Consequences

- Users can run the Gateway alongside their own providers in the same client;
  "use CodexHub" and "use anything else" stop being exclusive.
- Migration of existing takeover clients is phased (#434, #435); existing
  managed users migrate on first Apply after upgrade (restore backup, then
  inject). Takeover-era configs keep routing correctly until then.
- Ownership rules move from scattered predicates into a declarative
  per-client injection descriptor (#432), finally landing the #8 adapter seam.
- Activation being user-owned means Apply no longer guarantees traffic. The
  UI follows a connection metaphor: a per-client connect/disconnect toggle
  that IS inject/detach (it never touches the client's model-selection
  switch); no separate "active/pointing" indicator is surfaced.
- Byte-compare readback remains in force for not-yet-migrated clients; the
  two readback semantics coexist during the campaign.

## Campaign status (architecture candidate)

Coordinator `adapter_for` now registers `dsh`, `codex`, `opencode`, `pi`,
`omp`, and `zcode`. Native apply for opencode/pi/omp/zcode is coordinated
directly through `apply_native_at`; publication, restore, preview, and
readback are coordinator-owned operations. Codex live overlay stays in
`config.rs` / `config_overlay.py` because of the stable-history-bucket
exception above; `CodexAdapter` is the registry entry and keeps isolated
gateway apply fail-closed to `apply_codex_config_isolated`. Isolated
dispatch no longer special-cases `client_id == "codex"`. The internal adapter
seam exposes exactly `metadata()`, `inspect(ctx)`, and `plan(intent, ctx)`;
the coordinator owns all filesystem effects and result mapping.
