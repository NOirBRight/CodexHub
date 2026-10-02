# CLI 0.153.4 declaration captures

Captured on 2026-09-06 from Linux and native Windows CLI 0.153.4 using an
empty temporary Codex home and a loopback Responses server. V1 disables
multi_agent_v2; V2 enables it with non_code_mode_only=false. Each fixture
contains only Collaboration namespace declarations; descriptions are replaced
with `sanitized`. No credentials, prompts, session IDs or local paths are kept.

The Windows V1-only fixture additionally reproduces the release failure with
the isolated OpenCode Go client configuration. Its spawn_agent declaration
omits agent_type and service_tier. The existing frozen contract already accepts
omission of agent_type; production now also accepts omission of service_tier
for V1 only. All remaining schema fields, including required and encryption,
continue to match exactly. The historical #392 evidence is unchanged.

## Current Code Mode capture (0.159.2)

`codex-cli-0.159.2-linux-code-mode.json` was captured on 2026-10-01 using
an empty temporary Codex home and a loopback endpoint that rejects before
inference. The account-free configuration enables `features.multi_agent_v2=true`
and Code Mode. The current runtime advertises namespaced `functions.exec`
(custom), its caller-owned wait/input methods, clock.sleep, and a native V2
namespace. Both original tool placements are hoisted in the sanitized fixture;
only the exec transport-identification lines survive dynamic-description
sanitization. This is declaration evidence, not a live model or V2 lifecycle
qualification. Tests with nested-handler-only Code Mode descriptions exercise
supported signature parsing and caller subsets separately.
