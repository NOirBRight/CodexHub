# OMP native Default subagent — #619

Contract checked on 2026-10-02 against installed `omp/18.4.10` and its
[native model configuration](https://github.com/can1357/oh-my-pi/blob/main/docs/models.md),
[task-agent discovery](https://github.com/can1357/oh-my-pi/blob/main/docs/task-agent-discovery.md),
and [model selector resolver](https://github.com/can1357/oh-my-pi/blob/main/packages/coding-agent/src/config/model-resolver.ts).

The seven integrated bundled agents use `task.agentModelOverrides`. A native
provider/model selector may carry a trailing thinking suffix. Exact model IDs
take precedence over suffix parsing: tags such as `child:cloud` and literal IDs
ending in `:max` remain model identities. Custom agent overrides and unrelated
task settings stay outside the saved slice. No independent Fast field is added.

Inference-free CLI corroboration used an agent directory inside this isolated
worktree with synthetic provider credentials and no production configuration
writes. `omp models --json --no-extensions --config <isolated-config.yml>`
returned `native/child:cloud` with `thinking: ["low", "high"]` for native
`thinking: {mode: effort, efforts: [low, high]}`, and `native/plain` with
`thinking: null`. The older `thinkingLevelMap` property was ignored by this
client; the implementation therefore uses the actual CLI metadata or declared
native `thinking.efforts`. Discovery is bounded to ten seconds and excludes
injected CodexHub models. Native selection uses distinct picker identities.

The existing isolated managed-client seam verifies direct native save/readback
for all seven agents; reopening; native reset; tagged and literal-suffix model
IDs; block and inline YAML; prioritized agent models; preservation of main
model, authentication/provider files, comments and custom agents; connected
native saves; stale Gateway preferences; repeated Connect/republish; Disconnect
after a Gateway pin; and manual native edits made before Disconnect. Saved
native defaults replace old snapshots during detach without restoring invalid
Gateway selectors. Success uses the existing Toast and asks to restart OMP.

Focused worker checks: 26 matching OMP Rust tests, serially; TypeScript compile;
18 Default subagent frontend contracts; IPC-name contract; three Rust-generated
desktop command-manifest contracts; and `git diff --check`. Rust checks used the
actual canonical pinned runtime resources prepared by the Orchestrator. Full
strict integration suites and final review belong to the Orchestrator. No live
inference request or interactive OMP/browser session was used as evidence.
