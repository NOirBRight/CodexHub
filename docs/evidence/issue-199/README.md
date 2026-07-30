# Issue #199 agents configuration preservation

Candidate base: `ae927c687390017903435cd9bf4314610b6da229` (`origin/dev`)
Independent-review repair fixed point:
`ed3e4afa642dd8ba2b806cdd990cf111444f961a`
Second-review repair fixed point:
`c9a7a09cd7e88c640cf3d71fd69c862f23e3dd5f`
Completion-receipt review fixed point:
`5e9e7e9416f4cecbeef421fc71e20542e2c1a5bf`
Digest-anchor and status-legality review fixed point:
`b7b8e46595f924203a5bd9eb17d1acce81d66282`

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

Cleanup completion is also durable after both recovery artifacts disappear.
Before deleting either artifact, CodexHub publishes and reads back one bounded
completion receipt containing only versioned owner identifiers, a fixed
terminal status, the completed SHA-256, and the exact SHA-256 of a backup that
a later same-owner apply may stage. It contains no config text or credential.
An owned, unowned/unified, or interrupted cleanup retry therefore recognizes
the exact completed generation even when sidecar deletion committed before an
error or process death was reported.

The receipt has a bounded one-file lifecycle rather than an accumulating
history. Context-guard writes rebase its two digests under the lifecycle lock.
A later apply retires it only after the new live config and recovery backup are
durable, and after takeover metadata is durable when ownership changes.
Pre-publication crashes roll back to the completed receipt; ambiguous
receipt-retirement failures recover from the new backup/metadata. Same-owner,
different-owner, Direct Official, explicit-provider no-op, and second-restore
paths are covered. Unrelated artifacts cannot use a stale receipt as recovery
authority.

Takeover metadata reads explicitly classify the sidecar as absent, valid, or
invalid. Only absence permits the legacy no-sidecar restore path. Truncated or
unreadable JSON, future or non-integral versions, unknown fields or owners,
duplicate keys, partial/null journals, malformed digests, unsupported statuses,
and inconsistent owner/digest/status/artifact combinations all fail closed
while retaining the live config, recovery backup, and sidecar.

Active takeover metadata now binds the exact recovery backup bytes with a
lowercase SHA-256 in addition to both owner identities. Takeover apply reads
back the durable backup and sidecar before publishing the new live config.
Apply, context guard, restart, and restore share one typed lifecycle snapshot;
active, interrupted, cleanup-journal, and completion-receipt paths validate the
same owner/digest anchor before any restore or cleanup mutation. Same-marker
drift in unknown agent keys, model/provider fields, comments, or raw newline
bytes therefore fails closed across owned and unowned takeovers, both channel
directions, interrupted takeover, and a fresh-process retry.

Cleanup journal and completion receipt status legality also uses one typed
phase validator. Unified-history transformation statuses are legal only when
the original config was unowned. `interrupted_takeover_discarded` and
`restored_takeover_backup` are the only identity statuses legal with an owned
original. The full original-owner × status × active/journal/receipt matrix is
tested independently from the implementation. A forged owned journal with
otherwise coherent source/recovery/final digests is rejected during sidecar
read, before live publication or receipt creation.

### Active-takeover context-guard re-anchor repair

Final Spec review of `415a45f077dc03f0cc051b09b6121aad4bc14770`
found that a successful context-guard update changed the active takeover's
recovery backup but left `recovery_sha256` anchored to the previous bytes.
The update reported success, while the next restore failed closed with
`recovery backup is missing or diverged`.

Context-guard backup updates now use the existing takeover metadata and
lifecycle phase model under the same canonical per-config lock. The active
anchor remains the base authority while a bounded
`RECOVERY_REANCHOR_JOURNAL` records only the candidate recovery SHA-256.
Journal readback is durable before the candidate backup is published; exact
backup readback is durable before ordinary active metadata promotes the
candidate digest. Resume accepts only the exact base or candidate bytes with
the recorded original owner. Base bytes roll the journal back to the base
anchor, candidate bytes promote the candidate anchor, and any third bytes or
owner mismatch fail closed without changing or deleting recovery evidence.

The context-guard state is durable before an enabled active-takeover update so
a fresh retry retains the distinct prior live and backup values. The recovery
backup and anchor commit before the live config, so every injected failure
between journal, backup, and anchor publication leaves the live takeover bytes
unchanged. A fresh CLI process resolves the typed journal, retries the requested
enable or disable, and restores the original owner with the intended context
settings while preserving provider, model, agents, comments, and unknown TOML.

Tests cover before- and after-commit errors at journal publication, recovery
backup publication, and candidate-anchor publication for both enable and
disable (`12` crash-prefix subtests). Duplicate, future, unknown, malformed,
mixed-phase, and same-owner re-anchor records fail before mutation. A valid
journal with unknown third recovery bytes retains the live config, backup,
metadata, and context state. Re-anchor metadata contains exactly version,
owners, and the two fixed SHA-256 fields; bounded-size assertions prove it
contains no agent config, collaboration setting, provider credential, or
Gateway key.

Completion receipts use the same strict absent/valid/invalid classification.
Legacy or missing fields, truncated or unreadable JSON, duplicate keys,
future/non-integral versions, unknown fields/owners/statuses, malformed
digests, and owner/status inconsistencies all fail closed without mutation.

### 0.1.7 active-sidecar recovery boundary

An active 0.1.7 takeover sidecar has `version`, `takeover_owner`, and
`original_owner`, but no durable recovery digest. The current reader reports
the typed `MISSING_RECOVERY_ANCHOR` reason and refuses Stable/Developer
reapply or restore before changing the live config, backup, sidecar, or
completion receipt. It never silently strips the legacy sidecar or computes a
new digest from the untrusted backup, because either action would bless bytes
whose identity was never recorded.

Safe recovery is therefore an explicit offline operation:

1. Stop Stable, Developer, and the Gateway so no config writer remains.
2. Copy the live config, takeover backup, takeover sidecar, and any completion
   receipt to a separate quarantine location. Do not delete either recovery
   artifact.
3. Compare the backup with an independent known-good pre-takeover copy or have
   the user verify the intended owner/config bytes. The legacy sidecar alone
   cannot establish that identity. If no independent evidence exists, retain
   all artifacts and escalate instead of auto-restoring.
4. With all writers still stopped, restore only the independently verified
   backup bytes to the live config. Move the legacy backup and sidecar together
   to quarantine rather than deleting them, then restart the verified original
   owner channel. Retain the quarantine until normal connect/restore behavior
   is confirmed.

This path deliberately avoids logging or embedding config, provider
credentials, Gateway keys, or agent config contents. New sidecars contain only
fixed owner/version fields and the bounded recovery digest.

Managed-path validation now covers the config, backup, metadata, completion
receipt, context state, lifecycle target, and every corresponding
`file_lock_for` `.lock` and `.lock.guard` namespace. Direct aliases and
hardlinks fail before lifecycle-lock acquisition or file mutation, including
the formerly reentrant `state = lifecycle target` and lock-poisoning
`state = config.lock` cases.

CodexHub continues to own only its existing provider/catalog/context overlay
and websocket feature flags. It does not parse, normalize, enable, or supply
defaults for agent or collaboration V2 configuration.

## Local verification

- Post-review active-takeover context-guard repair:
  - ordinary enable and enable-then-disable lifecycle tests were red on
    `415a45f077dc03f0cc051b09b6121aad4bc14770` because restore rejected the
    stale recovery anchor; both are green after the typed re-anchor repair;
  - committed-backup-error fresh-process retry was red before the journal and
    green afterward;
  - `py -3.13 -m pytest -q tests/test_config_overlay.py`
    - `110 passed, 202 subtests passed in 94.01s`;
  - subsequent parser-only delta:
    `1 passed, 31 subtests passed in 6.64s`;
  - `py -3.13 -m py_compile src-python/config_overlay.py
    tests/test_config_overlay.py` and `py -3.13 -m compileall -q src-python
    tests/test_config_overlay.py` passed;
  - `git diff --check` passed;
  - report-only quality gates exited `0` with `parse_errors: 0`; repository
    baseline remained 2 unused imports, 83 dead-function reports, and 146
    duplicate-name reports, with no changed-file finding.
- `Python313\python.exe -m pytest -q tests/test_config_overlay.py`
  - `106 passed, 186 subtests passed in 43.83s`
- `Python313\python.exe -m pytest -q --ignore=tests/test_real_client_e2e.py`
  with the Python 3.13 directory first on `PATH`
  - authoritative corrected-toolchain run: `1519 passed, 1 skipped, 598
    subtests passed in 133.56s`
  - the first run inherited the Hermes Python 3.11 executable in child-process
    `PATH`: `1517 passed, 1 skipped, 598 subtests passed`, with only the two
    issue #108 PowerShell evidence replays failing
  - sanitized failure artifacts identified Python 3.11 parsing the repository's
    PEP 695 syntax and `evidence_validator_execution_failed`; placing Python
    3.13 first on `PATH` made those isolated two tests pass before the
    authoritative full run
- repair-specific TDD evidence
  - same-owner-marker agent-key backup drift: red because restore accepted the
    bytes; green after durable recovery anchoring
  - owner × status × phase matrix: 12 owned/unowned-only journal combinations
    red; all `81` matrix subtests green after the shared legality validator
  - durable backup/sidecar readback replacement: 2 red subtests; both green
    after pre-live-publication validation
- `py -3.13 -m compileall -q src-python tests/test_config_overlay.py`
  - passed
- `git diff --check`
  - passed
- `py -3.13 scripts/report_quality_gates.py --json`
  - report-only exit `0`, `parse_errors: 0`
  - repository baseline: 2 unused imports, 83 dead-function reports, and 146
    duplicate-name reports; no changed-file finding was identified
- Worker delta inspection does not claim review acceptance; the pushed exact
  SHA remains subject to a fresh Orchestrator-owned Standards/Spec review.

The issue's literal unpartitioned `python -m pytest -q` command was also
attempted once. It exceeded a 1,200-second local outer bound while including
the separately governed real-client module and emitted no captured failure.
The repository verification policy classifies this diff under the Python core
suite above; none of the changed paths are in the synthetic real-client
contract relevance list.
