"""Evidence input and pre-dispatch budget contract for Claude campaign #557."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Thread

import pytest

from evidence_input_budget import (
    BudgetRefused,
    EvidenceInput,
    EvidenceInputError,
    materialize_credential,
)

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
ORIGIN = "https://synthetic.example.test"


def _credential(tmp_path: Path, *, target_origin: str = ORIGIN, secret: str = "never-log-me") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "credential.json"
    path.write_text(
        json.dumps(
            {
                "schema": "codexhub.synthetic-credential.v1",
                "target_origin": target_origin,
                "secret": secret,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _contract(
    tmp_path: Path,
    *,
    decision: str = "approved",
    expires_at: datetime = NOW + timedelta(minutes=10),
    protocol: str = "responses",
    model: str = "synthetic-model",
    token_field: str = "max_output_tokens",
    reasoning_effort: str | None = "max",
    endpoint: str = ORIGIN,
    credential_origin: str = ORIGIN,
    deadline_seconds: int = 1800,
    max_attempts: int = 20,
    credential_path: Path | None = None,
) -> Path:
    credential_path = credential_path or _credential(tmp_path, target_origin=credential_origin)
    payload = {
        "schema": "codexhub.claude-evidence-input.v1",
        "limits": {
            "max_attempts_per_protocol": max_attempts,
            "max_output_tokens": 2048,
            "deadline_seconds": deadline_seconds,
        },
        "grant": {
            "schema": "codexhub.claude-evidence-grant.v1",
            "round_id": "synthetic-round-1",
            "issued_at": NOW.isoformat(),
            "expires_at": expires_at.isoformat(),
            "claim_path": str(tmp_path / f"grant-{protocol}-{token_field}.claim"),
            "legs": {"leg-1": {"decision": decision}},
        },
        "legs": [
            {
                "id": "leg-1",
                "protocol": protocol,
                "provider": "synthetic-provider",
                "model": model,
                "reasoning_effort": reasoning_effort,
                "token_field": token_field,
                "endpoint": endpoint,
                "routes": {
                    "/v1/responses": ["POST"],
                    "/v1/chat/completions": ["POST"],
                    "/v1/messages": ["POST"],
                    "/v1/messages/count_tokens": ["POST"],
                    "/v1/models": ["GET", "HEAD"],
                },
                "credential": (
                    {
                        "path": str(credential_path),
                        "schema": "codexhub.synthetic-credential.v1",
                        "target_origin": credential_origin,
                        "secret_fields": ["secret"],
                    }
                    if decision == "approved"
                    else None
                ),
            }
        ],
    }
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "evidence-input.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _body(model: str = "synthetic-model", **extra: object) -> bytes:
    return json.dumps({"model": model, **extra}, separators=(",", ":")).encode()


def _load(path: Path, *, clock: list[float] | None = None, wall: list[datetime] | None = None) -> EvidenceInput:
    clock = clock or [100.0]
    wall = wall or [NOW]
    return EvidenceInput.load(path, monotonic=lambda: clock[0], wall_clock=lambda: wall[0])


def test_load_requires_explicit_fresh_grant_and_redacts_credential(tmp_path: Path) -> None:
    inputs = _load(_contract(tmp_path))

    summary = inputs.redacted_summary()
    assert summary["grant"]["round_id"] == "synthetic-round-1"
    assert summary["legs"][0]["credential_present"] is True
    assert "never-log-me" not in repr(summary)
    assert str(_credential(tmp_path)) not in repr(summary)


def test_undecided_leg_is_explicit_and_refused_before_dispatch(tmp_path: Path) -> None:
    inputs = _load(_contract(tmp_path, decision="undecided"))

    assert inputs.redacted_summary()["legs"][0]["authorization"] == "undecided"
    with pytest.raises(BudgetRefused, match="undecided"):
        inputs.reserve(
            "responses", "POST", f"{ORIGIN}/v1/responses", _body(max_output_tokens=32)
        )
    assert inputs.admission_summary()["counts"] == {}


def test_refused_leg_may_have_no_credential_and_stays_refused(tmp_path: Path) -> None:
    path = _contract(tmp_path, decision="refused")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["legs"][0]["credential"] = None
    path.write_text(json.dumps(payload), encoding="utf-8")
    inputs = _load(path)

    summary = inputs.redacted_summary()
    assert summary["legs"][0]["credential_present"] is False
    with pytest.raises(BudgetRefused, match="refused"):
        inputs.reserve("responses", "POST", f"{ORIGIN}/v1/responses", _body(max_output_tokens=1, reasoning={"effort": "max"}))


def test_expired_grant_is_refused_without_a_new_round(tmp_path: Path) -> None:
    clock = [100.0]
    wall = [NOW + timedelta(minutes=11)]
    inputs = _load(_contract(tmp_path), clock=clock, wall=wall)

    with pytest.raises(BudgetRefused, match="expired"):
        inputs.reserve(
            "responses", "POST", f"{ORIGIN}/v1/responses", _body(max_output_tokens=32)
        )
    wall[0] = NOW
    with pytest.raises(BudgetRefused, match="expired"):
        inputs.reserve(
            "responses", "POST", f"{ORIGIN}/v1/responses", _body(max_output_tokens=32)
        )
    assert inputs.redacted_summary()["grant"]["round_id"] == "synthetic-round-1"
    assert inputs.admission_summary()["counts"] == {}


def test_each_protocol_uses_its_declared_output_field(tmp_path: Path) -> None:
    for protocol, token_field in (
        ("anthropic_messages", "max_tokens"),
        ("responses", "max_output_tokens"),
        ("chat_completions", "max_tokens"),
        ("chat_completions", "max_completion_tokens"),
    ):
        path = _contract(tmp_path / protocol.replace("_", "-"), protocol=protocol, token_field=token_field)
        inputs = _load(path)
        route = {
            "anthropic_messages": "/v1/messages",
            "responses": "/v1/responses",
            "chat_completions": "/v1/chat/completions",
        }[protocol]
        reasoning = (
            {"output_config": {"effort": "max"}, "thinking": {"type": "adaptive", "display": "summary"}}
            if protocol == "anthropic_messages"
            else {"reasoning": {"effort": "max"}}
        )
        inputs.reserve(protocol, "POST", f"{ORIGIN}{route}", _body(**{token_field: 32, **reasoning}))


def test_native_reasoning_uses_canonical_output_config_effort(tmp_path: Path) -> None:
    inputs = _load(_contract(tmp_path, protocol="anthropic_messages", token_field="max_tokens"))
    canonical = _body(
        max_tokens=32,
        output_config={"effort": "max"},
        thinking={"type": "adaptive", "display": "summary"},
    )
    inputs.reserve("anthropic_messages", "POST", f"{ORIGIN}/v1/messages", canonical)

    for body in (
        _body(max_tokens=32, thinking={"effort": "max"}),
        _body(max_tokens=32, output_config={"format": {"type": "text"}}, thinking={"type": "adaptive"}),
        _body(max_tokens=32, output_config={"effort": 1}, thinking={"type": "adaptive"}),
        _body(max_tokens=32, output_config={"effort": "low"}, thinking={"type": "adaptive"}),
        _body(
            max_tokens=32,
            output_config={"effort": "max"},
            thinking={"effort": "low"},
        ),
    ):
        with pytest.raises(BudgetRefused, match="reasoning"):
            inputs.reserve("anthropic_messages", "POST", f"{ORIGIN}/v1/messages", body)
    assert inputs.admission_summary()["counts"] == {"anthropic_messages": 1}


def test_native_unbound_output_config_controls_are_not_rejected(tmp_path: Path) -> None:
    inputs = _load(
        _contract(
            tmp_path,
            protocol="anthropic_messages",
            token_field="max_tokens",
            reasoning_effort=None,
        )
    )
    inputs.reserve(
        "anthropic_messages",
        "POST",
        f"{ORIGIN}/v1/messages",
        _body(
            max_tokens=32,
            output_config={"format": {"type": "text"}},
            thinking={"type": "adaptive", "display": "summary"},
        ),
    )
    assert inputs.admission_summary()["counts"] == {"anthropic_messages": 1}


def test_unknown_or_ambiguous_token_field_fails_closed(tmp_path: Path) -> None:
    inputs = _load(_contract(tmp_path, protocol="responses", token_field="max_output_tokens"))
    for body in (
        _body(max_tokens=32),
        _body(max_output_tokens=32, max_tokens=32),
        _body(max_output_tokens=32, max_new_tokens=32),
        _body(max_output_tokens="32"),
        _body(),
    ):
        with pytest.raises(BudgetRefused, match="token"):
            inputs.reserve("responses", "POST", f"{ORIGIN}/v1/responses", body)
    assert inputs.admission_summary()["counts"] == {}


def test_count_tokens_auxiliary_body_may_omit_output_limit(tmp_path: Path) -> None:
    inputs = _load(_contract(tmp_path, protocol="anthropic_messages", token_field="max_tokens"))
    inputs.reserve(
        "anthropic_messages",
        "POST",
        f"{ORIGIN}/v1/messages/count_tokens",
        _body(),
    )
    assert inputs.admission_summary()["counts"] == {"anthropic_messages": 1}


def test_remote_plaintext_endpoint_is_rejected(tmp_path: Path) -> None:
    path = _contract(
        tmp_path,
        endpoint="http://upstream.example.test",
        credential_origin="http://upstream.example.test",
    )
    with pytest.raises(EvidenceInputError, match="plaintext"):
        _load(path)


def test_contract_and_credential_json_reject_duplicate_or_nonfinite_values(tmp_path: Path) -> None:
    contract = _contract(tmp_path / "contract")
    text = contract.read_text(encoding="utf-8")
    duplicate = text.replace(
        '"round_id": "synthetic-round-1"',
        '"round_id": "synthetic-round-1", "round_id": "other-round"',
        1,
    )
    contract.write_text(duplicate, encoding="utf-8")
    with pytest.raises(EvidenceInputError, match="invalid evidence input JSON"):
        EvidenceInput.load(contract)

    nan_contract = _contract(tmp_path / "nan")
    nan_text = nan_contract.read_text(encoding="utf-8")
    nan_contract.write_text(nan_text[:-1] + ', "nonfinite": NaN}', encoding="utf-8")
    with pytest.raises(EvidenceInputError, match="invalid evidence input JSON"):
        EvidenceInput.load(nan_contract)

    credential_contract = _contract(tmp_path / "credential")
    credential_path = tmp_path / "credential" / "credential.json"
    credential_text = credential_path.read_text(encoding="utf-8")
    credential_path.write_text(
        credential_text.replace('"secret": "never-log-me"', '"secret": "one", "secret": "two"'),
        encoding="utf-8",
    )
    with pytest.raises(EvidenceInputError, match="invalid explicit credential"):
        EvidenceInput.load(credential_contract)


def test_duplicate_json_keys_and_bool_token_limits_refuse(tmp_path: Path) -> None:
    inputs = _load(_contract(tmp_path))
    duplicate = b'{"model":"synthetic-model","max_output_tokens":1,"max_output_tokens":2}'
    for body in (duplicate, _body(max_output_tokens=True), _body(max_output_tokens=32, value=float("nan"))):
        with pytest.raises(BudgetRefused, match="body|token"):
            inputs.reserve("responses", "POST", f"{ORIGIN}/v1/responses", body)
    assert inputs.admission_summary()["counts"] == {}


def test_configured_reasoning_selection_is_required_on_ordinary_requests(tmp_path: Path) -> None:
    inputs = _load(_contract(tmp_path))
    for body in (
        _body(max_output_tokens=32),
        _body(max_output_tokens=32, reasoning={"effort": "low"}),
    ):
        with pytest.raises(BudgetRefused, match="reasoning"):
            inputs.reserve("responses", "POST", f"{ORIGIN}/v1/responses", body)
    assert inputs.admission_summary()["counts"] == {}


def test_model_endpoint_and_reasoning_are_bound_without_rewriting_body(tmp_path: Path) -> None:
    inputs = _load(_contract(tmp_path))
    body = _body(max_output_tokens=32, reasoning={"effort": "max"})
    inputs.reserve("responses", "POST", f"{ORIGIN}/v1/responses?attempt=1", body)

    for bad_url, bad_body in (
        ("https://other.example.test/v1/responses", body),
        (f"{ORIGIN}/v1/responses", _body(model="other-model", max_output_tokens=32)),
        (
            f"{ORIGIN}/v1/responses",
            _body(max_output_tokens=32, reasoning={"effort": "low"}),
        ),
    ):
        with pytest.raises(BudgetRefused):
            inputs.reserve("responses", "POST", bad_url, bad_body)
    assert inputs.admission_summary()["counts"] == {"responses": 1}


def test_n_plus_one_is_refused_before_the_attempt(tmp_path: Path) -> None:
    inputs = _load(_contract(tmp_path, max_attempts=2))
    body = _body(max_output_tokens=32, reasoning={"effort": "max"})
    for _ in range(2):
        inputs.reserve("responses", "POST", f"{ORIGIN}/v1/responses", body)
    with pytest.raises(BudgetRefused, match="attempt"):
        inputs.reserve("responses", "POST", f"{ORIGIN}/v1/responses", body)
    assert inputs.admission_summary()["counts"] == {"responses": 2}


def test_reserve_is_atomic_under_thread_race(tmp_path: Path) -> None:
    inputs = _load(_contract(tmp_path, max_attempts=20))
    barrier = Barrier(40)
    accepted: list[int] = []
    refused: list[str] = []

    def attempt() -> None:
        barrier.wait()
        try:
            reservation = inputs.reserve(
                "responses", "POST", f"{ORIGIN}/v1/responses", _body(max_output_tokens=32, reasoning={"effort": "max"})
            )
            accepted.append(reservation.attempt)
        except BudgetRefused as error:
            refused.append(str(error))

    threads = [Thread(target=attempt) for _ in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(accepted) == 20
    assert len(refused) == 20
    assert sorted(accepted) == list(range(1, 21))
    assert inputs.admission_summary()["counts"] == {"responses": 20}


def test_deadline_is_monotonic_and_does_not_reset_after_refusal(tmp_path: Path) -> None:
    clock = [100.0]
    inputs = _load(_contract(tmp_path, deadline_seconds=2), clock=clock)
    body = _body(max_output_tokens=32, reasoning={"effort": "max"})
    inputs.reserve("responses", "POST", f"{ORIGIN}/v1/responses", body)
    clock[0] = 102.0
    with pytest.raises(BudgetRefused, match="deadline"):
        inputs.reserve("responses", "POST", f"{ORIGIN}/v1/responses", body)
    clock[0] = 100.5
    with pytest.raises(BudgetRefused, match="deadline"):
        inputs.reserve("responses", "POST", f"{ORIGIN}/v1/responses", body)
    assert inputs.admission_summary()["counts"] == {"responses": 1}


def test_grant_claim_blocks_reload_in_process_and_subprocess(tmp_path: Path) -> None:
    path = _contract(tmp_path, max_attempts=2)
    inputs = _load(path)
    inputs.reserve("responses", "POST", f"{ORIGIN}/v1/responses", _body(max_output_tokens=1, reasoning={"effort": "max"}))

    with pytest.raises(EvidenceInputError, match="already been claimed"):
        EvidenceInput.load(path)

    source_root = Path(__file__).resolve().parents[1]
    code = "from evidence_input_budget import EvidenceInput; EvidenceInput.load(__import__('sys').argv[1])"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source_root / "src-python")
    result = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "already been claimed" in result.stderr


def test_credential_target_mismatch_refuses(tmp_path: Path) -> None:
    path = _contract(tmp_path, credential_origin="https://other.example.test")
    with pytest.raises(EvidenceInputError, match="credential target"):
        _load(path)


def test_owner_only_helpers_refuse_before_creating_files_without_fchmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _credential(tmp_path, secret="synthetic-only-secret")
    destination = tmp_path / "isolated" / "credential.json"
    destination.parent.mkdir()
    monkeypatch.setattr(os, "fchmod", None, raising=False)

    with pytest.raises(EvidenceInputError, match="owner-only"):
        materialize_credential(source, destination)
    assert not destination.exists()

    contract = _contract(tmp_path / "contract")
    with pytest.raises(EvidenceInputError, match="owner-only"):
        EvidenceInput.load(contract)
    assert not (contract.parent / "grant-responses-max_output_tokens.claim").exists()


def test_materialize_credential_is_owner_only_and_does_not_modify_source(tmp_path: Path) -> None:
    source = _credential(tmp_path, secret="synthetic-only-secret")
    before = hashlib.sha256(source.read_bytes()).digest()
    destination = tmp_path / "isolated" / "credential.json"
    destination.parent.mkdir()

    materialize_credential(source, destination)

    assert destination.read_bytes() == source.read_bytes()
    assert hashlib.sha256(source.read_bytes()).digest() == before
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert "synthetic-only-secret" not in repr(EvidenceInput.load(_contract(tmp_path)).redacted_summary())


def test_materialize_refuses_overwrite(tmp_path: Path) -> None:
    source = _credential(tmp_path)
    destination = tmp_path / "out.json"
    destination.write_text("sentinel", encoding="utf-8")
    with pytest.raises(EvidenceInputError, match="exists"):
        materialize_credential(source, destination)


def test_non_bytes_body_is_rejected_before_dispatch(tmp_path: Path) -> None:
    inputs = _load(_contract(tmp_path))
    with pytest.raises(BudgetRefused, match="immutable bytes"):
        inputs.reserve("responses", "POST", f"{ORIGIN}/v1/responses", bytearray(_body(max_output_tokens=1)))  # type: ignore[arg-type]
    assert inputs.admission_summary()["counts"] == {}
