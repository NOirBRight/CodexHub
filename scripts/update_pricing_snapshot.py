"""Build an offline, prices-only snapshot from a reviewed Models.dev download."""
from __future__ import annotations

try:
    from scripts.python_runtime_contract import require_python_313
except ModuleNotFoundError:
    from python_runtime_contract import require_python_313
require_python_313(__file__)

import argparse
import hashlib
import json
import math
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]
# Original labs only. Never infer an official price from reseller offers.
OWNERS = {
    "gpt-": "openai", "claude-": "anthropic", "deepseek-": "deepseek",
    "grok-": "xai", "muse-": "meta", "glm-": "zai",
    "kimi-": "moonshotai", "minimax-": "minimax", "gemini-": "google",
    "qwen": "alibaba", "mimo-": "xiaomi", "step-": "stepfun",
    "nemotron-": "nvidia", "mistral-": "mistral",
}
ALIASES = {
    "qwen3.5:397b": "qwen3.5-397b-a17b",
    "mistral-large-3:675b": "mistral-large-2512",
    "nemotron-3-super": "nvidia/nemotron-3-super-120b-a12b",
    "nemotron-3-ultra": "nvidia/nemotron-3-ultra-550b-a55b",
    "minimax-m2.7-free": "minimax-m2.7",
}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--date", required=True, help="UTC retrieval date, YYYY-MM-DD")
    args = parser.parse_args()
    raw = args.source.read_bytes()
    upstream = json.loads(raw)
    presets = tomllib.loads((ROOT / "config/providers.toml").read_text())
    wanted = {"gpt-6-astra", "gpt-5.3-codex-spark"}
    for provider in presets["providers"]:
        if provider["id"] in {"ollama-cloud", "commandcode", "opencode-go", "xai"}:
            wanted.update(model["id"] for model in provider.get("models", []))
    entries, unresolved = [], []
    for model_id in sorted(wanted):
        bare = model_id.split("/")[-1].lower()
        canonical = ALIASES.get(bare, bare)
        owner = next((owner for prefix, owner in OWNERS.items() if bare.startswith(prefix)), None)
        models = upstream.get(owner, {}).get("models", {})
        match = next((key for key in models if key.lower() == canonical), None)
        cost = models.get(match, {}).get("cost") or {}
        # Zero is not evidence of a normal paid API tariff. Keep it unknown.
        valid = all(isinstance(cost.get(key), (int, float)) and math.isfinite(cost[key])
                    and cost[key] > 0 for key in ("input", "output"))
        if not valid:
            unresolved.append(model_id)
            continue
        cached = cost.get("cache_read")
        if not isinstance(cached, (int, float)) or not math.isfinite(cached) or cached < 0:
            cached = None
        entries.append({"id": model_id, "official_source": owner, "official_model": match,
                        "input_per_million": cost["input"],
                        "cached_input_per_million": cached, "output_per_million": cost["output"]})
    result = {"source_url": "https://models.dev/api.json", "retrieved_at": args.date,
              "source_sha256": hashlib.sha256(raw).hexdigest(), "currency": "USD",
              "basis": "Official API base tariff estimate; no provider discounts or context/time tiers.",
              "entries": entries, "unresolved": unresolved}
    (ROOT / "config/model_pricing_snapshot.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"Pricing snapshot: {len(entries)} priced IDs, {len(unresolved)} unresolved IDs")

if __name__ == "__main__":
    main()
