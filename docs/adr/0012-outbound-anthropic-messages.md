# Outbound Anthropic Messages is a Chat-subset converter

Third-party clients reach OpenCode Go models that speak `/messages` by converting through the existing Chat Completions subset in `anthropic_messages.py`. This is not ADR-0001: that IR is for inbound Claude Code. Codex adapter surfaces stay deferred; Provider-level `anthropic_messages` remains unsupported except for catalog-owned models such as `union-alpha`.
