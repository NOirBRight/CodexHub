# Report-only quality gates

Run from the repository root:

```powershell
python scripts/report_quality_gates.py
```

Machine-readable output for automation or review:

```powershell
python scripts/report_quality_gates.py --json
```

These checks are intentionally **report-only**. Findings must not fail CI or block a release until the baseline noise is reviewed and the allowlist is tightened.

## Current reports

- `python_unused_imports`: simple AST-based top-level Python import references.
- `python_dead_functions`: simple AST-based top-level Python functions that are not referenced by scanned Python names.
- `duplicate_function_names`: simple duplicate helper/function names across Python, TypeScript/TSX, and Rust source files.

## Allowlist

Intentional legacy compatibility or conventional entrypoint names live in:

```text
config/report-quality-allowlist.json
```

Keep allowlist entries narrow:

- Prefer `{"path": "...", "name": "..."}` when suppressing a specific Python finding.
- Use name-only duplicate suppressions only for intentional compatibility shims or conventional entrypoints.
- Include a concrete `reason` so future cleanup can decide whether the exception still applies.

Do not add new allowlist entries only to hide fresh code smells. Fix the code first unless the duplicate/dead shape is intentional compatibility.
