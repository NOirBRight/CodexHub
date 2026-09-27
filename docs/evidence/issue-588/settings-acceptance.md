# Issue #588 settings and runtime acceptance

**Evidence status:** Linux and Windows x64 package qualification is complete
for candidate `c0d4840ed411e4089cb7f675cb1181dc645e49bd` (`0.2.29`). Linux has
real-account and client-flow evidence. Windows package settings and runtime
lifecycle checks used synthetic state only; Windows login remains unknown and
text/tools readiness was not established. No application release or
production installation has occurred.

The Web Runtime source pin and build contract are recorded in
[`runtime-packaging.md`](runtime-packaging.md). Both platform packages below
were built from the same CodexHub candidate. The c0 product source tree matches
the Linux live-tested predecessor `f2f95fe1aff0294dc28b848ed4b4bfa4d5be429d`;
the c0 delta fixes Python partition counting and adds its regression test.
The f2 live-flow evidence and c0 package evidence are kept separate for clear
attribution.

## Linux

The f2 Linux core suite passed with 3,564 passed tests, 196 skipped tests, and
283 passing subtests. On c0, the planner/checker suite passed 49 tests. The
standalone c0 partition check collected 3,925 tests: 3,761 core and 164
synthetic, with no overlap or missing tests.

The f2 packaged Linux client completed sequential streamed text → client tool
→ text turns in one runtime process. The client tool read an unpredictable
isolated marker, streamed responses were observed, and text/tool readiness
remained current throughout. This is f2 real-account/client evidence; c0 has
the same product source tree, but was not represented as a fresh live-client
run.

The c0 Linux portable package archive is
`CodexHub_0.2.29_linux_portable_c0d4840e.tar.gz`, SHA-256
`e58b0791488e4328c5248884322cc1a6ecd060b314af5eef33a2000ebd036a36`. Resource
inspection matched all 98 packaged Python/HTML source files, matched the
runtime hash, and found no private-state files. An isolated upgrade and start
preserved the existing account and settings; the saved settings were active,
restart was not pending, readiness was current for text and tools, and nine
models were available.

## Windows

The c0 Windows core suite passed: 3,693 passed, 68 skipped, and 283 passing
subtests in 435.45 seconds. The c0 synthetic watchdog passed all 164 tests in
1,296.22 seconds. Both commands exited successfully. Their isolated test area
had no leftover processes, and the synthetic watchdog left no temporary file.

The c0 Windows portable package is
`CodexHub_0.2.29_portable_c0d4840e.zip`, 83,801,037 bytes, SHA-256
`c51ea9126482a0d3f74d6077cf4fb22ca51fb49b15965699ef2691efb18130ec`. Packaged
resource inspection found all 98 expected Python/HTML source files and no
private-state files. Source contents matched exactly after normalizing CRLF
to LF; all 98 raw-byte differences were Windows checkout line endings. The
bundled runtime hash matched the pin.

The extracted package's Python settings adapter served the page; bootstrap,
session, and origin checks passed; legacy fixture settings could be read,
saved, and reopened; the saved secret was redacted from HTTP. This adapter
check did not start a runtime or establish text/tools readiness.

The packaged runtime lifecycle check passed all eight stages: legacy-state
read/migration, pinned runtime installation, first start, save-with-pending
restart, explicit stop/start applying saved settings, same-package upgrade
preserving settings and synthetic account/control markers, post-upgrade start,
and clean stop. Saving moved the new value to `saved` while `active` stayed on
the prior value; `pending_restart` became true and the runtime process stayed
unchanged. Explicit stop/start activated the saved value and cleared pending.
The same-package upgrade preserved saved and active settings plus the synthetic
account marker and legacy control-token digest, then required a restart. After
the post-upgrade start, the final stop removed the process and stop-request
records; an independent process check confirmed all six recorded PIDs were no
longer alive. The lifecycle used no credentials or real account: after
upgrade, `ready` remained false, login was unknown, and text/tools readiness
was false. No real Windows account login or upstream provider call is claimed.
During r1 startup, CodexHub replaced the undersized synthetic control token,
so the probe's preservation assertion failed. The r2 test fixture was
corrected and passed. No token value or hash is included here.

The four focused Windows rechecks were run on f2, not c0, after runtime
resource preparation and passed. An earlier f2 full-core run had four
failures; the final c0 full-core run passed as recorded above.

## Settings contract and review

Focused source-level coverage verifies that saving during an open stream keeps
the old active configuration, leaves restart pending, and does not queue a
restart; an explicit stop/start applies the saved settings. A concurrent
Gateway request to another provider survives a settings save. Public settings
tests cover local origin/host checks, bootstrap authorization, secret
redaction and keep/replace/clear behavior, and invalid-save/restart handling.
These checks do not imply that the synthetic Windows package authenticated a
real account.

The final Spec and Standards delta reviews reported no outstanding findings
for Windows fixture portability or partition counting. Earlier Windows
qualification exposed runtime defects: startup used a Unix socket where the
upstream Windows runtime requires a named pipe, process liveness used a
signaling API unsafe for Windows, and shutdown could leave an owned child
process. The branch changed to a named pipe, read-only Win32 process checks,
and graceful owned-process shutdown. Focused Yoga source probes and the
lifecycle fixture later passed with owned processes gone. Those earlier
source-level checks are distinct from the packaged c0 lifecycle results above.

## Release boundary

The issue-owned acceptance evidence is complete for this candidate. The
separate #567–#575 client gates remain open and have not been waived. No
application release or production installation has occurred.

Sanitized, machine-readable summaries:

- [`settings-acceptance-linux-f2f95fe1.json`](settings-acceptance-linux-f2f95fe1.json)
- [`settings-acceptance-linux-c0d4840e.json`](settings-acceptance-linux-c0d4840e.json)
- [`settings-acceptance-windows-c0d4840e.json`](settings-acceptance-windows-c0d4840e.json)

The summaries omit local paths, account and connector identifiers, process
IDs, secret material, token values and token digests, and raw transcripts.
Package and runtime hashes remain so the artifacts can be verified.
