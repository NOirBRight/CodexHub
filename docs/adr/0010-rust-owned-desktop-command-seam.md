# ADR-0010: Rust-owned Desktop Command seam with thin transport adapters

Date: 2026-08-31
Status: Accepted for campaign DM-3 (#491, #494)

## Context

The desktop command interface was duplicated across four shallow sites with no
owning module:

- `frontend/src/lib/commands.ts` — ~79 command names hand-typed as a
  `COMMANDS` map plus a `CommandName` union.
- `frontend/src/lib/tauri.ts` — per-command `api.*` wrappers re-declaring
  every argument shape and result type, plus a Tauri-IPC → Web Bridge fallback.
- `src-tauri/src/main.rs` — `#[tauri::command]` free functions and a
  hand-maintained `tauri::generate_handler![...]` inventory.
- `src-tauri/src/web_bridge.rs` — an 80-arm `dispatch` match re-implementing
  argument parsing, aliases, and error mapping for the HTTP bridge adapter.

Adding one command required editing all four sites and a regex test
(`frontend/scripts/commands.test.mjs`) that only proved the literal lists
contained the same text. Locality was zero; depth was near zero.

Tauri 2 requires a **static** `generate_handler!` literal (permission/allowlist
model); a dynamic `HashMap<String, Box<dyn Fn(Value)>>` dispatcher would bypass
the static allowlist and degrade typing to `dispatch(string, json)`.

## Decision

Rust owns the command interface. TypeScript is a checked client adapter, not a
second source of truth.

1. **One registry module** `src-tauri/src/desktop_commands/` owns the command
   table through a declarative macro: canonical name, Tauri handler path, Web Bridge decoder/handler,
   `frontend_exposed`, `bridge_exposed`, `desktop_only`, and argument alias
   metadata. The registry macro expands to the literal
   `tauri::generate_handler![...]` expression, the typed Web Bridge match, and
   a read-only `command_manifest()`.
2. **Two thin adapters, one seam.** `main.rs` installs `tauri_handlers!()`;
   `web_bridge.rs` handles only the HTTP envelope (origin, body, response
   envelope) and calls `dispatch_web(...)`. Both adapters share the registry;
   neither owns a command list or argument parser.
3. **No codegen dependency, no committed generated frontend files.** The
   registry is plain Rust; no Specta/ts-rs/schemars, no build-script Node, no
   checked-in generated TypeScript. `frontend/src/lib/commands.ts` and
   `tauri.ts` stay hand-typed but are verified against `command_manifest()`
   by contract tests comparing structured JSON (not source regex). Result types
   are verified by TypeScript compilation plus representative Serde round-trip
   fixtures.
4. **Asymmetries are explicit.** `dsh_client_info`-style commands that exist
   in Rust/bridge but not in the frontend are recorded in registry metadata
   rather than expressed by omission.
5. **No third transport, no untyped dispatch.** The two existing transports
   remain; the public dispatch surface stays typed per command.

## Consequences

- Adding an ordinary command is one registry row plus its typed handler;
  Tauri registration, bridge dispatch, and manifest follow automatically.
- `commands.test.mjs` source-regex checks are replaced by contract tests over
  the manifest; static-registration and entry-discipline checks remain.
- Unknown command, missing-arg, alias, desktop-only, AppHandle/Window
  injection, blocking/async dispatch, `CodexHubError` envelope, and
  fallback-to-bridge behavior must be preserved byte-for-byte.
- The Rust phases require a working `cargo` in the verification environment;
  no campaign completion claim without `cargo test --locked -- --test-threads=1`
  and `cargo clippy --locked --all-targets -- -D warnings`.

## Implementation status

The registry macro now emits `CommandMeta`, parsing/name helpers, the static
Tauri handler expression, and the typed Web Bridge match from one row list.
Contract tests invoke a Rust test to write a temporary manifest and compare
structured JSON; no generated manifest is checked into the repository.
