from scripts.validate_issue_369_matrix import validate


def test_issue_369_matrix_is_sanitized_and_selector_fails_closed() -> None:
    payload = validate()
    assert payload["candidate_revision"]
    assert {row["model"] for row in payload["models"]} == {
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.3-codex-spark",
        "gpt-5.2",
        "gpt-5.4",
        "gpt-5.4-mini",
        "codex-auto-review",
    }
