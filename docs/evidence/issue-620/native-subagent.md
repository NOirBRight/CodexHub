# Grok CLI native Default subagent — #620

Contract checked on 2026-10-02 with the installed official Grok CLI:
`grok 1.0.44 (5b807183dd79) [stable]`. The public source was separately pinned
to xai-org/grok-build commit `2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8`.
The installed binary's short build hash is not a published commit in that repo;
these are two separate pieces of evidence, not a claim of identical builds.

## Native contract

- [Official subagent guide](https://github.com/xai-org/grok-build/blob/2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8/crates/codegen/xai-grok-pager/docs/user-guide/16-subagents.md): the builtin types are general-purpose, explore and plan; config.toml uses `[subagents.models]` for type-specific model pins.
- [Native role schema](https://github.com/xai-org/grok-build/blob/2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8/crates/codegen/xai-grok-subagent-resolution/src/config.rs): role tables expose `model` and `reasoning_effort`. Native Save changes those keys and builtin model pins, preserving other role fields and custom agents.
- [Production role and definition resolution](https://github.com/xai-org/grok-build/blob/2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8/crates/codegen/xai-grok-subagent-resolution/src/definition.rs) selects the role named for the builtin type before persona defaults. [Production model resolution](https://github.com/xai-org/grok-build/blob/2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8/crates/codegen/xai-grok-shell/src/agent/subagent/mod.rs) applies runtime role models before per-type model pins. An explicit later Gateway selection therefore replaces the native role model while retaining the native detach baseline.
- [Native agent definition schema](https://github.com/xai-org/grok-build/blob/2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8/crates/codegen/xai-grok-agent/src/config.rs) calls its YAML effort field `effort`, not `reasoning_effort`. Native Save edits existing builtin frontmatter while retaining prompt bodies and unrelated fields. Non-table role values remain intact; actual installed bundled definitions supply the fallback without inventing a builtin prompt. A prior Gateway shadow is replaced using its original native baseline before the native edit.

## Actual native model discovery

The installed `grok models` command completed successfully and reported:

| Native picker identity | Advertised effort values | Native default |
| --- | --- | --- |
| grok-4.7 | xhigh, high, medium, low | high |
| grok-4.7-build-fast | xhigh, high, medium, low | high |
| grok-4.6 | xhigh, high, medium, low | high |
| grok-4.5 | high, medium, low | high |

Effort metadata was read from each entry's `info` object in the official
client's models_cache.json. The adapter returns only model labels/identities
and declared effort values, plus configured native model entries. It does not
return cache credentials, login identity, endpoints or authentication data.
The Gateway's xAI exclusion does not apply to native discovery. The real
build-fast entry is an ordinary model choice; no extra Fast switch or
unadvertised effort grade is created. Empty effort removes the explicit
override and lets the client resolve its default.

## Verification

`cargo test --locked grok -- --test-threads=1` passed 20 tests, including six
native contracts covering disconnected save/readback/reset, native xAI model
menus and credential omission, Connect/republish/Disconnect with manual native
edits, explicit Gateway selection with latest native detach baseline, and
owned shadows/non-table roles with preserved builtin prompt bodies, and an
absent original config baseline. Tests use
the existing isolated managed-client harness and actual pinned packaging
resources supplied by the Orchestrator.

`npx tsc --noEmit` passed after incorporating the Orchestrator's shared locale
fix a3e4ce32. Frontend default-subagent/IPC-name checks passed 20 tests; the
Rust-generated command-manifest checks passed three tests. `git diff --check`
passed. Report-only quality gates exited 0 with zero parse errors and reported
existing nonblocking findings.

The Clients page uses native-prefixed option identities and the existing Toast
lifecycle, naming Grok CLI as the client to restart after a direct save. Native
save does not publish providers or change the existing connection. Active
native selection suppresses stale Gateway preference; an explicit subsequent
Gateway selection remains authoritative on reconnect. Publishing captures
the latest non-Hub native slice regardless of active state for later detach.

No inference request or interactive Grok session was used as evidence. Full
strict suites, final review and integrated UI verification remain with the
Orchestrator.
