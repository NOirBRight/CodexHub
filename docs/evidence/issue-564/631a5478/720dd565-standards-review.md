## Standards

No documented-standard violations found in `720dd565`. The new `-OfficialCatalog` input is threaded through the runner supervisor’s exact argument contract, constrained to a regular file under the isolated run root, checked for reparse points, parsed as JSON, copied into the candidate runtime, and verified against the original SHA-256 before use. Invalid, missing, and outside-root inputs fail closed in the added runner-contract tests. The tests exercise the runner interface rather than private production internals, consistent with `docs/agents/verification-policy.md` (“The interface is the test surface”). The documentation in `docs/agents/real-client-e2e.md` describes the mode’s limits and retained live checks.

Possible baseline smells: none found. `OfficialCatalog` is a clear name; the new option and its validation, provenance record, tests, and docs form one cohesive change. No duplicated control flow or speculative abstraction stands out.
