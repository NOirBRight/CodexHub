# Claude Code subscription authentication through an LLM gateway

Checked 2026-09-23 against the live official Claude Code and Anthropic API documentation. This note answers whether a single Claude Code CLI process can keep a saved claude.ai subscription login while sending requests through an LLM gateway, and separates documented behavior from behavior the docs do not promise.

## Findings

### A saved subscription login can remain active while inference uses a gateway URL

The official [Other LLM gateways guide](https://code.claude.com/docs/en/llm-gateway#subscriptions-and-gateways) now explicitly documents this configuration: `ANTHROPIC_BASE_URL` alone points requests at a gateway without replacing a saved claude.ai login. The subscription remains the active credential and its usage limits and billing apply. Setting a gateway credential (`ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, or `apiKeyHelper`) changes the result: that credential replaces the subscription for the session, and the gateway credential owner's account is billed instead. The [connection guide](https://code.claude.com/docs/en/llm-gateway-connect#conflicts-with-an-existing-login) likewise says the login remains saved but unused while a gateway credential variable is active.

This is an explicit, documented way for one CLI session to send requests through a gateway while keeping the user's subscription authentication active. It does not document use of a non-Claude model as a supported subscription feature: the same [gateway guide](https://code.claude.com/docs/en/llm-gateway) says Anthropic does not support routing Claude Code to non-Claude models through any gateway.

### The gateway must preserve the OAuth capability in the Anthropic Messages request

The [gateway compatibility guide's request-header reference](https://code.claude.com/docs/en/llm-gateway-protocol#request-headers) says that when a saved claude.ai login is used with `ANTHROPIC_BASE_URL` and no gateway credential, `anthropic-beta` also carries an OAuth capability required by the upstream; stripping it causes `401` responses. It instructs gateway operators to forward the complete `anthropic-beta` header verbatim, rather than allowlisting a fixed set of beta values, because the set changes over Claude Code releases. The [Anthropic Messages API format section](https://code.claude.com/docs/en/llm-gateway-protocol#api-formats) also requires forwarding `anthropic-beta` and `anthropic-version` unchanged. The gateway guide links the [Anthropic beta headers reference](https://platform.claude.com/docs/en/api/beta-headers) for current values; the Claude Code compatibility guide describes the OAuth capability but does not print its literal beta token.

For an Anthropic Messages gateway, the compatibility guide lists `POST /v1/messages` as the inference endpoint and `/v1/messages/count_tokens` as optional. The count endpoint is optional because Claude Code falls back to an approximate character-based context estimate when it is absent. These endpoints are common to the Anthropic-format connection; the guide does not define a separate inference endpoint for each model.

### Model selection and gateway model discovery do not select a client-side endpoint

The [model configuration guide](https://code.claude.com/docs/en/model-config) states directly that `ANTHROPIC_BASE_URL` changes where requests are sent, not which model answers them. Claude Code selects model IDs through `/model`, `--model`, `ANTHROPIC_MODEL`, settings, and alias variables such as `ANTHROPIC_DEFAULT_OPUS_MODEL`. `modelOverrides` maps Anthropic model IDs to provider-specific model identifiers sent to the API; it is not documented as a URL or endpoint mapping.

For an Anthropic Messages gateway, [model discovery](https://code.claude.com/docs/en/llm-gateway-protocol#model-discovery) is an opt-in (`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`) startup `GET /v1/models?limit=1000` that adds returned model IDs to the `/model` picker. It is off by default, only applies to the Anthropic Messages format, and filters discovered IDs to entries containing `claude` or `anthropic`. Discovery changes the selectable model list; it does not change the base URL or specify how the gateway routes a selected ID upstream.

The discovery section describes its credentials as `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, or `apiKeyHelper` values and says it skips discovery when neither credential header resolves. It does not say that saved subscription OAuth is used to authenticate the `/v1/models` request. Therefore, subscription-only authentication should not be assumed to enable model discovery; that behavior is not established by the docs reviewed here.

The [settings reference for `modelPicker`](https://code.claude.com/docs/en/settings-reference#modelpicker) offers a separate way to list model IDs explicitly. Its `options` accept aliases, Anthropic model IDs, and provider-format IDs including LLM gateway IDs. By default, `replaceBuiltInOptions` is `false`, so these rows are appended after the built-in lineup; setting it to `true` replaces the built-in lineup and also hides rows supplied by gateway discovery and `ANTHROPIC_CUSTOM_MODEL_OPTION`. This setting requires Claude Code v2.1.242 or later. A row label affects display only. This config controls the picker, not the request endpoint or support status of a model provider.

The docs describe `ANTHROPIC_BASE_URL` as the gateway address for the process and the model settings above as model-ID selection or mapping. They do not document a per-model client-side base URL, nor a mode in which one selected model bypasses the gateway and another uses it. A gateway can implement its own upstream routing after receiving the request, but that is gateway-side behavior. Anthropic's support limitation for non-Claude model routing still applies.

### `claude gateway` is a distinct enterprise gateway sign-in path

The [Claude apps gateway guide](https://code.claude.com/docs/en/claude-apps-gateway) describes `claude gateway --config gateway.yaml` as starting Anthropic's self-hosted enterprise gateway server from the `claude` binary. Developers sign in to it using corporate OIDC, while the gateway stores credentials for configured upstreams and provides per-group model access, managed settings, and telemetry. It supports Anthropic API and specified cloud-provider upstreams; the [configuration reference](https://code.claude.com/docs/en/claude-apps-gateway-config#upstreams) says upstreams are ordered and inference goes to the first one that resolves the requested model, with failover on specified errors.

That product is not the subscription-preserving `ANTHROPIC_BASE_URL`-only mode. The [Claude apps gateway guide](https://code.claude.com/docs/en/claude-apps-gateway#whats-enforced-on-developers) says that after `/login`, the gateway token is the session's only credential and earlier claude.ai logins are ignored. It is an organization-operated identity and upstream-credential model, with Anthropic API as one possible upstream.

### Mid-session model changes through the Agent SDK are documented; a raw stream-json request is not

The official [TypeScript Agent SDK reference](https://code.claude.com/docs/en/agent-sdk/typescript#query-object) publishes `Query.setModel(model?)`; it changes the current session model and is available only in streaming input mode. Passing `undefined` or `"default"` resets to Claude Code's default model. The [Python Agent SDK reference](https://code.claude.com/docs/en/agent-sdk/python#claudesdkclient) likewise publishes `ClaudeSDKClient.set_model(model=None)` to change the current session model, with `None` resetting it; its client API is used for streaming conversations.

These are supported Agent SDK methods for changing a live session's model. The [CLI reference](https://code.claude.com/docs/en/cli-reference) documents `--input-format stream-json` for print mode, but the official docs reviewed do not publish a raw `control_request` subtype named `set_model`, its JSON envelope, or a direct stdin protocol for changing the model. Thus SDK-driven mid-session switching is documented; directly emitting a raw stream-json `set_model` control request is not documented by the sources reviewed.

## Evidence and limits

**Explicitly documented:** base URL alone preserves the saved subscription login; a gateway credential supersedes it; OAuth capability in `anthropic-beta` must be retained when forwarding to Anthropic; gateway model discovery and custom `modelPicker` options can populate the picker; enterprise gateway sign-in uses a gateway token and separate upstream credentials; Agent SDK streaming sessions can change models via `setModel` / `set_model`; Anthropic does not support non-Claude model routing through gateways.

**Not established by the docs:** per-model client endpoint selection within one process; using the saved subscription login as authorization for a non-Claude model; using subscription OAuth to authenticate gateway `/v1/models` discovery; a raw stream-json `set_model` control request; or support guarantees for translating Claude Code requests to a non-Claude model. The docs permit a gateway to expose model IDs and describe gateway-side upstream selection, but do not turn those capabilities into a supported subscription-backed non-Claude model path.

## Primary sources

- [Other LLM gateways](https://code.claude.com/docs/en/llm-gateway) — subscription and gateway credential behavior; Anthropic's support boundary for non-Claude models.
- [Connect Claude Code to an LLM gateway](https://code.claude.com/docs/en/llm-gateway-connect) — credential variables, login conflicts, and the Anthropic Messages test endpoint.
- [Claude Code gateway compatibility guide](https://code.claude.com/docs/en/llm-gateway-protocol) — API formats, required headers, OAuth capability note, and model discovery.
- [Model configuration](https://code.claude.com/docs/en/model-config) — model selection, aliases, custom IDs, and `modelOverrides`.
- [Settings reference: `modelPicker`](https://code.claude.com/docs/en/settings-reference#modelpicker) — custom picker rows, append/replace behavior, and minimum version.
- [Claude apps gateway](https://code.claude.com/docs/en/claude-apps-gateway) and [configuration reference](https://code.claude.com/docs/en/claude-apps-gateway-config) — enterprise gateway server, sign-in, and upstream routing.
- [Agent SDK TypeScript reference](https://code.claude.com/docs/en/agent-sdk/typescript#query-object), [Agent SDK Python reference](https://code.claude.com/docs/en/agent-sdk/python#claudesdkclient), and [CLI reference](https://code.claude.com/docs/en/cli-reference) — supported live-session model switching and documented stream-json CLI mode.
