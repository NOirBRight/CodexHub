import json
import tempfile
import unittest
from pathlib import Path

import codex_semantic_adapter
import worker_binding_signing
import collaboration_adapter
import gateway_compat


class Issue408WorkerBindingRegressionTests(unittest.TestCase):
    """Regression tests for GitHub issue #408.

    Bug 1: the model-facing ``agent_type`` selector "general" was passed
    through to the native Codex runtime, which only accepts "worker",
    "default" and "explorer".

    Bug 2: a successful native worker spawn only returns
    ``{"agent_id", "nickname"}``, but history re-validation required an
    ``effective_binding`` readback that nothing ever generated, permanently
    poisoning the session.
    """

    def test_compatible_response_body_preserves_unregistered_role_and_alias(self):
        """A spelling without client declaration provenance grants no authority."""
        body = json.dumps(
            {
                "model": "glm-5.2",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps(
                            {"agent_type": "general", "message": "do work"}
                        ),
                    }
                ],
            }
        ).encode("utf-8")

        transformed = json.loads(
            gateway_compat.compatible_response_body(body, "ollama_cloud")
        )

        args = json.loads(transformed["output"][0]["arguments"])
        self.assertEqual(args["agent_type"], "general")
        self.assertEqual(transformed["output"][0]["name"], "multi_agent_v1__spawn_agent")
        self.assertNotIn("namespace", transformed["output"][0])

    def test_compatible_request_body_accepts_native_worker_spawn_history(self):
        """Bug 2: native-style spawn output without effective_binding does not poison history."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            original_root = collaboration_adapter.WORKER_BINDING_SIGNING_ROOT
            collaboration_adapter.WORKER_BINDING_SIGNING_ROOT = tmp_path
            try:
                call_id = "call_worker_1"
                binding = {
                    "contract_version": "codexhub.requested-worker-binding.v1",
                    "agent_type": "worker",
                    "model": "glm-5.2",
                    "reasoning": "high",
                }
                canonical = json.dumps(
                    binding, ensure_ascii=True, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                signature = worker_binding_signing.sign(
                    tmp_path, call_id.encode("utf-8") + b"\0" + canonical
                )
                sidecar = {**binding, "signature": signature}

                body = json.dumps(
                    {
                        "model": "glm-5.2",
                        "input": [
                            {"type": "message", "role": "user", "content": "spawn worker"},
                            {
                                "type": "function_call",
                                "call_id": call_id,
                                "name": "spawn_agent",
                                "namespace": "multi_agent_v1",
                                "arguments": json.dumps(
                                    {
                                        "agent_type": "worker",
                                        "message": "do work",
                                        "model": None,
                                        "_codexhub_worker_requested_binding": sidecar,
                                    }
                                ),
                            },
                            {
                                "type": "function_call_output",
                                "call_id": call_id,
                                "output": json.dumps(
                                    {"agent_id": "019f-child", "nickname": "child"}
                                ),
                            },
                            {"type": "message", "role": "user", "content": "continue"},
                        ],
                        "tools": [
                            {
                                "type": "function",
                                "name": "multi_agent_v1__spawn_agent",
                                "parameters": {"type": "object", "properties": {}},
                            }
                        ],
                    }
                ).encode("utf-8")

                upstream = {
                    "name": "ollama_cloud",
                    "upstream_model": "glm-5.2",
                    "upstream_format": "responses",
                    "tool_protocol": "responses_structured",
                }

                # Must not raise external_worker_binding_rejected.
                payload = json.loads(
                    gateway_compat.compatible_request_body(body, upstream, model_id="glm-5.2")
                )
                forwarded_arguments = json.loads(payload["input"][1]["arguments"])
                self.assertNotIn(
                    "_codexhub_worker_requested_binding",
                    forwarded_arguments,
                )
                self.assertEqual(payload["input"][-1]["content"], "continue")
            finally:
                collaboration_adapter.WORKER_BINDING_SIGNING_ROOT = original_root


class Issue408SemanticAdapterTests(unittest.TestCase):
    """Unit-level coverage for the semantic selector helper from #408."""

    def test_normalize_multi_agent_arguments_maps_general_to_default(self):
        value, tool_name, changed = codex_semantic_adapter.normalize_multi_agent_arguments(
            '{"message":"do work","agent_type":"general"}',
            "spawn_agent",
        )

        self.assertTrue(changed)
        self.assertEqual(tool_name, "spawn_agent")
        self.assertEqual(json.loads(value)["agent_type"], "default")
