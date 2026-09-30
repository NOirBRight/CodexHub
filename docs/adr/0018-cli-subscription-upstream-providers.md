# ADR-0018: CLI subscription upstream Providers

Date: 2026-10-01. Status: Accepted product direction from the confirmed
[planning interview](../research/2026-09-29-claude-cursor-subscription-provider-planning.md).

Claude Subscription Provider uses the locally signed-in official Claude Code
CLI and caller tools bridged through MCP. Cursor Subscription Provider uses
the currently signed-in Cursor CLI account and its AgentService transport.
Both join the existing Provider catalog, Client Projection and Gateway protocol
adapters. Magpie is feasibility evidence, not a production binary dependency.

This narrowly amends ADR-0005's Anthropic exclusion and ADR-0015's
unrelated-client exclusion for this CLI-backed Provider. It does not introduce
a general consumer-OAuth-to-Messages relay or token-refresh ownership in
SubscriptionCredential. Official CLIs own login and refresh; CodexHub must
isolate backend settings from downstream Gateway injection and never switch
accounts. It also supersedes the rejected upstream recommendation in the
2026-09-09 Cursor research, without adding Cursor as a downstream Client.

Discovery retains exact account model IDs without Magpie's export cap or a
qualification whitelist. Backend lifecycle and cancellation belong behind an
exchange interface, separate from bearer-header credentials. Tool execution,
agent scheduling and task recreation remain caller-owned. Reuse the existing
request-scoped custom/namespace codecs and directed-message adaptation rather
than importing the prototype's competing codec. Recover mislabelled plaintext
only from an authoritative matching assignment; ciphertext is never treated as
a task or replaced by a success-shaped placeholder.

Claude enablement requires explicit confirmation that calling-agent system
instructions travel in CLI user context. Cursor requires actual Code Mode,
both mixed-provider V2 directions and full completed-history continuation after
Gateway restart. Durable in-flight agents are outside scope. Each Provider may
ship independently after its complete contract passes; discovery or an ordinary
chat response alone is not release acceptance.
