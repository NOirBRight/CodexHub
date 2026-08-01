# Issue #61 verification record

Risk class: `strict` (Gateway routing/protocol policy).

Base: `origin/dev` at `42b0c375d97bad025a8deed07c6a72068df2b97c`.

Implementation candidate (code-only) head: `2e578bc10abf26084facc6b8009205ffd85801cb`.
The later commits on this PR refresh this evidence record only; they do not
change the implementation or test files.

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
- Production planning is pure and emits unmaterialized attempt headers. After
  all typed fail-closed decisions prove the route executable, the handler
  materializes authentication exactly once and binds one complete outbound
  `FrozenRequestHeaders` snapshot across the immutable attempt plan. Header
  values use redacted, deeply immutable wrappers excluded from representation,
  equality, telemetry, and event evidence; they are expanded only when the
  HTTP `Request` is constructed. Provider/config/auth changes after binding
  cannot affect the active request, while the next request takes a fresh
  snapshot.
- Success, streaming, and final `HTTPError` responses all consume the same
  required `RelayExecutionPlan`. The relay no longer accepts profile strings
  or optional policy arguments from which it could re-derive streaming, usage,
  response/SSE mutation, verification, lifecycle, or request-kind behavior.
- Planned telemetry is explicitly scoped as a `planned_union`, while selected
  attempt telemetry reports the actual executed protocol, net wire adapter,
  conversion chain, and mutations. In particular, a Chat caller on an `auto`
  route truthfully records Chat-to-Responses for attempt 0 and
  Chat-to-Responses followed by Responses-to-Chat for the fallback attempt.
  Safe request-body observations are recomputed from each real attempt body;
  fallback evidence describes the failed body and final evidence describes the
  last executed body. Planned, selected, fallback, and final aliases derive
  from one canonical attempt telemetry snapshot.
- Caller-tool stripping is a named `caller_tool_stripping` mutation. Optional
  capability-manifest state now fails closed unless version and cryptographic
  hash form a complete valid pair using the supported
  `provider-capabilities.v3` schema; future/unknown versions remain
  `Unqualified`. Only a supported pair with an explicit `Supported` state is
  reported as supported.

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
- Independent fourth-review repair used four vertical red/green slices:
  future manifest `v999` was incorrectly `Supported`; a provider key changed
  after planning altered the active request; Chat auto fallback final HMAC
  described attempt 0; and final `HTTPError` lacked the future-policy relay
  sentinel. Each test failed on `76b02adf` before its corresponding repair and
  passed afterward.
- Fifth-review repair added public-seam RED coverage for the remaining four
  review findings. Both compact `auto` directions reproduced a Responses 404
  followed by a Chat 400 whose final relay still used attempt 0 format; real
  JSON and SSE relays with same-format `verify=true` reproduced the deleted
  `behavior_profile` `NameError`; and the RoutePlan dataclass still accepted
  duplicate primary-attempt constructor fields. The focused RED reported five
  failing subtests at `013e1daa`. The repaired focused selection then passed
  6 tests and 4 subtests.
- Relay tests now use small typed fixtures with literal policy expectations.
  Converted fixtures are selected explicitly at the call site; the helper does
  not branch on production profiles or derive expected policy from the tested
  upstream/inbound formats. A full routing run found one remaining assertion
  against the deliberately removed `upstream_format` relay keyword
  (589 passed, 221 subtests passed). Updating that assertion to the required
  `RelayExecutionPlan.selected_upstream_format` contract produced 590 passed,
  221 subtests passed; this was a test-contract update, not a production
  regression.

## Candidate verification

### Exact-head verification (2026-08-01)

- Focused routing/Gateway gate:
  `py -3.13 -m pytest -q tests/test_routing.py
  tests/test_chat_completions_gateway.py tests/test_proxy_event_logging.py`
  — 712 passed, 233 subtests passed in 50.28 seconds.
- Authoritative Python 3.13 core:
  `py -3.13 -m pytest -q --ignore=tests/test_real_client_e2e.py`
  — 1,527 passed, 1 skipped, 460 subtests passed in 124.21 seconds.
  A separate repeat in an ambient nested-interpreter environment reproduced
  two unchanged issue-108 replay-smoke failures; the Hosted Python core run
  is the acceptance result for this exact head.
- `py -3.13 -m py_compile src-python/codex_proxy.py tests/test_routing.py`
  — passed.
- `git diff --check` — passed (only the repository's CRLF normalization
  warning was emitted).
- `py -3.13 scripts/report_quality_gates.py --json` — report-only, exit 0:
  2 unused imports, 84 dead functions, 152 duplicate names, 0 parse errors.
  These findings remain non-blocking baseline reports; no new RoutePlan
  finding was introduced.
- Hosted CI run `30682449604` for this exact head passed `CI classifier`,
  `Python core`, and `CI / gate`; the remaining jobs were correctly skipped
  by the path classifier.

- Third-review affected delta:
  `python -m pytest -q tests/test_routing.py tests/test_chat_completions_gateway.py tests/test_proxy_event_logging.py`
  — 701 passed, 226 subtests passed in 44.98 seconds with Python 3.13.
  Coverage includes the attempt execution contract, actual Chat `auto` 404
  fallback bodies/telemetry, manifest identity pairs, caller-tool stripping,
  and handler-level Vision Proxy pass/proxy/reject/failure I/O behavior.
- Fourth-review repair focused gate:
  `python -m pytest -q tests/test_routing.py tests/test_chat_completions_gateway.py tests/test_proxy_event_logging.py`
  — 704 passed, 227 subtests passed in 52.12 seconds with Python 3.13.
- After moving generated Codex request identities into the immutable
  materialization input, the affected focused suites plus the shutdown
  cancellation contract passed again: 705 passed, 227 subtests passed in
  47.30 seconds.
- Fourth-review repair Python core candidate:
  `python -m pytest -q --ignore=tests/test_real_client_e2e.py`
  — 1,481 passed, 1 skipped, 452 subtests passed in 86.65 seconds with Python
  3.13.11 first on `PATH`. Before correcting the inherited `PATH`, a
  non-candidate run had two nested issue-108 PowerShell replay failures, one
  shared diagnostic-tail ordering failure, and one shutdown test that still
  mocked the superseded header-building seam (1,477 passed, 1 skipped). The
  affected tests all passed after interpreter/seam correction.
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
- `python -m py_compile src-python/codex_proxy.py tests/test_routing.py tests/test_proxy_shutdown.py`
  — passed.
- `git diff --check` — passed (Git emitted only the repository's CRLF
  normalization warning).
- `python scripts/report_quality_gates.py --json` — report-only, exit 0:
  2 unused imports, 83 dead functions, 146 duplicate names, 0 parse errors.
  No new RoutePlan finding remains; baseline findings were not changed or
  allowlisted.

## Fifth-review repair verification

- Full routing: `py -3.13 -m pytest -q tests/test_routing.py` — 590 passed,
  221 subtests passed in 31.03 seconds.
- Chat/event/shutdown focused gate:
  `py -3.13 -m pytest -q tests/test_chat_completions_gateway.py
  tests/test_proxy_event_logging.py tests/test_proxy_shutdown.py` — 137 passed,
  10 subtests passed in 23.76 seconds.
- Final routing plus shutdown gate after removing every duplicated executable
  primary field, including `request_kind`: 610 passed, 221 subtests passed in
  34.46 seconds.
- `/shutdown` has an explicit independent-credential regression: the real
  endpoint completes while both upstream selection and operational provider
  authentication materialization are fail-if-called. The directed test passed.
- The first core attempt used a Python 3.13 parent but inherited Hermes Python
  3.11 first on `PATH`. It reported 1,481 passed, 1 skipped, 456 subtests and
  three failures: one shared diagnostic incident-counter ordering failure, plus
  two nested issue-108 PowerShell replays whose child interpreter rejected the
  repository's Python 3.13 type-parameter syntax. The diagnostic test passed
  immediately in isolation; a direct traceback identified the two replay
  failures as Python 3.11 `SyntaxError`, not route-plan behavior.
- Authoritative core with
  `C:\Users\noirb\AppData\Local\Programs\Python\Python313` first on `PATH`:
  1,484 passed, 1 skipped, 456 subtests passed in 92.29 seconds. A later
  redundant path-recording run printed the same Python 3.13.11 executable and
  had only the independently reproduced diagnostic incident-counter flake
  (1,483 passed); no product or test code was changed to mask it.
- `py -3.13 -m py_compile src-python/codex_proxy.py tests/test_routing.py
  tests/test_proxy_shutdown.py` — passed.
- Static contract scan: `_relay_upstream_response` has no `upstream_format`
  argument, contains no `behavior_profile` name, and all four production calls
  pass the required `RelayExecutionPlan`. `RoutePlan` exposes executable
  primary values only as read-only attempt views; its dataclass fields no
  longer include the duplicated values.
- `py -3.13 scripts/report_quality_gates.py --json` — report-only, exit 0:
  2 unused imports, 83 dead functions, 146 duplicate names, 0 parse errors.
  The obsolete cross-protocol profile helper was deleted; no new finding was
  introduced.
- `git diff --check` — passed apart from the repository's CRLF normalization
  warnings. The synthetic real-client partition was not run because none of
  its policy-listed contract-surface paths changed.

## Sixth-review repair

- Authentication ordering used a focused RED/GREEN slice. Before the repair,
  the valid route had no post-plan binding seam and both the unsupported
  official Anthropic route and Vision `REJECT` route materialized Codex
  credentials before failing. The handler now plans first, rejects
  non-executable routes before credential, mutation, or network work, then
  materializes exactly once and binds one redacted immutable header snapshot.
  A key-rotation assertion proves the active request cannot drift after
  binding.
- No-attempt plans now carry exactly one typed `RouteFailureObservation`
  instead of deriving fallback defaults from an absent attempt. The RED
  compact/API-key/Anthropic case reported `main_generation`; GREEN preserves
  the true `compact` request kind and `api_key` authentication in both the plan
  and safe event evidence.
- Runtime model metadata now retains #250's nested `capability_binding` as a
  typed `RouteCapabilityBinding`. A present malformed, stale, or protocol-
  mismatched binding yields `Unqualified` with zero attempts before
  authentication, request mutation, or upstream I/O. A valid current Chat
  binding remains executable. Missing legacy metadata remains observable as
  unqualified evidence without retroactively blocking pre-#250 catalogs.
- Exact restart disclosure and the committed transaction/readback lifecycle
  remain owned by #250 / PR #261. The #61 issue records that boundary and the
  exact downstream requirement in
  https://github.com/NOirBRight/CodexHub/issues/61#issuecomment-5127547716:
  quit and reopen Codex App; stop and restart a running `codex app-server`;
  newly started CLI processes apply immediately. This repair changes no #250
  files and does not claim that downstream Toast integration is complete.
- The relay test helper no longer accepts a legacy `behavior_profile` selector
  or verification override. The exact 38 mapping-dependent callers were
  converted to typed literal `RelayPlanFixture` objects, and a focused RED test
  proves the removed selector is rejected.

## Sixth-review repair verification

- Focused auth ordering and non-executable route gate: 4 passed, 2 subtests
  passed in 0.52 seconds.
- Focused no-attempt observation, route-capability binding, and relay-helper
  contract tests each passed after their corresponding RED failure.
- Routing plus shutdown gate:
  `py -3.13 -m pytest -q tests/test_routing.py tests/test_proxy_shutdown.py`
  — 614 passed, 223 subtests passed in 35.08 seconds.
- Chat/event gate:
  `py -3.13 -m pytest -q tests/test_chat_completions_gateway.py
  tests/test_proxy_event_logging.py` — 117 passed, 10 subtests passed in
  17.43 seconds.
- The five new review-matrix tests passed together (5 passed, 2 subtests);
  the binding test also passed after switching its setup to the real
  `choose_upstream` metadata-copy path.
- `py -3.13 -m py_compile src-python/codex_proxy.py tests/test_routing.py
  tests/test_proxy_shutdown.py tests/test_chat_completions_gateway.py
  tests/test_proxy_event_logging.py` — passed.
- `py -3.13 scripts/report_quality_gates.py --json` — report-only, exit 0:
  2 unused imports, 83 dead functions, 146 duplicate names, 0 parse errors.
- `git diff --check` — passed apart from the repository's CRLF normalization
  warnings.
- Per the strict delta policy, the previously green full Python core was not
  rerun for this repair. The synthetic real-client partition remains not
  applicable because no listed contract-surface path changed.

## Final relay-helper contract repair

- RED: the new static contract reported the helper's `RELAY_GATEWAY` default
  plus all 58 implicit call sites. GREEN requires the keyword-only
  `RelayPlanFixture`, and all 109 helper calls now supply an explicit typed
  fixture; the sole legacy `behavior_profile` call remains the deliberate
  rejection regression.
- Directed static-contract and legacy-rejection tests: 2 passed in 1.17
  seconds. Full routing: `py -3.13 -m pytest -q tests/test_routing.py` —
  595 passed, 223 subtests passed in 31.21 seconds.
- `py -3.13 -m py_compile tests/test_routing.py` and `git diff --check`
  passed; the latter emitted only the repository's CRLF warning. The
  report-only quality scan remained at 2 unused imports, 83 dead functions,
  146 duplicate names, and 0 parse errors.
- This repair changes test contracts and evidence only; production behavior
  is unchanged. No full core or synthetic real-client suite was rerun.

No live-provider or manual Desktop evidence is required by issue #61.
