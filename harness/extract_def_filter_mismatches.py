#!/usr/bin/env python3
"""Extract defensive-filter mismatch positions from round-robin game logs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

BOARD_SIZE = 15


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_dir", type=Path, help="Round-robin result directory containing per-game JSON logs.")
    parser.add_argument("--out", type=Path, default=None, help="Output JSONL path. Defaults to results_dir/def_filter_mismatches.jsonl.")
    parser.add_argument("--include-non-filtered", action="store_true", help="Also include no-filter move differences that were not removed by the filter.")
    return parser.parse_args()


def empty_board() -> list[list[str]]:
    return [["." for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]


def board_rows(board: list[list[str]]) -> list[str]:
    return ["".join(row) for row in board]


def move_token(move: dict[str, Any]) -> str:
    return f"{move['x']},{move['y']}"


def record_move_token(record: dict[str, Any]) -> str:
    move = record.get("move", {})
    return f"{move.get('x')},{move.get('y')}"


def iter_game_paths(results_dir: Path) -> list[Path]:
    return sorted(path for path in results_dir.glob("*_vs_*/*.json") if path.name.startswith("game_"))


def extract_game(path: Path, include_non_filtered: bool) -> list[dict[str, Any]]:
    game = json.loads(path.read_text(encoding="utf-8"))
    records_by_slot: dict[str, list[dict[str, Any]]] = {
        slot: list(records)
        for slot, records in game.get("engine_search_records", {}).items()
    }
    record_index = {slot: 0 for slot in records_by_slot}
    board = empty_board()
    moves_before: list[dict[str, Any]] = []
    extracted: list[dict[str, Any]] = []

    for move in game.get("moves", []):
        slot = str(move.get("engine_slot", ""))
        record: dict[str, Any] | None = None
        if slot in records_by_slot:
            index = record_index[slot]
            if index < len(records_by_slot[slot]):
                candidate = records_by_slot[slot][index]
                if record_move_token(candidate) == move_token(move):
                    record = candidate
                    record_index[slot] = index + 1

        if record is not None:
            filtered_best = int(record.get("filtered_best", 0)) != 0
            nofilter_diff = int(record.get("nofilter_diff", 0)) != 0
            if nofilter_diff and (filtered_best or include_non_filtered):
                extracted.append(
                    {
                        "source_game": str(path),
                        "game_index": game.get("game_index"),
                        "ply": move.get("ply"),
                        "side": move.get("color"),
                        "engine_slot": slot,
                        "engine_name": move.get("engine_name"),
                        "chosen_move": {"x": move.get("x"), "y": move.get("y")},
                        "no_def_filter_move": record.get("nofilter"),
                        "filtered_out_no_def_filter_move": filtered_best,
                        "no_def_filter_move_was_in_before_set": int(record.get("nofilter_in_before", 0)) != 0,
                        "def_filter_reason": record.get("def_reason"),
                        "def_filter_before": record.get("def_before"),
                        "def_filter_after": record.get("def_after"),
                        "def_filter_removed": record.get("def_removed"),
                        "score": record.get("score"),
                        "depth": record.get("depth"),
                        "nodes": record.get("nodes"),
                        "opening_id": None if game.get("opening") is None else game["opening"].get("id"),
                        "result": game.get("result"),
                        "winner_name": game.get("winner_name"),
                        "winner_color": game.get("winner_color"),
                        "board_before_rows": board_rows(board),
                        "moves_before": list(moves_before),
                    }
                )

        x = int(move["x"])
        y = int(move["y"])
        board[y][x] = "X" if move.get("color") == "black" else "O"
        moves_before.append(
            {
                "ply": move.get("ply"),
                "color": move.get("color"),
                "x": x,
                "y": y,
            }
        )

    return extracted


def main() -> int:
    args = parse_args()
    out_path = args.out or (args.results_dir / "def_filter_mismatches.jsonl")
    rows: list[dict[str, Any]] = []
    for path in iter_game_paths(args.results_dir):
        rows.extend(extract_game(path, args.include_non_filtered))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    print(f"wrote {len(rows)} mismatch positions to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
