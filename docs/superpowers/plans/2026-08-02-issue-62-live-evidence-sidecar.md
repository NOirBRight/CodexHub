# Issue #62 Live-Evidence Sidecar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone, explicitly enabled loopback sidecar that independently fingerprints complete request, response, and SSE application-body bytes on the pre- and post-Gateway hops without changing production routing.

**Architecture:** One standard-library Python script owns validation, HTTP forwarding, streaming fingerprints, atomic sanitized artifacts, and bounded shutdown. Two independent instances use the same HMAC key and different `pre`/`post` hop labels; focused tests connect them to a real local fake upstream and compare their independently written artifacts.

**Tech Stack:** Python 3.13 standard library, pytest, loopback `http.server`/`http.client`, SHA-256, HMAC-SHA-256, atomic `os.replace`.

## Global Constraints

- The script starts only with `--enable-live-capture` and listens only on IPv4 or IPv6 loopback.
- Do not modify or import the script from production Gateway, telemetry, diagnostic-recorder, Tauri, Desktop, config, or route handlers.
- Add no dependency and perform no real upstream request in tests or verification.
- Persist only sanitized metadata and digests; never persist URLs, paths, headers, credentials, key material, raw bodies, prompts, tool values, or wire identifiers.
- Request/response byte caps and connect/read/overall timeouts are mandatory and positive.
- Overflow, timeout, cancellation, forwarding failure, missing SSE terminal, and incomplete SSE framing produce an `incomplete` record and leave no `.partial` file.
- Synthetic tests prove only `synthetic_lane_verified`; they do not change `docs/evidence/issue-62/runtime-wire-inventory.json` or any qualification state.

---

### Task 1: Activation, configuration, and sanitized atomic artifacts

**Files:**
- Create: `scripts/capture_issue_62_live_evidence.py`
- Create: `tests/test_issue_62_live_evidence_sidecar.py`

**Interfaces:**
- Produces: `SidecarConfig`, `validate_config(config)`, `write_capture_record(output_dir, hop, record, capture_key=..., correlation_token=...)`, and `main(argv=None) -> int`.
- Consumes: only Python standard-library modules and a pre-existing HMAC key file.

- [x] **Step 1: Write failing activation and artifact tests**

```python
def test_main_requires_explicit_enable(capsys):
    assert sidecar.main([]) == 2
    assert "live capture is disabled" in capsys.readouterr().err

@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.10"])
def test_config_rejects_non_loopback(host, base_config):
    with pytest.raises(sidecar.ConfigurationError, match="listen_not_loopback"):
        sidecar.validate_config(dataclasses.replace(base_config, listen_host=host))

def test_atomic_record_is_sanitized_and_leaves_no_partial(tmp_path):
    record_path = sidecar.write_capture_record(
        tmp_path,
        "pre",
        SAFE_RECORD,
        capture_key=KEY,
        correlation_token=CORRELATION_TOKEN,
    )
    assert json.loads(record_path.read_text(encoding="utf-8"))["schema"] == (
        "codexhub.issue62.live-evidence-lane.v1"
    )
    assert not list(tmp_path.glob("*.partial"))
```

- [x] **Step 2: Run the tests and verify RED**

Run: `py -3.13 -m pytest -q tests/test_issue_62_live_evidence_sidecar.py -k "requires_explicit_enable or rejects_non_loopback or atomic_record"`

Expected: collection fails because `scripts/capture_issue_62_live_evidence.py` does not exist.

- [x] **Step 3: Implement validation and atomic writing**

Implement a frozen `SidecarConfig`, fixed schema/failure vocabularies, loopback `ipaddress.ip_address(...).is_loopback` validation, strict URL/key/bound checks, a default-disabled CLI, recursive allow-listed record validation, and unique `.partial` -> `.json` replacement with cleanup in `finally`.

- [x] **Step 4: Run the focused tests and verify GREEN**

Run: `py -3.13 -m pytest -q tests/test_issue_62_live_evidence_sidecar.py -k "requires_explicit_enable or rejects_non_loopback or atomic_record"`

Expected: all selected tests pass.

### Task 2: Complete request/response and SSE fingerprint primitives

**Files:**
- Modify: `scripts/capture_issue_62_live_evidence.py`
- Modify: `tests/test_issue_62_live_evidence_sidecar.py`

**Interfaces:**
- Produces: `BodyFingerprint`, `SseSequenceFingerprint`, and artifact dictionaries with `bytes`, `sha256`, `hmac_sha256`, `complete`, `frame_count`, `frame_bytes`, and allow-listed `terminal_classes`.
- Consumes: raw byte chunks in their observed order and the pre-loaded HMAC key bytes.

- [x] **Step 1: Write failing literal digest and chunk-boundary tests**

```python
def test_body_fingerprint_hashes_every_byte():
    observed = sidecar.BodyFingerprint(KEY, b"request-body")
    observed.update(b"abc")
    observed.update(b"\x00def")
    assert observed.complete() == {
        "bytes": 7,
        "sha256": hashlib.sha256(b"abc\x00def").hexdigest(),
        "hmac_sha256": hmac.new(KEY, b"request-body\0abc\x00def", hashlib.sha256).hexdigest(),
        "complete": True,
    }

def test_sse_sequence_digest_is_independent_of_transport_chunks():
    wire = b'data: {"type":"response.output_text.delta","delta":"secret"}\n\ndata: {"type":"response.completed"}\n\n'
    one = consume_sse([wire])
    split = consume_sse([wire[:5], wire[5:41], wire[41:]])
    assert split == one
    assert split["frame_count"] == 2
    assert split["terminal_classes"] == ["response.completed"]
```

- [x] **Step 2: Run the tests and verify RED**

Run: `py -3.13 -m pytest -q tests/test_issue_62_live_evidence_sidecar.py -k "fingerprint_hashes or sequence_digest"`

Expected: failure because the fingerprint classes are absent.

- [x] **Step 3: Implement minimal incremental fingerprints**

Use `hashlib.sha256()` and `hmac.new(key, domain + b"\0", hashlib.sha256)` for bodies. Split complete LF or CRLF SSE frames independent of read chunks, hash `len(frame).to_bytes(8, "big") + frame` in order, classify only `response.completed`, `response.failed`, `error`, and `[DONE]`, and mark trailing frames or missing terminals incomplete.

- [x] **Step 4: Run the focused tests and verify GREEN**

Run: `py -3.13 -m pytest -q tests/test_issue_62_live_evidence_sidecar.py -k "fingerprint_hashes or sequence_digest"`

Expected: all selected tests pass.

### Task 3: Real loopback forwarding, bounds, cancellation, and cleanup

**Files:**
- Modify: `scripts/capture_issue_62_live_evidence.py`
- Modify: `tests/test_issue_62_live_evidence_sidecar.py`

**Interfaces:**
- Produces: `CaptureSidecarServer`, `start()`/`shutdown()` lifecycle, and one atomic sanitized capture record per request.
- Consumes: validated `SidecarConfig`; forwards via `http.client.HTTPConnection` or `HTTPSConnection` and strips HTTP hop-by-hop headers.

- [x] **Step 1: Write failing two-hop integration test**

```python
def test_two_hops_capture_matching_complete_request_response_and_sse(tmp_path):
    request_body = b'{"input":"private prompt","stream":true}'
    sse_body = b'data: {"type":"response.output_text.delta","delta":"private output"}\n\ndata: {"type":"response.completed"}\n\n'
    with fake_upstream(sse_body) as upstream, running_lane(tmp_path, upstream.url) as lane:
        status, response_body = post_bytes(lane.pre_url + "/responses?opaque=wire-id", request_body)
    assert status == 200
    assert response_body == sse_body
    pre = read_only_record(lane.pre_output)
    post = read_only_record(lane.post_output)
    assert pre["request"] == post["request"]
    assert pre["response"] == post["response"]
    assert pre["sse"]["sequence_sha256"] == post["sse"]["sequence_sha256"]
    assert pre["outcome"] == post["outcome"] == "complete"
```

- [x] **Step 2: Run the integration test and verify RED**

Run: `py -3.13 -m pytest -q tests/test_issue_62_live_evidence_sidecar.py::test_two_hops_capture_matching_complete_request_response_and_sse`

Expected: failure because the HTTP server is absent.

- [x] **Step 3: Implement the minimal server and forwarding path**

Build a `ThreadingHTTPServer` with tracked request/client/upstream lifecycles, exact `Content-Length` request reads, application-body forwarding, response streaming in bounded chunks, deadline checks, fixed sanitized failure codes, and `shutdown()` that closes the listener and active sockets before a bounded active-handler drain. Do not store exception text.

- [x] **Step 4: Run the integration test and verify GREEN**

Run: `py -3.13 -m pytest -q tests/test_issue_62_live_evidence_sidecar.py::test_two_hops_capture_matching_complete_request_response_and_sse`

Expected: one complete record in each output directory with identical request, response, and SSE digests.

- [x] **Step 5: Write failing bound, timeout, sanitization, and cancellation tests**

```python
@pytest.mark.parametrize("case", ["request_overflow", "response_overflow", "upstream_timeout", "client_cancel"])
def test_failure_cases_are_incomplete_sanitized_and_clean(case, tmp_path):
    result = run_failure_case(case, tmp_path)
    record = read_only_record(result.output)
    serialized = json.dumps(record, sort_keys=True)
    assert record["outcome"] == "incomplete"
    assert record["failure"] in EXPECTED_FAILURES
    assert not list(result.output.glob("*.partial"))
    for sensitive in result.sensitive_values:
        assert sensitive not in serialized
```

- [x] **Step 6: Run the failure tests and verify RED**

Run: `py -3.13 -m pytest -q tests/test_issue_62_live_evidence_sidecar.py -k "failure_cases"`

Expected: at least one case fails because its bound or cleanup path is not implemented.

- [x] **Step 7: Implement bounded failure paths and cleanup**

Reject invalid/oversized requests before forwarding, stop response relay at the cap, apply socket and overall deadlines, classify broken downstream writes as `downstream_cancelled`, close every upstream connection in `finally`, null incomplete full-body digests, atomically write the incomplete record, and remove every owned `.partial` file.

- [x] **Step 8: Run the full sidecar test file and verify GREEN**

Run: `py -3.13 -m pytest -q tests/test_issue_62_live_evidence_sidecar.py`

Expected: all sidecar tests pass and no test contacts a non-loopback address.

### Task 4: Usage documentation and candidate verification

**Files:**
- Modify: `docs/evidence/issue-62/README.md`
- Modify: `docs/superpowers/plans/2026-08-02-issue-62-live-evidence-sidecar.md`

**Interfaces:**
- Produces: copyable pre/post commands that remain inert until explicitly enabled and state the exact non-qualification boundary.
- Consumes: the CLI implemented in Tasks 1-3.

- [x] **Step 1: Add isolated two-process usage commands**

Document distinct output directories and loopback ports, the shared isolated HMAC key file, required bounds/timeouts, and explicit operator-supplied tokens for the separately authorized isolated Gateway/upstream addresses. State that the command must not be aimed at the currently running Desktop/Gateway without a new control-window authorization.

- [x] **Step 2: Run focused and evidence regression checks**

Run:

```powershell
py -3.13 -m pytest -q tests/test_issue_62_live_evidence_sidecar.py tests/test_diagnostic_recorder.py tests/test_diagnostic_recorder_gateway.py tests/test_issue_62_runtime_trace.py tests/test_issue_62_runtime_audit.py tests/test_issue_62_runtime_inventory.py
py -3.13 -m pytest -q --ignore=tests/test_real_client_e2e.py
python scripts/build_issue_62_runtime_inventory.py --check-drift
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check-codex-thread-tool-surface.ps1
python scripts/report_quality_gates.py
git diff --check
```

Expected: focused tests pass and the strict Python suite excluding the out-of-scope real-client E2E contract completes without a sidecar regression. Any strict-suite failure must be reproduced at the exact `origin/dev` baseline and reported rather than counted as a pass. Inventory drift reports a match; PowerShell reports `THREAD_TOOL_SURFACE_COMPLETE` without changing qualification; quality-gate output remains report-only; diff check is clean.

- [x] **Step 3: Confirm the qualification artifact is unchanged**

Run: `git diff origin/dev -- docs/evidence/issue-62/runtime-wire-inventory.json`

Expected: no output.

- [x] **Step 4: Commit the implementation candidate**

```powershell
git add scripts/capture_issue_62_live_evidence.py tests/test_issue_62_live_evidence_sidecar.py docs/evidence/issue-62/README.md docs/superpowers/plans/2026-08-02-issue-62-live-evidence-sidecar.md
git commit -m "test: add isolated Issue 62 live evidence sidecar"
```
