# Current Code Mode compatibility implementation evidence

Tracker: [#605](https://github.com/NOirBRight/CodexHub/issues/605).
Risk class: strict. Candidate full suite and live subscription qualification
remain owned by the campaign Orchestrator.

A loopback-only capture on 2026-10-01 observed installed Codex CLI **0.159.2**.
It made no vendor inference request and used an empty temporary CLI home with
no account credentials. The sanitized declaration fixture is
`tests/fixtures/collaboration/codex-cli-0.159.2-linux-code-mode.json`.
The prototype's `[features.multi_agent_v2] enabled=true` table no longer enables
this stable feature; the actual current setting is `features.multi_agent_v2=true`.
The captured catalogue includes namespaced custom exec and a native six-method
V2 namespace. This does not claim that Code Mode always advertises every method:
the implementation also recognizes complete typed nested handler declarations
and retains the exact supported caller subset.

Deterministic public-interface checks cover current flat/namespaced custom exec,
additional_tools, eager/deferred preparation, subscription and generic external
Providers, namespace/custom collisions and malformed envelopes, true Call and
Item identity, completed history with a fresh plan, same-child followup targets,
and SSE reconstruction. Official portability uses the existing ordinary
namespace inverse. External preparation uses the existing V2/custom codecs.

Current opaque agent_message content fails before an external generation
request, with bounded instructions for caller-owned restatement/reissue from
original requirements and existing child state/results. A later, nonempty,
entirely plaintext agent_message from the same author to the same recipient
establishes a newer addressed assignment. Only earlier opaque items for that
address become explicitly disclosed historical opaque envelopes. Each envelope
retains the original item, ID, routing and ciphertext strings as unavailable
history, never as a recovered task. Existing child calls/results and the current
plaintext assignment retain their identities and content. This permits the
addressed caller-owned restatement path without replaying completed effects.
Different addresses, assistant/tool output, mixed encrypted/plaintext messages,
and ordinary user turns do not establish that boundary. The ordinary-user/new
task boundary remains **partial**: the current contract supplies no structural
replacement marker, and text such as "Continue" does not prove that the missing
requirements are available. Reasoning ciphertext uses its separate policy.

`recover_observed_assignments` requires explicit executed-assignment provenance
(author, recipient, exact text, source Call ID and typed Item ID). Different
addresses/text or ambiguous calls remain opaque. It has no process-global cache,
JavaScript execution, ciphertext inference or automatic source observation.
This recovery helper is currently **test-only**, with no production observer
or invocation. Native paired collaboration calls/results provide target/message
and real Call/typed Item IDs, but the frozen contract does not provide a trusted
acting-author identity. Spawn results name the child, and send/followup results
may be null; they do not bind the initiating author to an agent_message. The
agent_message itself has author/recipient but no source Call ID. Matching those
items by text, route suffix or assumed `/root` would invent the missing
association. Actual exec source is not parsed or executed to fill that gap.
Therefore production observed-assignment recovery remains **unimplemented**
and is an open requirement under [#605](https://github.com/NOirBRight/CodexHub/issues/605),
rather than an integration claim. Production does not invent provenance from
arbitrary source text; a boundary without trusted observations continues to
reject current opaque tasks. Real mixed-model
V2 lifecycle and restart qualification remains required on the integrated
Provider candidate; the historical 0.158 prototype is not substituted for it.

Targeted Python checks use the repository launcher with the selected development
interpreter and a work-disk TMPDIR (host /tmp quota is exhausted). During
implementation, the final targeted selection passed **637 tests**, including
the sanitized 0.159.2 Gateway fixture, namespace/flat custom owner collisions,
complete SSE terminal output, and repository entry/seam/module boundaries.
The report-only quality command reported zero parse errors and no new-module
unused imports. No full-suite or hosted Actions gate is claimed here.

The completed-history and addressed historical-opaque followup selection passed
**753 tests and 113 subtests**. It includes actual converter-generated complete
output replay (empty/nonempty completed messages and function calls), flat and
namespaced custom continuation, fresh request contexts, and both tool-surface
strategies across subscription/generic external Providers. These are local
public-seam checks; they do not qualify a live historical sealed task or supply
the missing production assignment observer.
