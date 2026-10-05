"""Public evidence oracle tests: synthetic data cannot count as live success."""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("qualification", ROOT / "scripts/qualify_subscription_codemode.py")
qualification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualification)


def test_observer_does_not_mutate_history_or_expose_description():
    payload = {"model": "cursor-subscription/exact", "tools": [{"type": "namespace", "name": "functions", "tools": [{"type": "custom", "name": "exec", "description": "SECRET-DYNAMIC-DESCRIPTION", "format": {"type": "grammar"}}]}],
               "input": [{"type": "agent_message", "author": "/root", "recipient": "/root/child", "content": [{"type": "input_text", "text": "SECRET-TASK"}]},
                         {"type": "custom_tool_call_output", "call_id": "stable-call", "id": "typed-item", "output": "random-fixture"}]}
    original = json.dumps(payload, sort_keys=True)
    observed = qualification.observe_request(payload, 1, "random-fixture")
    assert json.dumps(payload, sort_keys=True) == original
    assert "SECRET" not in json.dumps(observed)
    assert observed["tool_outputs"][0]["call_sha256"] == qualification.digest("stable-call")
    assert observed["declarations"][0]["type"] == "custom"
    assert observed["declarations"][0]["namespace"] == "functions"


def session(child=False, reads=1, spawn=False, followup=False, wait=False, model=None):
    return {"is_child": child, "models": [model or qualification.CURSOR_MODEL], "calls": [{"type": "custom_tool_call", "name": "exec", "reads_fixture": bool(reads), "call_sha256": "call", "spawn": spawn, "followup": followup, "wait": wait}] * reads,
            "effects": [{"call_sha256": "call", "fixture_present": True}], "assistant_returned_fixture": True, "assistant_returned_reversed_fixture": True}


def test_code_mode_requires_actual_custom_effect_and_exact_final():
    turns = [{"exit": 0, "timed_out": False, "errors": [], "finals": ["nonce"], "pid": 10}]
    result = qualification.assess_case("code-mode", turns, [{"model": qualification.CURSOR_MODEL}], [session()], "nonce", [1])
    assert result["passed"]
    fake = session()
    fake["effects"][0]["call_sha256"] = "different-call"
    assert not qualification.assess_case("code-mode", turns, [{"model": qualification.CURSOR_MODEL}], [fake], "nonce", [1])["passed"]
    turns[0]["finals"] = ["nonce\u200b"]
    assert not qualification.assess_case("code-mode", turns, [{"model": qualification.CURSOR_MODEL}], [session()], "nonce", [1])["passed"]


def test_restart_requires_new_gateway_and_caller_and_same_history_calls():
    turns = [{"exit": 0, "timed_out": False, "errors": [], "finals": [value], "pid": pid} for value, pid in [("nonce", 10), ("ecnon", 11)]]
    traces = [{"epoch": epoch, "tool_outputs": [{"call_sha256": "call"}], "model": qualification.CURSOR_MODEL} for epoch in (0, 1)]
    assert qualification.assess_case("completed-history-restart", turns, traces, [session()], "nonce", [1, 2])["passed"]
    assert not qualification.assess_case("completed-history-restart", turns, traces, [session()], "nonce", [1, 1])["passed"]
    traces[1]["tool_outputs"][0]["call_sha256"] = "fabricated-new-call"
    assert not qualification.assess_case("completed-history-restart", turns, traces, [session()], "nonce", [1, 2])["passed"]


def test_v2_requires_same_single_child_and_real_followup_wait_and_direction():
    turns = [{"exit": 0, "timed_out": False, "errors": [], "finals": [value], "pid": pid} for value, pid in [("nonce\necnon", 10), ("ecnon", 11)]]
    traces = [{"epoch": epoch, "tool_outputs": [{"call_sha256": "call"}], "model": model} for epoch, model in [(0, qualification.OFFICIAL_MODEL), (0, qualification.CURSOR_MODEL), (1, qualification.OFFICIAL_MODEL)]]
    sessions = [session(reads=0, model=qualification.OFFICIAL_MODEL), session(child=True, reads=2)]
    sessions[0]["calls"] = [{"type": "function_call", "name": "spawn_agent", "reads_fixture": False, "call_sha256": "spawn", "spawn": True, "followup": True, "wait": True, "spawn_target_sha256": "reader", "target_sha256": "reader"}]
    assert qualification.assess_case("official-to-cursor", turns, traces, sessions, "nonce", [1, 2])["passed"]
    assert not qualification.assess_case("official-to-cursor", turns, traces, sessions + [session(child=True)], "nonce", [1, 2])["passed"]
    sessions[0]["calls"][0].update(type="custom_tool_call", name="exec", reads_fixture=True)
    assert not qualification.assess_case("official-to-cursor", turns, traces, sessions, "nonce", [1, 2])["passed"]


def test_bad_finals_keep_zero_width_evidence_and_raw_text_hash():
    stdout = '\n'.join(json.dumps(event) for event in [{"type": "item.completed", "item": {"type": "agent_message", "text": "commentary"}},
                                                       {"type": "item.completed", "item": {"type": "agent_message", "text": "nonce\u200b"}}])
    result = qualification.parse_turn(stdout, SimpleNamespace(returncode=0, pid=1), False, "nonce")
    assert result["finals"] == [{"sha256": qualification.digest("nonce\u200b"), "length": 6, "zero_width_count": 1}]


def test_private_rollout_summary_proves_child_execution_with_call_identity(tmp_path):
    path = tmp_path / "rollout.jsonl"
    rows = [{"type": "session_meta", "payload": {"id": "PRIVATE-CHILD-ID", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "PRIVATE-PARENT"}}}}},
            {"type": "turn_context", "payload": {"model": qualification.CURSOR_MODEL}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "namespace": "functions", "id": "typed", "call_id": "call", "input": 'text(await tools.exec_command({cmd:"cat probe-value.txt"}))'}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "call", "output": "nonce"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "nonce"}]}}]
    path.write_text('\n'.join(json.dumps(row) for row in rows))
    result = qualification.summarize_rollouts([path], "nonce")
    assert result[0]["is_child"] and result[0]["calls"][0]["reads_fixture"]
    assert result[0]["effects"][0]["call_sha256"] == qualification.digest("call")
    assert "PRIVATE" not in json.dumps(result)


def test_harness_rejects_unbounded_timeout_before_any_run(tmp_path):
    with pytest.raises(SystemExit):
        qualification.main(["--case-timeout", "181", "--output", str(tmp_path / "result.json")])


@pytest.mark.parametrize("change", ["staged", "unstaged", "untracked"])
def test_advanced_main_rejects_dirty_runtime_before_snapshot_or_cases(tmp_path, monkeypatch, change):
    source = tmp_path / "source"
    runtime = source / "src-python"
    runtime.mkdir(parents=True)
    (runtime / "candidate.txt").write_text("committed")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Qualification Fixture",
                    "-c", "user.email=fixture@example.invalid", "commit", "-qm", "candidate"], check=True)
    (runtime / ("new.py" if change == "untracked" else "candidate.txt")).write_text("uncommitted")
    if change == "staged":
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)

    def unreachable(*args, **kwargs):
        pytest.fail("dirty runtime must fail before snapshot, accounts, or case execution")

    monkeypatch.setattr(qualification, "freeze_candidate", unreachable)
    monkeypatch.setattr(qualification, "run_case", unreachable)
    output = tmp_path / "result.json"
    assert qualification.main(["--checkout", str(source), "--codex", sys.executable,
                               "--case", "code-mode", "--output", str(output)]) == 1
    report = json.loads(output.read_text())
    assert report["source_runtime_dirty"] and not report["candidate_sha_is_exact_runtime"]
    assert not report["passed"] and report["snapshot_tree_removed"]
    assert report["cases"] == [{"case": "candidate.admission", "checks": {"passed": False},
                                "failure_class": "candidate-runtime-not-clean"}]



def test_frozen_candidate_restart_bytes_do_not_follow_worker_edits(tmp_path):
    source, frozen = tmp_path / "source", tmp_path / "frozen"
    for name in ("src-python", "config"):
        (source / name).mkdir(parents=True)
        (source / name / "sample.txt").write_text("candidate")
    digest = qualification.freeze_candidate(source, frozen)
    (source / "src-python/sample.txt").write_text("worker edit")
    assert (frozen / "src-python/sample.txt").read_text() == "candidate"
    assert len(digest) == 64



def test_harness_rejects_duplicate_cases_before_any_candidate_or_inference(tmp_path):
    with pytest.raises(SystemExit):
        qualification.main(["--case", "code-mode", "--case", "code-mode", "--output", str(tmp_path / "result.json")])


@pytest.mark.parametrize("mode", ["default", "observed", "gzip-budget", "plain-budget", "depth-budget", "node-budget", "request-budget"])
def test_public_case_smoke_uses_real_gateway_but_no_inference_and_checks_cleanup(tmp_path, monkeypatch, record_property, mode):
    import os
    import gzip
    import http.client
    with_observation = mode != "default"
    if os.name == "nt":
        pytest.skip("POSIX fake executable fixture")
    source, user, frozen = tmp_path / "codex-source", tmp_path / "user", tmp_path / "candidate"
    (source / "model-catalogs").mkdir(parents=True)
    (user / ".config/cursor").mkdir(parents=True)
    (source / "model-catalogs/codexhub-model-catalog.json").write_text(json.dumps({"models": [{"slug": qualification.OFFICIAL_MODEL, "display_name": "official fixture", "visibility": "list", "supported_in_api": True, "tool_mode": "code_mode", "multi_agent_version": "v2"}]}))
    (source / "auth.json").write_text('{"auth_mode":"chatgpt","tokens":{"access_token":"dummy-never-used","refresh_token":"dummy-never-used","id_token":"dummy-never-used"}}')
    (user / ".config/cursor/auth.json").write_text('{"accessToken":"dummy-never-used"}')
    installed = tmp_path / "installed" / "2026.09.28-64d2043"
    installed.mkdir(parents=True)
    cursor = installed / "cursor-agent"
    cursor.write_text("#!/bin/sh\nexit 1\n")
    cursor.chmod(0o700)
    fake = tmp_path / "fake-codex"
    fake.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then printf "codex-cli 0.159.3\\n"; else printf \'{"type":"turn.failed","error":{"message":"fake caller no request"}}\\n\'; exit 1; fi\n')
    fake.chmod(0o700)
    qualification.freeze_candidate(ROOT, frozen)
    plan = None
    runner = qualification.run_case
    if with_observation:
        module, manifest = qualification.freeze_observation(ROOT, frozen)
        plan = module.load_observation(frozen / "scripts/subscription_fixture_observer.py").fixture_plan()
        # Explicit synthetic final: exercise publication without a model call.
        compound = plan["value"] + "\n" + plan["value"][::-1]
        fake.write_text(f'#!{sys.executable}\nimport json,sys\nif sys.argv[1] == "--version": print("codex-cli 0.159.3")\nelse:\n print(json.dumps({{"type":"item.completed","item":{{"type":"agent_message","text":{compound!r}}}}}))\n raise SystemExit(1)\n')
        runner = module.run_case
        assert set(manifest) == set(qualification.OBSERVATION_WRAPPERS)
        if mode.endswith("budget"):
            observer_module = module.load_observation(frozen / "scripts/subscription_fixture_observer.py")
            observer_module.MAX_PARSE_BYTES = 256
            if mode == "request-budget":
                observer_module.MAX_REQUESTS = 0
            monkeypatch.setattr(module, "load_observation", lambda *args: observer_module)
            body = json.dumps({"model": "inert-unknown-model", "input": [{"role": "user", "content": "x" * 300}]}).encode()
            if mode == "depth-budget":
                body = b'{"model":"inert-unknown-model","input":' + b'[' * 20 + b'0' + b']' * 20 + b'}'
            if mode == "node-budget":
                observer_module.MAX_PARSE_BYTES = 16384
                body = b'{"model":"inert-unknown-model","input":[' + b'0,' * 4096 + b'0]}'
            encoding = "gzip" if mode == "gzip-budget" else "identity"
            if encoding == "gzip":
                body = gzip.compress(body)
            fake.write_text(f'''#!{sys.executable}
import http.client,json,os,tomllib
from pathlib import Path
import sys
if sys.argv[1] == "--version":
 print("codex-cli 0.159.3")
else:
 config=tomllib.loads((Path(os.environ["CODEX_HOME"])/"config.toml").read_text())
 url=config["model_providers"]["qualification"]["base_url"]
 from urllib.parse import urlsplit
 connection=http.client.HTTPConnection(urlsplit(url).netloc,timeout=3)
 connection.request("POST","/v1/responses",body={body!r},headers={{"Content-Encoding":{encoding!r},"Authorization":"Bearer "+os.environ["SUBSCRIPTION_QUALIFICATION_KEY"],"Content-Type":"application/json"}})
 response=connection.getresponse();response.read();connection.close()
 print(json.dumps({{"type":"turn.failed"}}))
 raise SystemExit(1)
''')
            forwarded = []
            original_request = http.client.HTTPConnection.request
            def measured_request(connection, method, url, body=None, headers={}, **kwargs):
                if method == "POST":
                    forwarded.append((body, dict(headers)))
                return original_request(connection, method, url, body, headers, **kwargs)
            monkeypatch.setattr(http.client.HTTPConnection, "request", measured_request)
            parsed_requests, decoded_reads = [], []
            original_loads = json.loads
            def measured_loads(data, *args, **kwargs):
                if isinstance(data, (bytes, bytearray)) and b"inert-unknown-model" in data:
                    parsed_requests.append(len(data))
                return original_loads(data, *args, **kwargs)
            monkeypatch.setattr(json, "loads", measured_loads)
            original_gzip = gzip.GzipFile
            class MeasuredGzip(original_gzip):
                def read1(self, size=-1):
                    result = super().read1(size)
                    decoded_reads.append((size, len(result)))
                    return result
            monkeypatch.setattr(gzip, "GzipFile", MeasuredGzip)
    monkeypatch.syspath_prepend(str(frozen / "src-python"))
    monkeypatch.setenv("PATH", str(installed) + os.pathsep + os.environ.get("PATH", os.defpath))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    result = runner("cursor-to-official" if with_observation else "code-mode", frozen, source, user, fake, 30,
                    **({"observation_plan": plan} if plan else {}))
    assert len(result["gateway_pids"]) == 1
    if mode.endswith("budget"):
        assert len(result["requests"]) == 1
        assert result["requests"][0]["status"] >= 400  # unknown model; never inference
        assert forwarded == [(body, {"Accept-Encoding": "identity", "Content-Encoding": encoding, "Authorization": forwarded[0][1]["Authorization"],
            "Content-Type": "application/json", "Content-Length": str(len(body))})]
        row = result["requests"][0]
        assert parsed_requests == []  # Reject before stdlib materializes the tree.
        if mode == "gzip-budget":
            assert decoded_reads and all(0 < size <= 257 for size, _ in decoded_reads)
            assert sum(size for _, size in decoded_reads) <= 257  # Budget + one sentinel byte.
        if mode == "request-budget":
            assert row["fixture_observation_absent"] == "request-count-limit"
            assert "model" not in row  # No parser work after count rejection.
        else:
            assert row["observation_invalid"]
            assert result["fixture_observations"][0]["capture_failure"] == "observation-incomplete"
    else:
        assert result["requests"] == []  # no inference request
    assert result["failure_class"] is None
    assert not result["checks"]["passed"]
    assert result["private_tree_removed"]
    owned_pids = [*result["gateway_pids"], *(row["pid"] for row in result["turns"])]
    for pid in owned_pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    record_property("scope", "synthetic-no-inference")
    record_property("owned_pids_reaped", json.dumps(owned_pids))
    record_property("private_tree_removed", str(result["private_tree_removed"]))
    if mode == "observed":
        assert result["fixture_observations"][0]["boundaries"]["fixtureinput"]["utf8_hex"] == (plan["value"] + "\n").encode().hex()
        assert result["fixture_observations"][0]["boundaries"]["nativefield"]["state"] == "absent"
        assert result["turns"][0]["finals"] == [{"sha256": qualification.digest(compound), "length": 49, "zero_width_count": 0}]
        assert compound not in json.dumps(result)



def test_public_oracle_rejects_a_model_fallback_even_when_fixture_matches():
    turns = [{"exit": 0, "timed_out": False, "errors": [], "finals": ["nonce"], "pid": 1}]
    wrong = qualification.assess_case("code-mode", turns, [{"model": "nearest-family-low"}], [session()], "nonce", [2])
    assert not wrong["exact_selected_model_identity"] and not wrong["passed"]


def test_observer_preserves_payload_and_records_safe_selection_controls():
    request = {"model": qualification.CURSOR_MODEL, "reasoning": {"effort": "high"}, "parallel_tool_calls": True}
    before = json.dumps(request)
    result = qualification.observe_request(request, 0, "nonce")
    assert result["requested_reasoning_effort"] == "high"
    assert result["parallel_tool_calls"] is True
    assert json.dumps(request) == before
    result = qualification.observe_request({"reasoning_effort": "PRIVATE-DYNAMIC-STRING", "parallel_tool_calls": 1}, 0, "nonce")
    assert result["requested_reasoning_effort"] == "unsupported"
    assert result["parallel_tool_calls"] == "invalid"
    assert "PRIVATE" not in json.dumps(result)



def test_delegation_message_mentioning_fixture_command_is_not_parent_execution():
    assert not qualification.is_fixture_execution({"type": "function_call", "name": "spawn_agent", "reads_fixture": True})
    assert not qualification.is_fixture_execution({"type": "function_call", "name": "followup_task", "reads_fixture": True})
    assert qualification.is_fixture_execution({"type": "custom_tool_call", "name": "exec", "reads_fixture": True})


def test_parent_restart_replays_its_results_without_merging_child_internals():
    turns = [{"exit": 0, "timed_out": False, "errors": [], "finals": [value], "pid": pid} for value, pid in [("nonce\necnon", 10), ("ecnon", 11)]]
    traces = [{"epoch": epoch, "tool_outputs": [{"call_sha256": call}], "model": model} for epoch, model, call in [(0, qualification.CURSOR_MODEL, "parent-call"), (0, qualification.OFFICIAL_MODEL, "child-call"), (1, qualification.CURSOR_MODEL, "parent-call")]]
    parent = session(reads=0)
    parent["calls"] = [{"type": "function_call", "name": "spawn_agent", "reads_fixture": True, "call_sha256": "spawn", "spawn": True, "followup": True, "wait": True, "spawn_target_sha256": "reader", "target_sha256": "reader"}]
    result = qualification.assess_case("cursor-to-official", turns, traces, [parent, session(child=True, reads=2, model=qualification.OFFICIAL_MODEL)], "nonce", [1, 2])
    assert result["parent_did_not_read"]
    assert result["completed_calls_replayed"]
    assert result["passed"]



def test_encryption_observation_records_shape_without_retaining_ciphertext():
    assert qualification.encryption_marker({}) == "absent"
    assert qualification.encryption_marker({"encrypted_function_args": []}) == "empty-list"
    request = {"input": [{"type": "function_call", "name": "spawn_agent", "call_id": "call", "encrypted_function_args": ["PRIVATE-CIPHERTEXT"]}]}
    evidence = qualification.observe_request(request, 0, "nonce")
    assert evidence["history_calls"][0]["encrypted_function_args_shape"] == "nonempty-list"
    assert "PRIVATE" not in json.dumps(evidence)


@pytest.mark.parametrize("parent_model,child_model", [
    (qualification.CURSOR_MODEL, qualification.OFFICIAL_MODEL),
    ("unknown-parent", qualification.CURSOR_MODEL),
    (qualification.OFFICIAL_MODEL, "unknown-child"),
    (None, qualification.CURSOR_MODEL),
    (qualification.OFFICIAL_MODEL, None),
])
def test_v2_oracle_rejects_wrong_or_missing_observed_role_models(parent_model, child_model):
    turns = [{"exit": 0, "timed_out": False, "errors": [], "finals": [value], "pid": pid}
             for value, pid in [("nonce\necnon", 10), ("ecnon", 11)]]
    traces = [{"epoch": epoch, "tool_outputs": [{"call_sha256": "call"}], "model": model}
              for epoch, model in [(0, qualification.OFFICIAL_MODEL), (0, qualification.CURSOR_MODEL), (1, qualification.OFFICIAL_MODEL)]]
    parent, child = session(reads=0), session(child=True, reads=2)
    parent["models"] = [parent_model] if parent_model else []
    child["models"] = [child_model] if child_model else []
    parent["calls"] = [{"type": "function_call", "name": "spawn_agent", "reads_fixture": False,
                        "call_sha256": "spawn", "spawn": True, "followup": True, "wait": True,
                        "spawn_target_sha256": "reader", "target_sha256": "reader"}]
    result = qualification.assess_case("official-to-cursor", turns, traces, [parent, child], "nonce", [1, 2])
    assert result["direction_models"]
    assert not result["passed"]
    assert not (result["parent_role_model"] and result["child_role_model"])


@pytest.mark.parametrize("kind", ["bytes", "files"])
def test_rollout_capture_has_an_aggregate_read_budget(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(qualification, "MAX_CAPTURE", 16)
    monkeypatch.setattr(qualification, "MAX_ROLLOUT_FILES", 2)
    files = []
    for index in range(3 if kind == "files" else 2):
        path = tmp_path / f"rollout-{index}.jsonl"
        path.write_bytes(b"{}\n" if kind == "files" else b" " * 9)
        files.append(path)
    with pytest.raises(qualification.CaptureLimitExceeded):
        qualification.summarize_rollouts(files, "nonce")


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group resource fixture")
def test_stdout_budget_stops_a_running_caller_before_capture_can_grow(tmp_path, monkeypatch):
    monkeypatch.setattr(qualification, "MAX_CAPTURE", 128)
    capture = tmp_path / "caller.jsonl"
    before = time.monotonic()
    with pytest.raises(qualification.CaptureLimitExceeded):
        qualification.capture_caller([sys.executable, "-c", "import os,time; os.write(1,b'x'*4096); time.sleep(30)"],
                                     env=os.environ.copy(), cwd=tmp_path, capture=capture,
                                     rollout_home=tmp_path / "sessions", timeout=5)
    assert capture.stat().st_size <= 128
    assert time.monotonic() - before < 5


@pytest.mark.skipif(sys.platform != "linux", reason="Linux native per-file resource limit")
def test_native_rollout_writer_is_capped_while_caller_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(qualification, "MAX_CAPTURE", 2048)
    rollouts = tmp_path / "sessions"
    rollouts.mkdir()
    capture = tmp_path / "caller.jsonl"
    data, child, timed_out = qualification.capture_caller(
        [sys.executable, "-c", "import os; f=os.open('sessions/rollout-large.jsonl',os.O_CREAT|os.O_WRONLY,0o600); os.write(f,b'x'*8192); os.write(f,b'x'*8192)"],
        env=os.environ.copy(), cwd=tmp_path, capture=capture, rollout_home=rollouts, timeout=5)
    assert child.returncode != 0 and not timed_out and data == b""
    assert (rollouts / "rollout-large.jsonl").stat().st_size <= 2048
