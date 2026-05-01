#!/usr/bin/env python3
"""A/B sweep: pit minimax v0 (baseline) against minimax v1 (improved).

Runs N games at each time control with color alternation, cycling openings.
Reports win/draw/loss and Wilson 95% CI on v1 score (win=1, draw=0.5, loss=0).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.openings import load_openings_file
from harness.run_match import (
    EngineProcess,
    EngineSpec,
    append_jsonl,
    initialize_engine,
    other_slot,
    play_game,
    restart_engine,
    send_configuration,
    summarize_game_line,
    write_json,
)

V0_SLOT = "a"
V1_SLOT = "b"
WILSON_Z_95 = 1.959964
DEFAULT_TIMES_MS = [500, 1000, 2000, 5000, 10000]
DEFAULT_GAMES_PER_CELL = 20


def wilson_ci(wins: int, draws: int, losses: int) -> tuple[float, float, float]:
    n = wins + draws + losses
    if n == 0:
        return 0.5, 0.0, 1.0
    p_hat = (wins + 0.5 * draws) / n
    z2 = WILSON_Z_95 ** 2
    center = (p_hat + z2 / (2 * n)) / (1 + z2 / n)
    margin = WILSON_Z_95 * math.sqrt((p_hat * (1 - p_hat) + z2 / (4 * n)) / n) / (1 + z2 / n)
    return p_hat, max(0.0, center - margin), min(1.0, center + margin)


def score_for_v1(record: dict[str, Any]) -> float:
    w = record["winner_slot"]
    if w is None:
        return 0.5
    return 1.0 if w == V1_SLOT else 0.0


def run_cell(
    time_ms: int,
    games: int,
    v0_proc: EngineProcess,
    v1_proc: EngineProcess,
    openings: list[dict],
    results_dir: Path,
) -> dict[str, Any]:
    runtimes = {V0_SLOT: v0_proc, V1_SLOT: v1_proc}
    wins = draws = losses = 0
    v1_depth_total = 0
    v1_search_count = 0
    v0_depth_total = 0
    v0_search_count = 0
    global_idx = 0

    for game_idx in range(1, games + 1):
        global_idx += 1
        transcript: list[dict[str, Any]] = []

        if global_idx > 1:
            restart_engine(runtimes[V0_SLOT], transcript)
            restart_engine(runtimes[V1_SLOT], transcript)
        send_configuration(runtimes[V0_SLOT], transcript)
        send_configuration(runtimes[V1_SLOT], transcript)

        opening = None if not openings else openings[(game_idx - 1) % len(openings)]
        black_slot = V1_SLOT if game_idx % 2 == 1 else V0_SLOT

        try:
            record = play_game(
                game_index=global_idx,
                runtimes=runtimes,
                black_slot=black_slot,
                transcript=transcript,
                opening=opening,
            )
        except Exception as exc:
            print(f"  game {game_idx}: FAILED ({exc})", file=sys.stderr)
            losses += 1
            restart_engine(runtimes[V0_SLOT], [])
            restart_engine(runtimes[V1_SLOT], [])
            continue

        write_json(results_dir / f"game_{time_ms}ms_{game_idx:03d}.json", record)

        score = score_for_v1(record)
        if score == 1.0:
            wins += 1
        elif score == 0.0:
            losses += 1
        else:
            draws += 1

        v1_stats = record["engine_search_stats"][V1_SLOT]
        v0_stats = record["engine_search_stats"][V0_SLOT]
        v1_depth_total += v1_stats["depth_total"]
        v1_search_count += v1_stats["search_count"]
        v0_depth_total += v0_stats["depth_total"]
        v0_search_count += v0_stats["search_count"]

        if game_idx % 5 == 0 or game_idx == games:
            print(f"  {game_idx}/{games}  W{wins} D{draws} L{losses}")

    p_hat, ci_lo, ci_hi = wilson_ci(wins, draws, losses)
    avg_v1 = v1_depth_total / v1_search_count if v1_search_count else 0
    avg_v0 = v0_depth_total / v0_search_count if v0_search_count else 0

    return {
        "time_ms": time_ms,
        "games": games,
        "v1_wins": wins,
        "draws": draws,
        "v1_losses": losses,
        "v1_score": round(p_hat, 3),
        "ci_95_lo": round(ci_lo, 3),
        "ci_95_hi": round(ci_hi, 3),
        "avg_v1_depth": round(avg_v1, 1),
        "avg_v0_depth": round(avg_v0, 1),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--times", nargs="+", type=int, default=DEFAULT_TIMES_MS)
    parser.add_argument("--games", type=int, default=DEFAULT_GAMES_PER_CELL)
    parser.add_argument("--openings", type=str, default="results/crazy_sensei_openings_253.json")
    args = parser.parse_args()

    harness_root = Path(__file__).resolve().parents[1]
    openings = []
    opath = harness_root / args.openings
    if opath.exists():
        openings = load_openings_file(opath)
        print(f"Loaded {len(openings)} openings")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_dir = harness_root / "results" / f"v0_v1_sweep_{timestamp}"
    results_dir.mkdir(parents=True, exist_ok=True)

    print(f"minimax v0 (baseline) vs v1 (improved) A/B sweep")
    print(f"Time controls: {args.times} ms")
    print(f"Games per cell: {args.games}")
    print(f"Results dir: {results_dir}")
    print()

    all_rows = []

    for time_ms in args.times:
        print(f"=== {time_ms} ms ===")

        v0_spec = EngineSpec(
            slot=V0_SLOT, name="v0", command=f"./adapters/minimax-v0/run.sh --controller expert --time-ms {time_ms}",
            turn_timeout_ms=time_ms, match_timeout_ms=None, max_memory_bytes=None,
        )
        v1_spec = EngineSpec(
            slot=V1_SLOT, name="v1", command=f"./adapters/minimax-v1/run.sh --controller expert --time-ms {time_ms}",
            turn_timeout_ms=time_ms, match_timeout_ms=None, max_memory_bytes=None,
        )

        v0_proc = EngineProcess(v0_spec)
        v1_proc = EngineProcess(v1_spec)

        try:
            initialize_engine(v0_proc, [])
            initialize_engine(v1_proc, [])

            row = run_cell(time_ms, args.games, v0_proc, v1_proc, openings, results_dir)
            all_rows.append(row)
            print(f"  => W{row['v1_wins']} D{row['draws']} L{row['v1_losses']}  "
                  f"score={row['v1_score']:.3f}  CI=[{row['ci_95_lo']:.3f}, {row['ci_95_hi']:.3f}]  "
                  f"depth v1={row['avg_v1_depth']} v0={row['avg_v0_depth']}")
        finally:
            v0_proc.shutdown()
            v1_proc.shutdown()

    csv_path = results_dir / "summary.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_rows[0].keys() if all_rows else [])
        writer.writeheader()
        writer.writerows(all_rows)

    write_json(results_dir / "summary.json", all_rows)
    print(f"\nSummary: {csv_path}")


if __name__ == "__main__":
    main()
