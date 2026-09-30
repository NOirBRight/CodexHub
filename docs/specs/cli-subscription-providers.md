# Claude and Cursor Subscription Providers

Date: 2026-10-01. Tracker specification: [#603](https://github.com/NOirBRight/CodexHub/issues/603).
Confirmed requirements: [planning interview](../research/2026-09-29-claude-cursor-subscription-provider-planning.md).
Architecture: [ADR-0018](../adr/0018-cli-subscription-upstream-providers.md).
Risk class: **strict** for implementation; documentation alone is **fast**.

## Product behavior

Two upstream Providers make the current official CLI account's models available
through the existing Gateway. They are distinct from downstream Claude Code
connection/coexistence and from an Anthropic API-key Provider. Users retain the
existing Provider/model enable, export and Client Projection controls. There is
no separate model directory, account pool, account-import flow, fallback model,
Gateway tool executor or agent scheduler.

Detection reports CLI missing, signed out, expired account, discovery failure
and account change separately. An account change during discovery invalidates
the result; a change during an active request never silently changes its
identity. Official CLIs retain login/refresh ownership. Status/catalog work
does not run inference or change normal CLI/Gateway configuration. Raw CLI
output, account identifiers and credentials stay out of public status, errors
and evidence. Temporary credential copies are private and removed on exit.

Cursor discovery keeps every distinct vendor ID and its display name, including
effort and fast variants. Discovery availability is not a model reasoning
guarantee. Do not manufacture context limits, usage, costs or cancellation
acknowledgements that the upstream does not supply. Claude account model
discovery must use the account's vendor list; the downstream native picker
snapshot is not an exhaustive entitlement list. Model listing alone cannot
prove generation permission.

Before enabling Claude, show and record consent to this exact adaptation:
calling-agent system instructions are carried in CLI user context, so their
native system-message priority is not preserved. Do not imply that a generic
Provider enable toggle grants that consent. All persistent changes use the
shared Toast lifecycle and name any exact required runtime restart.

## Transport and compatibility

Claude uses an isolated official CLI subprocess and a private MCP bridge. Only
caller-declared tools are exposed. Built-in shell/filesystem tools, plugins,
hooks, user/project settings and downstream Gateway routing are excluded.
The caller receives actual tool requests and owns execution; returned results
match the original Call identity. Cancellation terminates and reaps the whole
backend process tree and removes private artifacts.

Cursor uses its CLI account's AgentService/Run streaming transport, with exact
selected model identity and only caller tools. No ACP replacement and no
production dependency on Magpie. Implement framing, keepalives, dynamic tools,
blobs, streamed errors and cancellation behind the exchange interface. A caller
disconnect must close the upstream; do not claim billing cessation without
evidence. Interrupted waits fail visibly and never replay tool side effects.

Reuse the existing Responses, Chat Completions and Anthropic Messages adapters.
The selected Route Plan remains immutable. Preserve tool namespaces, custom
input, Call identity, typed Item identity, stream order and full caller history.
Unknown/malformed inverses fail with bounded errors rather than changing codecs
or Providers. Images and unsupported advanced behaviors obey the existing
declared compatibility policy; essential content is never silently removed.

For current Code Mode, detect actual supported caller declarations including
flat custom exec and child subsets. Expose only the collaboration handlers that
caller declares. Prevent opaque mixed-provider assignments before generation;
preserve author/recipient and followup context. Existing mislabelled plaintext
may be recovered only from an exact observed assignment with matching provenance.
Opaque-only tasks require caller-owned restatement/reissue using original
requirements and existing child state/results. Do not execute generated
JavaScript in Gateway, infer a task from ciphertext, or blindly replay effects.

## Acceptance matrix

| Behavior | Claude | Cursor |
| --- | --- | --- |
| Current-account discovery, exact model catalog, enable/export/projection | Required | Required; all discovered models |
| Text, streaming, multi-turn, real caller tool request/result | Required | Required |
| Responses, Chat Completions, Anthropic Messages | Required via existing adapters | Required via existing adapters |
| Actual Codex Code Mode custom exec | Disclosed best effort; essential semantics safe | Hard gate |
| Official parent → subscription child; reverse direction; same-child followup/wait | Disclosed best effort; essential semantics safe | Hard V2 gate |
| Completed full history after backend/Gateway restart and fresh caller | Required ordinary continuation | Hard gate, including Code Mode/V2 |
| Caller cancellation, cleanup, auth expiry and account changes | Required | Required |
| Linux and Windows release candidate from the same SHA | Required before own release | Required before own release |

The transport contract is model-independent. Deterministic tests cover all
discovered rows and route/effort preservation; bounded live fixtures exercise
the currently selected account model without an artificial whitelist. Do not
guarantee that every model solves every task. Unpredictable disposable fixtures
and child rollouts establish actual tool execution and caller ownership, not
just HTTP 200 or a function named exec. Do not normalize model output to hide
incorrect results (including zero-width characters).

## Evidence and delivery graph

Retain the [2026-09-29 outcome](../evidence/subscription-provider-probe-2026-09-29-W4H0kq/summary.json)
as historical feasibility evidence. Cursor passed actual Code Mode, both V2
directions and completed Code Mode restart. A malformed custom envelope and
V2 restart exact-output failure remain failed. Claude generation was denied by
organization policy; its current entitlement must be rechecked before claiming
live success. Listed models or unexpired OAuth are not permission proof.

1. **Discovery ([#604](https://github.com/NOirBRight/CodexHub/issues/604)):** current CLI account detection and model list, isolated and
   sanitized; no generation, login or production catalog publication yet.
2. **Portable collaboration ([#605](https://github.com/NOirBRight/CodexHub/issues/605)):** current Code Mode declarations, child subsets,
   typed history identity and authoritative-source recovery. Depends on the
   confirmed contracts; independent of account-policy changes.
3. **Cursor backend ([#606](https://github.com/NOirBRight/CodexHub/issues/606)):** AgentService transport and exchange integration. Depends
   on discovery; full V2 acceptance also depends on portable collaboration.
4. **Claude backend ([#607](https://github.com/NOirBRight/CodexHub/issues/607)):** official CLI/MCP exchange, isolated routing and cleanup.
   Depends on discovery; live acceptance depends on organization permission.
5. **Provider workspace:** independent Cursor [#608](https://github.com/NOirBRight/CodexHub/issues/608)
   and Claude [#612](https://github.com/NOirBRight/CodexHub/issues/612) enablement/status/catalog
   and existing Client Projection; Claude consent; Toast/restart feedback.
   Each depends on discovery and its own backend. Preserve unrelated configuration.
6. **Cursor qualification ([#609](https://github.com/NOirBRight/CodexHub/issues/609)):** real-client/protocol matrix, both V2 directions,
   restart, failure/auth/cancellation evidence and dual-platform packaging.
   Depends on portable collaboration, Cursor backend and workspace.
7. **Claude qualification ([#610](https://github.com/NOirBRight/CodexHub/issues/610)):** its ordinary/multi-turn/tool/cancellation matrix,
   disclosed advanced adaptations and dual-platform packaging. Depends on
   Claude backend/workspace and organization permission
   [#611](https://github.com/NOirBRight/CodexHub/issues/611), independently of Cursor.

## Discovery implementation and current evidence

The first implementation exposes `discover_subscription` and the diagnostic
command below. It does not publish catalog rows, enable either Provider or
install a generation backend. The account vendor model list and the existing
downstream Claude native-picker snapshot remain distinct.

```sh
./scripts/codexhub-python.sh scripts/discover_cli_subscription.py cursor-subscription --summary
./scripts/codexhub-python.sh scripts/discover_cli_subscription.py claude-subscription --summary
```

Omit `--summary` to inspect all sanitized ID/display-name rows. The command
exits zero for complete available discovery and nonzero for a bounded blocked
state. `generation_qualified` remains false even on discovery success.
Official CLI configuration overrides (`CLAUDE_CONFIG_DIR`, `XDG_CONFIG_HOME`,
`APPDATA`) determine the source account; `--source-home` supplies the home
fallback. No login/refresh command runs; expired credentials require the
official CLI owner to renew them before discovery is retried.

The [2026-10-01 discovery evidence](../evidence/cli-subscription-discovery-2026-10-01.json)
records all 246 current Cursor IDs. First-party Claude.ai login and CLI 2.1.285
were detected, but its vendor catalog request returned not-eligible. This is a
catalog observation, not a new inference test or a newly proven organization
policy diagnosis. The historical generation denial remains recorded separately.

Execute targeted tests during implementation and the affected full suites once
per candidate under docs/agents/verification-policy.md. Python checks always
use the repository launcher. Release follows docs/agents/release.md and is a
separate action from implementation or draft PR publication.
