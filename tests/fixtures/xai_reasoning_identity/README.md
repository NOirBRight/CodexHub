`rez-search.jsonl` is the 446-event direct xAI Responses SSE capture from
2026-09-27, converted to one JSON event per line. It keeps event order,
output indices, IDs, search stages, annotations, terminal output, and usage.
Reasoning summary text is replaced with a marker, and encrypted reasoning
content is removed. The original capture is held outside the repository at
`~/.local/state/ayaspace-search-probe/20260927/grok-direct-rez.sse`.
