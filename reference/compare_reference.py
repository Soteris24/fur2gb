#!/usr/bin/env python3
"""Compare Furnace and fur2gb JSONL traces without applying audio heuristics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(path: Path):
    records = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    return records


def key(record):
    if record.get("kind") == "update":
        return ("update", record.get("update"), record.get("order"), record.get("row"))
    return (
        "write",
        record.get("update"),
        record.get("channel"),
        record.get("event"),
        record.get("register"),
        record.get("address"),
        record.get("value"),
        record.get("write_order"),
    )


def describe(record):
    if record is None:
        return "<missing>"
    return json.dumps(record, sort_keys=True)


def main():
    parser = argparse.ArgumentParser(description="Diff Furnace and fur2gb JSONL traces")
    parser.add_argument("reference", type=Path)
    parser.add_argument("actual", type=Path)
    args = parser.parse_args()

    expected = load(args.reference)
    actual = load(args.actual)
    mismatches = []
    for index in range(max(len(expected), len(actual))):
        left = expected[index] if index < len(expected) else None
        right = actual[index] if index < len(actual) else None
        if left is None or right is None or key(left) != key(right):
            mismatches.append((index, left, right))

    if not mismatches:
        print(f"TRACE PASS: {len(expected)} records match")
        return 0

    index, left, right = mismatches[0]
    print("FIRST MISMATCH")
    print(f"record: {index}")
    print(f"expected: {describe(left)}")
    print(f"actual:   {describe(right)}")
    print()
    print(f"FOLLOWING MISMATCHES ({len(mismatches) - 1})")
    for index, left, right in mismatches[1:]:
        print(f"record {index}: expected={describe(left)} actual={describe(right)}")
    print()
    print(f"TRACE FAIL: {len(mismatches)} mismatched records")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
