# Claude coexistence live candidate evidence (#564)

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
switching and the packaged Usage UI were not verified by this run. The final
code SHA has separate candidate evidence below.

## Packaged candidate `c97d0889`

The final code SHA is `c97d08897bdae3f56de6e3ce51235150356d8a43`.
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

The final code SHA also passed Linux Python (3260), Windows Python (3385),
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
