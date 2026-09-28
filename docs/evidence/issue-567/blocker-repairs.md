# F1–F3 repair and revalidation

Date: 2026-09-28. Scope: the three reproduced defects in the
[original acceptance audit](acceptance-audit.md), not full acceptance of
#567–#575. No release, production install, or Issue closure is implied.

Final application source candidate: `48df83453c68355007860ded3db30e11d07a8a5d`.
Review delta: `e197c9cc...48df8345`; the prior user-confirmed `cfb6f893` review
is retained, with only these later changes reviewed again. Test-only commit `5fc3c385` makes settings-preservation assertions unconditional on the actual
`runtime-settings.json` file.

## Repairs

- **F1:** The pinned component now remembers the digest of the one native
  environment envelope it successfully validated. A subsequent turn can reuse
  it only within the same stored thread, with the same item identity/content,
  no current environment update, and compatible current sandbox/workspace
  metadata. Different, missing, malformed or cross-thread claims stay rejected.
  No cwd or permission grant is invented. The cache persists across component
  restarts; normal expiry and current-request tool declarations still apply.
- **F2:** The shared request boundary recognizes `x-claude-code-session-id`,
  preferring it over generic session hints. Claude and generic session keys are
  separately namespaced, including their tool ownership. Arbitrary
  `metadata.user_id` does not become a session identity. Existing Messages
  HTTP lifecycle tests now use the actual Claude header.
- **F3:** Upgrade records the verified generation's pin and a pending-first-start
  marker. If the replacement fails startup, owned processes are stopped before
  moving installations. Recovery validates the old pin, install record and
  archive hash, restores and starts the old generation, and reports the failed
  upgrade explicitly. The same verification/pin recovery now also covers failed
  promotion before a new process starts (`48df8345`); a different replacement
  pin is regression-tested. The effective recovered pin is limited to the exact failed
  target pin; future incompatible targets cannot silently reuse it. Legacy
  installs without saved pins are retained only after compatibility/hash checks.
  Login data and persisted settings remain in place. Corrupt/incompatible old
  generations are never executed. A normal later failure after a successful
  upgrade does not trigger this first-start rollback.

## Revalidation

**F1 real provider/client:** Codex CLI 0.157.1, Linux, existing authorized
isolated ChatGPT account, `chatgpt-web/gpt-5.6-sol`. Fresh workspace and client
profile, 180-second bound per step:

| Step | Outcome | Elapsed |
|---|---|---|
| First text contains unpredictable memory code | Exact response | 17.75s |
| `exec resume --last` asks for code without repeating it | Exact response, same thread | 19.22s |
| Next resume asks to read an unpredictable file | One real terminal command, exact file result and answer | 25.76s |

[Sanitized live evidence](live-codex-context-retest.json). A preceding attempt
failed at the first turn because the ChatGPT connector menu did not open; after
the readiness helper had finished and the browser was idle, a fresh isolated
attempt passed. That intermittent connector-menu failure is recorded, not
claimed repaired or hidden by the successful retry. No raw client requests,
account files, credentials, or private logs are committed.

**F2 actual client request capture:** Claude Code 2.1.283 now resolves its real
session header; OpenCode 1.18.32 continues to resolve `x-session-id`.
[Capture evidence](client-request-contracts-retest.json). The capture endpoint
intentionally returns HTTP 400 and uses synthetic credentials. This is identity
contract proof, not live Claude/OpenCode model or tool success. Public HTTP
fixtures separately cover Messages streaming, multi-turn identity, tool-result
ownership/replay, cancellation and upstream errors.

**F3 failure injection:** Public install/upgrade/start/status APIs with synthetic
verified archives. A healthy generation is replaced by an archive exiting17;
startup reports rollback, the original generation runs, and another stop/start
works. Both retained-pin and legacy-pin cases preserve account-marker bytes and
persisted settings. Corrupt old archive / incompatible old pin cases fail closed.
The final module passes all **47 cases on each platform**, including six
startup/promotion recovery cases. [Parsed JUnit evidence](recovery-retest.json)
records exact source and test identities. This proves process/install recovery
without using a real account as a destructive failure fixture.

## Engineering and artifact checks

- Linux Python core at `65c48d16`: **3583 passed, 196 skipped, 283 subtests**.
  Command: repository Python3.13 launcher, `-m pytest -q
  --ignore=tests/test_real_client_e2e.py`. Final pin/preparer delta: **2 passed**.
- Upstream runtime suite: **793 passed, 29 skipped, 3 failures** caused by a
  missing Electron installation in the isolated development dependencies.
  After installing the pinned Electron package, the entire affected
  `zero-risk-adapter.test.ts` module: **11 passed, 0 failed**. The initial full
  command did not exit zero. Earlier invocation problems from temporary paths
  and missing dependencies are not counted as product evidence.
- Original captured Codex request replay turned red before the runtime fix;
  environment/history tests after the fix: **73 passed, 1 platform skip**
  (including that private replay). The committed regression uses synthetic
  native-shaped messages and checks persistence, altered claims, another
  thread, current updates and sandbox conflict.
- Runtime source typecheck passes. Linux and Windows each pass relocated and
  unpacked bundle smoke, built from the same source tree and reviewed patch.
- Python partitions: core3780 + synthetic164 = full3944; disjoint and complete.
  No changes to the synthetic E2E harness contract; its prior qualification is
  retained. Report-only quality scan completed, zero parse errors.
- Final Linux runtime-manager module at `48df8345`: **47 passed** (including
  unconditional persisted-settings assertions and different-pin promotion failure).
- Windows Python core at `15a00228`: **3706 passed, 70 skipped, 283 subtests,
  3 failures** caused by the absent embedded Python runtime in the new isolated
  checkout. After copying the existing isolated embedded runtime and passing
  `Prepare-PythonRuntime.ps1 -CheckOnly`, the complete runtime-contract and
  runtime-manager modules at `48df8345` report **165 passed, 3 skipped**. JUnit
  confirms every original failing test is covered and passed. The original
  full Windows command did not exit zero; its result is retained with the
  corrective rerun, not relabeled as a clean full run.

| Runtime artifact | SHA256 |
|---|---|
| Linux x64 | `2422e62c875714a5627420367b2526887fe0b36ab71144e8e984400087e8be8f` |
| Windows x64 (built on Yoga) | `4667ea434130dc5374802e5bde8d1e3756beda2bdff3d315d53f65b8d7c0ec0a` |

Runtime revision `5a307bb48de3d96465305288a2fff5c4086299fb`, tree
`b4ea65a73c4b481497a012c1a9f569e024f6f9ce`, upstream base `a13cd099`.
The [cumulative source patch](codex-chatgpt-web-runtime.patch) has SHA256
`7b2c91dcec4e3d4c5bd977f6eeaf3082f11feddd3a8558e8b02c36addddcd717`.
The original issue590 patch remains unchanged as historical evidence.

## Standards

Exact-candidate Standards review through `48df8345`: **0 hard findings** after updating the built
Windows artifact's pin. Judgement-only notes: recovered-start errors represent
a failed upgrade even when the old process is running; session key encoding and
pin constant duplication could be clarified if those interfaces grow. No
speculative module rewrite was added.

## Spec

Spec review found no remaining concrete defect in the repaired F1–F3 paths;
acceptance gaps below remain explicit. The named defects have implementation
and bounded regression evidence; full
#567–#575 acceptance remains open. In particular this does not prove actual
Claude/OpenCode provider turns, the complete V2 lifecycle, post-compaction
live-tool continuation, images, Desktop behavior, or a complete dual-platform
release matrix. The prior portable7608 has not been replaced by this source
repair and still contains the original bugs.
