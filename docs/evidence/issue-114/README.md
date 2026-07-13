# Issue #114 sanitized Desktop capture evidence

This directory contains only sanitized, task-owned summaries of manual
Desktop captures for Issue #114. It must never contain raw prompts, response
text, endpoint values, credentials, session or task identifiers, local paths,
or copied Gateway logs.

Each record keys a capture by Desktop build, route, model, proxy mode,
protocol/WebSocket setting, prompt provenance, timing semantics, terminal
classification, and evidence provenance. A renderer observation and a Gateway
SSE event are deliberately distinct: Gateway exposure does not prove that text
was visible in the renderer.

## Reuse rule

An equivalent current control may be reused only when every recorded key is
equivalent and the evidence has not been explicitly expired. A historical
reporter observation never substitutes for an instrumented control. Records
whose private prompt digest was intentionally not retained are not reusable as
prompt-equivalent controls.

The current record is a non-reproducing delayed-visibility observation. It
does not establish a first closing side, a network root cause, or general
stream reliability. Per the Issue #114 localization decision, retain
instrumentation for the next natural faulty window rather than repeating a
rate control or changing production transport behavior.

## Records

- [Gateway Official-auto delayed-visibility observation](gateway-official-auto-delayed-visibility.json)
