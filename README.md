# CodexHub

A local proxy layer for [OpenAI Codex](https://developers.openai.com/codex/) that lets you use official OpenAI models and third-party providers (Ollama Cloud, Volcano Engine, MiniMax.cn, etc.) side by side in the Codex desktop app.

## What it does

- **Multi-provider routing**: Transparently routes requests to official OpenAI or third-party endpoints based on model name.
- **Catalog sync**: Discovers available models from Ollama Cloud and external providers, generates a unified catalog for Codex App.
- **History management**: Normalizes and migrates conversation history when switching between official and custom providers.
- **Config switching**: One-command switch between official (`openai`) and custom (`custom`) provider configs without losing conversations.
- **Provider health**: Probes upstream endpoints and reports availability.

## Project structure

```
CodexHub/
  src-python/             # Core Python modules
    codex_proxy.py        # HTTP proxy server (routing, SSE relay, reasoning handling)
    catalog.py            # Catalog model loading and policy filtering
    catalog_sync.py       # Ollama Cloud + provider catalog discovery and generation
    providers_config.py   # External provider config loader for config/providers.toml
    config_overlay.py     # Codex config.toml overlay writer
    global_state_repair.py# .codex-global-state.json sanitizer
    history_overlay.py    # Session JSONL provider-label normalizer
    history_consolidate.py# History merge and consolidation
    bucket_sync.py        # Legacy bucket sync helpers
    probe_provider_endpoints.py # Upstream endpoint probes
  scripts/                # PowerShell launcher and mode-switch scripts
  config/                 # Default policy and config templates
    catalog_policy.toml   # Routing rules, model allow/deny lists, display names
    providers.toml        # External provider endpoints, models, and env key bindings
  tests/                  # Python unittest suite
  frontend/               # (planned) Settings UI
```

## Quick start

1. Install Python 3.12+ (needs `tomllib`).
2. Review `config/catalog_policy.toml` and `config/providers.toml`.
3. Set provider API keys as environment variables referenced by `config/providers.toml`.
4. Run the proxy:
   ```powershell
   $env:PYTHONPATH='src-python'; python src-python/codex_proxy.py --port 9099
   ```
5. Switch Codex App to custom provider:
   ```powershell
   scripts\codex-mode.cmd proxy
   ```

## Configuration

See `config/catalog_policy.toml` for routing rules, model visibility, and display names.

Provider endpoints and model aliases are read from `config/providers.toml`; secrets stay in environment variables.

## License

MIT (planned)
