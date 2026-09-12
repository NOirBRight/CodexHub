from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "e2e_image_tool_compact.py"


def _runner():
    spec = importlib.util.spec_from_file_location("e2e_image_tool_compact", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_non_media_text_scan_ignores_image_url_but_catches_stringified_output() -> None:
    runner = _runner()
    marker = "data:image/png;base64,UNIQUE_E2E_MARKER"
    lifted = {
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Tool result image 1/1 from call_id=c1."},
                    {"type": "input_image", "image_url": marker},
                ],
            }
        ]
    }
    stringified = {
        "input": [
            {
                "type": "message",
                "role": "developer",
                "content": "Read-only Codex function result transcript\noutput:\n" + marker,
            }
        ]
    }
    assert marker not in runner.collect_non_media_text(lifted)
    assert marker in runner.collect_non_media_text(stringified)
    assert runner.count_user_input_images(lifted) == 1
    assert runner.count_user_input_images(stringified) == 0


def test_rewrite_xai_base_url_only_rewrites_the_xai_provider() -> None:
    runner = _runner()
    original = (
        'id = "xai"\nname = "xAI"\nbase_url = "https://api.x.ai/v1"\n'
        'id = "opencode-go"\nbase_url = "https://api.x.ai/v1"\n'
    )
    rewritten = runner.rewrite_xai_base_url(original, "http://127.0.0.1:9/v1")
    assert rewritten.startswith('id = "xai"\nname = "xAI"\nbase_url = "http://127.0.0.1:9/v1"')
    assert rewritten.count("https://api.x.ai/v1") == 1
