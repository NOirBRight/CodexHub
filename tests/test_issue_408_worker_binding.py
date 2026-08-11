import json
import tempfile
import unittest
from pathlib import Path

import codex_proxy
import codex_semantic_adapter
import worker_binding_signing


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

    def test_compatible_response_body_maps_general_agent_type_to_default(self):
        """Bug 1: "general" is rewritten to "default" before reaching the native runtime."""
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
            codex_proxy.compatible_response_body(body, "ollama_cloud")
        )

        args = json.loads(transformed["output"][0]["arguments"])
        self.assertEqual(args["agent_type"], "default")

    def test_compatible_request_body_accepts_native_worker_spawn_history(self):
        """Bug 2: native-style spawn output without effective_binding does not poison history."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            original_root = codex_proxy.WORKER_BINDING_SIGNING_ROOT
            codex_proxy.WORKER_BINDING_SIGNING_ROOT = tmp_path
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
                                "name": "multi_agent_v1__spawn_agent",
                                "arguments": json.dumps(
                                    {
                                        "agent_type": "worker",
                                        "message": "do work",
                                        "model": "glm-5.2",
                                        "reasoning_effort": "high",
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
                    codex_proxy.compatible_request_body(body, upstream, model_id="glm-5.2")
                )
                forwarded_arguments = json.loads(payload["input"][1]["arguments"])
                self.assertNotIn(
                    "_codexhub_worker_requested_binding",
                    forwarded_arguments,
                )
                self.assertEqual(payload["input"][-1]["content"], "continue")
            finally:
                codex_proxy.WORKER_BINDING_SIGNING_ROOT = original_root


class Issue408SemanticAdapterTests(unittest.TestCase):
    """Unit-level coverage for the semantic helpers introduced for #408."""

    def test_normalize_multi_agent_arguments_maps_general_to_default(self):
        value, tool_name, changed = codex_semantic_adapter.normalize_multi_agent_arguments(
            '{"message":"do work","agent_type":"general"}',
            "spawn_agent",
        )

        self.assertTrue(changed)
        self.assertEqual(tool_name, "spawn_agent")
        self.assertEqual(json.loads(value)["agent_type"], "default")

    def test_synthesize_effective_worker_binding_readback_fills_native_output(self):
        requested = {
            "agent_type": "worker",
            "model": "glm-5.2",
            "reasoning": "high",
        }
        native_output = {"agent_id": "019f-child", "nickname": "child"}

        readback = codex_semantic_adapter.synthesize_effective_worker_binding_readback(
            requested, native_output
        )

        self.assertIn("effective_binding", readback)
        effective = readback["effective_binding"]
        self.assertEqual(effective["contract_version"], "codexhub.worker-binding.v1")
        self.assertEqual(effective["support"], "supported")
        self.assertEqual(effective["status"], "accepted")
        self.assertEqual(effective["agent_type"], "worker")
        self.assertEqual(effective["model"], "glm-5.2")
        self.assertEqual(effective["reasoning"], "high")

    def test_synthesize_effective_worker_binding_readback_accepts_nullable_native_nickname(self):
        requested = {
            "agent_type": "worker",
            "model": "glm-5.2",
            "reasoning": "high",
        }
        native_output = {"agent_id": "019f-child", "nickname": None}

        readback = codex_semantic_adapter.synthesize_effective_worker_binding_readback(
            requested, native_output
        )

        self.assertIsNotNone(readback)
        self.assertIn("effective_binding", readback)

    def test_synthesize_effective_worker_binding_readback_preserves_existing_readback(self):
        requested = {
            "agent_type": "worker",
            "model": "glm-5.2",
            "reasoning": "high",
        }
        existing = {
            "effective_binding": {
                "contract_version": "codexhub.worker-binding.v1",
                "support": "supported",
                "status": "accepted",
                "agent_type": "worker",
                "model": "glm-5.2",
                "reasoning": "high",
            }
        }

        readback = codex_semantic_adapter.synthesize_effective_worker_binding_readback(
            requested, existing
        )

        self.assertIs(readback, existing)
