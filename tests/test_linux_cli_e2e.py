from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "e2e_linux_cli_clients", ROOT / "scripts" / "e2e_linux_cli_clients.py"
)
assert SPEC and SPEC.loader
E2E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(E2E)
CONTRACT = json.loads(E2E.CLI_CONTRACT_PATH.read_text(encoding="utf-8"))
CHAT_CONTRACT = json.loads(
    (ROOT / "scripts" / "real_client_chat_contract.v1.json").read_text(encoding="utf-8")
)
CURRENT_OFFICIAL_MODEL = "gpt-6-luna"


@pytest.mark.parametrize("client", ["codex", "opencode", "pi", "omp"])
def test_echoed_prompt_is_not_a_successful_assistant_turn(client):
    sentinel = "SENTINEL:test"
    output = json.dumps(
        {
            "type": "agent_end",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": sentinel}]},
                {"role": "assistant", "content": [], "stopReason": "error"},
            ],
        }
    )
    assert not E2E.assistant_returned_sentinel(client, output, sentinel)


@pytest.mark.parametrize(
    ("client", "events"),
    [
        (
            "codex",
            [
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "/bin/bash -lc 'cat ./sentinel.txt'",
                        "status": "completed",
                        "exit_code": 0,
                    },
                },
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "SENTINEL:test"},
                },
                {"type": "turn.completed"},
            ],
        ),
        (
            "opencode",
            [
                {
                    "type": "tool_use",
                    "part": {
                        "type": "tool",
                        "tool": "read",
                        "state": {
                            "status": "completed",
                            "input": {"filePath": "./sentinel.txt"},
                        },
                    },
                },
                {"type": "text", "part": {"type": "text", "text": "SENTINEL:test"}},
                {"type": "step_finish", "part": {"reason": "stop"}},
            ],
        ),
        *[
            (
                client,
                [
                    {
                        "type": "tool_execution_end",
                        "toolName": "read",
                        "input": {"path": "./sentinel.txt"},
                        "isError": False,
                    },
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "stopReason": "stop",
                            "content": [{"type": "text", "text": "SENTINEL:test"}],
                        },
                    },
                    {"type": "agent_end"},
                ],
            )
            for client in ("pi", "omp")
        ],
    ],
)
def test_real_client_jsonl_shape_requires_tool_sentinel_and_terminal(client, events, tmp_path):
    sentinel = "SENTINEL:test"
    case_root = tmp_path / client
    case_root.mkdir()
    (case_root / "sentinel.txt").write_text(sentinel + "\n", encoding="utf-8")
    output = "\n".join(json.dumps(event) for event in events)

    evidence = E2E.parse_client_output(client, output, sentinel, case_root)

    assert evidence["tool_call_count"] == 1
    assert evidence["read_only_tool_call_count"] == 1
    assert evidence["sentinel_chunk_count"] == 1
    assert evidence["terminal_count"] == 1
    assert evidence["terminal_classification"] == "completed"
    assert evidence["error_event_count"] == 0


def test_codex_read_command_resolves_from_case_root_and_rejects_shell_extras(tmp_path, monkeypatch):
    case_root = tmp_path / "isolated" / "case"
    case_root.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    assert E2E._read_only_sentinel_command("cat ./sentinel.txt", case_root)
    assert E2E._read_only_sentinel_command(
        "/bin/bash -lc 'cat ./sentinel.txt'", case_root
    )
    assert not E2E._read_only_sentinel_command(
        "/bin/bash -lc 'cat ./sentinel.txt; touch unexpected'", case_root
    )


def test_pi_tool_target_requires_exact_path_and_correlated_start():
    end = {"type": "tool_execution_end", "toolName": "read", "toolCallId": "read-1", "isError": False}
    start = {"type": "tool_execution_start", "toolCallId": "read-1", "args": {"path": "./sentinel.txt"}}
    assert E2E.parse_client_output("pi", json.dumps(end), "S")["read_only_tool_call_count"] == 0
    output = "\n".join(json.dumps(e) for e in (start, end))
    assert E2E.parse_client_output("pi", output, "S")["read_only_tool_call_count"] == 1
    start["args"]["path"] = "not-sentinel.txt"
    output = "\n".join(json.dumps(e) for e in (start, end))
    assert E2E.parse_client_output("pi", output, "S")["read_only_tool_call_count"] == 0


def test_arbitrary_model_and_terminal_text_are_not_saved_in_evidence():
    secret = "unexpected-private-value"
    gateway = E2E._gateway_evidence(E2E.CASES[0], [
        {"event": "request_complete", "model": secret, "request_id": "private-id"},
    ], 0, 2)
    output = "\n".join(json.dumps(e) for e in [
        {"type": "message_end", "message": {"role": "assistant", "stopReason": secret}},
        {"type": "agent_end"},
    ])
    client = E2E.parse_client_output("pi", output, "S")
    assert gateway["model_matches_expected"] is False
    assert client["terminal_classification"] == "unclassified"
    assert secret not in json.dumps([gateway, client])
    assert "private-id" not in json.dumps(gateway)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("codex 0.144.5", (0, 144, 5)),
        ("v1.18.4", (1, 18, 4)),
        ("1.2.3-beta.1", None),
        ("1.2.3.4", None),
        ("1.2.3 and 4.5.6", None),
    ],
)
def test_client_version_parser_accepts_one_stable_three_part_version(value, expected):
    assert E2E._version_tuple(value) == expected


@pytest.mark.skipif(os.name == "nt", reason="Linux process-group cleanup gate")
def test_client_timeout_kills_descendant_process_group(tmp_path):
    marker = tmp_path / "descendant-survived"
    child = "import pathlib,sys,time; time.sleep(.5); pathlib.Path(sys.argv[1]).touch()"
    parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); time.sleep(10)"
    result = E2E._run_client_bounded(
        [E2E.sys.executable, "-c", parent, child, str(marker)],
        env=os.environ.copy(),
        cwd=tmp_path,
        timeout=0.1,
        input_text=None,
    )

    assert result["timed_out"] is True
    time.sleep(0.6)
    assert not marker.exists()


@pytest.mark.parametrize("case", E2E.CASES, ids=lambda case: case.case_id)
def test_preview_and_apply_use_fresh_roots_and_readback_uses_apply(case, tmp_path, monkeypatch):
    roots = {}

    def managed(_binary, verb, _case, root, *_inputs):
        roots[verb] = root
        marker = root / "runtime"
        if verb == "readback":
            assert marker.is_dir()
        else:
            assert not marker.exists(), "isolated root is not fresh"
            marker.mkdir(parents=True)
        return {"ok": True, "returncode": 0}

    monkeypatch.setattr(E2E, "_managed", managed)
    monkeypatch.setattr(E2E, "_client_launch", lambda *args: {"ok": False})
    monkeypatch.setattr(E2E, "_gateway_events_for_attempt", lambda *args, **kwargs: ([], 0))
    result, _, _ = E2E._run_case_attempt(
        case, binary=tmp_path / "candidate", work=tmp_path,
        env={"CODEXHUB_RUNTIME_HOME": str(tmp_path / "runtime")},
        settings=tmp_path / "settings.json", providers=tmp_path / "providers.toml",
        catalog=tmp_path / "catalog.json", timeout=1,
    )

    assert all(result[verb]["ok"] for verb in ("preview", "apply", "readback"))
    assert roots["preview"] != roots["apply"]
    assert roots["apply"] == roots["readback"]
    assert (roots["preview"] / "runtime").is_dir()


def test_retried_client_attempt_gets_a_fresh_case_home(tmp_path, monkeypatch):
    case = E2E.CASES[0]
    roots = []

    def launch(_case, _managed_root, case_root, _env, _timeout):
        roots.append(case_root)
        (case_root / "home").mkdir()
        return {"ok": True}

    monkeypatch.setattr(E2E, "_client_launch", launch)
    monkeypatch.setattr(E2E, "_gateway_events_for_attempt", lambda *args, **kwargs: ([], 0))
    monkeypatch.setattr(E2E, "_gateway_evidence", lambda *args, **kwargs: {})
    monkeypatch.setattr(E2E, "_attempt_passed", lambda *args, **kwargs: True)
    env = {"CODEXHUB_RUNTIME_HOME": str(tmp_path / "runtime")}

    for attempt in (1, 2):
        E2E._run_case_attempt(
            case,
            binary=tmp_path / "candidate",
            work=tmp_path,
            env=env,
            settings=tmp_path / "settings.json",
            providers=tmp_path / "providers.toml",
            catalog=tmp_path / "catalog.json",
            timeout=1,
            configure=False,
            attempt_number=attempt,
        )

    assert roots[0] != roots[1]


def test_cli_contract_is_versioned_and_complete() -> None:
    assert CONTRACT["schema"] == "codexhub.real-client-cli-contract.v1"
    assert set(CONTRACT["minimum_versions"]) == {"codex_cli", "opencode", "pi", "omp"}
    assert {
        case["client"] for case in CONTRACT["cases"]
    } == {"codex_cli", "opencode", "pi", "omp"}
    assert {case["provider_id"] for case in CONTRACT["cases"]} == {
        "official",
        "deepseek",
    }
    assert len(CONTRACT["cases"]) == 8
    assert len({case["case_ids"]["linux"] for case in CONTRACT["cases"]}) == 8
    assert len({case["case_ids"]["windows"] for case in CONTRACT["cases"]}) == 8
    required_evidence = {
        "case_id",
        "client",
        "provider_id",
        "client_selector",
        "canonical_model",
        "gateway_model",
        "endpoint_binding",
        "protocol",
        "outcome",
    }
    assert required_evidence <= set(CONTRACT["evidence_fields"])


def test_linux_cli_matrix_covers_four_clients_and_two_required_models() -> None:
    linux = CONTRACT["platforms"]["linux"]
    expected_cases = {
        (linux["client_names"][case["client"]], linux["provider_names"][case["provider_id"]])
        for case in CONTRACT["cases"]
    }
    assert {(case.client, case.provider) for case in E2E.CASES} == expected_cases
    assert {case.gateway_model for case in E2E.CASES} == {
        case.get("platform_overrides", {}).get("linux", {}).get("gateway", case["models"]["gateway"])
        for case in CONTRACT["cases"]
    }


def test_client_selectors_keep_gateway_provider_identity() -> None:
    linux = CONTRACT["platforms"]["linux"]
    for case, expected in zip(E2E.CASES, CONTRACT["cases"], strict=True):
        models = expected["models"]
        overrides = expected.get("platform_overrides", {}).get("linux", {})
        assert case.client == linux["client_names"][expected["client"]]
        assert case.provider == linux["provider_names"][expected["provider_id"]]
        assert case.case_id == expected["case_ids"]["linux"]
        assert case.managed_model == models["managed"]
        assert case.selector == models["selector"]
        assert case.gateway_model == overrides.get("gateway", models["gateway"])


def test_deepseek_contract_uses_deepseek_flash_and_responses_route() -> None:
    deepseek_cases = [case for case in CONTRACT["cases"] if case["provider_id"] == "deepseek"]
    assert {case["client"] for case in deepseek_cases} == {
        "codex_cli",
        "opencode",
        "pi",
        "omp",
    }
    for case in deepseek_cases:
        assert case["diagnostic_provider_id"] == "deepseek"
        assert case["models"] == {
            "managed": "deepseek/deepseek-flash",
            "selector": "codexhub-deepseek/deepseek-flash",
            "canonical": "deepseek/deepseek-flash",
            "gateway": "deepseek/deepseek-flash",
        }
        assert case["endpoint_binding"] == "/v1/providers/deepseek/responses"
        assert case["protocol"] == "responses"
    assert {case["case_ids"]["linux"] for case in deepseek_cases} == {
        "codex-deepseek",
        "opencode-deepseek",
        "pi-deepseek",
        "omp-deepseek",
    }
    assert {case["case_ids"]["windows"] for case in deepseek_cases} == {
        "codex-cli-deepseek",
        "opencode-deepseek",
        "pi-deepseek",
        "omp-deepseek",
    }


def test_official_model_identity_matches_chat_contract() -> None:
    official_cases = [case for case in CONTRACT["cases"] if case["provider_id"] == "official"]
    assert {case["client"] for case in official_cases} == {
        "codex_cli",
        "opencode",
        "pi",
        "omp",
    }
    for case in official_cases:
        models = case["models"]
        if case["client"] == "codex_cli":
            assert models == {
                "managed": CURRENT_OFFICIAL_MODEL,
                "selector": CURRENT_OFFICIAL_MODEL,
                "canonical": CURRENT_OFFICIAL_MODEL,
                "gateway": CURRENT_OFFICIAL_MODEL,
            }
        else:
            assert models == {
                "managed": f"openai/{CURRENT_OFFICIAL_MODEL}",
                "selector": f"codexhub-openai/{CURRENT_OFFICIAL_MODEL}",
                "canonical": f"codexhub-openai/{CURRENT_OFFICIAL_MODEL}",
                "gateway": f"openai/{CURRENT_OFFICIAL_MODEL}",
            }
            assert case["platform_overrides"]["linux"]["gateway"] == CURRENT_OFFICIAL_MODEL

    official_chat = next(
        case for case in CHAT_CONTRACT["cases"] if case["provider_id"] == "official"
    )
    assert official_chat["model"] == CURRENT_OFFICIAL_MODEL


def test_linux_cli_deepseek_credentials_fail_closed(tmp_path: Path) -> None:
    assert E2E.load_deepseek_api_key(None) == ""
    missing = tmp_path / "missing.json"
    with pytest.raises(ValueError, match="missing DeepSeek credentials"):
        E2E.load_deepseek_api_key(missing)
    bad_schema = tmp_path / "bad.json"
    bad_schema.write_text('{"schema":"other","api_key":"x"}', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid DeepSeek credential schema"):
        E2E.load_deepseek_api_key(bad_schema)
    good = tmp_path / "good.json"
    good.write_text(
        json.dumps(
            {
                "schema": "codexhub.real-client-deepseek.v1",
                "api_key": "  live-key  ",
            }
        ),
        encoding="utf-8",
    )
    assert E2E.load_deepseek_api_key(good) == "live-key"
