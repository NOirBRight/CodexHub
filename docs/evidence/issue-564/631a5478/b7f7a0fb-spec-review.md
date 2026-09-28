Spec review — `720dd565...b7f7a0fb` (commit `b7f7a0fb`)

No spec findings.

The change accepts the production `restart_required` field only as an optional field on non-Codex `apply` results, using the existing bounded safe-string validator. Required fields, unknown-key rejection, materialization/readback identity checks, and the four-client × two-model live checks remain intact. The fixture emits this field for non-Codex apply and the renamed test exercises it. This is consistent with #564's requirement to preserve existing client behavior.

Evidence follow-up: the prior catalog-provenance finding is resolved by an evidence index that records the same source input path and SHA-256 as `official-catalog-input.json`, links the separate `refresh-models` diagnostic, binds both to the same candidate SHA, and labels refresh as blocked/unverified. No provenance field is required in the runner artifact schema. The index was not yet present in this checkout when reviewed, so this resolution depends on adding it as described.
