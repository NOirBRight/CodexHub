# Estimated API prices

`config/model_pricing_snapshot.json` is a prices-only Models.dev snapshot.
Its source URL, retrieval date and SHA-256 identify the reviewed input. It does
not modify the model catalog, capabilities, provider settings or credentials.

Estimates use each model lab's base API tariff in USD per million tokens,
including cache-read rates when available. They do not represent subscription
payments, reseller discounts, peak/off-peak billing or long-context surcharges.
DeepSeek uses its official ordinary tariff regardless of the gateway provider.
Existing explicit Fast model tariffs remain separate.

Refresh with a downloaded, reviewed `https://models.dev/api.json`:

```sh
./scripts/codexhub-python.sh scripts/update_pricing_snapshot.py /path/to/api.json --date YYYY-MM-DD
```

Review the JSON diff before shipping. The importer only selects allowlisted
original labs and known model IDs from maintained visible presets. Explicit
aliases cover selected channel and Ollama names. Missing prices, zero-priced
input/output offers and unresolved aliases remain unknown. No network fetch
occurs while serving requests. The bundled snapshot supersedes stale cached
rates when estimating both historical and new usage; it does not rewrite
usage events. This makes historical figures estimates at snapshot prices,
not reconstructed invoices.

2026-09-07: GPT-5.4 mini cache reads update from the legacy bundled $0.0375
to the snapshot's ordinary API rate of $0.075 per million tokens.
