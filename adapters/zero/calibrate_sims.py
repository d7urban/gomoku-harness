#!/usr/bin/env python3
"""Calibrate SIMS_PER_MS for the GomokuZero adapter on this machine.

Times AIPlayer.get_move at a range of sim counts on representative positions,
fits a linear sims = a + b*ms model, and reports the recommended SIMS_PER_MS.
"""

from __future__ import annotations

import os
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
os.environ.setdefault("TF_FORCE_GPU_ALLOW_GROWTH", "true")

ZERO_REPO = Path(__file__).resolve().parents[2].parent / "GomokuZero-player"
sys.path.insert(0, str(ZERO_REPO))
os.chdir(ZERO_REPO)

from gomoku import GomokuGame  # noqa: E402
from entrypoint_shared import (  # noqa: E402
    AIPlayer,
    load_model_and_predict_fn,
    select_weights,
)

SIM_LADDER = [100, 250, 500, 1000, 2000, 4000]
TRIALS_PER_RUNG = 3


def midgame_position() -> GomokuGame:
    g = GomokuGame()
    # A natural-looking opening sequence; alternates internally.
    for r, c in [(7, 7), (7, 8), (8, 7), (8, 8), (6, 7), (9, 8), (7, 6), (8, 9)]:
        g.make_move(r, c)
    return g


def time_get_move(ai: AIPlayer, game_factory, sims: int) -> float:
    ai.sims = sims
    samples: list[float] = []
    for _ in range(TRIALS_PER_RUNG):
        g = game_factory()
        t0 = time.perf_counter()
        ai.get_move(g)
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples)


def main() -> int:
    wf, label = select_weights(mode="best")
    if not wf:
        print("no weights file found", file=sys.stderr)
        return 1
    print(f"weights: {wf} ({label})")
    print("loading model...", flush=True)
    t0 = time.time()
    _model, predict_fn = load_model_and_predict_fn(wf)
    print(f"  loaded in {time.time() - t0:.1f}s")
    ai = AIPlayer(predict_fn, simulations=64, difficulty="calib")

    # Warmup: first MCTS call pays JIT / cudnn autotune cost.
    print("warmup...", flush=True)
    g = GomokuGame()
    g.make_move(7, 7)
    ai.sims = 64
    ai.get_move(g)
    ai.get_move(midgame_position())

    rows: list[tuple[str, int, float, float]] = []
    for label_pos, factory in [("empty", GomokuGame), ("midgame", midgame_position)]:
        for sims in SIM_LADDER:
            secs = time_get_move(ai, factory, sims)
            ms = secs * 1000.0
            rate = sims / ms if ms > 0 else float("inf")
            rows.append((label_pos, sims, ms, rate))
            print(f"  {label_pos:>7s} sims={sims:5d}  {ms:7.1f} ms  -> {rate:5.2f} sims/ms")

    # Fit sims = a + b*ms on the midgame samples (more representative).
    midgame_rows = [(ms, sims) for label_pos, sims, ms, _ in rows if label_pos == "midgame"]
    n = len(midgame_rows)
    sx = sum(ms for ms, _ in midgame_rows)
    sy = sum(sims for _, sims in midgame_rows)
    sxx = sum(ms * ms for ms, _ in midgame_rows)
    sxy = sum(ms * sims for ms, sims in midgame_rows)
    denom = n * sxx - sx * sx
    if denom == 0:
        print("degenerate fit (all timings equal)", file=sys.stderr)
        return 1
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n

    # Average sims/ms across the upper half of the ladder where overhead matters less.
    upper = [r for _, sims, ms, r in rows if sims >= 500 and ms > 0]
    avg_rate = sum(upper) / len(upper)

    print()
    print(f"linear fit (midgame): sims = {intercept:.1f} + {slope:.3f} * ms")
    print(f"average sims/ms (sims >= 500, both positions): {avg_rate:.3f}")
    print()
    print(f"recommended SIMS_PER_MS = {slope:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
