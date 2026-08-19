#!/usr/bin/env python3
"""Build the #66 Responses-to-Chat conversion matrix from accepted runtime contracts."""

from __future__ import annotations

from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


SCHEMA = "codexhub.issue66.chat-conversion-matrix.v1"
ARTIFACT_KIND = "chat_conversion_matrix"
SOURCE_CONTRACT = Path("docs/evidence/issue-392/collaboration-runtime-contract.json")
INVENTORY = Path("docs/evidence/issue-64/collaboration-v1-v2-inventory.json")
TRANSLATION = Path("src-python/protocol_translation.py")
DEFAULT_OUTPUT = Path("docs/evidence/issue-66/chat-conversion-matrix.json")

V2_TOOLS = (
    "spawn_agent",
    "send_message",
    "followup_task",
    "wait_agent",
    "interrupt_agent",
    "list_agents",
)
DISPOSITIONS = frozenset(
    {"native", "consumed_locally", "reversibly_adapted", "unavailable"}
)


class MatrixValidationError(ValueError):
    """Bounded matrix failure; never includes request content."""


def _sha256_file(path: Path) -> str:
    canonical = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(canonical).hexdigest()


def _row(
    *,
    row_id: str,
    category: str,
    key: str,
    execution_owner: str,
    responses: str,
    chat: str,
    disposition: str,
    when: str,
    fail_closed: bool,
    implementation: str,
    notes: str,
) -> dict[str, Any]:
    if disposition not in DISPOSITIONS:
        raise MatrixValidationError(f"invalid_disposition:{row_id}")
    return {
        "category": category,
        "chat": chat,
        "disposition": disposition,
        "execution_owner": execution_owner,
        "fail_closed": fail_closed,
        "id": row_id,
        "implementation": implementation,
        "key": key,
        "notes": notes,
        "responses": responses,
        "when": when,
    }


def _request_fields() -> list[dict[str, Any]]:
    current = "protocol_translation"
    return [
        _row(
            row_id="request.model",
            category="request_field",
            key="model",
            execution_owner="gateway",
            responses="string",
            chat="string",
            disposition="native",
            when="any non-empty string",
            fail_closed=False,
            implementation=current,
            notes="Identity is a routing concern; conversion preserves the caller string.",
        ),
        _row(
            row_id="request.input",
            category="request_field",
            key="input",
            execution_owner="caller",
            responses="string|message[]",
            chat="messages[]",
            disposition="reversibly_adapted",
            when="string or list of supported items",
            fail_closed=True,
            implementation=current,
            notes="Unknown item types fail before upstream sampling.",
        ),
        _row(
            row_id="request.instructions",
            category="request_field",
            key="instructions",
            execution_owner="caller",
            responses="string",
            chat="system message",
            disposition="reversibly_adapted",
            when="string",
            fail_closed=True,
            implementation=current,
            notes="Non-string instructions fail closed.",
        ),
        _row(
            row_id="request.tools.function",
            category="tool_declaration",
            key="tools[type=function]",
            execution_owner="codex_client",
            responses="function tool",
            chat="tools[].function",
            disposition="reversibly_adapted",
            when="type=function with name/parameters",
            fail_closed=True,
            implementation=current,
            notes="Standard function declarations round-trip.",
        ),
        _row(
            row_id="request.tools.namespace",
            category="tool_declaration",
            key="tools[type=namespace]",
            execution_owner="codex_client",
            responses="namespace + child functions",
            chat="function tools with injective aliases",
            disposition="reversibly_adapted",
            when="Collaboration V2 namespace collaboration",
            fail_closed=True,
            implementation="required_by_394",
            notes="Chat has no namespace tool; Gateway adapter must wrap children injectively.",
        ),
        _row(
            row_id="request.tools.custom",
            category="tool_declaration",
            key="tools[type=custom|freeform]",
            execution_owner="codex_client",
            responses="custom/freeform tool",
            chat="none",
            disposition="unavailable",
            when="any custom/freeform/code-mode declaration",
            fail_closed=True,
            implementation=current,
            notes="Not implied by V2 support.",
        ),
        _row(
            row_id="request.tools.hosted",
            category="tool_declaration",
            key="tools[hosted web|image|mcp]",
            execution_owner="upstream_provider",
            responses="hosted tool",
            chat="none",
            disposition="unavailable",
            when="any hosted/server-executed declaration",
            fail_closed=True,
            implementation=current,
            notes="Gateway cannot synthesize a provider-hosted capability.",
        ),
        _row(
            row_id="request.tool_choice",
            category="request_field",
            key="tool_choice",
            execution_owner="caller",
            responses="auto|none|required|function",
            chat="auto|none|required|function",
            disposition="reversibly_adapted",
            when="supported function choice",
            fail_closed=True,
            implementation=current,
            notes="Unknown choice shapes fail closed.",
        ),
        _row(
            row_id="request.stream",
            category="request_field",
            key="stream",
            execution_owner="gateway",
            responses="bool",
            chat="bool",
            disposition="native",
            when="boolean",
            fail_closed=False,
            implementation=current,
            notes="Streaming policy is owned by RoutePlan, not conversion.",
        ),
        _row(
            row_id="request.sampling",
            category="request_field",
            key="temperature|top_p|max_output_tokens|parallel_tool_calls",
            execution_owner="caller",
            responses="number|bool",
            chat="same fields (max_tokens for max_output_tokens)",
            disposition="reversibly_adapted",
            when="JSON-compatible scalar",
            fail_closed=True,
            implementation=current,
            notes="max_output_tokens maps to max_tokens.",
        ),
        _row(
            row_id="request.reasoning",
            category="request_field",
            key="reasoning",
            execution_owner="caller",
            responses="object",
            chat="none",
            disposition="unavailable",
            when="any present reasoning object",
            fail_closed=True,
            implementation=current,
            notes="No proven Chat equivalent.",
        ),
        _row(
            row_id="request.client_metadata.empty",
            category="request_field",
            key="client_metadata",
            execution_owner="gateway",
            responses="{}",
            chat="absent",
            disposition="consumed_locally",
            when="missing, null, or {}",
            fail_closed=False,
            implementation=current,
            notes="Codex transport bookkeeping with no Chat meaning.",
        ),
        _row(
            row_id="request.client_metadata.nonempty",
            category="request_field",
            key="client_metadata",
            execution_owner="caller",
            responses="non-empty object",
            chat="none",
            disposition="unavailable",
            when="any non-default object",
            fail_closed=True,
            implementation=current,
            notes="Non-empty metadata cannot be dropped.",
        ),
        _row(
            row_id="request.include.empty",
            category="request_field",
            key="include",
            execution_owner="gateway",
            responses="[]",
            chat="absent",
            disposition="consumed_locally",
            when="missing, null, or []",
            fail_closed=False,
            implementation=current,
            notes="Each requested include value is classified independently when present.",
        ),
        _row(
            row_id="request.include.nonempty",
            category="request_field",
            key="include",
            execution_owner="caller",
            responses="string[]",
            chat="none",
            disposition="unavailable",
            when="any non-empty include list",
            fail_closed=True,
            implementation=current,
            notes="No Chat field carries Responses include semantics.",
        ),
        _row(
            row_id="request.prompt_cache_key.empty",
            category="request_field",
            key="prompt_cache_key",
            execution_owner="gateway",
            responses='""',
            chat="absent",
            disposition="consumed_locally",
            when="missing, null, or empty string",
            fail_closed=False,
            implementation=current,
            notes="Empty cache key has no Chat meaning.",
        ),
        _row(
            row_id="request.prompt_cache_key.nonempty",
            category="request_field",
            key="prompt_cache_key",
            execution_owner="caller",
            responses="string",
            chat="none",
            disposition="unavailable",
            when="any non-empty string",
            fail_closed=True,
            implementation=current,
            notes="A real cache key cannot be forwarded or silently dropped.",
        ),
        _row(
            row_id="request.store.false",
            category="request_field",
            key="store",
            execution_owner="gateway",
            responses="false",
            chat="absent",
            disposition="consumed_locally",
            when="missing, null, or false on a non-storing route",
            fail_closed=False,
            implementation=current,
            notes="store=false is the no-op default.",
        ),
        _row(
            row_id="request.store.true",
            category="request_field",
            key="store",
            execution_owner="caller",
            responses="true",
            chat="none",
            disposition="unavailable",
            when="store=true",
            fail_closed=True,
            implementation=current,
            notes="Chat Completions does not store Responses items.",
        ),
        _row(
            row_id="request.text.empty",
            category="request_field",
            key="text",
            execution_owner="gateway",
            responses="{}",
            chat="absent",
            disposition="consumed_locally",
            when="missing, null, or {}",
            fail_closed=False,
            implementation=current,
            notes="Empty text config is a no-op.",
        ),
        _row(
            row_id="request.text.structured",
            category="request_field",
            key="text",
            execution_owner="caller",
            responses="non-empty object",
            chat="none",
            disposition="unavailable",
            when="any structured text/format object",
            fail_closed=True,
            implementation=current,
            notes="Plain-text-only mapping is not a structured-output equivalent.",
        ),
        _row(
            row_id="request.unknown_field",
            category="request_field",
            key="*",
            execution_owner="caller",
            responses="unknown field",
            chat="none",
            disposition="unavailable",
            when="any field not listed in this matrix",
            fail_closed=True,
            implementation=current,
            notes="Unknown fields cannot disappear.",
        ),
    ]


def _input_items() -> list[dict[str, Any]]:
    current = "protocol_translation"
    return [
        _row(
            row_id="item.message.text",
            category="input_item",
            key="input[type=message]",
            execution_owner="caller",
            responses="message role+content",
            chat="messages[] role+content",
            disposition="reversibly_adapted",
            when="system|user|assistant|developer text/image parts",
            fail_closed=True,
            implementation=current,
            notes="developer maps to system. Progressive text uses the same content mapping.",
        ),
        _row(
            row_id="item.function_call",
            category="input_item",
            key="input[type=function_call]",
            execution_owner="codex_client",
            responses="function_call + call_id + name + arguments",
            chat="assistant.tool_calls[].id",
            disposition="reversibly_adapted",
            when="non-empty call_id and name",
            fail_closed=True,
            implementation=current,
            notes="Missing/duplicate/unsafe call_id fails as unpaired_tool_call.",
        ),
        _row(
            row_id="item.function_call_output",
            category="input_item",
            key="input[type=function_call_output]",
            execution_owner="codex_client",
            responses="function_call_output + call_id + output",
            chat="tool role + tool_call_id",
            disposition="reversibly_adapted",
            when="non-empty call_id and string output",
            fail_closed=True,
            implementation=current,
            notes="Results must restore the original call identity.",
        ),
        _row(
            row_id="item.tool_search_call",
            category="input_item",
            key="input[type=tool_search_call]",
            execution_owner="codex_client",
            responses="client tool_search_call",
            chat="none as an item; tools[] on the next request",
            disposition="consumed_locally",
            when="execution=client",
            fail_closed=True,
            implementation=current,
            notes="Chat has no tool-search item. Loaded definitions travel as function tools.",
        ),
        _row(
            row_id="item.tool_search_hosted",
            category="input_item",
            key="input[type=tool_search_call]",
            execution_owner="upstream_provider",
            responses="non-client tool_search",
            chat="none",
            disposition="unavailable",
            when="execution!=client",
            fail_closed=True,
            implementation=current,
            notes="Hosted search cannot be emulated.",
        ),
        _row(
            row_id="item.unknown",
            category="input_item",
            key="input[type=*]",
            execution_owner="caller",
            responses="unknown item type",
            chat="none",
            disposition="unavailable",
            when="any type other than message/function_call/function_call_output/tool_search_*",
            fail_closed=True,
            implementation=current,
            notes="Includes item_reference, reasoning, custom_tool_call, MCP, and future types.",
        ),
    ]


def _stream_and_identity() -> list[dict[str, Any]]:
    current = "protocol_translation"
    return [
        _row(
            row_id="stream.text.delta",
            category="stream_event",
            key="response.output_text.delta",
            execution_owner="upstream_model",
            responses="output_text.delta",
            chat="choices[].delta.content",
            disposition="reversibly_adapted",
            when="progressive text",
            fail_closed=True,
            implementation=current,
            notes="Symmetric with chat.completion.chunk content deltas.",
        ),
        _row(
            row_id="stream.function.delta",
            category="stream_event",
            key="response.function_call_arguments.delta",
            execution_owner="upstream_model",
            responses="function_call_arguments.delta",
            chat="choices[].delta.tool_calls[].function.arguments",
            disposition="reversibly_adapted",
            when="function argument deltas",
            fail_closed=True,
            implementation=current,
            notes="Deltas assemble against the request-scoped call identity.",
        ),
        _row(
            row_id="stream.terminal.completed",
            category="stream_event",
            key="response.completed",
            execution_owner="gateway",
            responses="response.completed",
            chat="finish_reason + final chunk",
            disposition="reversibly_adapted",
            when="successful terminal",
            fail_closed=True,
            implementation=current,
            notes="Exactly one terminal event.",
        ),
        _row(
            row_id="stream.terminal.failed",
            category="stream_event",
            key="response.failed|response.incomplete|error",
            execution_owner="gateway",
            responses="failed/incomplete/error",
            chat="error chunk or non-stream error body",
            disposition="reversibly_adapted",
            when="error or incomplete terminal",
            fail_closed=True,
            implementation=current,
            notes="Cancellation and errors close the stream; they do not leave it open.",
        ),
        _row(
            row_id="identity.call_id",
            category="identity_rule",
            key="call_id",
            execution_owner="gateway",
            responses="call_id on function_call / function_call_output",
            chat="tool_calls[].id / tool_call_id",
            disposition="reversibly_adapted",
            when="present string",
            fail_closed=True,
            implementation=current,
            notes="Missing, duplicate, conflicting, or unsafe IDs fail explicitly.",
        ),
        _row(
            row_id="identity.item_id",
            category="identity_rule",
            key="id",
            execution_owner="gateway",
            responses="item id",
            chat="absent or consumed locally",
            disposition="consumed_locally",
            when="Responses item id with a paired call_id",
            fail_closed=True,
            implementation=current,
            notes="Chat has no item id; call_id is the preserved identity.",
        ),
        _row(
            row_id="usage",
            category="stream_event",
            key="usage",
            execution_owner="gateway",
            responses="input_tokens/output_tokens",
            chat="prompt_tokens/completion_tokens",
            disposition="reversibly_adapted",
            when="usage object present",
            fail_closed=False,
            implementation=current,
            notes="Token fields map in both directions.",
        ),
        _row(
            row_id="cancellation",
            category="stream_event",
            key="downstream cancel",
            execution_owner="gateway",
            responses="abort upstream + terminal",
            chat="abort upstream + terminal",
            disposition="native",
            when="client disconnect or local shutdown",
            fail_closed=True,
            implementation=current,
            notes="Owned by SSE/relay, not by a Chat-specific fallback.",
        ),
    ]


def _collaboration_v2() -> list[dict[str, Any]]:
    rows = [
        _row(
            row_id="v2.namespace",
            category="collaboration_v2",
            key="tools[namespace=collaboration]",
            execution_owner="codex_client",
            responses="namespace collaboration with six child functions",
            chat="six injective function aliases",
            disposition="reversibly_adapted",
            when="complete namespace+child schema, no V1 mix",
            fail_closed=True,
            implementation="required_by_394",
            notes="Never classify from child name alone. Mixed V1/V2 fails closed.",
        )
    ]
    for name in V2_TOOLS:
        rows.append(
            _row(
                row_id=f"v2.tool.{name}",
                category="collaboration_v2",
                key=f"collaboration.{name}",
                execution_owner="codex_client",
                responses=f"function_call name={name} under namespace collaboration",
                chat="function tool call with request-scoped injective alias",
                disposition="reversibly_adapted",
                when="valid V2 arguments from the #392 schema",
                fail_closed=True,
                implementation="required_by_394",
                notes="Alias names are deterministic for the same ordered declarations.",
            )
        )
    rows.extend(
        [
            _row(
                row_id="v2.task_identity",
                category="collaboration_v2",
                key="task_name|path_prefix",
                execution_owner="codex_client",
                responses="task_name string; path_prefix optional",
                chat="function argument strings",
                disposition="reversibly_adapted",
                when="plain string identities",
                fail_closed=True,
                implementation="required_by_394",
                notes="Canonical task identity is agent_path serialized as task_name.",
            ),
            _row(
                row_id="v2.encrypted_fields",
                category="collaboration_v2",
                key="encrypted_content|encrypted argument fields",
                execution_owner="codex_client",
                responses="encrypted string fields",
                chat="none",
                disposition="unavailable",
                when="any encrypted=true field",
                fail_closed=True,
                implementation="required_by_394",
                notes="Third-party Chat cannot carry Official encrypted payloads. Cross-Provider encrypted handoff is #395-B.",
            ),
            _row(
                row_id="v2.agent_message",
                category="collaboration_v2",
                key="input[type=agent_message]",
                execution_owner="codex_client",
                responses="agent_message with author/recipient",
                chat="none native; plaintext history only via reversible adapter",
                disposition="unavailable",
                when="encrypted agent_message or missing author/recipient",
                fail_closed=True,
                implementation="required_by_394",
                notes="Plaintext author/recipient preservation is required before any adapt path is approved.",
            ),
            _row(
                row_id="v2.history_replay",
                category="collaboration_v2",
                key="same-home call/result/history replay",
                execution_owner="codex_client",
                responses="ordered function_call + function_call_output + agent_message",
                chat="ordered assistant.tool_calls + tool results",
                disposition="reversibly_adapted",
                when="same-home, non-encrypted, identities intact",
                fail_closed=True,
                implementation="required_by_394",
                notes="Replay must restore original namespace/child/call/task identities.",
            ),
            _row(
                row_id="v2.unknown",
                category="collaboration_v2",
                key="unknown V2 field or child",
                execution_owner="codex_client",
                responses="unknown",
                chat="none",
                disposition="unavailable",
                when="unknown child, missing discriminator, or mixed V1/V2",
                fail_closed=True,
                implementation="required_by_394",
                notes="Unknown collaboration items cannot disappear or be guessed.",
            ),
        ]
    )
    return rows


def _invariants() -> dict[str, Any]:
    return {
        "fail_closed_unknown": True,
        "hidden_fallback_forbidden": [
            "responses_protocol",
            "official_openai",
            "other_model",
            "other_provider",
        ],
        "keyed_by": [
            "field_or_item",
            "value_when",
            "declaration_or_item_type",
            "execution_owner",
            "protocol_representation",
        ],
        "not_keyed_by": ["model_name", "provider_name"],
        "terminal_exactly_one": True,
        "v1_v2_isolation": True,
    }


def build_matrix() -> dict[str, Any]:
    rows = _request_fields() + _input_items() + _stream_and_identity() + _collaboration_v2()
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise MatrixValidationError("duplicate_row_id")
    v2_ids = {f"v2.tool.{name}" for name in V2_TOOLS}
    if not v2_ids.issubset(set(ids)):
        raise MatrixValidationError("missing_v2_tools")
    return {
        "artifact_kind": ARTIFACT_KIND,
        "evidence_binding": {
            "collaboration_inventory": {
                "file": INVENTORY.as_posix(),
                "sha256": _sha256_file(INVENTORY),
            },
            "protocol_translation": {
                "file": TRANSLATION.as_posix(),
                "sha256": _sha256_file(TRANSLATION),
            },
            "source_contract": {
                "file": SOURCE_CONTRACT.name,
                "sha256": _sha256_file(SOURCE_CONTRACT),
            },
        },
        "invariants": _invariants(),
        "qualification_status": "contract_for_chat_bridge",
        "rows": rows,
        "schema": SCHEMA,
        "v2_tools": list(V2_TOOLS),
    }


def validate_matrix(payload: dict[str, Any]) -> None:
    if payload.get("schema") != SCHEMA:
        raise MatrixValidationError("schema")
    if payload.get("artifact_kind") != ARTIFACT_KIND:
        raise MatrixValidationError("artifact_kind")
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise MatrixValidationError("rows")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise MatrixValidationError("row_type")
        row_id = row.get("id")
        if not isinstance(row_id, str) or row_id in seen:
            raise MatrixValidationError("row_id")
        seen.add(row_id)
        if row.get("disposition") not in DISPOSITIONS:
            raise MatrixValidationError("disposition")
        if row["disposition"] == "unavailable" and not row.get("fail_closed"):
            raise MatrixValidationError("unavailable_must_fail_closed")
    if payload.get("v2_tools") != list(V2_TOOLS):
        raise MatrixValidationError("v2_tools")
    if not payload.get("invariants", {}).get("fail_closed_unknown"):
        raise MatrixValidationError("unknown_policy")
    forbidden = payload.get("invariants", {}).get("not_keyed_by", [])
    if "model_name" not in forbidden or "provider_name" not in forbidden:
        raise MatrixValidationError("keyed_by_model")


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        matrix = build_matrix()
        validate_matrix(matrix)
        rendered = _canonical(matrix)
        if args.check:
            existing = args.output.read_text(encoding="utf-8")
            if existing.replace("\r\n", "\n") != rendered:
                print("MATRIX_DRIFT")
                return 1
            print(json.dumps({"schema": SCHEMA, "reconciled": True}, sort_keys=True))
            return 0
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(
            json.dumps(
                {"output": str(args.output), "rows": len(matrix["rows"]), "schema": SCHEMA},
                sort_keys=True,
            )
        )
        return 0
    except MatrixValidationError as error:
        print(f"MATRIX_INVALID:{error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
