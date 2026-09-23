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
reviewed SHA still requires its own candidate evidence and platform checks.
