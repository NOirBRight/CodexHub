# Issue #62 runtime-plan evidence

This evidence set captures one sanitized current-side Codex runtime plan and
one replay-consistency fixture. Opaque aliases replace request, response, call,
and item identifiers. Prompt text, tool arguments, tool output, and upstream
payloads are redacted.

## Facts established by the capture

- The captured codex_app dynamic namespace registers 15 functions.
- Three functions omit deferLoading; the installed runtime maps that to Direct.
  Twelve functions set deferLoading true; the runtime maps those to Deferred.
- The caller request includes client-executed tool_search. Its codex_app
  namespace contains the three Direct functions; Deferred functions remain
  discoverable through tool_search.
- The captured Gateway route is official Responses-to-Responses. Route
  classification comes from the Gateway upstream route and catalog binding, not
  from configured provider id custom.
- A caller/upstream request-prefix match was observed for 65,536 bytes.
  Full-body request and response fingerprints were not captured. The manually
  derived replay fixture checks internal consistency only; it does not rule out
  Gateway filtering beyond the observed prefix. The exact-version Desktop core
  and Code Mode app-server controls pass.

The source snapshot is OpenAI Codex commit
9e552e9d15ba52bed7077d5357f3e18e330f8f38. At that revision, the dynamic
tool protocol defines optional deferLoading; the dynamic handler maps true to
Deferred and missing or false to Direct. ToolExposure keeps Direct,
DirectModelOnly, Deferred, and Hidden distinct. Tool search is planned only
when model supports_search_tool and provider namespace_tools are both true.

## State coverage

| State | Evidence status | Meaning in this artifact |
| --- | --- | --- |
| Direct | Observed | Three codex_app functions omit deferLoading. |
| DirectModelOnly | Source contract | Distinct planner state; not used by the captured namespace. |
| Deferred | Observed | Twelve codex_app functions set deferLoading true. |
| Hidden | Source contract | Distinct planner state; not used by the captured namespace. |
| hosted-only | Sentinel | Host-binding tag retained distinctly; not inferred as a planner enum. |
| host-unavailable | Sentinel | Host-binding tag retained distinctly; not inferred as a planner enum. |

Unknown tags in the wire fixture are deliberately opaque sentinels. A replay
must preserve them rather than delete or normalize them.

## Wire and replay coverage

The wire fixture records sanitized pre-Gateway and post-Gateway request and
response/SSE shapes, request/history/response item aliases, call/item links,
observed streaming SSE event kinds, a non-streaming contract sentinel, and a
separate choice-control sentinel. The catalog source includes a read-only
fingerprint and model-entry validation for the captured catalog binding. The
replay checks:

1. reconcile registered, contributor, pre-Gateway, and post-Gateway tool
   surfaces in the replay fixture;
2. validate request/history/response call-to-output identities;
3. preserve tagged unknown SSE and non-streaming items; and
4. assert every required thread tool is registered, Deferred, and discoverable;
   and
5. fail visibly for in-memory mutation, deletion, loss, required-set deletion,
   and required-membership mutation controls.

## Fact/hypothesis boundary and remaining gap

Observed: the Desktop host/model did not select an available tool_search during
this trace. The retained evidence is insufficient to conclude whether Gateway
filtering contributed outside the observed request prefix.

The complete installed model-visible plan remains partial: this retained
sanitized capture contains the codex_app contributor only. Other contributors
and namespaces have not been inferred from this one capture.

Unproven: that a post-rewrite catalog timeline created a stale
StaticModelsManager, or that a clean restart for the current CodexHub binding
changes selection. The in-process rewrite and clean-cold-start cases remain
separate; no shared-runtime restart or configuration experiment was run for
this evidence update. Reluctant-model and tool_search lifecycle work belongs
to #63.
