# Issue #199 agents configuration preservation

Candidate base: `ae927c687390017903435cd9bf4314610b6da229` (`origin/dev`)
Independent-review repair fixed point:
`ed3e4afa642dd8ba2b806cdd990cf111444f961a`
Second-review repair fixed point:
`c9a7a09cd7e88c640cf3d71fd69c862f23e3dd5f`

## Proven lifecycle

The regression fixture follows Codex `rust-v0.145.0` agent configuration:

- `[agents]` enablement, canonical
  `max_concurrent_threads_per_session`, and the retained `max_threads` alias;
- default subagent model and reasoning effort;
- nested role descriptions, `config_file` paths, and nickname candidates;
- structured `features.multi_agent_v2` settings with explicit enabled and
  disabled choices;
- an absent backend/defaults case proving that CodexHub does not invent a V2
  choice, agent tree, role, model, or reasoning default;
- an unknown future agent value proving open-set preservation.

Tests exercise Stable connect, same-owner restart/reapply and readback,
takeover in both Stable/Developer directions, interrupted takeover cleanup,
each owner restore, and exact final restoration of the pre-owned bytes. Both
inline structured `features.multi_agent_v2` and explicit
`[features.multi_agent_v2]` table syntax are covered.

The interrupted-takeover fixture exposed a defect: after the takeover backup
and sidecar were durable but before the live config write completed, restore
rewrote the still-active Stable configuration as unified Official history.
Independent review then identified that owner markers alone were insufficient:
a missing or same-owner-but-diverged live file could lose its only backup, and
a concurrent takeover retry could invalidate restore's snapshot before
cleanup.

Apply, restore, and context-guard writes now hold one shared, canonical
per-config lifecycle lock from the first read through final publication and
cleanup. Managed paths are canonicalized before use, and path aliases or
hardlinks between the config, backup, sidecar, and context-guard state are
rejected before mutation. Under the lock, every writer preflights takeover
metadata: terminal cleanup completes before a new write, invalid or
unrecognized state fails closed, and only an exact takeover retry may proceed
from interrupted state. Missing or diverged state retains every recovery
artifact. A deterministic thread test proves a retry cannot publish while
restore holds this transaction. Real writer-versus-writer subprocess tests
prove apply and context guard serialize, and that a second restore resumes a
published cleanup journal after the contending restore process is killed.

Cleanup is a resumable two-phase operation. Before deleting recovery bytes,
CodexHub atomically records separate source-config, recovery-backup, and
intended-final SHA-256 digests plus the terminal status in the takeover
sidecar. This journal is durable before final config publication. A retry
publishes or cleans up only after the live config, both owners, intended
terminal transformation, and every remaining artifact revalidate. Journal
publication, final publication, backup deletion, and sidecar deletion failures
are covered before and after each operation's externally visible commit point,
including transformed unified history from an unowned original, completed
owner restoration, and interrupted takeover cleanup.

Takeover metadata reads explicitly classify the sidecar as absent, valid, or
invalid. Only absence permits the legacy no-sidecar restore path. Truncated or
unreadable JSON, future or non-integral versions, unknown fields or owners,
duplicate keys, partial/null journals, malformed digests, unsupported statuses,
and inconsistent owner/digest/status/artifact combinations all fail closed
while retaining the live config, recovery backup, and sidecar.

CodexHub continues to own only its existing provider/catalog/context overlay
and websocket feature flags. It does not parse, normalize, enable, or supply
defaults for agent or collaboration V2 configuration.

## Local verification

- `python -m pytest -q tests/test_config_overlay.py`
  - `79 passed, 37 subtests passed in 10.24s`
- `python -m pytest -q --ignore=tests/test_real_client_e2e.py`
  - corrected-toolchain rerun: `1491 passed, 1 failed, 1 skipped, 449
    subtests passed in 109.43s`
  - the sole failure was the unrelated localized-PowerShell stderr assertion
    `test_portable_rejects_invalid_flavor_before_building`; it passed in the
    immediate focused diagnostic run together with the two child-Python tests
    affected by the restarted host's initial `PATH`
- `git diff --check`
  - passed
- `python scripts/report_quality_gates.py`
  - report-only exit `0`, `parse_errors: 0`
  - repository baseline: 2 unused imports, 83 dead-function reports, and 146
    duplicate-name reports; no changed-file finding was identified
- local Standards/Spec self-review
  - writer-preflight, duplicate-key, path-alias, and ambiguous-commit edge
    cases found during review were repaired; the final delta has no remaining
    actionable finding

The issue's literal unpartitioned `python -m pytest -q` command was also
attempted once. It exceeded a 1,200-second local outer bound while including
the separately governed real-client module and emitted no captured failure.
The repository verification policy classifies this diff under the Python core
suite above; none of the changed paths are in the synthetic real-client
contract relevance list.
