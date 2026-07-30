# Issue #199 agents configuration preservation

Candidate base: `ae927c687390017903435cd9bf4314610b6da229` (`origin/dev`)

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
Developer takeover, interrupted takeover cleanup, each owner restore, and exact
final restoration of the pre-owned bytes. The interrupted-takeover fixture
exposed a defect: after the takeover backup and sidecar were durable but before
the live config write completed, restore rewrote the still-active Stable
configuration as unified Official history. Cleanup now recognizes that pending
state, removes only the incomplete Developer takeover artifacts, and leaves the
prior owner's live configuration byte-for-byte unchanged.

CodexHub continues to own only its existing provider/catalog/context overlay
and websocket feature flags. It does not parse, normalize, enable, or supply
defaults for agent or collaboration V2 configuration.

## Local verification

- `python -m pytest -q tests/test_config_overlay.py`
  - `49 passed, 8 subtests passed in 2.60s`
- `python -m pytest -q --ignore=tests/test_real_client_e2e.py`
  - `1462 passed, 1 skipped, 420 subtests passed in 114.73s`
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
