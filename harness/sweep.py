#!/usr/bin/env python3
"""Calibration sweep: compare minimax-expert against GomokuZero across time controls.

Drives a single Zero difficulty (easy=250, medium=500, hard=2000 sims) against a
schedule of minimax-expert time-per-move values. Both engines are spawned once
and reused via RESTART; the per-cell minimax budget is pushed through
`INFO timeout_turn` between games. Each cell runs N games with color alternation
and is summarized with a Wilson 95% CI on the minimax score (chess scoring:
win=1, draw=0.5, loss=0). After the coarse pass an optional bisect adds
log-spaced midpoints around the 50% crossing until either the target CI width
is met or the bisect budget is exhausted. In protocol v2, the sweep can instead
run named period-clock presets (`blitz`, `fast`, `slow`) by sending live
`time_left` / `moves_to_reset` updates before each move.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.openings import load_openings_file
from harness.run_match import (
    EngineFailure,
    EngineProcess,
    EngineSpec,
    PROTOCOL_V1,
    PROTOCOL_V2,
    append_jsonl,
    initialize_engine,
    play_game,
    restart_engine,
    send_configuration,
    write_json,
)

DIFFICULTY_TO_SIMS = {"easy": 250, "medium": 500, "hard": 2000}
DEFAULT_TIMES_MS = (50, 100, 250, 500, 1000, 2500, 5000)
DEFAULT_GAMES_PER_CELL = 10
DEFAULT_BISECT_ROUNDS = 3
DEFAULT_BISECT_TARGET_CI_WIDTH = 0.30
DEFAULT_ZERO_TURN_TIMEOUT_MS = 60_000
DEFAULT_ZERO_CMD = "./adapters/zero/run.sh"
DEFAULT_MINIMAX_CMD = "./adapters/minimax/run.sh --controller expert --time-ms 1000"
WILSON_Z_95 = 1.959964
ZERO_SLOT = "a"
MINIMAX_SLOT = "b"
PRESET_SPECS = {
    "blitz": {"period_time_ms": 5 * 60 * 1000, "period_moves": 40},
    "fast": {"period_time_ms": 15 * 60 * 1000, "period_moves": 60},
    "slow": {"period_time_ms": 60 * 60 * 1000, "period_moves": 60},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--difficulty", required=True, choices=sorted(DIFFICULTY_TO_SIMS), help="GomokuZero difficulty (drives --sims).")
    parser.add_argument("--zero-cmd", default=DEFAULT_ZERO_CMD, help="Base command to launch the Zero adapter; --sims N is appended.")
    parser.add_argument("--zero-cmd-no-sims", action="store_true", help="If set, do NOT append --sims to the Zero command (use when the user wires sims into --zero-cmd themselves).")
    parser.add_argument("--minimax-cmd", default=DEFAULT_MINIMAX_CMD, help="Command to launch the minimax adapter; numeric v1 cells push INFO timeout_turn, while v2 preset cells use live clock INFO updates.")
    parser.add_argument("--protocol-version", choices=(PROTOCOL_V1, PROTOCOL_V2), default=PROTOCOL_V1, help="Sweep wire-protocol profile: v1 keeps the original fixed-turn sweep; v2 sends live clock updates for preset period controls.")
    parser.add_argument("--zero-name", default="zero", help="Friendly name for the Zero engine.")
    parser.add_argument("--minimax-name", default="minimax-expert", help="Friendly name for the minimax engine.")
    parser.add_argument("--times-ms", default=None, help="Comma-separated list of minimax per-move times to sweep, in ms. Default in protocol v1: 50,100,250,500,1000,2500,5000.")
    parser.add_argument("--preset-cells", default=None, help="Comma-separated preset cells for protocol v2 (`blitz`, `fast`, `slow`).")
    parser.add_argument("--games-per-cell", type=int, default=DEFAULT_GAMES_PER_CELL, help="Number of games per (difficulty, time_ms) cell.")
    parser.add_argument("--zero-turn-timeout-ms", type=int, default=DEFAULT_ZERO_TURN_TIMEOUT_MS, help="Safety cap sent as INFO timeout_turn to Zero (sims should bind, this just keeps a runaway position from hanging).")
    parser.add_argument("--match-timeout-ms", type=int, default=0, help="INFO timeout_match value sent to both engines (0 = no per-match cap).")
    parser.add_argument("--bisect", action="store_true", help="After the coarse sweep, add log-spaced midpoints around the 50%% crossing.")
    parser.add_argument("--bisect-rounds", type=int, default=DEFAULT_BISECT_ROUNDS, help="Maximum bisect rounds (each round adds at most one new cell).")
    parser.add_argument("--bisect-target-ci-width", type=float, default=DEFAULT_BISECT_TARGET_CI_WIDTH, help="Stop bisecting once a cell whose CI brackets 0.5 has width <= this.")
    parser.add_argument("--seed", type=int, default=None, help="Seed for any randomized choices (currently unused; reserved for future).")
    parser.add_argument("--openings-file", type=Path, default=None, help="Optional harness openings JSON file; games cycle through these seeded starts.")
    parser.add_argument("--log-dir", type=Path, default=None, help="Output directory; default results/sweep_<difficulty>_<timestamp>.")
    parser.add_argument("--label", default=None, help="Optional label injected into the output directory name.")
    args = parser.parse_args()
    if args.games_per_cell < 1:
        parser.error("--games-per-cell must be >= 1")
    if args.bisect_rounds < 0:
        parser.error("--bisect-rounds must be >= 0")
    if not (0.0 < args.bisect_target_ci_width <= 1.0):
        parser.error("--bisect-target-ci-width must be in (0, 1]")
    if args.times_ms is not None and args.preset_cells is not None:
        parser.error("use either --times-ms or --preset-cells, not both")
    if args.protocol_version == PROTOCOL_V2:
        if args.preset_cells is None:
            parser.error("--protocol-version v2 requires --preset-cells")
        if args.match_timeout_ms not in (0, None):
            parser.error("protocol v2 preset cells manage the game clock internally; leave --match-timeout-ms at 0")
        if args.bisect:
            parser.error("--bisect is only supported for numeric --times-ms sweeps")
    elif args.times_ms is None:
        args.times_ms = ",".join(str(t) for t in DEFAULT_TIMES_MS)
    return args


def parse_preset_cells(text: str) -> list[str]:
    out: list[str] = []
    for token in text.split(","):
        token = token.strip().lower()
        if not token:
            continue
        if token not in PRESET_SPECS:
            raise ValueError(f"unknown preset cell {token!r}; expected one of {', '.join(sorted(PRESET_SPECS))}")
        out.append(token)
    if not out:
        raise ValueError("--preset-cells produced no values")
    return list(dict.fromkeys(out))


def parse_times_ms(text: str) -> list[int]:
    out: list[int] = []
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        value = int(token)
        if value <= 0:
            raise ValueError(f"--times-ms entries must be positive, got {value}")
        out.append(value)
    if not out:
        raise ValueError("--times-ms produced no values")
    return sorted(set(out))


def derive_zero_cmd(base: str, sims: int, no_sims: bool) -> str:
    if no_sims:
        return base
    return f"{base} --sims {sims}"


def default_log_dir(difficulty: str, label: str | None) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{label}" if label else ""
    return REPO_ROOT / "results" / f"sweep_{difficulty}{suffix}_{stamp}"


def make_specs(args: argparse.Namespace, sims: int) -> tuple[EngineSpec, EngineSpec]:
    zero_cmd = derive_zero_cmd(args.zero_cmd, sims, args.zero_cmd_no_sims)
    match_timeout = args.match_timeout_ms if args.match_timeout_ms > 0 else None
    zero_spec = EngineSpec(
        slot=ZERO_SLOT,
        name=args.zero_name,
        command=zero_cmd,
        turn_timeout_ms=args.zero_turn_timeout_ms,
        match_timeout_ms=match_timeout,
        max_memory_bytes=None,
        protocol_version=PROTOCOL_V1,
    )
    minimax_spec = EngineSpec(
        slot=MINIMAX_SLOT,
        name=args.minimax_name,
        command=args.minimax_cmd,
        turn_timeout_ms=DEFAULT_TIMES_MS[0] if args.protocol_version == PROTOCOL_V1 else None,
        match_timeout_ms=match_timeout if args.protocol_version == PROTOCOL_V1 else None,
        max_memory_bytes=None,
        protocol_version=args.protocol_version,
    )
    return zero_spec, minimax_spec


def build_cells(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.protocol_version == PROTOCOL_V2:
        presets = parse_preset_cells(args.preset_cells)
        cells: list[dict[str, Any]] = []
        for index, preset in enumerate(presets):
            spec = PRESET_SPECS[preset]
            cells.append(
                {
                    "index": index,
                    "label": preset,
                    "time_ms": None,
                    "protocol_version": PROTOCOL_V2,
                    "period_time_ms": spec["period_time_ms"],
                    "period_moves": spec["period_moves"],
                }
            )
        return cells

    times_ms = parse_times_ms(args.times_ms)
    return [make_numeric_cell_spec(index, time_ms) for index, time_ms in enumerate(times_ms)]


def make_numeric_cell_spec(index: int, time_ms: int) -> dict[str, Any]:
    return {
        "index": index,
        "label": f"{time_ms}ms",
        "time_ms": time_ms,
        "protocol_version": PROTOCOL_V1,
        "period_time_ms": None,
        "period_moves": None,
    }


def wilson_interval(successes: float, n: int, z: float = WILSON_Z_95) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def score_for_minimax(record: dict[str, Any]) -> float:
    if record["winner_slot"] == MINIMAX_SLOT:
        return 1.0
    if record["winner_slot"] == ZERO_SLOT:
        return 0.0
    return 0.5  # draw or no winner


def play_cell(
    *,
    runtimes: dict[str, EngineProcess],
    openings: list[dict[str, Any]],
    cell_spec: dict[str, Any],
    n_games: int,
    games_done_before_cell: int,
    log_dir: Path,
    cells_jsonl_path: Path,
    games_jsonl_path: Path,
) -> dict[str, Any]:
    """Run one sweep cell and return its aggregated record."""
    time_ms = cell_spec["time_ms"]
    runtimes[MINIMAX_SLOT].spec.protocol_version = cell_spec["protocol_version"]
    runtimes[MINIMAX_SLOT].spec.period_time_ms = cell_spec["period_time_ms"]
    runtimes[MINIMAX_SLOT].spec.period_moves = cell_spec["period_moves"]
    if cell_spec["protocol_version"] == PROTOCOL_V1:
        runtimes[MINIMAX_SLOT].spec.turn_timeout_ms = time_ms
    else:
        runtimes[MINIMAX_SLOT].spec.turn_timeout_ms = None
        runtimes[MINIMAX_SLOT].spec.match_timeout_ms = cell_spec["period_time_ms"]

    minimax_wins = 0
    minimax_losses = 0
    draws = 0
    minimax_time_total = 0
    zero_time_total = 0
    minimax_search_count = 0
    minimax_depth_total = 0
    minimax_max_depth: int | None = None
    cell_dir_name = f"cell_t{time_ms:06d}ms" if time_ms is not None else f"cell_{cell_spec['label']}"
    cell_dir = log_dir / cell_dir_name
    cell_dir.mkdir(parents=True, exist_ok=True)
    games_compact: list[dict[str, Any]] = []
    fatal = False
    fatal_reason: str | None = None

    for cell_index in range(1, n_games + 1):
        global_index = games_done_before_cell + cell_index
        transcript: list[dict[str, Any]] = []
        if global_index > 1:
            restart_engine(runtimes[ZERO_SLOT], transcript)
            restart_engine(runtimes[MINIMAX_SLOT], transcript)
        send_configuration(runtimes[ZERO_SLOT], transcript)
        send_configuration(runtimes[MINIMAX_SLOT], transcript)
        opening = None if not openings else openings[(cell_index - 1) % len(openings)]

        black_slot = ZERO_SLOT if cell_index % 2 == 1 else MINIMAX_SLOT
        record = play_game(
            game_index=global_index,
            runtimes=runtimes,
            black_slot=black_slot,
            transcript=transcript,
            opening=opening,
        )
        write_json(cell_dir / f"game_{cell_index:03d}.json", record)

        score = score_for_minimax(record)
        if score == 1.0:
            minimax_wins += 1
        elif score == 0.0:
            minimax_losses += 1
        else:
            draws += 1
        minimax_time_total += record["engine_time_ms"][MINIMAX_SLOT]["elapsed_ms"]
        zero_time_total += record["engine_time_ms"][ZERO_SLOT]["elapsed_ms"]
        search_stats = record["engine_search_stats"][MINIMAX_SLOT]
        minimax_search_count += search_stats["search_count"]
        minimax_depth_total += search_stats["depth_total"]
        if search_stats["max_depth"] is not None:
            minimax_max_depth = (
                search_stats["max_depth"]
                if minimax_max_depth is None
                else max(minimax_max_depth, search_stats["max_depth"])
            )

        compact = {
            "cell_label": cell_spec["label"],
            "cell_time_ms": time_ms,
            "cell_protocol_version": cell_spec["protocol_version"],
            "cell_period_time_ms": cell_spec["period_time_ms"],
            "cell_period_moves": cell_spec["period_moves"],
            "cell_game_index": cell_index,
            "global_game_index": global_index,
            "result": record["result"],
            "winner_slot": record["winner_slot"],
            "winner_name": record["winner_name"],
            "termination": record["termination"],
            "move_count": record["move_count"],
            "duration_ms": record["duration_ms"],
            "black_slot": black_slot,
            "score_for_minimax": score,
            "minimax_avg_depth": search_stats["avg_depth"],
            "opening_id": None if record["opening"] is None else record["opening"]["id"],
            "opening_ply": None if record["opening"] is None else record["opening"]["ply"],
            "fatal_stop": record["fatal_stop"],
        }
        append_jsonl(games_jsonl_path, compact)
        games_compact.append(compact)
        zero_color = "black" if black_slot == ZERO_SLOT else "white"
        minimax_color = "white" if black_slot == ZERO_SLOT else "black"
        zero_name = runtimes[ZERO_SLOT].spec.name
        minimax_name = runtimes[MINIMAX_SLOT].spec.name
        if record["winner_slot"] == MINIMAX_SLOT:
            outcome = f"{minimax_name} ({minimax_color}) beat {zero_name} ({zero_color})"
        elif record["winner_slot"] == ZERO_SLOT:
            outcome = f"{zero_name} ({zero_color}) beat {minimax_name} ({minimax_color})"
        else:
            outcome = f"draw ({zero_name}={zero_color}, {minimax_name}={minimax_color})"
        depth_suffix = ""
        if search_stats["avg_depth"] is not None:
            depth_suffix = f", minimax avg depth {search_stats['avg_depth']:.2f}"
        opening_suffix = "" if record["opening"] is None else f", opening {record['opening']['id']}"
        print(
            f"  [cell={cell_spec['label']} g{cell_index}/{n_games}] {outcome} "
            f"by {record['termination']} in {record['move_count']} ply, "
            f"minimax used {record['engine_time_ms'][MINIMAX_SLOT]['elapsed_ms']} ms{depth_suffix}{opening_suffix}"
        )

        if record["fatal_stop"]:
            fatal = True
            fatal_reason = record["reason"]
            break

    n = minimax_wins + minimax_losses + draws
    score_total = minimax_wins + draws / 2
    proportion = score_total / n if n > 0 else 0.0
    lo, hi = wilson_interval(score_total, n) if n > 0 else (0.0, 1.0)
    cell = {
        "cell_index": cell_spec["index"],
        "cell_label": cell_spec["label"],
        "protocol_version": cell_spec["protocol_version"],
        "time_ms": time_ms,
        "period_time_ms": cell_spec["period_time_ms"],
        "period_moves": cell_spec["period_moves"],
        "n_games": n,
        "minimax_wins": minimax_wins,
        "minimax_losses": minimax_losses,
        "draws": draws,
        "minimax_score": proportion,
        "wilson_low": lo,
        "wilson_high": hi,
        "wilson_width": hi - lo,
        "minimax_time_ms_total": minimax_time_total,
        "zero_time_ms_total": zero_time_total,
        "minimax_search_count": minimax_search_count,
        "minimax_avg_depth": None if minimax_search_count == 0 else minimax_depth_total / minimax_search_count,
        "minimax_max_depth": minimax_max_depth,
        "fatal_stop": fatal,
        "fatal_reason": fatal_reason,
    }
    append_jsonl(cells_jsonl_path, cell)
    cell["games"] = games_compact
    return cell


def find_bracketing_pair(cells: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    sorted_cells = sorted(cells, key=lambda c: c["time_ms"])
    for lo, hi in zip(sorted_cells, sorted_cells[1:]):
        s_lo = lo["minimax_score"]
        s_hi = hi["minimax_score"]
        if (s_lo - 0.5) * (s_hi - 0.5) <= 0 and s_lo != s_hi:
            return (lo, hi)
    return None


def cell_meets_target(cell: dict[str, Any], target_width: float) -> bool:
    return cell["wilson_low"] <= 0.5 <= cell["wilson_high"] and cell["wilson_width"] <= target_width


def next_bisect_time(cells: list[dict[str, Any]]) -> int | None:
    pair = find_bracketing_pair(cells)
    if pair is None:
        return None
    lo, hi = pair
    existing = {c["time_ms"] for c in cells}
    geo = int(round(math.sqrt(lo["time_ms"] * hi["time_ms"])))
    if geo not in existing and lo["time_ms"] < geo < hi["time_ms"]:
        return geo
    arith = (lo["time_ms"] + hi["time_ms"]) // 2
    if arith not in existing and lo["time_ms"] < arith < hi["time_ms"]:
        return arith
    return None


def interpolated_50_time_ms(cells: list[dict[str, Any]]) -> float | None:
    pair = find_bracketing_pair(cells)
    if pair is None:
        return None
    lo, hi = pair
    s_lo = lo["minimax_score"]
    s_hi = hi["minimax_score"]
    if s_lo == s_hi:
        return None
    x_lo = math.log10(lo["time_ms"])
    x_hi = math.log10(hi["time_ms"])
    t = (0.5 - s_lo) / (s_hi - s_lo)
    return 10 ** (x_lo + t * (x_hi - x_lo))


def check_monotone(cells: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    sorted_cells = sorted(cells, key=lambda c: c["time_ms"])
    issues: list[str] = []
    for prev, curr in zip(sorted_cells, sorted_cells[1:]):
        if curr["minimax_score"] + 1e-9 < prev["minimax_score"]:
            issues.append(
                f"t={prev['time_ms']}ms score={prev['minimax_score']:.3f} -> "
                f"t={curr['time_ms']}ms score={curr['minimax_score']:.3f}"
            )
    return (not issues, issues)


def write_csv_summary(path: Path, cells: list[dict[str, Any]]) -> None:
    sorted_cells = sorted(cells, key=lambda c: c["cell_index"])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "cell_label",
                "protocol_version",
                "time_ms",
                "period_time_ms",
                "period_moves",
                "n_games",
                "minimax_wins",
                "minimax_losses",
                "draws",
                "minimax_score",
                "wilson_low",
                "wilson_high",
                "wilson_width",
                "minimax_time_ms_total",
                "zero_time_ms_total",
                "minimax_search_count",
                "minimax_avg_depth",
                "minimax_max_depth",
                "fatal_stop",
            ]
        )
        for cell in sorted_cells:
            writer.writerow(
                [
                    cell["cell_label"],
                    cell["protocol_version"],
                    cell["time_ms"],
                    cell["period_time_ms"],
                    cell["period_moves"],
                    cell["n_games"],
                    cell["minimax_wins"],
                    cell["minimax_losses"],
                    cell["draws"],
                    f"{cell['minimax_score']:.4f}",
                    f"{cell['wilson_low']:.4f}",
                    f"{cell['wilson_high']:.4f}",
                    f"{cell['wilson_width']:.4f}",
                    cell["minimax_time_ms_total"],
                    cell["zero_time_ms_total"],
                    cell["minimax_search_count"],
                    "" if cell["minimax_avg_depth"] is None else f"{cell['minimax_avg_depth']:.4f}",
                    "" if cell["minimax_max_depth"] is None else cell["minimax_max_depth"],
                    cell["fatal_stop"],
                ]
            )


def write_md_summary(
    path: Path,
    *,
    cells: list[dict[str, Any]],
    difficulty: str,
    sims: int,
    zero_cmd: str,
    minimax_cmd: str,
    games_per_cell: int,
    openings_file: Path | None,
    opening_count: int,
    bisect: bool,
    bisect_rounds_used: int,
    bisect_target_ci_width: float,
    interp_ms: float | None,
    monotone_ok: bool,
    monotone_issues: list[str],
    sweep_started: str,
    sweep_finished: str,
    total_games: int,
) -> None:
    sorted_cells = sorted(cells, key=lambda c: c["cell_index"])
    numeric_cells = all(cell["time_ms"] is not None for cell in sorted_cells)
    lines: list[str] = []
    lines.append(f"# Calibration sweep — Zero {difficulty} ({sims} sims) vs minimax expert\n")
    lines.append("")
    lines.append(f"- Started: {sweep_started}")
    lines.append(f"- Finished: {sweep_finished}")
    lines.append(f"- Games per cell: {games_per_cell}")
    lines.append(f"- Total games played: {total_games}")
    if openings_file is None:
        lines.append("- Opening seeds: none (empty-board starts)")
    else:
        lines.append(f"- Opening seeds: `{openings_file}` ({opening_count} openings, cycled identically in each time bucket by cell index)")
    lines.append(f"- Bisect: {'on' if bisect else 'off'} (rounds used: {bisect_rounds_used}, target CI width: {bisect_target_ci_width:.2f})")
    lines.append(f"- Zero command: `{zero_cmd}`")
    lines.append(f"- Minimax command: `{minimax_cmd}`")
    lines.append("")
    lines.append("## Win-rate matrix (minimax-expert point of view)")
    lines.append("")
    lines.append("Score = (wins + draws/2) / games. CI is Wilson 95% (chess-scoring approximation: draws contribute half a success, the binomial variance term is taken on the score proportion). `M avg depth` is the weighted average of the minimax adapter's reported `depth=` value over all minimax turns in the cell.")
    lines.append("")
    lines.append("| cell | avg ms | period clock | games | M wins | M losses | draws | score | 95% CI | width | M turns | M avg depth |")
    lines.append("|:---|---:|:---|---:|---:|---:|---:|---:|:---|---:|---:|---:|")
    for cell in sorted_cells:
        avg_depth = "—" if cell["minimax_avg_depth"] is None else f"{cell['minimax_avg_depth']:.2f}"
        time_text = "—" if cell["time_ms"] is None else str(cell["time_ms"])
        if cell["period_time_ms"] is None:
            period_text = "—"
        else:
            period_text = f"{cell['period_time_ms']} ms / {cell['period_moves']}"
        lines.append(
            f"| {cell['cell_label']} | {time_text} | {period_text} | {cell['n_games']} | {cell['minimax_wins']} | "
            f"{cell['minimax_losses']} | {cell['draws']} | {cell['minimax_score']:.3f} | "
            f"[{cell['wilson_low']:.3f}, {cell['wilson_high']:.3f}] | {cell['wilson_width']:.3f} | "
            f"{cell['minimax_search_count']} | {avg_depth} |"
        )
    lines.append("")
    lines.append("## Interpolated 50% crossing")
    lines.append("")
    if not numeric_cells:
        lines.append("Not computed for preset/global-clock cells.")
    elif interp_ms is None:
        if sorted_cells and all(c["minimax_score"] < 0.5 for c in sorted_cells):
            lines.append("All sampled cells have minimax score < 0.5 — Zero wins everything in this range. Add larger times.")
        elif sorted_cells and all(c["minimax_score"] > 0.5 for c in sorted_cells):
            lines.append("All sampled cells have minimax score > 0.5 — minimax wins everything in this range. Add smaller times.")
        else:
            lines.append("No bracketing pair found.")
    else:
        lines.append(f"Linear interpolation in log10(time): minimax-expert time ≈ **{interp_ms:.0f} ms** for a 50/50 result against Zero {difficulty}.")
    lines.append("")
    lines.append("## Monotonicity sanity check")
    lines.append("")
    if not numeric_cells:
        lines.append("Not computed for preset/global-clock cells.")
    elif monotone_ok:
        lines.append("Score is non-decreasing in minimax time across all sampled cells. ✅")
    else:
        lines.append("**Score is NOT monotone in minimax time.** Possible causes: search instability at short time controls, MCTS variance with small game counts, or opening-book bias. Offending pairs:")
        for issue in monotone_issues:
            lines.append(f"- {issue}")
    lines.append("")
    fatal_cells = [c for c in sorted_cells if c["fatal_stop"]]
    if fatal_cells:
        lines.append("## Fatal stops")
        lines.append("")
        for cell in fatal_cells:
            lines.append(f"- {cell['cell_label']}: {cell['fatal_reason']}")
        lines.append("")
    path.write_text("\n".join(lines))


def run_sweep(args: argparse.Namespace) -> int:
    sims = DIFFICULTY_TO_SIMS[args.difficulty]
    cells_to_run = build_cells(args)
    log_dir = args.log_dir or default_log_dir(args.difficulty, args.label)
    log_dir.mkdir(parents=True, exist_ok=True)
    openings = [] if args.openings_file is None else load_openings_file(args.openings_file)

    zero_spec, minimax_spec = make_specs(args, sims)
    runtimes = {
        ZERO_SLOT: EngineProcess(zero_spec),
        MINIMAX_SLOT: EngineProcess(minimax_spec),
    }
    cells: list[dict[str, Any]] = []
    cells_jsonl_path = log_dir / "cells.jsonl"
    games_jsonl_path = log_dir / "games.jsonl"
    sweep_meta = {
        "difficulty": args.difficulty,
        "protocol_version": args.protocol_version,
        "sims": sims,
        "zero_cmd": zero_spec.command,
        "minimax_cmd": minimax_spec.command,
        "times_ms_initial": [cell["time_ms"] for cell in cells_to_run if cell["time_ms"] is not None],
        "preset_cells_initial": [cell["label"] for cell in cells_to_run if cell["period_time_ms"] is not None],
        "games_per_cell": args.games_per_cell,
        "openings_file": None if args.openings_file is None else str(args.openings_file),
        "opening_count": len(openings),
        "bisect": args.bisect,
        "bisect_rounds": args.bisect_rounds,
        "bisect_target_ci_width": args.bisect_target_ci_width,
    }
    write_json(log_dir / "sweep_config.json", sweep_meta)

    sweep_started = datetime.now().isoformat(timespec="seconds")
    if args.protocol_version == PROTOCOL_V2:
        labels = [cell["label"] for cell in cells_to_run]
        print(f"[sweep] difficulty={args.difficulty} sims={sims} preset_cells={labels} protocol={args.protocol_version} games_per_cell={args.games_per_cell}")
    else:
        times_ms = [cell["time_ms"] for cell in cells_to_run]
        print(f"[sweep] difficulty={args.difficulty} sims={sims} times_ms={times_ms} protocol={args.protocol_version} games_per_cell={args.games_per_cell}")
    print(f"[sweep] log_dir={log_dir}")
    if openings:
        print(f"[sweep] openings={len(openings)} from {args.openings_file}")

    games_done = 0
    bisect_rounds_used = 0
    exit_code = 0

    try:
        initialize_engine(runtimes[ZERO_SLOT], [])
        initialize_engine(runtimes[MINIMAX_SLOT], [])
        print(f"[sweep] zero ABOUT: {runtimes[ZERO_SLOT].metadata.raw_about if runtimes[ZERO_SLOT].metadata else '<none>'}")
        print(f"[sweep] minimax ABOUT: {runtimes[MINIMAX_SLOT].metadata.raw_about if runtimes[MINIMAX_SLOT].metadata else '<none>'}")

        for cell_spec in cells_to_run:
            label = cell_spec["label"]
            print(f"[sweep] cell {label} ({args.games_per_cell} games)")
            cell = play_cell(
                runtimes=runtimes,
                openings=openings,
                cell_spec=cell_spec,
                n_games=args.games_per_cell,
                games_done_before_cell=games_done,
                log_dir=log_dir,
                cells_jsonl_path=cells_jsonl_path,
                games_jsonl_path=games_jsonl_path,
            )
            cells.append(cell)
            games_done += cell["n_games"]
            avg_depth_text = "n/a" if cell["minimax_avg_depth"] is None else f"{cell['minimax_avg_depth']:.2f}"
            print(
                f"[sweep] cell {label} result: "
                f"M {cell['minimax_wins']}-{cell['minimax_losses']}-{cell['draws']} "
                f"score={cell['minimax_score']:.3f} CI=[{cell['wilson_low']:.3f}, {cell['wilson_high']:.3f}] "
                f"avg_depth={avg_depth_text} over {cell['minimax_search_count']} turns"
            )
            if cell["fatal_stop"]:
                print(f"[sweep] fatal stop at {label}: {cell['fatal_reason']} — aborting sweep")
                exit_code = 1
                break

        if args.bisect and exit_code == 0:
            for round_idx in range(args.bisect_rounds):
                hit = next((c for c in cells if cell_meets_target(c, args.bisect_target_ci_width)), None)
                if hit is not None:
                    print(f"[bisect] cell t={hit['time_ms']}ms brackets 0.5 with CI width {hit['wilson_width']:.3f} <= target {args.bisect_target_ci_width:.3f}; stopping")
                    break
                next_t = next_bisect_time(cells)
                if next_t is None:
                    print("[bisect] no further bisect target available; stopping")
                    break
                print(f"[bisect] round {round_idx + 1}/{args.bisect_rounds}: adding cell at t={next_t}ms")
                cell_spec = make_numeric_cell_spec(len(cells_to_run) + bisect_rounds_used, next_t)
                cell = play_cell(
                    runtimes=runtimes,
                    openings=openings,
                    cell_spec=cell_spec,
                    n_games=args.games_per_cell,
                    games_done_before_cell=games_done,
                    log_dir=log_dir,
                    cells_jsonl_path=cells_jsonl_path,
                    games_jsonl_path=games_jsonl_path,
                )
                cells.append(cell)
                games_done += cell["n_games"]
                avg_depth_text = "n/a" if cell["minimax_avg_depth"] is None else f"{cell['minimax_avg_depth']:.2f}"
                bisect_rounds_used += 1
                print(
                    f"[bisect] cell t={next_t}ms result: "
                    f"M {cell['minimax_wins']}-{cell['minimax_losses']}-{cell['draws']} "
                    f"score={cell['minimax_score']:.3f} CI=[{cell['wilson_low']:.3f}, {cell['wilson_high']:.3f}] "
                    f"avg_depth={avg_depth_text} over {cell['minimax_search_count']} turns"
                )
                if cell["fatal_stop"]:
                    print(f"[bisect] fatal stop at t={next_t}ms: {cell['fatal_reason']} — aborting bisect")
                    exit_code = 1
                    break

    except EngineFailure as exc:
        print(f"[sweep] engine failure: {exc}", file=sys.stderr)
        exit_code = 1
    finally:
        runtimes[ZERO_SLOT].shutdown()
        runtimes[MINIMAX_SLOT].shutdown()

    sweep_finished = datetime.now().isoformat(timespec="seconds")

    if cells:
        numeric_cells = all(cell["time_ms"] is not None for cell in cells)
        interp_ms = interpolated_50_time_ms(cells) if numeric_cells else None
        monotone_ok, monotone_issues = check_monotone(cells) if numeric_cells else (True, [])
        write_csv_summary(log_dir / "summary.csv", cells)
        write_md_summary(
            log_dir / "summary.md",
            cells=cells,
            difficulty=args.difficulty,
            sims=sims,
            zero_cmd=zero_spec.command,
            minimax_cmd=minimax_spec.command,
            games_per_cell=args.games_per_cell,
            openings_file=args.openings_file,
            opening_count=len(openings),
            bisect=args.bisect,
            bisect_rounds_used=bisect_rounds_used,
            bisect_target_ci_width=args.bisect_target_ci_width,
            interp_ms=interp_ms,
            monotone_ok=monotone_ok,
            monotone_issues=monotone_issues,
            sweep_started=sweep_started,
            sweep_finished=sweep_finished,
            total_games=sum(c["n_games"] for c in cells),
        )

        print("")
        print(f"[sweep] wrote {log_dir / 'summary.csv'}")
        print(f"[sweep] wrote {log_dir / 'summary.md'}")
        if interp_ms is not None:
            print(f"[sweep] interpolated 50% crossing: minimax-expert ~ {interp_ms:.0f} ms vs Zero {args.difficulty}")
        elif numeric_cells:
            print(f"[sweep] no 50% crossing in sampled range; widen --times-ms")
        else:
            print("[sweep] interpolated 50% crossing skipped for preset/global-clock cells")
        if numeric_cells and not monotone_ok:
            print(f"[sweep] WARNING: minimax score is non-monotone in time across {len(monotone_issues)} adjacent pair(s)")
    else:
        print("[sweep] no cells completed; nothing to summarize", file=sys.stderr)

    return exit_code


def main() -> int:
    args = parse_args()
    return run_sweep(args)


if __name__ == "__main__":
    raise SystemExit(main())
