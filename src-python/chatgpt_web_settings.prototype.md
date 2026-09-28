# ChatGPT coding setup — throwaway UI prototype

Question: which layout best explains the next action and the handoff between
Runtime Settings, the daily browser, OpenAI Platform, and CodexHub?

Product decision: [ADR-0019](../docs/adr/0019-chatgpt-coding-setup-wizard.md).
Coding capability is the only completion target. Saving returns immediately;
manual restart and status checking are separate steps. The user finalized layout A and its refined visual design on 2026-09-28.
The approved visual reference is commit `1c1b4a98`; the prototype is not production
implementation. B/C are retained only as historical alternatives.

From the repository root:

```sh
npm run prototype:chatgpt-setup --prefix frontend
```

Open http://127.0.0.1:43127/?variant=A (or B / C):

- A, 单步聚焦: one task at a time, compact progress navigation.
- B, 全程清单: an expanded checklist row, with the entire journey visible.
- C, 双侧协作: current task alongside a mock of the external destination.

The bottom switcher preserves in-memory progress. Arrow keys switch variants
except in form controls or dialogs. The “场景 / 状态” drawer offers fresh login,
existing account, pending restart, blocked permissions, failed verification,
and expired login scenarios; it also displays the complete simulated state.
Reloading resets everything. Fields are read-only dummy values. All external
handoffs, account connections, credentials, restarts, and probes are simulated.
No real authentication, account configuration, or production services are used.
The Node stdlib server binds to loopback and refuses production mode.

Manual inspection in the T3 collaborative browser (2026-09-28):

- Inspected A/B/C at desktop width and 390px; no horizontal page overflow.
- Existing account starts at tools; fresh account demonstrates extension then
  account connection without conflating it with connector authorization.
- Save leaves runtime inactive; recheck cannot pass until simulated manual restart.
- Switching layouts retains state. Blocked authorization and failed tool
  verification remain incomplete. The simulated complete path reaches the handoff.
- Reset cancels a pending simulated probe so an old result cannot finish a new scenario.

Verification class: fast, docs plus isolated throwaway UI. Browser walkthrough
and diff hygiene only; no production test suite or real-client acceptance claim.
Preserve this source on `prototype/chatgpt-coding-wizard`, outside main. Track
feedback on #592; #567 remains responsible for the real integration acceptance.

Selected A, visual iteration 2: light studio canvas, existing CodexHub/ChatGPT
SVG assets, connected step navigation, a focused service sheet, compact credential
preparation actions, and explicit save/next-step footer. Original B/C remain for
historical comparison. Desktop and 390px browser inspection found no horizontal
overflow. The revised save action still leaves runtime inactive and restart manual.

Verdict: A won. The user accepted its one-task-at-a-time structure and then
approved the refined visual treatment without further changes. Use this exact
revision as the implementation reference; remove prototype-only controls and
replace mock actions with verified production behavior during implementation.
