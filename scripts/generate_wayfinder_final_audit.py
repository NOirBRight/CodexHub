#!/usr/bin/env python3
"""Generate and validate sanitized Wayfinder migration audit artifacts.

This utility is read-only with respect to GitHub and Git. ``capture`` consumes a
native ``codex_app__list_threads`` JSON capture supplied by the caller, performs
fresh GET/list operations, and writes the repository artifact named by
``--output``. ``finalize`` combines that immutable frontier artifact with a
guarded checkpoint-update transcript. ``validate`` performs offline structural
and hash validation; add ``--live`` for a fresh read-only GitHub/Git comparison.

The utility never reads Codex SQLite data, persists native Task/thread IDs, or
persists local absolute paths. It fails closed on incomplete native evidence,
unavailable hosts, Task matches, or active file/hotset intersections.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import time
from typing import Any, Iterable


ROOT = pathlib.Path(__file__).resolve().parents[1]
REPOSITORY = "NOirBRight/CodexHub"
SCHEMA_VERSION = 1
CORE_KIND = "wayfinder-frontier-ownership-audit"
FINAL_KIND = "wayfinder-final-migration-audit"
FRONTIER_EXPECTED = [159, 161, 139, 149, 150, 111]
PRIORITY_ORDER = [159, 161, 139, 149, 150, 111, 141, 138, 151, 143, 112, 156]
EXPECTED_TITLES = {
    159: "Bound repeated empty tool_search misses for external models",
    161: "Preserve Worker selector and effective binding validation for external delegation",
    139: "Serialize Gateway lifecycle operations and prevent duplicate startup processes",
    149: "Make Rust/Python atomic-file lock ownership and stale recovery safe",
    150: "Coalesce automatic OpenAI usage probes and avoid repeated Codex app-server cold starts",
    111: "Make Windows app autostart registration verifiable and reliable",
}
LIFECYCLE_LABELS = {
    "needs-triage",
    "needs-info",
    "ready-for-agent",
    "ready-for-human",
    "wontfix",
}
EXPECTED_MEMBERSHIP = {
    "0.1.6 — Codex control-plane reliability": [
        111,
        112,
        138,
        139,
        141,
        143,
        149,
        150,
        151,
        156,
        159,
        161,
    ],
    "0.1.7 — Official GPT reliability": [18, 19, 20, 21, 104, 109, 114, 157],
    "0.1.8 — Third-party model certification": [
        17,
        22,
        57,
        58,
        59,
        61,
        62,
        63,
        64,
        65,
        66,
        67,
    ],
    "0.1.9 — Managed client reliability": [8, 28, 83, 153, 154, 155],
    "0.1.10 — Existing product reliability": [86, 87, 88, 113, 115, 126, 160],
}
COMMENT_PREFIXES = {
    8: "### Wayfinder baseline correction",
    10: "Wayfinder closure reconciliation:",
    12: "Wayfinder closure reconciliation:",
    62: "Wayfinder ownership reconciliation:",
    147: "## Reliability-gated Wayfinder migration readback",
}
CHECKPOINT_COMMENT_ID = 4994927680
CORE_ARTIFACT_PATH = (
    "docs/superpowers/reviews/wayfinder-frontier-ownership-audit-v1.json"
)
TRANSCRIPT_ARTIFACT_PATH = (
    "docs/superpowers/reviews/wayfinder-checkpoint-update-v1.json"
)
FINAL_ARTIFACT_PATH = "docs/superpowers/reviews/wayfinder-final-audit-v1.json"

ORIGINAL_CHECKPOINT_BODY = """## Reliability-gated Wayfinder migration readback

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

Frontier order remains subject to native dependencies and hotset ownership. Map publication is not a claim."""


def run(*args: str, cwd: pathlib.Path | None = None, check: bool = True) -> str:
    attempts = 3 if args and args[0] == "gh" else 1
    result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(attempts):
        result = subprocess.run(
            args,
            cwd=cwd or ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
        )
        if result.returncode == 0 or attempt == attempts - 1:
            break
        time.sleep(1 + attempt)
    assert result is not None
    if check and result.returncode:
        raise RuntimeError(
            f"read-only command failed ({' '.join(args)}): {result.stderr.strip()}"
        )
    return result.stdout


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_body(value: str | None) -> str:
    return (value or "").replace("\r\n", "\n").rstrip()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def serialized_json_sha256(value: Any) -> str:
    """Hash the exact UTF-8 bytes emitted by :func:`write_json`."""
    encoded = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} capture timestamp missing")
    candidate = value.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        raise ValueError(f"{field} capture timestamp must include timezone")
    return value


def api_json(path: str) -> Any:
    return json.loads(run("gh", "api", path))


def api_pages(path: str) -> list[dict[str, Any]]:
    joiner = "&" if "?" in path else "?"
    values: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = api_json(f"{path}{joiner}per_page=100&page={page}")
        if not isinstance(batch, list):
            raise ValueError(f"expected list from GitHub endpoint: {path}")
        values.extend(batch)
        if len(batch) < 100:
            return values
        page += 1


def issue_parent(number: int) -> int | None:
    output = run(
        "gh",
        "api",
        f"repos/{REPOSITORY}/issues/{number}/parent",
        check=False,
    )
    if not output.strip():
        return None
    value = json.loads(output)
    if value.get("status") == "404" or value.get("message") == "No parent issue found":
        return None
    return int(value["number"])


def blockers(number: int) -> list[int]:
    return sorted(
        int(item["number"])
        for item in api_pages(
            f"repos/{REPOSITORY}/issues/{number}/dependencies/blocked_by"
        )
    )


def children(number: int) -> list[int]:
    return sorted(
        int(item["number"])
        for item in api_pages(f"repos/{REPOSITORY}/issues/{number}/sub_issues")
    )


def issue_snapshot(number: int, include_hotset: bool = False) -> dict[str, Any]:
    raw = api_json(f"repos/{REPOSITORY}/issues/{number}")
    body = normalize_body(raw.get("body"))
    snapshot: dict[str, Any] = {
        "number": number,
        "title": raw["title"],
        "state": raw["state"].lower(),
        "body_normalized_sha256": sha256_text(body),
        "labels": sorted(label["name"] for label in raw["labels"]),
        "assignees": sorted(value["login"] for value in raw["assignees"]),
        "milestone": raw["milestone"]["title"] if raw["milestone"] else None,
        "parent": issue_parent(number),
        "blocked_by": blockers(number),
        "children": children(number),
        "url": raw["html_url"],
    }
    if include_hotset:
        snapshot["expected_hotset"] = parse_expected_hotset(body, number)
    return snapshot


def _normalize_hotset_entry(raw: str) -> str:
    value = raw.strip()
    value = re.sub(r"^[-*]\s+", "", value)
    value = value.replace("`", "")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _path_matchers(entry: str, number: int) -> list[str]:
    candidates = re.findall(
        r"(?<![\w.-])((?:src-python|src-tauri|frontend|tests|scripts|docs)/[A-Za-z0-9_./*-]+)",
        entry,
    )
    matchers: list[str] = []
    for candidate in candidates:
        candidate = candidate.rstrip(".,;:)")
        if candidate.endswith("/"):
            candidate += "**"
        matchers.append(candidate)

    lowered = entry.lower()
    if "fixture" in lowered and "tests/fixtures" in lowered:
        matchers.append("tests/fixtures/**")
    if number == 161 and "routing tests" in lowered:
        matchers.append("tests/test_routing.py")
    if number == 139 and "rust lifecycle" in lowered:
        matchers.append("src-tauri/src/**")
    if number == 139 and "frontend" in lowered:
        matchers.append("frontend/**")
    if number == 149 and "cross-language" in lowered:
        matchers.extend(["tests/**", "src-tauri/**"])
    if number == 149 and "atomic-write callers" in lowered:
        matchers.extend(["src-python/**", "src-tauri/src/**"])
    if number == 150 and "rust tests" in lowered:
        matchers.append("src-tauri/src/openai_usage.rs")
    if number == 111 and "tauri settings" in lowered:
        matchers.append("src-tauri/src/**")
    if number == 111 and "settings drawer" in lowered:
        matchers.extend(
            [
                "frontend/src/pages/SettingsPage.tsx",
                "frontend/src/components/SettingsDrawer.tsx",
                "frontend/src/components/**/Settings*",
            ]
        )
    if number == 111 and "ui-contract tests" in lowered:
        matchers.extend(["src-tauri/src/**", "frontend/scripts/**"])
    if number == 111 and ("smoke harness" in lowered or "documented verification" in lowered):
        matchers.extend(
            [
                "scripts/*autostart*",
                "scripts/**/autostart*",
                "docs/*autostart*",
                "docs/**/autostart*",
            ]
        )
    return sorted(set(matchers))


def parse_expected_hotset(body: str, number: int) -> dict[str, Any]:
    normalized = normalize_body(body)
    match = re.search(
        r"(?ms)^## Expected hotset\s*\n(?P<section>.*?)(?=^##\s+|\Z)", normalized
    )
    if not match:
        raise ValueError(f"missing Expected hotset section on #{number}")
    raw_entries = [
        line.strip()
        for line in match.group("section").splitlines()
        if re.match(r"^\s*[-*]\s+\S", line)
    ]
    if not raw_entries:
        raise ValueError(f"empty Expected hotset section on #{number}")
    entries: list[dict[str, Any]] = []
    all_matchers: list[str] = []
    for raw in raw_entries:
        entry = _normalize_hotset_entry(raw)
        matchers = _path_matchers(entry, number)
        if not matchers:
            raise ValueError(f"Expected hotset entry has no path matcher on #{number}: {entry}")
        entries.append(
            {"raw": raw, "normalized": entry, "path_matchers": matchers}
        )
        all_matchers.extend(matchers)
    return {"entries": entries, "matchers": sorted(set(all_matchers))}


def intersections(files: Iterable[str], matchers: Iterable[str]) -> list[str]:
    normalized = sorted(set(value.replace("\\", "/") for value in files if value))
    patterns = list(matchers)
    return [
        path
        for path in normalized
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)
    ]


def validate_native_task_capture(
    capture: dict[str, Any], expected_issues: list[int]
) -> list[dict[str, Any]]:
    captured_at = parse_timestamp(capture.get("captured_at"), "native Task")
    if capture.get("tool") != "codex_app__list_threads":
        raise ValueError("native Task tool must be codex_app__list_threads")
    if not isinstance(capture.get("limit"), int) or capture["limit"] <= 0:
        raise ValueError("native Task capture limit missing")
    records = capture.get("records")
    if not isinstance(records, list):
        raise ValueError("native Task records missing")
    expected_pairs = {
        (number, kind)
        for number in expected_issues
        for kind in ("exact_title", "issue_number")
    }
    actual_pairs = {(record.get("issue"), record.get("kind")) for record in records}
    if actual_pairs != expected_pairs or len(records) != len(expected_pairs):
        raise ValueError(
            f"native Task records incomplete: actual={sorted(actual_pairs)} expected={sorted(expected_pairs)}"
        )
    normalized: list[dict[str, Any]] = []
    for record in records:
        number = int(record["issue"])
        kind = record["kind"]
        expected_query = (
            EXPECTED_TITLES[number] if kind == "exact_title" else f"Issue {number}"
        )
        if record.get("query") != expected_query:
            raise ValueError(f"native Task query mismatch on #{number} {kind}")
        if record.get("schemaVersion") != 2:
            raise ValueError(f"native Task schemaVersion mismatch on #{number} {kind}")
        threads = record.get("threads")
        unavailable_hosts = record.get("unavailableHosts")
        if not isinstance(threads, list) or not isinstance(unavailable_hosts, list):
            raise ValueError(f"native Task arrays missing on #{number} {kind}")
        if unavailable_hosts:
            raise ValueError(f"NEEDS_CONTEXT: native Task host unavailable on #{number}")
        if threads:
            raise ValueError(f"active native Task matches #{number}")
        normalized.append(
            {
                "issue": number,
                "issue_title": EXPECTED_TITLES[number],
                "kind": kind,
                "query": expected_query,
                "captured_at": captured_at,
                "schema_version": 2,
                "unavailable_hosts": [],
                "unavailable_host_count": 0,
                "match_count": 0,
            }
        )
    normalized.sort(key=lambda value: (expected_issues.index(value["issue"]), value["kind"]))
    return normalized


def _porcelain_paths(output: str) -> list[str]:
    paths: list[str] = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        value = line[3:]
        if " -> " in value:
            old, new = value.split(" -> ", 1)
            paths.extend([old, new])
        else:
            paths.append(value)
    return sorted(set(path.replace("\\", "/") for path in paths))


def _diff_files(base_ref: str, revision: str) -> list[str]:
    output = run("git", "diff", "--name-only", f"{base_ref}...{revision}")
    return sorted(set(line.strip().replace("\\", "/") for line in output.splitlines() if line.strip()))


def _hotset_map(files: list[str], candidates: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        str(candidate["number"]): intersections(
            files, candidate["expected_hotset"]["matchers"]
        )
        for candidate in candidates
    }


def _parse_worktrees() -> list[dict[str, str]]:
    output = run("git", "worktree", "list", "--porcelain")
    blocks = re.split(r"\r?\n\r?\n", output.strip())
    values: list[dict[str, str]] = []
    for block in blocks:
        record: dict[str, str] = {}
        for line in block.splitlines():
            if " " in line:
                key, value = line.split(" ", 1)
                record[key] = value
            else:
                record[line] = "true"
        if "worktree" in record and "HEAD" in record:
            values.append(record)
    return values


def collect_surfaces(
    base_ref: str,
    candidates: list[dict[str, Any]],
    planned_paths: list[str],
) -> dict[str, Any]:
    worktree_raw = _parse_worktrees()
    worktrees: list[dict[str, Any]] = []
    checked_out_branches: set[str] = set()
    for raw in worktree_raw:
        revision = raw["HEAD"]
        branch_ref = raw.get("branch")
        branch = branch_ref.removeprefix("refs/heads/") if branch_ref else None
        if branch:
            checked_out_branches.add(branch)
        path = pathlib.Path(raw["worktree"])
        status_output = run(
            "git", "status", "--porcelain", "--untracked-files=all", cwd=path
        )
        dirty_files = _porcelain_paths(status_output)
        changed_files = sorted(set(_diff_files(base_ref, revision) + dirty_files))
        try:
            is_current = path.resolve() == ROOT.resolve()
        except OSError:
            is_current = False
        if is_current:
            changed_files = sorted(set(changed_files + planned_paths))
        hotset = _hotset_map(changed_files, candidates)
        overlapping = sorted(
            {path for values in hotset.values() for path in values}
        )
        if is_current and not overlapping:
            classification = "migration-control"
        elif branch == base_ref and not dirty_files and not changed_files:
            classification = "audited-baseline"
        elif overlapping:
            classification = "active-product-hotset"
        else:
            classification = "non-overlapping-active-worktree"
        worktrees.append(
            {
                "logical_identity": f"worktree:{branch or 'detached-' + revision[:12]}",
                "branch": branch,
                "revision": revision,
                "clean": not bool(dirty_files),
                "dirty_files": dirty_files,
                "changed_files_vs_dev": changed_files,
                "hotset_intersections": hotset,
                "classification": classification,
                "active": True,
            }
        )

    open_prs_raw = json.loads(
        run(
            "gh",
            "pr",
            "list",
            "--repo",
            REPOSITORY,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,title,headRefName,headRefOid,baseRefName,url",
        )
    )
    prs: list[dict[str, Any]] = []
    pr_heads: dict[str, list[int]] = {}
    for raw in open_prs_raw:
        number = int(raw["number"])
        pr_heads.setdefault(raw["headRefName"], []).append(number)
        files = sorted(
            item["filename"]
            for item in api_pages(f"repos/{REPOSITORY}/pulls/{number}/files")
        )
        prs.append(
            {
                "logical_identity": f"pull-request:#{number}",
                "number": number,
                "title": raw["title"],
                "head_ref": raw["headRefName"],
                "head_revision": raw["headRefOid"],
                "base_ref": raw["baseRefName"],
                "changed_files_vs_base": files,
                "hotset_intersections": _hotset_map(files, candidates),
                "url": raw["url"],
                "active": True,
            }
        )

    branches: list[dict[str, Any]] = []
    branch_lines = run(
        "git", "for-each-ref", "--format=%(refname:short)%09%(objectname)", "refs/heads"
    ).splitlines()
    for line in branch_lines:
        name, revision = line.split("\t", 1)
        changed_files = _diff_files(base_ref, revision)
        branches.append(
            {
                "logical_identity": f"local-branch:{name}",
                "name": name,
                "revision": revision,
                "changed_files_vs_dev": changed_files,
                "hotset_intersections": _hotset_map(changed_files, candidates),
                "checked_out_in_worktree": name in checked_out_branches,
                "open_prs": sorted(pr_heads.get(name, [])),
                "active": name in checked_out_branches or bool(pr_heads.get(name)),
            }
        )
    return {
        "worktrees": sorted(worktrees, key=lambda value: value["logical_identity"]),
        "local_branches": sorted(branches, key=lambda value: value["name"]),
        "open_pr_heads": sorted(prs, key=lambda value: value["number"]),
    }


def collect_comment_evidence() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for number, prefix in COMMENT_PREFIXES.items():
        comments = api_pages(f"repos/{REPOSITORY}/issues/{number}/comments")
        purpose = [
            item
            for item in comments
            if normalize_body(item.get("body")).startswith(prefix)
        ]
        if number == 147 and not purpose:
            result[str(number)] = None
            continue
        if len(purpose) != 1:
            raise ValueError(f"same-purpose comment multiplicity on #{number}: {len(purpose)}")
        match = purpose[0]
        body = normalize_body(match.get("body"))
        exact = [
            item
            for item in comments
            if normalize_body(item.get("body")) == body
        ]
        if len(exact) != 1:
            raise ValueError(f"exact comment multiplicity on #{number}: {len(exact)}")
        if number == 147 and int(match["id"]) != CHECKPOINT_COMMENT_ID:
            raise ValueError("checkpoint public comment ID changed")
        result[str(number)] = {
            "public_comment_id": int(match["id"]),
            "purpose_prefix": prefix,
            "normalized_body_sha256": sha256_text(body),
            "prefix_multiplicity": 1,
            "exact_body_multiplicity": 1,
            "created_at": match["created_at"],
            "updated_at": match["updated_at"],
            "url": match["html_url"],
        }
    return result


def collect_global_audit() -> dict[str, Any]:
    all_issues = [
        item
        for item in api_pages(f"repos/{REPOSITORY}/issues?state=all")
        if "pull_request" not in item
    ]
    open_issues = [item for item in all_issues if item["state"] == "open"]
    lifecycle_violations = []
    for item in open_issues:
        labels = {label["name"] for label in item["labels"]}
        matches = sorted(labels & LIFECYCLE_LABELS)
        if len(matches) != 1:
            lifecycle_violations.append(
                {"issue": int(item["number"]), "lifecycle_labels": matches}
            )
    if lifecycle_violations:
        raise ValueError(f"canonical lifecycle violations: {lifecycle_violations}")
    membership_actual = {
        title: sorted(
            int(item["number"])
            for item in open_issues
            if item.get("milestone") and item["milestone"]["title"] == title
        )
        for title in EXPECTED_MEMBERSHIP
    }
    if membership_actual != EXPECTED_MEMBERSHIP:
        raise ValueError(f"milestone membership mismatch: {membership_actual}")
    milestones = api_pages(f"repos/{REPOSITORY}/milestones?state=all")
    milestone_records = [
        {
            "number": int(item["number"]),
            "title": item["title"],
            "description": item.get("description"),
            "state": item["state"],
            "open_issues": int(item["open_issues"]),
            "closed_issues": int(item["closed_issues"]),
        }
        for item in milestones
    ]
    open_assignees = {
        str(item["number"]): sorted(value["login"] for value in item["assignees"])
        for item in open_issues
        if item["assignees"]
    }
    contracts = {
        str(number): issue_snapshot(number)
        for number in [28, 147, 156, 159, 160, 161]
    }
    relations = {
        str(number): {
            "parent": issue_parent(number),
            "blocked_by": blockers(number),
            "children": children(number),
        }
        for number in [57, 64, 71, 73, 111, 147, 156, 159, 160, 161]
    }
    if relations["156"]["children"] != [159, 161]:
        raise ValueError("#156 child set mismatch")
    if relations["156"]["blocked_by"] != [159, 161]:
        raise ValueError("#156 blocker set mismatch")
    if relations["64"]["blocked_by"] != [62, 63, 156]:
        raise ValueError("#64 blocker set mismatch")
    if relations["160"]["blocked_by"] != [111]:
        raise ValueError("#160 blocker set mismatch")
    return {
        "open_issue_count": len(open_issues),
        "canonical_lifecycle_violations": lifecycle_violations,
        "milestone_membership_expected": EXPECTED_MEMBERSHIP,
        "milestone_membership_actual": membership_actual,
        "milestones": milestone_records,
        "open_issue_assignees": open_assignees,
        "critical_contracts": contracts,
        "relations": relations,
        "comments": collect_comment_evidence(),
    }


def _candidate_eligibility(
    candidate: dict[str, Any],
    task_records: list[dict[str, Any]],
    surfaces: dict[str, Any],
) -> dict[str, Any]:
    number = candidate["number"]
    label_dependency_reasons: list[str] = []
    if candidate["state"] != "open":
        label_dependency_reasons.append("not-open")
    if "ready-for-agent" not in candidate["labels"]:
        label_dependency_reasons.append("not-ready-for-agent")
    if candidate["assignees"]:
        label_dependency_reasons.append("assigned")
    if candidate["blocked_by"]:
        label_dependency_reasons.append("blocked")

    candidate_tasks = [record for record in task_records if record["issue"] == number]
    ownership_reasons: list[str] = []
    if len(candidate_tasks) != 2:
        ownership_reasons.append("incomplete-native-task-evidence")
    if any(record["unavailable_host_count"] for record in candidate_tasks):
        ownership_reasons.append("native-task-host-unavailable")
    if any(record["match_count"] for record in candidate_tasks):
        ownership_reasons.append("native-task-match")
    if candidate["assignees"]:
        ownership_reasons.append("assignee")
    active_intersections: list[dict[str, Any]] = []
    for kind in ("worktrees", "local_branches", "open_pr_heads"):
        for surface in surfaces[kind]:
            hits = surface["hotset_intersections"][str(number)]
            if surface["active"] and hits:
                active_intersections.append(
                    {
                        "surface": surface["logical_identity"],
                        "changed_files": hits,
                    }
                )
    if active_intersections:
        ownership_reasons.append("active-file-hotset-intersection")
    return {
        "label_dependency": {
            "open": candidate["state"] == "open",
            "ready_for_agent": "ready-for-agent" in candidate["labels"],
            "unassigned": not bool(candidate["assignees"]),
            "open_blockers": candidate["blocked_by"],
            "eligible": not label_dependency_reasons,
            "ineligible_reasons": label_dependency_reasons,
        },
        "ownership_hotset": {
            "native_task_query_count": len(candidate_tasks),
            "native_task_matches": sum(record["match_count"] for record in candidate_tasks),
            "native_task_unavailable_hosts": sum(
                record["unavailable_host_count"] for record in candidate_tasks
            ),
            "assignees": candidate["assignees"],
            "active_surface_intersections": active_intersections,
            "eligible": not ownership_reasons,
            "ineligible_reasons": ownership_reasons,
        },
    }


def _assert_sanitized(value: Any) -> None:
    serialized = json.dumps(value, ensure_ascii=False)
    if re.search(r"(?i)\b[A-Z]:[\\/]", serialized):
        raise ValueError("artifact contains a local absolute path")
    if re.search(r'"(?:thread_id|task_id|rollout_id|callback_id)"\s*:', serialized):
        raise ValueError("artifact contains a private identifier field")


def build_core(
    native_capture: dict[str, Any],
    base_ref: str,
    planned_paths: list[str],
    native_capture_file_sha256: str | None = None,
) -> dict[str, Any]:
    task_records = validate_native_task_capture(native_capture, FRONTIER_EXPECTED)
    candidates = [issue_snapshot(number, include_hotset=True) for number in FRONTIER_EXPECTED]
    for candidate in candidates:
        expected_title = EXPECTED_TITLES[candidate["number"]]
        if candidate["title"] != expected_title:
            raise ValueError(f"title mismatch on #{candidate['number']}")
    surfaces = collect_surfaces(base_ref, candidates, planned_paths)
    for candidate in candidates:
        candidate["eligibility"] = _candidate_eligibility(
            candidate, task_records, surfaces
        )
    frontier = [
        number
        for number in PRIORITY_ORDER
        if number in FRONTIER_EXPECTED
        and next(item for item in candidates if item["number"] == number)[
            "eligibility"
        ]["label_dependency"]["eligible"]
        and next(item for item in candidates if item["number"] == number)[
            "eligibility"
        ]["ownership_hotset"]["eligible"]
    ]
    if frontier != FRONTIER_EXPECTED:
        raise ValueError(f"frontier fail-closed mismatch: {frontier}")

    for surface in surfaces["worktrees"]:
        if surface["classification"] == "active-product-hotset":
            raise ValueError(
                f"active worktree intersects a frontier hotset: {surface['logical_identity']}"
            )
    for surface in surfaces["local_branches"]:
        if surface["active"] and any(surface["hotset_intersections"].values()):
            raise ValueError(
                f"active local branch intersects a frontier hotset: {surface['logical_identity']}"
            )
    for surface in surfaces["open_pr_heads"]:
        if any(surface["hotset_intersections"].values()):
            raise ValueError(
                f"open PR intersects a frontier hotset: {surface['logical_identity']}"
            )

    core = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": CORE_KIND,
        "captured_at": utc_now(),
        "repository": REPOSITORY,
        "generator": "scripts/generate_wayfinder_final_audit.py",
        "base_ref": base_ref,
        "base_revision": run("git", "rev-parse", base_ref).strip(),
        "planning_branch": run("git", "branch", "--show-current").strip(),
        "planning_revision": run("git", "rev-parse", "HEAD").strip(),
        "native_task_capture": {
            "tool": "codex_app__list_threads",
            "source_capture_file_sha256": native_capture_file_sha256,
            "source_capture_canonical_json_sha256": canonical_json_sha256(native_capture),
            "captured_at": native_capture["captured_at"],
            "limit": native_capture["limit"],
            "records": task_records,
        },
        "candidates": candidates,
        "ownership_surfaces": surfaces,
        "frontier": frontier,
        "global_github_audit": collect_global_audit(),
        "derivation": {
            "label_dependency_rule": "open AND ready-for-agent AND unassigned AND zero open native blockers",
            "ownership_hotset_rule": "two exact native queries clear AND no unavailable native host AND unassigned AND no active worktree/local-branch/open-PR changed-file intersection with the parsed Expected hotset",
            "branch_names_used_as_ownership_evidence": False,
            "inactive_local_branches_block_eligibility": False,
            "inactive_branch_reason": "A local branch is active only when checked out in a worktree or used as an open PR head; every local branch is still enumerated with its file intersections.",
            "planning_worktree_rule": "The current planning worktree is migration-control evidence only because its complete changed-file set has zero frontier product-hotset intersections.",
            "dev_rule": "The dev worktree is independently audited for revision, cleanliness, and changed-file intersections; it is not trusted by name alone.",
            "result": "eligible",
        },
        "sanitization": {
            "native_task_ids_persisted": False,
            "local_absolute_paths_persisted": False,
            "worktree_identity_basis": "branch or detached revision, never filesystem path",
            "changed_file_paths": "repository-relative",
        },
    }
    _assert_sanitized(core)
    validate_core(core)
    return core


def validate_core(core: dict[str, Any]) -> None:
    if core.get("schema_version") != SCHEMA_VERSION or core.get("artifact_kind") != CORE_KIND:
        raise ValueError("frontier artifact schema/kind mismatch")
    parse_timestamp(core.get("captured_at"), "frontier artifact")
    native = core.get("native_task_capture", {})
    parse_timestamp(native.get("captured_at"), "native Task")
    if not re.fullmatch(
        r"[0-9a-f]{64}", str(native.get("source_capture_file_sha256", ""))
    ):
        raise ValueError("native Task source-capture file hash missing")
    records = native.get("records")
    if not isinstance(records, list) or len(records) != 12:
        raise ValueError("frontier artifact must contain 12 native Task records")
    pairs = {(item.get("issue"), item.get("kind")) for item in records}
    expected_pairs = {
        (number, kind)
        for number in FRONTIER_EXPECTED
        for kind in ("exact_title", "issue_number")
    }
    if pairs != expected_pairs:
        raise ValueError("frontier artifact native Task pair mismatch")
    for record in records:
        number = record["issue"]
        expected_query = (
            EXPECTED_TITLES[number]
            if record["kind"] == "exact_title"
            else f"Issue {number}"
        )
        if record.get("query") != expected_query:
            raise ValueError("frontier artifact native Task query mismatch")
        if record.get("schema_version") != 2:
            raise ValueError("frontier artifact native schema mismatch")
        if record.get("match_count") != 0:
            raise ValueError("frontier artifact contains native Task matches")
        if record.get("unavailable_host_count") != 0 or record.get("unavailable_hosts") != []:
            raise ValueError("frontier artifact contains unavailable native hosts")
        parse_timestamp(record.get("captured_at"), "native Task record")
    candidates = core.get("candidates")
    if not isinstance(candidates, list) or [item.get("number") for item in candidates] != FRONTIER_EXPECTED:
        raise ValueError("frontier artifact candidate mismatch")
    for candidate in candidates:
        if candidate.get("title") != EXPECTED_TITLES[candidate["number"]]:
            raise ValueError("frontier artifact title mismatch")
        hotset = candidate.get("expected_hotset", {})
        if not hotset.get("entries") or not hotset.get("matchers"):
            raise ValueError("frontier artifact hotset missing")
        eligibility = candidate.get("eligibility", {})
        if not eligibility.get("label_dependency", {}).get("eligible"):
            raise ValueError("frontier label/dependency eligibility failed")
        if not eligibility.get("ownership_hotset", {}).get("eligible"):
            raise ValueError("frontier ownership/hotset eligibility failed")
    if core.get("frontier") != FRONTIER_EXPECTED:
        raise ValueError("frontier artifact derived order mismatch")
    surfaces = core.get("ownership_surfaces", {})
    for key in ("worktrees", "local_branches", "open_pr_heads"):
        if not isinstance(surfaces.get(key), list):
            raise ValueError(f"frontier ownership surface missing: {key}")
        for surface in surfaces[key]:
            if "revision" not in surface and "head_revision" not in surface:
                raise ValueError(f"surface revision missing: {surface.get('logical_identity')}")
            if "hotset_intersections" not in surface:
                raise ValueError(f"surface intersections missing: {surface.get('logical_identity')}")
    if core.get("derivation", {}).get("result") != "eligible":
        raise ValueError("frontier artifact derivation did not fail closed")
    _assert_sanitized(core)


def validate_transcript(transcript: dict[str, Any], core_sha256: str) -> None:
    if transcript.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("checkpoint transcript schema mismatch")
    parse_timestamp(transcript.get("captured_at"), "checkpoint transcript")
    if transcript.get("issue") != 147:
        raise ValueError("checkpoint transcript issue mismatch")
    if transcript.get("purpose_prefix") != COMMENT_PREFIXES[147]:
        raise ValueError("checkpoint transcript purpose prefix mismatch")
    operation = transcript.get("operation_decision")
    if operation not in {"create", "patch", "exact-no-op"}:
        raise ValueError("checkpoint operation decision missing")
    desired = transcript.get("desired", {})
    desired_body = normalize_body(desired.get("normalized_body"))
    if desired.get("normalized_body_sha256") != sha256_text(desired_body):
        raise ValueError("checkpoint desired hash mismatch")
    required_reference = f"{CORE_ARTIFACT_PATH} (SHA-256: `{core_sha256}`)"
    if required_reference not in desired_body:
        raise ValueError("checkpoint desired body lacks durable artifact path/hash")
    historical = transcript.get("historical_original", {})
    original_body = normalize_body(historical.get("normalized_body"))
    expected_original = (
        desired_body if operation == "create" else normalize_body(ORIGINAL_CHECKPOINT_BODY)
    )
    if original_body != expected_original:
        raise ValueError("checkpoint historical original body mismatch")
    if historical.get("normalized_body_sha256") != sha256_text(original_body):
        raise ValueError("checkpoint historical original hash mismatch")
    pre = transcript.get("pre_update", {})
    if operation == "create":
        if (
            pre.get("public_comment_id") is not None
            or pre.get("normalized_body_sha256") is not None
        ):
            raise ValueError("checkpoint create operation invents prior state")
    else:
        if not isinstance(pre.get("public_comment_id"), int) or pre["public_comment_id"] <= 0:
            raise ValueError("checkpoint prior public comment ID missing")
        if not re.fullmatch(r"[0-9a-f]{64}", str(pre.get("normalized_body_sha256", ""))):
            raise ValueError("checkpoint prior hash missing")
    post = transcript.get("post_readback", {})
    if not isinstance(post.get("public_comment_id"), int) or post["public_comment_id"] <= 0:
        raise ValueError("checkpoint post public comment ID missing")
    if operation != "create" and post["public_comment_id"] != pre["public_comment_id"]:
        raise ValueError("checkpoint public comment identity changed")
    if post.get("prefix_multiplicity") != 1 or post.get("exact_body_multiplicity") != 1:
        raise ValueError("checkpoint post-readback multiplicity mismatch")
    if post.get("normalized_body_sha256") != desired.get("normalized_body_sha256"):
        raise ValueError("checkpoint post-readback body mismatch")
    _assert_sanitized(transcript)


def build_final(core: dict[str, Any], core_path: str, transcript: dict[str, Any]) -> dict[str, Any]:
    validate_core(core)
    core_sha = serialized_json_sha256(core)
    validate_transcript(transcript, core_sha)
    final = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": FINAL_KIND,
        "captured_at": utc_now(),
        "repository": REPOSITORY,
        "generator": "scripts/generate_wayfinder_final_audit.py",
        "frontier_artifact": {
            "repository_relative_path": core_path,
            "sha256": core_sha,
            "embedded": core,
        },
        "checkpoint_update_transcript": transcript,
        "derivation": {
            "frontier": core["frontier"],
            "label_dependency_eligibility": {
                str(item["number"]): item["eligibility"]["label_dependency"]
                for item in core["candidates"]
            },
            "ownership_hotset_eligibility": {
                str(item["number"]): item["eligibility"]["ownership_hotset"]
                for item in core["candidates"]
            },
            "checkpoint_exact_body_guard": transcript["operation_decision"],
            "checkpoint_post_readback_exact": True,
        },
        "artifact_hashes": {
            core_path: core_sha,
            TRANSCRIPT_ARTIFACT_PATH: serialized_json_sha256(transcript),
        },
        "sanitization": core["sanitization"],
    }
    _assert_sanitized(final)
    validate_final(final)
    return final


def validate_final(final: dict[str, Any]) -> None:
    if final.get("schema_version") != SCHEMA_VERSION or final.get("artifact_kind") != FINAL_KIND:
        raise ValueError("final artifact schema/kind mismatch")
    parse_timestamp(final.get("captured_at"), "final artifact")
    frontier_artifact = final.get("frontier_artifact", {})
    core = frontier_artifact.get("embedded")
    if not isinstance(core, dict):
        raise ValueError("final artifact does not embed frontier evidence")
    validate_core(core)
    core_sha = serialized_json_sha256(core)
    if frontier_artifact.get("repository_relative_path") != CORE_ARTIFACT_PATH:
        raise ValueError("final artifact frontier path mismatch")
    if frontier_artifact.get("sha256") != core_sha:
        raise ValueError("final artifact embedded frontier hash mismatch")
    transcript = final.get("checkpoint_update_transcript")
    if not isinstance(transcript, dict):
        raise ValueError("final artifact checkpoint transcript missing")
    validate_transcript(transcript, core_sha)
    recorded_prior = core["global_github_audit"]["comments"].get("147")
    transcript_prior = transcript["pre_update"]
    if transcript["operation_decision"] == "create":
        if recorded_prior is not None:
            raise ValueError("frontier capture unexpectedly contains a checkpoint prior to create")
    elif (
        not isinstance(recorded_prior, dict)
        or recorded_prior["public_comment_id"] != transcript_prior["public_comment_id"]
        or recorded_prior["normalized_body_sha256"]
        != transcript_prior["normalized_body_sha256"]
        or recorded_prior["prefix_multiplicity"] != 1
        or recorded_prior["exact_body_multiplicity"] != 1
    ):
        raise ValueError("frontier capture does not match checkpoint transcript prior readback")
    hashes = final.get("artifact_hashes", {})
    if hashes.get(CORE_ARTIFACT_PATH) != core_sha:
        raise ValueError("final artifact hash manifest mismatch")
    if hashes.get(TRANSCRIPT_ARTIFACT_PATH) != serialized_json_sha256(transcript):
        raise ValueError("final transcript hash manifest mismatch")
    _assert_sanitized(final)


def compare_live(final: dict[str, Any]) -> None:
    validate_final(final)
    core = final["frontier_artifact"]["embedded"]
    fresh_global = collect_global_audit()
    recorded_global = core["global_github_audit"]
    for key in (
        "canonical_lifecycle_violations",
        "milestone_membership_actual",
        "critical_contracts",
        "relations",
    ):
        if fresh_global[key] != recorded_global[key]:
            raise ValueError(f"fresh live GitHub audit mismatch: {key}")
    for number in ("8", "10", "12", "62"):
        if fresh_global["comments"][number] != recorded_global["comments"][number]:
            raise ValueError(f"fresh live GitHub audit mismatch: comment #{number}")
    fresh_checkpoint = fresh_global["comments"]["147"]
    transcript_post = final["checkpoint_update_transcript"]["post_readback"]
    if (
        fresh_checkpoint["public_comment_id"] != transcript_post["public_comment_id"]
        or fresh_checkpoint["normalized_body_sha256"]
        != transcript_post["normalized_body_sha256"]
        or fresh_checkpoint["prefix_multiplicity"]
        != transcript_post["prefix_multiplicity"]
        or fresh_checkpoint["exact_body_multiplicity"]
        != transcript_post["exact_body_multiplicity"]
    ):
        raise ValueError("fresh live GitHub audit mismatch: updated checkpoint #147")
    fresh_candidates = [
        issue_snapshot(number, include_hotset=True) for number in FRONTIER_EXPECTED
    ]
    for fresh, recorded in zip(fresh_candidates, core["candidates"], strict=True):
        for key in (
            "number",
            "title",
            "state",
            "body_normalized_sha256",
            "labels",
            "assignees",
            "milestone",
            "parent",
            "blocked_by",
            "children",
            "expected_hotset",
        ):
            if fresh[key] != recorded[key]:
                raise ValueError(f"fresh frontier Issue mismatch #{fresh['number']}: {key}")
    fresh_surfaces = collect_surfaces(core["base_ref"], fresh_candidates, [])
    for kind in ("worktrees", "local_branches", "open_pr_heads"):
        for surface in fresh_surfaces[kind]:
            if surface["active"] and any(surface["hotset_intersections"].values()):
                raise ValueError(
                    f"fresh active surface/hotset intersection: {surface['logical_identity']}"
                )
    checkpoint_comment_id = transcript_post["public_comment_id"]
    current_comment = api_json(
        f"repos/{REPOSITORY}/issues/comments/{checkpoint_comment_id}"
    )
    desired = final["checkpoint_update_transcript"]["desired"]
    if sha256_text(normalize_body(current_comment.get("body"))) != desired["normalized_body_sha256"]:
        raise ValueError("fresh checkpoint body mismatch")
    print(
        json.dumps(
            {
                "status": "live-ok",
                "open_issue_count": fresh_global["open_issue_count"],
                "frontier": core["frontier"],
                "checkpoint_comment_id": checkpoint_comment_id,
                "worktree_count": len(fresh_surfaces["worktrees"]),
                "local_branch_count": len(fresh_surfaces["local_branches"]),
                "open_pr_head_count": len(fresh_surfaces["open_pr_heads"]),
            },
            ensure_ascii=False,
        )
    )


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )


def read_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path.name}")
    return value


def relative_path(path: pathlib.Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def command_capture(args: argparse.Namespace) -> None:
    native_path = pathlib.Path(args.native_task_evidence)
    output = pathlib.Path(args.output)
    planned = sorted(
        set(
            args.planned_path
            + [
                relative_path(output),
                CORE_ARTIFACT_PATH,
                TRANSCRIPT_ARTIFACT_PATH,
                FINAL_ARTIFACT_PATH,
            ]
        )
    )
    core = build_core(
        read_json(native_path),
        args.base_ref,
        planned,
        native_capture_file_sha256=sha256_file(native_path),
    )
    write_json(output, core)
    validate_core(read_json(output))
    print(
        json.dumps(
            {
                "status": "captured-and-self-validated",
                "artifact": relative_path(output),
                "sha256": serialized_json_sha256(core),
                "file_sha256": sha256_file(output),
                "frontier": core["frontier"],
                "native_query_count": len(core["native_task_capture"]["records"]),
            },
            ensure_ascii=False,
        )
    )


def command_finalize(args: argparse.Namespace) -> None:
    core_path = pathlib.Path(args.core)
    transcript_path = pathlib.Path(args.transcript)
    output = pathlib.Path(args.output)
    core = read_json(core_path)
    transcript = read_json(transcript_path)
    final = build_final(core, relative_path(core_path), transcript)
    write_json(output, final)
    validate_final(read_json(output))
    print(
        json.dumps(
            {
                "status": "finalized-and-self-validated",
                "artifact": relative_path(output),
                "sha256": serialized_json_sha256(final),
                "file_sha256": sha256_file(output),
                "frontier_artifact_sha256": serialized_json_sha256(core),
                "checkpoint_operation": transcript["operation_decision"],
            },
            ensure_ascii=False,
        )
    )


def command_validate(args: argparse.Namespace) -> None:
    path = pathlib.Path(args.artifact)
    value = read_json(path)
    if value.get("artifact_kind") == CORE_KIND:
        validate_core(value)
    elif value.get("artifact_kind") == FINAL_KIND:
        validate_final(value)
        if args.live:
            compare_live(value)
    else:
        raise ValueError("unknown Wayfinder audit artifact kind")
    print(
        json.dumps(
            {
                "status": "validated",
                "artifact": relative_path(path),
                "sha256": serialized_json_sha256(value),
                "file_sha256": sha256_file(path),
                "live": bool(args.live),
            },
            ensure_ascii=False,
        )
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subcommands = result.add_subparsers(dest="command", required=True)
    capture = subcommands.add_parser("capture", help="capture fresh read-only frontier evidence")
    capture.add_argument("--native-task-evidence", required=True)
    capture.add_argument("--output", default=CORE_ARTIFACT_PATH)
    capture.add_argument("--base-ref", default="dev")
    capture.add_argument("--planned-path", action="append", default=[])
    capture.set_defaults(handler=command_capture)
    finalize = subcommands.add_parser(
        "finalize", help="combine a frontier artifact and guarded checkpoint transcript"
    )
    finalize.add_argument("--core", default=CORE_ARTIFACT_PATH)
    finalize.add_argument("--transcript", default=TRANSCRIPT_ARTIFACT_PATH)
    finalize.add_argument("--output", default=FINAL_ARTIFACT_PATH)
    finalize.set_defaults(handler=command_finalize)
    validate = subcommands.add_parser("validate", help="self-validate an audit artifact")
    validate.add_argument("--artifact", required=True)
    validate.add_argument("--live", action="store_true")
    validate.set_defaults(handler=command_validate)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        args.handler(args)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
