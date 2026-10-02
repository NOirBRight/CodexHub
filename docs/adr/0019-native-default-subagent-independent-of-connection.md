# ADR-0019: Native Default subagent settings are independent of connection

Date: 2026-10-02
Status: Accepted product direction; implementation pending per-client tickets.

Amends ADR-0013 and the Default subagent lifecycle in ADR-0015. Provider
Injection, Activation, the Codex History Bucket exception, and existing
Gateway-backed Default subagent behavior otherwise remain unchanged.

## Context

Today a disconnected client can save a Default subagent preference in CodexHub,
but the preference is only written to the client on Connect. ADR-0013 treats
the spawn-target keys as a connected owned slice and restores their baseline
on Disconnect. The user wants to keep existing client connections while also
editing each client's native Default subagent without connecting to CodexHub.

The user selected native client models for disconnected operation, rather than
using Gateway models through a separate child-only connection, and requested
coverage of every integrated client.

## Decision

1. **Native Default subagent is an independent user setting.** An explicit save
   writes the client's native spawn-target configuration even while disconnected.
   Saving only a deferred CodexHub preference does not satisfy this behavior.
2. **Keep the connection independent.** A native Default subagent save does not
   Connect, Disconnect, change Activation, change the main model, or change the
   client's existing route, providers, or authentication. It uses models already
   available through the client's own model/provider configuration and credentials.
   It does not inject a Gateway provider or acquire subscription credentials.
3. **Keep native identity and capability.** Native choices come from the client,
   rather than requiring membership in CodexHub's exported catalog. Reasoning
   effort and Fast are written only where supported by that client's native
   configuration; the existing settings remain available where supported.
4. **Preserve explicit native saves through the connection lifecycle.** Connect,
   republish, and Disconnect must not unintentionally replace a separately saved
   native choice with a stale Gateway preference or an older rollback snapshot.
   Existing Gateway-backed pins remain supported while connected. Their detach
   still restores the appropriate native configuration; this decision does not
   make a Gateway-only model usable after Disconnect.
5. **Apply only the Default subagent change.** Preserve unrelated configuration
   and custom agents. Read back the written native setting and use the existing
   Toast lifecycle to disclose the client's exact reload/restart requirement.
   There is no universal new restart rule.
6. **Cover all integrated clients.** Scope includes Codex, Claude Code, OpenCode,
   ZCode, OMP, Grok CLI, Pi, and DSH. The repository already has spawn-target
   integration for the first six. Pi and DSH require investigation of their
   native capability before support can be claimed. If a client does not offer
   the native capability, record version-specific evidence and the limitation;
   do not invent client settings or add a Gateway-owned scheduler to simulate it.

## Consequences

- The connected ownership rule of ADR-0013 continues to describe Gateway-backed
  pins; it no longer prohibits explicit native Default subagent writes while
  disconnected.
- Claude Code's Disconnect restore in ADR-0015 must preserve independently saved
  native Default subagent settings without changing the separate family mappings,
  default-model selection, or subscription authentication contract.
- Each client is a complete implementation slice: native model/configuration
  readback, user selection, persistence, native apply, connection lifecycle, and
  focused verification. No shared architecture refactor is required by this ADR.
- Per-client work changes persistence and configuration lifecycle and therefore
  uses the repository's strict verification class with the affected suites.

## Implementation tickets

The user approved eight independent client slices. All tickets are published
with `ready-for-agent` and have no blocking dependencies:

- [Codex: #615](https://github.com/NOirBRight/CodexHub/issues/615)
- [Claude Code: #616](https://github.com/NOirBRight/CodexHub/issues/616)
- [OpenCode: #617](https://github.com/NOirBRight/CodexHub/issues/617)
- [ZCode: #618](https://github.com/NOirBRight/CodexHub/issues/618)
- [OMP: #619](https://github.com/NOirBRight/CodexHub/issues/619)
- [Grok CLI: #620](https://github.com/NOirBRight/CodexHub/issues/620)
- [Pi: #621](https://github.com/NOirBRight/CodexHub/issues/621)
- [DSH: #622](https://github.com/NOirBRight/CodexHub/issues/622)
