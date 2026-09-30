# Claude and Cursor subscription providers: planning interview

Date: 2026-09-29.
Status: Shared plan confirmed; the user subsequently authorized live model qualification. Isolated probes now demonstrate Cursor Code Mode, both official/Cursor V2 directions, and completed-history replay after backend restart, with explicit failures and limits retained below. Claude generation is blocked by the current organization's subscription-access policy. This is feasibility evidence, not production implementation, complete release qualification, or a vendor-authorization claim.

## Baseline and scope

- Local planning checkout: `fix/claude-client-settings`, last observed commit `7185c1a1`. It is not assumed to match GitHub main. Preserve this checkout; do not switch branches or incorporate unrelated changes to plan the feature.
- The target is two upstream Providers, not two downstream Client configuration entries. `CONTEXT.md` records Claude Subscription Provider and Cursor Subscription Provider separately from their clients.
- Existing issue/spec tracker: GitHub Issues, per `../agents/issue-tracker.md`. Do not publish build tickets until the interview has a confirmed shared plan.

## Confirmed round 1

User response: `A A Claude B Cursor A`.

1. **Implementation direction — A:** use the Magpie-style paths: local official Claude Code CLI with caller tools bridged through MCP; Cursor CLI-authenticated internal Agent service. Cursor ACP is not the selected route. These are accepted engineering directions, not proof of availability or vendor authorization.
2. **Client scope — A:** both Providers join the existing Gateway Provider/model catalog and export through existing Gateway client/protocol surfaces. Do not build a Codex-only private route or a separate editable model catalog. Availability and advanced capability parity are separate claims.
3. **Claude acceptance — B:** complete ordinary text, streaming, multi-turn, tool round trips and cancellation; allow disclosed best-effort advanced adaptations where essential semantics remain safe. Unrepresentable essential behavior must fail visibly rather than silently dropping tools/content or changing the selected Provider/model.
4. **Cursor acceptance — A:** Code Mode, Collaboration V2, and history continuation are hard delivery gates in addition to ordinary Agent behavior. A chat-only or ordinary-tools-only backend does not satisfy this choice. Round 2 and the user's subsequent correction govern mixed-provider handoffs; round 3 below settles model coverage and restart scope. No silent weakening if feasibility fails.

## Confirmed round 2

User response: `OK` to recommended choices `4B, 5A, 6A`.

5. **Cursor mixed-provider handoffs — 4B, amended by subsequent user correction:** mixed-provider task execution remains a hard V2 gate. The user rejected treating unreadable sealed tasks plus explicit errors as acceptable completed compatibility: CodexHub's goal is to make tasks run through practical adaptations. Preventing opaque handoffs and recovering existing affected tasks are compatibility work to investigate, not exclusions justified by an error message. Do not fabricate missing task contents, silently substitute another Provider/model, or count placeholder-only continuation as a successful handoff. Other Providers' documented limitations must still be stated accurately.
6. **Claude prompt-priority adaptation — 5A:** permit the CLI-backed route to carry calling-agent system instructions in user context instead of claiming native system-priority equivalence. Disclose this exact difference before enabling the Provider and obtain the user's explicit confirmation. This is narrow consent to that adaptation, not authority to silently drop instructions or misrepresent the route as a native model API.
7. **Account scope — 6A:** reuse each official CLI's currently signed-in account. Official CLIs own the login experience; CodexHub detects and uses that local account. Do not add an account pool, separate account-import management, or automatic account rotation. Credential-refresh ownership and account changes during an active turn must be handled without interfering with the user's CLI session.

## Confirmed round 3

User response: `OK` to recommended choices `7A, 8A, 9A`.

8. **Cursor model coverage — 7A:** all models discovered for the current account enter the existing catalog, with existing enable/export controls and shared compatibility adaptation. Do not introduce a separately maintained qualification whitelist. Correct task, tool and history transport is the software acceptance criterion; individual model reasoning/task success is not guaranteed. State known capability differences accurately.
9. **Restart scope — 8A:** completed turns can continue from full caller history after a Gateway restart. Durable restoration of an interrupted process, stream or pending tool wait is not required. Recover or reissue work using available results and caller-owned task state without blindly replaying tools that may already have executed. This does not remove the requirement to investigate and support practical sealed-task workarounds.
10. **Independent delivery — 9A:** accept and release each Provider independently. Claude may ship when its own contract passes; Cursor must still satisfy its full Code Mode, Collaboration V2 and history-continuation gates. Independent delivery is sequencing, not permission to omit Cursor or release a reduced Cursor contract as complete.

## Existing decision boundaries

- [ADR-0005](../adr/0005-subscription-credential-seam.md) provides the persisted subscription credential seam but explicitly excludes Anthropic consumer OAuth. The new direction requires a narrow, explicit amendment distinguishing CLI-backed generation from a general OAuth-to-Messages relay; do not silently remove the old boundary.
- [ADR-0014](../adr/0014-claude-code-gateway-client.md) concerns Claude Code as a downstream Gateway Client. It is not authorization for a Claude subscription upstream. New upstream work must not disturb existing client settings or produce a Gateway-to-CLI-to-Gateway routing loop.
- [Earlier Cursor research](2026-09-09-cursor-managed-client.md#d-rejected-cursor-subscription-as-a-gateway-upstream) explicitly recommended against maintaining an internal `AgentService/Run` subscription upstream (also see its section “If the ask is expose Cursor subscription models to OpenCode/Codex”). Round 1's explicit choice reopens that product boundary. The final specification must record what replaces that recommendation for upstream Providers only, without treating Cursor as a supported downstream Client or asserting vendor policy approval.
- User acceptance of the approach is not a legal determination or a promise of exemption from vendor restrictions.

## Source evidence informing the interview

- Magpie reference revision: [`9e9c2ce96c2e3f21f82e539c248c4cf7e429de83`](https://github.com/yetone/magpie/commit/9e9c2ce96c2e3f21f82e539c248c4cf7e429de83).
- [Claude CLI startup](https://github.com/yetone/magpie/blob/9e9c2ce96c2e3f21f82e539c248c4cf7e429de83/internal/gateway/claude_subscription.go#L230-L313) and [prompt rendering](https://github.com/yetone/magpie/blob/9e9c2ce96c2e3f21f82e539c248c4cf7e429de83/internal/gateway/claude_subscription.go#L798-L824): caller instructions are placed in user content rather than retaining native system-message precedence. Round 2 accepts this difference with explicit pre-enablement disclosure and consent.
- [Cursor internal Run request](https://github.com/yetone/magpie/blob/9e9c2ce96c2e3f21f82e539c248c4cf7e429de83/internal/gateway/cursor.go#L225-L297): a CLI-authenticated internal service, not the documented ACP transport or a stable public model API.
- [Magpie sealed agent handoff guard](https://github.com/yetone/magpie/blob/9e9c2ce96c2e3f21f82e539c248c4cf7e429de83/internal/gateway/codex_backend.go#L77-L95) rejects native OpenAI encrypted subagent content on Magpie-served models. This describes Magpie's limitation, not an accepted CodexHub product outcome. CodexHub already has a proactive plaintext-delivery implementation and historical mixed-provider qualification evidence, detailed below.
- [Anthropic Gateway documentation](https://code.claude.com/docs/en/llm-gateway#subscriptions-and-gateways) documents routing Claude Code requests with subscription credentials. It does not by itself establish blanket approval for exposing that subscription to other agents.

## Local product-seam findings

Read-only source investigation of the local baseline found reusable Provider catalog, subscription status, client projection, and telemetry surfaces, but no implemented Claude CLI or Cursor AgentService upstream backend.

- `src-python/subscription_credential.py` and `xai_auth.py` are the credential/status precedent. The selected current-CLI-login-only scope does not authorize copying xAI's independent device-login/account-management product behavior.
- `src-python/providers_config.py`, `gateway_catalog_runtime.py`, `gateway_exchange.py`, and `gateway_transport.py` are the runtime Provider/catalog/exchange paths. Merely adding a row to `config/providers.toml` cannot implement a CLI process or Cursor's internal streaming service.
- `frontend/src/hooks/useProviderWorkspace.ts`, `frontend/src/lib/providerWorkspace/core.ts`, and `frontend/src/components/providers/XaiLoginCard.tsx` locate the existing Provider enablement/auth UX. Provider enablement must stay separate from downstream Client activation.
- `src-tauri/src/gateway/inject.rs` and [ADR-0011](../adr/0011-display-name-and-client-projection.md) own derived Client Projection and naming. Reuse these rather than adding a second model directory.
- `src-python/provider_registry.py` is an OpenCode configuration discovery helper, not the subscription backend registry; [ADR-0008](../adr/0008-provider-preset-seam.md) distinguishes those responsibilities.
- Existing request/usage views are under `src-tauri/src/gateway/telemetry.rs`, `src-python/proxy_telemetry.py`, and `frontend/src/pages/GatewayPage.tsx`. Unknown upstream usage remains unknown; a subscription does not justify invented API costs or a new quota dashboard.
- Newer GitHub-main [ADR-0015](https://github.com/NOirBRight/CodexHub/blob/main/docs/adr/0015-claude-subscription-coexistence-and-metering.md) concerns Claude Code's own subscription requests passing through Gateway. It must not be confused with the proposed subscription Provider for unrelated clients. Final implementation should reconcile against its actual target revision.

## Local runtime-seam findings and qualification prerequisites

- `SubscriptionCredential` is a bearer-token/header/refresh interface, not a process/session backend. `RouteProtocol`, exchange transport, and relay currently model ordinary Responses, Chat Completions, and Anthropic Messages HTTP exchanges. The new generation paths require a backend integration design; do not grow a credential adapter into a second agent scheduler.
- The existing request-scoped tool-compatibility adapters can represent V2 function namespaces and plaintext agent messages. Existing fixture/CLI evidence does not prove Cursor's internal service preserves Code Mode, grammar/freeform input, stream identity, or restart continuation. Those remain real-service qualification prerequisites.
- **Local lossy fallback, not a complete compatibility strategy:** `src-python/gateway_compat/request.py:751-760` invokes `sanitize_third_party_reasoning_items(... preserve_collaboration_agent_message_encryption=False)` before encoding a non-official tool plan. `src-python/gateway_request.py:297-330` removes encrypted agent-message parts, retaining plaintext or substituting `[Official encrypted agent_message unavailable]` as a developer message. A request continuing does not establish that its assigned task survived. The earlier proposed replacement with unconditional rejection is withdrawn; first reuse preventive plaintext delivery and investigate recovery. Reconcile against the actual implementation target revision before changing existing behavior.
- The same sanitizer removes a third-party `previous_response_id`; full-history continuation and opaque upstream session-ID continuation are not interchangeable. Prove the chosen Cursor continuation contract without assuming that an OpenAI Responses ID can be reused on Cursor.
- A Claude backend child must not inherit downstream Claude Code settings/environment that route it back into CodexHub. Isolate backend routing while preserving access to the official CLI login; verify without modifying the user's normal Claude settings or treating the local Gateway key as a subscription credential.
- Qualification must exercise caller-owned tool execution, cross-request tool-result correlation, streamed partial output, cancellation/process cleanup, expired authentication, current-account changes, and task-preserving mixed-provider handoffs. A local stub or existing Chat fixture is not sufficient evidence for live Claude/Cursor compatibility.

## User correction: work around encryption rather than accept frequent errors

The user explicitly made task completion the priority and requested a workaround investigation. Rejection is not the planned solution or a passing acceptance criterion for mixed-provider collaboration.

### Existing preventive path — reuse before inventing a replacement

- [`collaboration_delivery.py`](../../src-python/gateway_compat/collaboration_delivery.py), lines 25–92, recognizes the full supported V2 declaration, maps it to ordinary namespace `codexhub_plaintext_collaboration`, and removes encryption annotations from `spawn_agent`, `send_message`, and `followup_task` message parameters. It handles both top-level `tools` and `input[].additional_tools`. This changes generation before the task becomes opaque; it is not decryption.
- Lines 95–134 restore `collaboration` call identity and `encrypted_function_args=[]` in response bodies and SSE items. Plaintext call history is mapped back on subsequent parent requests. Unknown schemas and namespace collisions are deliberately left untouched; current-client qualification must cover actual declarations rather than assuming every version matches.
- [Earlier research](2026-09-08-portable-collaboration-v2.md) records that merely deleting encryption annotations from the native reserved namespace was rejected by the official service. The ordinary-namespace alias is the established workaround.
- [Checked-in runtime evidence](../evidence/portable-collaboration-v2/runtime-results.json) records nine successful parent/child paths on Codex CLI 0.153.4, including official Astra to Grok, DeepSeek, GLM and Muse, exercising spawn, wait, followup and wait, with two plaintext and zero encrypted child handoffs per path. These are historical results, not new live-service verification and not Claude/Cursor qualification.
- This investigation ran the current local production request/response adapters through the canonical Python launcher, without network/model calls or persistent files. Both declaration placements produced the ordinary namespace; task arguments survived body/SSE decoding and parent-history replay; a synthetic plaintext child task reached the third-party Responses request with its exact task text, author and recipient in the existing `__codexhub_agent_message_v2__` envelope. Exit code 0. The upstream response and child dispatch were synthetic; this proves local adaptation, not current Codex execution or subscription-service compatibility.
- Installed runtimes currently report CLI 0.158.0 and app-bundled CLI 0.158.0-alpha.2.1. Historical 0.153.4 evidence must not be presented as qualification of those runtimes.

### Selected subscription transports — preserve the portable representation

- Magpie's [Claude MCP helper](https://github.com/yetone/magpie/blob/9e9c2ce96c2e3f21f82e539c248c4cf7e429de83/internal/claudebridge/mcp.go#L20-L145) carries ordinary JSON arguments with a tool-use ID; [the waiting bridge](https://github.com/yetone/magpie/blob/9e9c2ce96c2e3f21f82e539c248c4cf7e429de83/internal/gateway/claude_subscription.go#L871-L962) matches returned results by that ID. [Cursor's dynamic-tool transport](https://github.com/yetone/magpie/blob/9e9c2ce96c2e3f21f82e539c248c4cf7e429de83/internal/gateway/cursor.go#L734-L934) also carries argument objects and tool-call IDs. These are source-supported carriers for readable task messages, not proof that the proposed CodexHub backends already work.
- Do not copy Magpie's entire history normalization: its [Responses parser](https://github.com/yetone/magpie/blob/9e9c2ce96c2e3f21f82e539c248c4cf7e429de83/internal/gateway/responses.go#L83-L154) turns `agent_message` into ordinary user content without preserving directed author/recipient fields. Reuse CodexHub's existing readable envelope and inverse mappings so subscription transport adaptation does not discard that context.
- Claude renders previous calls/results into text; Cursor reconstructs history and re-encodes protobuf values. Qualification must cover exact task strings, routing context, call/result identity and followups across those transformations, including fresh-process history replay. Neither transport supplies post-hoc decryption or automatic task reconstruction.

### Existing sealed-task recovery — candidate policy, not implemented recovery

- No historical decryption/reconstruction path was found. [`verify_rollout_recovery_copy.py`](../../scripts/verify_rollout_recovery_copy.py), lines 32–69 and 90–97, quarantines a specifically identified damaged JSONL line in a copy; it does not recover encrypted task text. [`history_overlay.py`](../../src-python/history_overlay.py), lines 226–237 and 798–814, handles rollout location and Provider metadata, not plaintext reconstruction.
- First distinguish an incidental encrypted history item from the only current task instruction. Do not reject an otherwise usable request merely because some unrelated old ciphertext exists, and do not silently treat a missing current task as recovered.
- If the matching parent call or original instruction retains the complete readable task, use that source and its call/target association. This is conditional: no personal rollouts were inspected and native encrypted calls need not contain a plaintext copy.
- If exact recovery is unavailable, a caller-owned parent may deliberately restate/reissue the task through the proactive plaintext path. That is a fresh assignment based on available original requirements, not decryption or a claim of exact historical reconstruction. The [existing V2 lifecycle](../adr/0002-runtime-derived-tool-compatibility.md) provides caller-owned list/wait/message/followup/spawn operations; the Gateway must not invent calls or become another agent scheduler.
- Before retry/reissue, account for child state and effects already performed; do not blindly replay tool side effects or promise exactly-once execution. A fresh task can avoid importing irrelevant old turns, but `fork_turns="none"` alone does not prevent message encryption. Recovery acceptance should observe the actual readable assignment, selected child model, results and followup, not merely an HTTP success or lack of exceptions.

### Remaining blocker investigation

- Prefer the existing source-generation workaround for new mixed-provider work. New Claude/Cursor backends must carry its readable tasks, tool results and routing identities through their own transports.
- Already-created sealed tasks are a separate recovery problem. Investigate authoritative plaintext availability and caller-owned task recreation; do not claim that ciphertext, unrelated surrounding history or a placeholder reconstructs the original instruction.
- A recoverable old task must not cause blind re-execution of tools that may already have side effects. The user's correction did not itself authorize live calls; the subsequent explicit authorization below permitted isolated qualification, not production deployment.

## Shared-plan confirmation and next evidence gate

- The user explicitly confirmed the consolidated scope with `确认`. Product-choice interviewing is complete; retain the recorded choices rather than reopening them during technical qualification.
- This is a multi-session build. Next follow the ask-matt flow: a prototype handoff for runnable uncertainties, feed its findings back into the plan, then write the specification and dependency-linked implementation tickets on the existing tracker.
- The prototype questions are technical, not choices for the user to guess: whether the current Codex declarations use the existing preventive path; whether Claude's MCP and Cursor's AgentService carry readable task envelopes, Code Mode input, caller-owned tool results and followups; and whether full-history continuation works after backend restart. Apply the distinct Claude/Cursor acceptance levels rather than demanding identical parity.
- Keep any prototype isolated from the user's running Gateway, normal CLI settings and existing checkout changes. Live authenticated service qualification consumes subscription resources and must have explicit authorization; no live run is implied by this planning approval.
- Subsequent explicit authorization: the user said `你直接来开展即可，我授权你使用模型验证`. Proceed with isolated live model qualification using current accounts; no further authorization prompt is needed for these bounded probes. Production deployment, account changes and normal client/Gateway configuration changes remain outside this authorization.
- The prototype answers feasibility questions, not the complete feature acceptance matrix. Production qualification must still cover all selected client/protocol surfaces and the repository's supported release platforms. Unresolved feasibility remains a blocker to solve rather than a silent scope reduction or rejection-as-success.
- The executed prototype is retained outside temporary storage at `/home/noirbright/Workstation/CodexHub-subscription-prototype-W4H0kq`, branch `prototype/subscription-provider-probes-W4H0kq`. The original handoff is retained under `prototype/subscription/HANDOFF.md` there. The baseline and preexisting-user runtime overlays are recorded in the evidence; those overlays are not prototype-authored product changes.

## Implementation status

No production Provider implementation, deployment, login/account switch, or normal client/Gateway configuration change was made. The authorized follow-on work used isolated runnable prototypes, disposable fixture histories and copied current-account credentials. Prototype services were stopped; copied credentials and raw probe logs were removed. Sanitized findings and evidence were added to this planning checkout.

## Authorized live prototype findings

The [machine-readable outcome and evidence index](../evidence/subscription-provider-probe-2026-09-29-W4H0kq/summary.json) distinguishes passes, retained failures, unexercised cases and cleanup. The retained prototype has runnable sources, pinned Acorn dependency, reproduction instructions and intermediate failure artifacts. Baseline: `7185c1a1499e455300fe79bf226671491f0114b8` plus six explicitly hashed preexisting-user runtime overlays; CLI 0.158.0, Cursor Agent `2026.09.28-64d2043`, installed Magpie v0.1.393. No private user history was used.

### Cursor: positive live paths, not a blanket parity claim

- Real current-Codex Code Mode executed caller-owned `exec_command` against a random file fixture through the Cursor AgentService bridge and a reversible freeform-to-function adapter. The caller received the actual tool result and returned the unpredictable value. This is stronger than an ordinary function merely named `exec`.
- Official `gpt-6-astra` parent → Cursor child and Cursor parent → official child both completed spawn, wait, same-child followup and a second read/result. Separate child rollouts verify the selected client model IDs, two real read calls, both returned values, and no parent-side fixture read. Successful artifacts are `official-to-cursor.json`, `cursor-to-official.json`, and their lifecycle companions in the evidence directory.
- A completed Code Mode turn survived actual replacement of both the reference backend process and CodexHub, followed by a fresh Codex process resuming full history. The replay returned the reversed fixture value without another tool call. `cursor-backend-restart.json` records distinct backend PIDs and successful cleanup. This does not promise durable in-flight agents.
- The selected route was `cursor/gpt-5.6-luna`, effort `high`. The current-account catalog contains `gpt-5.6-luna-high`; **[INFERENCE]** Magpie's source maps the route/effort pair to that ID. No protobuf wire-model-ID capture was enabled. Do not convert the route observation into a stronger exact-wire-model claim.
- Discovery found 246 raw model IDs and 68 grouped families. Magpie exports only 24 families by default, leaving 44 available families unexposed. The product must not copy that cap or turn this one-family generation probe into an artificial whitelist.
- Caller cancellation closed a real stream after its first delta and before completion; the bridge remained healthy. Upstream cancellation acknowledgement and billing cessation were not observed.

### Current-client workaround and recovery evidence

- Current Code Mode can embed collaboration handlers inside the custom `exec` description rather than expose the separate namespace recognized by the existing preventive adapter. Its developer guidance can simultaneously prohibit nested collaboration calls. A live model refused to spawn under that mismatch; a different live call emitted readable task JavaScript but the client placed its literal message in `agent_message.encrypted_content`.
- The prototype exposes only collaboration handlers actually declared by that caller, including flat custom declarations and child subsets. It transports those as ordinary functions and restores native collaboration identity plus `encrypted_function_args=[]` toward the caller. [Upstream source](https://github.com/openai/codex/blob/c026e7a622ecae45f0af1939729e7baab9d1637b/codex-rs/core/src/tools/handlers/multi_agents_v2.rs) distinguishes `DirectPlaintextMessage` from the encrypted-source path; installed-client behavior was observed independently.
- The custom-call carrier preserves JavaScript as `{input: string}` and reverses the SSE/history representation. Converting a history item from custom to ordinary function also requires a valid ID prefix for its new type; one reverse-direction failure exposed that omission. `call_id` remains stable across calls/results.
- Live exact-source recovery repaired four message parts across requests from two observed child-message call literals. The parser does not execute JavaScript. It requires exact message, author and recipient provenance; it does not infer plaintext from arbitrary ciphertext. [Source excerpts and correlations](../evidence/subscription-provider-probe-2026-09-29-W4H0kq/live-recovery.json) are retained. Offline boundary checks left unrelated opaque content and wrong-author matches untouched.
- The source index is process-local; dynamic JavaScript expressions and genuinely opaque-only old tasks are not proven recoverable. Caller-owned restatement/reissue and side-effect-aware recovery remain product work, not claims established by this prototype.

### Failures retained rather than reclassified as success

- A later V2 restart case had correct child results, but the Cursor parent inserted two U+200B characters in the final reversed value; resumed history repeated that value. Both strict final-output checks failed. The child execution evidence passed, but the complete case remains failed in `cursor-v2-restart-exact-output-failure.json`. The successful completed Code Mode restart case above is separate evidence, not a substitute result for this failed case.
- One earlier Cursor custom-call envelope did not satisfy `{input: string}`. Its actual argument body was not captured; later successful runs do not establish the cause or a universal conformance fix.
- All selected Gateway clients/protocols/platforms, every discovered model, expired-auth/account-change behavior and opaque-only recovery remain outside completed qualification. Do not label the product shippable from this prototype.

### Claude: exact current-account prerequisite

- The isolated reference bridge used the genuine Claude Code CLI with first-party Claude.ai OAuth. Login status was present, token expiry was about 17,856 seconds away at inspection, and `user:inference` was present. Sixteen model IDs were listed; listing is not entitlement proof.
- Explicit `claude/claude-sonnet-5-5` and `claude/claude-sonnet-4-5` generation requests returned local Gateway HTTP 502 with the same upstream message: **“Your organization has disabled Claude subscription access for Claude Code · Use an Anthropic API key instead, or ask your admin to enable access”.** Magpie recorded three internal attempts per rejected request; a distinct upstream HTTP status was not captured.
- A direct official-CLI control also failed, with zero reported input/output tokens. No API-key substitution, account switch or further generation retry was used to bypass the policy. Ordinary generation, tools, replay, cancellation and Claude mixed-provider behavior therefore remain unqualified, not passing.
- The actionable prerequisite is enabling Claude Code subscription access for the current organization/account. This is an account-policy change, outside probe authorization; changing transport code cannot establish that entitlement. After it is enabled, rerun the prepared Claude qualification rather than infer success from the Cursor path.

### Input to specification and implementation tickets

Keep both selected transports and the user's run-through requirement. Reuse/deepen the existing portability and directed-message layers for current Code Mode declarations, typed history identity and authoritative-source recovery; do not add a Gateway agent scheduler. Preserve full account model discovery rather than Magpie's default export cap. Track current-client compatibility, model-output conformance, Claude's account prerequisite and the remaining protocol/platform acceptance matrix separately. Independent release ordering remains allowed; neither a Claude policy denial nor a Cursor chat-only result lowers the confirmed acceptance contract.
