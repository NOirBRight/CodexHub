# Claude subscription and Gateway coexistence: isolated verification

Date: 2026-09-23. CLI: Claude Code 2.1.280. Candidate: `a5ec2c5706d900d129cc344e57d7078078539293`.

## Conclusion and scope

A subscription-preserving front relay is technically feasible. The existing
candidate does not implement that relay: it was added only as a disposable
experiment in front of an isolated instance of the packaged candidate. The
installed application was not changed or restarted.

This corrects the earlier assumption that separate launchers are the only
possible solution. It does not establish that the current candidate supports
coexistence, or that Anthropic supports Claude Code with non-Claude models.
See the [official documentation findings](2026-09-23-claude-subscription-gateway-coexistence-docs.md).

## Isolation and credential boundary

- Fresh temporary HOME, Claude config, working directory, CodexHub runtime,
  Gateway port and web-bridge port; no request to the production port 9099.
- Claude safe mode, no tools or MCP servers, telemetry and updater disabled.
- Only a snapshot of the still-valid subscription access token and account
  metadata was copied. Refresh tokens were excluded, preventing the experiment
  from rotating the active session's subscription credentials.
- Native test model IDs went to the fixed `api.anthropic.com` destination,
  preserving the incoming OAuth credential and complete `anthropic-beta`.
- The explicit DeepSeek projected ID went to the isolated packaged Gateway.
  The relay removed incoming authorization/API-key headers and supplied the
  isolated Gateway key. The Gateway used its separate official DeepSeek key.
  Evidence records equality checks, model IDs and HTTP status, not secrets.
- Relay requests were bounded to 16 live calls per run, with upstream and CLI
  timeouts. Other model IDs and API paths were refused.

## Verified authentication and resumed conversation

[Sanitized resume evidence](2026-09-23-claude-coexistence-resume-evidence.json)
records the requests and CLI results. Hash comparisons of the host Claude
settings, credentials, and account metadata were all unchanged after that run.

| Case | Observation |
| --- | --- |
| Base URL only, synthetic receiver | CLI sent saved subscription OAuth and the OAuth beta capability; no model discovery request |
| Base URL plus synthetic Gateway token | CLI sent that token instead of OAuth; model discovery ran |
| Real subscription Haiku through relay | HTTP 200, successful CLI result |
| Resume that test session with Gateway DeepSeek | HTTP 200, successful CLI result; recalled the marker from the Haiku turn |
| Resume the same test session with subscription Haiku | HTTP 200, successful CLI result; recalled the same marker |
| Credential separation | External request to the isolated Gateway did not contain the subscription bearer token |

The live models were `claude-haiku-4-5-20251001` and
`claude-codexhub-deepseek-deepseek-flash` (resolved by the candidate to official
DeepSeek Flash). This is evidence for one subscription model, not a live test
of every model available under the subscription.

An initial disposable relay run returned upstream HTTP 200 but failed in the
CLI because the prototype did not preserve response compression headers.
The relay was corrected to request identity encoding and preserve a returned
Content-Encoding. The subsequent complete resume sequence passed.

## Verified interactive session and manual compaction

A second successful run used one actual Claude Code process in a pseudo-terminal,
with typed `/model` and `/compact` commands, not separate print requests.
[Sanitized interactive evidence](2026-09-23-claude-coexistence-interactive-evidence.json)
records seven successful upstream requests, including client-side probes.

- The `/model` menu displayed the native Default, Opus, Fable, Sonnet and Haiku
  entries together with the additional DeepSeek row.
- After a subscription Haiku turn established a synthetic project codename,
  `/model` switched to DeepSeek. The test accepted Claude Code's cache-cost
  confirmation; DeepSeek correctly recalled the codename.
- `/model` switched back to Haiku, which correctly recalled the codename.
- `/compact` displayed `Compacted (ctrl+o to see full summary)`. The following
  Haiku response still recalled the codename. All observed upstream statuses
  were HTTP 200.
- Host settings, credentials and account metadata hashes remained unchanged.

Earlier terminal automation attempts did not satisfy this acceptance sequence:
one selected the default exit option in the temporary workspace trust prompt,
and another omitted the model-switch confirmation. The successful run explicitly
handled both dialogs. These were harness issues, not evidence of model routing
failure.

The CLI header continued to display the saved subscription plan even with the
DeepSeek model selected. That header is not proof of which account pays for the
external request; the route and credential records establish that DeepSeek used
the separate API-backed Gateway path. Product labels should make this distinction
clear.

## Implementation consequences

1. A coexistence connection must keep native subscription authentication
   active: do not overwrite it with `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`
   or a Gateway `apiKeyHelper`.
2. Preserve built-in model IDs and aliases. Add external picker rows with
   `modelPicker.options` and `replaceBuiltInOptions:false`. OAuth-only model
   discovery did not run in the carrier experiment.
3. Gateway needs an explicit native-Claude subscription pass-through route,
   distinct from external provider routing. An unknown external ID must not
   silently become a subscription request. Subscription credentials must be
   confined to the fixed official destination and excluded from logs/events.
   Production local-client authentication must also preserve this OAuth
   carrier; the disposable relay's access-token equality check is not a
   production authentication design.
4. Existing processes that loaded the old Gateway token need to restart under
   the new connection configuration, then resume their existing conversation.
   Preserving model names alone does not change an already-running process's
   credential source.

Automatic compaction of a long production conversation, tool histories across
providers, credential expiry/refresh, all subscription model variants, and
disconnection/migration behavior remain outside this feasibility result.
There were no production source changes, so this verification does not replace
the strict auth/routing test matrix required for a future implementation.
