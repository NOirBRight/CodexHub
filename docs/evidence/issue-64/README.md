# Issue #64 Collaboration V1/V2 inventory

This directory contains a bounded, structural inventory of the two native
Collaboration protocol families that must be distinguished before any Gateway
tool exposure or semantic repair. It is a schema boundary and adapter
requirement record, not a live protocol capture and not a Collaboration
lifecycle implementation.

## Source contract and evidence boundary

The inventory is bound to the accepted Issue #62 source contract:

- Codex CLI `0.146.0`
- source tag `rust-v0.146.0`
- attested source commit `e363b08c9175ac1cbe5893615dd2cb9ddf95043b`
- `capture_status=not_observed`
- source-contract `qualification_status=unqualified`

The source-contract file is retained by name and canonical-LF SHA-256 in the
JSON artifact (`6f38b8b07b98c6f28edd7418b63449242ee41d6396694392a70c0d7fb2b70f2c`).
This is source/repository evidence only: no CLI request, credentials, raw
capture, prompt, call ID, item ID, or token is retained here. A historical
0.144 trace, if encountered elsewhere, must remain labeled 0.144 and must not
be promoted or relabeled as the 0.146 contract.

Regenerate or check the canonical artifact with:

```powershell
python scripts/build_issue_64_collaboration_inventory.py
python scripts/build_issue_64_collaboration_inventory.py --check
```

## Boundary discriminator

Classification happens before exposure and before any semantic repair. Both
`namespace` and child tool `name` are mandatory discriminators:

| Family | Namespace | Tools |
| --- | --- | --- |
| Collaboration V1 | `multi_agent_v1` | `spawn_agent`, `send_input`, `wait_agent`, `close_agent`, `resume_agent` |
| Collaboration V2 | `collaboration` | `spawn_agent`, `send_message`, `followup_task`, `wait_agent`, `interrupt_agent`, `list_agents` |

A flattened alias such as `multi_agent_v1__spawn_agent` is not sufficient to
choose a family without its namespace. Unknown namespaces/tools, missing
discriminators, ambiguous flattened names, and payloads mixing V1 and V2
fields fail closed; they are not guessed or repaired.

## V1 shape (`multi_agent_v1`)

The V1 entry records the current CodexHub compatibility surface from
`src-python/codex_proxy.py#MULTI_AGENT_DISCOVERY_TOOLS`: a `namespace` with
child `function` tools. It is not an official raw CLI capture; the official
CLI 0.146 V1 shape remains `not_observed`. Calls are `function_call` items
carrying `namespace`, `name`, `item_id`, `call_id`, and `arguments`; results are
`function_call_output` items linked by `call_id` and `item_id`.

The compatibility-surface spawn call requires `agent_type` and may carry
`fork_context` or `message`. `send_input` requires `target` and may carry
`message` or `interrupt`; `wait_agent` requires `targets` (with optional
`timeout_ms`); `close_agent` requires `target`; and `resume_agent` requires
`id`.

V1 results identify the child with `agent_id`. The result fields are
tool-specific: spawn returns `agent_id`/`nickname`, wait returns
`timed_out`/`status`, close returns `previous_status`, and send/resume return
`status`. History links use `agent_id`, `call_id`, `call_item_id`, and
`output_item_id`; the continuation identity is `agent_id`. `close_agent` and
`resume_agent` are separate V1 operations. V1 has no V2 `task_path`,
`continuation_id`, or `fork_turns` semantics.

## V2 shape (`collaboration`)

V2 is an accepted structural contract, not a live capture; its official CLI
capture status is `not_observed`. V2 declarations are a `namespace` with child
`function` tools. Calls use the
same Responses `function_call` envelope (`namespace`, child `name`,
`item_id`, `call_id`, and `arguments`), and results use
`function_call_output` linked by `call_id` and `item_id`.

V2 spawn requires `task_name` and `message`; optional controls are
`fork_turns`, `agent_type`, `model`, and `reasoning_effort`. `fork_turns` is
preserved as one of `all`, `none`, or a positive decimal string. The other
operations are `send_message(target, message)`,
`followup_task(target, message)`, `wait_agent(timeout_ms?)`,
`interrupt_agent(target)`, and `list_agents(path_prefix?)`.

V2 continuation and history identity use `task_path` (with
`continuation_id`, `task_name`, and `fork_turns` carried alongside it), plus
the call/item links. V2 does not acquire V1's `agent_id`, `send_input`,
`close_agent`, `resume_agent`, or `fork_context` semantics. Full pagination and
continuation behavior remains a deferred qualification.

## Common wire lifecycle shape

For both families, the bounded function-call stream shape is:

1. `response.output_item.added`
2. `response.function_call_arguments.delta`
3. `response.function_call_arguments.done`
4. `response.output_item.done`

The terminal set is `response.completed`, `response.incomplete`, and
`response.failed`; the error event is `response.failed` with `id`, `status`,
and `error` fields. These stream and terminal/error entries are structural
markers with `qualification=not_observed`; they do not claim a live run.

## Ownership and adapter boundary

The Codex client owns execution for both V1 and V2 (`owner=codex_client`,
`executor=codex_client`). The Gateway is not an executor, scheduler, result
forger, or V2-to-V1 downgrade path.

The JSON records requirements only for follow-up issues #198 and #58. The V2
namespace may pass natively when the selected protocol supports namespaces;
otherwise a generic namespace-to-function Adapter is optional for V2 and must
remain reversible. The current V1 compatibility namespace similarly requires
that Adapter only when the selected protocol lacks namespace support.

- map a namespace child to an injective, request-scoped function alias;
- preserve `task_path`, `continuation_id`, `task_name`, and `fork_turns`;
- preserve inverse declaration/call/result/history mappings;
- assemble streamed arguments before validation;
- reject unknown or ambiguous boundaries; and
- keep V1 and V2 repair paths isolated.

No adapter, route, catalog, model-selection, scheduling, or tool-execution
implementation is included in this issue.

## Deferred qualification

The inventory deliberately does not qualify the complete
`spawn → message/follow-up → wait → interrupt → list` lifecycle. Restart and
cold-root resume, cross-Home topology/history, and full V2 pagination are also
deferred to the Collaboration V2/Beta4 phase (`#284`, with topology/history
work in `#197`). Those controls require separately authorized evidence; this
artifact must not be read as a release, capability unlock, or issue-closure
claim.
