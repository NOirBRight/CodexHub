# Cipher-task test contract review

Reviewed against campaign `b89f7c87`, following the Linux core run at `19c4ad01`
reported as 5 failed, 3859 passed, 201 skipped and 283 subtests passed.
This review changes tests and the generated matrix artifact only.

Four failures encoded the superseded lossy behavior: the #283 C1 native fixture
and three third-party tests expected encrypted assignment parts to disappear,
sometimes behind a placeholder, while the request continued. A readable fragment
such as `done` or `inspect the tray` cannot prove that the encrypted portion is
irrelevant or that complete requirements survived. These tests now exercise the
public compatibility seam and require the reviewed bounded
`encrypted_agent_message_unavailable` failure, an unchanged caller payload and
no ciphertext in the error. The compaction case has the same task-content
constraint. Separate plaintext controls prove exact native call/result/routing
retention and complete readable directed-message delivery through the existing
envelope. No automatic archival, restoration or task-completeness heuristic was
introduced.

The previous fourth third-party test used an object for Responses `input`,
outside the supported string/list input contract. Its compatibility-only lossy
rewrite was not evidence of a valid task-preserving exchange. The replacement
test verifies that public protocol preparation rejects that malformed shape.
This does not qualify legacy object-input rewriting as task recovery.

The fifth failure was deterministic matrix drift. Running
`scripts/build_issue_66_chat_conversion_matrix.py` regenerated the artifact;
the only change is the `protocol_translation.py` source SHA-256 from
`b73bcdcf7ebe285a741da1fc1d75df4fb96eb1fc4563035b7e0f5e74aeee2d3e` to
`64ad3c302fb4a9821bcb717c4f29538353b67f46ca9d0092520ac7f46de22c74`.
All 51 rows, dispositions and invariants remain unchanged. The `--check` test
reconciles the freshly generated artifact; no expected behavior was relaxed.

The affected tests plus current Code Mode/V2 and entry/seam/module gates pass:
**124 passed** using the canonical Python launcher and worktree-local TMPDIR.
The campaign owns the next full-suite run; this is not a replacement full-suite
claim or live Provider qualification.

Under [#605](https://github.com/NOirBRight/CodexHub/issues/605), distinguishing
unrelated old ciphertext from an unavailable current task remains **partial**.
Production observed-assignment recovery remains **unimplemented** without a
trusted observer and exact author/recipient/message/source-Call/Item association.
The existing recovery helper is test-only. Bounded failure is a safety constraint,
not a passing acceptance criterion for mixed-provider sealed-task recovery.
