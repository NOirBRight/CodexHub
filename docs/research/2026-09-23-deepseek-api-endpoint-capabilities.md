# DeepSeek API endpoint capabilities

Checked against DeepSeek's official API documentation on 2026-09-23.

## Endpoint and model matrix

| Protocol | Documented base URL | Operation | Model IDs |
| --- | --- | --- | --- |
| OpenAI Chat Completions | `https://api.deepseek.com` | `POST /chat/completions` | `deepseek-flash`, `deepseek-v4-pro` |
| Anthropic Messages | `https://api.deepseek.com/anthropic` | Anthropic SDK `messages.create(...)`; the compatibility guide refers to `/messages` | `deepseek-flash`, `deepseek-v4-pro` |
| OpenAI Responses | `https://api.deepseek.com` | Responses API | `deepseek-flash`, `deepseek-v4-pro` |

The Anthropic guide specifies the base URL, and the Claude Code integration uses it as `ANTHROPIC_BASE_URL`. The guide does not state the fully expanded HTTP URL including the API version. With the standard Anthropic Messages SDK, the expected wire path is `/anthropic/v1/messages`; treat that joined path as an SDK-derived value, not a literal full URL published by DeepSeek. DeepSeek's compatibility details mention `/messages` without the version prefix.

DeepSeek's current model table maps `deepseek-flash` to **DeepSeek-V4.1-Flash** and `deepseek-v4-pro` to **DeepSeek-V4-Pro-0813**. It lists both models as supporting Chat Completions, Responses, Anthropic API, tool calls, and JSON output. The table gives a 1M context length and a maximum output of 384K. It says `deepseek-v4-flash` and `deepseek-v4-flash-vision-exp` remain accepted legacy names, but those models are retired and requests are served by V4.1 Flash.

## Auth and balance

The API reference declares HTTP Bearer authentication for DeepSeek API calls. Claude Code's official integration uses `ANTHROPIC_AUTH_TOKEN` with the same DeepSeek API key; the Anthropic compatibility guide also marks `x-api-key` as supported.

Balance is available from `GET /user/balance` on the OpenAI-format API base. Its documented response shape is:

```json
{
  "is_available": true,
  "balance_infos": [
    {
      "currency": "CNY",
      "total_balance": "110.00",
      "granted_balance": "10.00",
      "topped_up_balance": "100.00"
    }
  ]
}
```

`balance_infos` can contain `CNY` or `USD` entries. Amounts are strings; `total_balance` includes the unexpired granted amount and topped-up amount. `is_available` indicates whether the balance is sufficient for API calls. The docs do not describe per-model or per-endpoint balances, so this is a provider-account-level query.

## Anthropic compatibility limits

The Anthropic compatibility guide explicitly supports streaming, system prompts, stop sequences, temperature, thinking, and tool definitions/tool choice. Some fields are ignored: `anthropic-version`, `anthropic-beta` for Messages, `top_k`, cache-control fields, and `thinking.budget_tokens`. Only `output_config.effort` is supported within `output_config`; `top_p` only affects thinking mode and has a lower bound of 0.95. Document and search-result content blocks, redacted thinking, code-execution results, and MCP tool-use/result blocks are listed as unsupported. Model names beginning with `claude-opus` map to `deepseek-v4-pro`; names beginning with `claude-haiku` or `claude-sonnet` map to `deepseek-flash`. An unsupported model name falls back to `deepseek-flash`, according to the guide.

## Provider configuration implication

Represent DeepSeek as one provider credential with independently selectable protocol routes: Chat Completions for OpenAI-compatible chat clients, Anthropic Messages for Claude Code, and Responses for clients such as Codex. Keep the API key and balance query at provider scope; select protocol/base URL and model on the client route or capability profile. The two requested Claude/Chat routes can therefore share one DeepSeek account while using different request and response adapters.

## Official sources

- [Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing) — model IDs, model versions, endpoint formats, and listed capabilities.
- [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion) — `POST /chat/completions` and request schema.
- [Using the Anthropic API](https://api-docs.deepseek.com/guides/anthropic_api) — Anthropic base URL, SDK example, model mapping, authentication headers, and compatibility matrix.
- [Integrate with Claude Code](https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code) — Claude Code environment variables and model aliases.
- [Using the Responses API](https://api-docs.deepseek.com/guides/responses_api) — Responses base URL and SDK usage.
- [Get User Balance](https://api-docs.deepseek.com/api/get-user-balance) — balance route, auth, and response schema.
- [DeepSeek API authentication](https://api-docs.deepseek.com/api/deepseek-api) — bearer authentication scheme.
- [Lists Models](https://api-docs.deepseek.com/api/list-models) — `GET /models`, which returns currently available model IDs.
