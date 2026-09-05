"""Current CLI capture regression through the public runtime contract seam."""
import copy
import json
from pathlib import Path

import pytest

from collaboration_runtime_contract import (
    COLLABORATION_V1,
    CollaborationContractError,
    classify_collaboration_tools,
)


def captured_tools():
    return json.loads((Path(__file__).parent / "fixtures/collaboration/codex-cli-0.153.4-windows-v1.json").read_text())


@pytest.mark.parametrize("configured_role", [False, True])
def test_cli_153_v1_without_service_tier(configured_role):
    tools = captured_tools()
    spawn = next(t for t in tools[0]["tools"] if t["name"] == "spawn_agent")
    if configured_role:
        spawn["parameters"]["properties"]["agent_type"] = {"type": "string"}
    assert classify_collaboration_tools(tools) == COLLABORATION_V1


@pytest.mark.parametrize("mutation", ["unexpected", "wrong_tier_type", "required_tier", "missing_message", "strict"])
def test_cli_153_variants_keep_contract_validation(mutation):
    tools = copy.deepcopy(captured_tools())
    spawn = next(t for t in tools[0]["tools"] if t["name"] == "spawn_agent")
    parameters = spawn["parameters"]
    if mutation == "unexpected":
        parameters["properties"]["unexpected"] = {"type": "string"}
    elif mutation == "wrong_tier_type":
        parameters["properties"]["service_tier"] = {"type": "number"}
    elif mutation == "required_tier":
        parameters["required"] = ["service_tier"]
    elif mutation == "missing_message":
        del parameters["properties"]["message"]
    else:
        spawn["strict"] = True
    with pytest.raises(CollaborationContractError):
        classify_collaboration_tools(tools)


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_cli_153_actual_platform_v1_and_v2_captures(platform):
    captures = json.loads((Path(__file__).parent / f"fixtures/collaboration/codex-cli-0.153.4-{platform}.json").read_text())
    for capture in captures:
        assert classify_collaboration_tools(capture["tools"]) == (
            "collaboration_v2" if capture["v2_enabled"] else COLLABORATION_V1
        )
