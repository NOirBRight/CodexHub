"""Verified endpoint capabilities for cross-protocol prompt cache keys.

Same-protocol requests retain their original controls. Cross-protocol callers
must supply the immutable decision made for the actual destination endpoint.
Usage reporting alone is never evidence that an endpoint accepts a cache key.
"""

from enum import Enum
from urllib.parse import urlsplit


class PromptCacheKeyPolicy(str, Enum):
    PRESERVE = "preserve"
    DROP_UNVERIFIED = "drop_unverified_endpoint"


# OpenAI Chat/Responses API references document prompt_cache_key. The Codex
# subscription Responses path already preserves it on the native HTTP route.
# See docs/research/2026-09-08-gateway-prompt-cache-token-risk.md for evidence.
_VERIFIED_ENDPOINTS = frozenset({
    ("api.openai.com", "/v1/chat/completions", "chat_completions"),
    ("api.openai.com", "/v1/responses", "responses"),
    ("chatgpt.com", "/backend-api/codex/responses", "responses"),
})


def cache_key_policy_for_endpoint(endpoint_url: str, protocol: str) -> PromptCacheKeyPolicy:
    try:
        url = urlsplit(endpoint_url)
        verified = (
            url.scheme == "https"
            and url.port in {None, 443}
            and url.username is None
            and url.password is None
            and (url.hostname, url.path.rstrip("/"), protocol) in _VERIFIED_ENDPOINTS
        )
    except ValueError:
        verified = False
    return PromptCacheKeyPolicy.PRESERVE if verified else PromptCacheKeyPolicy.DROP_UNVERIFIED
