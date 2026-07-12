# ADR-0001: Use an AnthropicMessage representation before any Messages route

Date: 2026-07-12
Status: Accepted for a future implementation seam; no production route is authorized

## Context

Issue #74 tested Claude Code as a **downstream Gateway client**. Claude Code
sends Anthropic Messages requests with ordered content blocks, full history,
opaque tool IDs, open-set headers/body fields, and an incremental Messages SSE
response. CodexHub currently has only Responses and Chat Completions inbound
formats; its implicit conversion helpers are coupled to the production Gateway
handler.

The Spike's strict prototype can translate a small text/tool subset and convert
Responses or Chat Completions streams into named Messages SSE events. Its real
Claude Code loopback evidence also observed default fields with no established
non-Anthropic equivalent: adaptive thinking, effort output configuration,
cache-control-bearing blocks, and open Anthropic beta/header fields.

## Decision

If a later issue adds an Anthropic Messages adapter, it will introduce a
dedicated, pure `AnthropicMessage` intermediate representation. It will not add
another set of implicit conversions inside `codex_proxy.py`.

The representation's public interface must preserve:

- message role and ordered content blocks;
- tool-use and tool-result blocks, including the original opaque tool ID;
- top-level system blocks without reordering them;
- known request options plus an explicit collection of unmodelled fields;
- case-insensitive headers classified as credential, transport, opaque client
  metadata, or Anthropic semantic fields.

Adapters from this representation to Responses and Chat Completions must return
one of two explicit outcomes:

1. a translated request plus a list of declared adaptations; or
2. a non-forwardable result naming every unmodelled field.

An adapter may not emit a request after silently dropping a field. A tool call's
ID must round-trip unchanged through the assistant tool-use response and the
following user tool-result request.

Usage mapping may omit only a validated redundant total. Cache, reasoning, or
provider-detail usage fields require a named representation/mapping decision;
until then the adapter must return an explicit non-forwardable result rather
than collapse them into base token counts.

## Version and extension policy

- The evidence pin is `anthropic-version: 2023-06-01`, revalidated against the
  Claude Code gateway protocol on 2026-07-12.
- The public source pin is Claude Code `v2.1.207`; the local loopback evidence
  used `v2.1.201` and is not interchangeable with the source pin.
- `anthropic-*`, `anthropic-beta`, and `x-claude-code-*` are open sets. An
  Anthropic-format upstream may forward the required semantic fields unchanged.
  A non-Anthropic adapter must map a known semantic or return an explicit
  unsupported result; it must never rely on an allowlist that silently strips a
  new field.
- Downstream Gateway credentials are consumed at the Gateway boundary and are
  never used as upstream provider credentials or retained in traces.

## Compatibility level

**Scoped PARTIAL.** The prototype demonstrates explicit adaptation for basic
text/history, client-tool ID lifecycle, Messages SSE, reported base usage (with
validated totals), a generic error envelope, and deterministic Responses/Chat
protocol shapes. It explicitly rejects unmapped cache/reasoning/provider usage
detail. It does not establish safe production behavior for thinking, prompt
caching, context compaction/resume, server tools, beta fields, count tokens,
cancellation, or Claude Code automatic retry semantics.

Therefore this decision explicitly prohibits, until a follow-up closes the
named gaps:

- production `POST /v1/messages` or `/v1/messages/count_tokens` handling;
- changes to `codex_proxy.py` handler registration or routing;
- Claude Code client auto-configuration or discovery UI;
- treating Claude Code as an upstream ACP/AgentProvider.

## Consequences

The future module becomes the single seam callers and tests use for Messages
translation. That gives tests a deep, in-memory interface and keeps protocol
evolution localized. It also creates an intentional extra implementation step
before a route can ship: each new Messages capability needs a representation
decision and a test, rather than becoming an accidental omission inside an
existing Gateway conversion helper.

## Alternatives considered

### Extend the current implicit Gateway conversion helpers

Rejected. They are shaped around Responses and Chat Completions HTTP handling,
would couple Messages evolution to the production route, and cannot expose an
honest non-forwardable result before upstream I/O.

### Forward every field to a non-Anthropic upstream

Rejected. Responses and Chat Completions do not share the Anthropic Messages
schema; forwarding beta/body pairs or cache controls unchanged would cause
opaque failures or semantic loss.

### Model Claude Code as an AgentProvider

Rejected for this scope. A downstream Messages client is distinct from the
separate upstream Claude Code/ACP AgentProvider roadmap. No AgentProvider,
session, permission, or ACP lifecycle is introduced by this decision.
