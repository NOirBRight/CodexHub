# Desktop tool compatibility matrix

The 0.2.2 Grok failure in task `01a083c5-9352-7fd3-bcdb-314bd77e7316`
was caused by `mcp__codex_app.automation_update`, whose stable upstream alias
is `__codexhub_ns_fa0cf542e6_1`. Its root `oneOf` contains further `oneOf`
branches. xAI rejected that structure even after every branch received
`type: object`. Moving the root union into an object root's `allOf` preserved
the constraints and made the actual schema acceptable. Flattening `oneOf`
would not preserve exclusivity in general.

The earlier CLI matrix covered Luna and Muse Spark, while the separate xAI
probe used simplified schemas. Neither established acceptance of the full
Desktop catalog. The automation schema captured from Desktop on 2026-09-09
is retained in `tests/fixtures/tool_schemas/codex_app_automation_update.json`.

## Capture and run

Run capture from a Linux Codex Desktop environment with
`CODEX_APP_TOOLS_PIPE_PATH` available. Supply the actual Desktop CLI binary
and the desired configuration home explicitly:

```bash
./scripts/codexhub-python.sh scripts/capture_desktop_tool_catalog.py \
  --codex /usr/lib/chatgpt/resources/codex \
  --source-home "$HOME/.codex" \
  --output /tmp/desktop-tools.json

./scripts/codexhub-python.sh scripts/e2e_desktop_tool_matrix.py \
  --live --source-home "$HOME/.codex" \
  --tools /tmp/desktop-tools.json \
  --output /tmp/desktop-tool-matrix.json
```

Capture uses the installed runtime, an isolated ordinary session (ephemeral
sessions omit Goal tools), and a loopback endpoint. It adds the current
Desktop tools/list catalog to the runtime's declarations. The output contains
tool definitions, not conversation content or credentials. Tool descriptions
can contain machine-specific configuration; inspect before sharing the raw
capture. Temporary credential copies are deleted when each runner exits.

The matrix starts the checkout Gateway on an isolated loopback port. It tests
`gpt-5.6-luna`, `xai/grok-4.6`, and
`opencode-go/muse-spark-1.3-contributor`. Every request carries the captured
catalog, plus a single controlled read tool. The model must call that tool
exactly once, receive a random value read from a temporary file, and reproduce
the value in its final response. Unexpected calls fail without execution.

The accepted 2026-09-09 capture contained 282 adapted aliases and 11 native
function tools, matching the incident's 293 upstream functions. There is also
a web-search declaration. The matrix adds one read probe, making 294 upstream
functions on the adapted third-party routes. Both calls for every model
returned HTTP 200 and completed successfully.

This proves whole-catalog schema acceptance and a controlled tool roundtrip
through the Gateway. It does not claim execution coverage of all 293 tools,
Desktop UI behavior, or subagent lifecycle behavior. It does not replace the
separate real-client and collaboration lifecycle suites.

Capture again after runtime/plugin changes; an old fixture is not evidence
that newly installed tools work. Keep the catalog hash and sanitized matrix
result with the verification report. Do not infer one provider's result from
another provider's success.
