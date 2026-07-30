# Issue #61 verification record

Risk class: `strict` (Gateway routing/protocol policy).

Base: `origin/dev` at `ae927c687390017903435cd9bf4314610b6da229`.

## Architecture

- `route_plan_for_request` is the single pure seam that produces the frozen
  `RoutePlan` before request mutation or upstream I/O.
- The plan binds provider, requested/canonical/upstream model, authentication,
  inbound/configured/selected protocol, capability-manifest version, Codex
  compatibility, tool exposure/evidence, collaboration backend, execution
  owner, streaming, retry, usage, Vision Proxy, transport, and named mutation
  policies.
- `Supported`, `Unsupported`, and `Unqualified` are typed capability states.
  Candidate native tool modes retain their evidence but resolve to current
  Gateway compatibility for third-party Codex App routes. They cannot enable
  passthrough or remove semantic repair.
- `_proxy_post_request` consumes the plan's final route flags and policies.
  Existing request/response adapters, relay paths, retries, usage capture,
  Vision Proxy behavior, and official keepalive transport remain unchanged.

## TDD and review

- Initial focused RED:
  `python -m pytest -q tests/test_routing.py -k "route_plan"` — 3 failed
  because the `RoutePlan` seam and typed policy enums did not exist.
- Focused GREEN after implementation:
  `python -m pytest -q tests/test_routing.py -k "route_plan"` — 8 passed,
  559 deselected, 8 subtests passed.
- Local Standards/Spec self-review against issue #61 and the repository rules
  removed the legacy `RouteDecision` middle-man so there is one planning
  interface. The report-only scan then identified the obsolete
  `vision_proxy_policy_for_route` helper; it was removed before publication.

## Candidate verification

- `python -m pytest tests/test_routing.py tests/test_chat_completions_gateway.py tests/test_proxy_event_logging.py`
  — 684 passed in 51.39 seconds after the final review fix.
- `python -m pytest -q --ignore=tests/test_real_client_e2e.py`
  — 1,461 passed, 1 skipped, 425 subtests passed in 101.52 seconds.
- A literal `python -m pytest` was attempted because the issue text names it,
  then terminated without a test result when the current verification policy
  guardrail identified the separately governed synthetic real-client
  partition. None of that partition's listed contract-surface paths changed,
  so it is not applicable to this PR.
- `git diff --check` — passed (Git emitted only the repository's CRLF
  normalization warning).
- `python scripts/report_quality_gates.py --json` — report-only, exit 0:
  2 unused imports, 83 dead functions, 146 duplicate names, 0 parse errors.
  No new RoutePlan finding remains; baseline findings were not changed or
  allowlisted.

No live-provider or manual Desktop evidence is required by issue #61.
