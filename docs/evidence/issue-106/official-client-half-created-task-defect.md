# Upstream report: failed Task creation leaves an unmanageable sidebar stub

## Summary

When a new sidebar-visible, worktree-backed Task times out during startup, the
official client can leave a visible client placeholder and a provisioned
worktree without materializing a rollout/session. The resulting object is not
a native Task and cannot be managed or removed through the supported Task API.

## Preconditions

- A global optional MCP server delays `thread/start` initialization past the
  client materialization timeout.
- Create one new worktree-backed Task. Do not retry the creation after timeout;
  retries create additional ambiguous placeholders.

## Observed result

1. Worktree setup and a client-side placeholder complete.
2. No rollout/session materializes.
3. Native Task listing omits the placeholder.
4. Read, message, and rename have no usable Task target; archive and supported
   CLI deletion reject the placeholder because no real session exists.
5. Clean orphan worktrees can be removed independently, but a failed
   placeholder can retain an empty client-held directory until the client
   releases its file handle.

## Expected result

Task creation should be atomic from the sidebar's perspective: either a native
Task/rollout is materialized and returned, or the client deterministically
removes the placeholder and any provisioned worktree. If cleanup is deferred,
the sidebar should expose a supported retry/cancel action and retain enough
state for that action to succeed.

## Impact

The sidebar can show duplicate-looking or permanent `New task` cards that do
not correspond to any native Task. Operators cannot archive or delete them
through supported interfaces, and repeated retries risk accumulating orphan
worktrees.

## Scope and safety boundary

This is a retained historical incident report, not a current live-control
replay. Its fixture verifier validates only sanitized structural facts; it does
not create a live Task or establish active Task listing, remote-control
enrollment, worktree/placeholder cleanup, or repeated-run leak coverage. The
historical A/B isolated the startup delay to an optional global MCP server; the
reported full-access preflight was policy metadata, not a filesystem or network
access exercise. CodexHub does run bounded external app-server probes for model
and usage reads, but no committed path proves that those probes start or break
this MCP, or that they control native Task persistence. CodexHub does not manage
this MCP server or the native Task persistence lifecycle. No credentials, system
proxy, official binary, global configuration (beyond the reversible diagnostic
A/B), protocol translation, or internal Codex database should be changed to
work around this defect.

## Requested fix

Provide an official client-side rollback path for a Task that has acquired a
client placeholder or worktree but has no rollout/session, including removal of
the sidebar card and deterministic release/removal of its worktree directory.
