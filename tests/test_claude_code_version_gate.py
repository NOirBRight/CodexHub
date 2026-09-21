from scripts.claude_code_version_gate import report


def test_version_gate_matches_pin() -> None:
    payload = report("2.1.278 (Claude Code)")
    assert payload["match"] is True
    assert payload["drift"] is False


def test_version_gate_reports_drift_without_secrets() -> None:
    payload = report("2.1.300 (Claude Code)")
    assert payload["drift"] is True
    assert payload["found"] == "2.1.300"
    assert "ANTHROPIC" not in repr(payload)
    assert "sk-" not in repr(payload)
