#!/usr/bin/env python3
"""Build harness opening files from a Crazy Sensei crawl manifest."""

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

from harness.openings import (  # noqa: E402
    OPENINGS_FORMAT,
    OPENINGS_VERSION,
    canonical_key,
    normalize_opening_entry,
    write_openings_file,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="Crazy Sensei crawl manifest.json")
    parser.add_argument("--output-all", type=Path, default=None, help="Output path for the full harness openings file.")
    parser.add_argument("--output-curated", type=Path, default=None, help="Output path for the symmetry-deduped harness openings file.")
    parser.add_argument("--min-ply", type=int, default=1, help="Minimum opening ply to include.")
    parser.add_argument("--max-ply", type=int, default=99, help="Maximum opening ply to include.")
    parser.add_argument("--max-curated", type=int, default=128, help="Optional cap on the curated opening count (0 = no cap, default: 128).")
    args = parser.parse_args()
    if args.min_ply < 1:
        parser.error("--min-ply must be >= 1")
    if args.max_ply < args.min_ply:
        parser.error("--max-ply must be >= --min-ply")
    if args.max_curated < 0:
        parser.error("--max-curated must be >= 0")
    if args.output_all is None:
        args.output_all = args.manifest.parent / "harness_openings_all.json"
    if args.output_curated is None:
        args.output_curated = args.manifest.parent / "harness_openings_curated.json"
    return args


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("manifest must be a JSON object")
    seeds = payload.get("seeds")
    if not isinstance(seeds, list):
        raise ValueError("manifest must contain a seeds list")
    return payload


def build_entry(seed: dict[str, Any], source: str | None) -> dict[str, Any]:
    moves = [{"x": int(move["x"]), "y": int(move["y"])} for move in seed["moves"]]
    entry = normalize_opening_entry(
        {
            "id": f"crazy-sensei-{seed['path'].replace(',', '__')}",
            "name": seed["path"].replace(",", " "),
            "moves": moves,
            "source_path": seed.get("path"),
            "source_url": seed.get("url"),
            "source_save_file": seed.get("save_file"),
            "source": source,
            "last_move_value": seed.get("last_move_value"),
            "last_move_size": seed.get("last_move_size"),
            "path_min_size": seed.get("path_min_size"),
            "terminal_reason": seed.get("terminal_reason"),
        }
    )
    entry["canonical_key"] = canonical_key(entry["moves"])
    return entry


def openings_payload(*, source_manifest: Path, source: str | None, openings: list[dict[str, Any]], curated: bool) -> dict[str, Any]:
    return {
        "format": OPENINGS_FORMAT,
        "version": OPENINGS_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "source_manifest": str(source_manifest),
        "curated": curated,
        "opening_count": len(openings),
        "openings": openings,
    }


def representative_key(entry: dict[str, Any]) -> tuple[int, int, int, str]:
    return (
        -(entry.get("path_min_size") or 0),
        -(entry.get("last_move_size") or 0),
        -entry["ply"],
        str(entry.get("source_path") or entry["id"]),
    )


def main() -> int:
    args = parse_args()
    manifest = load_manifest(args.manifest)

    all_openings: list[dict[str, Any]] = []
    for seed in manifest["seeds"]:
        entry = build_entry(seed, manifest.get("source"))
        if not (args.min_ply <= entry["ply"] <= args.max_ply):
            continue
        all_openings.append(entry)

    all_openings.sort(key=lambda entry: (entry["ply"], str(entry.get("source_path") or entry["id"])))

    by_symmetry: dict[str, list[dict[str, Any]]] = {}
    for entry in all_openings:
        by_symmetry.setdefault(entry["canonical_key"], []).append(entry)

    curated_openings: list[dict[str, Any]] = []
    for key, group in by_symmetry.items():
        representative = min(group, key=representative_key)
        curated = dict(representative)
        curated["symmetry_group_size"] = len(group)
        curated["symmetry_group_paths"] = sorted(str(item.get("source_path") or item["id"]) for item in group)
        curated_openings.append(curated)

    curated_openings.sort(
        key=lambda entry: (
            -entry["symmetry_group_size"],
            -entry["ply"],
            -(entry.get("path_min_size") or 0),
            str(entry.get("source_path") or entry["id"]),
        )
    )
    if args.max_curated > 0:
        curated_openings = curated_openings[: args.max_curated]

    args.output_all.parent.mkdir(parents=True, exist_ok=True)
    args.output_curated.parent.mkdir(parents=True, exist_ok=True)
    write_openings_file(
        args.output_all,
        openings_payload(
            source_manifest=args.manifest,
            source=manifest.get("source"),
            openings=all_openings,
            curated=False,
        ),
    )
    write_openings_file(
        args.output_curated,
        openings_payload(
            source_manifest=args.manifest,
            source=manifest.get("source"),
            openings=curated_openings,
            curated=True,
        ),
    )

    print(f"input_manifest={args.manifest}")
    print(f"all_openings={len(all_openings)}")
    print(f"symmetry_groups={len(by_symmetry)}")
    print(f"curated_openings={len(curated_openings)}")
    print(f"output_all={args.output_all}")
    print(f"output_curated={args.output_curated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
