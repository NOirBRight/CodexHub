# Review and Linux portable acceptance

Review baselines: CodexHub `cc1b36b3`; upstream `a13cd099`.
Scope is the two reported repairs plus their Linux test packaging; strict verification.

## Standards

Final parallel reviewer result: 0 remaining findings. Generated resource declarations
now drive packaging, so cached Cargo output cannot introduce stale source resources.

## Spec

Final parallel reviewer result: 0 remaining source/spec findings. Resolved findings:
- Reject contradictory or malformed version evidence across the whole announcement.
- Reuse the existing login in the launcher's private local state directory.
- Update the packaging contract check after switching to declared source resources.

## Artifact and provenance

- Portable build source: `683e5b4a` (includes all production changes).
- Review/test-only follow-up: `5623ecf8`; no packaged source changed after the build.
- Upstream runtime: `81b270efbd561835c1d4b78e30bb488944e0c790`.
- Archive: `CodexHub_0.2.27_linux_portable_683e5b4a.tar.gz`.
- Archive SHA256: `8500ab5ef6ec03484957788baa3b9cdbeba72f48b8a58242b94a86c6f78d4403`.
- Runtime SHA256: `4c5fc30299820e078a9df4d1dc6bef725855d29c0981770335d421e1bd70f075`.

Final archive inventory matches source Python and script files, excludes caches,
stale helper scripts and credentials, and contains the exact live-tested runtime.
The supervised runtime was installed from the bundled artifact in the preceding
portable build; its runtime and Python source are byte-identical in this final package.
Both Pro and Sol completed actual website turns with `ACCEPT7K3`.
See [sanitized results](model-selection-repair.json) and [verification](model-selection-repair.md).

## Test instructions

Close existing CodexHub, extract the archive and run `./Start-ChatGPT-Test.sh`.
Existing login is already available on this workstation under
`~/.local/state/codexhub-portable-test/chatgpt-web` (outside the archive).
Host Python 3.13+ and Chromium are required. This is the 0.2.27 acceptance branch,
not the unrelated newer development workspace. No Windows or full Gateway/client
release qualification is claimed.

Final count: Standards 0; Spec 0. No remaining finding within either axis.
