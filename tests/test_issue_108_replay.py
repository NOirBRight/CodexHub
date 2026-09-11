"""Exercise semantic evidence replay on every platform, without PowerShell."""
import json
from pathlib import Path

import pytest
import gateway_compat
from validate_issue_108_evidence import EvidenceValidationError, validate_tool_surface_fixture

ROOT = Path(__file__).resolve().parents[1]


def fixture_payload(*, historical=False):
    name = "issue_108_tool_surface_replay.json" if historical else "issue_108_tool_surface_client_owned_replay.json"
    return json.loads((ROOT / 'tests/fixtures' / name).read_text())


def test_old_tool_surface_evidence_cannot_qualify_a_candidate_without_injected_agents():
    # Keep the historical digest intact. Its injected V1 surface no longer
    # qualifies a candidate that preserves the client's declared authority.
    with pytest.raises(EvidenceValidationError, match='tool_surface_prepared_digest_mismatch'):
        validate_tool_surface_fixture(fixture_payload(historical=True), ROOT)


@pytest.mark.parametrize('change', ['allow_extra_properties', 'description', 'tool_name'])
def test_tool_surface_replay_rejects_actual_surface_changes(monkeypatch, change):
    original = gateway_compat.compatible_request_body

    def changed_surface(*args, **kwargs):
        payload = json.loads(original(*args, **kwargs))
        tool = payload['tools'][0]
        if change == 'allow_extra_properties':
            tool['parameters']['additionalProperties'] = {}
        elif change == 'description':
            tool['description'] += ' Changed.'
        else:
            tool['name'] = 'different_tool'
        return json.dumps(payload).encode()

    monkeypatch.setattr(gateway_compat, 'compatible_request_body', changed_surface)
    with pytest.raises(EvidenceValidationError, match='tool_surface_prepared_digest_mismatch'):
        validate_tool_surface_fixture(fixture_payload(), ROOT)


def test_client_owned_surface_has_green_red_green_without_injected_agents():
    result = validate_tool_surface_fixture(fixture_payload(), ROOT)
    assert result["passed"] is True
    assert result["direct_tool_counts"] == {
        "minimal_core": 2, "namespace_200_eager": 202, "namespace_200_deferred_core": 3,
    }
    assert result["case_outcomes"] == {
        "minimal_core": "green", "namespace_200_eager": "red", "namespace_200_deferred_core": "green",
    }
