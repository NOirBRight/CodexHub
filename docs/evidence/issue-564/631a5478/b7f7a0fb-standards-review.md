## Standards

No documented-standard violations found in the delta. The runner allows only the production `restart_required` optional key for non-Codex apply results and passes it through the existing validator, which rejects non-strings, values over 512 characters, credential-like content, and Windows/UNC paths. Required and unknown-key checks remain unchanged. The fixture now emits the optional field, and the renamed test exercises it through the runner contract. This follows the repository’s test-seam rule in `docs/agents/verification-policy.md`; the E2E contract docs also identify the new field and validation.

Possible baseline smells: none. The change is narrowly scoped across the runner, its fixture/test, and the matching contract documentation.
