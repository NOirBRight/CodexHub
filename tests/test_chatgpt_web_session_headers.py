"""Observed native client headers must bind separate Gateway Web sessions."""
import pytest

from gateway_request import request_context_from_headers
from chatgpt_web_client_session import client_session_id, thread_id_for


def test_claude_explicit_header_wins_over_generic_and_body_metadata():
    context = request_context_from_headers({
        'X-Claude-Code-Session-Id': 'claude-session', 'x-session-id': 'generic-session',
    })
    assert context['session_id'] == 'claude-session'
    assert context['session_source'] == 'claude-code'
    assert client_session_id({'sessionID': 'spoof'}, context) == client_session_id({}, context)
    assert client_session_id({'metadata': {'user_id': 'arbitrary'}}, {}) is None


def test_equal_claude_and_generic_session_ids_do_not_share_thread_or_tool_identity():
    claude = request_context_from_headers({'x-claude-code-session-id': 'same'})
    generic = request_context_from_headers({'x-session-id': 'same'})
    a, b = client_session_id({}, claude), client_session_id({}, generic)
    assert a and b and a != b
    assert thread_id_for(a) != thread_id_for(b)
    assert client_session_id({'sessionID': 'same'}, {}) == b


@pytest.mark.parametrize('generic', ['x-session-id', 'x-codex-session-id'])
def test_blank_claude_header_does_not_hide_explicit_session(generic):
    context = request_context_from_headers({'x-claude-code-session-id': ' ', generic: 'session'})
    assert context['session_id'] == 'session'
