# Claude Code 2.1.280 coexistence qualification

Date: 2026-09-23. Candidate checkout: `313deb69`; code baseline: `a5ec2c57`.
Raw sanitized observations: [claude-coexistence-qualification.json](claude-coexistence-qualification.json).

The bounded run used the real Claude subscription access token only in a
temporary Claude home. The refresh token was omitted. A loopback Messages mock
returned two synthetic output tokens per success. No live Provider was called.
The CLI attempted 33 CONNECTs to `api.anthropic.com` and
`platform.claude.com`; the loopback proxy returned 502 and an isolated network
namespace prevented external traffic. The host credential file hash was
unchanged.

| Behavior | Result |
| --- | --- |
| Mapped `opus`, `sonnet`, `haiku`, `fable` aliases | Pass; each sent its configured Gateway ID |
| Unmapped `haiku` | Pass; sent `claude-haiku-4-5-20251001` |
| Explicit `claude-opus-5-5` | Pass; full ID preserved, CLI requested 128,000 max output tokens |
| Configured `opus` default | Pass; resolved through the family mapping |
| `opusplan` Plan / execution phases | Pass; mapped Opus / Sonnet targets respectively |
| OAuth plus local Gateway header | Pass; the same request carried the saved OAuth bearer, OAuth beta, and `x-codexhub-gateway-key`; no `x-api-key` was set |
| Missing or wrong local header | Pass; both requests returned 401 despite valid OAuth |
| Gateway model discovery with subscription OAuth only | Pass; no `/v1/models` request, even when discovery was enabled |
| Managed `modelPicker` row rendering | Unknown; bounded PTY run did not expose the picker rows. Official settings docs say appended `modelPicker.options` can include gateway IDs while preserving built-ins; client rendering still needs qualification |
| Resumed print-mode session after `--model claude-opus-5-5` | Potential contradiction; resume selected configured `codexhub/opus`. This seed used a print-mode flag, so it does not establish whether a native model chosen interactively with `/model` is persisted across resume |
| Inherited and explicitly pinned subagent selection | Unknown; tools were disabled in this HTTP harness |
| OAuth stripping before an external Provider | Not exercised; this is a Gateway routing test for #561 |

One product integration change is already clear: Claude Code can carry a local
credential in `ANTHROPIC_CUSTOM_HEADERS` without replacing subscription OAuth,
but the current Gateway authorization seam accepts only `Authorization`.
The Gateway must accept the separate local header and consume it before either
native forwarding or external Provider dispatch. Subscription OAuth must remain
confined to the official Anthropic route.

Model discovery cannot populate the external picker in this mode: Claude Code
does not use saved OAuth or custom-only headers for `/v1/models`. Keep discovery
off and publish the Gateway Client Projection through managed
`modelPicker.options` with `replaceBuiltInOptions` unset or false. That keeps
native rows by documented contract without installing a second user-editable
catalog. The exact rendered rows remain unknown from this run.

To reproduce, run from the qualification worktree. The output directory is the
only writable host path exposed to the sandbox; all other host files are
read-only and the CLI process has a separate network namespace:

```bash
ROOT="$(git rev-parse --show-toplevel)"
HOST_NETNS="$(readlink /proc/self/ns/net)"
bwrap --unshare-net --ro-bind / / --dev-bind /dev /dev --proc /proc --tmpfs /tmp \
  --bind "$ROOT/docs/evidence/issue-560" "$ROOT/docs/evidence/issue-560" \
  --chdir "$ROOT" --clearenv --setenv PATH /usr/bin:/bin \
  --setenv LANG C.UTF-8 \
  --setenv CODEXHUB_PYTHON /home/noirbright/Workstation/CodexHub/.venv-ci/bin/python \
  --setenv CODEXHUB_HOST_NETNS_ID "$HOST_NETNS" \
  "$ROOT/scripts/codexhub-python.sh" \
  scripts/e2e_claude_coexistence_qualification.py \
  --claude-bin /home/noirbright/.local/share/mise/installs/claude/2.1.280/claude \
  --oauth-credentials "$HOME/.claude/.credentials.json" \
  --out docs/evidence/issue-560/claude-coexistence-qualification.json
```

The harness refuses an access token that will expire before its bounded run;
it never copies or uses the refresh token.
