# Issue #66 Responses-to-Chat conversion matrix

This directory holds the versioned conversion matrix for Codex Responses
inbound traffic that must ride a third-party Chat Completions upstream and
return Responses to Codex App.

The matrix is keyed by field/item, value, declaration type, execution owner,
and protocol representation. It is not keyed by model or Provider name.

## Sources

- `src-python/protocol_translation.py` for the already-landed standard Chat floor
- [issue-392 collaboration runtime contract](../issue-392/collaboration-runtime-contract.json)
- [issue-64 V1/V2 inventory](../issue-64/collaboration-v1-v2-inventory.json)

No live CLI capture, credentials, or raw request bodies are stored here.

## Dispositions

| Disposition | Meaning |
| --- | --- |
| `native` | Chat carries the semantic without mapping |
| `consumed_locally` | Gateway consumes a Codex transport default; nothing is forwarded |
| `reversibly_adapted` | An approved equivalent exists in both directions |
| `unavailable` | Reject before upstream sampling |

Unknown fields/items cannot disappear. There is no hidden fallback to
Responses, Official, another model, or another Provider.

Collaboration V2 namespace tools have no Chat native form. Their Chat path is
the Gateway adapter implemented by #394 (injective function aliases plus
request-bound plaintext `agent_message` envelopes). Encrypted Official payloads
stay `unavailable` on third-party Chat. Exact text-format custom tools are
reversibly adapted only when the selected protocol capability explicitly accepts
the request-scoped custom adapter; missing capability or malformed envelopes
remain `unavailable` and fail closed.

The real Codex transport defaults are classified by exact value before Chat
sampling: lifecycle `client_metadata`, cache identity, the encrypted-reasoning
include request, bounded verbosity, and documented reasoning controls (`effort`,
`summary`, legacy `generate_summary`, `mode`, and `context`) are consumed
locally. Opaque `client_metadata` objects remain local; Chat cannot return
Responses reasoning controls or summaries, and unknown top-level transport
fields or unapproved include/text/reasoning shapes still fail closed.

## Regenerate

```powershell
.\scripts\codexhub-python.cmd scripts\build_issue_66_chat_conversion_matrix.py
.\scripts\codexhub-python.cmd scripts\build_issue_66_chat_conversion_matrix.py --check
```
