# OpenCode native Default subagent — #617

Contract checked on 2026-10-02 against OpenCode upstream commit
`1ddb0873aee50d209d1a8d7f91b89c5daf692d49`:

- [Native agent schema](https://github.com/anomalyco/opencode/blob/1ddb0873aee50d209d1a8d7f91b89c5daf692d49/packages/core/src/v1/config/agent.ts) declares `model` and `variant`. Variant applies when the agent uses its configured model. Native saves therefore write provider/model in model and the declared variant separately; they do not use Gateway selectors or add Fast fields.
- [Models command](https://github.com/anomalyco/opencode/blob/1ddb0873aee50d209d1a8d7f91b89c5daf692d49/packages/opencode/src/cli/cmd/models.ts) lists models from the client's Provider service. `models --verbose` prints provider/model followed by model JSON metadata. Discovery also reads native configured provider models, excludes injected CodexHub selectors, and respects enabled/disabled native providers. The subprocess uses the selected configuration through OpenCode's OPENCODE_CONFIG binding.
- [Native configuration](https://github.com/anomalyco/opencode/blob/1ddb0873aee50d209d1a8d7f91b89c5daf692d49/packages/core/src/v1/config/config.ts) declares provider/model selection and provider allow/deny lists. OpenCode accepts JSONC; saves retain native setting values and string literals when serializing the updated configuration as JSON.

The existing isolated managed-client harness verifies direct native save/readback;
general, explore and scout coverage; preservation of parent models, provider
options, and custom agents; native choice versus stale Gateway preference;
connected native updates; Gateway-pin detach; reset to native defaults; and
manual native edits surviving Connect and Disconnect. The picker regression
checks that native models without declared variants do not gain invented
reasoning levels. Success uses the existing Toast and names OpenCode as the
client to restart.

Focused checks run by this worker (full strict integration suites belong to the
Orchestrator): TypeScript compile; native/OpenCode Rust tests, serially; frontend
Default subagent, IPC-name and Rust-generated command-manifest contracts;
`git diff --check`. No live provider request or interactive OpenCode session was
used as evidence. Initial Rust builds were blocked by omitted packaging resources;
focused compilation first used a process-local Tauri resource override, then
checks were rerun with the Orchestrator's actual pinned runtime archive.
