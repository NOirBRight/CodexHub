# Post-manifest qualification, 2026-09-26/27

No release approval, merge, installation, or gate waiver. This continues #564
on this branch while the other session repairs the installed 0.2.26 line.

## Identities

Both portable packages contain product SHA
`631a5478b474d46ea78690767971642de80deda2` and version string `0.2.25`.
The AppX resolver fix `b5eea871` is included. Subsequent harness-only commits
`720dd5658ab7dd660ef9caf6c65ce8d928cb1161` and
`b7f7a0fbe818b6cc78973324fa458aa7b469770f` do not relabel these binaries.

| Archive | SHA256 |
| --- | --- |
| Linux debug portable | `c7ae5b7d44d9454ab851a865c84cd37bccedfad837f81bad02d6f5b750e51f4e` |
| Windows debug portable | `ba1981d61dc9a7a0c874aca82650616ebc61458eed0f9364bd807bd4f90f288e` |

Client versions remain those recorded in [the e0 evidence](../e0a2289b/README.md).
Linux Codex 0.157.1 / OpenCode 1.18.32 / Pi 0.87.1 / OMP 18.3.2;
Windows Codex 0.156.1 / OpenCode 1.18.32 / Pi 0.80.6 / OMP 17.0.3.
No client upgrade was performed.

## Confirmed results

| Gate | Result | Evidence |
| --- | --- | --- |
| Linux four CLI clients × Luna/DeepSeek Flash | Passed 8/8 | [Report](linux-cli.json), adjacent case artifacts |
| Linux independent inbound Chat | Passed 2/2 | [Report](linux-chat.json) |
| Windows independent inbound Chat | Passed 2/2 | [Report](windows-chat.json), explicit candidate proxy |
| Windows four CLI clients × Luna/DeepSeek Flash | Passed 8/8 | [Report](windows-cli/summary.json); actual binary 631a5478, harness b7f7a0fb |
| Windows synthetic contract | Passed 164/164 | [Local check record](local-checks.json); harness b7f7a0fb |

[Local check record](local-checks.json): Linux `verify-linux.sh` passed on 631a5478: Python 3,421 passed / 189 skipped /
283 subtests; Rust 792 passed / 1 ignored; clippy; frontend build and physical
pointer/DOM verification. Windows 631a5478 Rust passed 780 / 3 ignored,
clippy and debug-portable packaging exited 0. Prior e0 Python core evidence
remains attributed to e0 under the incremental verification policy.

## Windows blockers and bounded gate updates

1. **Manifest:** Desktop 26.917.6896.0 contains one visible application and a
   hidden command-runner helper. b5eea871 excludes manifest-hidden helpers and
   retains unique visible-app and safe executable-path checks. Its behavioral
   fixture and real installed-manifest resolution passed.
2. **Running operator Desktop:** after the manifest fix, the packaged
   `refresh-models` command correctly refuses the Official commit while Desktop
   is running. [Separate sanitized diagnostic](windows-refresh-diagnostic.json)
   records `codex_desktop_became_running_before_commit`, exit 1, unchanged source
   auth and no Official state publication. No Desktop was stopped or restarted.
3. **Explicit catalog:** the Windows CLI harness now accepts an isolated,
   unmodified Official snapshot, matching Linux's explicit `--catalog` input.
   [Catalog provenance](catalog-provenance.json) records its exact source and
   staged-copy hash. This is the same input used for the 631 Linux CLI and both
   independent Chat runs, fetched 2026-09-26T04:00:40.351663Z by client 0.2.24.
   It is input data, not a claim that 631 refreshed it. The per-run
   `official-catalog-input.json` must match SHA-256
   `c46ce534c0f316943d01c26c5deebc3d1651c943b29c2514df1853b5130c2ce0`.
   Invalid/missing/outside-isolation inputs fail closed; production
   materialization still resolves the exact model and context. All eight live
   route, tool, streaming, terminal, retry, and correlation checks remain.
   Snapshot mode does **not** qualify model refresh or override its protection.
4. **Production apply metadata:** OpenCode apply succeeds and includes
   `restart_required`; the older harness rejected the extra production field.
   b7f7a0fb accepts it through the existing bounded safe-string validator.
   Missing required fields, arbitrary new keys, and credential/path output
   remain rejected. The regression failed before the fix and passed afterward.
5. **Synthetic assertion:** the 0c1c28a1 full suite finished 158 passed / 1 failed.
   Its sole failure expected telemetry provider `official`; the versioned
   contract specifies `openai`. The assertion now reads the exact contract ID.
   Six focused snapshot/ID tests passed; the later restart-metadata regression
   also passed. The partial 720dd565 full run was intentionally stopped for the
   new materializer delta and is not a full-suite pass.

The source auth was renewed through the owner's completed dedicated login.
Windows upstream traffic uses the explicit SSH loopback proxy; clients retain
isolated configurations. No host credentials, installed Gateway, or operator
checkout was modified. [Source integrity checks](windows-source-integrity.json) pass.
Source catalog/auth fingerprints are checked separately
from the copied test runtimes. Only sanitized reports are committed.

## Remaining scope

The [e0 bounded Claude evidence](../e0a2289b/README.md) retains its original SHA:
native usage through the rendered Linux UI, explicit Opus resume on both OSes,
same-session native/external/native, tools, manual compact/resume, cache, real
upstream 401 and CLI cancellation 499. It is not relabeled as 631 evidence.
Inherited/pinned subagents, too-long recovery, current-version automatic
compaction and Windows rendered Usage UI remain unverified. The final combined
candidate with the other session still needs its own qualification; no release
is authorized by this evidence.

中文：Windows manifest 兼容性问题已修复；后续刷新被正在运行的 Desktop
保护机制阻止，未关闭 Desktop。CLI 门禁改为显式隔离目录快照输入，继续保留
全部八项真实调用标准，另记录刷新阻塞。测试脚本也已兼容生产返回的重启提示
字段。Linux 八项和双平台独立 Chat 已通过，Windows 八项与164项完整模拟门禁均已全部通过；本次不发布、安装或合并，未验证能力保持未验证。
