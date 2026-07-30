# Issue #61 verification record

Risk class: `strict` (Gateway routing/protocol policy).

Base: `origin/dev` at `ae927c687390017903435cd9bf4314610b6da229`.

## Architecture

- `route_plan_for_request` is the single pure seam that produces the frozen
  `RoutePlan` before request mutation or upstream I/O.
- The plan binds provider, requested/canonical/upstream model, authentication,
  inbound/configured protocol, immutable ordered protocol attempts and their
  body conversion/fallback/telemetry policy, optional route-qualified
  capability-manifest identity, Codex compatibility, tool exposure/evidence,
  collaboration backend, execution owner, streaming, retry, usage, Vision
  Proxy, transport, and named mutation policies.
- `Supported`, `Unsupported`, and `Unqualified` are typed capability states.
  Candidate native tool modes retain their evidence but resolve to current
  Gateway compatibility for third-party Codex App routes. They cannot enable
  passthrough or remove semantic repair.
- The RoutePlan schema identity is independent of optional manifest
  version/hash evidence; absent #250 metadata remains `Unqualified` rather
  than receiving a fabricated manifest identity.
- `_proxy_post_request` consumes plan-owned compact/raw/candidate injection,
  ordered Responses-to-Chat fallback attempts, and the effective Vision action
  and network plan. Recognized-but-unimplemented Anthropic routes fail
  `Unsupported`; unknown configured protocols fail `Unqualified`; both stop
  before upstream I/O.
  Existing request/response adapters, relay paths, retries, usage capture,
  Vision Proxy behavior, and official keepalive transport remain unchanged.
- Each immutable `RouteAttemptPlan` now owns its endpoint URL, actual ordered
  request-conversion steps, authentication/streaming/usage/transport/mutation
  policies, tool protocol choices, cross-protocol verification choice, and a
  frozen `RetryExecutionPlan`. Runtime settings are captured into
  `RouteRuntimeFacts` before planning; the handler/open/relay execution path
  consumes the attempt contract without re-reading routing or retry settings.
- Planned telemetry is explicitly scoped as a `planned_union`, while selected
  attempt telemetry reports the actual executed protocol, net wire adapter,
  conversion chain, and mutations. In particular, a Chat caller on an `auto`
  route truthfully records Chat-to-Responses for attempt 0 and
  Chat-to-Responses followed by Responses-to-Chat for the fallback attempt.
- Caller-tool stripping is a named `caller_tool_stripping` mutation. Optional
  capability-manifest state now fails closed unless version and cryptographic
  hash form a complete valid pair; only a valid pair with an explicit
  `Supported` state is reported as supported.

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

- Third-review affected delta:
  `python -m pytest -q tests/test_routing.py tests/test_chat_completions_gateway.py tests/test_proxy_event_logging.py`
  — 701 passed, 226 subtests passed in 44.98 seconds with Python 3.13.
  Coverage includes the attempt execution contract, actual Chat `auto` 404
  fallback bodies/telemetry, manifest identity pairs, caller-tool stripping,
  and handler-level Vision Proxy pass/proxy/reject/failure I/O behavior.
- `python -m pytest -q --ignore=tests/test_real_client_e2e.py`
  — 1,477 passed, 1 skipped, 451 subtests passed in 100.23 seconds with Python
  3.13 first on `PATH` so nested PowerShell replay subprocesses use the
  repository-required interpreter. An earlier run from the Python 3.13 parent
  process but with ambient Python 3.11 first on `PATH` produced only two
  issue-108 replay-smoke failures because the nested interpreter rejected
  Python 3.13 syntax (1,475 passed, 1 skipped); that environment result is not
  the candidate gate. Per the verification policy, the later repair delta
  reran affected tests rather than duplicating the complete core suite.
- A literal `python -m pytest` was attempted because the issue text names it,
  then terminated without a test result when the current verification policy
  guardrail identified the separately governed synthetic real-client
  partition. None of that partition's listed contract-surface paths changed,
  so it is not applicable to this PR.
- `python -m py_compile src-python/codex_proxy.py tests/test_routing.py` —
  passed.
- `git diff --check` — passed (Git emitted only the repository's CRLF
  normalization warning).
- `python scripts/report_quality_gates.py --json` — report-only, exit 0:
  2 unused imports, 83 dead functions, 146 duplicate names, 0 parse errors.
  No new RoutePlan finding remains; baseline findings were not changed or
  allowlisted.

No live-provider or manual Desktop evidence is required by issue #61.
