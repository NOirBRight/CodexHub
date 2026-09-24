# Claude coexistence live candidate evidence (#564)

## Reviewed candidate `fae658a2`

The reviewed **code** SHA is `fae658a2c523ec8431ba0959bf4da22756903bb3`.
The isolated Linux debug portable archive is
`CodexHub_0.2.24_debug_linux_portable_fae658a2.tar.gz` (SHA256
`d05adf8f57052424d73352bb99c320a5edb5fdf67dcf6b30377d258a8553b416`).
The Windows archive built from the same SHA on `yoga` in
`D:\\Workstation\\CodexHub-claude-review-fae658a2` is
`CodexHub_0.2.24_debug_portable_fae658a2.zip` (SHA256
`afd0d2007de3be2761c2e6abfa4a1c7dce814ee7472190d6e66f60c848f03fc3`).
Both packages are staged under `/tmp/codexhub-claude-review-fae658a2/` on
Linux. Neither package was installed. `D:\\Workstation\\CodexHub` remained on
`main`; the active Linux application, Gateway and conversations were untouched.
At this code SHA, Linux Python passed 3,260 tests; Windows Python passed 3,385
core and 152 synthetic real-client tests; Windows Rust passed 774 tests.
Linux Rust/clippy, Windows clippy, frontend build/UI contracts and the real
Linux window pointer check passed. The report-only quality scan had no parse
errors; its advisory findings did not gate this candidate.

- [Model/family and picker qualification](picker-family-fae658a2.json): Claude
  Code 2.1.280 used an isolated network namespace and bounded loopback server.
  Mapped Opus/Sonnet/Haiku/Fable, explicit native Opus 5.5, plain versus
  explicit-model resume, `opusplan`, local credential rejection and the native
  plus Gateway `/model` rows passed. Tool execution was disabled, so inherited
  and pinned subagent selection is **unknown** in this probe.
- [Combined settings flow](combined-settings-fae658a2.json): the packaged
  binary, isolated web bridge, real Chromium UI and Claude Code 2.1.280 passed
  preview, Connect, family/default edits, readback, invalid-target/conflict
  handling, loopback external-default generation and Disconnect. This fixture
  used a synthetic saved OAuth token and made no official Provider call. Its
  initial run without a saved-login fixture failed client model recognition;
  the corrected fixture passed without changing product code.
- [Real native Opus 5.5 resume](explicit-model-resume-fae658a2.json): creation
  and `--resume --model claude-opus-5-5` both reached the native subscription
  route with the exact ID and persisted upstream usage.
- [Real native Haiku and rendered Usage page](native-haiku-usage-ui-fae658a2.json)
  ([screenshot](native-haiku-usage-ui-fae658a2.png)): one bounded call reached
  the subscription, persisted 29,484 input, 116 output, 19,436 cache-read and
  10,038 cache-write tokens, and appeared in the packaged Usage details.
- [Official DeepSeek and Codex live routes](live-providers-fae658a2.json):
  DeepSeek Flash Messages/Chat, Codex Luna Responses (max effort), and the
  official DeepSeek balance query passed with canonical Provider/model identity
  and persisted usage. This run used Claude Code 2.1.281; the other real-client
  probes here used 2.1.280.
- [Interactive native → DeepSeek → native session](interactive-native-external-native-fae658a2.json)
  and [Gateway/usage rows](interactive-routes-fae658a2.json): one Claude Code
  2.1.280 terminal process selected each model with `/model`, recalled the same
  synthetic marker across all three assistant replies and recorded subscription,
  DeepSeek and subscription usage respectively.
- [Automatic compaction](auto-compaction-fae658a2.json) and
  [Gateway/usage rows](auto-compaction-gateway-fae658a2.json): the public
  `--autocompact 100k` option generated a `compact_boundary(trigger=auto)` at
  175,032 pre-compaction tokens and 2,478 post-compaction tokens. Five native
  Gateway requests had persisted usage; the following reply recalled the marker.
- [Manual compaction](manual-compaction-fae658a2.json) and
  [Gateway/usage rows](manual-compaction-gateway-fae658a2.json): `/compact`
  generated `compact_boundary(trigger=manual)` at 3,892 pre-compaction and 727
  post-compaction tokens. Its native Haiku summary request persisted 5,214
  input and 839 output tokens; the next reply recalled the marker. A first
  diagnostic failed because the test capped output at 128 tokens, below the
  summary's need. The passing probe used a bounded 2,048-token cap.

The real-client tests used temporary home/config/history/runtime, independent
ports, access-token-only Claude credential snapshots (no refresh token), and
bounded requests, output and wall time recorded in each JSON. The interactive
switch allowed three generation turns, 128 output tokens and 360 seconds. The
automatic compaction run allowed four CLI invocations, 2,048 output tokens per
request, at most 650,000 prompt characters per invocation and 600 seconds.
Too-long recovery, pinned/inherited subagents, live tool/cancellation/error
paths, real cache-hit reuse, every native subscription version and Windows
live subscription traffic are **not yet verified** on this candidate.
Deterministic token/accounting edge cases are covered by the local test suites,
not by these live records. Historical evidence below describes earlier
builds and must not be read as evidence for `fae658a2`.

`explicit-model-resume-a5b783cd.json` is a sanitized, bounded qualification
run on Linux with packaged candidate `a5b783cdcb53a7041e270c324f204ce5adb27c47`
and Claude Code 2.1.280. A temporary home, isolated Gateway port and a
subscription credential snapshot without refresh token were used; the source
credential file and installed app were not changed. The runner reserved two
Claude Code generation invocations, limited each to 90 seconds and output to
128 tokens, and set a 300-second overall deadline.

The initial `--model claude-opus-5-5` request and the later
`--resume <session-id> --model claude-opus-5-5` request both reached the native
subscription route with the original model identity and persisted complete
usage rows. The resumed request recorded 23,055 input tokens, 36 output tokens,
22,730 cache-read tokens and 323 cache-write tokens. `explicit_model_resume`
is verified for this candidate; plain `--resume`, same-session native/external
switching and the packaged Usage UI were not verified by this run. The reviewed
`fae658a2` candidate has separate evidence above.

## Earlier packaged candidate `c97d0889`

This earlier code SHA is `c97d08897bdae3f56de6e3ce51235150356d8a43`.
The Linux debug portable archive SHA256 is
`72050ed47656ae556202a35ef3a9e6bd102de51803f5f9b23aeb9de2315321de`;
the isolated Windows debug portable archive SHA256 is
`d8ddaec8eff14959f565bb7e19f7a47b62fdebea64613c44d7b65b552c794216`.
Neither replaced the installed app.

- [Real subscription Opus explicit resume](explicit-model-resume-c97d0889.json):
  Claude Code 2.1.280 created and explicitly resumed one session with
  `claude-opus-5-5`. Both Gateway requests retained that complete ID, returned
  HTTP 200 and persisted upstream token/cache counts. Plain `--resume` was
  previously observed to follow the current default/family mapping; it is not
  an identity-preserving recovery command.
- [Real subscription Haiku and packaged Usage page](native-haiku-usage-ui-c97d0889.json)
  ([screenshot](native-haiku-usage-ui-c97d0889.png)): one bounded Claude Code
  generation invocation used native `claude-haiku-4-5-20251001`; Gateway,
  SQLite and the rendered packaged Usage details all showed the subscription
  request and upstream token/cache counts. The UI ran with independent D-Bus,
  Xvfb, temporary HOME/runtime and ports. The first 640×480 virtual-display
  attempt could not show the details row; the 1280×940 rerun passed.

Both live runs used an access-token-only subscription snapshot, excluded the
refresh token, capped output at 128 tokens per request and verified source
credentials were unchanged. The Opus run reserved two CLI generation attempts,
90 seconds each and 300 seconds overall. The Haiku run reserved one CLI
generation attempt, 90 seconds and 180 seconds overall; Claude Code itself can
send more than one Gateway request during an invocation.

This earlier candidate also passed Linux Python (3260), Windows Python (3385),
Windows synthetic real-client contract (152), Windows serial Rust (774),
Windows clippy, frontend build and UI contract checks. Earlier DeepSeek
official Messages/Chat, balance and Codex Luna Responses results were obtained
on precursor `154af624`, not repeated on `c97d0889`.

## Follow-up interactive qualification on the same code SHA

[Interactive evidence](interactive-native-external-native-c97d0889.json) and
[Gateway/usage evidence](interactive-routes-c97d0889.json) used Claude Code
2.1.280 in one isolated terminal process. The user-visible `/model` command
selected native Haiku → official DeepSeek Flash → native Haiku. Three assistant
replies in the isolated session transcript recalled the same synthetic marker;
the corresponding Gateway requests were HTTP 200 and persisted under
`claude_subscription`, `deepseek`, then `claude_subscription`. The runner
reserved three generation turns, a 128-token output cap and a 360-second
interactive deadline. Claude Code's interactive model selection changed only
the temporary settings file; the fixture was restored before the outer
settings-integrity check. Source account data remained unchanged.

[Automatic-compaction evidence](auto-compaction-c97d0889.json) and
[Gateway/usage evidence](auto-compaction-gateway-c97d0889.json) now **pass**
on the same packaged code SHA and Claude Code 2.1.280. Four print-mode
invocations used one isolated session, explicit native Haiku selection and the
public `--autocompact 100k` option. The third invocation produced a
`compact_boundary` with `trigger=auto`, `preTokens=175032` and
`postTokens=2478`; its two Gateway requests and all five requests in the
run returned HTTP 200 with complete persisted upstream usage. A subsequent
reply recalled the original synthetic marker. Bounds were four CLI
invocations, at most 650,000 prompt characters per invocation, 2,048 output
tokens per request, 120 seconds per invocation and 600 seconds overall.
Credentials were snapshotted without the refresh token; source files and the
installed app were untouched.

The earlier [small-context probe](auto-compaction-not-triggered-c97d0889.json)
is retained as a failed diagnostic, not a product failure. Its approximately
10k-token context was well below the real trigger observed above; the private
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` did not establish a passing automatic
boundary. The first attempt also used a 128-token cap that interrupted an
internal reply. The follow-up uses the public CLI option and asserts the
actual client boundary. Every subscription version and Windows live
subscription traffic remain unverified.
