"""Shared Desktop heartbeat history item for omit-unproven-result tests."""

from __future__ import annotations


def desktop_automation_heartbeat_result(*, call_id=None):
    item = {
        "type": "function_call_output",
        "id": "fco_01a0a8ae-eb1b-7a73-a360-6ac018c90d48",
        "name": "automation_update",
        "namespace": "codex_app",
        "output": '{"status":"ok"}',
    }
    if call_id is not None:
        item["call_id"] = call_id
    return item
