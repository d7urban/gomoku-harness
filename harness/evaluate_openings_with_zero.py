#!/usr/bin/env python3
"""Evaluate harness openings with GomokuZero MCTS and write balanced subsets."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
os.environ.setdefault("TF_FORCE_GPU_ALLOW_GROWTH", "true")

INITIAL_CWD = Path.cwd()
REPO_ROOT = Path(__file__).resolve().parents[1]
CODE_ROOT = REPO_ROOT.parent
ZERO_REPO = CODE_ROOT / "GomokuZero-player"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(ZERO_REPO) not in sys.path:
    sys.path.insert(0, str(ZERO_REPO))

from harness.openings import OPENINGS_FORMAT, OPENINGS_VERSION, load_openings_file, write_openings_file

# GomokuZero discovers weights relative to its repository root.
os.chdir(ZERO_REPO)

from entrypoint_shared import AI_MCTS_BATCH, load_model_and_predict_fn, select_weights  # noqa: E402
from gomoku import BOARD_SIZE, PLAYER1, GomokuGame, encode_state, mcts_policy, mcts_search_batched  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Input harness openings file.")
    parser.add_argument("--output-dir", type=Path, required=True, help="Directory for evaluated and balanced output files.")
    parser.add_argument("--sims", type=int, default=2000, help="MCTS simulations per opening.")
    parser.add_argument("--balanced-threshold", type=float, action="append", default=[0.10, 0.15, 0.20], help="Absolute black-value thresholds to emit. May be repeated.")
    parser.add_argument("--weights", type=str, default=None, help="Optional explicit GomokuZero weights file.")
    args = parser.parse_args()
    if args.sims < 1:
        parser.error("--sims must be >= 1")
    if any(threshold < 0 for threshold in args.balanced_threshold):
        parser.error("--balanced-threshold values must be >= 0")
    if not args.input.is_absolute():
        args.input = INITIAL_CWD / args.input
    if not args.output_dir.is_absolute():
        args.output_dir = INITIAL_CWD / args.output_dir
    return args


def make_game(opening: dict[str, Any]) -> GomokuGame:
    game = GomokuGame()
    for move in opening["moves"]:
        reward, done = game.make_move(int(move["y"]), int(move["x"]))
        if done:
            raise ValueError(f"opening {opening['id']} ended game while applying {move}: reward={reward}")
    return game


def best_move_from_root(root) -> dict[str, Any]:
    policy = mcts_policy(root, temperature=0.05)
    index = int(policy.argmax())
    row, col = divmod(index, BOARD_SIZE)
    child = root.children.get((row, col))
    return {
        "x": col,
        "y": row,
        "visits": 0 if child is None else int(child.visit_count),
        "q": None if child is None or child.visit_count == 0 else float(child.q_value),
    }


def evaluate_opening(opening: dict[str, Any], predict_fn, sims: int) -> dict[str, Any]:
    game = make_game(opening)
    state = encode_state(game)[None, ...]
    _, raw_value = predict_fn(state)
    raw_current_value = float(raw_value.ravel()[0])
    raw_black_value = raw_current_value if game.current_player == PLAYER1 else -raw_current_value

    started = time.perf_counter()
    root = mcts_search_batched(
        game,
        predict_fn,
        num_simulations=sims,
        batch_size=AI_MCTS_BATCH,
        c_puct=1.5,
        add_noise=False,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    current_value = float(root.q_value)
    black_value = current_value if game.current_player == PLAYER1 else -current_value

    evaluated = dict(opening)
    evaluated["zero_eval"] = {
        "sims": sims,
        "elapsed_ms": elapsed_ms,
        "side_to_move": "black" if game.current_player == PLAYER1 else "white",
        "current_player_value": current_value,
        "black_value": black_value,
        "abs_black_value": abs(black_value),
        "raw_current_player_value": raw_current_value,
        "raw_black_value": raw_black_value,
        "best_move": best_move_from_root(root),
        "root_visits": int(root.visit_count),
    }
    return evaluated


def payload_base(args: argparse.Namespace, *, weights_file: str, weight_label: str, openings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "format": OPENINGS_FORMAT,
        "version": OPENINGS_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_openings_file": str(args.input),
        "evaluator": "GomokuZero-player",
        "weights": weights_file,
        "weights_label": weight_label,
        "sims": args.sims,
        "opening_count": len(openings),
        "openings": openings,
    }


def write_summary(path: Path, *, evaluated: list[dict[str, Any]], thresholds: list[float], output_files: dict[float, Path]) -> None:
    values = [float(opening["zero_eval"]["black_value"]) for opening in evaluated]
    abs_values = sorted(abs(value) for value in values)
    lines = [
        "# GomokuZero Opening Evaluation",
        "",
        f"- Openings: {len(evaluated)}",
        f"- Min black value: {min(values):+.3f}",
        f"- Max black value: {max(values):+.3f}",
        f"- Median abs black value: {abs_values[len(abs_values) // 2]:.3f}",
        "",
        "## Balanced Outputs",
        "",
        "| threshold | openings | file |",
        "|---:|---:|:---|",
    ]
    for threshold in thresholds:
        count = sum(1 for opening in evaluated if float(opening["zero_eval"]["abs_black_value"]) <= threshold)
        lines.append(f"| {threshold:.3f} | {count} | `{output_files[threshold].name}` |")
    lines.extend(
        [
            "",
            "Value convention: `black_value` is positive when GomokuZero prefers Black, negative when it prefers White.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    openings = load_openings_file(args.input)
    weights_file, weight_label = (args.weights, "explicit") if args.weights else select_weights(mode="best")
    if not weights_file:
        raise SystemExit("no GomokuZero weights file found")

    print(f"loading GomokuZero weights: {weights_file} ({weight_label})", flush=True)
    load_started = time.perf_counter()
    _, predict_fn = load_model_and_predict_fn(weights_file)
    print(f"weights loaded in {time.perf_counter() - load_started:.1f}s", flush=True)

    evaluated: list[dict[str, Any]] = []
    run_started = time.perf_counter()
    for index, opening in enumerate(openings, start=1):
        evaluated_opening = evaluate_opening(opening, predict_fn, args.sims)
        evaluated.append(evaluated_opening)
        ze = evaluated_opening["zero_eval"]
        print(
            f"[{index:03d}/{len(openings)}] {opening['id']} "
            f"stm={ze['side_to_move']} black_value={ze['black_value']:+.3f} "
            f"abs={ze['abs_black_value']:.3f} t={ze['elapsed_ms']}ms",
            flush=True,
        )

    evaluated.sort(key=lambda opening: float(opening["zero_eval"]["abs_black_value"]))
    evaluated_path = args.output_dir / f"crazy_sensei_openings_zero_evaluated_s{args.sims}.json"
    write_openings_file(
        evaluated_path,
        payload_base(args, weights_file=weights_file, weight_label=weight_label, openings=evaluated),
    )

    output_files: dict[float, Path] = {}
    thresholds = sorted(set(float(threshold) for threshold in args.balanced_threshold))
    for threshold in thresholds:
        balanced = [
            dict(opening)
            for opening in evaluated
            if float(opening["zero_eval"]["abs_black_value"]) <= threshold
        ]
        output_path = args.output_dir / f"crazy_sensei_openings_zero_balanced_s{args.sims}_abs{int(round(threshold * 1000)):03d}.json"
        output_files[threshold] = output_path
        payload = payload_base(args, weights_file=weights_file, weight_label=weight_label, openings=balanced)
        payload["balanced"] = True
        payload["balance_method"] = "gomokuzero_mcts_black_value"
        payload["balance_threshold"] = threshold
        write_openings_file(output_path, payload)

    summary_path = args.output_dir / f"crazy_sensei_openings_zero_evaluation_s{args.sims}.md"
    write_summary(summary_path, evaluated=evaluated, thresholds=thresholds, output_files=output_files)

    print(f"evaluated={evaluated_path}")
    for threshold in thresholds:
        print(f"balanced_abs<={threshold:.3f}={output_files[threshold]}")
    print(f"summary={summary_path}")
    print(f"total_time={time.perf_counter() - run_started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
