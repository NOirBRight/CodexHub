# Issue #199 agents configuration preservation

Candidate base: `ae927c687390017903435cd9bf4314610b6da229` (`origin/dev`)
Independent-review repair fixed point:
`ed3e4afa642dd8ba2b806cdd990cf111444f961a`

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

Apply and restore now hold one shared per-config lifecycle lock from the first
read through final publication and cleanup. Under that lock, interrupted
cleanup requires an existing live config byte-identical to the recovery
backup. Missing or diverged state fails closed without deleting either
recovery artifact. A deterministic thread test proves a retry cannot publish
while restore holds this transaction.

Cleanup is a resumable two-phase operation. Before deleting recovery bytes,
CodexHub atomically records the exact final-config SHA-256 and terminal status
in the takeover sidecar. A retry removes remaining artifacts only after the
live config, original owner, digest, and any remaining backup all revalidate.
Journal publication, backup deletion, and sidecar deletion failures are
covered, including completed owner restoration as well as interrupted
takeover cleanup.

CodexHub continues to own only its existing provider/catalog/context overlay
and websocket feature flags. It does not parse, normalize, enable, or supply
defaults for agent or collaboration V2 configuration.

## Local verification

- `python -m pytest -q tests/test_config_overlay.py`
  - `58 passed, 8 subtests passed in 3.36s`
- `python -m pytest -q --ignore=tests/test_real_client_e2e.py`
  - `1471 passed, 1 skipped, 420 subtests passed in 106.86s`
- `git diff --check`
  - passed
- `python scripts/report_quality_gates.py`
  - report-only exit `0`, `parse_errors: 0`
  - repository baseline: 2 unused imports, 83 dead-function reports, and 146
    duplicate-name reports; no changed-file finding was identified

The issue's literal unpartitioned `python -m pytest -q` command was also
attempted once. It exceeded a 1,200-second local outer bound while including
the separately governed real-client module and emitted no captured failure.
The repository verification policy classifies this diff under the Python core
suite above; none of the changed paths are in the synthetic real-client
contract relevance list.
