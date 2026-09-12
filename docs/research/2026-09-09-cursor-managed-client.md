# Cursor as a CodexHub managed client: evidence and recommended operation

Date: 2026-09-09
Scope: Research only. This note records whether Cursor can join CodexHub's
official managed-client roster under Provider Injection (ADR-0004). It does
not add, imply, or authorize a production Cursor adapter, a Maintained
Provider that consumes Cursor subscription traffic, or a rewrite of Cursor
BYOK settings.

## Evidence boundary and source pins

All protocol and product claims below are from Cursor's own documentation,
the local official CLI/IDE install, or CodexHub's own ADRs and managed-client
seam. Community reverse-engineering of `api2.cursor.sh` is cited only as a
rejected path.

| Source | Pinned / accessed evidence | What it establishes |
| --- | --- | --- |
| [Bring your own API key](https://cursor.com/help/models-and-usage/api-keys.md) | Accessed 2026-09-09 | BYOK providers are OpenAI, Anthropic, Google, Azure, AWS Bedrock. Keys leave the device; every request is routed through Cursor's servers for prompt building. Chat only; Tab stays on Cursor models. |
| [Models & Pricing](https://cursor.com/docs/models-and-pricing.md) | Accessed 2026-09-09 | Cursor Models vs Other Models usage pools. No local-gateway or OpenAI-compatible custom endpoint. |
| [AWS Bedrock](https://cursor.com/docs/customizing/aws-bedrock.md) | Accessed 2026-09-09 | Custom routing is IAM-role / AWS credentials into Bedrock, still via Cursor, not a user-owned HTTP base URL. |
| [CLI configuration](https://cursor.com/docs/cli/reference/configuration.md) | Accessed 2026-09-09 | `~/.cursor/cli-config.json` schema: permissions, display, selected model. No provider map, no `baseURL`, no API key injection point. |
| [CLI ACP](https://cursor.com/docs/cli/acp.md) | Accessed 2026-09-09 | Official integration for *hosting* Cursor's agent (`agent acp` over stdio). Cursor owns the turn and tools. |
| [TypeScript SDK](https://cursor.com/docs/sdk/typescript.md) | Accessed 2026-09-09 | `@cursor/sdk` runs Cursor's agent. "Local" means local files, not local models. All inference goes through Cursor-hosted models. Not a chat-completions API. |
| [Cursor APIs overview](https://cursor.com/docs/api.md) | Accessed 2026-09-09 | Cloud Agents API / SDKs are agent workflows, "not a standalone model-inference or chat-completions API". |
| [Model and integration management](https://cursor.com/docs/enterprise/model-and-integration-management.md) | Accessed 2026-09-09 | Enterprise BYOK controls restrict personal API keys. No custom OpenAI-compatible provider type. |
| ADR-0004 | Repo `docs/adr/0004-coexistence-provider-injection.md` | Managed clients inject one `codexhub` block, never activate, never rewrite foreign providers. |
| Local Cursor IDE | `cursor` shim; update URL `.../linux-x64/cursor/3.19.13/...` | Installed IDE is 3.19.13 on this host. User `settings.json` has no model-provider map. |
| Local Cursor CLI | `cursor-agent --version` → `2026.09.02-c22c1a3` | Official CLI. `--endpoint` defaults to `https://api2.cursor.sh`. `--list-models` lists account models, not a custom catalog. |

### CLI and platform pin

Verified locally without sending an inference request:

| Item | Observation |
| --- | --- |
| Cursor IDE | 3.19.13 (Linux x64 stable update channel) |
| Cursor Agent CLI | 2026.09.02-c22c1a3 (`cursor-agent`) |
| User settings | `~/.config/Cursor/User/settings.json` — editor prefs only |
| CLI config | `~/.cursor/cli-config.json` — selected model `default`/`auto`; `serverConfigCache.backendUrl` = `https://api2.cursor.sh` |
| ACP config | `~/.cursor/acp-config.json` = `{}` |

A later implementation spike must re-pin the exact IDE and `cursor-agent` versions it actually uses.

## Two product questions (do not mix)

1. **CodexHub → Cursor (managed client).** Make Cursor IDE/CLI consume the local Gateway the way OpenCode, Pi, OMP, ZCode, and DSH do.
2. **Cursor → other clients (Maintained Provider).** Expose Cursor subscription models through the Gateway to Codex / OpenCode / Pi. This is the `dsh-llm-cursor` / Oh My Pi `cursor` provider class.

Question 1 is the CodexHub managed-client campaign. Question 2 is a ToS and product-boundary reject for an officially maintained client. Both are recorded so a later issue cannot silently switch questions.

## CodexHub contract the new client would have to meet

From ADR-0004 and the current coordinator (`src-tauri/src/gateway/managed_clients.rs`):

- Inject exactly one provider entry (Injected Block, route key `codexhub`).
- Preserve every user-owned provider and setting.
- Never flip the client's global default model (activation is the user's action).
- Credentials are one surgical key; detach removes only the block + key.
- Readback is block-fingerprint, not whole-file byte compare.
- Adapters expose `metadata()` / `inspect()` / `plan()`; the coordinator publishes.

The current official roster in `list_gateway_clients` is: Generic (copy-only), OpenCode, ZCode, Pi, OMP, DSH. Codex stays a history-bucket exception. Adding Cursor means a new adapter that satisfies the same seam, plus isolated apply/readback and a real-client E2E pin (`docs/agents/real-client-e2e.md`).

Gateway endpoints the Injected Block would advertise today:

- `http://127.0.0.1:<port>/v1`
- `/v1/models`, `/v1/chat/completions`, `/v1/responses`

That is a loopback OpenAI-compatible HTTP service. The client must call it *directly*.

## Verified Cursor surfaces

### A. Official agent products (Cursor owns the harness)

These are supported, documented, and the wrong abstraction for CodexHub Gateway:

| Surface | Role | Inference | Fits Provider Injection? |
| --- | --- | --- | --- |
| Cursor IDE Agent | Full IDE | Cursor-hosted models | No. No injectable provider map. |
| `cursor-agent` / `agent` CLI | Terminal agent | Cursor-hosted; `--endpoint` is Cursor's API | No. Config has no Gateway block. |
| `agent acp` | ACP server for a custom shell | Cursor-hosted | No. DSH-style host, not a Gateway client. |
| `@cursor/sdk` / Cloud Agents API | Programmable Cursor agent | Cursor-hosted even in "local" mode | No. Explicitly not chat-completions. |

Use these if the product intent is "run Cursor's agent inside DSH / another shell". That work lives in `dsh-acp-cursor`, not in CodexHub.

### B. Official BYOK (closest thing to "bring a provider")

[Help: Bring your own API key](https://cursor.com/help/models-and-usage/api-keys.md) is the only documented way to point Cursor at *your* models. Facts that block a local Gateway:

1. Provider list is closed: OpenAI, Anthropic, Google, Azure, AWS Bedrock. There is no "OpenAI-compatible" / Ollama / custom base URL type.
2. UI path is **Cursor Settings → Models → paste key → Save**. There is no documented file CodexHub can surgically edit as an Injected Block. Local `settings.json` on this host has no provider keys at all; BYOK is not a coexistence JSON map like OpenCode `provider` or DSH `llm-pi-ai.providers`.
3. **"Your API key is not stored on our servers. It is sent to our backend with every request because all requests are routed through Cursor's servers for final prompt building."** A `http://127.0.0.1:9099/v1` Gateway is unreachable from Cursor's cloud. Even if someone stuffed the Gateway client key into the OpenAI slot, Cursor's backend would call OpenAI, not CodexHub.
4. BYOK is chat-only. Tab completion stays on Cursor models. That is fine for Gateway, but it confirms Cursor will not treat the Injected Block as the whole product.
5. OpenAI BYOK is limited to "standard, non-reasoning chat models". Official Codex reasoning models that CodexHub users actually want would not be in that picker even if the network path existed.
6. Enterprise can disable personal BYOK entirely.

Historical "Override OpenAI Base URL" is **not** in the current help or docs index. Do not design an adapter on a removed or undocumented control.

### C. CLI config is not a provider file

`~/.cursor/cli-config.json` (and Windows `%USERPROFILE%\.cursor\cli-config.json`) holds the selected model, permissions, sandbox, and a cache of `backendUrl: https://api2.cursor.sh`. Writing a CodexHub `baseURL` there has no documented effect. `CURSOR_API_ENDPOINT` / `--endpoint` retargets the *Cursor* backend, not an OpenAI-compatible Gateway. Pointing it at CodexHub would be protocol impersonation of `api2.cursor.sh`, not Provider Injection.

Project `.cursor/cli.json` may only override permissions.

### D. Rejected: Cursor subscription as a Gateway upstream

There is no official `POST /v1/chat/completions` on `api.cursor.com` for subscription models. Cursor's public APIs say so. The working unofficial path (Deep Control PKCE + HTTP/2 Connect/protobuf `AgentService/Run` on `api2.cursor.sh`) is what `dsh-llm-cursor` and Oh My Pi already ship, with an explicit ToS / ban warning.

An officially maintained CodexHub client must not grow a Maintained Provider on that path. It is the opposite of transparent official proxying (Codex login → official models). It would also put Cursor account enforcement on CodexHub users.

## Fit against ADR-0004

| ADR-0004 rule | Cursor today |
| --- | --- |
| Inject one block, keep foreign providers | No provider map to inject into. BYOK overwrites a first-party OpenAI/Anthropic/Google slot. |
| Never activate | BYOK *is* activation of that provider's models. No separate user-owned switch CodexHub can leave alone. |
| Surgical credential | Keys live in Cursor's secret store / are forwarded to Cursor's backend, not a sibling `.credentials.yaml`. |
| Loopback Gateway traffic | BYOK traffic is built and sent from Cursor servers. 127.0.0.1 never sees it. |
| Block-fingerprint readback | No owned file content to fingerprint. |
| Isolated E2E apply | No documented config file the runner can copy as the client's consumed root. |

This is closer to the Claude Code research outcome (`docs/research/2026-07-12-claude-code-external-client-support.md`) than to OpenCode/DSH: the official product is an agent harness, not a pluggable OpenAI-compatible client.

## Recommended operation

### Do not ship a Cursor Connect adapter in the current campaign

Do not add `cursor` to `ISOLATED_MANAGED_CLIENTS`, `adapter_for`, or `list_gateway_clients` as `auto_apply_supported: true`. A Connect button that rewrites BYOK or `cli-config.json` would be Managed Takeover of the wrong protocol, fail closed on localhost, or both.

### If the ask is "use Gateway models from Cursor IDE"

Honest product for now:

1. Keep **Generic OpenAI-compatible** as the only copy surface.
2. If Cursor must appear on the Gateway page, make it **detect-only / copy-only**, same class as Generic: installed-or-not, no apply, status text that BYOK cannot reach a loopback Gateway.
3. Write the limitation in the card, not in a blog post after users click Connect.
4. Revisit only when Cursor documents a **direct** OpenAI-compatible custom endpoint (client → user URL, not client → Cursor cloud → provider) **and** a coexistence file or API for that entry.

Admission bar for a future adapter (all must be true):

- Primary docs describe a custom provider with `baseURL` + `apiKey` that the **local process** calls.
- The entry can sit beside Cursor's own models without replacing OpenAI/Anthropic keys.
- CodexHub can name the file(s), inject `codexhub` only, detach surgically, and fingerprint the block.
- A pinned IDE/CLI version completes an isolated apply/readback plus one live `/v1/chat/completions` stream from that client to the Gateway.

Until then, Cursor is not an officially maintained managed client.

### If the ask is "run Cursor's agent inside our official client" (DSH)

That is not a CodexHub Gateway client. The official path is already `cursor-agent acp` / `@cursor/sdk`. Keep it in the DSH plugin (`dsh-acp-cursor`). Do not route that traffic through Gateway.

### If the ask is "expose Cursor subscription models to OpenCode/Codex"

Reject for the official roster. Point at the existing unofficial plugin and its ban warning. Do not add a `cursor` row to `config/providers.toml`.

## Implementation sketch (only after the admission bar)

Not authorized. Recorded so a later issue does not reinvent the seam:

1. New `clients::cursor` adapter behind the DM-4 coordinator; descriptor first, no `gateway.rs` match-arm special case.
2. Detection: IDE `~/.config/Cursor` / `%APPDATA%\Cursor`; CLI `cursor-agent` / `~/.cursor/cli-config.json`.
3. Shape: `SingleBlock` only if Cursor grows a provider map; otherwise stay copy-only.
4. Wire: Chat Completions first (`/v1/chat/completions` stream). Do not assume Responses until a captured Cursor request proves it.
5. Restart disclosure: Cursor IDE typically needs a reload after model-provider changes; confirm on the pinned version.
6. Real-client E2E: new isolated layout + CLI-only case if `cursor-agent` can be pointed at Gateway; GUI evidence only if IDE apply is in scope.

## Open unknowns (need a sanctioned smoke, not guessing)

- Whether any remaining undocumented IDE setting still overrides OpenAI base URL **and** dials that URL from the local process. A smoke must use a throwaway key and a local listener; do not infer from old forum posts.
- Exact BYOK request shape Cursor's backend sends to OpenAI (chat vs responses, reasoning, tools). Irrelevant to localhost until the network path exists.
- Whether `cursor-agent --endpoint` can ever be a compatible OpenAI root. Current docs say it is the Cursor API endpoint; treat impersonation as out of scope.

## Decision

**Do not add Cursor to the officially maintained managed-client list in 0.1.9.** Treat it as copy-only or omit it. Reopen only against a Cursor-owned document that satisfies the admission bar above.

## Addendum: custom OpenAI Base URL (same-day follow-up)

Date: 2026-09-09. Trigger: the product *does* expose a custom base URL; the first pass only read the BYOK help page, which omits it.

Evidence is from the installed Cursor IDE AppImage (`3.19.13`, workbench `out/vs/workbench/workbench.desktop.main.js`) plus the same help page as above. No live Agent request was sent.

### What exists in the IDE

**Cursor Settings → Models → OpenAI API Key**, with a checkbox **Override OpenAI Base URL** ("Change the base URL for OpenAI API requests.").

Persistent fields in `applicationUserPersistentStorage` (SQLite ItemTable key `src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl.persistentStorage.applicationUser` under the Cursor user data dir):

| Field | Role |
| --- | --- |
| `useOpenAIKey` | BYOK master switch for the OpenAI slot |
| `openAIBaseUrl` | Custom root; `null` means `https://api.openai.com/v1` |
| `aiSettings.userAddedModels` | Extra model *names* added via **Add Custom Model** |
| `aiSettings.modelOverrideEnabled` / `modelOverrideDisabled` | Which catalog rows are on in the picker |

The OpenAI secret itself is not that JSON blob; it is written through `storeOpenAIKey`.

**Verify** (`tryChallenge`) is a **local** `fetch`:

- `POST ${openAIBaseUrl}/chat/completions`
- `Authorization: Bearer <key>`
- non-streaming, `max_tokens: 10`
- model chosen from enabled picker names (`gpt-4o-mini` / substring `gpt` / `o1` / `o3` for generic hosts; `grok-4` if hostname is `api.x.ai`)

So a CodexHub Gateway at `http://127.0.0.1:<port>/v1` **can** pass Verify, because the IDE process dials that URL. The Gateway already serves `/v1/chat/completions`.

**Add Custom Model** only appends a string to `userAddedModels`. It is not a second provider. Every custom name still rides the single OpenAI key + `openAIBaseUrl`.

### What does *not* follow from that UI

1. **This is not a coexistence provider map.** One OpenAI slot. Injecting CodexHub displaces whatever OpenAI key / base URL the user already had. Cursor-native models (Grok, Composer, Auto) stay in the picker; that is coexistence at the *model catalog* layer, not ADR-0004's extra `codexhub` provider entry.

2. **Agent turns do not use that local `fetch`.** `AgentClientService.run` is:

   - `vl.localMode === true` → `runLocalAgentInExtensionHost` (local agent-exec, `baseUrl` + `apiKey` on the machine, `openai-compatible` `/chat/completions` or `anthropic-messages`)
   - otherwise → `this.client.run` (Cursor backend)

   This consumer build compiles `localMode: false` in `buildFlags.js`. Local Private Inference / `CURSOR_LOCAL_AGENT_BASE_URL` is a different SKU (the workbench copy even cites Tesla `inference.tesla.com`).

3. **Help still says BYOK keys leave the device:** every request is "routed through Cursor's servers for final prompt building." Combined with `this.client.run`, Agent is expected to present `{apiKey, baseUrl: openaiApiBaseUrl}` as `apiKeyCredentials` to Cursor's cloud, which then calls that base URL **from Cursor infrastructure**. `127.0.0.1` on the user's laptop is not `127.0.0.1` on Cursor's servers.

4. **No documented file CodexHub can edit while Cursor is running.** The live store is `state.vscdb` (plus a secret). Rewriting it under a running IDE is not the OpenCode `opencode.json` seam.

### Fit against ADR-0004 (revised)

| Rule | Custom base URL path |
| --- | --- |
| Inject one block, keep foreign providers | Partial: Cursor models remain. The OpenAI BYOK slot is a single overwrite. |
| Never activate | Setting `useOpenAIKey=true` is activation of that slot. Picker default (`aiSettings.modelConfig`) can be left alone. |
| Surgical credential | Possible in principle (backup previous key + `openAIBaseUrl`), but the store is SQLite + secret API, not a sibling credential file. |
| Loopback Gateway traffic | **Verify: yes. Agent: not supported by the consumer build.** |
| Block-fingerprint readback | Fingerprint `openAIBaseUrl` + `useOpenAIKey` + `userAddedModels`, never the whole DB. |
| Isolated E2E | Need Cursor closed or a throwaway `--user-data-dir`; not the current isolated-root layouts. |

### Recommended operation (revised)

Do **not** treat this as a green light for a Connect adapter.

Do this instead:

1. **Sanctioned smoke (required before any apply code).** With Gateway running, set Override OpenAI Base URL to `http://127.0.0.1:<port>/v1`, paste the Gateway client key, add one enabled catalog id as a custom model, click Verify, then run one Agent turn. Record whether Agent hits the Gateway or dies in Cursor's cloud. Until that capture exists, do not claim Agent support.

2. **Ship copy-only field mapping** on the Gateway page (or a Cursor card that does not Apply):

   - Base URL: `http://127.0.0.1:<port>/v1` (must include `/v1`; Cursor appends `/chat/completions`)
   - API key: Gateway client key
   - Model: an enabled Gateway id (prefer a `gpt*` id so Verify's heuristic matches)
   - Disclosure: Verify is local; Agent is expected to call the URL from Cursor's servers unless a future localMode/Private Inference build is in use.

3. **Do not auto-write `state.vscdb` in 0.1.9.** Even if the smoke shows Agent somehow local, the store is the wrong shape for Provider Injection. Revisit only with a documented export/import or a stopped-IDE file protocol.

4. **Do not recommend tunnels** as the official client path. A public URL would make cloud-mediated BYOK work, but that is a different product (expose Gateway to the internet) and is out of CodexHub's local-Gateway contract.

5. Admission bar from the parent note still stands, with one extra clause: **Agent (not only Verify) must originate from the local process.** The Override OpenAI Base URL UI alone does not satisfy that.

### Implementation sketch if the smoke proves local Agent

Still not authorized. If Agent is local:

- Adapter owns three fields only: `useOpenAIKey`, `openAIBaseUrl`, `aiSettings.userAddedModels` (union CodexHub ids; never delete user names).
- Backup previous OpenAI key + base URL as the rollback baseline.
- Never write `aiSettings.modelConfig` (activation stays user-owned).
- Restart disclosure: reload Cursor window.
- Fail closed if Cursor is running and the DB is locked.

If the smoke shows Agent staying on Cursor cloud, keep copy-only and close the adapter issue as blocked on Cursor.

## Addendum 2: public HTTPS ingress for Cursor BYOK (noirbright.top vs Cloudflare)

Date: 2026-09-09. Trigger: loopback cannot be the Agent URL; the operator can publish an owned hostname.

This is **operator ingress**, not a CodexHub product default. Do not bake `noirbright.top` into the app. Gateway bind stays `127.0.0.1` (settings already reject anything else). Cursor's Override OpenAI Base URL should be `https://<host>/v1`.

Shared constraints for either path:

- Terminate TLS on the public name. Cursor's cloud is the HTTP client.
- Do not put HTTP basic-auth in front of `/v1`. Cursor only sends `Authorization: Bearer <gateway_client_key>`.
- Disable reverse-proxy response buffering (`flush_interval -1` / equivalent). Agent streams SSE.
- Long timeouts (minutes). Agent turns are not a 30s page load.
- Do not enable Cloudflare Access / VPS IP allowlists unless Cursor egress IPs are known. They are not.
- Do not reuse `ai.noirbright.top`: that Caddy site reverse-proxies `/v1/*` to **127.0.0.1:4000**, not CodexHub `:9099`.
- Quick Tunnel (`trycloudflare.com`) is rejected: Cloudflare documents it as testing-only and **does not support SSE**.

### Way A — owned domain on the Aliyun VPS (`*.noirbright.top`)

Pattern already used for DSH mobile: Host keeps an outbound reverse tunnel; VPS terminates TLS; origin stays loopback.

1. DNS: `codexhub.noirbright.top` A → `120.26.124.92` (apex today). Do not point the apex itself at Gateway.
2. Laptop: CodexHub Gateway remains `127.0.0.1:9099`.
3. Laptop: dedicated `frpc` (or the existing restricted `ssh -R`) to a VPS loopback port, e.g. `127.0.0.1:19099`.
4. VPS Caddy site `codexhub.noirbright.top`:

   - `reverse_proxy 127.0.0.1:19099` with `flush_interval -1`
   - no `basic_auth` on `/v1*`
   - automatic HTTPS

5. Cursor: Base URL `https://codexhub.noirbright.top/v1`, API key = Gateway client key, custom model = an enabled Gateway id.

Pros: ICP domain, domestic origin, existing Caddy/frpc muscle, no Cloudflare SSE/Quick-Tunnel footgun. Cons: VPS is another hop; must not collide with `ai.noirbright.top:4000`.

### Way B — named Cloudflare Tunnel

`cloudflared` 2026.8.2 is already on this host. Use a **named** tunnel only.

1. `cloudflared tunnel create codexhub-gateway` (stable UUID).
2. Ingress: `http://127.0.0.1:9099` (Gateway still loopback).
3. Public hostname, pick one:
   - CNAME `codexhub.noirbright.top` → `<uuid>.cfargotunnel.com` (HiChina can CNAME a subdomain without moving the whole zone), or
   - a hostname on a zone that is already in Cloudflare.
4. Do not turn on Cloudflare Access, Bot Fight, or HTML buffering on that hostname.
5. Cursor: `https://codexhub.noirbright.top/v1` (or the CF hostname) + the same Gateway client key.

Pros: no VPS port-forward recipe; TLS at the edge; laptop only makes outbound connections. Cons: mainland path to Cloudflare has already been a DSH reliability problem; named tunnel needs a Cloudflare account; Quick Tunnel is not a substitute.

### Operator pick

For this maintainer, **Way A is the default** (same reason as dsh-mobile ADR-0005: owned domestic VPS + stable ICP name). Way B is the fallback when the VPS hop is down or when testing from a network that already reaches Cloudflare well.

Neither way is a CodexHub Connect adapter. After ingress is up, the smoke is still: Verify then one Agent turn against that `https://…/v1`, watching Gateway telemetry. Only then consider copy-only Cursor fields that use the public URL instead of `127.0.0.1`.
