# ADR-0002: Position CodexHub as the model-access console for Codex desktop users

Date: 2026-07-22
Status: Accepted

## Context

Three constraints force a positioning decision:

1. **Competition.** OpenCodex (lidge-jun/opencodex) occupies the CLI-first,
   breadth-first segment: `npm install -g`, 40+ provider presets, OAuth,
   ChatGPT account pooling, Claude Code support, cross-platform daemons.
   Its shape is optimal for developer self-serve, but it has no desktop
   product form, no managed-experience concept (it injects/ejects but does
   not own the user's Codex state), and no quality tiering for routed
   models.
2. **Bandwidth.** The 0.1.7 cycle showed our real delivery capacity: one
   stability chain absorbed the entire team for days. A second front
   (feature-for-feature chase) is not an option.
3. **Upstream floor.** Codex natively supports custom providers via
   `config.toml`, and OpenAI keeps raising this floor (0.145 `[agents]`
   unification, model catalog metadata). Protocol conversion alone is a
   depreciating asset; management, state ownership, and observability
   appreciate instead.

CodexHub already holds three assets the CLI competitors do not: a Tauri
desktop app for non-CLI users, deep managed integration with Codex Desktop
(catalog injection, takeover/restore, cross-home history unification), and
usage/recovery telemetry.

## Decision

CodexHub is the **model-access console for Codex desktop users**: install
it, use official subscription and third-party models side by side, and get
an experience that is **stable, visible, and reversible**.

- **Stable** — not "the protocol converts", but tiered, verifiable
  capability promises per provider (tool calls, subagents, streaming).
  Stability is the brand, not a bug list.
- **Visible** — telemetry (usage, cost, retries, recoveries) becomes
  user-facing: model health, which hop failed, what a request cost.
- **Reversible** — takeover/restore is lossless, history is preserved,
  and users can cleanly return to native Codex at any time. Low exit cost
  is a trust feature and a hedge against the rising upstream floor.

Target user: people on Windows using Codex Desktop who want third-party
models without touching TOML or npm — and people who tried, but were
burned by instability.

### Explicit non-goals (next three versions)

- No Claude Code support (#73–#78, #85 stay unscheduled).
- No account pooling / quota routing.
- No macOS/Linux packaging promises (drop from active commitments).
- No preset-count race: 8–12 curated presets (GLM, DeepSeek, Kimi, Qwen,
  OpenRouter, Ollama, Volc, …) cover ~90% of users.
- No big-bang multi-agent V2 adaptation; keep the fail-closed gate
  (#197/#198 stay P2).

### Version themes

- **0.1.7 — Trustworthy**: finish the in-flight stability chain only
  (#196 → #190 → #193 → #18/#19/#20 → #114/#141, plus #157). The release
  itself is the down payment on "stable".
- **0.1.8 — Onboardable**: curated provider presets, categories, guided
  add with Probe. New user goes from install to a successful third-party
  tool-call task in Codex in ≤ 5 minutes (#71, #89, #90, #91, #83, #28).
- **0.1.9 — Dependable**: per-provider capability tiers
  (supported / best-effort / unsupported) for tool calls, subagents,
  streaming, shown in the UI; fail fast with readable reasons on
  unsupported paths; publish a telemetry-backed compatibility matrix
  (#57–#67, #22, #17, #198, #199, #197).
- **0.1.10 — Visible (console completion)**: usage/cost dashboards,
  health status, diagnostic export, multi-client management polish
  (#86–#88, #113, #115, #126, #153, #179). This version may be renumbered
  0.2.0 when scoped.

### Metrics (from 0.1.8 onward)

- Activation: share of installs completing "add provider + first
  successful third-party request" within 24h.
- Stability: per-provider tool-call success rate / session interruption
  rate, from existing telemetry.
- Retention: weekly active Gateway days.

## Consequences

- Milestones are renamed to the four themes and issues re-sorted
  accordingly; the roadmap in #147 is superseded by the 2026-07-22
  reconciliation comment.
- Issues outside the themes (Claude Code chain, OAuth framework, account
  pooling) remain unscheduled and must not be admitted into milestones
  without revisiting this ADR.
- DESIGN.md's cross-platform packaging/autostart sections describe a
  deferred aspiration, not a commitment.
- Protocol conversion work is scoped to "common paths stable + failures
  visible and diagnosable", not to out-converting CLI competitors.
