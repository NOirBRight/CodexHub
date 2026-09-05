"""Exercise semantic evidence replay on every platform, without PowerShell."""
import json
from pathlib import Path

import pytest
import gateway_compat
from validate_issue_108_evidence import EvidenceValidationError, validate_tool_surface_fixture

ROOT = Path(__file__).resolve().parents[1]


def fixture_payload():
    return json.loads((ROOT / 'tests/fixtures/issue_108_tool_surface_replay.json').read_text())


def test_tool_surface_replay_retains_original_evidence():
    assert validate_tool_surface_fixture(fixture_payload(), ROOT)['passed'] is True


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
