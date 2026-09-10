import copy
import pytest
from collaboration_runtime_contract import EXPECTED_PARAMETER_SCHEMAS
from runtime_tool_compatibility import build_tool_compatibility_plan, ProtocolCapabilities


@pytest.mark.parametrize("version,namespace", [("v1", "multi_agent_v1"), ("v2", "collaboration")])
def test_provider_wait_schema_expresses_native_integer_requirement(version, namespace):
    tools = [{"type": "namespace", "name": namespace, "description": "runtime", "tools": [
        {"type": "function", "name": name, "description": "runtime", "strict": False, "parameters": copy.deepcopy(schema)}
        for name, schema in EXPECTED_PARAMETER_SCHEMAS["collaboration_" + version].items()]}]
    original = copy.deepcopy(tools)
    plan = build_tool_compatibility_plan(tools, selected_protocol="chat_tools", tool_choice="auto",
        protocol_capabilities=ProtocolCapabilities(function_lifecycle=True, accepts_namespace_adapter=True))
    encoded = plan.encode_payload({"tools": tools})
    wait = next(t for t in encoded["tools"] if "timeout_ms" in t.get("parameters", {}).get("properties", {}))
    assert wait["parameters"]["properties"]["timeout_ms"]["type"] == "integer"
    assert "300000.0" in wait["parameters"]["properties"]["timeout_ms"]["description"]
    assert tools == original
