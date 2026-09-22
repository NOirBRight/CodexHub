"""The POSIX-only evidence helper must not copy secrets on Windows."""

import os

import pytest

from evidence_input_budget import EvidenceInputError, materialize_credential


@pytest.mark.skipif(os.name == "posix", reason="Non-POSIX fail-closed contract")
def test_unsupported_platform_refuses_before_reading_or_creating_credential(tmp_path):
    destination = tmp_path / "credential.json"
    with pytest.raises(EvidenceInputError, match="owner-only"):
        materialize_credential(tmp_path / "absent-source.json", destination)
    assert not destination.exists()
