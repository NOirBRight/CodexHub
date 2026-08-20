"""Direct seam tests for the extracted Vision Proxy adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gateway_errors import ImageProxyError
from route_plan import VisionPlan
from route_primitives import (
    VISION_PROXY_CODEX_APP_ADAPTER,
    RouteProtocol,
    VisionAction,
    VisionNetworkAction,
)
from vision_proxy import (
    VisionFacts,
    VisionProxyAdapter,
    VisionProxyHooks,
)


_IMAGE_URL = "https://example.test/chart.png"
_VISION_UPSTREAM = {
    "name": "vision-provider",
    "upstream_format": "responses",
}


def _reject_plan() -> VisionPlan:
    return VisionPlan(
        policy=VISION_PROXY_CODEX_APP_ADAPTER,
        action=VisionAction.REJECT,
        network_action=VisionNetworkAction.NONE,
        input_has_image=True,
        target_accepts_images=False,
        image_proxy_enabled=False,
    )


def test_text_only_image_rejection_is_fail_closed_before_hooks() -> None:
    adapter = VisionProxyAdapter(
        hooks=VisionProxyHooks(
            resolve_upstream=lambda _model: pytest.fail(
                "rejected image reached catalog resolution"
            )
        )
    )
    payload = {
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": _IMAGE_URL},
                ],
            }
        ]
    }

    with pytest.raises(ImageProxyError, match="Vision Proxy is disabled"):
        adapter.enforce_text_only_boundary(
            payload,
            inbound_protocol=RouteProtocol.RESPONSES,
            target_model="text-only",
            target_upstream={"name": "target"},
            vision_plan=_reject_plan(),
        )

    assert payload["input"][0]["content"][0]["type"] == "input_image"


def test_chat_url_image_is_replaced_through_typed_seam(tmp_path: Path) -> None:
    adapter = VisionProxyAdapter(
        facts=VisionFacts(cache_path=tmp_path / "cache.sqlite"),
        hooks=VisionProxyHooks(
            enabled_reader=lambda: True,
            vision_model_reader=lambda: "vision-model",
            resolve_upstream=lambda _model: _VISION_UPSTREAM,
            model_supports_image=lambda model, _upstream: model == "vision-model",
            describe_image_override=lambda *_args, **_kwargs: "A rising blue chart.",
        ),
    )
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this."},
                    {
                        "type": "image_url",
                        "image_url": {"url": _IMAGE_URL},
                    },
                ],
            }
        ]
    }

    changed = adapter.apply_chat_payload(
        payload,
        "text-only",
        {"name": "target"},
        target_accepts_images=False,
    )

    assert changed
    replacement = payload["messages"][0]["content"][1]
    assert replacement["type"] == "text"
    assert "A rising blue chart." in replacement["text"]
    assert 'path="codexhub://image/' in replacement["text"]
    assert _IMAGE_URL not in json.dumps(payload)


def test_description_cache_miss_then_hit_avoids_second_model_call(
    tmp_path: Path,
) -> None:
    model_calls: list[str] = []
    events: list[str] = []

    def describe(
        _part: Any,
        model: str,
        _upstream: Any,
        _context: Any,
    ) -> str:
        model_calls.append(model)
        return "Cached visual context."

    adapter = VisionProxyAdapter(
        facts=VisionFacts(cache_path=tmp_path / "cache.sqlite"),
        hooks=VisionProxyHooks(
            describe_image_override=describe,
            write_event=lambda _context, event, **_fields: events.append(event),
        ),
    )
    part = {"type": "input_image", "image_url": _IMAGE_URL}

    first = adapter.description_for_part(
        part,
        "vision-model",
        _VISION_UPSTREAM,
    )
    second = adapter.description_for_part(
        part,
        "vision-model",
        _VISION_UPSTREAM,
    )

    assert first == second == "Cached visual context."
    assert model_calls == ["vision-model"]
    assert events == ["image_proxy_cache_hit"]


class _JsonResponse:
    def __init__(self, body: bytes):
        self.headers = {"Content-Type": "application/json"}
        self.status = 200
        self._body = body

    def __enter__(self) -> _JsonResponse:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        body, self._body = self._body, b""
        return body


def test_malformed_description_response_fails_closed() -> None:
    response = _JsonResponse(
        json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": {"unexpected": "object"},
                        }
                    }
                ]
            }
        ).encode("utf-8")
    )
    adapter = VisionProxyAdapter(
        hooks=VisionProxyHooks(
            compatible_request_body=lambda body, _upstream, **_kwargs: body,
            responses_url=lambda _upstream, _path: "https://vision.example.test/v1/responses",
            upstream_headers=lambda headers, _upstream: dict(headers),
            open_upstream=lambda _request, **_kwargs: response,
        )
    )

    with pytest.raises(ImageProxyError, match="no image description"):
        adapter.call_vision_model_for_description(
            {"type": "input_image", "image_url": _IMAGE_URL},
            "vision-model",
            _VISION_UPSTREAM,
        )
