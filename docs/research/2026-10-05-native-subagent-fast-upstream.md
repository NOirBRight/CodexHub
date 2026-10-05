# Native subagent Fast: upstream issues and workarounds

Checked 2026-10-05 against the public `openai/codex` GitHub repository and its
official CLI/configuration documentation.

## Finding

The requested behavior—keep the root session on Standard while a selected
native Luna subagent uses Fast—has been reported directly upstream. It is not
available through the current native spawn path. The merged change that made
subagents inherit the root tier is still present in upstream `main` at
`823ea830c0fd418b09ff02d36cad9a1fff66465b`.

There is a practical process-level workaround: launch a separate `codex exec`
with a per-invocation `service_tier = "fast"` override. That process has its
own tier, so it does not change the parent session. It is an independent Codex
run rather than a native `spawn_agent` child: the caller must pass task context
and collect the result, and it will not appear as a child in the native agent
tree. The command form is documented by Codex and was confirmed against the
local CLI help; the outgoing request was not live-tested here.

```sh
codex exec --model gpt-6-luna -c 'service_tier="fast"' 'Do the worker task'
```

See the official [Codex `exec` command documentation](https://learn.chatgpt.com/docs/developer-commands?surface=cli#cli-codex-exec)
and [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
for invocation and config override semantics.

## Direct upstream reports

| Issue | State on 2026-10-05 | What it establishes |
| --- | --- | --- |
| [#45568 — Standard Astra coordinator with Fast Luna workers](https://github.com/openai/codex/issues/45568) | Open; 0 comments | Exact Desktop workflow and requested explicit per-role/per-spawn control. No maintainer response yet. The report itself says it has not independently verified backend routing or billing. |
| [#42665 — custom-agent tier overrides](https://github.com/openai/codex/issues/42665) | Open | Requests a human-authored role `service_tier` override while retaining inheritance when omitted. Its only non-bot comment is a user adding the Astra/Luna use case; no maintainer response. |
| [#42612 — custom-agent Fast ignored with Standard parent](https://github.com/openai/codex/issues/42612) | Open | Reports CLI 0.153.0 ignoring role `service_tier = "fast"`; the author says the same setup worked on CLI 0.152.1, based on direct observation. That version claim conflicts with the official [`rust-v0.152.1` source](https://github.com/openai/codex/blob/rust-v0.152.1/codex-rs/core/src/tools/handlers/multi_agents_common.rs#L322-L349), which applies the root tier, and the [spawn path](https://github.com/openai/codex/blob/rust-v0.152.1/codex-rs/core/src/tools/handlers/multi_agents/spawn.rs#L98-L109), which applies the same override after loading a role. GitHub's [tag comparison](https://github.com/openai/codex/compare/dc2ccc6843abb09c9d297862dc10b6bd12a3935d...rust-v0.152.1) confirms the tag contains the #41308 commit. The public report does not explain the discrepancy, so 0.152.1 is not a substantiated workaround. A commenter traces the current behavior to #41308. |
| [#35187 — choose Normal/Fast per `spawn_agent`](https://github.com/openai/codex/issues/35187) | Open | Asks for a per-call tier parameter. It has a user follow-up asking for an update, but no maintainer response. |

The separate role-file and per-spawn requests are both still unresolved in the
issues checked. No merged PR restoring independent per-subagent Fast was found
in the relevant PR searches.

## Why native spawn cannot currently honor it

[PR #41308](https://github.com/openai/codex/pull/41308), merged 2026-08-28 as
[`dc2ccc6843abb09c9d297862dc10b6bd12a3935d`](https://github.com/openai/codex/commit/dc2ccc6843abb09c9d297862dc10b6bd12a3935d), deliberately shares the root
thread's current tier across the agent tree and removes role-level and
per-spawn tier overrides. The current upstream source still applies this rule
when it prepares a spawned child
([`spawn.rs`](https://github.com/openai/codex/blob/823ea830c0fd418b09ff02d36cad9a1fff66465b/codex-rs/core/src/agent/control/spawn.rs#L453))
and again while resolving each native child request
([`session/mod.rs`](https://github.com/openai/codex/blob/823ea830c0fd418b09ff02d36cad9a1fff66465b/codex-rs/core/src/session/mod.rs#L3717-L3733)).
Therefore, storing `service_tier = "fast"` in the role or changing only the
initial child config cannot work around the runtime policy; a client-side fork
would need to change both enforcement points and account for reloads.

Separate reports describe the opposite direction: [#29940](https://github.com/openai/codex/issues/29940)
and [#30407](https://github.com/openai/codex/issues/30407) describe children
using Fast when the parent is Standard, and [#38277](https://github.com/openai/codex/issues/38277)
reports a child retaining Fast after the parent changes back to Standard. These
reports illustrate the inheritance tradeoff. PR #41308's description says its
goal is root-controlled routing; it does not say these reports motivated the
change. They also do not resolve the explicit per-worker override request.

Other tier reports are adjacent rather than workarounds. [#39740](https://github.com/openai/codex/issues/39740)
reports that a Luna child accepted only `priority`/Fast and rejected `default`
in one App/API-login setup. [PR #46230](https://github.com/openai/codex/pull/46230)
preserves configured Flex service tiers; it does not restore independent Fast
for native children. Neither changes the root-tier inheritance rule.

## Discussions and search scope

The bounded GitHub Discussions searches (`repo:openai/codex subagent fast`,
`repo:openai/codex service_tier agent`, and `repo:openai/codex "Fast" worker`)
did not surface a dedicated discussion about Standard roots with Fast native
children. The closest matches were:

- [#13974 — community Agent Team Orchestrator](https://github.com/openai/codex/discussions/13974)
  describes a separate orchestration runtime with YAML `fast/default/deep`
  profiles. It predates native multi-agent support and does not show a way to
  configure the built-in `spawn_agent` path.
- [#46658 — adaptive model/tool/subagent allocation](https://github.com/openai/codex/discussions/46658)
  discusses resource selection broadly, without proposing an independent
  native service-tier control.

These searches are evidence about the public repository results, not proof that
no related discussion exists elsewhere.

## Practical options

- For an immediate workaround while keeping the root Standard, run the worker
  in a separate `codex exec` with `-c 'service_tier="fast"'`. Supply the
  relevant task/context and pass its output back manually or through a wrapper.
- Upstream `rust-v0.151.0` is a source-confirmed native-role candidate: the
  spawn path loads the role and then chooses the first supported tier from
  `[role config, per-spawn request, parent tier]`, so an explicit role tier can
  take precedence over a Standard parent
  ([tier selection](https://github.com/openai/codex/blob/rust-v0.151.0/codex-rs/core/src/tools/handlers/multi_agents_common.rs#L321-L373),
  [call order](https://github.com/openai/codex/blob/rust-v0.151.0/codex-rs/core/src/tools/handlers/multi_agents/spawn.rs#L98-L115)).
  This behavior is absent by `rust-v0.152.0`
  ([source](https://github.com/openai/codex/blob/rust-v0.152.0/codex-rs/core/src/tools/handlers/multi_agents_common.rs#L322-L349))
  and remains absent in 0.152.1.
  It is only a candidate for an isolated CLI version pin: no live test was made
  with GPT-6 Luna, a current Desktop build, or the user's subscription, and the
  older client may not recognize the current model catalog. Do not assume it
  restores the current Desktop flow.
- Setting the root session to Fast would make the whole tree Fast and therefore
  does not meet the independent-worker requirement.
- Do not treat CLI 0.152.1 as a workaround: #42612 reports it worked in the
  author's setup, but the official source tagged `rust-v0.152.1` already applies
  the root service tier after loading a role. The posted evidence does not
  explain this conflict.
- A local Codex fork could restore role-tier behavior, but it must remove both
  spawn-time and request-time root-tier enforcement and preserve the chosen
  policy through child reloads. No upstream patch or maintained fork providing
  that behavior was found in this search.
