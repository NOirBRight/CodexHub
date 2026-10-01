"""Subscription exchange and catalog behavior through public boundaries."""
import json
import socket
import threading
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from catalog import CatalogPolicy
from catalog_sync import build_external_provider_model
from gateway_admission import GatewayRequestAdmission, activate_gateway_request, restore_gateway_request, watch_downstream_disconnect
from gateway_transport import build_upstream_headers
from providers_config import ModelConfig, ProviderConfig, build_external_model_index, load_providers, save_providers
from protocol_translation import prepare_exchange
from route_plan import route_plan_for_request
from subscription_backend_contract import BackendError, load_http2_dependencies
from subscription_exchange import SYSTEM_CONTEXT_CONSENT, open_subscription


def chunk(delta, finish=None):
    return {"id": "actual", "model": "exact-high-fast", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


@pytest.mark.parametrize("inbound,payload", [
    ("responses", {"model": "exact-high-fast", "input": "hello", "stream": True}),
    ("chat_completions", {"model": "exact-high-fast", "messages": [{"role": "user", "content": "hello"}], "stream": True}),
    ("anthropic_messages", {"model": "exact-high-fast", "messages": [{"role": "user", "content": "hello"}], "stream": True, "max_tokens": 128}),
])
def test_existing_protocol_adapters_feed_same_subscription_exchange(inbound, payload):
    prepared = prepare_exchange(json.dumps(payload).encode(), inbound_format=inbound, outbound_format="chat_completions")
    observed = []
    def backend(body, **options):
        observed.append(body)
        yield chunk({"content": "actual reply"})
        yield chunk({}, "stop")
    with open_subscription(Request("https://cli-subscription.invalid/v1/chat/completions", data=prepared.upstream_body), provider_id="cursor-subscription", timeout=2, backend=backend) as response:
        body = response.read()
    assert observed[0]["model"] == "exact-high-fast"
    if inbound == "anthropic_messages":
        assert observed[0]["max_tokens"] == 128
    assert "actual reply" in body.decode()
    assert body.endswith(b"data: [DONE]\n\n")


def test_nonstream_collects_real_call_identity_fragments_and_only_supplied_usage():
    def backend(body, **options):
        yield chunk({"tool_calls": [{"index": 0, "id": "call_original", "function": {"name": "actual", "arguments": '{"x":'}}]})
        yield chunk({"tool_calls": [{"index": 0, "function": {"arguments": '1}'}}]}, "tool_calls")
    request = Request("https://cli-subscription.invalid", data=b'{"stream":false}')
    with open_subscription(request, provider_id="claude-subscription", timeout=2, backend=backend) as response:
        result = json.loads(response.read())
    assert result["choices"][0]["message"]["tool_calls"] == [{"id": "call_original", "type": "function", "function": {"name": "actual", "arguments": '{"x":1}'}}]
    assert "usage" not in result


def test_generation_error_before_exposure_is_bounded_and_not_replayed():
    calls = []
    def backend(body, **options):
        calls.append(body)
        raise BackendError("not-eligible", "Account cannot generate.", 403)
        yield
    with pytest.raises(HTTPError) as caught:
        with open_subscription(Request("https://cli-subscription.invalid", data=b"{}"), provider_id="claude-subscription", timeout=2, backend=backend):
            pass
    assert caught.value.code == 403
    assert json.loads(caught.value.read())["error"]["code"] == "not-eligible"
    assert len(calls) == 1


def test_admission_cancellation_closes_real_reader_and_backend():
    stopped = threading.Event()
    def backend(body, *, cancel, timeout):
        try:
            yield chunk({"content": "started"})
            assert cancel.wait(timeout=2)
        finally:
            stopped.set()
    admission = GatewayRequestAdmission()
    previous = activate_gateway_request(admission)
    try:
        with open_subscription(Request("https://cli-subscription.invalid", data=b'{"stream":true}'), provider_id="cursor-subscription", timeout=2, backend=backend):
            admission.cancel()
        assert stopped.wait(timeout=1)
    finally:
        restore_gateway_request(previous)


def test_local_socket_disconnect_cancels_an_admitted_request():
    local, caller = socket.socketpair()
    admission = GatewayRequestAdmission()
    previous = activate_gateway_request(admission)
    stop = watch_downstream_disconnect(local)
    try:
        caller.close()
        assert admission.wait_for_cancellation(1)
    finally:
        stop.set()
        local.close()
        restore_gateway_request(previous)


@pytest.mark.parametrize("provider_id", ["cursor-subscription", "claude-subscription"])
def test_catalog_requires_consent_and_keeps_exact_model_identity_without_key(provider_id, tmp_path):
    provider = ProviderConfig(provider_id, "CLI", "", "", models=[ModelConfig("exact/high-fast", upstream_model="exact/high-fast")])
    if provider_id == "claude-subscription":
        assert build_external_model_index([provider]) == {}
        provider.system_context_consent = SYSTEM_CONTEXT_CONSENT
    entries = build_external_model_index([provider])
    entry = entries[f"{provider_id}/exact/high-fast"]
    assert entry["upstream_model"] == "exact/high-fast"
    assert entry["upstream_format"] == "chat_completions"
    assert entry["api_key"] is None
    target = tmp_path / "providers.toml"
    save_providers([provider], target)
    assert load_providers(target)[0].system_context_consent == provider.system_context_consent
    policy = CatalogPolicy(set(), set(), {})
    row = build_external_provider_model(entry, policy, {"context_window": 999999, "max_output_tokens": 777777})
    assert "context_window" not in row and "max_output_tokens" not in row
    assert row["supported_reasoning_levels"] == []


def test_subscription_routes_have_no_automatic_replay_and_borrow_no_headers():
    upstream = {"name": "cursor-subscription", "provider_id": "cursor-subscription", "upstream_model": "exact-high-fast", "model_id": "cursor-subscription/exact-high-fast", "base_url": "https://cli-subscription.invalid/v1", "auth": "official_cli_session", "upstream_format": "chat_completions", "available_upstream_formats": ("chat_completions",)}
    plan = route_plan_for_request(upstream, {}, inbound_format="responses", model_requested="cursor-subscription/exact-high-fast")
    assert len(plan.attempts) == 1
    assert plan.attempts[0].retry.base_open_attempts == 1
    assert plan.attempts[0].retry.base_relay_attempts == 1
    assert plan.attempts[0].retry.failure_expansion_attempts == 0
    headers = build_upstream_headers({"Authorization": "Bearer caller-secret", "x-api-key": "caller-other-secret"}, upstream)
    assert not any(key.lower() in {"authorization", "x-api-key"} for key in headers)


def test_packaged_http2_dependencies_import_without_site_packages():
    load_http2_dependencies()
    import h2.connection
    assert h2.connection.H2Connection is not None
