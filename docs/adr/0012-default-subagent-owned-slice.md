# ADR-0012: Default subagent is a connected sibling owned slice

Date: 2026-09-17
Status: Accepted

Amends ADR-0004. Does not reopen Provider Injection, Activation, or the
Codex History Bucket exception.

## Context

ADR-0004 owns exactly the Injected Block (Client Provider Groups plus one
credential key) and treats Activation as user-owned, never drift. Codex already
pins Default subagent through the overlay (`agents.default_subagent_model` /
`default_subagent_reasoning_effort`) without making that pin Activation.

OpenCode, ZCode, OMP, and Grok CLI have first-party spawn-target keys for
built-in child sessions. Those keys sit outside the Injected Block. Leaving
them foreign means children inherit the parent session; taking them over as
provider entries would expand the Injected Block past ADR-0004.

## Decision

1. **Default subagent is a third ownership class.** The user owns the choice
   (Clients-page pin, empty = CLI default). CodexHub owns the bytes only while
   that client is connected. This is not Activation and not Managed Takeover.
2. **The Injected Block stays the provider/credential set.** Spawn-target keys
   are a sibling owned slice: OpenCode `agent.{general,explore,scout}`, OMP
   `task.agentModelOverrides` for bundled agents, ZCode built-in
   `general-purpose` / `Explore` agent files, Grok `[subagents.models]` plus
   `[subagents.roles.<type>].reasoning_effort` (shadow agent files only when a
   built-in type cannot carry role effort).
3. **Connect does not invent a pin.** An empty pin leaves pre-existing native
   spawn-target keys untouched. A saved pin is written on Connect, Repair,
   republish, and save-while-connected.
4. **Disconnect restores the slice surgically** from the pre-connect baseline,
   together with Injected Block detach. Custom / non-built-in agents stay
   foreign.
5. **Connected hand-edits of this slice are drift.** Activation and foreign
   keys remain unvalidated. Disconnected hand-edits are the user's.
6. **Pi and DSH are out of this ADR.** Codex overlay keys stay on the Codex
   History Bucket path and are unchanged.

## Consequences

- Client adapters may write spawn-target files beyond the Injected Block, but
  only the documented built-in targets, and only while connected.
- Readback / expected-match for those four clients includes this slice.
- A stale catalog slug reverts the pin to CLI default without failing Connect.
