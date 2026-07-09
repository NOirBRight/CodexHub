## Agent skills

### Issue tracker

Issues live in GitHub Issues via the `gh` CLI; external PRs are not a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical labels used as-is: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout — one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### CI and manual verification

Use GitHub Actions PR checks for `dev` and `main`; when unavailable, run the documented fallback commands. See `docs/agents/ci.md`.
