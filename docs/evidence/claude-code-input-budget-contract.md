# Claude Code evidence input and budget contract (#557)

`src-python/evidence_input_budget.py` is the offline preflight seam for the
Claude Code evidence round. It does not perform HTTP. The caller supplies one
explicit contract path and calls `reserve` immediately before every actual
upstream attempt:

```python
inputs = EvidenceInput.load(explicit_input_path)
inputs.reserve(protocol, method, final_url, exact_immutable_body_bytes)
```

The caller must pass the same bytes to its HTTP client after admission and must
not put redirects or automatic retries below the seam. A refusal means no
outbound attempt and does not reset or create a new round. The claim is an
atomic owner-only marker (`O_EXCL`); reloading the same grant, including from a
new process, is refused. A fresh round needs a new user grant, `round_id`, and
claim path. Owner-only materialization/claims fail closed before file creation
on platforms without `os.fchmod`; this helper has no Windows ACL claim.

## Input shape

The input file has schema `codexhub.claude-evidence-input.v1` and contains:

- `limits.max_attempts_per_protocol` (1–20),
  `limits.max_output_tokens` (1–2048), and `limits.deadline_seconds` (1–1800);
- a `grant` with schema `codexhub.claude-evidence-grant.v1`, one `round_id`,
  timezone-aware `issued_at`/`expires_at`, a new explicit `claim_path`, and a
  decision for **every** leg;
- `legs`, each with a unique id, one fixed protocol/provider/model, one fixed
  HTTPS (or loopback-only HTTP synthetic) endpoint origin, explicit route paths and methods, the protocol's
  exact `token_field`, and (for an approved leg) a credential descriptor with
  an absolute `path`, schema, target origin, and declared secret fields. Refused
  and undecided legs carry no credential descriptor.

A decision is one of `approved`, `refused`, or `undecided`. Missing, expired,
not-yet-valid, refused, and undecided legs fail closed. An unsettled native
Anthropic/Ollama leg is represented explicitly as `undecided`; it is not
silently treated as approved. A leg cannot wildcard its model, origin, route,
or token field, and duplicate protocol bindings are rejected. JSON contracts,
JSON credential files, and JSON request bodies use the repository strict parser
and reject duplicate keys/non-finite values. TOML credential files use
`tomllib` and its TOML rules.

Credential paths are never discovered from a home directory, environment
variable, configured provider, or personal auth store. The optional
`materialize_credential(source, destination)` helper only copies an explicitly
supplied synthetic/source file to a new non-existing owner-only (`0600`) file;
it never writes the source, refreshes OAuth, or exposes the copied value.
Summaries expose only `credential_present: true|false`.

## Body and token-field binding

| Upstream protocol | Required output-limit field | Allowed request route |
| --- | --- | --- |
| `anthropic_messages` | `max_tokens` | exact configured route(s) |
| `responses` | `max_output_tokens` | exact configured route(s) |
| `chat_completions` | exactly one configured choice: `max_tokens` **or** `max_completion_tokens` | exact configured route(s) |

The configured field must be present on ordinary request bodies, be a positive
integer, and be at most the configured bound (never above 2,048). A Chat leg
chooses one field in its input contract; the other field, both fields, or an
unknown token-limit field is refused. The count-token auxiliary route may omit
an output-limit field, but unknown token-limit fields still refuse. The body
model must equal the leg's fixed model. Reasoning selections, when present,
must use the configured selection (`max` for the approved Responses leg), and
an ordinary request must carry that exact selection; missing selection refuses.
The auxiliary count-token route may omit it. Selection is never injected,
normalized, or silently changed. Once an expired grant is observed, it remains
terminal even if the wall clock moves backward.

`reserve` validates the protocol, method, final URL origin/path, model, body,
credential authorization, monotonic deadline, and attempt count while holding
one lock; only then does it increment the count and return a non-sensitive
reservation. The global deadline is monotonic and shared by all legs. Every
redirect/auxiliary/retry attempt consumes the same per-protocol bound, and an
N+1 or deadline refusal happens before its HTTP write.

The focused synthetic checks are in
`tests/test_evidence_input_budget.py`. They do not read, copy, refresh, or call
any real credential/provider.
