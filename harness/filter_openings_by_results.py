#!/usr/bin/env python3
"""Build a color-balanced opening subset from completed harness results."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.openings import OPENINGS_FORMAT, OPENINGS_VERSION, load_openings_file, write_openings_file


@dataclass
class OpeningResult:
    opening_id: str
    games: int = 0
    black_wins: int = 0
    white_wins: int = 0
    draws: int = 0

    def black_score(self) -> float:
        if self.games == 0:
            return 0.5
        return (self.black_wins + 0.5 * self.draws) / self.games

    def deviation(self) -> float:
        return abs(self.black_score() - 0.5)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openings-file", type=Path, required=True, help="Source harness openings JSON file.")
    parser.add_argument("--results-dir", type=Path, action="append", required=True, help="Completed result directory containing games.jsonl. May be repeated.")
    parser.add_argument("--output", type=Path, required=True, help="Output harness openings JSON file.")
    parser.add_argument("--min-games", type=int, default=4, help="Minimum result games required for an opening.")
    parser.add_argument("--max-black-score-deviation", type=float, default=0.20, help="Keep openings with |black_score - 0.5| at or below this value.")
    parser.add_argument("--max-unique", type=int, default=32, help="Maximum openings to keep after sorting by balance.")
    args = parser.parse_args()
    if args.min_games < 1:
        parser.error("--min-games must be >= 1")
    if args.max_black_score_deviation < 0:
        parser.error("--max-black-score-deviation must be >= 0")
    if args.max_unique < 1:
        parser.error("--max-unique must be >= 1")
    return args


def update_result_from_game(result: OpeningResult, game: dict[str, Any]) -> None:
    result.games += 1
    winner = game.get("winner_preset")
    if winner is None:
        result.draws += 1
    elif winner == game.get("black_preset"):
        result.black_wins += 1
    else:
        result.white_wins += 1


def load_result_stats(results_dirs: list[Path]) -> dict[str, OpeningResult]:
    stats: dict[str, OpeningResult] = {}
    for results_dir in results_dirs:
        games_path = results_dir / "games.jsonl"
        if not games_path.exists():
            raise FileNotFoundError(f"missing games.jsonl: {games_path}")
        for line_number, line in enumerate(games_path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            game = json.loads(line)
            opening_id = game.get("opening_id")
            if opening_id is None:
                continue
            result = stats.setdefault(str(opening_id), OpeningResult(str(opening_id)))
            update_result_from_game(result, game)
    return stats


def result_sort_key(result: OpeningResult, source_order: dict[str, int]) -> tuple[float, int, str]:
    return (result.deviation(), -result.games, f"{source_order.get(result.opening_id, 10**9):09d}:{result.opening_id}")


def make_output_payload(
    *,
    openings_file: Path,
    results_dirs: list[Path],
    selected: list[dict[str, Any]],
    selected_stats: list[OpeningResult],
    args: argparse.Namespace,
) -> dict[str, Any]:
    return {
        "format": OPENINGS_FORMAT,
        "version": OPENINGS_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_openings_file": str(openings_file),
        "source_results_dirs": [str(path) for path in results_dirs],
        "balanced": True,
        "balance_method": "result_black_score",
        "selection": {
            "min_games": args.min_games,
            "max_black_score_deviation": args.max_black_score_deviation,
            "max_unique": args.max_unique,
        },
        "unique_opening_count": len(selected),
        "opening_count": len(selected),
        "opening_stats": [
            {
                "id": stat.opening_id,
                "games": stat.games,
                "black_wins": stat.black_wins,
                "white_wins": stat.white_wins,
                "draws": stat.draws,
                "black_score": stat.black_score(),
                "deviation": stat.deviation(),
            }
            for stat in selected_stats
        ],
        "openings": selected,
    }


def main() -> int:
    args = parse_args()
    openings = load_openings_file(args.openings_file)
    by_id = {str(opening["id"]): opening for opening in openings}
    source_order = {str(opening["id"]): index for index, opening in enumerate(openings)}
    stats = load_result_stats(args.results_dir)

    candidates = [
        result
        for opening_id, result in stats.items()
        if opening_id in by_id
        and result.games >= args.min_games
        and result.deviation() <= args.max_black_score_deviation
    ]
    candidates.sort(key=lambda result: result_sort_key(result, source_order))
    selected_stats = candidates[: args.max_unique]
    if not selected_stats:
        raise SystemExit("no openings matched the requested result-balance filter")

    selected = [dict(by_id[result.opening_id]) for result in selected_stats]
    payload = make_output_payload(
        openings_file=args.openings_file,
        results_dirs=args.results_dir,
        selected=selected,
        selected_stats=selected_stats,
        args=args,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_openings_file(args.output, payload)

    print(f"source_openings={args.openings_file}")
    print(f"source_results={', '.join(str(path) for path in args.results_dir)}")
    print(f"observed_openings={len(stats)}")
    print(f"selected_openings={len(selected)}")
    for result in selected_stats:
        print(
            f"{result.opening_id}: games={result.games} "
            f"black={result.black_wins} white={result.white_wins} draws={result.draws} "
            f"black_score={result.black_score():.3f} deviation={result.deviation():.3f}"
        )
    print(f"output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
