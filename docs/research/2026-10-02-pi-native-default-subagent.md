# Pi native Default subagent capability

Date: 2026-10-02. Execution ticket: [#621](https://github.com/NOirBRight/CodexHub/issues/621).

## Finding

Pi 0.80.6 does not expose a built-in Default subagent model setting. Its own
documentation places delegation outside the core, implemented through extensions,
packages, or separately launched Pi processes. Therefore CodexHub cannot apply
a native child-model pin for this version without adding or depending on an
extension, which this execution ticket excludes.

Primary sources, pinned to the official `v0.80.6` commit
`2b3fda9921b5590f285165287bd442a25817f17b`:

- [Official usage documentation](https://github.com/earendil-works/pi/blob/2b3fda9921b5590f285165287bd442a25817f17b/packages/coding-agent/docs/usage.md):
  the design section distinguishes core features from extension-owned workflows
  and explicitly excludes built-in subagents.
- [Official settings contract](https://github.com/earendil-works/pi/blob/2b3fda9921b5590f285165287bd442a25817f17b/packages/coding-agent/src/core/settings-manager.ts):
  `defaultProvider`, `defaultModel`, and `defaultThinkingLevel` select the main
  agent. There is no separate native child-model setting.
- [Official built-in tools](https://github.com/earendil-works/pi/blob/2b3fda9921b5590f285165287bd442a25817f17b/packages/coding-agent/src/core/tools/index.ts):
  the core tool inventory contains file and shell operations, rather than a
  built-in delegation tool with a native default-model contract.

## Direct verification

The installed official package `@earendil-works/pi-coding-agent` reported
`0.80.6`. Running its CLI with `--no-extensions --no-skills
--no-prompt-templates --no-themes --version` and then `--help` succeeded with
exit code 0. The probe bound `PI_CODING_AGENT_DIR` to a disposable directory,
made no inference request, and did not write real client configuration.
The help advertised main-agent model/thinking options and no independent native
subagent model option. Installed usage documentation and the generated settings
type corroborated the pinned upstream sources above.

| Installed evidence | SHA-256 |
|---|---|
| `docs/usage.md` | `9ebe057b1a3dc48ac4bb0d1fd214e5b16bd9bc4e5762ab264a81edc1b231fd8e` |
| `dist/core/settings-manager.d.ts` | `ac1b863d9755a261849f34d28c8daa9e97e0c0f9bf2169408fc0844dd68fb7d4` |

## Scope and outcome

Pi's existing connection and model configuration remain intact. No deferred-only
picker, invented configuration field, scheduler, or mandatory extension is added.
This establishes the unsupported-native outcome for the verified 0.80.6 core;
it makes no claim about every third-party extension or a future Pi version.
