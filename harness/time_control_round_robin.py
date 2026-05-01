#!/usr/bin/env python3
"""Round-robin tournament between minimax time-control presets.

Each preset is represented as the same gomoku-minimax adapter command with a
different protocol-v2 period clock. The harness tracks clocks independently for
each side and sends `INFO time_left` / `INFO moves_to_reset` before every move.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import sys
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
    PROTOCOL_V2,
    append_jsonl,
    initialize_engine,
    play_game,
    restart_engine,
    send_configuration,
    write_json,
)

DEFAULT_ENGINE_CMD = "./adapters/minimax/run.sh --controller expert --time-ms 1000 --threads 4"
DEFAULT_OPENINGS = "results/crazy_sensei_openings_253.json"
DEFAULT_GAMES_PER_PAIR = 4
PRESET_SPECS = {
    "blitz": {"period_time_ms": 5 * 60 * 1000, "period_moves": 40},
    "fast": {"period_time_ms": 15 * 60 * 1000, "period_moves": 60},
    "slow": {"period_time_ms": 60 * 60 * 1000, "period_moves": 60},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engine-cmd", default=DEFAULT_ENGINE_CMD, help="Adapter command used for every time-control competitor.")
    parser.add_argument("--presets", default="blitz,fast,slow", help="Comma-separated preset names to include.")
    parser.add_argument(
        "--games-per-pair",
        type=int,
        default=DEFAULT_GAMES_PER_PAIR,
        help="Games per unordered pairing; must be even. Adjacent games mirror the same opening with colors swapped.",
    )
    parser.add_argument("--openings-file", type=Path, default=Path(DEFAULT_OPENINGS), help="Optional openings JSON file; pairings cycle through it identically.")
    parser.add_argument("--log-dir", type=Path, default=None, help="Output directory; default results/time_control_round_robin_<timestamp>.")
    parser.add_argument("--label", default=None, help="Optional label injected into the output directory name.")
    args = parser.parse_args()
    if args.games_per_pair < 1:
        parser.error("--games-per-pair must be >= 1")
    if args.games_per_pair % 2 != 0:
        parser.error("--games-per-pair must be even so colors and openings can be mirrored")
    return args


def parse_presets(text: str) -> list[str]:
    presets: list[str] = []
    for token in text.split(","):
        preset = token.strip().lower()
        if not preset:
            continue
        if preset not in PRESET_SPECS:
            raise ValueError(f"unknown preset {preset!r}; expected one of {', '.join(sorted(PRESET_SPECS))}")
        presets.append(preset)
    presets = list(dict.fromkeys(presets))
    if len(presets) < 2:
        raise ValueError("--presets must name at least two presets")
    return presets


def default_log_dir(label: str | None) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{label}" if label else ""
    return REPO_ROOT / "results" / f"time_control_round_robin{suffix}_{stamp}"


def make_spec(slot: str, preset: str, command: str) -> EngineSpec:
    spec = PRESET_SPECS[preset]
    return EngineSpec(
        slot=slot,
        name=preset,
        command=command,
        turn_timeout_ms=None,
        match_timeout_ms=spec["period_time_ms"],
        max_memory_bytes=None,
        protocol_version=PROTOCOL_V2,
        period_time_ms=spec["period_time_ms"],
        period_moves=spec["period_moves"],
    )


def score_for(record: dict[str, Any], slot: str) -> float:
    if record["winner_slot"] == slot:
        return 1.0
    if record["winner_slot"] is None:
        return 0.5
    return 0.0


def format_duration_ms(duration_ms: int) -> str:
    total_seconds = max(0, int(round(duration_ms / 1000.0)))
    minutes, seconds = divmod(total_seconds, 60)
    if minutes == 0:
        return f"{seconds}s"
    hours, minutes = divmod(minutes, 60)
    if hours == 0:
        return f"{minutes}m{seconds:02d}s"
    return f"{hours}h{minutes:02d}m{seconds:02d}s"


def format_stop_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "-"
    order = ["soft_limit", "affordability", "hard_limit", "node_limit", "max_depth", "incomplete"]
    labels = {
        "soft_limit": "soft",
        "affordability": "aff",
        "hard_limit": "hard",
        "node_limit": "node",
        "max_depth": "max",
        "incomplete": "inc",
    }
    parts = [f"{labels[reason]}:{counts[reason]}" for reason in order if counts.get(reason, 0)]
    for reason in sorted(counts):
        if reason not in labels and counts[reason]:
            parts.append(f"{reason}:{counts[reason]}")
    return " ".join(parts) if parts else "-"


def build_pair_game_plan(games_per_pair: int, openings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if games_per_pair < 1:
        raise ValueError("games_per_pair must be >= 1")
    if games_per_pair % 2 != 0:
        raise ValueError("games_per_pair must be even")

    plan: list[dict[str, Any]] = []
    for opening_pair_index in range(games_per_pair // 2):
        opening = None if not openings else openings[opening_pair_index % len(openings)]
        for black_slot in ("a", "b"):
            plan.append(
                {
                    "game_index": len(plan) + 1,
                    "opening_pair_index": opening_pair_index + 1,
                    "opening": opening,
                    "black_slot": black_slot,
                }
            )
    return plan


def empty_standing(preset: str) -> dict[str, Any]:
    return {
        "preset": preset,
        "games": 0,
        "points": 0.0,
        "wins": 0,
        "losses": 0,
        "draws": 0,
        "black_games": 0,
        "white_games": 0,
        "total_time_ms": 0,
        "search_count": 0,
        "depth_total": 0,
        "normal_search_count": 0,
        "normal_depth_total": 0,
        "total_nodes": 0,
        "total_log_time_ms": 0,
        "root_total": 0,
        "root_count": 0,
        "book_count": 0,
        "threat_sequence_count": 0,
        "panic_count": 0,
        "def_filter_applied_count": 0,
        "filtered_best_count": 0,
        "nofilter_diff_count": 0,
        "stop_reason_counts": {},
        "last_iter_ms_total": 0.0,
        "last_iter_ms_count": 0,
        "next_iter_est_ms_total": 0.0,
        "next_iter_est_ms_count": 0,
        "max_depth": None,
    }


def update_standing(standing: dict[str, Any], record: dict[str, Any], slot: str, black_slot: str) -> None:
    score = score_for(record, slot)
    standing["games"] += 1
    standing["points"] += score
    standing["black_games"] += 1 if slot == black_slot else 0
    standing["white_games"] += 1 if slot != black_slot else 0
    if score == 1.0:
        standing["wins"] += 1
    elif score == 0.0:
        standing["losses"] += 1
    else:
        standing["draws"] += 1

    standing["total_time_ms"] += record["engine_time_ms"][slot]["elapsed_ms"]
    stats = record["engine_search_stats"][slot]
    standing["search_count"] += stats["search_count"]
    standing["depth_total"] += stats["depth_total"]
    standing["normal_search_count"] += stats.get("normal_search_count", 0)
    standing["normal_depth_total"] += stats.get("normal_depth_total", 0)
    standing["total_nodes"] += stats.get("total_nodes", 0)
    standing["total_log_time_ms"] += stats.get("total_log_time_ms", 0)
    standing["root_total"] += stats.get("root_total", 0)
    standing["root_count"] += stats.get("root_count", 0)
    standing["book_count"] += stats.get("book_count", 0)
    standing["threat_sequence_count"] += stats.get("threat_sequence_count", 0)
    standing["panic_count"] += stats.get("panic_count", 0)
    standing["def_filter_applied_count"] += stats.get("def_filter_applied_count", 0)
    standing["filtered_best_count"] += stats.get("filtered_best_count", 0)
    standing["nofilter_diff_count"] += stats.get("nofilter_diff_count", 0)
    for reason, count in stats.get("stop_reason_counts", {}).items():
        current = standing["stop_reason_counts"].get(reason, 0)
        standing["stop_reason_counts"][reason] = current + count
    if stats.get("avg_last_iter_ms") is not None:
        count = stats.get("search_count", 0)
        standing["last_iter_ms_total"] += stats["avg_last_iter_ms"] * count
        standing["last_iter_ms_count"] += count
    if stats.get("avg_next_iter_est_ms") is not None:
        count = stats.get("search_count", 0)
        standing["next_iter_est_ms_total"] += stats["avg_next_iter_est_ms"] * count
        standing["next_iter_est_ms_count"] += count
    if stats["max_depth"] is not None:
        standing["max_depth"] = stats["max_depth"] if standing["max_depth"] is None else max(standing["max_depth"], stats["max_depth"])


def standing_rows(standings: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for standing in standings.values():
        games = standing["games"]
        search_count = standing["search_count"]
        normal_search_count = standing["normal_search_count"]
        total_log_time_ms = standing["total_log_time_ms"]
        root_count = standing["root_count"]
        last_iter_count = standing["last_iter_ms_count"]
        next_iter_count = standing["next_iter_est_ms_count"]
        rows.append(
            {
                **standing,
                "score": 0.0 if games == 0 else standing["points"] / games,
                "avg_depth": None if search_count == 0 else standing["depth_total"] / search_count,
                "avg_normal_depth": None if normal_search_count == 0 else standing["normal_depth_total"] / normal_search_count,
                "mnps": None if total_log_time_ms == 0 else standing["total_nodes"] / (total_log_time_ms * 1000.0),
                "avg_root": None if root_count == 0 else standing["root_total"] / root_count,
                "avg_last_iter_ms": None if last_iter_count == 0 else standing["last_iter_ms_total"] / last_iter_count,
                "avg_next_iter_est_ms": None if next_iter_count == 0 else standing["next_iter_est_ms_total"] / next_iter_count,
            }
        )
    return sorted(rows, key=lambda row: (-row["points"], -row["score"], row["preset"]))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_results_md(
    path: Path,
    *,
    presets: list[str],
    pair_rows: list[dict[str, Any]],
    standings: list[dict[str, Any]],
    games_per_pair: int,
    openings_file: Path | None,
    opening_count: int,
    engine_cmd: str,
    started: str,
    finished: str,
) -> None:
    lines: list[str] = []
    lines.append("# Time-Control Round Robin")
    lines.append("")
    lines.append(f"- Started: {started}")
    lines.append(f"- Finished: {finished}")
    lines.append(f"- Engine command: `{engine_cmd}`")
    lines.append(f"- Presets: {', '.join(presets)}")
    lines.append(f"- Games per pairing: {games_per_pair}")
    if openings_file is None:
        lines.append("- Opening seeds: none")
    else:
        lines.append(
            f"- Opening seeds: `{openings_file}` ({opening_count} openings, cycled by opening pair; each selected opening is played with colors swapped)"
        )
    lines.append("")
    lines.append("## Preset Clocks")
    lines.append("")
    lines.append("| preset | period clock | average per move |")
    lines.append("|:---|:---|---:|")
    for preset in presets:
        spec = PRESET_SPECS[preset]
        avg_ms = spec["period_time_ms"] / spec["period_moves"]
        lines.append(f"| {preset} | {spec['period_time_ms'] // 60000}:00 / {spec['period_moves']} | {avg_ms / 1000:.1f}s |")
    lines.append("")
    lines.append("## Standings")
    lines.append("")
    lines.append("| rank | preset | points | score | W-L-D | games | avg depth | normal depth | avg root | stop reasons | iter ms | def filter | filtered best | nofilter diff | book | thseq | panic | MN/s | total think time |")
    lines.append("|---:|:---|---:|---:|:---|---:|---:|---:|---:|:---|:---|---:|---:|---:|---:|---:|---:|---:|:---|")
    for rank, row in enumerate(standings, start=1):
        avg_depth = "-" if row["avg_depth"] is None else f"{row['avg_depth']:.2f}"
        avg_normal_depth = "-" if row["avg_normal_depth"] is None else f"{row['avg_normal_depth']:.2f}"
        avg_root = "-" if row["avg_root"] is None else f"{row['avg_root']:.1f}"
        stop_counts = format_stop_counts(row.get("stop_reason_counts", {}))
        avg_last_iter = "-" if row.get("avg_last_iter_ms") is None else f"{row['avg_last_iter_ms'] / 1000:.1f}"
        avg_next_iter = "-" if row.get("avg_next_iter_est_ms") is None else f"{row['avg_next_iter_est_ms'] / 1000:.1f}"
        iter_ms = f"{avg_last_iter}/{avg_next_iter}"
        mnps = "-" if row["mnps"] is None else f"{row['mnps']:.2f}"
        total_minutes = row["total_time_ms"] / 60000.0
        lines.append(
            f"| {rank} | {row['preset']} | {row['points']:.1f} | {row['score']:.3f} | "
            f"{row['wins']}-{row['losses']}-{row['draws']} | {row['games']} | {avg_depth} | "
            f"{avg_normal_depth} | {avg_root} | {stop_counts} | {iter_ms} | {row['def_filter_applied_count']} | "
            f"{row['filtered_best_count']} | {row['nofilter_diff_count']} | "
            f"{row['book_count']} | {row['threat_sequence_count']} | "
            f"{row['panic_count']} | {mnps} | {total_minutes:.1f} min |"
        )
    lines.append("")
    lines.append("## Pair Results")
    lines.append("")
    lines.append("| pairing | games | result | score split | avg depth |")
    lines.append("|:---|---:|:---|:---|:---|")
    for row in pair_rows:
        a = row["preset_a"]
        b = row["preset_b"]
        result = f"{a} {row['a_wins']}-{row['b_wins']} {b}, {row['draws']} draws"
        split = f"{a} {row['a_score']:.3f} / {b} {row['b_score']:.3f}"
        depth = f"{a} {row['a_avg_depth']:.2f}, {b} {row['b_avg_depth']:.2f}"
        lines.append(f"| {a} vs {b} | {row['games']} | {result} | {split} | {depth} |")
    lines.append("")
    lines.append("Score uses chess scoring from the preset's point of view: win = 1, draw = 0.5, loss = 0.")
    path.write_text("\n".join(lines), encoding="utf-8")


def play_pair(
    *,
    preset_a: str,
    preset_b: str,
    engine_cmd: str,
    openings: list[dict[str, Any]],
    games_per_pair: int,
    games_done_before_pair: int,
    log_dir: Path,
    standings: dict[str, dict[str, Any]],
    games_jsonl_path: Path,
) -> dict[str, Any]:
    spec_a = make_spec("a", preset_a, engine_cmd)
    spec_b = make_spec("b", preset_b, engine_cmd)
    runtimes = {"a": EngineProcess(spec_a), "b": EngineProcess(spec_b)}
    pair_dir = log_dir / f"{preset_a}_vs_{preset_b}"
    pair_dir.mkdir(parents=True, exist_ok=True)

    a_wins = b_wins = draws = 0
    a_depth_total = b_depth_total = 0
    a_search_count = b_search_count = 0

    try:
        initialize_engine(runtimes["a"], [])
        initialize_engine(runtimes["b"], [])
        for plan_item in build_pair_game_plan(games_per_pair, openings):
            game_index = plan_item["game_index"]
            transcript: list[dict[str, Any]] = []
            if game_index > 1:
                restart_engine(runtimes["a"], transcript)
                restart_engine(runtimes["b"], transcript)
            send_configuration(runtimes["a"], transcript)
            send_configuration(runtimes["b"], transcript)

            opening = plan_item["opening"]
            black_slot = plan_item["black_slot"]
            record = play_game(
                game_index=games_done_before_pair + game_index,
                runtimes=runtimes,
                black_slot=black_slot,
                transcript=transcript,
                opening=opening,
            )
            write_json(pair_dir / f"game_{game_index:03d}.json", record)

            score_a = score_for(record, "a")
            if score_a == 1.0:
                a_wins += 1
            elif score_a == 0.0:
                b_wins += 1
            else:
                draws += 1

            update_standing(standings[preset_a], record, "a", black_slot)
            update_standing(standings[preset_b], record, "b", black_slot)
            a_stats = record["engine_search_stats"]["a"]
            b_stats = record["engine_search_stats"]["b"]
            a_depth_total += a_stats["depth_total"]
            a_search_count += a_stats["search_count"]
            b_depth_total += b_stats["depth_total"]
            b_search_count += b_stats["search_count"]

            compact = {
                "pair": f"{preset_a}_vs_{preset_b}",
                "pair_game_index": game_index,
                "global_game_index": games_done_before_pair + game_index,
                "black_preset": preset_a if black_slot == "a" else preset_b,
                "white_preset": preset_b if black_slot == "a" else preset_a,
                "winner_preset": None if record["winner_slot"] is None else (preset_a if record["winner_slot"] == "a" else preset_b),
                "result": record["result"],
                "termination": record["termination"],
                "move_count": record["move_count"],
                "duration_ms": record["duration_ms"],
                "score_for_a": score_a,
                "opening_id": None if record["opening"] is None else record["opening"]["id"],
                "opening_pair_index": plan_item["opening_pair_index"],
                "fatal_stop": record["fatal_stop"],
            }
            append_jsonl(games_jsonl_path, compact)
            print(
                f"  [{preset_a} vs {preset_b} g{game_index}/{games_per_pair}] "
                f"{compact['winner_preset'] or 'draw'} by {record['termination']} "
                f"in {record['move_count']} ply, match {format_duration_ms(record['duration_ms'])}"
            )
            if record["fatal_stop"]:
                raise EngineFailure(record["reason"])
    finally:
        runtimes["a"].shutdown()
        runtimes["b"].shutdown()

    games = a_wins + b_wins + draws
    return {
        "preset_a": preset_a,
        "preset_b": preset_b,
        "games": games,
        "a_wins": a_wins,
        "b_wins": b_wins,
        "draws": draws,
        "a_score": 0.0 if games == 0 else (a_wins + 0.5 * draws) / games,
        "b_score": 0.0 if games == 0 else (b_wins + 0.5 * draws) / games,
        "a_avg_depth": 0.0 if a_search_count == 0 else a_depth_total / a_search_count,
        "b_avg_depth": 0.0 if b_search_count == 0 else b_depth_total / b_search_count,
    }


def main() -> int:
    args = parse_args()
    try:
        presets = parse_presets(args.presets)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    openings_file = None
    openings: list[dict[str, Any]] = []
    if args.openings_file is not None:
        openings_file = args.openings_file
        openings = load_openings_file(openings_file)

    log_dir = args.log_dir or default_log_dir(args.label)
    log_dir.mkdir(parents=True, exist_ok=True)
    games_jsonl_path = log_dir / "games.jsonl"
    pair_rows: list[dict[str, Any]] = []
    standings = {preset: empty_standing(preset) for preset in presets}
    started = datetime.now().isoformat(timespec="seconds")
    games_done = 0

    config = {
        "engine_cmd": args.engine_cmd,
        "presets": presets,
        "preset_specs": {preset: PRESET_SPECS[preset] for preset in presets},
        "games_per_pair": args.games_per_pair,
        "openings_file": None if openings_file is None else str(openings_file),
        "opening_count": len(openings),
        "opening_schedule": "paired_color_swap",
        "protocol_version": PROTOCOL_V2,
    }
    write_json(log_dir / "tournament_config.json", config)

    print("[time-control] round robin")
    print(f"[time-control] presets={presets} games_per_pair={args.games_per_pair}")
    print(f"[time-control] log_dir={log_dir}")
    if openings_file is not None:
        print(f"[time-control] openings={len(openings)} from {openings_file}")

    exit_code = 0
    try:
        for preset_a, preset_b in itertools.combinations(presets, 2):
            print(f"[time-control] pairing {preset_a} vs {preset_b}")
            row = play_pair(
                preset_a=preset_a,
                preset_b=preset_b,
                engine_cmd=args.engine_cmd,
                openings=openings,
                games_per_pair=args.games_per_pair,
                games_done_before_pair=games_done,
                log_dir=log_dir,
                standings=standings,
                games_jsonl_path=games_jsonl_path,
            )
            pair_rows.append(row)
            games_done += row["games"]
            print(
                f"[time-control] result {preset_a} vs {preset_b}: "
                f"{row['a_wins']}-{row['b_wins']}-{row['draws']} "
                f"score {row['a_score']:.3f}/{row['b_score']:.3f}"
            )
    except EngineFailure as exc:
        print(f"[time-control] engine failure: {exc}", file=sys.stderr)
        exit_code = 1

    finished = datetime.now().isoformat(timespec="seconds")
    standings_rows = standing_rows(standings)
    write_json(log_dir / "pair_results.json", {"pairs": pair_rows})
    write_json(log_dir / "standings.json", {"standings": standings_rows})
    write_csv(
        log_dir / "pair_results.csv",
        pair_rows,
        ["preset_a", "preset_b", "games", "a_wins", "b_wins", "draws", "a_score", "b_score", "a_avg_depth", "b_avg_depth"],
    )
    write_csv(
        log_dir / "standings.csv",
        standings_rows,
        [
            "preset",
            "games",
            "points",
            "score",
            "wins",
            "losses",
            "draws",
            "black_games",
            "white_games",
            "total_time_ms",
            "search_count",
            "avg_depth",
            "normal_search_count",
            "avg_normal_depth",
            "root_count",
            "avg_root",
            "avg_last_iter_ms",
            "avg_next_iter_est_ms",
            "def_filter_applied_count",
            "filtered_best_count",
            "nofilter_diff_count",
            "book_count",
            "threat_sequence_count",
            "panic_count",
            "total_nodes",
            "total_log_time_ms",
            "mnps",
            "max_depth",
        ],
    )
    write_results_md(
        log_dir / "Results.md",
        presets=presets,
        pair_rows=pair_rows,
        standings=standings_rows,
        games_per_pair=args.games_per_pair,
        openings_file=openings_file,
        opening_count=len(openings),
        engine_cmd=args.engine_cmd,
        started=started,
        finished=finished,
    )
    print(f"[time-control] wrote {log_dir / 'Results.md'}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
