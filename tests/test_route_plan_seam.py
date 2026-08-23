"""Seam tests for ``route_plan`` / ``route_primitives``.

Migrated from ``tests/test_routing.py`` (campaign #451 tranche: route selection).
These tests call the extracted planner directly and must not import
the Gateway handler class or underscore-private ``codex_proxy`` members.
"""

from __future__ import annotations

import ast
import os
import unittest
from dataclasses import fields, replace
from pathlib import Path
from unittest.mock import patch

from collaboration_runtime_contract import COLLABORATION_V1, COLLABORATION_V2

import route_plan
import route_primitives
import codex_auth
import gateway_transport


class RoutePlanSeamTests(unittest.TestCase):
    def test_official_codex_app_responses_uses_http_passthrough_profile(self):
        upstream = {"name": "official"}
        context = {"client_id": "codex-app"}

        self.assertEqual(
            route_plan.behavior_profile_for_request(upstream, context, inbound_format="responses"),
            route_primitives.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
        )

    def test_official_chat_completions_uses_gateway_compat_profile(self):
        upstream = {"name": "official"}
        context = {"client_id": "codex-app"}

        self.assertEqual(
            route_plan.behavior_profile_for_request(upstream, context, inbound_format="chat_completions"),
            route_primitives.BEHAVIOR_OFFICIAL_GATEWAY_COMPAT,
        )

    def test_official_unknown_client_uses_gateway_compat_profile(self):
        upstream = {"name": "official"}
        context = {"client_id": "unknown"}

        self.assertEqual(
            route_plan.behavior_profile_for_request(upstream, context, inbound_format="responses"),
            route_primitives.BEHAVIOR_OFFICIAL_GATEWAY_COMPAT,
        )

    def test_third_party_always_uses_external_gateway_profile(self):
        upstream = {"name": "ollama"}
        context = {"client_id": "codex-app"}

        self.assertEqual(
            route_plan.behavior_profile_for_request(upstream, context, inbound_format="responses"),
            route_primitives.BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY,
        )

    def test_route_plan_codex_app_third_party_chat_upstream_uses_codex_adapter_and_wire_conversion(self):
        upstream = {"name": "volcengine", "upstream_format": "chat_completions"}
        decision = route_plan.route_plan_for_request(
            upstream,
            {"client_id": "codex-app"},
            inbound_format="responses",
        )

        self.assertEqual(decision.behavior_profile, route_primitives.BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER)
        self.assertEqual(decision.codex_semantic_adapter, route_primitives.CODEX_SEMANTIC_EXTERNAL_ADAPTER)
        self.assertEqual(decision.wire_format_adapter, route_primitives.WIRE_RESPONSES_TO_CHAT)
        self.assertEqual(decision.retry_policy, route_primitives.RETRY_GATEWAY_FULL)
        self.assertEqual(decision.usage_policy, route_primitives.USAGE_SYNC_CAPTURE)
        self.assertEqual(decision.repair_policy, route_primitives.REPAIR_CODEX_SUBAGENT)

    def test_route_plan_third_party_app_provider_same_format_is_transparent_metered(self):
        upstream = {"name": "volcengine", "upstream_format": "chat_completions"}
        decision = route_plan.route_plan_for_request(
            upstream,
            {"client_id": "zcode"},
            inbound_format="chat_completions",
            provider_hint="volc",
        )

        self.assertEqual(decision.behavior_profile, route_primitives.BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED)
        self.assertEqual(decision.codex_semantic_adapter, route_primitives.CODEX_SEMANTIC_NONE)
        self.assertEqual(decision.wire_format_adapter, route_primitives.WIRE_TRANSPARENT)
        self.assertEqual(decision.retry_policy, route_primitives.RETRY_CONSERVATIVE_PRE_OUTPUT)
        self.assertEqual(decision.usage_policy, route_primitives.USAGE_ASYNC_TAP)
        self.assertEqual(decision.repair_policy, route_primitives.REPAIR_NONE)

    def test_route_plan_provider_scoped_v2_uses_gateway_compatibility_adapter(self):
        decision = route_plan.route_plan_for_request(
            {
                "name": "ollama_cloud",
                "upstream_format": "responses",
                "upstream_model": "glm-5.2",
            },
            {"client_id": "zcode"},
            inbound_format="responses",
            provider_hint="ollama-cloud",
            model_requested="ollama-cloud/glm-5.2",
            collaboration_protocol=COLLABORATION_V2,
        )

        self.assertEqual(
            decision.behavior_profile,
            route_primitives.BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY,
        )
        self.assertFalse(decision.transparent_metered)
        self.assertEqual(
            decision.codex_compatibility_policy,
            route_primitives.CodexCompatibilityPolicy.CURRENT_COMPATIBILITY,
        )
        self.assertEqual(
            decision.collaboration_backend,
            route_primitives.CollaborationBackend.GATEWAY_COMPATIBILITY,
        )
        self.assertEqual(decision.retry_policy, route_primitives.RETRY_GATEWAY_FULL)
        self.assertEqual(decision.usage_policy, route_primitives.USAGE_SYNC_CAPTURE)
        self.assertEqual(
            decision.tool_exposure.effective_mode,
            route_primitives.ToolExposureMode.CURRENT_COMPATIBILITY,
        )

    def test_route_plan_provider_scoped_v1_keeps_transparent_metering(self):
        decision = route_plan.route_plan_for_request(
            {
                "name": "ollama_cloud",
                "upstream_format": "responses",
                "upstream_model": "glm-5.2",
            },
            {"client_id": "zcode"},
            inbound_format="responses",
            provider_hint="ollama-cloud",
            model_requested="ollama-cloud/glm-5.2",
            collaboration_protocol=COLLABORATION_V1,
        )

        self.assertEqual(
            decision.behavior_profile,
            route_primitives.BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED,
        )
        self.assertTrue(decision.transparent_metered)

    def test_route_plan_standard_external_v2_keeps_existing_transparent_behavior(self):
        decision = route_plan.route_plan_for_request(
            {
                "name": "ollama_cloud",
                "upstream_format": "responses",
                "upstream_model": "glm-5.2",
            },
            {"client_id": "zcode"},
            inbound_format="responses",
            model_requested="ollama-cloud/glm-5.2",
            collaboration_protocol=COLLABORATION_V2,
        )

        self.assertEqual(
            decision.behavior_profile,
            route_primitives.BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED,
        )
        self.assertTrue(decision.transparent_metered)

    def test_route_plan_provider_scoped_raw_v2_probe_keeps_passthrough(self):
        decision = route_plan.route_plan_for_request(
            {
                "name": "ollama_cloud",
                "upstream_format": "responses",
                "upstream_model": "glm-5.2",
            },
            {"client_id": "zcode"},
            inbound_format="responses",
            provider_hint="ollama-cloud",
            model_requested="ollama-cloud/glm-5.2",
            collaboration_protocol=COLLABORATION_V2,
            raw_provider_probe=True,
        )

        self.assertEqual(
            decision.behavior_profile,
            route_primitives.BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED,
        )
        self.assertTrue(decision.transparent_metered)

    def test_route_plan_official_codex_app_v2_remains_passthrough(self):
        decision = route_plan.route_plan_for_request(
            {
                "name": "official",
                "upstream_format": "responses",
                "upstream_model": "gpt-5.5",
            },
            {"client_id": "codex-app"},
            inbound_format="responses",
            model_requested="openai/gpt-5.5",
            collaboration_protocol=COLLABORATION_V2,
        )

        self.assertEqual(
            decision.behavior_profile,
            route_primitives.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
        )
        self.assertFalse(decision.transparent_metered)
        self.assertEqual(
            decision.collaboration_backend,
            route_primitives.CollaborationBackend.CODEX_RUNTIME,
        )

    def test_route_plan_third_party_app_official_responses_is_transparent_metered(self):
        upstream = {"name": "official", "upstream_format": "responses"}
        decision = route_plan.route_plan_for_request(
            upstream,
            {"client_id": "opencode"},
            inbound_format="responses",
        )

        self.assertEqual(decision.behavior_profile, route_primitives.BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED)
        self.assertEqual(decision.wire_format_adapter, route_primitives.WIRE_TRANSPARENT)
        self.assertEqual(decision.usage_policy, route_primitives.USAGE_ASYNC_TAP)

    def test_route_plan_official_unknown_client_is_gateway_compat(self):
        upstream = {"name": "official", "upstream_format": "responses"}
        decision = route_plan.route_plan_for_request(
            upstream,
            {"client_id": "unknown"},
            inbound_format="responses",
        )

        self.assertEqual(decision.behavior_profile, route_primitives.BEHAVIOR_OFFICIAL_GATEWAY_COMPAT)
        self.assertEqual(decision.codex_semantic_adapter, route_primitives.CODEX_SEMANTIC_NONE)
        self.assertEqual(decision.request_kind_policy, route_primitives.REQUEST_KIND_GATEWAY)
        self.assertEqual(decision.retry_policy, route_primitives.RETRY_GATEWAY_FULL)
        self.assertEqual(decision.usage_policy, route_primitives.USAGE_SYNC_CAPTURE)
        self.assertEqual(decision.repair_policy, route_primitives.REPAIR_NONE)

    def test_route_plan_third_party_standard_unknown_client_uses_gateway_profile(self):
        upstream = {"name": "volcengine", "upstream_format": "chat_completions"}
        decision = route_plan.route_plan_for_request(
            upstream,
            {"client_id": "unknown"},
            inbound_format="chat_completions",
        )

        self.assertEqual(decision.behavior_profile, route_primitives.BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY)
        self.assertEqual(decision.codex_semantic_adapter, route_primitives.CODEX_SEMANTIC_EXTERNAL_ADAPTER)
        self.assertEqual(decision.request_kind_policy, route_primitives.REQUEST_KIND_GATEWAY)
        self.assertEqual(decision.retry_policy, route_primitives.RETRY_GATEWAY_FULL)
        self.assertEqual(decision.usage_policy, route_primitives.USAGE_SYNC_CAPTURE)

    def test_route_plan_fixtures_are_route_qualified_and_decision_complete(self):
        cases = (
            {
                "name": "official_passthrough",
                "upstream": {
                    "name": "official",
                    "auth": "codex_auth",
                    "upstream_model": "gpt-5.5",
                    "upstream_format": "responses",
                },
                "context": {"client_id": "codex-app"},
                "inbound_format": "responses",
                "provider_hint": None,
                "expected": {
                    "behavior_profile": route_primitives.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
                    "provider_id": "openai",
                    "canonical_model": "openai/gpt-5.5",
                    "upstream_model": "gpt-5.5",
                    "inbound_protocol": route_primitives.RouteProtocol.RESPONSES,
                    "upstream_protocol": route_primitives.RouteProtocol.RESPONSES,
                    "wire_format_adapter": route_primitives.WIRE_TRANSPARENT,
                    "tool_mode": route_primitives.ToolExposureMode.OFFICIAL_NATIVE,
                    "effective_tool_mode": route_primitives.ToolExposureMode.OFFICIAL_NATIVE,
                    "tool_state": route_primitives.CapabilityState.SUPPORTED,
                    "supports_search_tool": None,
                    "codex_compatibility_policy": route_primitives.CodexCompatibilityPolicy.OFFICIAL_NATIVE,
                    "collaboration_backend": route_primitives.CollaborationBackend.CODEX_RUNTIME,
                    "streaming_policy": route_primitives.StreamingPolicy.OFFICIAL_PASSTHROUGH,
                    "transport_policy": route_primitives.TransportPolicy.OFFICIAL_KEEPALIVE,
                    "mutations": (
                        route_primitives.RouteMutation.MODEL_ALIAS,
                        route_primitives.RouteMutation.OFFICIAL_TOOL_SEARCH_PRESERVATION,
                    ),
                },
            },
            {
                "name": "codex_app_external_compatibility_without_search",
                "upstream": {
                    "name": "ollama_cloud",
                    "auth": "ollama_api_key",
                    "upstream_model": "glm-5.2",
                    "upstream_format": "responses",
                    "supports_search_tool": False,
                },
                "context": {"client_id": "codex-app"},
                "inbound_format": "responses",
                "provider_hint": None,
                "expected": {
                    "behavior_profile": route_primitives.BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER,
                    "provider_id": "ollama-cloud",
                    "canonical_model": "ollama-cloud/glm-5.2",
                    "upstream_model": "glm-5.2",
                    "inbound_protocol": route_primitives.RouteProtocol.RESPONSES,
                    "upstream_protocol": route_primitives.RouteProtocol.RESPONSES,
                    "wire_format_adapter": route_primitives.WIRE_TRANSPARENT,
                    "tool_mode": route_primitives.ToolExposureMode.CURRENT_COMPATIBILITY,
                    "effective_tool_mode": route_primitives.ToolExposureMode.CURRENT_COMPATIBILITY,
                    "tool_state": route_primitives.CapabilityState.SUPPORTED,
                    "supports_search_tool": False,
                    "codex_compatibility_policy": route_primitives.CodexCompatibilityPolicy.CURRENT_COMPATIBILITY,
                    "collaboration_backend": route_primitives.CollaborationBackend.GATEWAY_COMPATIBILITY,
                    "streaming_policy": route_primitives.StreamingPolicy.GATEWAY_ADAPTED,
                    "transport_policy": route_primitives.TransportPolicy.STANDARD,
                    "mutations": (
                        route_primitives.RouteMutation.HARD_CODED_SCHEMA_INJECTION,
                        route_primitives.RouteMutation.MODEL_ALIAS,
                        route_primitives.RouteMutation.NAMESPACE_FLATTENING,
                        route_primitives.RouteMutation.SEMANTIC_REPAIR,
                        route_primitives.RouteMutation.SYNTHETIC_TERMINAL_FAILURE,
                    ),
                },
            },
            {
                "name": "provider_scoped_responses_to_chat",
                "upstream": {
                    "name": "volcengine",
                    "auth": "api_key",
                    "upstream_model": "glm-5.2",
                    "upstream_format": "chat_completions",
                },
                "context": {"client_id": "zcode"},
                "inbound_format": "responses",
                "provider_hint": "volc",
                "expected": {
                    "behavior_profile": route_primitives.BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED,
                    "provider_id": "volc",
                    "canonical_model": "volc/glm-5.2",
                    "upstream_model": "glm-5.2",
                    "inbound_protocol": route_primitives.RouteProtocol.RESPONSES,
                    "upstream_protocol": route_primitives.RouteProtocol.CHAT_COMPLETIONS,
                    "wire_format_adapter": route_primitives.WIRE_RESPONSES_TO_CHAT,
                    "tool_mode": route_primitives.ToolExposureMode.UNKNOWN,
                    "effective_tool_mode": route_primitives.ToolExposureMode.UNKNOWN,
                    "tool_state": route_primitives.CapabilityState.UNQUALIFIED,
                    "supports_search_tool": None,
                    "codex_compatibility_policy": route_primitives.CodexCompatibilityPolicy.NONE,
                    "collaboration_backend": route_primitives.CollaborationBackend.CLIENT_RUNTIME,
                    "streaming_policy": route_primitives.StreamingPolicy.TRANSPARENT_CONVERTED,
                    "transport_policy": route_primitives.TransportPolicy.STANDARD,
                    "mutations": (
                        route_primitives.RouteMutation.MODEL_ALIAS,
                        route_primitives.RouteMutation.WIRE_CONVERSION,
                    ),
                },
            },
            {
                "name": "provider_scoped_responses_same_format",
                "upstream": {
                    "name": "volcengine",
                    "auth": "api_key",
                    "upstream_model": "glm-5.2",
                    "upstream_format": "responses",
                },
                "context": {"client_id": "zcode"},
                "inbound_format": "responses",
                "provider_hint": "volc",
                "expected": {
                    "behavior_profile": route_primitives.BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED,
                    "provider_id": "volc",
                    "canonical_model": "volc/glm-5.2",
                    "upstream_model": "glm-5.2",
                    "inbound_protocol": route_primitives.RouteProtocol.RESPONSES,
                    "upstream_protocol": route_primitives.RouteProtocol.RESPONSES,
                    "wire_format_adapter": route_primitives.WIRE_TRANSPARENT,
                    "tool_mode": route_primitives.ToolExposureMode.UNKNOWN,
                    "effective_tool_mode": route_primitives.ToolExposureMode.UNKNOWN,
                    "tool_state": route_primitives.CapabilityState.UNQUALIFIED,
                    "supports_search_tool": None,
                    "codex_compatibility_policy": route_primitives.CodexCompatibilityPolicy.NONE,
                    "collaboration_backend": route_primitives.CollaborationBackend.CLIENT_RUNTIME,
                    "streaming_policy": route_primitives.StreamingPolicy.TRANSPARENT,
                    "transport_policy": route_primitives.TransportPolicy.STANDARD,
                    "mutations": (route_primitives.RouteMutation.MODEL_ALIAS,),
                },
            },
            {
                "name": "codex_app_chat_to_responses_compatibility",
                "upstream": {
                    "name": "ollama_cloud",
                    "auth": "ollama_api_key",
                    "upstream_model": "glm-5.2",
                    "upstream_format": "responses",
                    "supports_search_tool": False,
                },
                "context": {"client_id": "codex-app"},
                "inbound_format": "chat_completions",
                "provider_hint": None,
                "expected": {
                    "behavior_profile": route_primitives.BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER,
                    "provider_id": "ollama-cloud",
                    "canonical_model": "ollama-cloud/glm-5.2",
                    "upstream_model": "glm-5.2",
                    "inbound_protocol": route_primitives.RouteProtocol.CHAT_COMPLETIONS,
                    "upstream_protocol": route_primitives.RouteProtocol.RESPONSES,
                    "wire_format_adapter": route_primitives.WIRE_CHAT_TO_RESPONSES,
                    "tool_mode": route_primitives.ToolExposureMode.CURRENT_COMPATIBILITY,
                    "effective_tool_mode": route_primitives.ToolExposureMode.CURRENT_COMPATIBILITY,
                    "tool_state": route_primitives.CapabilityState.SUPPORTED,
                    "supports_search_tool": False,
                    "codex_compatibility_policy": route_primitives.CodexCompatibilityPolicy.CURRENT_COMPATIBILITY,
                    "collaboration_backend": route_primitives.CollaborationBackend.GATEWAY_COMPATIBILITY,
                    "streaming_policy": route_primitives.StreamingPolicy.GATEWAY_ADAPTED,
                    "transport_policy": route_primitives.TransportPolicy.STANDARD,
                    "mutations": (
                        route_primitives.RouteMutation.HARD_CODED_SCHEMA_INJECTION,
                        route_primitives.RouteMutation.MODEL_ALIAS,
                        route_primitives.RouteMutation.NAMESPACE_FLATTENING,
                        route_primitives.RouteMutation.SEMANTIC_REPAIR,
                        route_primitives.RouteMutation.SYNTHETIC_TERMINAL_FAILURE,
                        route_primitives.RouteMutation.WIRE_CONVERSION,
                    ),
                },
            },
        )

        for case in cases:
            with self.subTest(case=case["name"]):
                plan = route_plan.route_plan_for_request(
                    case["upstream"],
                    case["context"],
                    inbound_format=case["inbound_format"],
                    provider_hint=case["provider_hint"],
                    model_requested=case["expected"]["canonical_model"],
                )

                expected = case["expected"]
                self.assertEqual(plan.behavior_profile, expected["behavior_profile"])
                self.assertEqual(plan.provider_id, expected["provider_id"])
                self.assertEqual(plan.canonical_model, expected["canonical_model"])
                self.assertEqual(plan.upstream_model, expected["upstream_model"])
                self.assertEqual(plan.inbound_protocol, expected["inbound_protocol"])
                self.assertEqual(plan.upstream_protocol, expected["upstream_protocol"])
                self.assertEqual(plan.wire_format_adapter, expected["wire_format_adapter"])
                self.assertEqual(
                    plan.schema_version,
                    route_primitives.ROUTE_PLAN_SCHEMA_VERSION,
                )
                self.assertIsNone(plan.capability_manifest_version)
                self.assertIsNone(plan.capability_manifest_hash)
                self.assertEqual(
                    plan.capability_manifest_state,
                    route_primitives.CapabilityState.UNQUALIFIED,
                )
                self.assertEqual(plan.tool_exposure.requested_mode, expected["tool_mode"])
                self.assertEqual(plan.tool_exposure.effective_mode, expected["effective_tool_mode"])
                self.assertEqual(plan.tool_exposure.capability_state, expected["tool_state"])
                self.assertEqual(plan.tool_exposure.supports_search_tool, expected["supports_search_tool"])
                self.assertEqual(
                    plan.tool_exposure.gateway_schema_injection,
                    expected["effective_tool_mode"] == route_primitives.ToolExposureMode.CURRENT_COMPATIBILITY,
                )
                self.assertEqual(plan.codex_compatibility_policy, expected["codex_compatibility_policy"])
                self.assertEqual(plan.collaboration_backend, expected["collaboration_backend"])
                self.assertEqual(plan.execution_owner, route_primitives.ExecutionOwner.CODEX_CLIENT)
                self.assertEqual(plan.streaming_policy, expected["streaming_policy"])
                self.assertEqual(plan.retry_eligibility, route_primitives.CapabilityState.SUPPORTED)
                self.assertEqual(plan.request_kind, route_primitives.RETRY_REQUEST_MAIN_GENERATION)
                self.assertEqual(plan.transport_policy, expected["transport_policy"])
                self.assertIsInstance(plan.request_mutation_policy, route_primitives.MutationPolicy)
                self.assertIsInstance(plan.response_mutation_policy, route_primitives.MutationPolicy)
                self.assertIsInstance(plan.sse_mutation_policy, route_primitives.MutationPolicy)
                self.assertEqual(plan.mutation_summary, expected["mutations"])

    def test_route_plan_candidate_and_unknown_tool_modes_fail_closed_to_compatibility(self):
        cases = (
            (
                route_primitives.ToolExposureMode.NATIVE_DEFERRED_SEARCH_CANDIDATE.value,
                route_primitives.CapabilityState.UNQUALIFIED,
                route_primitives.CapabilityState.UNQUALIFIED,
                (),
            ),
            (
                route_primitives.ToolExposureMode.NATIVE_NO_SEARCH_CANDIDATE.value,
                route_primitives.CapabilityState.UNQUALIFIED,
                route_primitives.CapabilityState.UNQUALIFIED,
                ("function", "custom"),
            ),
            (
                route_primitives.ToolExposureMode.UNKNOWN.value,
                route_primitives.CapabilityState.UNSUPPORTED,
                route_primitives.CapabilityState.UNQUALIFIED,
                (),
            ),
            (
                route_primitives.ToolExposureMode.UNSUPPORTED.value,
                route_primitives.CapabilityState.UNSUPPORTED,
                route_primitives.CapabilityState.UNSUPPORTED,
                (),
            ),
        )

        for requested_mode, reported_state, expected_state, proven_tool_subset in cases:
            with self.subTest(requested_mode=requested_mode):
                plan = route_plan.route_plan_for_request(
                    {
                        "name": "ollama_cloud",
                        "auth": "ollama_api_key",
                        "upstream_model": "glm-5.2",
                        "upstream_format": "responses",
                        "tool_exposure_mode": requested_mode,
                        "tool_capability_state": reported_state.value,
                        "proven_tool_subset": proven_tool_subset,
                    },
                    {"client_id": "codex-app"},
                    inbound_format="responses",
                    model_requested="ollama-cloud/glm-5.2",
                )

                self.assertEqual(plan.tool_exposure.requested_mode.value, requested_mode)
                self.assertEqual(plan.tool_exposure.effective_mode, route_primitives.ToolExposureMode.CURRENT_COMPATIBILITY)
                self.assertEqual(plan.tool_exposure.capability_state, expected_state)
                self.assertEqual(plan.tool_exposure.proven_tool_subset, proven_tool_subset)
                self.assertTrue(plan.tool_exposure.gateway_schema_injection)
                self.assertEqual(plan.behavior_profile, route_primitives.BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER)
                self.assertEqual(plan.repair_policy, route_primitives.REPAIR_CODEX_SUBAGENT)
                self.assertIn(route_primitives.RouteMutation.SEMANTIC_REPAIR, plan.named_mutations)
                self.assertFalse(plan.official_http_passthrough)
                self.assertFalse(plan.transparent_metered)

    def test_route_plan_reports_disabled_tool_protocol_without_schema_injection(self):
        plan = route_plan.route_plan_for_request(
            {
                "name": "custom_endpoint",
                "auth": "api_key",
                "upstream_model": "thinking-model",
                "upstream_format": "chat_completions",
                "tool_protocol": "none",
            },
            {"client_id": "codex-app"},
            inbound_format="responses",
            model_requested="custom-endpoint/thinking-model",
        )

        self.assertEqual(plan.primary_attempt.tool_protocol, "none")
        self.assertEqual(
            plan.tool_exposure.capability_state,
            route_primitives.CapabilityState.UNSUPPORTED,
        )
        self.assertEqual(
            plan.tool_exposure.effective_mode,
            route_primitives.ToolExposureMode.UNSUPPORTED,
        )
        self.assertFalse(plan.tool_exposure.gateway_schema_injection)
        self.assertNotIn(
            route_primitives.RouteMutation.HARD_CODED_SCHEMA_INJECTION,
            plan.named_mutations,
        )

    def test_route_plan_and_nested_tool_policy_are_immutable(self):
        plan = route_plan.route_plan_for_request(
            {
                "name": "ollama_cloud",
                "upstream_model": "glm-5.2",
                "upstream_format": "responses",
            },
            {"client_id": "codex-app"},
            inbound_format="responses",
            model_requested="ollama-cloud/glm-5.2",
        )

        with self.assertRaises(AttributeError):
            plan.behavior_profile = route_primitives.BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED
        with self.assertRaises(AttributeError):
            plan.tool_exposure.gateway_schema_injection = False

    def test_route_plan_makes_schema_injection_and_repair_request_scoped(self):
        cases = (
            {
                "name": "normal_compatibility",
                "request_kind": route_primitives.RETRY_REQUEST_MAIN_GENERATION,
                "raw_provider_probe": False,
                "tool_mode": route_primitives.ToolExposureMode.CURRENT_COMPATIBILITY.value,
                "reported_state": route_primitives.CapabilityState.SUPPORTED.value,
                "expected_injection": True,
                "expected_repair": route_primitives.REPAIR_CODEX_SUBAGENT,
                "expected_state": route_primitives.CapabilityState.SUPPORTED,
            },
            {
                "name": "compact",
                "request_kind": route_primitives.RETRY_REQUEST_COMPACT,
                "raw_provider_probe": False,
                "tool_mode": route_primitives.ToolExposureMode.CURRENT_COMPATIBILITY.value,
                "reported_state": route_primitives.CapabilityState.SUPPORTED.value,
                "expected_injection": False,
                "expected_repair": route_primitives.REPAIR_CODEX_SUBAGENT,
                "expected_state": route_primitives.CapabilityState.SUPPORTED,
            },
            {
                "name": "raw_probe",
                "request_kind": route_primitives.RETRY_REQUEST_MAIN_GENERATION,
                "raw_provider_probe": True,
                "tool_mode": route_primitives.ToolExposureMode.CURRENT_COMPATIBILITY.value,
                "reported_state": route_primitives.CapabilityState.SUPPORTED.value,
                "expected_injection": False,
                "expected_repair": route_primitives.REPAIR_NONE,
                "expected_state": route_primitives.CapabilityState.SUPPORTED,
            },
            {
                "name": "unresolved_native_candidate",
                "request_kind": route_primitives.RETRY_REQUEST_MAIN_GENERATION,
                "raw_provider_probe": False,
                "tool_mode": route_primitives.ToolExposureMode.NATIVE_DEFERRED_SEARCH_CANDIDATE.value,
                "reported_state": route_primitives.CapabilityState.SUPPORTED.value,
                "expected_injection": True,
                "expected_repair": route_primitives.REPAIR_CODEX_SUBAGENT,
                "expected_state": route_primitives.CapabilityState.UNQUALIFIED,
            },
        )

        for case in cases:
            with self.subTest(case=case["name"]):
                plan = route_plan.route_plan_for_request(
                    {
                        "name": "ollama_cloud",
                        "upstream_model": "glm-5.2",
                        "upstream_format": "responses",
                        "tool_exposure_mode": case["tool_mode"],
                        "tool_capability_state": case["reported_state"],
                    },
                    {"client_id": "codex-app"},
                    inbound_format="responses",
                    model_requested="ollama-cloud/glm-5.2",
                    request_kind=case["request_kind"],
                    raw_provider_probe=case["raw_provider_probe"],
                )

                self.assertEqual(
                    plan.tool_exposure.gateway_schema_injection,
                    case["expected_injection"],
                )
                self.assertEqual(plan.repair_policy, case["expected_repair"])
                self.assertEqual(
                    plan.tool_exposure.capability_state,
                    case["expected_state"],
                )
                self.assertEqual(
                    route_primitives.RouteMutation.HARD_CODED_SCHEMA_INJECTION
                    in plan.named_mutations,
                    case["expected_injection"],
                )
                self.assertEqual(
                    plan.tool_exposure.strip_caller_tools,
                    case["request_kind"] == route_primitives.RETRY_REQUEST_COMPACT,
                )

    def test_route_plan_auto_protocol_contains_immutable_typed_attempts(self):
        plan = route_plan.route_plan_for_request(
            {
                "name": "ollama_cloud",
                "base_url": "https://ollama.example.test/v1",
                "auth": "ollama_api_key",
                "upstream_model": "glm-5.2",
                "upstream_format": "auto",
            },
            {"client_id": "codex-app"},
            inbound_format="responses",
            model_requested="ollama-cloud/glm-5.2",
        )

        self.assertEqual(
            [attempt.upstream_protocol for attempt in plan.attempts],
            [
                route_primitives.RouteProtocol.RESPONSES,
                route_primitives.RouteProtocol.CHAT_COMPLETIONS,
            ],
        )
        self.assertEqual(
            [attempt.wire_format_adapter for attempt in plan.attempts],
            [
                route_primitives.WIRE_TRANSPARENT,
                route_primitives.WIRE_RESPONSES_TO_CHAT,
            ],
        )
        self.assertEqual(
            [attempt.request_body_mode for attempt in plan.attempts],
            [
                route_primitives.AttemptRequestBodyMode.PREPARED_DIRECT,
                route_primitives.AttemptRequestBodyMode.CONVERT_RESPONSES_TO_CHAT,
            ],
        )
        self.assertEqual(
            [attempt.endpoint_url for attempt in plan.attempts],
            [
                "https://ollama.example.test/v1/responses",
                "https://ollama.example.test/v1/chat/completions",
            ],
        )
        self.assertTrue(
            all(
                attempt.authentication_strategy
                == route_primitives.AuthenticationStrategy.OLLAMA_API_KEY
                for attempt in plan.attempts
            )
        )
        self.assertTrue(
            all(
                attempt.streaming_policy
                == route_primitives.StreamingPolicy.GATEWAY_ADAPTED
                for attempt in plan.attempts
            )
        )
        self.assertTrue(
            all(
                attempt.usage_policy == route_primitives.UsagePolicy.SYNC_CAPTURE
                for attempt in plan.attempts
            )
        )
        self.assertTrue(plan.attempts[0].allows_protocol_fallback_status(415))
        self.assertFalse(plan.attempts[0].allows_protocol_fallback_status(429))
        self.assertFalse(plan.attempts[1].fallback_http_statuses)
        self.assertNotIn(
            route_primitives.RouteMutation.WIRE_CONVERSION,
            plan.attempts[0].named_mutations,
        )
        self.assertIn(
            route_primitives.RouteMutation.WIRE_CONVERSION,
            plan.attempts[1].named_mutations,
        )
        with self.assertRaises(AttributeError):
            plan.attempts[0].upstream_protocol = route_primitives.RouteProtocol.CHAT_COMPLETIONS

    def test_route_plan_primary_execution_fields_are_read_only_attempt_views(self):
        plan = route_plan.route_plan_for_request(
            {
                "name": "ollama_cloud",
                "base_url": "https://ollama.example.test/v1",
                "auth": "ollama_api_key",
                "upstream_model": "glm-5.2",
                "upstream_format": "auto",
            },
            {"client_id": "codex-app"},
            inbound_format="responses",
            model_requested="ollama-cloud/glm-5.2",
        )
        duplicate_primary_fields = {
            "authentication_strategy",
            "upstream_protocol",
            "selected_upstream_format",
            "wire_format_adapter",
            "request_kind",
            "retry_policy",
            "retry_eligibility",
            "usage_policy",
            "streaming_policy",
            "transport_policy",
            "request_mutation_policy",
            "response_mutation_policy",
            "sse_mutation_policy",
        }

        self.assertTrue(plan.attempts)
        self.assertTrue(
            duplicate_primary_fields.isdisjoint(
                field.name for field in fields(route_plan.RoutePlan)
            )
        )
        primary_attempt = plan.attempts[0]
        self.assertEqual(
            plan.authentication_strategy,
            primary_attempt.authentication_strategy,
        )
        self.assertEqual(plan.upstream_protocol, primary_attempt.upstream_protocol)
        self.assertEqual(
            plan.selected_upstream_format,
            primary_attempt.selected_upstream_format,
        )
        self.assertEqual(
            plan.wire_format_adapter,
            primary_attempt.wire_format_adapter,
        )
        self.assertEqual(plan.request_kind, primary_attempt.retry.request_kind)
        self.assertEqual(plan.retry_policy, primary_attempt.retry.policy)
        self.assertEqual(
            plan.retry_eligibility,
            primary_attempt.retry.eligibility,
        )
        self.assertEqual(plan.usage_policy, primary_attempt.usage_policy)
        self.assertEqual(
            plan.streaming_policy,
            primary_attempt.streaming_policy,
        )
        self.assertEqual(
            plan.transport_policy,
            primary_attempt.transport_policy,
        )
        self.assertEqual(
            plan.request_mutation_policy,
            primary_attempt.request_mutation_policy,
        )
        self.assertEqual(
            plan.response_mutation_policy,
            primary_attempt.response_mutation_policy,
        )
        self.assertEqual(
            plan.sse_mutation_policy,
            primary_attempt.sse_mutation_policy,
        )
        with self.assertRaises(TypeError):
            replace(
                plan,
                selected_upstream_format="chat_completions",
            )

    def test_route_plan_attempt_retry_execution_uses_only_explicit_runtime_facts(self):
        runtime_facts = route_plan.RouteRuntimeFacts(
            request_timeout_seconds=41,
            request_kind_base_attempts=7,
            request_kind_attempts_configured=False,
            failure_expansion_attempts=19,
            official_open_attempts=2,
            capacity_elapsed_limit_seconds=83.0,
            stream_elapsed_limit_seconds=97.0,
            downstream_retry_notice_enabled=True,
            pre_response_budget_seconds=109.0,
        )
        with (
            patch(
                "route_plan.upstream_timeout_seconds",
                side_effect=AssertionError("planner read request timeout"),
            ),
            patch(
                "route_plan.gateway_auto_retry_max_attempts",
                side_effect=AssertionError("planner read retry expansion"),
            ),
            patch(
                "route_plan.gateway_capacity_retry_elapsed_limit_seconds",
                side_effect=AssertionError("planner read capacity budget"),
            ),
            patch(
                "route_plan.gateway_stream_retry_elapsed_limit_seconds",
                side_effect=AssertionError("planner read stream budget"),
            ),
        ):
            plan = route_plan.route_plan_for_request(
                {
                    "name": "volcengine",
                    "base_url": "https://ark.example.test/v1",
                    "auth": "api_key",
                    "upstream_model": "glm-5.2",
                    "upstream_format": "responses",
                },
                {"client_id": "codex-app"},
                inbound_format="responses",
                model_requested="volc/glm-5.2",
                caller_stream=True,
                runtime_facts=runtime_facts,
            )

        retry = plan.attempts[0].retry
        self.assertEqual(retry.eligibility, route_primitives.CapabilityState.SUPPORTED)
        self.assertEqual(retry.policy, route_primitives.RetryPolicy.GATEWAY_FULL)
        self.assertEqual(retry.request_timeout_seconds, 41)
        self.assertEqual(retry.base_open_attempts, 7)
        self.assertEqual(retry.base_relay_attempts, 7)
        self.assertEqual(
            retry.open_attempts_for_failure_class(
                route_primitives.RETRY_FAILURE_PROVIDER_THROTTLE
            ),
            19,
        )
        self.assertEqual(
            retry.relay_attempts_for_failure_class(
                route_primitives.RETRY_FAILURE_QUICK_TRANSIENT,
                stream_failure=True,
            ),
            19,
        )
        self.assertTrue(retry.capacity_elapsed_limit_allows(80.0, 3))
        self.assertFalse(retry.capacity_elapsed_limit_allows(80.1, 3))
        self.assertTrue(retry.stream_elapsed_limit_allows(94.0, 3))
        self.assertFalse(retry.stream_elapsed_limit_allows(94.1, 3))
        self.assertEqual(retry.pre_response_budget_seconds, 109.0)
        self.assertTrue(retry.emit_downstream_retry_notice)
        self.assertIsNone(retry.open_attempt_budget)

    def test_transparent_compact_route_uses_effective_main_generation_runtime_facts(self):
        compact_facts = route_plan.RouteRuntimeFacts(
            request_timeout_seconds=31,
            request_kind_base_attempts=3,
            request_kind_attempts_configured=True,
            failure_expansion_attempts=19,
            official_open_attempts=2,
            capacity_elapsed_limit_seconds=83.0,
            stream_elapsed_limit_seconds=97.0,
            downstream_retry_notice_enabled=False,
            pre_response_budget_seconds=109.0,
        )
        main_facts = replace(
            compact_facts,
            request_timeout_seconds=41,
            request_kind_base_attempts=5,
        )

        plan = route_plan.route_plan_for_request(
            {
                "name": "volcengine",
                "base_url": "https://ark.example.test/v1",
                "auth": "api_key",
                "upstream_model": "glm-5.2",
                "upstream_format": "chat_completions",
            },
            {"client_id": "zcode"},
            inbound_format="chat_completions",
            provider_hint="volc",
            model_requested="volc/glm-5.2",
            request_kind=route_primitives.RETRY_REQUEST_COMPACT,
            runtime_facts={
                route_primitives.RETRY_REQUEST_COMPACT: compact_facts,
                route_primitives.RETRY_REQUEST_MAIN_GENERATION: main_facts,
            },
        )

        self.assertEqual(
            plan.request_kind,
            route_primitives.RETRY_REQUEST_MAIN_GENERATION,
        )
        self.assertEqual(plan.attempts[0].retry.request_kind, plan.request_kind)
        self.assertEqual(plan.attempts[0].retry.request_timeout_seconds, 41)
        self.assertEqual(plan.attempts[0].retry.base_open_attempts, 5)
        self.assertEqual(plan.attempts[0].retry.base_relay_attempts, 5)

    def test_operational_auth_snapshot_refreshes_only_on_the_next_request_binding(self):
        def planned_headers(upstream, incoming_headers):
            authentication = (
                gateway_transport.materialize_operational_authentication(
                    incoming_headers,
                    upstream,
                )
            )
            plan = route_plan.route_plan_for_request(
                upstream,
                {"client_id": "codex-app"},
                inbound_format="responses",
                model_requested="volc/glm-5.2",
                official_http_passthrough_enabled=False,
            )
            plan = gateway_transport.bind_route_plan_operational_authentication(
                plan,
                incoming_headers,
                upstream,
                authentication,
            )
            return plan, plan.attempts[0].request_headers.to_dict()

        provider = {
            "name": "volcengine",
            "base_url": "https://ark.example.test/v1",
            "auth": "api_key",
            "api_key": "provider-old",
            "upstream_model": "glm-5.2",
            "upstream_format": "responses",
        }
        first_plan, first_headers = planned_headers(provider, {})
        provider["api_key"] = "provider-new"
        second_plan, second_headers = planned_headers(provider, {})
        self.assertEqual(first_headers["Authorization"], "Bearer provider-old")
        self.assertEqual(second_headers["Authorization"], "Bearer provider-new")
        self.assertEqual(first_plan, second_plan)

        incoming_provider = {
            **provider,
            "auth": "incoming",
        }
        _empty_plan, empty_headers = planned_headers(incoming_provider, {})
        _incoming_plan, incoming_headers = planned_headers(
            incoming_provider,
            {"Authorization": "Custom incoming-new"},
        )
        self.assertNotIn("Authorization", empty_headers)
        self.assertEqual(
            incoming_headers["Authorization"],
            "Custom incoming-new",
        )

        ollama_provider = {
            **provider,
            "auth": "ollama_api_key",
        }
        with patch.dict(
            os.environ,
            {"OLLAMA_API_KEY": "ollama-old"},
            clear=False,
        ):
            _ollama_plan, ollama_headers = planned_headers(
                ollama_provider,
                {},
            )
            os.environ["OLLAMA_API_KEY"] = "ollama-new"
            _next_ollama_plan, next_ollama_headers = planned_headers(
                ollama_provider,
                {},
            )
        self.assertEqual(
            ollama_headers["Authorization"],
            "Bearer ollama-old",
        )
        self.assertEqual(
            next_ollama_headers["Authorization"],
            "Bearer ollama-new",
        )

        official_provider = {
            "name": "official",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "auth": "codex_auth",
            "upstream_model": "gpt-5.6-sol",
            "upstream_format": "responses",
        }
        with (
            patch("codex_auth.access_token", return_value="codex-old"),
            patch("codex_auth.account_id", return_value="acct-old"),
        ):
            official_authentication = (
                gateway_transport.materialize_operational_authentication(
                    {},
                    official_provider,
                )
            )
            official_plan = route_plan.route_plan_for_request(
                official_provider,
                {},
                inbound_format="responses",
                model_requested="openai/gpt-5.6-sol",
                official_http_passthrough_enabled=False,
            )
            official_plan = (
                gateway_transport.bind_route_plan_operational_authentication(
                    official_plan,
                    {},
                    official_provider,
                    official_authentication,
                )
            )
            repeated_official_plan = route_plan.route_plan_for_request(
                official_provider,
                {},
                inbound_format="responses",
                model_requested="openai/gpt-5.6-sol",
                official_http_passthrough_enabled=False,
            )
            repeated_official_plan = (
                gateway_transport.bind_route_plan_operational_authentication(
                    repeated_official_plan,
                    {},
                    official_provider,
                    official_authentication,
                )
            )
        official_headers = (
            official_plan.attempts[0].request_headers.to_dict()
        )
        repeated_official_headers = (
            repeated_official_plan.attempts[0].request_headers.to_dict()
        )
        self.assertEqual(official_headers, repeated_official_headers)
        self.assertEqual(
            official_headers["Authorization"],
            "Bearer codex-old",
        )
        self.assertEqual(
            official_headers["Chatgpt-account-id"],
            "acct-old",
        )

    def test_route_plan_chat_auto_attempts_report_actual_conversion_chain(self):
        plan = route_plan.route_plan_for_request(
            {
                "name": "volcengine",
                "base_url": "https://ark.example.test/v1",
                "auth": "api_key",
                "upstream_model": "glm-5.2",
                "upstream_format": "auto",
            },
            {"client_id": "codex-app"},
            inbound_format="chat_completions",
            model_requested="volc/glm-5.2",
            request_kind=route_primitives.RETRY_REQUEST_COMPACT,
        )

        self.assertEqual(
            plan.caller_request_body_mode,
            route_primitives.CallerRequestBodyMode.PRESERVE_CALLER,
        )
        self.assertEqual(
            [attempt.wire_format_adapter for attempt in plan.attempts],
            [route_primitives.WIRE_CHAT_TO_RESPONSES, route_primitives.WIRE_TRANSPARENT],
        )
        self.assertEqual(
            [attempt.request_conversion_steps for attempt in plan.attempts],
            [
                (route_primitives.WIRE_CHAT_TO_RESPONSES,),
                (),
            ],
        )
        self.assertEqual(
            [attempt.request_body_mode for attempt in plan.attempts],
            [
                route_primitives.AttemptRequestBodyMode.CONVERT_CHAT_TO_RESPONSES,
                route_primitives.AttemptRequestBodyMode.PREPARED_DIRECT,
            ],
        )
        self.assertIn(
            route_primitives.RouteMutation.WIRE_CONVERSION,
            plan.attempts[0].named_mutations,
        )
        self.assertNotIn(
            route_primitives.RouteMutation.WIRE_CONVERSION,
            plan.attempts[1].named_mutations,
        )
        self.assertTrue(
            all(
                route_primitives.RouteMutation.CALLER_TOOL_STRIPPING
                in attempt.named_mutations
                for attempt in plan.attempts
            )
        )
        self.assertEqual(
            set(plan.mutation_summary),
            set().union(
                *(attempt.named_mutations for attempt in plan.attempts)
            ),
        )

    def test_route_plan_preserves_unsupported_protocol_identity_without_attempts(self):
        cases = (
            (
                "anthropic_messages",
                route_primitives.RouteProtocol.ANTHROPIC_MESSAGES,
                route_primitives.CapabilityState.UNSUPPORTED,
            ),
            (
                "invalid_wire",
                route_primitives.RouteProtocol.UNKNOWN,
                route_primitives.CapabilityState.UNQUALIFIED,
            ),
        )
        for configured_protocol, typed_protocol, capability_state in cases:
            with self.subTest(configured_protocol=configured_protocol):
                plan = route_plan.route_plan_for_request(
                    {
                        "name": "ollama_cloud",
                        "upstream_model": "glm-5.2",
                        "upstream_format": configured_protocol,
                    },
                    {"client_id": "codex-app"},
                    inbound_format="responses",
                    model_requested="ollama-cloud/glm-5.2",
                )

                self.assertEqual(
                    plan.configured_upstream_protocol_name,
                    configured_protocol,
                )
                self.assertEqual(
                    plan.configured_upstream_protocol,
                    typed_protocol,
                )
                self.assertEqual(
                    plan.protocol_capability_state,
                    capability_state,
                )
                self.assertEqual(plan.retry_eligibility, capability_state)
                self.assertEqual(plan.attempts, ())

    def test_route_plan_separates_schema_identity_from_optional_manifest_evidence(self):
        valid_manifest_hash = f"sha256:{'a' * 64}"
        unqualified = route_plan.route_plan_for_request(
            {
                "name": "ollama_cloud",
                "upstream_model": "glm-5.2",
                "upstream_format": "responses",
            },
            {"client_id": "codex-app"},
            inbound_format="responses",
            model_requested="ollama-cloud/glm-5.2",
        )
        qualified = route_plan.route_plan_for_request(
            {
                "name": "ollama_cloud",
                "upstream_model": "glm-5.2",
                "upstream_format": "responses",
                "capability_manifest_version": "provider-capabilities.v3",
                "capability_manifest_hash": valid_manifest_hash,
                "capability_manifest_state": route_primitives.CapabilityState.SUPPORTED.value,
            },
            {"client_id": "codex-app"},
            inbound_format="responses",
            model_requested="ollama-cloud/glm-5.2",
        )

        self.assertEqual(
            unqualified.schema_version,
            route_primitives.ROUTE_PLAN_SCHEMA_VERSION,
        )
        self.assertIsNone(unqualified.capability_manifest_version)
        self.assertIsNone(unqualified.capability_manifest_hash)
        self.assertEqual(
            unqualified.capability_manifest_state,
            route_primitives.CapabilityState.UNQUALIFIED,
        )
        self.assertEqual(
            unqualified.tool_exposure.effective_mode,
            route_primitives.ToolExposureMode.CURRENT_COMPATIBILITY,
        )
        self.assertTrue(unqualified.attempts)
        self.assertEqual(
            qualified.capability_manifest_version,
            "provider-capabilities.v3",
        )
        self.assertEqual(
            qualified.capability_manifest_hash,
            valid_manifest_hash,
        )
        self.assertEqual(
            qualified.capability_manifest_state,
            route_primitives.CapabilityState.SUPPORTED,
        )

    def test_route_plan_capability_manifest_identity_fails_closed_pairwise(self):
        valid_hash = f"sha256:{'b' * 64}"
        cases = (
            {
                "name": "missing_pair",
                "version": None,
                "hash": None,
                "state": route_primitives.CapabilityState.SUPPORTED.value,
                "expected_version": None,
                "expected_hash": None,
                "expected_state": route_primitives.CapabilityState.UNQUALIFIED,
            },
            {
                "name": "version_only",
                "version": "provider-capabilities.v3",
                "hash": None,
                "state": route_primitives.CapabilityState.SUPPORTED.value,
                "expected_version": None,
                "expected_hash": None,
                "expected_state": route_primitives.CapabilityState.UNQUALIFIED,
            },
            {
                "name": "hash_only",
                "version": None,
                "hash": valid_hash,
                "state": route_primitives.CapabilityState.SUPPORTED.value,
                "expected_version": None,
                "expected_hash": None,
                "expected_state": route_primitives.CapabilityState.UNQUALIFIED,
            },
            {
                "name": "malformed_version",
                "version": "../provider capabilities",
                "hash": valid_hash,
                "state": route_primitives.CapabilityState.SUPPORTED.value,
                "expected_version": None,
                "expected_hash": None,
                "expected_state": route_primitives.CapabilityState.UNQUALIFIED,
            },
            {
                "name": "future_version",
                "version": "provider-capabilities.v999",
                "hash": valid_hash,
                "state": route_primitives.CapabilityState.SUPPORTED.value,
                "expected_version": None,
                "expected_hash": None,
                "expected_state": route_primitives.CapabilityState.UNQUALIFIED,
            },
            {
                "name": "malformed_hash",
                "version": "provider-capabilities.v3",
                "hash": "sha256:not-a-digest",
                "state": route_primitives.CapabilityState.SUPPORTED.value,
                "expected_version": None,
                "expected_hash": None,
                "expected_state": route_primitives.CapabilityState.UNQUALIFIED,
            },
            {
                "name": "valid_pair_unqualified",
                "version": "provider-capabilities.v3",
                "hash": valid_hash,
                "state": route_primitives.CapabilityState.UNQUALIFIED.value,
                "expected_version": "provider-capabilities.v3",
                "expected_hash": valid_hash,
                "expected_state": route_primitives.CapabilityState.UNQUALIFIED,
            },
            {
                "name": "valid_pair_unsupported",
                "version": "provider-capabilities.v3",
                "hash": valid_hash,
                "state": route_primitives.CapabilityState.UNSUPPORTED.value,
                "expected_version": "provider-capabilities.v3",
                "expected_hash": valid_hash,
                "expected_state": route_primitives.CapabilityState.UNSUPPORTED,
            },
            {
                "name": "valid_supported_pair",
                "version": "provider-capabilities.v3",
                "hash": valid_hash,
                "state": route_primitives.CapabilityState.SUPPORTED.value,
                "expected_version": "provider-capabilities.v3",
                "expected_hash": valid_hash,
                "expected_state": route_primitives.CapabilityState.SUPPORTED,
            },
        )

        for case in cases:
            with self.subTest(case=case["name"]):
                upstream = {
                    "name": "ollama_cloud",
                    "upstream_model": "glm-5.2",
                    "upstream_format": "responses",
                    "capability_manifest_state": case["state"],
                }
                if case["version"] is not None:
                    upstream["capability_manifest_version"] = case["version"]
                if case["hash"] is not None:
                    upstream["capability_manifest_hash"] = case["hash"]
                plan = route_plan.route_plan_for_request(
                    upstream,
                    {"client_id": "codex-app"},
                    inbound_format="responses",
                    model_requested="ollama-cloud/glm-5.2",
                )

                self.assertEqual(
                    plan.capability_manifest_version,
                    case["expected_version"],
                )
                self.assertEqual(
                    plan.capability_manifest_hash,
                    case["expected_hash"],
                )
                self.assertEqual(
                    plan.capability_manifest_state,
                    case["expected_state"],
                )

    def test_route_plan_resolves_vision_network_and_mutation_before_execution(self):
        cases = (
            {
                "name": "no_image",
                "has_image": False,
                "accepts_image": False,
                "proxy_enabled": True,
                "expected_action": route_primitives.VisionAction.PASS_THROUGH,
                "expected_network": route_primitives.VisionNetworkAction.NONE,
                "expected_mutation": None,
            },
            {
                "name": "native_image",
                "has_image": True,
                "accepts_image": True,
                "proxy_enabled": True,
                "expected_action": route_primitives.VisionAction.PASS_THROUGH,
                "expected_network": route_primitives.VisionNetworkAction.NONE,
                "expected_mutation": None,
            },
            {
                "name": "proxy_text_only",
                "has_image": True,
                "accepts_image": False,
                "proxy_enabled": True,
                "expected_action": route_primitives.VisionAction.PROXY,
                "expected_network": route_primitives.VisionNetworkAction.IMAGE_PROXY,
                "expected_mutation": route_primitives.RouteMutation.IMAGE_CONTENT_REPLACEMENT,
            },
            {
                "name": "reject_text_only",
                "has_image": True,
                "accepts_image": False,
                "proxy_enabled": False,
                "expected_action": route_primitives.VisionAction.REJECT,
                "expected_network": route_primitives.VisionNetworkAction.NONE,
                "expected_mutation": route_primitives.RouteMutation.IMAGE_UNSUPPORTED_REJECTION,
            },
        )

        for case in cases:
            with self.subTest(case=case["name"]):
                with patch(
                    "gateway_settings.gateway_image_proxy_enabled",
                    side_effect=AssertionError("planner read runtime state"),
                ):
                    plan = route_plan.route_plan_for_request(
                        {
                            "name": "volcengine",
                            "upstream_model": "glm-5.2",
                            "upstream_format": "responses",
                        },
                        {"client_id": "zcode"},
                        inbound_format="responses",
                        provider_hint="volc",
                        model_requested="volc/glm-5.2",
                        input_has_image=case["has_image"],
                        target_accepts_images=case["accepts_image"],
                        image_proxy_enabled=case["proxy_enabled"],
                    )

                self.assertEqual(plan.vision.action, case["expected_action"])
                self.assertEqual(
                    plan.vision.network_action,
                    case["expected_network"],
                )
                if case["expected_mutation"] is None:
                    self.assertNotIn(
                        route_primitives.RouteMutation.IMAGE_CONTENT_REPLACEMENT,
                        plan.named_mutations,
                    )
                    self.assertNotIn(
                        route_primitives.RouteMutation.IMAGE_UNSUPPORTED_REJECTION,
                        plan.named_mutations,
                    )
                else:
                    self.assertIn(case["expected_mutation"], plan.named_mutations)
                    for attempt in plan.attempts:
                        self.assertIn(
                            case["expected_mutation"],
                            attempt.named_mutations,
                        )

    def test_route_plan_uses_only_explicit_runtime_facts(self):
        with (
            patch(
                "route_plan.gateway_official_http_passthrough_enabled",
                side_effect=AssertionError("planner read passthrough runtime state"),
            ),
            patch(
                "gateway_settings.gateway_image_proxy_enabled",
                side_effect=AssertionError("planner read image runtime state"),
            ),
        ):
            plan = route_plan.route_plan_for_request(
                {
                    "name": "official",
                    "upstream_model": "gpt-5.5",
                    "upstream_format": "responses",
                },
                {"client_id": "codex-app"},
                inbound_format="responses",
                model_requested="openai/gpt-5.5",
                official_http_passthrough_enabled=False,
                input_has_image=True,
                target_accepts_images=False,
                image_proxy_enabled=False,
            )

        self.assertEqual(
            plan.behavior_profile,
            route_primitives.BEHAVIOR_OFFICIAL_GATEWAY_COMPAT,
        )
        self.assertEqual(plan.vision.action, route_primitives.VisionAction.REJECT)


def test_route_plan_seam_source_does_not_use_handler_privates() -> None:
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "CodexProxyHandler" not in names
    assert "FakeHandler" not in names
    assert "post_handler" not in names
    assert not any(
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "codex_proxy"
        and node.attr.startswith("_")
        for node in ast.walk(tree)
    )
