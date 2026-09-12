"""OpenCode Go routing identity at the real route/header materialization seam."""
import pytest

from gateway_transport import bind_route_plan_operational_authentication, materialize_operational_authentication
from route_plan import route_plan_for_request
from route_primitives import UPSTREAM_USER_AGENT


def bound_headers(key='omp-session', incoming=None, endpoint='https://opencode.ai/zen/go/v1', api_key='test-provider-key'):
    incoming = incoming or {}
    upstream = {'name': 'custom', 'base_url': endpoint, 'upstream_format': 'responses', 'auth': 'api_key', 'api_key': api_key}
    plan = route_plan_for_request(upstream, {'client_id': 'omp'}, inbound_format='responses')
    auth = materialize_operational_authentication(incoming, upstream)
    bound = bind_route_plan_operational_authentication(plan, incoming, upstream, auth, prompt_cache_key=key)
    assert not plan.primary_attempt.request_headers.materialized
    return bound.primary_attempt.request_headers.to_dict()


def test_omp_cache_identity_becomes_stable_opencode_session():
    first = bound_headers()['x-opencode-session']
    assert first == bound_headers()['x-opencode-session']
    assert first != bound_headers('another-session')['x-opencode-session']
    assert first != bound_headers(api_key='other-account')['x-opencode-session']
    assert 'omp-session' not in first
    assert bound_headers('unicode-会话\r\n')['x-opencode-session'].isascii()


def test_explicit_opencode_identity_is_preserved_case_insensitively():
    headers = bound_headers(incoming={'X-OpenCode-Session': 'native-session'})
    assert {k.lower(): v for k, v in headers.items()}['x-opencode-session'] == 'native-session'
    assert sum(k.lower() == 'x-opencode-session' for k in headers) == 1


@pytest.mark.parametrize('header', ['session_id', 'Session-Id', 'X-Session-Id', 'X-Codex-Session-Id'])
def test_native_session_takes_precedence_over_cache_key(header):
    incoming = {header: 'native-stable'}
    assert bound_headers('first', incoming)['x-opencode-session'] == bound_headers('second', incoming)['x-opencode-session']


@pytest.mark.parametrize('endpoint', ['https://example.test/v1', 'https://opencode.ai.evil.test/zen/go/v1', 'http://opencode.ai/zen/go/v1', 'https://opencode.ai/other/v1', 'https://opencode.ai:444/zen/go/v1'])
def test_other_endpoints_do_not_receive_synthetic_opencode_identity(endpoint):
    assert 'x-opencode-session' not in bound_headers(endpoint=endpoint)


@pytest.mark.parametrize('key', [None, '', 42])
def test_missing_identity_does_not_create_random_or_shared_session(key):
    assert 'x-opencode-session' not in bound_headers(key)


def test_opencode_outbound_user_agent_is_gateway_identity_not_client_urllib():
    headers = bound_headers(incoming={"User-Agent": "Python-urllib/3.14", "x-session-id": "native-stable"})
    lowered = {k.lower(): v for k, v in headers.items()}
    assert lowered["user-agent"] == UPSTREAM_USER_AGENT
    assert "python-urllib" not in lowered["user-agent"].lower()
    assert "x-opencode-session" in lowered
