import json
from pathlib import Path
import sys


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    assert isinstance(value, dict)
    return value


def exact(value: dict, keys: set[str]) -> None:
    assert isinstance(value, dict)
    assert set(value) == keys


def main(case_root: Path) -> None:
    root_url = "http://127.0.0.1:19190"
    catalog = read_json(
        case_root / "appdata" / "roaming" / "ZCode" / "model-providers" / "codexhub.json"
    )
    cache = read_json(case_root / ".zcode" / "v2" / "bots-model-cache.v2.json")
    config = read_json(case_root / ".zcode" / "v2" / "config.json")
    for collection in (catalog, cache):
        exact(collection, {"schemaVersion", "providers"})
        assert collection["schemaVersion"] == "zcode.model-providers.v2"
        assert isinstance(collection["providers"], list)
    exact(config, {"provider"})
    exact(
        config["provider"],
        {"codexhub-openai", "codexhub-volc"},
    )
    expected = {
        "codexhub-openai": ("openai", "gpt-5.6-luna"),
        "codexhub-volc": ("volc", "glm-5.2"),
    }
    provider_keys = {
        "id",
        "name",
        "enabled",
        "source",
        "apiFormat",
        "endpoints",
        "apiKeyRequired",
        "apiKey",
        "defaultKind",
        "models",
        "createdAt",
        "updatedAt",
    }
    catalog_model_keys = {
        "id",
        "name",
        "kinds",
        "defaultKind",
        "modalities",
        "maxOutputTokens",
    }
    config_provider_keys = {
        "name",
        "kind",
        "enabled",
        "source",
        "apiFormat",
        "endpoints",
        "options",
        "models",
    }
    for provider_id, (route, model_id) in expected.items():
        catalog_matches = [item for item in catalog["providers"] if item.get("id") == provider_id]
        cache_matches = [item for item in cache["providers"] if item.get("id") == provider_id]
        assert len(catalog_matches) == len(cache_matches) == 1
        for provider in (catalog_matches[0], cache_matches[0]):
            exact(provider, provider_keys)
            assert provider["enabled"] is True
            assert provider["source"] == "custom"
            assert provider["apiFormat"] == "openai-responses"
            assert provider["apiKeyRequired"] is True
            assert isinstance(provider["apiKey"], str) and provider["apiKey"]
            assert provider["defaultKind"] == "openai"
            assert isinstance(provider["models"], list) and len(provider["models"]) == 1
            model = provider["models"][0]
            exact(model, catalog_model_keys)
            exact(model["modalities"], {"input", "output"})
            assert model["id"] == model_id
            assert model["kinds"] == ["openai"]
            assert model["defaultKind"] == "openai"
            assert isinstance(model["modalities"]["input"], list)
            assert isinstance(model["modalities"]["output"], list)
            assert model["maxOutputTokens"] == 32768
        assert catalog_matches[0]["endpoints"] == {
            "baseURL": root_url,
            "paths": {"openai": f"/v1/providers/{route}/responses"},
        }
        provider_url = f"{root_url}/v1/providers/{route}"
        assert cache_matches[0]["endpoints"] == {
            "baseURL": provider_url,
            "paths": {"openai": "/responses"},
        }
        config_provider = config["provider"][provider_id]
        exact(config_provider, config_provider_keys)
        exact(config_provider["options"], {"baseURL", "apiKey", "apiKeyRequired"})
        assert config_provider["kind"] == "openai"
        assert config_provider["enabled"] is True
        assert config_provider["source"] == "custom"
        assert config_provider["apiFormat"] == "openai-responses"
        assert config_provider["endpoints"] == {
            "baseURL": provider_url,
            "paths": {"openai": "/responses"},
        }
        assert config_provider["options"]["baseURL"] == provider_url
        assert config_provider["options"]["apiKeyRequired"] is True
        assert isinstance(config_provider["options"]["apiKey"], str)
        exact(config_provider["models"], {model_id})
        config_model = config_provider["models"][model_id]
        exact(config_model, {"name", "limit", "modalities"})
        exact(config_model["limit"], {"output"})
        exact(config_model["modalities"], {"input", "output"})
        assert config_model["limit"]["output"] == 32768
        assert isinstance(config_model["modalities"]["input"], list)
        assert isinstance(config_model["modalities"]["output"], list)
    assert len(catalog["providers"]) == len(cache["providers"]) == 2


if __name__ == "__main__":
    try:
        main(Path(sys.argv[1]))
    except Exception:
        raise SystemExit(1)
