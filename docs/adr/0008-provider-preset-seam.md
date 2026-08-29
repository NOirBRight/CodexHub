# ADR-0008: Provider Preset is the add-flow seam

Date: 2026-08-24
Status: Accepted

## Context

Adding xAI (SuperGrok device-code) made “Add Provider” a catalog-plus-custom
flow instead of a blank form only. Gateway seams for that work already exist
and must stay:

- Catalog / identity: `catalog`, `gateway_catalog_runtime`, `catalog_sync`
  (ADR-0006)
- Subscription auth: `subscription_credential` (ADR-0005). `codex_auth` is
  adapter #1, `xai_auth` is adapter #2. Catalog already uses
  `provider_auth_mode(provider_id)` — no Python `provider_id == "xai"` branch.
- Transport must not grow per-provider `elif`s.

The missing seam is the add-flow, not request-time routing. Treating Provider
as one Gateway owning module fails the deletion test and fights ADR-0005 and
ADR-0006. Splitting “OpenCode-maintained vs CodexHub-maintained” as a runtime
dependency also fails: OpenCode has no stable Provider package (Models.dev +
AI SDK + auth plugins). `provider_registry.py` only scrapes
`~/.config/opencode/opencode.json` for leftover volc/minimax keys; it is not
this seam.

Official (`__official__`, ChatGPT/Codex) is not a Preset. Custom is the empty
Preset. Three UI special-cases still keyed off `id === "xai"`: picker hint,
login card, and discovery retain-intersection.

## Decision

Provider is not a Gateway owning module; OpenCode is not a Preset maintainer;
the add-flow seam is Provider Preset.

A Provider Preset is reviewed add-flow default metadata: identity, empty-only
instantiate fields, and capability declarations. It carries no credentials.
Instantiate fills empty URL, models, formats, and prefix only; bundled
updates must not overwrite user-owned URL, protocol, enabled models, or
ordering.

The bundled catalog of Maintained Providers is:

- `ollama-cloud`
- `volc`
- `minimax-cn`
- `xai`

Xunfei is removed from the bundle only. A user-added Xunfei row on disk is
never deleted. Official is not a Preset. Custom is the empty Preset.

Capability fields on a Preset (and on a saved Provider copied from one):

- `auth_capabilities` — omitted is treated as `["api_key"]`
- `onboarding_hint` — omitted is no hint
- `discovery_policy` — omitted is `"merge"`; `"retain-intersection"` keeps
  only models discovery still returns

xAI device-code UI stays an `xai_auth` adapter detail. The xAI Preset only
declares `subscription:xai_oauth`. Gateway Python must not read these UI
fields at request time. Already-saved xAI rows that omit the fields are
backfilled from the bundled Preset by id in UI helpers — not by
`id === "xai"` branches.

## Consequences

- Adding a Maintained Provider is a bundled Preset row plus, when needed, one
  subscription adapter. It is not a new Gateway owning module and not a
  transport branch.
- OpenCode is not vendored, submoduled, npm-depended, or runtime-pulled as a
  Preset source.
- A later Models.dev snapshot may feed Preset metadata only if it is pinned,
  allowlisted, and non-secret. Models.dev is not a Preset maintainer and is
  out of this slice.
- Generic OAuth, an OS secret store, CatalogPublish, and merging
  `providers.toml` / overlay / env into one config module remain out of
  scope.
