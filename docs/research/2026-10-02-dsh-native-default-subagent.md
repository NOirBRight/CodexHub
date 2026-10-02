# DSH native Default subagent capability

Date: 2026-10-02. Execution ticket: [#622](https://github.com/NOirBRight/CodexHub/issues/622).

## Version-scoped finding

The repository-qualified DSH 0.1.0-rc.6 supports independent child provider/model
overrides in its **Headless** profile. The shipped spawn and fork tool rows accept
`config.agentOptions.provider/model`, forwarding them to the native subagent
service and the child agent driver. This is a composition-local capability, not
a global settings namespace. Writing `settings.yaml.tool-subagent.agentOptions`
would be ignored.

The Headless profile mounts `tool-subagent` and `tool-subagent-fork` in the host
composition. User overrides belong in `$DSH_HOME/profiles/headless/cordis.patch.yml`
(`~/.dsh` by default). Cordis replaces a patched row's entire config; therefore
the implementation preserves the client's composed tool config and changes only
the child provider/model pair. It preserves maxTokens, persona, filters, tool
names, spawn/fork backend selection, custom rows, and other row properties.
Original user patch text remains intact; only the two tool rows are overlaid in
a marked block. Reset removes provider/model from the effective child options
and preserves the remaining options.

The **Web** bundle disables these host delegation rows. Web uses `agent-presets`
and delegation tools composed inside the shipped standard/code/cordis presets.
The preset plugin exposes default preset selection and roots, without an
independent child-model override setting. Shipped roots take precedence over
same-name user presets. Applying a Web child override would require editing
shipped read-only inputs or authoring and selecting another preset, changing the
main preset behavior. Neither is done here. Web does not have an equivalent
standalone native Default subagent setting in the inspected version.

The same composition distinction was verified against installed official rc.7.
This finding does not assert that later versions lack additional capabilities.

## Primary sources

Evidence is from the official published packages, not third-party documentation.
No source Git commit was provided by npm metadata, so package versions and archive
SHA-1 values identify the inspected source:

| Official archive | SHA-1 | Relevant files / contract |
| --- | --- | --- |
| [DSH rc.6](https://registry.npmjs.org/@deepseek-ai/dsh/-/dsh-0.1.0-rc.6.tgz) | `de9fbf39056c7f4e658a3e284cb1d66ebc86d040` | `lib/profile-boot-DG5t9aNs.js`: bundle → profile patch → home patch → CLI overlay precedence; `--dump-config` composes without starting an agent. |
| [tool-subagent rc.6](https://registry.npmjs.org/@deepseek-ai/dsh-tool-subagent/-/dsh-tool-subagent-0.1.0-rc.6.tgz) | `1e752e43c5ca2856e2b5778cfa8dce8acee6945a` | `lib/index.js`: child options schema and forwarding into native subagent start. Only provider/model/maxTokens are child options; no child effort or Fast key. |
| [base rc.6](https://registry.npmjs.org/@deepseek-ai/dsh-base/-/dsh-base-0.1.0-rc.6.tgz) | `19c4078abad8ede970c64d08416988081fbaf959` | `cordis.patch.yml`: host spawn/fork tool configs. |
| [Headless rc.6](https://registry.npmjs.org/@deepseek-ai/dsh-headless/-/dsh-headless-0.1.0-rc.6.tgz) | `56e8daeebd62611100a91d9adcf4d1d949e20b75` | `cordis.patch.yml`: enabled host delegation composition. |
| [Web rc.6](https://registry.npmjs.org/@deepseek-ai/dsh-web-app/-/dsh-web-app-0.1.0-rc.6.tgz) | `08c41a0743fe60a94e32d95e2fc7dbd545089e5b` | `cordis.patch.yml`: host tools disabled, preset-based child compositions. |
| [agent-presets rc.6](https://registry.npmjs.org/@deepseek-ai/dsh-agent-presets/-/dsh-agent-presets-0.1.0-rc.6.tgz) | `7e05b72d208b854e9a145b6fb5e97d1b842a6b4e` | `lib/index.js` and shipped preset docs: default/roots settings, shipped presets and their precedence. |

[DSH's official repository](https://github.com/deepseek-ai/deepseek-harness)
contains the corresponding `packages/subagent/tool-subagent`, `packages/preset/agent-presets`,
and client bundle sources. The published rc.6 in-process driver forwards the
request's child options through `resolveChildAgentOptions` to the real child agent.

Native model discovery follows the installed official
[`dsh-llm-pi-ai`](https://www.npmjs.com/package/@deepseek-ai/dsh-llm-pi-ai/v/0.1.0-rc.6)
adapter contract: configured models replace its served catalog; when omitted,
it reads that adapter installation's own `@earendil-works/pi-ai/providers/all`
`getBuiltinModels(provider)` catalog. DeepSeek defaults come from the installed
[`dsh-llm-deepseek`](https://www.npmjs.com/package/@deepseek-ai/dsh-llm-deepseek/v/0.1.0-rc.6)
exported Config schema. The reader uses the same recursive settings overlay over
Cordis base config as `dsh-settings`. It excludes the injected codexhub provider
and extracts only provider/model IDs and display names. It never reads credentials
or makes provider requests. Native effort/Fast are not presented.

## Direct verification and applicability

Official rc.6 CLI and rc.6 DSH dependencies were installed in a disposable fixture
with dependency overrides preventing prerelease range drift. The dependency's own
Pi catalog package was 0.82.1. Official `--profile headless --dump-config` before/
after patching confirmed both native targets; `--profile web --dump-config`
confirmed the host tools stayed disabled and the Web default preset stayed standard.
Main-model, LLM adapter, and credential composition rows stayed unchanged.
Installed official rc.7 passed the same composition-only probe.

The Rust public native save/readback test ran against this exact rc.6 CLI in an
isolated DSH_HOME. It verified original patch text and settings/credentials,
configured provider models, a provider using Pi's built-in catalog, a profile-local
provider, DeepSeek defaults, a direct save while connected, Connect, republish,
Disconnect, and reset. The public API check also passed against installed rc.7. Focused
fixture tests additionally verify native tag/filter/maxTokens preservation and
rollback when effective readback does not match the saved model.

The CLI's configuration dump can initialize the Headless profile and regenerate
its derived `cordis.yml`, following the official profile preparation contract.
CodexHub does not write shipped presets or change the selected profile/main preset.
Higher-priority home patches remain authoritative; if they prevent a save from
applying, the saved profile patch is rolled back and an error Toast is shown.

The UI explicitly limits this setting to Headless and instructs the user to start
a new DSH Headless invocation after saving. Existing invocations are not restarted.
Web presets are unaffected. These checks did not run inference or claim a Web
child-model override, and did not mutate the user's live DSH configuration/install.

Reproduce the official public native check by providing the path to a reviewed
official npm-installed CLI (local `node_modules/.bin/dsh.cmd` works on Windows):

```powershell
$env:CODEXHUB_DSH_NATIVE_TEST_EXECUTABLE = '<official DSH CLI path>'
cargo test --locked dsh_native -- --include-ignored --test-threads=1
```

Run from `src-tauri`. The opt-in test always supplies a disposable DSH_HOME and
restores process environment bindings afterward. Ordinary tests do not require
an installed client. Orchestrator owns integrated full-suite validation.
