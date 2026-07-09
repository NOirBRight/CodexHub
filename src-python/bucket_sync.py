import argparse
import json
import shutil
from pathlib import Path

from atomic_io import atomic_write_bytes


def equivalent_line_key(line: bytes) -> bytes:
    try:
        item = json.loads(line)
    except Exception:
        return line

    if item.get("type") == "session_meta":
        payload = item.get("payload")
        if isinstance(payload, dict) and "model_provider" in payload:
            item = dict(item)
            payload = dict(payload)
            payload["model_provider"] = "<provider>"
            item["payload"] = payload

    return json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def lines_equivalent(left: bytes, right: bytes) -> bool:
    return left == right or equivalent_line_key(left) == equivalent_line_key(right)


def common_prefix_len(left: list[bytes], right: list[bytes]) -> int:
    count = 0
    for left_line, right_line in zip(left, right):
        if not lines_equivalent(left_line, right_line):
            break
        count += 1
    return count


def suffix_prefix_overlap_len(left: list[bytes], right: list[bytes]) -> int:
    limit = min(len(left), len(right))
    for size in range(limit, 0, -1):
        if all(lines_equivalent(a, b) for a, b in zip(left[-size:], right[:size])):
            return size
    return 0


def merged_jsonl_bytes(source: Path, destination: Path) -> bytes | None:
    source_lines = source.read_bytes().splitlines(keepends=True)
    destination_lines = destination.read_bytes().splitlines(keepends=True)

    if source_lines == destination_lines:
        return None

    prefix = common_prefix_len(source_lines, destination_lines)
    if prefix == len(source_lines):
        return None
    if prefix == len(destination_lines):
        return b"".join(destination_lines + source_lines[prefix:])

    destination_extra = destination_lines[prefix:]
    source_extra = source_lines[prefix:]
    overlap = suffix_prefix_overlap_len(destination_extra, source_extra)
    merged = destination_lines[:prefix] + destination_extra + source_extra[overlap:]
    return b"".join(merged)


def copy_file_atomic(source: Path, destination: Path) -> None:
    atomic_write_bytes(destination, source.read_bytes())
    shutil.copystat(source, destination)


def sync_file(source: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        copy_file_atomic(source, destination)
        return "copied"

    if source.suffix.lower() != ".jsonl":
        copy_file_atomic(source, destination)
        return "overwritten"

    if (
        source.stat().st_size == destination.stat().st_size
        and int(source.stat().st_mtime) == int(destination.stat().st_mtime)
    ):
        return "unchanged"

    merged = merged_jsonl_bytes(source, destination)
    if merged is None:
        return "kept-destination"

    atomic_write_bytes(destination, merged)
    return "merged"


def sync_dir(source: Path, destination: Path) -> dict[str, int]:
    if not source.exists():
        return {}
    if not source.is_dir():
        raise NotADirectoryError(source)

    counts: dict[str, int] = {}
    for path in source.rglob("*"):
        if path.is_dir():
            (destination / path.relative_to(source)).mkdir(parents=True, exist_ok=True)
            continue
        result = sync_file(path, destination / path.relative_to(source))
        counts[result] = counts.get(result, 0) + 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely sync Codex bucket directories.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_dir_parser = subparsers.add_parser("sync-dir")
    sync_dir_parser.add_argument("--source", required=True)
    sync_dir_parser.add_argument("--destination", required=True)

    args = parser.parse_args()
    if args.command == "sync-dir":
        counts = sync_dir(Path(args.source), Path(args.destination))
        summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
        print(summary or "no files")
        return 0

    raise ValueError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
