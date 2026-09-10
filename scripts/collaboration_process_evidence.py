"""Linux kernel-observed fixture execution; raw traces stay in private temp storage."""
from __future__ import annotations

from python_runtime_contract import require_python_313
require_python_313(__file__)

import ast
from pathlib import Path
import re
from datetime import datetime
import json


_RECORDER_SCHEMA = "codexhub.e2e-test-evidence.v1"
_EVENT_NAMES = {"suite_started", "case_started", "case_finished", "suite_finished"}
_CASE_OUTCOMES = {"passed", "failed", "skipped", "error"}


def _parse_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # Naive timestamps make process/cross-host ordering ambiguous.  The
        # fixed recorder always emits UTC with an explicit offset.
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def read_test_evidence(
    path: Path,
    *,
    expected_cases: tuple[str, ...] = (),
    recorder_path: Path | None = None,
    recorder_sha256: str | None = None,
) -> dict:
    """Read append-only recorder events and reject malformed/truncated logs.

    The JSONL file is evidence, not a result channel.  Every event must be a
    valid event from one recorder PID, in timestamp/line order, with exactly
    one suite boundary and one complete start/finish pair for every expected
    test case.  ``recorder_path``/``recorder_sha256`` are supplied by the
    trusted parent for real runs; omitting them keeps old in-memory fixtures
    readable while the process verifier still requires identity for a real
    observation.
    """
    result: dict[str, object] = {
        "available": path.is_file(),
        "schema": None,
        "events": [],
        "expected_cases": list(expected_cases),
        "verified_cases": [],
        "case_events": [],
        "suite_started_at": None,
        "suite_finished_at": None,
        "source_sha_at_test_start": None,
        "source_sha_at_test_finish": None,
        "test_sha_at_test_start": None,
        "test_sha_at_test_finish": None,
        "recorder_path": None,
        "recorder_sha256": None,
        "complete": False,
        "rejection": None,
    }
    if not path.is_file():
        result["rejection"] = "test_evidence_missing"
        return result
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            raise ValueError("empty_evidence")
        for line in lines:
            if not line.strip():
                raise ValueError("blank_evidence_line")
            event = json.loads(line)
            if not isinstance(event, dict) or event.get("schema") != _RECORDER_SCHEMA:
                raise ValueError("invalid_schema")
            result["events"].append(event)
            result["schema"] = event["schema"]
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        result["rejection"] = "test_evidence_unreadable"
        return result
    events = result["events"]
    if not isinstance(events, list):
        result["rejection"] = "test_evidence_unreadable"
        return result
    if not events:
        result["rejection"] = "test_evidence_empty"
        return result

    # Validate fields common to every event before interpreting lifecycle
    # semantics.  ``bool`` is an ``int`` subclass, so use exact type checks.
    event_pids: set[int] = set()
    event_times: list[float] = []
    monotonic_values: list[int | None] = []
    for event in events:
        if event.get("event") not in _EVENT_NAMES:
            result["rejection"] = "test_event_type_invalid"
            return result
        pid = event.get("pid")
        if type(pid) is not int or pid <= 0:
            result["rejection"] = "test_process_identity_invalid"
            return result
        event_pids.add(pid)
        timestamp = _parse_timestamp(event.get("timestamp"))
        if timestamp is None:
            result["rejection"] = "test_event_time_invalid"
            return result
        event_times.append(timestamp)
        monotonic = event.get("monotonic_ns")
        if monotonic is None:
            monotonic_values.append(None)
        elif type(monotonic) is int and monotonic >= 0:
            monotonic_values.append(monotonic)
        else:
            result["rejection"] = "test_event_monotonic_invalid"
            return result
    if len(event_pids) != 1:
        result["rejection"] = "test_process_identity_ambiguous"
        return result
    if any(later < earlier for earlier, later in zip(event_times, event_times[1:])):
        result["rejection"] = "test_event_time_order_invalid"
        return result
    if any(value is not None for value in monotonic_values):
        if any(value is None for value in monotonic_values):
            result["rejection"] = "test_event_monotonic_missing"
            return result
        if any(later < earlier for earlier, later in zip(monotonic_values, monotonic_values[1:])):
            result["rejection"] = "test_event_monotonic_order_invalid"
            return result

    # A real invocation must identify the fixed recorder source.  The digest
    # is checked against the harness copy rather than trusting the fixture's
    # claim about where it imported the module from.
    recorder_paths = {event.get("recorder_path") for event in events}
    recorder_hashes = {event.get("recorder_sha256") for event in events}
    if len(recorder_paths) != 1 or len(recorder_hashes) != 1:
        result["rejection"] = "test_recorder_identity_inconsistent"
        return result
    recorded_recorder_path = next(iter(recorder_paths))
    recorded_recorder_sha = next(iter(recorder_hashes))
    if not isinstance(recorded_recorder_path, str) or not Path(recorded_recorder_path).is_absolute():
        result["rejection"] = "test_recorder_path_invalid"
        return result
    if not isinstance(recorded_recorder_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", recorded_recorder_sha):
        result["rejection"] = "test_recorder_digest_invalid"
        return result
    result["recorder_path"] = recorded_recorder_path
    result["recorder_sha256"] = recorded_recorder_sha
    if recorder_path is not None and Path(recorded_recorder_path).resolve() != recorder_path.resolve():
        result["rejection"] = "test_recorder_path_mismatch"
        return result
    if recorder_sha256 is not None and recorded_recorder_sha != recorder_sha256:
        result["rejection"] = "test_recorder_digest_mismatch"
        return result

    started = [event for event in events if event.get("event") == "suite_started"]
    finished = [event for event in events if event.get("event") == "suite_finished"]
    case_events = [event for event in events if event.get("event") in {"case_started", "case_finished"}]
    result["case_events"] = case_events
    if len(started) != 1 or len(finished) != 1:
        result["rejection"] = "test_suite_terminal_missing_or_duplicate"
        return result
    if events[0] is not started[0] or events[-1] is not finished[0]:
        result["rejection"] = "test_suite_event_order_invalid"
        return result
    recorded_expected = started[0].get("expected_cases")
    if (
        not isinstance(recorded_expected, list)
        or not recorded_expected
        or any(not isinstance(item, str) or not item for item in recorded_expected)
        or len(set(recorded_expected)) != len(recorded_expected)
    ):
        result["rejection"] = "test_expected_cases_missing"
        return result
    expected = tuple(expected_cases) if expected_cases else tuple(recorded_expected)
    if tuple(recorded_expected) != expected:
        result["rejection"] = "test_expected_cases_mismatch"
        return result
    finished_expected = finished[0].get("expected_cases")
    if finished_expected != recorded_expected:
        result["rejection"] = "test_suite_expected_cases_drift"
        return result
    case_outcomes = finished[0].get("case_outcomes")
    if not isinstance(case_outcomes, dict) or set(case_outcomes) != set(expected):
        result["rejection"] = "test_suite_case_outcomes_missing_or_extra"
        return result
    if any(not isinstance(case, str) or outcome not in _CASE_OUTCOMES for case, outcome in case_outcomes.items()):
        result["rejection"] = "test_suite_case_outcome_invalid"
        return result
    if type(finished[0].get("complete")) is not bool:
        result["rejection"] = "test_suite_complete_invalid"
        return result
    by_case: dict[str, list[dict]] = {}
    suite_start_time = _parse_timestamp(started[0].get("timestamp"))
    suite_finish_time = _parse_timestamp(finished[0].get("timestamp"))
    if (
        suite_start_time is None
        or suite_finish_time is None
        or suite_start_time > suite_finish_time
    ):
        result["rejection"] = "test_suite_time_invalid"
        return result
    for event in case_events:
        case = event.get("case")
        if not isinstance(case, str) or not case:
            result["rejection"] = "test_case_identity_invalid"
            return result
        by_case.setdefault(case, []).append(event)
        event_time = _parse_timestamp(event.get("timestamp"))
        if event_time is None or not suite_start_time <= event_time <= suite_finish_time:
            result["rejection"] = "test_case_time_outside_suite"
            return result
        if event.get("event") == "case_finished" and event.get("outcome") not in _CASE_OUTCOMES:
            result["rejection"] = "test_case_outcome_invalid"
            return result
    unexpected = set(by_case) - set(expected)
    if unexpected:
        result["rejection"] = "test_case_set_unexpected"
        return result
    verified: list[str] = []
    for case in expected:
        entries = by_case.get(case, [])
        starts = [event for event in entries if event.get("event") == "case_started"]
        ends = [event for event in entries if event.get("event") == "case_finished"]
        if len(starts) == 1 and len(ends) == 1 and ends[0].get("outcome") == "passed":
            start_time = _parse_timestamp(starts[0].get("timestamp"))
            end_time = _parse_timestamp(ends[0].get("timestamp"))
            start_index = events.index(starts[0])
            end_index = events.index(ends[0])
            if start_time is not None and end_time is not None and start_index < end_index and start_time <= end_time:
                verified.append(case)
    result["verified_cases"] = verified
    result["suite_started_at"] = started[0].get("timestamp")
    result["suite_finished_at"] = finished[0].get("timestamp")
    result["source_sha_at_test_start"] = started[0].get("source_sha")
    result["source_sha_at_test_finish"] = finished[0].get("source_sha")
    result["test_sha_at_test_start"] = started[0].get("test_sha")
    result["test_sha_at_test_finish"] = finished[0].get("test_sha")
    if not all(
        isinstance(value, str) and value
        for value in (
            result["source_sha_at_test_start"],
            result["source_sha_at_test_finish"],
            result["test_sha_at_test_start"],
            result["test_sha_at_test_finish"],
        )
    ):
        result["rejection"] = "test_source_fingerprint_missing"
        return result
    if (
        result["source_sha_at_test_start"] != result["source_sha_at_test_finish"]
        or result["test_sha_at_test_start"] != result["test_sha_at_test_finish"]
    ):
        result["rejection"] = "test_source_changed_during_execution"
        return result
    result["complete"] = (
        tuple(verified) == expected
        and all(case_outcomes.get(case) == "passed" for case in expected)
        and finished[0].get("complete") is True
        and _parse_timestamp(result["suite_started_at"]) is not None
        and _parse_timestamp(result["suite_finished_at"]) is not None
    )
    if not result["complete"]:
        result["rejection"] = "test_case_set_incomplete"
    return result


def _recorder_opened(opened: set[str], recorder_path: Path | None) -> bool:
    if recorder_path is None:
        return False
    expected = recorder_path.resolve()
    for raw in opened:
        try:
            candidate = Path(raw).resolve()
        except OSError:
            continue
        if candidate == expected:
            return True
        # CPython may load the checked-in source from its adjacent __pycache__
        # without reopening the .py file.  The bytecode filename is still
        # anchored to the exact expected directory and module stem.
        if (
            candidate.parent.name == "__pycache__"
            and candidate.parent.parent == expected.parent
            and candidate.name.startswith(expected.stem + ".")
            and candidate.suffix == ".pyc"
        ):
            return True
    return False


def read_process_evidence(
    prefix: Path,
    fixture: Path,
    interpreter: Path,
    *,
    expected_cases: tuple[str, ...] = (),
    recorder_path: Path | None = None,
    evidence_path: Path | None = None,
    client_entrypoint: str | None = None,
    observed_process_tree: dict[int, set[int]] | None = None,
    observed_client_pids: set[int] | None = None,
) -> dict:
    """Read kernel traces and correlate the fixture process to its client.

    ``strace -ff`` writes one file per process.  The older verifier only
    inspected the unittest trace, which proved that *some* Python process ran
    but did not prove that it was a descendant of the Codex client that
    produced the turn.  When ``client_entrypoint`` is supplied we also parse
    the fork/clone edges and require a reachable client executable for every
    accepted test process.  The argument is optional so historical synthetic
    fixtures remain readable; the real harness always supplies it.
    """
    tests = []
    processes = []
    writes = []
    parent_children: dict[int, set[int]] = {}
    client_pids: set[int] = set()
    files = list(prefix.parent.glob(prefix.name + ".*"))
    for path in files:
        if not path.suffix[1:].isdigit():
            continue
        pid = int(path.suffix[1:])
        raw = path.read_text(encoding="utf-8", errors="strict")
        # Keep the process tree structural: no command arguments or request
        # bodies are copied into the report.  ``-ttt`` puts a timestamp before
        # each syscall, hence the optional numeric prefix below.
        for match in re.finditer(
            r"^(?:\d+\.\d+ )?(?:clone3?|fork|vfork)\(.*\)\s+=\s+(\d+)",
            raw,
            re.M,
        ):
            try:
                child_pid = int(match.group(1))
            except ValueError:
                continue
            if child_pid > 0:
                parent_children.setdefault(pid, set()).add(child_pid)
        if client_entrypoint:
            for match in re.finditer(r'^.*?execve\(("(?:[^"\\]|\\.)*"),', raw, re.M):
                try:
                    executable = ast.literal_eval(match.group(1))
                except (ValueError, SyntaxError):
                    continue
                if isinstance(executable, str) and Path(executable).name == client_entrypoint:
                    client_pids.add(pid)
        for line in raw.splitlines():
            for name in ("parent_task.py", "test_parent_task.py", "test_resume_turn.py"):
                if ("<" + str(fixture / name) + ">") in line and re.search(r'O_WRONLY|O_RDWR', line):
                    stamp = re.match(r"^(\d+\.\d+) ", line)
                    writes.append({"pid": pid, "file": name,
                                   "timestamp": float(stamp[1]) if stamp else None})
        text = re.sub(r"^\d+\.\d+ ", "", raw, flags=re.M)
        images = list(re.finditer(r'^execve\(("(?:[^"\\]|\\.)*"), (\[.*?\]), .*\)\s+= 0$', text, re.M))
        if not images:
            continue
        try:
            executable, argv = (ast.literal_eval(value) for value in images[-1].groups())
        except (ValueError, SyntaxError):
            continue
        timestamps = [
            float(match.group(1))
            for match in re.finditer(r"^(\d+\.\d+) ", raw, re.M)
        ]
        text = text[images[-1].end():]
        exit_match = re.search(r"^exit_group\((-?\d+)\)", text, re.M)
        opened = set(re.findall(r'= \d+<([^>]+)>', text))
        if Path(executable).resolve() != interpreter.resolve():
            continue
        if not isinstance(argv, list) or argv[1:3] != ["-m", "unittest"]:
            continue
        test_files = sorted(
            name for name in ("test_parent_task.py", "test_resume_turn.py")
            if str(fixture / name) in opened
        )
        recorder_open_verified = _recorder_opened(opened, recorder_path)
        evidence_target = evidence_path.resolve() if evidence_path is not None else None
        evidence_write_verified = bool(
            evidence_target is not None
            and any(
                f"<{evidence_target}>" in line
                and re.search(r"O_WRONLY|O_RDWR", line)
                for line in raw.splitlines()
            )
        )
        process = {
            "pid": pid,
            "interpreter": str(Path(executable).resolve()),
            "argv": [str(value) for value in argv],
            "test_files": test_files,
            "exit_code": int(exit_match.group(1)) if exit_match else None,
            "started_at": timestamps[0] if timestamps else None,
            "finished_at": timestamps[-1] if timestamps else None,
            "recorder_open_verified": recorder_open_verified,
            "evidence_write_verified": evidence_write_verified,
        }
        processes.append(process)
        if not exit_match or exit_match.group(1) != "0":
            continue
        for name in test_files:
            # A real interpreter must open this run's fixture test, not merely
            # print its name or run another directory's identically named test.
            tests.append({"pid": pid, "test": name, "exit_code": 0,
                          "interpreter_verified": True, "fixture_open_verified": True})
    if observed_process_tree:
        for parent, children in observed_process_tree.items():
            parent_children.setdefault(int(parent), set()).update(int(child) for child in children)
    if observed_client_pids:
        client_pids.update(int(pid) for pid in observed_client_pids)
    def _descends_from_client(pid: int) -> bool:
        if not client_entrypoint:
            return False
        if pid in client_pids:
            return True
        pending = list(client_pids)
        seen = set(pending)
        while pending:
            parent = pending.pop(0)
            for child in parent_children.get(parent, ()):
                if child == pid:
                    return True
                if child not in seen:
                    seen.add(child)
                    pending.append(child)
        return False

    successful_test_pids = {item["pid"] for item in tests}
    reachable_test_pids = {
        pid for pid in successful_test_pids if _descends_from_client(pid)
    }
    reverse_edges: dict[int, set[int]] = {}
    for parent, children in parent_children.items():
        for child in children:
            reverse_edges.setdefault(child, set()).add(parent)
    test_parent_candidates = {
        str(pid): sorted(reverse_edges.get(pid, ()))
        for pid in successful_test_pids
    }
    client_tree_verified = bool(client_entrypoint and client_pids)
    if client_tree_verified:
        # Every successful fixture process must be below the observed client;
        # otherwise a copied trace from an unrelated process could make a turn
        # look valid.
        client_tree_verified = bool(successful_test_pids) and len(reachable_test_pids) == len(successful_test_pids)
    return {
        "available": bool(files),
        "tests": tests,
        "writes": writes,
        "processes": processes,
        "client_entrypoint": client_entrypoint,
        "client_pids": sorted(client_pids),
        "process_tree_edges": sum(len(children) for children in parent_children.values()),
        "client_tree_verified": client_tree_verified,
        "test_pids": sorted(successful_test_pids),
        "reachable_test_pids": sorted(reachable_test_pids),
        "unmatched_test_pids": sorted(successful_test_pids - reachable_test_pids),
        "test_parent_candidates": test_parent_candidates,
        "expected_cases": list(expected_cases),
        "recorder_path": str(recorder_path.resolve()) if recorder_path is not None else None,
        "recorder_open_verified": bool(recorder_path is not None and any(
            process.get("recorder_open_verified") is True for process in processes
        )),
        "evidence_write_verified": bool(evidence_path is not None and any(
            process.get("evidence_write_verified") is True for process in processes
        )),
    }
