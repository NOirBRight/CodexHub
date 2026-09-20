# Ollama as the Claude Code campaign's Messages upstream

Status: research only; no Ollama inference, authenticated request, credential read,
installation, or user configuration change. Campaign #73 / input contract #557.
The delegated researcher hit its usage limit; the orchestrator completed this note.

## Evidence and limits

Sanctioned web search returned indexed **first-party** documentation on 2026-09-20.
A full-page fetch of the GitHub integration document was denied because its hostname
resolved to a non-public address. No alternate fetch path was attempted. Findings
below are documentation evidence, not a live availability or compatibility pass.

- [Official Claude Code integration source](https://github.com/ollama/ollama/blob/main/docs/integrations/claude-code.mdx)
  explicitly shows direct Cloud use: `ANTHROPIC_BASE_URL=https://ollama.com`,
  Bearer authentication through `ANTHROPIC_AUTH_TOKEN`, and model `glm-5.3-flash`.
  It says no local Ollama installation is needed for that path. It cautions that
  hosted WebSearch and advanced tool controls are not fully supported.
- [Official Anthropic compatibility source](https://github.com/ollama/ollama/blob/main/docs/api/anthropic-compatibility.mdx)
  describes `/v1/messages`, text, streaming, tools/results, and basic thinking.
  The indexed version lists limitations including token counting, tool choice,
  caching and some content formats. This inventory may lag current implementation;
  verify the chosen deployment rather than assuming full Anthropic equivalence.
- [Official launch announcement](https://registry.ollama.com/blog/claude)
  dates local Messages compatibility to Ollama 0.14.0 and describes both local and
  cloud-model use. It does not prove this user's installed version or account access.

## Decision options

| Deployment | Documentation conclusion | Campaign implication |
| --- | --- | --- |
| Direct Ollama Cloud | Messages support is documented; `glm-5.3-flash` is an explicit example | Suitable candidate for a wire-native Messages upstream; actual request/SSE fidelity still needs evidence |
| Local Ollama with local model | Messages compatibility is documented | Requires installed version/model/resource validation; not necessary for the selected Cloud provider |
| Local Ollama forwarding to Cloud | Documented integration pattern | Adds a process/hop; avoid when direct Cloud meets the requirement |

Here “native” means CodexHub sends Anthropic Messages directly to the upstream's
Messages interface. Ollama's open models are not Anthropic Claude models, and this
leg alone cannot establish Anthropic-specific signatures or server-feature fidelity.

## Repository fit and recommendation

`config/providers.toml:3-37` already defines `ollama-cloud`, base URL
`https://ollama.com/v1`, and `glm-5.3-flash`. Its preset currently advertises only
Responses. The chosen Messages path therefore needs an explicit isolated protocol
binding; do not silently change the user's existing provider or all client routes.
Endpoint joining must yield `/v1/messages`, never `/v1/v1/messages`.

Protocol candidate: **Ollama Cloud / `glm-5.3-flash`**, not a verified working or
Free-eligible replacement. Keep Responses = Codex / Luna / max and Chat =
CommandCode / `deepseek/deepseek-v4-flash` as already selected by the user.

## Free-account qualification (user clarified the plan)

The indexed [official pricing page](https://registry.ollama.com/pricing) says Free
includes starter usage for a subset of starter models, not the full Cloud catalog.
It does not identify that subset in the retrieved material. Buying credits unlocks
all models; do not buy credits or upgrade the account as part of this investigation.
The [official pricing announcement](https://ollama.com/blog/transparent-pricing)
dated2026-08-31 confirms this distinction; older broad free-cloud recommendations
are not evidence of present Free entitlement.

Consequently no exact model can honestly be labelled **Free-account verified**
from this research. A Cloud model-list entry, token price, or Messages example
proves neither account access nor remaining starter allowance. `glm-5.3-flash`
has the strongest protocol-specific example found, but its Free entitlement is
unknown. No account-authenticated eligibility or inference probe was executed.
The pricing full-page fetch was also blocked by non-public-address resolution;
no alternate fetch was attempted. An account-scoped check is needed before the
campaign selects a Free-only model, and remains separate from protocol support.

`docs/agents/real-client-e2e.md:16` currently prohibits Ollama in the existing live
release gate. Research permission does not waive that rule. Obtain explicit
approval for a separate, bounded campaign experiment and document the exception
in #557 before any Ollama model call. Do not alter the frozen eight-case matrix.
No claim of live success, available quota, price, latency, or complete native
Anthropic feature coverage is made here.
