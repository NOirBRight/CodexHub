# Managed browser login live check (AM01S)

Date: 2026-09-29

Context: operator completed sign-in in the isolated managed browser window for home `codexhub-584-HPkFy2`. No passwords, cookies, tokens, or Runtime Keys are recorded.

## Runtime snapshot (sanitized)

- Home: `~/.local/state/codexhub-584-HPkFy2/runtime`
- Component: codex-chatgpt-web 6.1.1 / pin a13cd099...
- Process: running on loopback; admitting turns
- Login: `signed_in`, window `closed`, control `owned-browser-v1`
- Tunnel: `ready`
- Connector: selectable (`Codex Native2 DEV` in active settings)
- Readiness cache: `ready` (capabilities match)
- healthz: `accepting_turns=true` after probe

## Live coding tool probe

After the operator confirmed login, a fresh `chatgpt_web_tool_probe.start_probe` against this home completed:

- state: `passed`
- reason: `null`
- live_attempted: `true`
- probe_id: `0ec3820d3e55ad26`
- checked_at: `2026-09-28T17:02:07Z` (UTC)
- coding_setup_complete: true for the current generation binding

No request bodies, model text, or credentials are retained in this note.

## Path coverage

- Covered now: isolated managed-browser sign-in path + full-mode Tunnel/connector + real tool roundtrip on the existing isolated home.
- Still open for #592: daily Chromium extension install and extension→settings handoff on AM01S; Windows daily-browser acceptance.
- Integration portable candidate `fd2d5862` remains unreleased test media; this live check used the already-running isolated supervisor home rather than relaunching the portable app binary.

## Conclusion

Managed-browser login for this isolated instance is usable for coding tools. Do not treat #592 or the full release gate as closed.
