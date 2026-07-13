## Agent skills

### Issue tracker

Issues live in GitHub Issues via the `gh` CLI; external PRs are not a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical labels used as-is: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout — one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### CI and manual verification

Classify work as fast, standard, or strict and select local checks from `docs/agents/verification-policy.md`. GitHub Actions remains the final PR gate for `dev` and `main`; when unavailable, run the documented fallback commands in `docs/agents/ci.md`.

### User feedback

Persistent state changes use the shared Toast lifecycle and disclose exact restart requirements. See `docs/agents/user-feedback.md`.

### Report-only quality gates

Use `python scripts/report_quality_gates.py` for non-blocking dead-code and duplicate-name reports. See `docs/agents/report-only-quality-gates.md`.
