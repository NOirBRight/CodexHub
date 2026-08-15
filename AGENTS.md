## Agent skills

### Issue tracker

Issues live in GitHub Issues via the `gh` CLI; external PRs are not a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical labels used as-is: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout — one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### CI and manual verification

Classify work as fast, standard, or strict and select local checks from `docs/agents/verification-policy.md`. GitHub Actions remains the final PR gate for `dev` and `main`; when unavailable, run the documented fallback commands in `docs/agents/ci.md`.

### Python runtime

CodexHub requires Python 3.13 or newer. The interactive Codex environment may
put a separate Python 3.11 virtualenv first on `PATH`, so never invoke bare
`python`, `python3`, `py`, or `pytest` for repository checks. Use the repository
launcher instead:

```powershell
.\scripts\codexhub-python.cmd -m pytest -q
.\scripts\codexhub-python.cmd path\to\script.py
```

The launcher validates one compatible interpreter, exports it to child
processes, and places that interpreter plus the repository `scripts` directory
first on the child `PATH`. A 3.11 error is an invocation error: do not retry
the same command with another bare Python name. Rust, packaged Gateway, and
real-client E2E entrypoints resolve or inherit the same contract independently.
Direct utilities under `src-python/`, `scripts/`, and the checked-in evidence
validators under `tests/` also run the canonical preflight in
`src-python/python_runtime_contract.py`; a direct ambient-3.11 launch must
fail closed before importing or mutating production code.
E2E fixture Python files use the same 3.13 floor and require the exact
`CODEXHUB_E2E_PYTHON` binding; the fixture launcher never falls back to a
copied runtime or ambient `PATH`.
Do not rely on a previous activation command: automated shell invocations may
start a fresh process, so use the repository launcher in every command that
needs Python.
For an interactive PowerShell session, dot-source
`.\scripts\Enter-CodexHubPython.ps1` once before using bare commands.

### User feedback

Persistent state changes use the shared Toast lifecycle and disclose exact restart requirements. See `docs/agents/user-feedback.md`.

### Report-only quality gates

Use `.\scripts\codexhub-python.cmd scripts/report_quality_gates.py` for non-blocking dead-code and duplicate-name reports. See `docs/agents/report-only-quality-gates.md`.
