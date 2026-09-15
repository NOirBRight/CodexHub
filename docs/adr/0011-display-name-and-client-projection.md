# ADR-0011: Short Display Name, composed Flat Label, derived Client Projection

Date: 2026-09-15
Status: Accepted

Provider settings are the only editable source of model identity and
capability. Managed Clients receive a derived Client Projection. Display Name
is the short, provider-independent label; brand is composed only as a Flat
Label on lists that mix Providers. Storing a prefixed string and copying it
into already-grouped Client pickers produced double-branded, over-long names
(`CodexHub Ollama Cloud` + `Ollama GLM-5.3`).

## Decision

1. **Provider owns; Client does not.** Connect and republish replace the
   Client Projection. A Client never keeps a second editable copy of names
   or capabilities. Adapter code may only reshape those fields into that
   Client's file schema.
2. **Display Name is short.** Maintained Catalog stores the label the Provider
   page already shows after stripping Display Prefix (for example `GLM-5.3`,
   `K3`, `MiniMax M3`). Custom models with no Display Name fall back to the
   short wire id, not the Gateway-qualified id.
3. **Flat Label is composed, not stored.** On mixed-Provider lists (Gateway
   `/v1/models`), Flat Label is `{Display Prefix} {Display Name}` when Prefix
   is set and Display Name does not already start with it; otherwise Display
   Name. Official models keep `official_short_display_name` and do not compose
   a Prefix.
4. **Catalog Display Name on Maintained rows is catalog-owned** when the
   stored value equals the previous or current catalog string. A user value
   that matches neither is preserved (ADR-0009). Custom-model names the user
   typed are preserved.
5. **Client Projection fields** from the Provider: short id, Display Name,
   input modalities, reasoning levels and default, context window, max
   output tokens. Client-shape only: OpenCode variants, ZCode kinds, schema-
   required dummy cost, `x-codex-client-id`. Max output is never a hardcoded
   `32768`. ZCode does not invent a global `off` effort; a thinking-off
   control maps from `thinking_mode = toggle` (ADR-0009).
6. **Injection shape is unchanged** (ADR-0004 amendment): DSH stays one
   `codexhub` block with Gateway-qualified ids; Clients with a provider map
   stay Client Provider Groups. Naming is fixed by (2)–(3), not by collapsing
   groups.

## Consequences

- Bundled Maintained rows and matching runtime `display_name` values become
  short; grouped Client `name` follows. Flat Gateway lists keep
  `Ollama GLM-5.3` vs `Volc GLM-5.3` and `Kimi K3` vs `Kimi CN K3`.
- Provider-page prefix stripping becomes unnecessary once stored names are
  already short.
- Pi / OMP / ZCode pick up Catalog max output on republish. ZCode reasoning
  `off` as a fake effort grade is removed.
