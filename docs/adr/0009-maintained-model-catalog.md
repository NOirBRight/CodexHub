# ADR-0009: Maintained Catalog is the capability source

Date: 2026-08-30
Status: Accepted

## Context

ADR-0008 made Provider Preset the add-flow seam and listed Ollama Cloud, Volc,
MiniMax.cn, and xAI as bundled Presets. Preset rows still only carried
identity plus a short model list. `catalog_sync.complete_third_party_reasoning_levels`
then filled every third-party model with low/medium/high/xhigh/max and defaulted
to high (or xhigh). That does not match vendor contracts: Kimi K3 is low/high/max
default max; K2.7 Code has no effort grades; MiniMax-M3 is thinking on/off; GLM-5.2
is high/max.

The same model id also appears on more than one endpoint with different
context/output limits. Empty-only instantiate (ADR-0008) would never add newly
documented official models to an existing runtime `providers.toml`.

## Decision

`maintained_catalog.py` is the capability source for Maintained Providers. It is
not a Gateway owning module. `catalog_sync` and request-time compat import the
module object and read attributes at call time (ADR-0007).

- Family policy (reasoning levels, thinking_mode, vision, developer-role) is
  shared across endpoints.
- Endpoint numbers (context, max_output, format, tool codec) stay on the Preset.
- Thinking mode (`none` / `always_on` / `toggle`) is orthogonal to effort grades.
  CodexHub does not add a global `off` effort.
- Maintained models do not get the five-level fill. Empty levels stay empty.
  Custom providers still get the five-level fill.
- Bundled official model ids that a runtime Preset is missing are inserted
  additively. User-disabled rows, user-edited fields, custom ids, and leftover
  retired ids (for example `volc/glm-5.2`) are not overwritten or deleted.
  URL, protocol, and prefix remain empty-only (ADR-0008).

The bundled Maintained Provider catalog is:

- `ollama-cloud`
- `volc`
- `minimax-cn`
- `kimi-cn`
- `kimi`
- `commandcode`
- `opencode-go`

The standalone `xai` Preset is not maintained by this catalog; it keeps its
Preset-owned rows while sharing the `grok` family policy.

## Consequences

- Adding or correcting a model’s name, thinking levels, default, or modalities
  is a Maintained Catalog change, then a Preset row when the model is new.
- Volc GLM-5.2 is not in the bundled official list. Family policy still resolves
  it so existing user rows keep high/max instead of five filled levels.
- Kimi dual Presets share one family table and differ by base URL and env key.
