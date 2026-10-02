# Subscription Provider delta review

PR #614. The final independent review covered `19c4ad01...e9449930` and
`e9449930...2f2e6e84`, following the earlier campaign baseline review and its
fixes. The second range contains tests and sanitized evidence only. Later
commits record engineering results and primary-source research; they do not
change production source. Reviewers performed read-only inspection and did
not run tests, vendor probes or edits.

## Standards

No new actionable Standards findings. Cursor transport failures expose fixed
stage codes/messages and preserve cancellation/deadline precedence without
exception, credential, peer or request-data leakage. Pi cleanup accumulation
is equivalent. Official compatibility uses the existing request-scoped
Collaboration alias/inverse; public `execute_exchange` tests cover preparation,
context propagation, SSE and separate Call/Item identity. Raw-probe and
unrelated inverse paths retain their behavior.

The final declaration test preserves the namespace, complete schema, all six
context handlers and absence of flattened aliases. Evidence binds successful
probes to exact source identity, records bounds and cleanup, retains original
failures and separates other cases' candidate snapshots.

## Spec

No new actionable production Spec regression. The actual `codex_exec` route
now performs source prevention, preserving declared handler subsets,
Call/Item IDs and completed results. It does not restore unsafe automatic
opaque-task archiving or infer missing requirements. Fixed transport errors
and revised cipher tests retain bounded failure and plaintext positive controls.
Scoped Responses tool/result/restart and Official → Cursor V2/followup/restart
evidence does not imply complete qualification.

The optional compatibility helper can alias with no retained context, matching
the existing passthrough helper assumption. Production `execute_exchange`
always supplies a request-context dictionary. Independently supporting that
optional helper would require retained inverse context before aliasing; this
was not classified as a new production regression.

Claude permission, Messages output budgets, Windows qualification,
authoritative assignment recovery and unrelated historical ciphertext handling
remain disclosed gaps. The reviewed delta was ready for local verification
and bounded probes; the subsequent results are in
[the Linux verification record](subscription-linux-verification-2026-10-01.md).

Final actionable findings: **Standards 0; Spec 0**. Neither axis establishes
complete Provider or release acceptance.
