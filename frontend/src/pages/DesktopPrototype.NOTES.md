# 0.2.0 desktop prototype — approved style, full interface mapping

Design verdict: the user approved and locked revision 5 on 2026-09-06. Preserve the compact desktop frame, top navigation, pearl/plum light and dark finish, typography and SVG identity. This remains a throwaway prototype on `codex/prototype-ui-020`; it is a primary source for the future production refactor.

Run from `frontend`: `npm run prototype:ui`.

Current: `/?prototype=overview&theme=light&page=客户端`.
Themes: `light`, `dark`. Pages: 概览, 使用统计, Provider, 客户端, 设置.
Earlier A/B/C explorations: append `legacy=1&variant=A`.

## Question

Can a 1024 × 768 desktop workspace communicate Gateway availability, Codex-to-CodexHub connection, provider resources, and external client connections without sacrificing the existing statistics controls?

## Implemented prototype behavior

- Gateway start, stop, restart with transient busy state and completion feedback.
- Codex connect/disconnect independent of Gateway runtime.
- Five external clients using the repository's icons, accurate configured example paths, independent switches, filters, configuration details and manual connection information.
- Provider brand marks, enabled model counts, resource balances, search and a simulated add flow.
- Production `StackedUsageChartShell` reused unchanged: Token/requests, provider/model/client breakdown, day/week aggregation, week/month/custom date selection, hidden series filtering, hover details, cost and cache input.
- A deterministic rolling 62-day fixture supplies chart events; aggregate costs are explicitly demo estimates.
- Theme changes and page selection reflected in URL. All connection and provider mutations are in memory. No real credentials or service requests.

## Limits

Gateway network settings and complete provider/model editing remain outside this iteration. Overview headline metrics and balances are fixture values. This is browser-hosted desktop layout exploration, not native window integration. Production chart behavior is reused with prototype-scoped appearance overrides; no production chart source changes.

## Verification

TypeScript compilation, diff hygiene, report-only quality gates. Browser checks cover light/dark client layouts, client busy/completion states, independent Codex disconnect, Token/request switching, legend filtering with aggregate recalculation, and the custom calendar. Manual QA continues as the prototype is refined.

## User feedback retained

The first two revisions were rejected for a web dashboard feel, plain visuals, missing direct connection controls, placeholder client layouts, missing brand icons and incomplete statistics. This revision addresses those constraints; no visual winner is assumed.

## Revision 4

- All main tabs, including Settings, moved to the top.
- Overview reminder banner removed; Kimi retains its numeric quota without an alert/advisory on overview.
- Settings rebuilt with General, Codex & clients, Gateway, Request policy, Diagnostics, About. Existing settings keys are preserved by normalizeSettings; draft changes stay in memory across navigation.
- Settings save validated in browser: changing port updates persistent header and connection URLs, with restart-specific feedback.
- Provider ModelSection and OfficialOpenAIUsagePanel reused with local fixtures. Custom provider connection editing, model management, official collaboration controls and provider unsaved-close protection added.
- Model editor layout visually verified in dark mode after isolating production form controls from prototype-wide form styling.
- New original SVG identity: opposing ports joined by a bridge. Full-color and monochrome variants in prototype-assets.
- Complete feature inventory and honest pending/partial distinctions: PrototypeFeatureParity.md. This prototype is not yet a feature-equivalent production rewrite.

## Revision 5 — visual finish only

Existing navigation, settings, interactions and production component reuse are preserved. Added DesktopPrototype.finish.css to refine surface/rim treatment, desktop toolbar selection, connection bridge detail, numerical hierarchy, provider resource rows, client card controls, settings panels and chart colors. Chart area colors and legend swatches are mapped together; date presets remain fully readable. Both light and dark variants inspected in the browser. This pass does not change the feature-parity status documented in PrototypeFeatureParity.md.

## Revision 6 — functional interface coverage

The approved finish remains intact. PrototypeFeatureParity.md maps the original user-visible source controls to the new locations. Complete maintained Provider catalog snapshot (8 providers / 123 models), custom onboarding, unified Provider management, ordering, xAI device authorization, official quota windows, client detection/version/drift/ownership, history conflict/defer, updater and diagnostic lifecycles are represented. Chinese/English rendering, in-memory draft protection, loading/success/error/retry feedback and exact restart notices are included. Titlebar controls simulate minimize/restore, maximize and close-to-tray.

Scenario controls live behind the titlebar prototype control or a disclosure in the relevant detail. They do not add overview warnings. Authentication, client file edits, process operations, installation and persistence remain simulated. Closing the browser resets the prototype; no account or production configuration is changed.

Verification: TypeScript compilation; staged diff hygiene; report-only quality gates (parse_errors: 0, existing non-blocking reports). Browser checks include the full catalog, adding xAI through to authorized quota windows, English settings and return to Chinese, Gateway restart confirmation, failed settings save preserving its draft then successful retry, client version/drift states, and light/dark visual inspection. Existing chart and model-editor behavior are retained by direct component reuse. This is UI coverage evidence, not production runtime acceptance.

No implementation issue was supplied, so the approved design verdict and branch pointer are captured here and in the prototype commit, without posting to an unrelated issue.

## Revision 7 — unified finish and complete viewport access

- Preserve the approved pearl/plum desktop direction in both themes. Shared control height, form fields, focus states, chips, panel spacing and provider/client identities now extend to all parity additions and reused model/usage controls.
- Remove QA scenario selectors from product forms; retain every scenario in the titlebar's Prototype Tools, grouped by service, account/model and maintenance. These remain in-memory fixtures.
- Modal heading and provider save bar remain visible. Each detail has a single scroll owner; provider tab changes begin at the top. Dialog widths are explicitly bounded by viewport width, including narrow desktop panes.
- Browser verification: 1280×720, 840×600 and 640×520; light/dark model forms, catalog onboarding, 46-model list bottom, official usage heatmap, client configuration expansion, all six settings categories, custom statistics calendar, overview and centralized tools. Settings and provider footers remain reachable/visible; no horizontal overflow in the inspected narrow overview or provider detail.
- This is a fast, isolated prototype change. Backend integration and native-window behavior remain outside this browser verification.

## Revision 8 — workspace roles and bounded scrolling

- Overview keeps page context, Codex connection and compact metrics fixed; only remaining-resource rows scroll. Removed the decorative latency sparkline because it had no meaningful scale or data relationship.
- Provider management now owns upstream endpoints, credential configuration, enabled-model counts/previews, ordering and direct model/account/configuration entry points. Quotas and balances remain on Overview.
- Client page keeps connection state and filters fixed while client cards scroll independently. Replaced the misleading additional-client tile with a Connection Parameters utility in the fixed footer. It explains compatible custom-endpoint requirements and per-client format differences; no automatic unknown-client adapter or AI integration is implied. Existing Rust adapters under src-tauri/src/gateway/clients remain unchanged.
- Browser checks at 1125×926, 840×600 and 640×520 cover both themes, resource/client independent scrolling, configuration deep links, parameter copy entry points and narrow layout bounds. No production lifecycle or persistence changes.

## Revision 9 — provider-specific resource details

- User-approved layout: Provider identity / resource details / enable switch. One or two independent resource groups use the available content width; there is no mandatory second metric.
- OpenAI combines 5-hour and weekly windows; xAI/Grok shows a single weekly quota; balance providers show balance and today's spend; Kimi shows remaining/total tokens. Percent, meter and reset text form one group. Model counts and duplicate healthy-balance status are removed from this overview.
- Added an explicitly simulated xAI subscription to the default fixtures. Its detail panel also uses only the weekly quota, starting with a signed-in demo account. Login/logout/error flows remain available. Chart fixtures include all five sample providers.
- Rows are 64px high. Browser checks cover light/dark at 1125×926, 840×600 and 640×520, the single-week detail link, last-row reachability and independent scrolling. Production integrations are unchanged.
