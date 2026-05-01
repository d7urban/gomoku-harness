#!/usr/bin/env python3
"""Build a balanced harness opening set from an existing harness openings file.

The output duplicates each selected opening in adjacent slots so the current sweep
scheduler plays it once with minimax as white and once with minimax as black.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.openings import OPENINGS_FORMAT, OPENINGS_VERSION, load_openings_file, write_openings_file  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Input harness openings JSON file.")
    parser.add_argument("--output", type=Path, default=None, help="Output path for the balanced openings file.")
    parser.add_argument("--max-abs-value", type=float, default=0.15, help="Keep only openings with |last_move_value| <= this threshold.")
    parser.add_argument("--min-ply", type=int, default=4, help="Minimum ply to keep.")
    parser.add_argument("--max-unique", type=int, default=16, help="Maximum number of unique openings before pair-duplication.")
    args = parser.parse_args()
    if args.max_abs_value < 0:
        parser.error("--max-abs-value must be >= 0")
    if args.min_ply < 1:
        parser.error("--min-ply must be >= 1")
    if args.max_unique < 1:
        parser.error("--max-unique must be >= 1")
    if args.output is None:
        args.output = args.input.with_name("harness_openings_balanced.json")
    return args


def balance_key(entry: dict[str, Any]) -> tuple[float, int, str]:
    value = abs(float(entry.get("last_move_value", 0.0)))
    return (value, -int(entry["ply"]), str(entry.get("source_path") or entry["id"]))


def make_output_payload(args: argparse.Namespace, selected: list[dict[str, Any]], paired: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "format": OPENINGS_FORMAT,
        "version": OPENINGS_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_openings_file": str(args.input),
        "balanced": True,
        "pairing": "adjacent_duplicate",
        "selection": {
            "max_abs_value": args.max_abs_value,
            "min_ply": args.min_ply,
            "max_unique": args.max_unique,
        },
        "unique_opening_count": len(selected),
        "opening_count": len(paired),
        "openings": paired,
    }


def main() -> int:
    args = parse_args()
    openings = load_openings_file(args.input)

    candidates = []
    for opening in openings:
        value = opening.get("last_move_value")
        if value is None:
            continue
        if abs(float(value)) > args.max_abs_value:
            continue
        if int(opening["ply"]) < args.min_ply:
            continue
        candidates.append(dict(opening))

    candidates.sort(key=balance_key)
    selected = candidates[: args.max_unique]
    if not selected:
        raise SystemExit("no openings matched the requested balance filter")

    paired: list[dict[str, Any]] = []
    for opening in selected:
        paired.append(dict(opening))
        paired.append(dict(opening))

    payload = make_output_payload(args, selected, paired)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_openings_file(args.output, payload)

    print(f"input={args.input}")
    print(f"candidates={len(candidates)}")
    print(f"unique_openings={len(selected)}")
    print(f"paired_openings={len(paired)}")
    print(f"output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
