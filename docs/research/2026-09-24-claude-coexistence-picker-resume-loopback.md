# Claude Code picker and resume identity: isolated loopback observation

Date: 2026-09-24. Claude Code: 2.1.280. Source checkout: `a1b3b6ae758a645143472862f2caa198287d53a7`.

This bounded diagnostic used Claude Code's real interactive `/model` picker,
`--bare`, a fresh temporary home, a synthetic `apiKeyHelper`, and a loopback
Messages mock inside `bwrap --unshare-net`. No real Claude subscription
credentials or Provider were used. Proxy-observed attempts to
`downloads.claude.ai` and `github.com` returned HTTP 502. This checks client
model-selection behavior only; it does not establish subscription OAuth,
Gateway authentication, billing, or live-provider behavior.

## Observations

| Case | Observation |
| --- | --- |
| Picker contents | The actual picker displayed the external model row and retained the explicit `Claude Opus 5.5` row alongside native models. |
| Family alias | With `ANTHROPIC_DEFAULT_OPUS_MODEL=codexhub/opus`, invoking `--model opus` sent `codexhub/opus` to the mock. |
| Explicit full ID | Invoking `--model claude-opus-5-5` sent `claude-opus-5-5` unchanged. |
| Session-only picker selection | Selecting `Claude Opus 5.5` in `/model` with `s` (session only) sent `claude-opus-5-5` to the mock. |
| Resume after picker selection | After sending a request with the picker-selected native Opus 5.5, resuming that session without a model override sent `codexhub/opus` under the same family mapping. |

The resume result conflicts with #559's intended behavior that a manually
selected full native ID remains native when an existing conversation resumes.
It is a concrete contradiction to resolve before claiming that acceptance
case passes. The diagnostic used `--bare` and synthetic API authentication,
not Claude subscription OAuth, so it does not establish whether OAuth-backed
sessions behave the same way.

Two identical native Messages requests were observed during the picker
selection run; their cause is unknown. The complete native → external → native
switch sequence was not verified. Treat both as open observations, not passes.

## Scope

This result extends the earlier print-mode resume observation recorded in
[`docs/evidence/issue-560/README.md`](../evidence/issue-560/README.md): an
interactive picker selection also resumed through the configured family alias
in this synthetic-auth setup. It does not replace the OAuth qualification or
the live candidate acceptance matrix in
[`2026-09-23-claude-coexistence-implementation-plan.md`](2026-09-23-claude-coexistence-implementation-plan.md).
