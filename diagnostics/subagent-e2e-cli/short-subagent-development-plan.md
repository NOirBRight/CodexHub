# Short Subagent Development E2E Plan

## Task 1: Write The Diagnostic Artifact

Use a fresh implementer subagent to create exactly one UTF-8 text artifact at
`OUTPUT_PATH`. The artifact must contain the exact sentinel line from
`SENTINEL`, plus the model, endpoint, and case values supplied by the
coordinator. It must end with a trailing newline.

The intended minimal artifact shape is:

```text
case: <CASE>
model: <MODEL_UNDER_TEST>
endpoint: <ENDPOINT_UNDER_TEST>
<SENTINEL>
artifact: ok
```

After the implementer reports DONE, run a spec reviewer subagent. The spec
reviewer verifies that `OUTPUT_PATH` exists, is UTF-8 text, contains the exact
sentinel line, and has no missing required fields.

After the spec reviewer reports PASS, run a code-quality reviewer subagent. The
code-quality reviewer verifies that the implementation is minimal: no product
source files changed, no extra implementer-owned files were created, and
runner-owned diagnostics scaffolding is ignored.

If a reviewer reports FAIL or BLOCKED, send a focused fix back through an
implementer subagent, then re-run the same reviewer before proceeding.

Final coordinator response must be exactly three lines:

```text
RESULT: PASS|FAIL
SENTINEL: <SENTINEL>
SUBAGENT_CHAIN: implementer,spec-reviewer,quality-reviewer
```
