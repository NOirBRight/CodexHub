# Wayfinder GitHub Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans and explicit Inline Execution to implement this plan task-by-task. Do not dispatch Hidden Subagents or delegated Workers for this migration. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate GitHub Wayfinder #147 from the obsolete feature-first roadmap to the approved reliability-gated release train without claiming or starting product implementation work.

**Architecture:** GitHub remains the only durable work-state authority. The migration is a two-phase, idempotent, readback-driven operation: first synchronize an isolated worktree and produce a read-only snapshot plus exact write-set preview; then stop for explicit human authorization before creating milestones, decomposing mixed-ownership Issues, normalizing hierarchy/labels/dependencies, rewriting #147, and recomputing the ready frontier. Product code and per-Issue implementation plans remain outside this plan.

**Tech Stack:** PowerShell 7, GitHub CLI (`gh`), GitHub REST API, Git, Markdown.

## Global Constraints

- Repository: `NOirBRight/CodexHub`; integration branch: `dev`.
- Approved design: `docs/superpowers/specs/2026-07-16-wayfinder-replanning-design.md` at or after commit `59477301`.
- Approved plan baseline: `docs/superpowers/plans/2026-07-16-wayfinder-github-migration.md` at or after commit `51a636c9`.
- Execute only from a clean linked worktree on a non-protected planning branch. Never execute from `dev`, `main`, `master`, or `develop`.
- Use `gh` for every GitHub Issue, milestone, label, dependency, and sub-issue operation.
- Do not assign an Issue, create a product branch/worktree, start a Worker, edit product code, or open a production PR during this migration.
- Execution mode for this GitHub planning-state migration is fixed to explicit Inline Execution. Do not use `task`-tool dispatch, Hidden Subagents, `spawn_agent`, or delegated Workers. Sidebar-visible Terra/max or Luna/max Workers remain the preferred surface for later product-Issue implementation, not for this sequential migration.
- Task 1 is mandatory and read-only with respect to GitHub. After Task 1, stop and obtain explicit human authorization for the displayed write set before running any command in Tasks 2–8. Prior generic approval, approval to write the plan, or approval to run the dry run does not authorize GitHub writes.
- Preserve exactly one canonical lifecycle label on each Issue: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, or `wontfix`.
- Preserve reporter evidence. Never publish credentials, Task IDs, callback addresses, private paths, prompts, or raw private traces.
- Native `blocked_by` edges represent only hard dependencies. Related work remains text-only.
- A GitHub write is complete only after title/body/labels/state/assignee/milestone/parent/dependencies/URL readback.
- The five active milestones are exact strings:
  - `0.1.6 — Codex control-plane reliability`
  - `0.1.7 — Official GPT reliability`
  - `0.1.8 — Third-party model certification`
  - `0.1.9 — Managed client reliability`
  - `0.1.10 — Existing product reliability`
- Git commits are required only for this plan document. Implementation tasks mutate GitHub state and use GitHub readback comments as durable checkpoints; they must not create empty repository commits.

---

### Task 1: Capture the read-only migration baseline

**Files:**
- Read: `docs/superpowers/specs/2026-07-16-wayfinder-replanning-design.md`
- Temporary: `$env:TEMP\codexhub-wayfinder-migration\issues.json`
- Temporary: `$env:TEMP\codexhub-wayfinder-migration\issues-api.json`
- Temporary: `$env:TEMP\codexhub-wayfinder-migration\milestones.json`
- Temporary: `$env:TEMP\codexhub-wayfinder-migration\map.json`
- Temporary: `$env:TEMP\codexhub-wayfinder-migration\map-children.json`
- Temporary: `$env:TEMP\codexhub-wayfinder-migration\open-prs.json`
- Temporary: `$env:TEMP\codexhub-wayfinder-migration\proposed-writes.json`

**Interfaces:**
- Consumes: approved design commit `59477301`, plan baseline commit `51a636c9`, and the current GitHub repository state.
- Produces: a synchronized isolated planning worktree, a timestamped read-only JSON snapshot, an exact write-set preview, and a mandatory human authorization checkpoint.

- [ ] **Step 1: Synchronize and verify the isolated planning worktree**

Run:

```powershell
$designBase = '59477301'
$planBase = '51a636c9'
$protected = @('dev','main','master','develop')
$branch = git branch --show-current
$status = @(git status --porcelain)
if ($status.Count) { throw 'planning worktree is not clean' }
if (-not $branch -or $branch -in $protected) { throw "unsafe execution branch: $branch" }

$gitDir = [IO.Path]::GetFullPath((git rev-parse --git-dir))
$gitCommon = [IO.Path]::GetFullPath((git rev-parse --git-common-dir))
$superproject = git rev-parse --show-superproject-working-tree
if ($superproject) { throw 'execution directory is a submodule, not the planning worktree' }
if ($gitDir -eq $gitCommon) { throw 'execution must use a linked worktree' }

git merge-base --is-ancestor $planBase HEAD
if ($LASTEXITCODE -ne 0) {
  $counts = @((git rev-list --left-right --count "HEAD...$planBase") -split '\s+')
  if ([int]$counts[0] -ne 0) {
    throw "stale branch has unique commits; do not reset or rebase automatically: $($counts -join '/')"
  }
  git merge --ff-only $planBase
  if ($LASTEXITCODE -ne 0) { throw 'safe fast-forward to approved plan baseline failed' }
}

git merge-base --is-ancestor $designBase HEAD
if ($LASTEXITCODE -ne 0) { throw 'approved design commit is not an ancestor of HEAD' }
git merge-base --is-ancestor $planBase HEAD
if ($LASTEXITCODE -ne 0) { throw 'approved plan baseline is not an ancestor of HEAD' }
if (-not (Test-Path 'docs/superpowers/specs/2026-07-16-wayfinder-replanning-design.md')) { throw 'design file is not in this worktree' }
if (-not (Test-Path 'docs/superpowers/plans/2026-07-16-wayfinder-github-migration.md')) { throw 'plan file is not in this worktree' }

git remote get-url origin
git branch --show-current
git status --short
gh repo view --json nameWithOwner,defaultBranchRef,url
gh auth status
```

Expected:

- remote is `https://github.com/NOirBRight/CodexHub.git`;
- branch is a non-protected planning branch in a linked worktree;
- `git status --short` is empty;
- both `59477301` and `51a636c9` are ancestors of `HEAD`, and both plan files are present in-tree;
- GitHub repository is `NOirBRight/CodexHub` and authentication succeeds.

- [ ] **Step 2: Capture Issues, REST metadata, milestones, map membership, and PRs**

Run:

```powershell
$root = Join-Path $env:TEMP 'codexhub-wayfinder-migration'
New-Item -ItemType Directory -Force -Path $root | Out-Null

gh issue list --state all --limit 1000 `
  --json number,title,state,body,labels,assignees,author,createdAt,updatedAt,closedAt,comments,url `
  | Set-Content -Encoding utf8 (Join-Path $root 'issues.json')

gh api --paginate --slurp 'repos/NOirBRight/CodexHub/issues?state=all&per_page=100' `
  | Set-Content -Encoding utf8 (Join-Path $root 'issues-api.json')

gh api 'repos/NOirBRight/CodexHub/milestones?state=all&per_page=100' `
  | Set-Content -Encoding utf8 (Join-Path $root 'milestones.json')

gh api repos/NOirBRight/CodexHub/issues/147 `
  | Set-Content -Encoding utf8 (Join-Path $root 'map.json')

gh api --paginate --slurp repos/NOirBRight/CodexHub/issues/147/sub_issues `
  | Set-Content -Encoding utf8 (Join-Path $root 'map-children.json')

gh pr list --state open --limit 100 `
  --json number,title,headRefName,baseRefName,author,url `
  | Set-Content -Encoding utf8 (Join-Path $root 'open-prs.json')
```

Expected: all six snapshot files exist and parse as JSON; the Issue snapshot contains 93 Issues at the design baseline, with later additions allowed only after they are reported before any write.

- [ ] **Step 3: Validate the snapshot without changing GitHub**

Run:

```powershell
@'
import json, os, pathlib
root = pathlib.Path(os.environ['TEMP']) / 'codexhub-wayfinder-migration'
issues = json.loads((root / 'issues.json').read_text(encoding='utf-8-sig'))
open_issues = [x for x in issues if x['state'] == 'OPEN']
maps = [x for x in open_issues if 'wayfinder:map' in [l['name'] for l in x['labels']]]
assert len(maps) == 1 and maps[0]['number'] == 147, maps
assert all(x['number'] != 147 or x['title'].startswith('Wayfinder:') for x in issues)
print({'total': len(issues), 'open': len(open_issues), 'map': maps[0]['url']})
'@ | python -
```

Expected: one open Wayfinder map, #147. If counts or map identity differ from the snapshot described by the design, stop and reconcile the delta before Task 2.

- [ ] **Step 4: Record the immutable pre-write hashes**

Run:

```powershell
$root = Join-Path $env:TEMP 'codexhub-wayfinder-migration'
Get-FileHash (Join-Path $root '*.json') -Algorithm SHA256 `
  | Sort-Object Path `
  | Format-Table Hash,Path -AutoSize
```

Expected: one SHA-256 per snapshot file. Retain this terminal output for the migration review checkpoint.

- [ ] **Step 5: Generate and display the exact GitHub write-set preview**

Run:

```powershell
$root = Join-Path $env:TEMP 'codexhub-wayfinder-migration'
$preview = [ordered]@{
  repository = 'NOirBRight/CodexHub'
  creates = [ordered]@{
    milestones = @(
      '0.1.6 — Codex control-plane reliability',
      '0.1.7 — Official GPT reliability',
      '0.1.8 — Third-party model certification',
      '0.1.9 — Managed client reliability',
      '0.1.10 — Existing product reliability'
    )
    issues_if_absent = @(
      'Bound repeated empty tool_search misses for external models',
      'Preserve Worker selector and effective binding validation for external delegation',
      'Remove CodexHub-owned Windows autostart registration during uninstall'
    )
  }
  mutations = [ordered]@{
    milestone_membership_issue_numbers = @(
      8,17,18,19,20,21,22,28,57,58,59,61,62,63,64,65,66,67,83,
      86,87,88,104,109,111,112,113,114,115,126,138,139,141,143,
      149,150,151,153,154,155,156,157
    )
    deferred_issue_numbers_remove_milestone = @(68,71,73,74,75,76,77,78,85,89,90,91,92,93,94,148,152)
    wayfinder_label_issue_numbers = @(8,28,68,71,83,86,87,88,89,90,91,92,93,94,104,109,111,113,115,126,155,156,157)
    hierarchy_issue_numbers = @(
      8,17,18,19,20,21,22,28,57,58,59,61,68,71,73,74,75,76,77,78,
      83,85,86,87,88,89,90,91,92,93,94,104,109,111,112,113,114,115,
      126,138,139,141,143,148,149,150,151,152,153,154,155,156,157
    )
    dependency_edges = @('#156 blocked by bounded-search child','#156 blocked by selector/binding child','#64 blocked by #156','uninstall child blocked by #111')
    dynamic_issue_mutations = @(
      'bounded-search child: bug + ready-for-agent + wayfinder:task, milestone 0.1.6, parent #156',
      'selector/binding child: bug + ready-for-agent + wayfinder:task, milestone 0.1.6, parent #156',
      'uninstall child: bug + ready-for-agent + wayfinder:task, milestone 0.1.10, parent #147'
    )
    bodies = @(
      '#156 title/body rewritten as the Host/runtime-only visible-Worker gate',
      '#28 narrowed to discovery performance',
      '#147 replaced with reliability-gated Wayfinder map including both #156 children'
    )
    comments = @('#8 baseline correction','#10 closure evidence','#12 closure evidence','#62 conditional ownership reconciliation','#147 final migration readback')
    assignee_changes = @('#62 removal only if native Task and PR readback prove ownership is stale')
    milestone_close = 'Third-party model agentic reliability, only after zero-open-Issue readback'
  }
  prohibited = @('Issue assignment other than conditional stale #62 removal','product branch/worktree','Worker dispatch','product code edit','production PR')
}
$preview | ConvertTo-Json -Depth 8 |
  Set-Content -Encoding utf8 (Join-Path $root 'proposed-writes.json')
Get-Content -Raw (Join-Path $root 'proposed-writes.json')
```

Expected: the seventh temporary JSON file exists and displays every class of durable write in Tasks 2–8. If the live snapshot changes any target or creates a title collision, revise the plan and regenerate this preview before requesting authorization.

- [ ] **Step 6: Stop for explicit human authorization**

Present the snapshot counts, SHA-256 hashes, and full `proposed-writes.json`, then ask exactly:

> Authorize Tasks 2–8 to perform the displayed durable GitHub writes on `NOirBRight/CodexHub`?

Expected: stop execution here. Continue to Task 2 only after an explicit affirmative response scoped to Tasks 2–8 and this displayed write set. A response authorizing only the dry run, plan revision, or worktree synchronization is not sufficient.

---

### Task 2: Create the five reliability milestones idempotently

**Files:**
- Read: `$env:TEMP\codexhub-wayfinder-migration\milestones.json`
- GitHub objects: repository milestones only.

**Interfaces:**
- Consumes: Task 1 read-only snapshot and the explicit human authorization captured by Task 1 Step 6.
- Produces: five open milestones discoverable by their exact titles; later tasks use titles rather than unstable milestone numbers.

- [ ] **Step 1: Define the exact milestone contracts**

Run:

```powershell
$milestones = [ordered]@{
  '0.1.6 — Codex control-plane reliability' = 'Exit: one reconciled Gateway lifecycle; sidebar-visible Worker materialization; bidirectional Task communication and receipts; bounded cancellation/exit/restart; recoverable Task state. Only 0.1.6 receives new work while this gate is active.'
  '0.1.7 — Official GPT reliability' = 'Exit: Official GPT catalog, Task/Worker communication, tool lifecycle, streaming/cancellation, and context authority are reliable enough to serve as the control group for third-party qualification.'
  '0.1.8 — Third-party model certification' = 'Exit: each supported provider/model/protocol/codec combination has runtime-derived capability evidence, full visible-Worker and Task communication coverage, fail-closed unknown semantics, and a #67 GO/PARTIAL/NO-GO decision.'
  '0.1.9 — Managed client reliability' = 'Exit: ZCode, OMP, OpenCode, and Pi preview/apply/readback/restore are transactional, identity-faithful, reasoning-faithful, and free of silent Official fallback.'
  '0.1.10 — Existing product reliability' = 'Exit: remaining existing pricing, Vision Proxy, diagnostics, notification, and uninstall-cleanup defects are resolved without adding new product capabilities.'
}
$milestones.GetEnumerator() | Format-Table Name,Value -Wrap
```

Expected: five exact titles and five non-empty exit contracts.

- [ ] **Step 2: Create or update each milestone**

Run:

```powershell
foreach ($entry in $milestones.GetEnumerator()) {
  $existing = @(gh api 'repos/NOirBRight/CodexHub/milestones?state=all&per_page=100' `
    --jq ".[] | select(.title == \"$($entry.Name)\") | .number")
  if ($existing.Count -gt 1) { throw "duplicate milestone title: $($entry.Name)" }
  if ($existing.Count -eq 0) {
    gh api --method POST repos/NOirBRight/CodexHub/milestones `
      -f title="$($entry.Name)" `
      -f description="$($entry.Value)" `
      -f state='open' | Out-Null
  } else {
    gh api --method PATCH "repos/NOirBRight/CodexHub/milestones/$($existing[0])" `
      -f title="$($entry.Name)" `
      -f description="$($entry.Value)" `
      -f state='open' | Out-Null
  }
}
```

Expected: every call succeeds; no duplicate title is created.

- [ ] **Step 3: Read back the milestone contracts**

Run:

```powershell
$actual = gh api 'repos/NOirBRight/CodexHub/milestones?state=all&per_page=100' | ConvertFrom-Json
foreach ($entry in $milestones.GetEnumerator()) {
  $matches = @($actual | Where-Object title -eq $entry.Name)
  if ($matches.Count -ne 1) { throw "milestone readback count: $($entry.Name)" }
  if ($matches[0].state -ne 'open') { throw "milestone not open: $($entry.Name)" }
  if ($matches[0].description -ne $entry.Value) { throw "description mismatch: $($entry.Name)" }
}
$actual | Where-Object title -in $milestones.Keys `
  | Select-Object number,title,state,open_issues,closed_issues `
  | Sort-Object number | Format-Table -AutoSize
```

Expected: five unique open milestones with exact descriptions.

---

### Task 3: Split #156 into one Host/runtime-only gate and two CodexHub adapter children

**Files:**
- Read: `docs/superpowers/specs/2026-07-16-wayfinder-replanning-design.md`
- GitHub Issue: #156, rewritten in place with exact title `Enable sidebar-visible third-party Worker callbacks and binding readback`.
- Discover or create GitHub Issue with exact title: `Bound repeated empty tool_search misses for external models`.
- Discover or create GitHub Issue with exact title: `Preserve Worker selector and effective binding validation for external delegation`.

**Interfaces:**
- Consumes: milestone `0.1.6 — Codex control-plane reliability` and Task 1's immutable pre-write Issue snapshot.
- Produces: `$localSearchIssue` and `$selectorBindingIssue`, both unassigned ready-for-agent CodexHub children of #156; #156 remains the unassigned ready-for-human Host/runtime-only gate; #156 is blocked by both children; #64 retains blockers #62/#63/#156.

- [ ] **Step 1: Re-read #156, #159, and comments immediately before writing**

Run:

```powershell
foreach ($n in 156,159) {
  gh issue view $n --comments `
    --json number,title,state,body,labels,assignees,milestone,comments,url
}
```

Expected: #156 is open, unassigned, and `ready-for-human`; #159 is open, unassigned, and `ready-for-agent`; all existing comments and sanitized evidence are visible before any mutation.

- [ ] **Step 2: Rewrite #156 as the exact Host/runtime-only gate with a fresh-body guard**

Run:

`````powershell
function Get-TextHash([string]$Text) {
  $bytes = [Text.Encoding]::UTF8.GetBytes(($Text -replace "`r`n","`n").TrimEnd())
  [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
}

$hostTitle = 'Enable sidebar-visible third-party Worker callbacks and binding readback'
$hostBody = @'
## Priority and external blocker

**Priority: P0 — complete the Host/runtime gate before resuming dependent orchestration work.**

This is a hard external blocker for [NOirBRight/github-work-orchestrator#16](https://github.com/NOirBRight/github-work-orchestrator/issues/16). Keep that Issue stopped and unclaimed until the native sidebar-visible Worker acceptance gate below passes.

## Sanitized reproduction

Environment: Codex Desktop 26.707.12708.0, Codex CLI 0.144.5, CodexHub route, Ollama Cloud native Responses, requested `glm-5.2 / max`.

1. A native worktree Worker was created from the parent and appeared as a separate sidebar Task.
2. Parent-to-child continuation worked, and the child had functioning shell/MCP tools.
3. The child could not access a supported parent-result callback or equivalent result-delivery channel. Exact-name searches returned `tools=[]`.
4. The child could not prove the effective agent type, model, and reasoning through supported readback.
5. Without a terminal unsupported result, the failed capability search could repeat. A bounded parent continuation stopped the read-only incident; the child returned `BLOCKED` and its worktree stayed clean.

Private rollout, Task, callback, local-path, call, and token identifiers remain excluded. Do not edit Codex SQLite or infer evidence from private IDs.

## Problem

The Codex Host/runtime does not yet provide a complete supported contract for third-party delegated implementation as a native **sidebar-visible Worker Task**. The required contract spans Worker materialization, bidirectional parent/Worker communication, result delivery with a confirmable receipt, effective binding readback, explicit unsupported terminalization, and a visible Active → Done lifecycle.

A model-visible callback schema is not sufficient when the child has no registered native handler. Requested settings are not effective binding evidence.

## Desired outcome

Native Host/runtime delegation can create a sidebar-visible third-party Worker and prove, before implementation begins, that the supported execution surface and effective binding match the request. The parent and Worker can communicate in both directions, the parent receives a confirmable result receipt, unsupported capabilities stop terminally, and the Worker becomes visibly Active and then Done.

## Scope — Host/runtime only

- Materialize delegated implementation as a separate native sidebar-visible Worker Task with supported discovery/readback.
- Register a supported parent-result callback in the Worker context, or provide an equivalent durable result-delivery channel whose receipt the parent can confirm.
- Support parent-to-Worker continuation and Worker-to-parent result return in the same isolated lifecycle.
- Expose supported effective readback for agent type, model, and reasoning effort before edits begin.
- Return an explicit, machine-classifiable terminal unsupported result when the requested Worker surface, callback/result channel, or binding cannot be provided.
- Expose supported lifecycle readback showing the Worker transition from Active to Done.
- Preserve sanitized Host/runtime evidence for a bounded manual replay.

## Non-goals

- Do not implement CodexHub's repeated identical empty-`tool_search` bound; #159 owns that work.
- Do not implement CodexHub Worker selector/codec preservation or effective-binding validation; #161 owns that work.
- Do not inject a callback schema without a registered Host/runtime handler.
- Do not substitute GPT, Hidden Subagent, Inline execution, Background process, `codex exec`, or a shell-launched model for the sidebar-visible Worker.
- Do not edit Codex SQLite, publish private IDs or local paths, synthesize rollout state, or infer binding from requested settings.
- Do not broaden into unrelated transport, routing, retry, apply-patch, or Task-activity rendering work.

## Acceptance criteria

- [ ] A native delegation materializes one separate sidebar-visible third-party Worker Task.
- [ ] Supported readback proves the effective Worker agent type, requested third-party model, and requested reasoning effort before implementation begins.
- [ ] Missing, unknown, contradictory, rejected, unsupported, or GPT-substituted binding evidence stops terminally before edits.
- [ ] Parent-to-Worker continuation and Worker-to-parent result delivery both succeed in the same isolated Worker lifecycle.
- [ ] The parent receives a supported confirmable receipt for the Worker's final result.
- [ ] An unavailable Worker surface, callback/result channel, or selector returns an explicit terminal unsupported result rather than continuing or substituting another surface.
- [ ] Supported Task APIs show the separate Worker as Active and then Done.
- [ ] The real validation performs only a disposable read-only action until surface, binding, and callback/receipt preflight pass.
- [ ] No GPT, Hidden Subagent, Inline, Background, `codex exec`, or shell-launched substitute appears in the evidence.

## Host/runtime verification and evidence

This Issue does not authorize or invent CodexHub product-code verification. Its acceptance is manual Host/runtime evidence from one isolated native Codex Desktop replay using a third-party model and requested reasoning:

1. create one native sidebar-visible Worker Task;
2. read back effective Worker agent type, model, and reasoning through supported APIs;
3. send one parent continuation;
4. return one Worker result and confirm the parent receipt;
5. read back Active → Done terminal visibility;
6. capture only sanitized outcome fields and failure classifications.

## Relationships

- External hard blocker: [NOirBRight/github-work-orchestrator#16](https://github.com/NOirBRight/github-work-orchestrator/issues/16)
- Broad collaboration verification: #64
- Deferred discovery classification: #63
- Runtime plan evidence: #62
- CodexHub repeated-search child: #159
- CodexHub selector/binding child: #161

## Expected hotset

Host/runtime-owned, outside this repository:

- native sidebar-visible Worker Task creation/materialization;
- child Host tool registration or equivalent result-delivery channel and receipt;
- supported effective agent/model/reasoning readback;
- supported Worker lifecycle state and explicit unsupported terminalization.

No CodexHub product-code hotset is owned by this Issue.

## Execution contract

Execution-Contract: v2
Verification-Class: strict
Verification-Commands: Host/runtime supported-API readback only; no CodexHub product-code command is claimed
Manual-Evidence: one isolated native third-party sidebar-visible Worker replay proving effective binding, bidirectional communication, confirmed parent receipt, explicit unsupported behavior, and Active → Done visibility
Architecture-Decision: resolved
Review-Owner: orchestrator
'@
$hostBody = ($hostBody -replace "`r`n","`n").TrimEnd()
$before = gh issue view 156 --comments `
  --json number,title,state,body,labels,assignees,milestone,comments,url | ConvertFrom-Json
$commentGuard = @($before.comments | ForEach-Object { "$($_.id):$(Get-TextHash $_.body)" })

if ($before.title -cne $hostTitle -or (Get-TextHash $before.body) -cne (Get-TextHash $hostBody)) {
  $snapshot = Get-Content -Raw (Join-Path $env:TEMP 'codexhub-wayfinder-migration/issues.json') | ConvertFrom-Json
  $baseline = @($snapshot | Where-Object number -eq 156)
  if ($baseline.Count -ne 1 -or $before.title -cne $baseline[0].title -or
      (Get-TextHash $before.body) -cne (Get-TextHash $baseline[0].body)) {
    throw '#156 changed since Task 1 snapshot; do not overwrite fresh evidence'
  }
  gh issue edit 156 --title $hostTitle --body $hostBody | Out-Null
}

$after = gh issue view 156 --comments `
  --json title,state,body,labels,assignees,milestone,comments,url | ConvertFrom-Json
$commentReadback = @($after.comments | ForEach-Object { "$($_.id):$(Get-TextHash $_.body)" })
if ($after.title -cne $hostTitle -or (Get-TextHash $after.body) -cne (Get-TextHash $hostBody)) { throw '#156 exact body readback failed' }
if (($commentReadback -join '|') -cne ($commentGuard -join '|')) { throw '#156 comments changed' }
`````

Expected: #156 has the exact Host/runtime-only title/body, all prior comments remain byte-equivalent after line-ending normalization, and no CodexHub product verification is claimed by the parent gate.

- [ ] **Step 3: Discover or create both exact CodexHub children idempotently**

Run:

`````powershell
$searchTitle = 'Bound repeated empty tool_search misses for external models'
$searchBody = @'
Part of #156.

## Problem

On the external-model compatibility path, an exact `tool_search` query can return `tools=[]` repeatedly while the same search function remains visible in replayed history. The model may resample the identical unavailable callback query without a terminal policy, amplifying requests, tokens, latency, and cost without creating a callable host tool.

## Outcome

Bound identical empty tool-search misses in the CodexHub adapter and return one sanitized, machine-classifiable unavailable terminal result. A different query and every non-empty result remain unaffected.

## Scope

- Track identical exact-name search misses within one request/turn history.
- Permit at most two identical empty searches or two additional model samples.
- On the bound, emit a deterministic unavailable result and prevent a third identical search sample.
- Preserve successful discovery, distinct queries, call IDs, history, SSE terminalization, and existing Official/third-party tool behavior.
- Record only sanitized query classification/count/status telemetry; do not record prompts, tool arguments, callback addresses, or Task identifiers.

## Non-goals

- Do not inject a callback schema without a real host handler.
- Do not implement Codex Host sidebar Worker registration, result receipts, or model-binding readback.
- Do not use GPT, Hidden Subagent, `codex exec`, Background, or Inline work as a substitute for #156 acceptance.
- Do not change unrelated retry, transport, apply-patch, or Task activity behavior.

## Acceptance criteria

- [ ] The first exact-name miss returns the existing explicit empty/unavailable search result.
- [ ] A second identical miss produces one classified terminal unavailable result and no third model sample for that query in the turn.
- [ ] A distinct legitimate query is not suppressed by the identical-query guard.
- [ ] A non-empty search result remains unchanged and callable.
- [ ] History replay preserves call/result identity and exactly one terminal outcome.
- [ ] Sanitized telemetry records the bound without prompts, callback addresses, credentials, paths, or Task identifiers.
- [ ] Official, namespace, MCP, collaboration, and strict apply-patch regressions remain green.

## Verification

```powershell
python -m pytest -q tests/test_routing.py -k "tool_search"
python -m pytest -q tests/test_codex_semantic_adapter.py
python -m pytest -q
python scripts/report_quality_gates.py
git diff --check
```

`report_quality_gates.py` is report-only.

## Expected hotset

- `src-python/codex_proxy.py`
- `src-python/codex_semantic_adapter.py`
- `tests/test_routing.py`
- `tests/test_codex_semantic_adapter.py`
- one sanitized fixture under `tests/fixtures/` if the deterministic replay needs it

## Relationships

- Parent/human Visible Worker gate: #156
- Full collaboration matrix: #64
- Deferred search classification: #63

## Execution contract

Execution-Contract: v2
Verification-Class: strict
Verification-Commands: targeted routing/semantic-adapter tests; full Python suite once; `git diff --check`; report-only quality gate
Manual-Evidence: none
Architecture-Decision: resolved
Review-Owner: orchestrator
'@
$selectorTitle = 'Preserve Worker selector and effective binding validation for external delegation'
$selectorBody = @'
Split from #156. This Issue owns only the CodexHub adapter contract for Worker selector preservation and supported effective-binding validation.

## Problem

`spawn_agent.agent_type=worker` is advertised at the adapter boundary but can be removed or weakened during normalization/history processing. The adapter also lacks a fail-closed contract for supported effective agent type, model, and reasoning readback, so requested settings can be mistaken for effective execution evidence.

## Outcome

CodexHub preserves the explicit Worker selector across every adapter stage, or rejects it before child execution, and consumes supported effective binding readback to prove the selected agent type, model, and reasoning. Missing, unknown, contradictory, rejected, or GPT-substituted results fail closed before edits.

## Scope — CodexHub adapter only

- Preserve `spawn_agent.agent_type=worker` across tool declaration, argument normalization, call execution, response normalization, and history replay.
- Reject unsupported `agent_type` values before child execution; do not delete, coerce, or silently substitute them.
- Consume the Host/runtime's supported effective agent type, model, and reasoning readback.
- Require effective readback to agree with the requested Worker selector, third-party model, and reasoning before edits begin.
- Fail closed for missing, unknown, contradictory, rejected, unsupported, or GPT-substituted readback/results.
- Emit sanitized, machine-classifiable telemetry for selector preservation/rejection and effective-binding validation outcomes.
- Add deterministic tests and a focused sanitized fixture derived only from shapes, enums, and synthetic values.

## Non-goals

- Do not register Host callbacks or implement parent-result delivery; #156 owns the Host/runtime surface.
- Do not implement the repeated identical empty-`tool_search` bound; #159 owns it.
- Do not use Codex SQLite, private Task/callback IDs, local paths, or rollout records as evidence.
- Do not broaden into unrelated transport, protocol routing, retry, apply-patch, or Task-activity work.
- Do not infer binding from requested settings and do not accept GPT substitution.

## Acceptance criteria

- [ ] `agent_type=worker` survives declaration, normalization, call execution, result normalization, and history replay unchanged.
- [ ] An unsupported selector is rejected with a sanitized terminal classification before child execution.
- [ ] Supported readback proves the effective agent type, model, and reasoning and matches the request before edits.
- [ ] Missing, unknown, contradictory, rejected, unsupported, or GPT-substituted readback/result stops before edits.
- [ ] Existing supported non-Worker collaboration behavior remains unchanged.
- [ ] Telemetry contains no credentials, prompts, private Task/callback identifiers, rollout data, or local paths.

## Minimal deterministic tests

1. Worker selector survives declaration, normalization, execution, response normalization, and history replay.
2. Missing and unsupported selector values reject before child execution.
3. Effective readback matching the requested Worker/model/reasoning passes.
4. Missing, unknown, contradictory, rejected, unsupported, and GPT-substituted readbacks each fail closed before edits.
5. Sanitized telemetry records only stable classification fields and synthetic fixture values.

## Verification

```powershell
python -m pytest -q tests/test_codex_semantic_adapter.py
python -m pytest -q tests/test_routing.py -k "agent_type or binding"
python -m pytest -q
git diff --check
python scripts/report_quality_gates.py
```

Run the full Python suite once at the candidate commit. `python scripts/report_quality_gates.py` is report-only.

## Expected hotset

- `src-python/codex_semantic_adapter.py`
- `src-python/codex_proxy.py` only where the selector/effective-binding result crosses the adapter boundary
- `tests/test_codex_semantic_adapter.py`
- focused routing tests for `agent_type`/binding
- one focused sanitized fixture under `tests/fixtures/` if required

## Relationships

- Parent Host/runtime gate: #156
- Sibling repeated-search bound: #159

## Execution contract

Execution-Contract: v2
Verification-Class: strict
Verification-Commands: targeted `tests/test_codex_semantic_adapter.py`; targeted routing tests for `agent_type`/binding; full Python suite once; `git diff --check`; `python scripts/report_quality_gates.py` report-only
Manual-Evidence: none; consume only supported effective readback supplied by the Host/runtime contract in #156
Architecture-Decision: resolved
Review-Owner: orchestrator
'@
$searchBody = ($searchBody -replace "`r`n","`n").TrimEnd()
$selectorBody = ($selectorBody -replace "`r`n","`n").TrimEnd()

$all = @(gh api --paginate 'repos/NOirBRight/CodexHub/issues?state=all&per_page=100' | ConvertFrom-Json | Where-Object { -not $_.pull_request })
$potentialSelectorDuplicates = @($all | Where-Object {
  $_.number -notin @(147,156,159) -and $_.title -cne $selectorTitle -and
  (($_.title + "`n" + $_.body) -match '(?is)agent_type\s*=\s*worker') -and
  (($_.title + "`n" + $_.body) -match '(?is)(effective.binding|binding.readback)') -and
  (($_.title + "`n" + $_.body) -match '(?is)(preserv|normaliz|codec|history)')
})
if ($potentialSelectorDuplicates.Count) {
  throw "possible selector/codec duplicate(s): $(@($potentialSelectorDuplicates.number) -join ',')"
}

function Find-OrCreateExactIssue([string]$Title,[string]$Body) {
  $matches = @($all | Where-Object title -CEQ $Title)
  if ($matches.Count -gt 1) { throw "duplicate exact-title Issues: $Title" }
  if ($matches.Count -eq 1) {
    if ($matches[0].state -ne 'open' -or (Get-TextHash $matches[0].body) -cne (Get-TextHash $Body)) {
      throw "existing exact-title Issue has conflicting state/body: $Title"
    }
    return [int]$matches[0].number
  }
  $bodyFile = Join-Path $env:TEMP ("wayfinder-" + [guid]::NewGuid().ToString('N') + '.md')
  [IO.File]::WriteAllText($bodyFile,$Body,[Text.UTF8Encoding]::new($false))
  try {
    $url = gh issue create --title $Title --body-file $bodyFile `
      --label bug --label ready-for-agent --label wayfinder:task `
      --milestone '0.1.6 — Codex control-plane reliability'
    return [int](($url | Select-Object -Last 1).TrimEnd('/') -split '/')[-1]
  } finally { Remove-Item -LiteralPath $bodyFile -ErrorAction SilentlyContinue }
}

$localSearchIssue = Find-OrCreateExactIssue $searchTitle $searchBody
$selectorBindingIssue = Find-OrCreateExactIssue $selectorTitle $selectorBody
"localSearchIssue=$localSearchIssue selectorBindingIssue=$selectorBindingIssue"
`````

Expected: exactly one open Issue exists for each exact title. A possible open or closed duplicate of the selector/codec outcome stops creation for review.

- [ ] **Step 4: Set exact metadata, parents, and dependencies idempotently**

Run:

```powershell
$milestone = @(gh api 'repos/NOirBRight/CodexHub/milestones?state=open&per_page=100' |
  ConvertFrom-Json | Where-Object title -CEQ '0.1.6 — Codex control-plane reliability')
if ($milestone.Count -ne 1) { throw '0.1.6 milestone lookup failed' }
foreach ($n in $localSearchIssue,$selectorBindingIssue) {
  @{labels=@('bug','ready-for-agent','wayfinder:task');milestone=[int]$milestone[0].number} |
    ConvertTo-Json -Compress | gh api --method PATCH "repos/NOirBRight/CodexHub/issues/$n" --input - | Out-Null
}
gh issue edit 156 --add-label wayfinder:grilling `
  --milestone '0.1.6 — Codex control-plane reliability' | Out-Null

function Get-Parent([int]$Child) {
  $json = gh api "repos/NOirBRight/CodexHub/issues/$Child/parent" 2>$null
  if ($LASTEXITCODE -eq 0) { return ($json | ConvertFrom-Json) }
  return $null
}
function Ensure-SubIssue([int]$Parent,[int]$Child) {
  $current = Get-Parent $Child
  if ($current -and $current.number -eq $Parent) { return }
  if ($current) { throw "#$Child already has parent #$($current.number)" }
  $childIssue = gh api "repos/NOirBRight/CodexHub/issues/$Child" | ConvertFrom-Json
  gh api --method POST "repos/NOirBRight/CodexHub/issues/$Parent/sub_issues" `
    -F "sub_issue_id=$($childIssue.id)" | Out-Null
}
function Ensure-BlockedBy([int]$Blocked,[int]$Blocker) {
  $existing = @(gh api "repos/NOirBRight/CodexHub/issues/$Blocked/dependencies/blocked_by" |
    ConvertFrom-Json | Where-Object number -eq $Blocker)
  if ($existing.Count -gt 1) { throw "duplicate dependency #$Blocked <- #$Blocker" }
  if ($existing.Count -eq 0) {
    $blocker = gh api "repos/NOirBRight/CodexHub/issues/$Blocker" | ConvertFrom-Json
    gh api --method POST "repos/NOirBRight/CodexHub/issues/$Blocked/dependencies/blocked_by" `
      -F "issue_id=$($blocker.id)" | Out-Null
  }
}

Ensure-SubIssue 147 156
Ensure-SubIssue 156 $localSearchIssue
Ensure-SubIssue 156 $selectorBindingIssue
Ensure-BlockedBy 156 $localSearchIssue
Ensure-BlockedBy 156 $selectorBindingIssue
Ensure-BlockedBy 64 156
```

Expected: #156 is a child of #147; both local Issues are children of #156; #156 is blocked by both local Issues; #64 retains #62/#63/#156.

- [ ] **Step 5: Read back the complete split exactly**

Run:

```powershell
foreach ($n in 156,$localSearchIssue,$selectorBindingIssue,64) {
  gh issue view $n --json number,title,state,body,labels,assignees,milestone,url
}
$host = gh issue view 156 --json state,labels,assignees,milestone | ConvertFrom-Json
$hostLabels = @($host.labels.name | Sort-Object)
if ($host.state -ne 'OPEN' -or $host.assignees.Count -ne 0 -or
    ($hostLabels -join ',') -cne 'bug,ready-for-human,wayfinder:grilling' -or
    $host.milestone.title -cne '0.1.6 — Codex control-plane reliability') { throw '#156 metadata mismatch' }
$children156 = @(gh api 'repos/NOirBRight/CodexHub/issues/156/sub_issues' | ConvertFrom-Json | ForEach-Object number | Sort-Object)
$blocked156 = @(gh api 'repos/NOirBRight/CodexHub/issues/156/dependencies/blocked_by' | ConvertFrom-Json | ForEach-Object number | Sort-Object)
$blocked64 = @(gh api 'repos/NOirBRight/CodexHub/issues/64/dependencies/blocked_by' | ConvertFrom-Json | ForEach-Object number | Sort-Object)
$expectedChildren = @($localSearchIssue,$selectorBindingIssue | Sort-Object)
if (($children156 -join ',') -cne ($expectedChildren -join ',')) { throw '#156 child set mismatch' }
if (($blocked156 -join ',') -cne ($expectedChildren -join ',')) { throw '#156 blocker set mismatch' }
if (($blocked64 -join ',') -cne '62,63,156') { throw '#64 blocker set mismatch' }
foreach ($n in $localSearchIssue,$selectorBindingIssue) {
  $issue = gh issue view $n --json state,labels,assignees,milestone | ConvertFrom-Json
  $labels = @($issue.labels.name | Sort-Object)
  if ($issue.state -ne 'OPEN' -or $issue.assignees.Count -ne 0 -or
      ($labels -join ',') -cne 'bug,ready-for-agent,wayfinder:task' -or
      $issue.milestone.title -cne '0.1.6 — Codex control-plane reliability') { throw "child metadata mismatch #$n" }
  $parent = Get-Parent $n
  if (-not $parent -or $parent.number -ne 156) { throw "child parent mismatch #$n" }
}
```

Expected: exact titles/bodies/metadata, hierarchy, and dependency sets; #156 is unassigned and `ready-for-human`; both children are unassigned and `ready-for-agent`; no Issue claim occurred.

---

### Task 4: Split #28 into client discovery performance and uninstall cleanup

**Files:**
- GitHub Issue: #28, rewritten in place for probe/catalog discovery performance.
- Create GitHub Issue with exact title: `Remove CodexHub-owned Windows autostart registration during uninstall`.

**Interfaces:**
- Consumes: milestones 0.1.9 and 0.1.10; runtime registration owner #111.
- Produces: #28 as a single-boundary 0.1.9 Issue and `$uninstallIssue` as a 0.1.10 Issue blocked by #111.

- [ ] **Step 1: Re-read #28 and preserve its reporter evidence**

Run:

```powershell
gh issue view 28 --comments `
  --json number,title,state,body,labels,assignees,milestone,comments,url
```

Expected: open enhancement with two mixed concerns: installer uninstall cleanup and probe/catalog discovery performance.

- [ ] **Step 2: Create the uninstall cleanup Issue if absent**

Run:

````powershell
$title = 'Remove CodexHub-owned Windows autostart registration during uninstall'
$matches = @(gh issue list --state all --limit 100 `
  --search 'in:title "Remove CodexHub-owned Windows autostart registration during uninstall"' `
  --json number,title,state,url `
  --jq ".[] | select(.title == \"$title\") | .number")
if ($matches.Count -gt 1) { throw 'duplicate uninstall-cleanup Issues' }

if ($matches.Count -eq 0) {
  $body = @'
Split from #28. Runtime registration/readback is owned by #111.

## Problem

CodexHub can create a Windows autostart registration, but the installer/uninstaller contract does not prove that uninstall removes only the CodexHub-owned registration. A stale registration can survive removal, point to a missing executable, or launch an unintended path after reinstall.

## Outcome

The Windows uninstall path removes the exact CodexHub-owned autostart registration using the same ownership identity defined by #111, while preserving unrelated scheduled tasks and user registrations.

## Scope

- Integrate ownership-verified autostart removal into the supported Windows uninstall flow.
- Treat missing registration as idempotent success.
- Detect mismatched owner/action/path and fail closed without deleting it.
- Cover normal/debug build flavors and supported installed-path replacement.
- Surface uninstall-log evidence without credentials or private path disclosure in user-facing output.

## Non-goals

- Do not redesign runtime enable/disable/readback; #111 owns that contract.
- Do not delete scheduled tasks by broad name, executable basename, or port.
- Do not change macOS/Linux startup behavior in this Windows Issue.

## Acceptance criteria

- [ ] Uninstall removes exactly one verified CodexHub-owned Windows autostart registration.
- [ ] Missing registration is an idempotent success.
- [ ] A mismatched registration is preserved and reported rather than deleted.
- [ ] Reinstall after uninstall creates one valid registration with no stale duplicate.
- [ ] Normal/debug and supported install replacement fixtures preserve the correct executable identity.
- [ ] A packaged Windows install→enable→uninstall smoke leaves no CodexHub-owned registration and does not change an unrelated control task.

## Verification

```powershell
Push-Location src-tauri
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
Pop-Location
git diff --check
```

Manual evidence: one packaged Windows install→enable→uninstall control with an unrelated scheduled-task fixture.

## Expected hotset

- `src-tauri/tauri.conf.json`
- Windows installer/NSIS hooks under `src-tauri/`
- Rust autostart ownership helpers and focused tests adjacent to #111

## Dependencies

- Blocked by #111.

## Execution contract

Execution-Contract: v2
Verification-Class: strict
Verification-Commands: affected Rust tests; full Rust test/clippy suites once; `git diff --check`
Manual-Evidence: packaged Windows install→enable→uninstall with unrelated-task preservation
Architecture-Decision: resolved
Review-Owner: orchestrator
'@
  $url = gh issue create --title $title --body $body `
    --label bug --label ready-for-agent --label wayfinder:task
  $uninstallIssue = [int]($url.TrimEnd('/') -split '/')[-1]
} else {
  $uninstallIssue = [int]$matches[0]
}
"uninstallIssue=$uninstallIssue"
````

Expected: exactly one open uninstall Issue; record its number as `$uninstallIssue`.

- [ ] **Step 3: Rewrite #28 as the discovery-performance Issue**

Run:

```powershell
$body28 = @"
The Windows uninstall-cleanup concern from the original report is now owned by #$uninstallIssue.

## Problem

Gateway client version probes and Provider/model catalog discovery remain partly serial, subprocess-heavy, and weakly cached. Startup and configuration flows can repeatedly launch known client probes or repeat unchanged discovery work, while invalidation and concurrency bounds are not explicit.

## Outcome

Make supported Gateway client version probes and Provider/catalog discovery bounded, cache-aware, and observable without changing their configuration semantics.

## Scope

- Inventory supported client version probes and catalog/model discovery calls.
- Bound subprocess concurrency and duration using the existing timeout/PATH safety from #13.
- Coalesce identical in-flight work.
- Cache only results with explicit keys, TTL/invalidation, and manual-refresh behavior.
- Preserve actionable failure classification and existing discovery output.

## Non-goals

- Do not change Windows uninstall behavior; #$uninstallIssue owns it.
- Do not add Provider presets, OAuth, automatic Provider enablement, or runtime Models.dev authority.
- Do not change Gateway routing, retry, or protocol translation.

## Acceptance criteria

- [ ] Identical concurrent probes/discovery requests share one bounded operation.
- [ ] A valid cache hit starts no subprocess or network discovery request.
- [ ] Manual refresh bypasses stale cached data but coalesces with an active live refresh.
- [ ] Timeout, malformed output, missing executable, cancellation, and application exit leave no child process running.
- [ ] Cache keys and invalidation distinguish client version, executable identity, Provider endpoint, auth reference, and relevant configuration revision without storing secrets.
- [ ] Existing model/catalog results and unsupported-client behavior remain unchanged.

## Verification

Run focused probe/catalog tests, then the full suite for every changed Rust, Python, or frontend boundary according to `docs/agents/verification-policy.md`. Run `git diff --check`; `python scripts/report_quality_gates.py` remains report-only.

## Expected hotset

- Gateway client probe/version-discovery Rust modules
- Provider/model catalog discovery commands and caches
- adjacent focused tests

## Relationships

- Split uninstall work: #$uninstallIssue
- Existing timeout/PATH hardening: #13
- Usage app-server singleflight uses a separate boundary: #150

## Execution contract

Execution-Contract: v2
Verification-Class: standard
Verification-Commands: targeted probe/catalog tests; affected component full suites once; `git diff --check`; report-only quality gate when source is in scan scope
Manual-Evidence: none
Architecture-Decision: resolved
Review-Owner: orchestrator
"@
$tmp = Join-Path $env:TEMP 'codexhub-issue-28-body.md'
Set-Content -Encoding utf8 $tmp $body28
gh issue edit 28 `
  --title 'Bound Gateway client probes and catalog discovery work' `
  --body-file $tmp `
  --add-label enhancement --add-label ready-for-agent --add-label wayfinder:task `
  --milestone '0.1.9 — Managed client reliability'
```

Expected: #28 owns only bounded probe/catalog discovery and links the exact uninstall Issue.

- [ ] **Step 4: Set hierarchy, milestone, and dependency**

Run:

```powershell
gh issue edit $uninstallIssue `
  --add-label bug --add-label ready-for-agent --add-label wayfinder:task `
  --milestone '0.1.10 — Existing product reliability'

function Ensure-SubIssue([int]$Parent, [int]$Child) {
  $parentJson = gh api "repos/NOirBRight/CodexHub/issues/$Child/parent" 2>$null
  if ($LASTEXITCODE -eq 0) {
    $current = $parentJson | ConvertFrom-Json
    if ($current.number -eq $Parent) { return }
    throw "#$Child already has parent #$($current.number)"
  }
  $childIssue = gh api "repos/NOirBRight/CodexHub/issues/$Child" | ConvertFrom-Json
  gh api --method POST "repos/NOirBRight/CodexHub/issues/$Parent/sub_issues" `
    -F "sub_issue_id=$($childIssue.id)" | Out-Null
}
function Ensure-BlockedBy([int]$Blocked, [int]$Blocker) {
  $existing = @(gh api "repos/NOirBRight/CodexHub/issues/$Blocked/dependencies/blocked_by" `
    --jq ".[] | select(.number == $Blocker) | .number")
  if ($existing.Count -eq 0) {
    $blockerId = gh api "repos/NOirBRight/CodexHub/issues/$Blocker" --jq .id
    gh api --method POST "repos/NOirBRight/CodexHub/issues/$Blocked/dependencies/blocked_by" `
      -F issue_id=$blockerId | Out-Null
  }
}
Ensure-SubIssue 147 28
Ensure-SubIssue 147 $uninstallIssue
Ensure-BlockedBy $uninstallIssue 111
```

Expected: #28 and the uninstall Issue are direct children of #147; the uninstall Issue is blocked by #111.

- [ ] **Step 5: Read back both contracts**

Run:

```powershell
gh issue view 28 --json number,title,state,body,labels,assignees,milestone,url
gh issue view $uninstallIssue --json number,title,state,body,labels,assignees,milestone,url
gh api "repos/NOirBRight/CodexHub/issues/$uninstallIssue/parent" --jq '{number,title,state}'
gh api "repos/NOirBRight/CodexHub/issues/$uninstallIssue/dependencies/blocked_by" `
  --jq '.[] | {number,title,state}'
```

Expected: both Issues have complete v2 contracts, exact milestones, one lifecycle label each, no assignee, correct parent, and #111 as the uninstall blocker.

---

### Task 5: Normalize milestone membership, Wayfinder labels, and hierarchy

**Files:**
- GitHub Issues and sub-issue relationships only.

**Interfaces:**
- Consumes: `$localSearchIssue`, `$selectorBindingIssue`, and `$uninstallIssue`, each discoverable by exact title.
- Produces: every active release Issue assigned to exactly one 0.1.6–0.1.10 milestone; every new-feature parent visible under #147 without an active reliability milestone; nested workstreams retain one parent.

- [ ] **Step 1: Rediscover the three newly created Issue numbers by exact title**

Run:

```powershell
function Find-ExactIssue([string]$Title) {
  $matches = @(gh issue list --state all --limit 200 --search "in:title `"$Title`"" `
    --json number,title,state `
    --jq ".[] | select(.title == \"$Title\") | .number")
  if ($matches.Count -ne 1) { throw "exact Issue lookup failed: $Title" }
  return [int]$matches[0]
}
$localSearchIssue = Find-ExactIssue 'Bound repeated empty tool_search misses for external models'
$selectorBindingIssue = Find-ExactIssue 'Preserve Worker selector and effective binding validation for external delegation'
$uninstallIssue = Find-ExactIssue 'Remove CodexHub-owned Windows autostart registration during uninstall'
```

Expected: three unique numeric Issue IDs.

- [ ] **Step 2: Assign exact milestone membership**

Run:

```powershell
$membership = [ordered]@{
  '0.1.6 — Codex control-plane reliability' = @(111,112,138,139,141,143,149,150,151,156,$localSearchIssue,$selectorBindingIssue)
  '0.1.7 — Official GPT reliability' = @(18,19,20,21,104,109,114,157)
  '0.1.8 — Third-party model certification' = @(17,22,57,58,59,61,62,63,64,65,66,67)
  '0.1.9 — Managed client reliability' = @(8,28,83,153,154,155)
  '0.1.10 — Existing product reliability' = @(86,87,88,113,115,126,$uninstallIssue)
}
foreach ($entry in $membership.GetEnumerator()) {
  foreach ($n in $entry.Value) {
    gh issue edit $n --milestone "$($entry.Name)" | Out-Null
  }
}
```

Expected: every listed open Issue reports the exact milestone; no Issue appears in two arrays.

- [ ] **Step 3: Add the approved Wayfinder type labels without changing lifecycle labels**

Run:

```powershell
$taskIssues = @(8,28,83,86,87,88,89,90,91,92,93,111,113,115,126,155,157,$localSearchIssue,$selectorBindingIssue,$uninstallIssue)
$researchIssues = @(68,104)
$grillingIssues = @(71,94,109,156)

foreach ($n in $taskIssues) { gh issue edit $n --add-label wayfinder:task | Out-Null }
foreach ($n in $researchIssues) { gh issue edit $n --add-label wayfinder:research | Out-Null }
foreach ($n in $grillingIssues) { gh issue edit $n --add-label wayfinder:grilling | Out-Null }
```

Expected: the added Wayfinder label matches task/research/grilling ownership; existing canonical lifecycle labels remain unchanged.

- [ ] **Step 4: Ensure the approved top-level map membership**

Run:

```powershell
function Ensure-SubIssue([int]$Parent, [int]$Child) {
  $parentJson = gh api "repos/NOirBRight/CodexHub/issues/$Child/parent" 2>$null
  if ($LASTEXITCODE -eq 0) {
    $current = $parentJson | ConvertFrom-Json
    if ($current.number -eq $Parent) { return }
    throw "#$Child already has parent #$($current.number)"
  }
  $childIssue = gh api "repos/NOirBRight/CodexHub/issues/$Child" | ConvertFrom-Json
  gh api --method POST "repos/NOirBRight/CodexHub/issues/$Parent/sub_issues" `
    -F "sub_issue_id=$($childIssue.id)" | Out-Null
}

$topLevel = @(
  111,112,138,139,141,143,149,150,151,156,
  18,19,20,21,104,109,114,157,
  17,22,57,58,59,61,
  8,28,83,153,154,155,
  86,87,88,113,115,126,$uninstallIssue,
  68,71,73,85,148,152
)
foreach ($n in $topLevel) { Ensure-SubIssue 147 $n }
Ensure-SubIssue 156 $localSearchIssue
Ensure-SubIssue 156 $selectorBindingIssue
```

Expected: every listed top-level Issue has parent #147; both local adapter children have parent #156. Existing nested #57 and #73 children are not moved.

- [ ] **Step 5: Ensure the Provider-auth and Claude workstream nesting**

Run:

```powershell
foreach ($n in 89,90,91,92,93,94) { Ensure-SubIssue 71 $n }
foreach ($n in 74,75,76,77,78) { Ensure-SubIssue 73 $n }
```

Expected: #89, #90, #91, #92, #93, and #94 have parent #71; #74, #75, #76, #77, and #78 retain or gain parent #73. Any pre-existing different parent fails closed rather than being silently replaced.

- [ ] **Step 6: Remove active reliability milestones from deferred new-feature work**

Run:

```powershell
$deferred = @(68,71,73,74,75,76,77,78,85,89,90,91,92,93,94,148,152)
foreach ($n in $deferred) {
  $issue = gh api "repos/NOirBRight/CodexHub/issues/$n" | ConvertFrom-Json
  if ($issue.milestone) { gh issue edit $n --remove-milestone | Out-Null }
}
```

Expected: deferred new-feature work has no 0.1.6–0.1.10 or legacy reliability milestone.

- [ ] **Step 7: Read back milestone coverage and hierarchy**

Run:

```powershell
foreach ($entry in $membership.GetEnumerator()) {
  foreach ($n in $entry.Value) {
    $issue = gh api "repos/NOirBRight/CodexHub/issues/$n" | ConvertFrom-Json
    if ($issue.milestone.title -ne $entry.Name) { throw "milestone mismatch #$n" }
  }
}

foreach ($n in $topLevel) {
  $parent = gh api "repos/NOirBRight/CodexHub/issues/$n/parent" --jq .number
  if ([int]$parent -ne 147) {
    throw "map parent mismatch #$n"
  }
}
foreach ($n in $localSearchIssue,$selectorBindingIssue) {
  $parent = gh api "repos/NOirBRight/CodexHub/issues/$n/parent" --jq .number
  if ([int]$parent -ne 156) { throw "#156 child mismatch #$n" }
}
'milestones-and-hierarchy-ok'
```

Expected: `milestones-and-hierarchy-ok`.

---

### Task 6: Reconcile stale Issue state and closure evidence

**Files:**
- GitHub Issues: #8, #10, #12, #62, #109, #74.
- Read-only Git history and native Codex Task list for #62 ownership.

**Interfaces:**
- Consumes: current `dev`/`main`, open PR list, and native Task state.
- Produces: durable comments for #8/#10/#12; an honest assignee state for #62; unchanged `needs-info` state for #109/#74.

- [ ] **Step 1: Record the current-branch correction on #8**

Run:

```powershell
function Normalize-IssueCommentBody([string]$Body) {
  return ($Body -replace "`r`n","`n").TrimEnd()
}
function Get-NormalizedBodySha256([string]$Body) {
  $normalized = Normalize-IssueCommentBody $Body
  $bytes = [Text.Encoding]::UTF8.GetBytes($normalized)
  return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
}
function Get-IssueComments([int]$Issue) {
  return @(gh api --paginate "repos/NOirBRight/CodexHub/issues/$Issue/comments?per_page=100" | ConvertFrom-Json)
}
function Write-ExactIssueComment([int]$Issue,[string]$PurposePrefix,[string]$Body) {
  $Body = Normalize-IssueCommentBody $Body
  $comments = @(gh api --paginate "repos/NOirBRight/CodexHub/issues/$Issue/comments?per_page=100" | ConvertFrom-Json)
  $exact = @($comments | Where-Object { (Normalize-IssueCommentBody $_.body) -ceq $Body })
  $purpose = @($comments | Where-Object { (Normalize-IssueCommentBody $_.body).StartsWith($PurposePrefix,[StringComparison]::Ordinal) })
  if ($exact.Count -gt 1) { throw "multiple exact comments on #$Issue" }
  if ($exact.Count -eq 1) {
    if ($purpose.Count -ne 1) { throw "conflicting same-purpose comment on #$Issue" }
  } else {
    if ($purpose.Count -gt 0) { throw "conflicting same-purpose comment on #$Issue" }
    gh issue comment $Issue --body $Body | Out-Null
  }
  $readback = @(gh api --paginate "repos/NOirBRight/CodexHub/issues/$Issue/comments?per_page=100" | ConvertFrom-Json)
  $purposeReadback = @($readback | Where-Object { (Normalize-IssueCommentBody $_.body).StartsWith($PurposePrefix,[StringComparison]::Ordinal) })
  $matches = @($readback | Where-Object { (Normalize-IssueCommentBody $_.body) -ceq $Body })
  if ($purposeReadback.Count -ne 1 -or $matches.Count -ne 1) { throw "exact create/no-op readback failed on #$Issue" }
  return [ordered]@{
    operation_decision = if ($exact.Count -eq 1) { 'exact-no-op' } else { 'create' }
    public_comment_id = [long]$matches[0].id
    normalized_body_sha256 = Get-NormalizedBodySha256 $Body
    prefix_multiplicity = $purposeReadback.Count
    exact_body_multiplicity = $matches.Count
    url = $matches[0].html_url
  }
}
function Set-ExactIssueComment(
  [int]$Issue,
  [string]$PurposePrefix,
  [string]$DesiredBody,
  [long]$ExpectedPriorPublicCommentId,
  [string]$ExpectedPriorNormalizedBodySha256
) {
  $DesiredBody = Normalize-IssueCommentBody $DesiredBody
  $comments = @(Get-IssueComments $Issue)
  $purpose = @($comments | Where-Object { (Normalize-IssueCommentBody $_.body).StartsWith($PurposePrefix,[StringComparison]::Ordinal) })
  if ($purpose.Count -ne 1) { throw "expected exactly one same-purpose comment on #$Issue" }
  $exact = @($comments | Where-Object { (Normalize-IssueCommentBody $_.body) -ceq $DesiredBody })
  if ($exact.Count -gt 1) { throw "multiple exact desired comments on #$Issue" }

  $selected = $purpose[0]
  $priorHash = Get-NormalizedBodySha256 ([string]$selected.body)
  if ($exact.Count -eq 1) {
    $operation = 'exact-no-op'
  } else {
    if ([long]$selected.id -ne $ExpectedPriorPublicCommentId) { throw "prior public comment ID mismatch on #$Issue" }
    if ($priorHash -cne $ExpectedPriorNormalizedBodySha256.ToLowerInvariant()) { throw "prior normalized-body SHA-256 mismatch on #$Issue" }
    @{body=$DesiredBody} | ConvertTo-Json -Compress |
      gh api --method PATCH "repos/NOirBRight/CodexHub/issues/comments/$ExpectedPriorPublicCommentId" --input - | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "checkpoint PATCH failed on #$Issue" }
    $operation = 'patch'
  }

  $readback = @(Get-IssueComments $Issue)
  $purposeReadback = @($readback | Where-Object { (Normalize-IssueCommentBody $_.body).StartsWith($PurposePrefix,[StringComparison]::Ordinal) })
  $exactReadback = @($readback | Where-Object { (Normalize-IssueCommentBody $_.body) -ceq $DesiredBody })
  if ($purposeReadback.Count -ne 1 -or $exactReadback.Count -ne 1) { throw "exact checkpoint update readback failed on #$Issue" }
  if ([long]$exactReadback[0].id -ne [long]$selected.id) { throw "checkpoint identity changed on #$Issue" }
  return [ordered]@{
    operation_decision = $operation
    prior_public_comment_id = [long]$selected.id
    prior_normalized_body_sha256 = $priorHash
    desired_normalized_body_sha256 = Get-NormalizedBodySha256 $DesiredBody
    post_public_comment_id = [long]$exactReadback[0].id
    post_prefix_multiplicity = $purposeReadback.Count
    post_exact_body_multiplicity = $exactReadback.Count
    url = $exactReadback[0].html_url
  }
}

git merge-base --is-ancestor 400d19bb dev
$ancestor = ($LASTEXITCODE -eq 0)
$moduleExists = Test-Path 'src-tauri/src/gateway/client_adapters.rs'
if ($ancestor -or $moduleExists) { throw 'The #8 correction assumptions changed; re-diagnose before commenting.' }

$body = @'
### Wayfinder baseline correction

The earlier local-completion comment does not describe the current `dev` baseline used by the replanned Wayfinder:

- commit `400d19bb` is not an ancestor of current `dev`;
- `src-tauri/src/gateway/client_adapters.rs` is absent from the current tree.

#8 therefore remains open and is scheduled under `0.1.9 — Managed client reliability`. This comment does not claim the orphaned implementation is reusable; a future Worker must compare it against current code before choosing reimplementation or recovery.
'@
Write-ExactIssueComment 8 '### Wayfinder baseline correction' $body
```

Expected: comment URL returned; Issue remains open, unassigned, and `ready-for-agent`.

- [ ] **Step 2: Add final ancestry evidence to closed #10 and #12**

Run:

```powershell
function Write-ExactIssueComment([int]$Issue,[string]$PurposePrefix,[string]$Body) {
  $Body = ($Body -replace "`r`n","`n").TrimEnd()
  $comments = @(gh api --paginate "repos/NOirBRight/CodexHub/issues/$Issue/comments?per_page=100" | ConvertFrom-Json)
  $exact = @($comments | Where-Object body -CEQ $Body)
  $purpose = @($comments | Where-Object { ([string]$_.body).StartsWith($PurposePrefix,[StringComparison]::Ordinal) })
  if ($exact.Count -gt 1) { throw "multiple exact comments on #$Issue" }
  if ($exact.Count -eq 1) {
    if ($purpose.Count -ne 1) { throw "conflicting same-purpose comment on #$Issue" }
  } else {
    if ($purpose.Count -gt 0) { throw "conflicting same-purpose comment on #$Issue" }
    gh issue comment $Issue --body $Body | Out-Null
  }
  $readback = @(gh api --paginate "repos/NOirBRight/CodexHub/issues/$Issue/comments?per_page=100" | ConvertFrom-Json)
  $matches = @($readback | Where-Object body -CEQ $Body)
  if ($matches.Count -ne 1) { throw "exact comment readback failed on #$Issue" }
  $matches[0].html_url
}

$checks = [ordered]@{10='7de67c51';12='77014e9'}
foreach ($entry in $checks.GetEnumerator()) {
  git merge-base --is-ancestor $entry.Value main
  if ($LASTEXITCODE -ne 0) { throw "#$($entry.Name) fix is not on main" }
  git merge-base --is-ancestor $entry.Value dev
  if ($LASTEXITCODE -ne 0) { throw "#$($entry.Name) fix is not on dev" }
  $state = gh issue view $entry.Name --json state --jq .state
  if ($state -ne 'CLOSED') { throw "#$($entry.Name) unexpectedly open" }
  $body = "Wayfinder closure reconciliation: fix commit ``$($entry.Value)`` is an ancestor of both current ``main`` and ``dev``. The Issue remains closed; this records the final evidence missing after the earlier post-tag reopen comment."
  Write-ExactIssueComment $entry.Name 'Wayfinder closure reconciliation:' $body
}
```

Expected: both Issues remain closed with one ancestry reconciliation comment each.

- [ ] **Step 3: Reconcile #62 execution ownership without guessing**

Run:

```powershell
gh issue view 62 --comments `
  --json number,title,state,labels,assignees,milestone,comments,url
gh pr list --state open --search 'head:codex/issue-62' `
  --json number,title,headRefName,baseRefName,url
```

Then use the native Codex task list and read APIs to look for a sidebar-visible #62 Worker. Do not use SQLite or filesystem inference.

Expected decision:

- if a real #62 Task is Active or has unintegrated durable work, keep the assignee and add no lifecycle comment;
- if no real #62 Task exists, no open PR exists, and no durable work owner exists, run:

```powershell
function Write-ExactIssueComment([int]$Issue,[string]$PurposePrefix,[string]$Body) {
  $Body = ($Body -replace "`r`n","`n").TrimEnd()
  $comments = @(gh api --paginate "repos/NOirBRight/CodexHub/issues/$Issue/comments?per_page=100" | ConvertFrom-Json)
  $exact = @($comments | Where-Object body -CEQ $Body)
  $purpose = @($comments | Where-Object { ([string]$_.body).StartsWith($PurposePrefix,[StringComparison]::Ordinal) })
  if ($exact.Count -gt 1) { throw "multiple exact comments on #$Issue" }
  if ($exact.Count -eq 1) {
    if ($purpose.Count -ne 1) { throw "conflicting same-purpose comment on #$Issue" }
  } else {
    if ($purpose.Count -gt 0) { throw "conflicting same-purpose comment on #$Issue" }
    gh issue comment $Issue --body $Body | Out-Null
  }
  $readback = @(gh api --paginate "repos/NOirBRight/CodexHub/issues/$Issue/comments?per_page=100" | ConvertFrom-Json)
  $matches = @($readback | Where-Object body -CEQ $Body)
  if ($matches.Count -ne 1) { throw "exact comment readback failed on #$Issue" }
  $matches[0].html_url
}

gh issue edit 62 --remove-assignee NOirBRight
$body = 'Wayfinder ownership reconciliation: no active sidebar Worker, open PR, or durable execution owner was found. The stale assignee is removed; #62 remains blocked by #61 and will return to the 0.1.8 frontier only after its native dependencies close.'
Write-ExactIssueComment 62 'Wayfinder ownership reconciliation:' $body
```

- if native Task state is unavailable or ambiguous, leave the assignee unchanged and stop this step with an explicit human reconciliation requirement.

- [ ] **Step 4: Assert #109 and #74 remain explicit needs-info gates**

Run:

```powershell
foreach ($n in 109,74) {
  $issue = gh issue view $n --json state,labels,assignees,url | ConvertFrom-Json
  $labels = @($issue.labels.name)
  if ($issue.state -ne 'OPEN' -or 'needs-info' -notin $labels) {
    throw "#$n no longer matches the approved needs-info design"
  }
}
'needs-info-gates-ok'
```

Expected: `needs-info-gates-ok`; do not promote, assign, close, or manufacture missing evidence.

---

### Task 7: Rewrite Wayfinder map #147

**Files:**
- GitHub Issue: #147 body.
- Temporary: `$env:TEMP\codexhub-wayfinder-map-body.md`.

**Interfaces:**
- Consumes: exact newly created Issue titles, milestone membership, hierarchy, dependencies, and approved design.
- Produces: one self-contained map body whose first active gate is 0.1.6 and whose lower tiers do not refill.

- [ ] **Step 1: Rediscover dynamic Issue numbers and re-read #147**

Run:

```powershell
function Find-ExactIssue([string]$Title) {
  $matches = @(gh issue list --state all --limit 200 --search "in:title `"$Title`"" `
    --json number,title,state `
    --jq ".[] | select(.title == \"$Title\") | .number")
  if ($matches.Count -ne 1) { throw "exact Issue lookup failed: $Title" }
  return [int]$matches[0]
}
$localSearchIssue = Find-ExactIssue 'Bound repeated empty tool_search misses for external models'
$selectorBindingIssue = Find-ExactIssue 'Preserve Worker selector and effective binding validation for external delegation'
$uninstallIssue = Find-ExactIssue 'Remove CodexHub-owned Windows autostart registration during uninstall'
gh issue view 147 --comments `
  --json number,title,state,body,labels,assignees,milestone,comments,url
```

Expected: #147 remains open, unassigned, and labeled `wayfinder:map` + `ready-for-human`; dynamic issue lookups are unique.

- [ ] **Step 2: Generate the complete approved map body**

Run:

````powershell
$mapBody = @"
## Destination

Make CodexHub reliable in this order: Codex internal execution control, Official GPT, individually certified third-party models, existing managed clients, then new features. GitHub Issue contracts and native dependencies are the execution source of truth. Updating this map never claims or dispatches a ticket.

## Priority gates

1. **Codex internal reliability** — control plane, Official GPT, then third-party model certification.
2. **Existing external-client reliability** — ZCode, OMP, OpenCode, and Pi.
3. **New features** — release channels, Provider onboarding/auth, Claude Code, Imagegen, and AgentProvider.

Only the active gate receives new work. Already-claimed lower-tier work may finish its bounded scope but does not refill. Credential/privacy exposure, data loss, irreversible migration, non-idempotent side effects, abnormal cost, and active-gate blockers may override this order.

## Execution policy

- Delegated implementation means a native **sidebar-visible Worker Task**, not a Hidden Subagent or Inline execution.
- Current eligible Workers are GPT-5.6 Terra/max and GPT-5.6 Luna/max after runtime binding and bidirectional receipt preflight.
- Models never substitute silently. A failed binding stops that lane.
- Third-party models enter the eligible Worker pool only after #156 and their model-specific certification pass.
- One Issue has one lane, one editor, and one isolated worktree. GitHub remains the durable state authority.

## 0.1.6 — Codex control-plane reliability

Lifecycle spine: #139 → #143 → #112.

Parallel gates:

- #156 — Host/runtime-only gate for sidebar-visible third-party Worker materialization, effective binding readback, bidirectional communication, receipt, explicit unsupported terminalization, and Active → Done visibility;
- #$localSearchIssue — CodexHub-owned bound for repeated empty `tool_search` misses;
- #$selectorBindingIssue — CodexHub-owned Worker selector preservation and effective binding validation;
- #141 — Desktop restart and Task disappearance first-failure evidence;
- #138 — auditable Task command output, diffs, progress, and terminal state;
- #150 — coalesced usage probes and app-server child cleanup;
- #149 — cross-language lock ownership and crash recovery;
- #151 — saved-workspace migration decision; default is no implicit import;
- #111 — verifiable Windows autostart registration.

Exit: one reconciled Gateway identity; reliable sidebar Worker materialization and Task communication; bounded stop/exit/restart; recoverable Task state; no silent Worker or model substitution.

## 0.1.7 — Official GPT reliability

- #114 — Official/external Responses disconnect differential;
- #18 → #19 → #20 — downstream disconnect classification, bounded SSE reader lifecycle, multi-line SSE events;
- #21 — global upstream concurrency limit;
- #104 — no stale unsupported GPT-5.2 picker fallback;
- #109 — paused first-party context-policy gate; it does not block unrelated Official reliability work;
- #157 — Official context cap cannot override third-party Task models.

Exit: Official GPT is the trustworthy control group for Task/Worker communication, tools, streaming/cancellation, model catalog, and context authority.

## 0.1.8 — Third-party model certification

Reliability: #17 and evidence-gated #22, consuming the completed 0.1.7 transport foundations.

Capability DAG:

```text
#61 → #62
       ├─→ #63 ─→ #64
       ├─→ #65
       └─→ #66
#63 + #64 + #65 + #66 → #67 → #58 → #59
```

#57 remains the parent Gate. Certification is keyed by Codex build + CodexHub version + provider + model + upstream format + route/codec + tool profile. Supported requires visible Worker/Task communication, Direct/Deferred/hosted tools, protocol/history/IDs/SSE, transport/retry/recovery, and model-specific context evidence. Unknown semantics fail closed.

## 0.1.9 — Managed client reliability

- #8 — recover or reimplement client-adapter modularization against current `dev`;
- #28 — bounded client probes and catalog discovery;
- #83 — separate persistence, post-save refresh, and endpoint-test failure state;
- #153 — transactional ZCode generation and crash recovery;
- #154 — faithful reasoning-level export;
- #155 — OMP YAML string-type safety.

Exit: ZCode, OMP, OpenCode, and Pi pass preview/apply/readback/restore with faithful model/reasoning identity, crash recovery, and no silent Official fallback.

## 0.1.10 — Existing product reliability

- #86, #87, #88 — pricing source, coverage, and Ollama reference estimates;
- #113 — atomic Vision Proxy enablement;
- #115 — privacy-safe diagnostic export;
- #126 — persistent Home status and Toast-only action feedback;
- #$uninstallIssue — ownership-safe Windows uninstall cleanup, blocked by #111.

No new product capability is added under this gate.

## 0.2.x+ — New features

These lanes do not refill until 0.1.6–0.1.10 gates complete:

1. Stable/Developer channel: #148 → #152.
2. Provider presets/auth: #71 with #89, #90, #91, #92, #93, and #94.
3. Claude Code downstream Messages client: #73 with #74, #75, #76, #77, and #78.
4. Imagegen compatibility: #68 after its authorization/security boundary is resolved.
5. AgentProvider: #85, separate from ModelProvider and the model Gateway.

## Decisions so far

- Reliability gates replace the former feature-first 0.1.6–0.3.0 ordering.
- Stable/Developer is one installation with shared state, but its implementation is deferred to the new-feature tier.
- Visible Worker, Hidden Subagent, and Inline are distinct execution surfaces and cannot substitute for one another.
- Terra/Luna Visible Workers implement current work; third-party models join only after certification.
- Provider/model support is granular and versioned; Responses endpoint naming is not compatibility evidence.
- #156 is the P0 Host/runtime-only Visible Worker gate; its local CodexHub children are #$localSearchIssue for bounded empty search and #$selectorBindingIssue for Worker selector/effective binding validation; #64 validates the full post-fix collaboration matrix.
- #8 remains open because its orphaned commit is absent from current `dev`.
- Every open Issue belongs to a gate, support/decision queue, or new-feature parking area.

## Current frontier

- Human P0 gate: #156.
- Ready local CodexHub candidates: #$localSearchIssue and #$selectorBindingIssue.
- Independent ready 0.1.6 foundations, subject to hotset ownership: #139, #149, #150, #111.
- #143 waits for #139; #112 waits for #143.
- Map publication does not claim any of these tickets.

## Fog / external blockers

- #156 still requires the Codex Host/runtime visible-Worker capability and effective binding readback.
- #141 must distinguish a CodexHub trigger from an upstream Desktop crash.
- #151 requires the maintainer migration-policy decision.
- #109 waits for consistent first-party context guidance.
- #74 waits for approved real-provider and remaining Messages semantic evidence and stays in the new-feature tier.

## Out of scope

- Hidden Subagent, `codex exec`, Background, shared-directory, or Inline substitution for sidebar-visible Workers.
- Silent model fallback or inferred effective binding.
- Side-by-side Stable/Developer installations or a revived live Beta channel.
- Big-bang Gateway rewrite, generic all-protocol IR, or wholesale Python-to-Rust migration.
- Production Chat or Messages routes before their evidence/decision gates.
- Automatic Task archival, Codex SQLite edits, or synthetic rollout state.
"@
$mapFile = Join-Path $env:TEMP 'codexhub-wayfinder-map-body.md'
Set-Content -Encoding utf8 $mapFile $mapBody
Get-Content -Raw $mapFile
````

Expected: complete body with real numeric dynamic Issue references, no placeholder text, no old GLM global binding, and no obsolete main→dev precondition.

- [ ] **Step 3: Update #147 body without changing ownership fields**

Run:

```powershell
gh issue edit 147 --body-file $mapFile
```

Expected: Issue URL returned; title, state, labels, assignees, and milestone remain unchanged.

- [ ] **Step 4: Read back and verify the map contract**

Run:

```powershell
$map = gh issue view 147 `
  --json number,title,state,body,labels,assignees,milestone,url | ConvertFrom-Json
$required = @(
  '## Destination','## Priority gates','## Execution policy',
  '## 0.1.6 — Codex control-plane reliability',
  '## 0.1.7 — Official GPT reliability',
  '## 0.1.8 — Third-party model certification',
  '## 0.1.9 — Managed client reliability',
  '## 0.1.10 — Existing product reliability',
  '## 0.2.x+ — New features','## Decisions so far',
  '## Current frontier','## Fog / external blockers','## Out of scope'
)
foreach ($heading in $required) {
  if (-not $map.body.Contains($heading)) { throw "map missing $heading" }
}
if ($map.body -match 'Every Visible Worker.*glm-5\.2|main result back into dev') {
  throw 'obsolete map contract survived'
}
if ($map.state -ne 'OPEN' -or $map.assignees.Count -ne 0) { throw 'map ownership changed' }
$labels = @($map.labels.name)
if ('wayfinder:map' -notin $labels -or 'ready-for-human' -notin $labels) { throw 'map labels changed' }
$map.url
```

Expected: #147 URL and no exception.

---

### Task 8: Close the legacy milestone, validate global state, and publish the frontier readback

**Files:**
- GitHub milestones and #147 comments only.
- Temporary: `$env:TEMP\codexhub-wayfinder-migration\final-issues-api.json`.
- Durable audit inputs/outputs: `scripts/generate_wayfinder_final_audit.py`, `docs/superpowers/reviews/wayfinder-frontier-ownership-audit-v1.json`, `docs/superpowers/reviews/wayfinder-checkpoint-update-v1.json`, and `docs/superpowers/reviews/wayfinder-final-audit-v1.json`.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: closed legacy milestone, exact lifecycle/milestone/hierarchy/dependency validation, a self-contained content-addressed audit package, and one durable #147 migration checkpoint with the unclaimed frontier plus the repository-relative frontier artifact path/SHA-256.

- [ ] **Step 1: Close the superseded legacy milestone only after reassignments**

Run:

```powershell
$legacy = gh api 'repos/NOirBRight/CodexHub/milestones?state=all&per_page=100' `
  --jq '.[] | select(.title == "Third-party model agentic reliability") | .number'
if (@($legacy).Count -ne 1) { throw 'legacy milestone lookup failed' }

$remaining = gh issue list --state open --limit 1000 `
  --milestone 'Third-party model agentic reliability' `
  --json number,title
if (($remaining | ConvertFrom-Json).Count -ne 0) { throw 'legacy milestone still has open Issues' }

gh api --method PATCH "repos/NOirBRight/CodexHub/milestones/$legacy" `
  -f state='closed' `
  -f description='Superseded on 2026-07-16 by the reliability-gated 0.1.6–0.1.10 milestones. Historical issue/decision context remains preserved; this milestone is no longer a scheduling pool.' | Out-Null
```

Expected: legacy milestone closes only when it has zero open Issues.

- [ ] **Step 2: Validate canonical lifecycle labels across every open Issue**

Run:

```powershell
$lifecycle = @('needs-triage','needs-info','ready-for-agent','ready-for-human','wontfix')
$issues = gh issue list --state open --limit 1000 `
  --json number,title,labels,assignees,milestone,url | ConvertFrom-Json
$errors = @()
foreach ($issue in $issues) {
  $names = @($issue.labels.name)
  $matches = @($lifecycle | Where-Object { $_ -in $names })
  if ($matches.Count -ne 1) {
    $errors += "#$($issue.number) lifecycle=$($matches -join ',')"
  }
}
if ($errors.Count) { throw ($errors -join '; ') }
"canonical-lifecycle-ok: $($issues.Count) open Issues"
```

Expected: every open Issue has exactly one canonical lifecycle label.

- [ ] **Step 3: Validate the five milestone memberships exactly**

Run:

```powershell
function Find-ExactIssue([string]$Title) {
  $matches = @(gh issue list --state all --limit 200 --search "in:title `"$Title`"" `
    --json number,title,state `
    --jq ".[] | select(.title == \"$Title\") | .number")
  if ($matches.Count -ne 1) { throw "exact Issue lookup failed: $Title" }
  return [int]$matches[0]
}
$localSearchIssue = Find-ExactIssue 'Bound repeated empty tool_search misses for external models'
$selectorBindingIssue = Find-ExactIssue 'Preserve Worker selector and effective binding validation for external delegation'
$uninstallIssue = Find-ExactIssue 'Remove CodexHub-owned Windows autostart registration during uninstall'
$expected = [ordered]@{
  '0.1.6 — Codex control-plane reliability' = @(111,112,138,139,141,143,149,150,151,156,$localSearchIssue,$selectorBindingIssue)
  '0.1.7 — Official GPT reliability' = @(18,19,20,21,104,109,114,157)
  '0.1.8 — Third-party model certification' = @(17,22,57,58,59,61,62,63,64,65,66,67)
  '0.1.9 — Managed client reliability' = @(8,28,83,153,154,155)
  '0.1.10 — Existing product reliability' = @(86,87,88,113,115,126,$uninstallIssue)
}
foreach ($entry in $expected.GetEnumerator()) {
  $actual = @(gh issue list --state open --limit 1000 --milestone "$($entry.Name)" `
    --json number --jq '.[].number' | ForEach-Object { [int]$_ } | Sort-Object)
  $wanted = @($entry.Value | Sort-Object)
  if (($actual -join ',') -ne ($wanted -join ',')) {
    throw "milestone membership mismatch: $($entry.Name); actual=$($actual -join ','); expected=$($wanted -join ',')"
  }
}
'milestone-membership-ok'
```

Expected: `milestone-membership-ok`.

- [ ] **Step 4: Validate critical hierarchy and dependency edges**

Run:

```powershell
function Assert-Parent([int]$Child,[int]$Parent) {
  $actual = gh api "repos/NOirBRight/CodexHub/issues/$Child/parent" --jq .number
  if ([int]$actual -ne $Parent) { throw "parent mismatch #$Child" }
}
function Assert-BlockedBy([int]$Blocked,[int]$Blocker) {
  $matches = @(gh api "repos/NOirBRight/CodexHub/issues/$Blocked/dependencies/blocked_by" `
    --jq ".[] | select(.number == $Blocker) | .number")
  if ($matches.Count -ne 1) { throw "dependency mismatch #$Blocked <- #$Blocker" }
}
Assert-Parent 156 147
Assert-Parent $localSearchIssue 156
Assert-Parent $selectorBindingIssue 156
Assert-Parent 64 57
Assert-Parent 71 147
Assert-Parent 73 147
foreach ($n in 89,90,91,92,93,94) { Assert-Parent $n 71 }
foreach ($n in 74,75,76,77,78) { Assert-Parent $n 73 }
Assert-BlockedBy 156 $localSearchIssue
Assert-BlockedBy 156 $selectorBindingIssue
Assert-BlockedBy 64 156
Assert-BlockedBy $uninstallIssue 111
$children156 = @(gh api 'repos/NOirBRight/CodexHub/issues/156/sub_issues' | ConvertFrom-Json | ForEach-Object number | Sort-Object)
$blocked156 = @(gh api 'repos/NOirBRight/CodexHub/issues/156/dependencies/blocked_by' | ConvertFrom-Json | ForEach-Object number | Sort-Object)
$blocked64 = @(gh api 'repos/NOirBRight/CodexHub/issues/64/dependencies/blocked_by' | ConvertFrom-Json | ForEach-Object number | Sort-Object)
$expectedLocal = @($localSearchIssue,$selectorBindingIssue | Sort-Object)
if (($children156 -join ',') -cne ($expectedLocal -join ',')) { throw '#156 child set mismatch' }
if (($blocked156 -join ',') -cne ($expectedLocal -join ',')) { throw '#156 dependency set mismatch' }
if (($blocked64 -join ',') -cne '62,63,156') { throw '#64 dependency set mismatch' }
'hierarchy-dependencies-ok'
```

Expected: `hierarchy-dependencies-ok`.

- [ ] **Step 5: Prove hotset ownership eligibility and compute the unclaimed 0.1.6 ready frontier**

First build the label/dependency candidate set with the following PowerShell. Then, for every candidate it prints, call native `codex_app__list_threads` twice: once with the exact Issue title and once with `Issue N`. Do not use SQLite. Preserve the actual sanitized tool output in one JSON object with top-level `captured_at`, `tool`, `limit`, and `records`; every record must contain `issue`, `kind`, `query`, `schemaVersion`, `threads`, and `unavailableHosts`. Write it to `$env:TEMP\codexhub-wayfinder-migration\frontier-task-evidence.json` only when every `threads` and `unavailableHosts` array is empty, so no Task ID can enter the file. If the native Task tool is unavailable, any host is unavailable, any matching Task exists, or the capture timestamp/schema/query is incomplete, stop with `NEEDS_CONTEXT` rather than guessing.

Run:

```powershell
$gate = gh issue list --state open --limit 1000 `
  --milestone '0.1.6 — Codex control-plane reliability' `
  --json number,title,body,labels,assignees,url | ConvertFrom-Json
$readyByNumber = @{}
foreach ($issue in $gate) {
  $labels = @($issue.labels.name)
  $blockers = @(gh api "repos/NOirBRight/CodexHub/issues/$($issue.number)/dependencies/blocked_by" | ConvertFrom-Json)
  if ('ready-for-agent' -in $labels -and $issue.assignees.Count -eq 0 -and $blockers.Count -eq 0) {
    $readyByNumber[$issue.number] = $issue
  }
}
$priorityOrder = @($localSearchIssue,$selectorBindingIssue,139,149,150,111,141,138,151,143,112,156)
$labelCandidates = @($priorityOrder | Where-Object { $readyByNumber.ContainsKey($_) } | ForEach-Object { $readyByNumber[$_] })
$labelCandidates | Select-Object number,title | Format-Table -AutoSize
```

After the native queries are saved, run:

```powershell
$taskEvidenceFile = Join-Path $env:TEMP 'codexhub-wayfinder-migration/frontier-task-evidence.json'
if (-not (Test-Path $taskEvidenceFile)) { throw 'NEEDS_CONTEXT: native Task evidence is missing' }
$artifactPath = 'docs/superpowers/reviews/wayfinder-frontier-ownership-audit-v1.json'
python scripts/generate_wayfinder_final_audit.py capture `
  --native-task-evidence $taskEvidenceFile `
  --output $artifactPath `
  --base-ref dev `
  --planned-path scripts/generate_wayfinder_final_audit.py `
  --planned-path docs/superpowers/plans/2026-07-16-wayfinder-github-migration.md
if ($LASTEXITCODE -ne 0) { throw 'frontier audit capture/self-validation failed closed' }

$audit = Get-Content -Raw $artifactPath | ConvertFrom-Json
if ($audit.schema_version -ne 1 -or $audit.artifact_kind -cne 'wayfinder-frontier-ownership-audit') {
  throw 'frontier audit schema mismatch'
}
if ($audit.native_task_capture.records.Count -ne (2 * $labelCandidates.Count)) {
  throw 'frontier native Task record count mismatch'
}
$frontier = @($audit.frontier | ForEach-Object {
  $number = [int]$_
  $match = @($labelCandidates | Where-Object number -eq $number)
  if ($match.Count -ne 1) { throw "frontier readback mismatch #$number" }
  $match[0]
})
$artifactSha256 = (Get-FileHash $artifactPath -Algorithm SHA256).Hash.ToLowerInvariant()
python scripts/generate_wayfinder_final_audit.py validate --artifact $artifactPath
if ($LASTEXITCODE -ne 0) { throw 'frontier artifact self-validation failed' }
$frontier | Select-Object number,title,url | Format-Table -AutoSize
"frontier-artifact=$artifactPath sha256=$artifactSha256"
```

Expected: the generator validates all twelve exact query records, parses/persists every candidate's normalized Expected hotset, enumerates revisions/cleanliness/file sets for every worktree/local branch/open PR head, computes per-candidate intersections without inferring ownership from branch names, and records label/dependency eligibility separately from ownership/hotset eligibility. The current planning worktree may be `migration-control` only with zero product-hotset intersections; `dev` must be audited clean rather than trusted. If live state remains unchanged, frontier order is `$localSearchIssue`, `$selectorBindingIssue`, #139, #149, #150, #111. Do not assign or dispatch any candidate.

- [ ] **Step 6: Publish one migration checkpoint comment on #147**

Run:

```powershell
function Normalize-IssueCommentBody([string]$Body) {
  return ($Body -replace "`r`n","`n").TrimEnd()
}
function Get-NormalizedBodySha256([string]$Body) {
  $bytes = [Text.Encoding]::UTF8.GetBytes((Normalize-IssueCommentBody $Body))
  return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
}
function Get-IssueComments([int]$Issue) {
  return @(gh api --paginate "repos/NOirBRight/CodexHub/issues/$Issue/comments?per_page=100" | ConvertFrom-Json)
}
function New-IssueComment([int]$Issue,[string]$Body) {
  gh issue comment $Issue --body $Body | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "checkpoint create failed on #$Issue" }
}
function Update-IssueComment([long]$PublicCommentId,[string]$Body) {
  @{body=$Body} | ConvertTo-Json -Compress |
    gh api --method PATCH "repos/NOirBRight/CodexHub/issues/comments/$PublicCommentId" --input - | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "checkpoint PATCH failed for comment $PublicCommentId" }
}
function Set-OrCreateExactIssueComment(
  [int]$Issue,
  [string]$PurposePrefix,
  [string]$DesiredBody,
  [long]$ExpectedPriorPublicCommentId,
  [string]$ExpectedPriorNormalizedBodySha256
) {
  $DesiredBody = Normalize-IssueCommentBody $DesiredBody
  $comments = @(Get-IssueComments $Issue)
  $priorReadbackAt = (Get-Date).ToUniversalTime().ToString('o')
  $purpose = @($comments | Where-Object { (Normalize-IssueCommentBody $_.body).StartsWith($PurposePrefix,[StringComparison]::Ordinal) })
  $exact = @($comments | Where-Object { (Normalize-IssueCommentBody $_.body) -ceq $DesiredBody })
  if ($purpose.Count -gt 1 -or $exact.Count -gt 1) { throw "multiple checkpoint comments on #$Issue" }

  $priorId = $null
  $priorHash = $null
  if ($purpose.Count -eq 0) {
    if ($exact.Count -ne 0) { throw "exact checkpoint lacks purpose prefix on #$Issue" }
    New-IssueComment $Issue $DesiredBody
    $operation = 'create'
    $priorReadbackAt = $null
  } else {
    $selected = $purpose[0]
    $priorId = [long]$selected.id
    $priorHash = Get-NormalizedBodySha256 ([string]$selected.body)
    if ($exact.Count -eq 1) {
      if ([long]$exact[0].id -ne $priorId) { throw "checkpoint exact-body identity mismatch on #$Issue" }
      $operation = 'exact-no-op'
    } else {
      if ($priorId -ne $ExpectedPriorPublicCommentId) { throw "prior public comment ID mismatch on #$Issue" }
      if ($priorHash -cne $ExpectedPriorNormalizedBodySha256.ToLowerInvariant()) { throw "prior normalized-body SHA-256 mismatch on #$Issue" }
      Update-IssueComment $priorId $DesiredBody
      $operation = 'patch'
    }
  }

  $readback = @(Get-IssueComments $Issue)
  $postReadbackAt = (Get-Date).ToUniversalTime().ToString('o')
  $purposeReadback = @($readback | Where-Object { (Normalize-IssueCommentBody $_.body).StartsWith($PurposePrefix,[StringComparison]::Ordinal) })
  $exactReadback = @($readback | Where-Object { (Normalize-IssueCommentBody $_.body) -ceq $DesiredBody })
  if ($purposeReadback.Count -ne 1 -or $exactReadback.Count -ne 1) { throw "exact checkpoint readback failed on #$Issue" }
  $postId = [long]$exactReadback[0].id
  if ($null -ne $priorId -and $postId -ne $priorId) { throw "checkpoint identity changed on #$Issue" }
  return [ordered]@{
    operation_decision = $operation
    prior_public_comment_id = $priorId
    prior_normalized_body_sha256 = $priorHash
    prior_readback_at = $priorReadbackAt
    desired_normalized_body_sha256 = Get-NormalizedBodySha256 $DesiredBody
    post_public_comment_id = $postId
    post_normalized_body_sha256 = Get-NormalizedBodySha256 ([string]$exactReadback[0].body)
    post_prefix_multiplicity = $purposeReadback.Count
    post_exact_body_multiplicity = $exactReadback.Count
    post_readback_at = $postReadbackAt
    url = $exactReadback[0].html_url
  }
}
function Resolve-ProvenCheckpointHistoricalProvenance(
  [System.Collections.IDictionary]$Guard,
  [string]$DesiredBody,
  [System.Collections.IDictionary]$PriorTranscript,
  [string]$HistoricalOriginalBody
) {
  $DesiredBody = Normalize-IssueCommentBody $DesiredBody
  $desiredHash = Get-NormalizedBodySha256 $DesiredBody
  if ($null -ne $PriorTranscript) {
    $priorHistorical = $PriorTranscript.historical_original
    $priorHistoricalBody = Normalize-IssueCommentBody ([string]$priorHistorical.normalized_body)
    if ([string]$priorHistorical.normalized_body_sha256 -cne (Get-NormalizedBodySha256 $priorHistoricalBody)) {
      throw 'tracked checkpoint transcript historical origin hash mismatch'
    }
    if ([long]$PriorTranscript.post_readback.public_comment_id -ne [long]$Guard.prior_public_comment_id -or
        [string]$PriorTranscript.post_readback.normalized_body_sha256 -cne [string]$Guard.prior_normalized_body_sha256) {
      throw 'tracked checkpoint transcript does not prove the current prior readback'
    }
    return [ordered]@{
      historical_original = [ordered]@{
        source = [string]$priorHistorical.source
        normalized_body = $priorHistoricalBody
        normalized_body_sha256 = [string]$priorHistorical.normalized_body_sha256
      }
      proof_kind = 'tracked-transcript'
      prior_transcript = $PriorTranscript
    }
  }

  if ($Guard.operation_decision -ceq 'create') {
    return [ordered]@{
      historical_original = [ordered]@{
        source = 'Task 8 initial publication desired body'
        normalized_body = $DesiredBody
        normalized_body_sha256 = $desiredHash
      }
      proof_kind = 'initial-create'
      prior_transcript = $null
    }
  }
  if ($Guard.operation_decision -ceq 'exact-no-op') {
    if ([string]$Guard.prior_normalized_body_sha256 -cne $desiredHash -or
        [string]$Guard.post_normalized_body_sha256 -cne $desiredHash) {
      throw 'exact-no-op pre/desired/post normalized hashes differ'
    }
    return [ordered]@{
      historical_original = [ordered]@{
        source = 'Task 8 live exact desired body'
        normalized_body = $DesiredBody
        normalized_body_sha256 = $desiredHash
      }
      proof_kind = 'live-exact-desired'
      prior_transcript = $null
    }
  }
  if ($Guard.operation_decision -cne 'patch') { throw 'unsupported checkpoint operation decision' }
  $historicalBody = Normalize-IssueCommentBody $HistoricalOriginalBody
  return [ordered]@{
    historical_original = [ordered]@{
      source = 'Task 8 execution report exact original published body'
      normalized_body = $historicalBody
      normalized_body_sha256 = Get-NormalizedBodySha256 $historicalBody
    }
    proof_kind = 'task8-execution-report'
    prior_transcript = $null
  }
}
function New-CheckpointTranscript(
  [System.Collections.IDictionary]$Guard,
  [System.Collections.IDictionary]$ProvenHistoricalProvenance,
  [string]$DesiredBody,
  [string]$ArtifactPath,
  [string]$ArtifactSha256
) {
  $DesiredBody = Normalize-IssueCommentBody $DesiredBody
  $original = $ProvenHistoricalProvenance.historical_original
  $originalBody = Normalize-IssueCommentBody ([string]$original.normalized_body)
  if ([string]$original.normalized_body_sha256 -cne (Get-NormalizedBodySha256 $originalBody)) {
    throw 'proven historical origin hash mismatch'
  }
  if ($Guard.operation_decision -ceq 'exact-no-op') {
    $hashes = @(@(
      [string]$Guard.prior_normalized_body_sha256,
      [string]$Guard.desired_normalized_body_sha256,
      [string]$Guard.post_normalized_body_sha256
    ) | Select-Object -Unique)
    if ($hashes.Count -ne 1) { throw 'exact-no-op pre/desired/post normalized hashes differ' }
  }
  return [ordered]@{
    schema_version = 1
    captured_at = (Get-Date).ToUniversalTime().ToString('o')
    issue = 147
    purpose_prefix = '## Reliability-gated Wayfinder migration readback'
    historical_original = [ordered]@{
      source = [string]$original.source
      normalized_body = $originalBody
      normalized_body_sha256 = [string]$original.normalized_body_sha256
    }
    historical_provenance = [ordered]@{
      proof_kind = [string]$ProvenHistoricalProvenance.proof_kind
      prior_transcript = $ProvenHistoricalProvenance.prior_transcript
    }
    pre_update = [ordered]@{
      public_comment_id = $Guard.prior_public_comment_id
      normalized_body_sha256 = $Guard.prior_normalized_body_sha256
      readback_at = $Guard.prior_readback_at
    }
    desired = [ordered]@{
      normalized_body = $DesiredBody
      normalized_body_sha256 = $Guard.desired_normalized_body_sha256
      frontier_artifact_path = $ArtifactPath
      frontier_artifact_sha256 = $ArtifactSha256
    }
    operation_decision = $Guard.operation_decision
    post_readback = [ordered]@{
      public_comment_id = $Guard.post_public_comment_id
      normalized_body_sha256 = $Guard.post_normalized_body_sha256
      prefix_multiplicity = $Guard.post_prefix_multiplicity
      exact_body_multiplicity = $Guard.post_exact_body_multiplicity
      readback_at = $Guard.post_readback_at
    }
  }
}

$frontierText = (($frontier | ForEach-Object { "#$($_.number) $($_.title)" }) -join "`n- ")
$comment = @"
## Reliability-gated Wayfinder migration readback

The approved roadmap migration is complete and read back:

- five active reliability milestones exist with exact membership;
- the legacy cross-version reliability milestone is closed as superseded;
- #156 is the ready-for-human Host/runtime-only Visible Worker gate, #$localSearchIssue is its ready bounded-search child, and #$selectorBindingIssue is its ready selector/effective-binding validation child;
- #64 is blocked by #156 for the full post-fix collaboration matrix;
- #28 is narrowed to client discovery performance and #$uninstallIssue owns uninstall cleanup behind #111;
- every open Issue has exactly one canonical lifecycle label;
- no ticket was assigned or dispatched by the migration.

Current unclaimed 0.1.6 ready frontier:

- $frontierText

Sanitized native Task/worktree/branch/PR/assignee/hotset preflight found no active ownership conflict at publication.

Durable frontier audit: $artifactPath (SHA-256: ``$artifactSha256``).

Frontier order remains subject to native dependencies and hotset ownership. Map publication is not a claim.
"@
$purposePrefix = '## Reliability-gated Wayfinder migration readback'
$transcriptPath = 'docs/superpowers/reviews/wayfinder-checkpoint-update-v1.json'
$priorTranscript = if (Test-Path $transcriptPath) {
  Get-Content -Raw $transcriptPath | ConvertFrom-Json -AsHashtable
} else { $null }
$guard = Set-OrCreateExactIssueComment 147 $purposePrefix $comment `
  4994927680 'b6d4d1e9bc58fd013c4968eb2799990fb7afa579572ade93534ee657c35ad82b'

$historicalOriginalBody = @'
## Reliability-gated Wayfinder migration readback

The approved roadmap migration is complete and read back:

- five active reliability milestones exist with exact membership;
- the legacy cross-version reliability milestone is closed as superseded;
- #156 is the ready-for-human Visible Worker gate and #159 is its ready local bounded-search child;
- #64 is blocked by #156 for the full post-fix collaboration matrix;
- #28 is narrowed to client discovery performance and #160 owns uninstall cleanup behind #111;
- every open Issue has exactly one canonical lifecycle label;
- no ticket was assigned or dispatched by the migration.

Current unclaimed 0.1.6 ready frontier:

- #159 Bound repeated empty tool_search misses for external models
- #139 Serialize Gateway lifecycle operations and prevent duplicate startup processes
- #149 Make Rust/Python atomic-file lock ownership and stale recovery safe
- #150 Coalesce automatic OpenAI usage probes and avoid repeated Codex app-server cold starts
- #111 Make Windows app autostart registration verifiable and reliable

Frontier order remains subject to native dependencies and hotset ownership. Map publication is not a claim.
'@
$provenance = Resolve-ProvenCheckpointHistoricalProvenance $guard $comment $priorTranscript $historicalOriginalBody
$transcript = New-CheckpointTranscript $guard $provenance $comment $artifactPath $artifactSha256
$transcript | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8NoBOM $transcriptPath
python scripts/generate_wayfinder_final_audit.py finalize `
  --core $artifactPath `
  --transcript $transcriptPath `
  --output docs/superpowers/reviews/wayfinder-final-audit-v1.json
if ($LASTEXITCODE -ne 0) { throw 'final Wayfinder audit package validation failed' }
$guard.url
```

Expected: initial publication remains create-only and guarded; an existing checkpoint is either an exact no-op or a PATCH of only public comment `4994927680` after both its public ID and normalized prior-body SHA-256 match. Readback leaves exactly one same-purpose prefix and one exact desired body. The durable transcript and final self-contained artifact validate before the comment URL is returned.

- [ ] **Step 7: Perform the final no-claim readback**

Run:

```powershell
gh issue view 147 --comments `
  --json number,title,state,labels,assignees,body,comments,url
$comments = @(gh api --paginate 'repos/NOirBRight/CodexHub/issues/147/comments?per_page=100' | ConvertFrom-Json)
$expectedComment = ($comment -replace "`r`n","`n").TrimEnd()
$prefixMatches = @($comments | Where-Object { ([string]$_.body).StartsWith('## Reliability-gated Wayfinder migration readback',[StringComparison]::Ordinal) })
$exactMatches = @($comments | Where-Object body -CEQ $expectedComment)
if ($prefixMatches.Count -ne 1 -or $exactMatches.Count -ne 1) { throw 'checkpoint multiplicity/body mismatch' }
$prs = @(gh pr list --state open --limit 100 `
  --json number,title,headRefName,baseRefName,url | ConvertFrom-Json)
if (@($prs | Where-Object { $_.headRefName -match '(?i)wayfinder|issue[-_/]?(159|161|139|149|150|111)' }).Count) {
  throw 'migration/candidate PR exists'
}
foreach ($path in @(
  'docs/superpowers/reviews/wayfinder-frontier-ownership-audit-v1.json',
  'docs/superpowers/reviews/wayfinder-checkpoint-update-v1.json',
  'docs/superpowers/reviews/wayfinder-final-audit-v1.json'
)) {
  if (-not (Test-Path $path)) { throw "durable audit artifact missing: $path" }
}
python scripts/generate_wayfinder_final_audit.py validate `
  --artifact docs/superpowers/reviews/wayfinder-final-audit-v1.json `
  --live
if ($LASTEXITCODE -ne 0) { throw 'fresh live final audit failed' }
git status --short --branch
```

Expected:

- #147 is open, unassigned, and contains the migration checkpoint;
- exactly one checkpoint prefix and exact body remain;
- #159 and the selector/binding child are present in the exact frontier and ownership-evidence readback;
- no migration-created product PR exists;
- no product Issue was assigned by this plan;
- repository working tree contains only the intended planning generator/docs/audit changes before their scoped commit, and is clean after commit.

---

## Self-review checklist

Before execution handoff, verify this plan against the approved design:

1. **Spec coverage:** Tasks 2–8 cover milestones, the full Host-only #156 rewrite, both CodexHub children, #28 split, all open-Issue routing, stale state, #147 rewrite, dependencies, readback, and ownership-backed frontier recomputation.
2. **No placeholders:** Dynamic new Issue numbers are discovered by exact titles and stored in variables; open and closed selector/codec duplicates are checked before creation; no guessed number is embedded.
3. **Type consistency:** `$localSearchIssue`, `$selectorBindingIssue`, `$uninstallIssue`, milestone titles, parent relationships, and dependency direction are identical in every task.
4. **Scope:** The plan changes only GitHub planning state. Every product Issue receives its own later spec/plan/implementation cycle.
5. **No hidden claim:** No command assigns an Issue, creates a product branch/worktree, starts a Worker, opens a production PR, or edits product code.
6. **Retry safety:** Every Task 6 comment and initial Task 8 publication uses the create/no-op exact-body/same-purpose-prefix guard. Every Task 8 checkpoint update uses `Set-ExactIssueComment`, optimistic concurrency on the expected public comment ID plus normalized prior-body SHA-256, a single-comment PATCH, and exact post-readback multiplicities.
7. **Ownership proof:** Task 8's tracked read-only generator validates twelve timestamped/schema-versioned native queries, parses normalized Expected hotsets, enumerates every worktree/local branch/open PR head with revisions and changed-file sets versus `dev`, computes file-based intersections without branch-name inference, keeps label/dependency and ownership/hotset eligibility separate, and stops fail-closed on incomplete evidence or conflicts.
8. **Review blockers resolved:** Task 1 safely fast-forwards only a clean stale planning branch with no unique commits, requires the design and plan in-tree, emits the full read-only write-set preview, and stops for explicit authorization; Tasks 2–8 use Inline Execution only.
