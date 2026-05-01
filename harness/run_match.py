#!/usr/bin/env python3
"""Run head-to-head Gomocup matches between two engines."""

from __future__ import annotations

import argparse
import json
import queue
import random
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.openings import load_openings_file
from harness.rules import BLACK, BOARD_SIZE, COLOR_NAMES, EMPTY, RefereeBoard, RefereeMove, WHITE

MOVE_RE = re.compile(r"^\s*(-?\d+)\s*,\s*(-?\d+)\s*$")
MINIMAX_SEARCH_LOG_RE = re.compile(r"^\[minimax\]\s+(?P<body>.*)$")
MINIMAX_SEARCH_KV_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>\S+)")
START_TIMEOUT_S = 10.0
ABOUT_TIMEOUT_S = 10.0
RESTART_TIMEOUT_S = 10.0
SHUTDOWN_TIMEOUT_S = 5.0
READ_POLL_S = 0.1
TIMEOUT_GRACE_BASE_MS = 100
TIMEOUT_GRACE_FRAC = 0.10
PROTOCOL_V1 = "v1"
PROTOCOL_V2 = "v2"


def timeout_grace_ms(limit_ms: int) -> int:
    # Base grace + 10% of the budget. Pure-constant grace is too tight for
    # iterative engines whose stop-checks land between deepening iterations:
    # minimax-expert reliably overshoots by up to ~50 ms at a 500 ms budget,
    # and IPC + scheduling adds a few ms on top.
    return TIMEOUT_GRACE_BASE_MS + int(limit_ms * TIMEOUT_GRACE_FRAC)


class EngineFailure(RuntimeError):
    """Base class for engine process failures."""


class EngineTimeout(EngineFailure):
    """Raised when an engine does not answer within the allotted time."""


class EngineExited(EngineFailure):
    """Raised when an engine exits unexpectedly."""


@dataclass
class EngineSpec:
    slot: str
    name: str
    command: str
    turn_timeout_ms: int | None
    match_timeout_ms: int | None
    max_memory_bytes: int | None
    protocol_version: str = PROTOCOL_V1
    period_time_ms: int | None = None
    period_moves: int | None = None

    def argv(self) -> list[str]:
        return shlex.split(self.command)

    def uses_period_clock(self) -> bool:
        return (
            self.protocol_version == PROTOCOL_V2
            and self.period_time_ms is not None
            and self.period_time_ms > 0
            and self.period_moves is not None
            and self.period_moves > 0
        )


@dataclass
class EngineMetadata:
    raw_about: str
    parsed_about: dict[str, str]


@dataclass
class PeriodClock:
    period_time_ms: int
    period_moves: int
    time_left_ms: int
    moves_to_reset: int


class EngineProcess:
    def __init__(self, spec: EngineSpec) -> None:
        self.spec = spec
        self.proc: subprocess.Popen[str] | None = None
        self._stdout_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._aux_lock = threading.Lock()
        self._aux_events: list[dict[str, Any]] = []
        self.metadata: EngineMetadata | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.spec.argv(),
            cwd=REPO_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._pump_stdout, name=f"{self.spec.slot}-stdout", daemon=True).start()
        threading.Thread(target=self._pump_stderr, name=f"{self.spec.slot}-stderr", daemon=True).start()

    def aux_cursor(self) -> int:
        with self._aux_lock:
            return len(self._aux_events)

    def aux_since(self, cursor: int) -> list[dict[str, Any]]:
        with self._aux_lock:
            return [dict(event) for event in self._aux_events[cursor:]]

    def send(self, line: str, transcript: list[dict[str, Any]] | None = None) -> None:
        proc = self._require_proc()
        if transcript is not None:
            transcript.append(
                {
                    "ts": time.time(),
                    "engine_slot": self.spec.slot,
                    "engine_name": self.spec.name,
                    "direction": "send",
                    "line": line,
                }
            )
        try:
            assert proc.stdin is not None
            proc.stdin.write(line + "\n")
            proc.stdin.flush()
        except BrokenPipeError as exc:
            raise EngineExited(f"{self.spec.name} stdin closed unexpectedly") from exc

    def read_response(self, timeout_s: float | None, transcript: list[dict[str, Any]] | None = None) -> str:
        proc = self._require_proc()
        deadline = None if timeout_s is None else time.monotonic() + timeout_s

        while True:
            if proc.poll() is not None and self._stdout_queue.empty():
                raise EngineExited(f"{self.spec.name} exited with code {proc.returncode}")

            timeout = READ_POLL_S
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise EngineTimeout(f"{self.spec.name} did not answer in time")
                timeout = min(timeout, remaining)

            try:
                event = self._stdout_queue.get(timeout=timeout)
            except queue.Empty:
                continue

            line = str(event["line"])
            if transcript is not None:
                transcript.append(
                    {
                        "ts": event["ts"],
                        "engine_slot": self.spec.slot,
                        "engine_name": self.spec.name,
                        "direction": "recv",
                        "line": line,
                    }
                )
            return line

    def shutdown(self) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    self.send("END")
                except EngineFailure:
                    pass
                try:
                    proc.wait(timeout=SHUTDOWN_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=SHUTDOWN_TIMEOUT_S)
        finally:
            if proc.stdin is not None:
                proc.stdin.close()
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()

    def _pump_stdout(self) -> None:
        proc = self._require_proc()
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            event = {
                "ts": time.time(),
                "stream": "stdout",
                "line": line,
            }
            if line.startswith("MESSAGE ") or line.startswith("DEBUG "):
                self._append_aux(event)
                continue
            self._stdout_queue.put(event)

    def _pump_stderr(self) -> None:
        proc = self._require_proc()
        assert proc.stderr is not None
        for raw in proc.stderr:
            self._append_aux(
                {
                    "ts": time.time(),
                    "stream": "stderr",
                    "line": raw.rstrip("\n"),
                }
            )

    def _append_aux(self, event: dict[str, Any]) -> None:
        with self._aux_lock:
            self._aux_events.append(event)

    def _require_proc(self) -> subprocess.Popen[str]:
        if self.proc is None:
            raise RuntimeError(f"{self.spec.name} process has not been started")
        return self.proc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a freestyle 15x15 Gomoku match between two adapters.")
    parser.add_argument("--engine-a-cmd", required=True, help="Command line used to launch engine A.")
    parser.add_argument("--engine-b-cmd", required=True, help="Command line used to launch engine B.")
    parser.add_argument("--engine-a-name", default="engine-a", help="Friendly name for engine A.")
    parser.add_argument("--engine-b-name", default="engine-b", help="Friendly name for engine B.")
    parser.add_argument("--engine-a-time-ms", type=int, default=None, help="Per-move timeout for engine A.")
    parser.add_argument("--engine-b-time-ms", type=int, default=None, help="Per-move timeout for engine B.")
    parser.add_argument("--engine-a-match-time-ms", type=int, default=None, help="Whole-game timeout budget for engine A.")
    parser.add_argument("--engine-b-match-time-ms", type=int, default=None, help="Whole-game timeout budget for engine B.")
    parser.add_argument("--time-ms", type=int, default=None, help="Shared per-move timeout when no per-engine override is supplied (default 1000 in protocol v1; disabled in protocol v2 unless explicitly set).")
    parser.add_argument("--match-time-ms", type=int, default=None, help="Shared whole-game timeout budget when no per-engine override is supplied.")
    parser.add_argument("--protocol-version", choices=(PROTOCOL_V1, PROTOCOL_V2), default=PROTOCOL_V1, help="Harness wire-protocol profile: v1 is legacy fixed-turn Gomocup INFO; v2 adds live clock updates (`time_left`, `moves_to_reset`).")
    parser.add_argument("--period-time-ms", type=int, default=None, help="Per-side clock period in milliseconds for protocol v2 (for example 300000 for Blitz).")
    parser.add_argument("--period-moves", type=int, default=None, help="Moves in each clock period for protocol v2 (for example 40 for Blitz).")
    parser.add_argument("--max-memory-bytes", type=int, default=None, help="Optional INFO max_memory value sent to both engines.")
    parser.add_argument("--games", type=int, default=1, help="Number of games to play.")
    parser.add_argument("--swap-colors", action="store_true", help="Alternate who plays black across games.")
    parser.add_argument("--black", choices=("a", "b", "random"), default="a", help="Who plays black in game 1.")
    parser.add_argument("--seed", type=int, default=None, help="Seed used when black is chosen randomly.")
    parser.add_argument("--openings-file", type=Path, default=None, help="Optional harness openings JSON file; games cycle through these seeded starts.")
    parser.add_argument("--log-dir", type=Path, default=None, help="Directory for per-game logs and the tournament summary.")
    args = parser.parse_args()
    if args.games < 1:
        parser.error("--games must be at least 1")
    if args.protocol_version == PROTOCOL_V2:
        if args.period_time_ms is None or args.period_moves is None:
            parser.error("--protocol-version v2 requires both --period-time-ms and --period-moves")
        if args.period_time_ms <= 0:
            parser.error("--period-time-ms must be positive")
        if args.period_moves <= 0:
            parser.error("--period-moves must be positive")
        if any(value is not None for value in (args.match_time_ms, args.engine_a_match_time_ms, args.engine_b_match_time_ms)):
            parser.error("protocol v2 uses --period-time-ms/--period-moves instead of --match-time-ms")
    return args


def parse_about_line(line: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for match in re.finditer(r'(\w+)="([^"]*)"|(\w+)=([^,]+)', line):
        key = match.group(1) or match.group(3)
        value = match.group(2) or match.group(4)
        parsed[str(key)] = str(value).strip()
    return parsed


def parse_move_line(line: str) -> tuple[int, int] | None:
    match = MOVE_RE.match(line)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _parse_search_int(value: str) -> int:
    if value.endswith("ms"):
        value = value[:-2]
    return int(value)


def parse_minimax_search_log(line: str) -> dict[str, Any] | None:
    match = MINIMAX_SEARCH_LOG_RE.match(line)
    if match is None:
        return None

    fields = {m.group("key"): m.group("value") for m in MINIMAX_SEARCH_KV_RE.finditer(match.group("body"))}
    if "move" not in fields or "depth" not in fields or "nodes" not in fields or "t" not in fields:
        return None

    move_match = MOVE_RE.match(fields["move"])
    if move_match is None:
        return None

    int_fields = {
        "depth",
        "score",
        "nodes",
        "t",
        "maxply",
        "complete",
        "root",
        "book",
        "thseq",
        "nps",
        "tt",
        "threat_nodes",
        "vcf_nodes",
        "win_nodes",
        "vcf_hits",
        "winv",
        "soft",
        "hard",
        "maxnodes",
        "threads",
        "panic",
        "last_iter_ms",
        "next_iter_est_ms",
        "def_before",
        "def_after",
        "def_applied",
        "nofilter_diff",
        "nofilter_in_before",
        "filtered_best",
    }
    parsed: dict[str, Any] = {
        "move": {"x": int(move_match.group(1)), "y": int(move_match.group(2))},
    }
    for key, value in fields.items():
        if key == "move":
            continue
        if key in int_fields:
            parsed[key] = _parse_search_int(value)
        else:
            parsed[key] = value
    return parsed


def parse_engine_search_records(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for event in events:
        line = str(event.get("line", ""))
        parsed = parse_minimax_search_log(line)
        if parsed is None:
            continue
        parsed["ts"] = event.get("ts")
        records.append(parsed)
    return records


def summarize_engine_search_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    search_count = len(records)
    depth_total = 0
    max_depth: int | None = None
    max_ply: int | None = None
    total_nodes = 0
    total_log_time_ms = 0
    root_total = 0
    root_count = 0
    complete_count = 0
    depth0_count = 0
    normal_depth_total = 0
    normal_search_count = 0
    book_count = 0
    threat_sequence_count = 0
    panic_count = 0
    def_filter_applied_count = 0
    def_filter_before_total = 0
    def_filter_after_total = 0
    nofilter_diff_count = 0
    nofilter_in_before_count = 0
    filtered_best_count = 0
    stop_reason_counts: dict[str, int] = {}
    last_iter_ms_total = 0
    last_iter_ms_count = 0
    next_iter_est_ms_total = 0
    next_iter_est_ms_count = 0
    source_counts: dict[str, int] = {}

    for record in records:
        depth = int(record.get("depth", 0))
        depth_total += depth
        if max_depth is None or depth > max_depth:
            max_depth = depth
        if "maxply" in record:
            ply = int(record["maxply"])
            max_ply = ply if max_ply is None else max(max_ply, ply)

        total_nodes += int(record.get("nodes", 0))
        total_log_time_ms += int(record.get("t", 0))
        if "root" in record:
            root_total += int(record["root"])
            root_count += 1
        complete_count += 1 if int(record.get("complete", 0)) != 0 else 0
        depth0_count += 1 if depth == 0 else 0
        book_count += 1 if int(record.get("book", 0)) != 0 else 0
        threat_sequence_count += 1 if int(record.get("thseq", 0)) != 0 else 0
        panic_count += 1 if int(record.get("panic", 0)) != 0 else 0
        if int(record.get("def_applied", 0)) != 0:
            def_filter_applied_count += 1
            def_filter_before_total += int(record.get("def_before", 0))
            def_filter_after_total += int(record.get("def_after", 0))
        nofilter_diff_count += 1 if int(record.get("nofilter_diff", 0)) != 0 else 0
        nofilter_in_before_count += 1 if int(record.get("nofilter_in_before", 0)) != 0 else 0
        filtered_best_count += 1 if int(record.get("filtered_best", 0)) != 0 else 0
        stop_reason = str(record.get("stop_reason", "unknown"))
        stop_reason_counts[stop_reason] = stop_reason_counts.get(stop_reason, 0) + 1
        if "last_iter_ms" in record:
            last_iter_ms_total += int(record["last_iter_ms"])
            last_iter_ms_count += 1
        if "next_iter_est_ms" in record:
            next_iter_est_ms_total += int(record["next_iter_est_ms"])
            next_iter_est_ms_count += 1
        source = str(record.get("src", "unknown"))
        source_counts[source] = source_counts.get(source, 0) + 1
        if source == "search" and 0 < depth < 20:
            normal_search_count += 1
            normal_depth_total += depth

    return {
        "search_count": search_count,
        "depth_total": depth_total,
        "avg_depth": None if search_count == 0 else depth_total / search_count,
        "max_depth": max_depth,
        "max_ply": max_ply,
        "complete_count": complete_count,
        "depth0_count": depth0_count,
        "normal_search_count": normal_search_count,
        "normal_depth_total": normal_depth_total,
        "avg_normal_depth": None if normal_search_count == 0 else normal_depth_total / normal_search_count,
        "total_nodes": total_nodes,
        "total_log_time_ms": total_log_time_ms,
        "mnps": None if total_log_time_ms == 0 else total_nodes / (total_log_time_ms * 1000.0),
        "root_total": root_total,
        "root_count": root_count,
        "avg_root": None if root_count == 0 else root_total / root_count,
        "book_count": book_count,
        "threat_sequence_count": threat_sequence_count,
        "panic_count": panic_count,
        "def_filter_applied_count": def_filter_applied_count,
        "avg_def_filter_before": None if def_filter_applied_count == 0 else def_filter_before_total / def_filter_applied_count,
        "avg_def_filter_after": None if def_filter_applied_count == 0 else def_filter_after_total / def_filter_applied_count,
        "nofilter_diff_count": nofilter_diff_count,
        "nofilter_in_before_count": nofilter_in_before_count,
        "filtered_best_count": filtered_best_count,
        "stop_reason_counts": stop_reason_counts,
        "avg_last_iter_ms": None if last_iter_ms_count == 0 else last_iter_ms_total / last_iter_ms_count,
        "avg_next_iter_est_ms": None if next_iter_est_ms_count == 0 else next_iter_est_ms_total / next_iter_est_ms_count,
        "source_counts": source_counts,
    }


def summarize_engine_search_logs(events: list[dict[str, Any]]) -> dict[str, Any]:
    return summarize_engine_search_records(parse_engine_search_records(events))


def effective_timeout(override: int | None, fallback: int | None) -> int | None:
    if override is not None and override <= 0:
        return None
    if fallback is not None and fallback <= 0:
        return None
    return override if override is not None else fallback


def default_log_dir() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return REPO_ROOT / "results" / f"match_{stamp}"


def choose_openings_schedule(games: int, openings: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
    if not openings:
        return [None] * games
    return [openings[game_index % len(openings)] for game_index in range(games)]


def make_engine_specs(args: argparse.Namespace) -> tuple[EngineSpec, EngineSpec]:
    if args.protocol_version == PROTOCOL_V2:
        shared_turn = args.time_ms if args.time_ms is not None and args.time_ms > 0 else None
        shared_match = args.period_time_ms
    else:
        shared_turn = args.time_ms if args.time_ms is not None else 1000
        shared_turn = shared_turn if shared_turn > 0 else None
        shared_match = args.match_time_ms if args.match_time_ms is None or args.match_time_ms > 0 else None
    return (
        EngineSpec(
            slot="a",
            name=args.engine_a_name,
            command=args.engine_a_cmd,
            turn_timeout_ms=effective_timeout(args.engine_a_time_ms, shared_turn),
            match_timeout_ms=effective_timeout(args.engine_a_match_time_ms, shared_match),
            max_memory_bytes=args.max_memory_bytes,
            protocol_version=args.protocol_version,
            period_time_ms=args.period_time_ms if args.protocol_version == PROTOCOL_V2 else None,
            period_moves=args.period_moves if args.protocol_version == PROTOCOL_V2 else None,
        ),
        EngineSpec(
            slot="b",
            name=args.engine_b_name,
            command=args.engine_b_cmd,
            turn_timeout_ms=effective_timeout(args.engine_b_time_ms, shared_turn),
            match_timeout_ms=effective_timeout(args.engine_b_match_time_ms, shared_match),
            max_memory_bytes=args.max_memory_bytes,
            protocol_version=args.protocol_version,
            period_time_ms=args.period_time_ms if args.protocol_version == PROTOCOL_V2 else None,
            period_moves=args.period_moves if args.protocol_version == PROTOCOL_V2 else None,
        ),
    )


def choose_black_slots(args: argparse.Namespace, rng: random.Random) -> list[str]:
    black_slots: list[str] = []
    first_black = args.black
    if args.swap_colors:
        if first_black == "random":
            first_black = rng.choice(["a", "b"])
        black_slots = [first_black if game_index % 2 == 0 else other_slot(first_black) for game_index in range(args.games)]
        return black_slots

    for _ in range(args.games):
        if args.black == "random":
            black_slots.append(rng.choice(["a", "b"]))
        else:
            black_slots.append(args.black)
    return black_slots


def other_slot(slot: str) -> str:
    return "b" if slot == "a" else "a"


def slot_color(slot: str, black_slot: str) -> int:
    return BLACK if slot == black_slot else WHITE


def send_board_position(runtime: EngineProcess, board: RefereeBoard, actor_color: int, transcript: list[dict[str, Any]]) -> None:
    runtime.send("BOARD", transcript)
    for y in range(board.size):
        for x in range(board.size):
            cell = board.grid[y][x]
            if cell == EMPTY:
                continue
            field = 1 if cell == actor_color else 2
            runtime.send(f"{x},{y},{field}", transcript)
    runtime.send("DONE", transcript)


def opening_record(opening: dict[str, Any] | None) -> dict[str, Any] | None:
    if opening is None:
        return None
    return {
        "id": opening["id"],
        "name": opening.get("name"),
        "ply": opening["ply"],
        "source_path": opening.get("source_path"),
        "source_url": opening.get("source_url"),
        "canonical_key": opening.get("canonical_key"),
        "symmetry_group_size": opening.get("symmetry_group_size"),
        "moves": [dict(move) for move in opening["moves"]],
    }


def apply_opening(board: RefereeBoard, opening: dict[str, Any] | None) -> None:
    if opening is None:
        return
    for index, move in enumerate(opening["moves"], start=1):
        placed = board.place(board.next_color(), int(move["x"]), int(move["y"]))
        if not placed.legal:
            raise ValueError(f"illegal opening move at ply {index}: {move['x']},{move['y']} ({placed.reason})")
        if placed.winner is not None or placed.draw:
            raise ValueError(f"opening ends the game at ply {index}: {move['x']},{move['y']}")


def send_configuration(runtime: EngineProcess, transcript: list[dict[str, Any]]) -> None:
    turn_value = runtime.spec.turn_timeout_ms or 0
    match_value = runtime.spec.match_timeout_ms or 0
    runtime.send(f"INFO timeout_turn {turn_value}", transcript)
    runtime.send(f"INFO timeout_match {match_value}", transcript)
    if runtime.spec.max_memory_bytes is not None:
        runtime.send(f"INFO max_memory {runtime.spec.max_memory_bytes}", transcript)


def make_period_clock(spec: EngineSpec) -> PeriodClock | None:
    if not spec.uses_period_clock():
        return None
    assert spec.period_time_ms is not None
    assert spec.period_moves is not None
    return PeriodClock(
        period_time_ms=spec.period_time_ms,
        period_moves=spec.period_moves,
        time_left_ms=spec.period_time_ms,
        moves_to_reset=spec.period_moves,
    )


def send_turn_context(runtime: EngineProcess, transcript: list[dict[str, Any]], clock: PeriodClock | None) -> None:
    if clock is None:
        return
    runtime.send(f"INFO time_left {clock.time_left_ms}", transcript)
    runtime.send(f"INFO moves_to_reset {clock.moves_to_reset}", transcript)


def charge_period_clock(clock: PeriodClock, elapsed_ms: int) -> None:
    clock.time_left_ms = max(0, clock.time_left_ms - max(0, elapsed_ms))
    clock.moves_to_reset -= 1
    if clock.moves_to_reset <= 0:
        clock.time_left_ms = clock.period_time_ms
        clock.moves_to_reset = clock.period_moves


def initialize_engine(runtime: EngineProcess, transcript: list[dict[str, Any]]) -> None:
    runtime.start()
    runtime.send(f"START {BOARD_SIZE}", transcript)
    reply = runtime.read_response(START_TIMEOUT_S, transcript)
    if reply != "OK":
        raise EngineFailure(f"{runtime.spec.name} rejected START: {reply}")
    runtime.send("ABOUT", transcript)
    about_line = runtime.read_response(ABOUT_TIMEOUT_S, transcript)
    runtime.metadata = EngineMetadata(raw_about=about_line, parsed_about=parse_about_line(about_line))


def restart_engine(runtime: EngineProcess, transcript: list[dict[str, Any]]) -> None:
    runtime.send("RESTART", transcript)
    reply = runtime.read_response(RESTART_TIMEOUT_S, transcript)
    if reply != "OK":
        raise EngineFailure(f"{runtime.spec.name} rejected RESTART: {reply}")


def response_limit_ms(turn_ms: int | None, remaining_match_ms: int | None) -> int | None:
    candidates = [value for value in (turn_ms, remaining_match_ms) if value is not None]
    if not candidates:
        return None
    return max(1, min(candidates))


def result_code(winner_color: int | None) -> str:
    if winner_color == BLACK:
        return "black_win"
    if winner_color == WHITE:
        return "white_win"
    return "draw"


def summarize_game_line(game_record: dict[str, Any]) -> str:
    black_name = game_record["black"]["name"]
    white_name = game_record["white"]["name"]
    result = game_record["result"]
    termination = game_record["termination"]
    move_count = game_record["move_count"]
    return (
        f"Game {game_record['game_index']}: {black_name} (black) vs {white_name} (white) "
        f"-> {result} via {termination} in {move_count} ply"
    )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")


def play_game(
    game_index: int,
    runtimes: dict[str, EngineProcess],
    black_slot: str,
    transcript: list[dict[str, Any]],
    opening: dict[str, Any] | None = None,
) -> dict[str, Any]:
    white_slot = other_slot(black_slot)
    board = RefereeBoard()
    apply_opening(board, opening)
    per_engine_time_ms = {slot: 0 for slot in runtimes}
    period_clocks = {slot: make_period_clock(runtime.spec) for slot, runtime in runtimes.items()}
    engine_log_cursors = {slot: runtime.aux_cursor() for slot, runtime in runtimes.items()}

    game_start = time.time()
    last_move: RefereeMove | None = None if not board.moves else board.moves[-1]
    needs_board_sync = {slot: bool(board.moves) for slot in runtimes}
    fatal = False
    termination = "unknown"
    reason = ""
    winner_color: int | None = None
    winner_slot: str | None = None

    while True:
        color = board.next_color()
        actor_slot = black_slot if color == BLACK else white_slot
        actor = runtimes[actor_slot]
        actor_period_clock = period_clocks[actor_slot]

        remaining_match_ms = None
        if actor_period_clock is not None:
            remaining_match_ms = actor_period_clock.time_left_ms
            if remaining_match_ms <= 0:
                fatal = True
                termination = "timeout"
                reason = f"{actor.spec.name} exhausted its period clock before moving"
                winner_color = WHITE if color == BLACK else BLACK
                winner_slot = other_slot(actor_slot)
                break
        elif actor.spec.match_timeout_ms is not None:
            remaining_match_ms = actor.spec.match_timeout_ms - per_engine_time_ms[actor_slot]
            if remaining_match_ms <= 0:
                fatal = True
                termination = "timeout"
                reason = f"{actor.spec.name} exhausted its whole-game time budget before moving"
                winner_color = WHITE if color == BLACK else BLACK
                winner_slot = other_slot(actor_slot)
                break

        limit_ms = response_limit_ms(actor.spec.turn_timeout_ms, remaining_match_ms)
        grace_ms = 0 if limit_ms is None else timeout_grace_ms(limit_ms)
        timeout_s = None if limit_ms is None else (limit_ms + grace_ms) / 1000.0
        use_board = needs_board_sync[actor_slot]
        command = "BOARD" if use_board else ("BEGIN" if last_move is None else f"TURN {last_move.x},{last_move.y}")

        started = time.monotonic()
        try:
            send_turn_context(actor, transcript, actor_period_clock)
            if use_board:
                send_board_position(actor, board, color, transcript)
            else:
                actor.send(command, transcript)
            reply = actor.read_response(timeout_s, transcript)
        except EngineTimeout:
            fatal = True
            termination = "timeout"
            reason = f"{actor.spec.name} failed to answer {command} in time"
            winner_color = WHITE if color == BLACK else BLACK
            winner_slot = other_slot(actor_slot)
            break
        except EngineExited as exc:
            fatal = True
            termination = "engine_crash"
            reason = str(exc)
            winner_color = WHITE if color == BLACK else BLACK
            winner_slot = other_slot(actor_slot)
            break

        elapsed_ms = int((time.monotonic() - started) * 1000)
        per_engine_time_ms[actor_slot] += elapsed_ms
        needs_board_sync[actor_slot] = False
        if actor_period_clock is not None:
            charge_period_clock(actor_period_clock, elapsed_ms)

        # Same grace on wall-time as on the I/O wait above.
        if limit_ms is not None and elapsed_ms > limit_ms + grace_ms:
            fatal = True
            termination = "timeout"
            reason = f"{actor.spec.name} exceeded the {limit_ms} ms budget (+{grace_ms} ms grace) with a {elapsed_ms} ms reply"
            winner_color = WHITE if color == BLACK else BLACK
            winner_slot = other_slot(actor_slot)
            break

        move = parse_move_line(reply)
        if move is None:
            fatal = True
            termination = "protocol_error"
            reason = f"{actor.spec.name} returned a non-move reply to {command}: {reply}"
            winner_color = WHITE if color == BLACK else BLACK
            winner_slot = other_slot(actor_slot)
            break

        x, y = move
        placed = board.place(color, x, y)
        if not placed.legal:
            fatal = True
            termination = "illegal_move"
            reason = f"{actor.spec.name} played an illegal move {x},{y}: {placed.reason}"
            winner_color = WHITE if color == BLACK else BLACK
            winner_slot = other_slot(actor_slot)
            break

        assert placed.move is not None
        last_move = placed.move

        if placed.winner is not None:
            termination = "five_in_row"
            winner_color = placed.winner
            winner_slot = actor_slot
            reason = f"{actor.spec.name} formed five in a row"
            break
        if placed.draw:
            termination = "board_full"
            winner_color = None
            winner_slot = None
            reason = "board filled without a five-in-a-row"
            break

    game_end = time.time()
    black_runtime = runtimes[black_slot]
    white_runtime = runtimes[white_slot]
    engine_logs = {
        slot: runtimes[slot].aux_since(cursor) for slot, cursor in engine_log_cursors.items()
    }
    engine_search_records = {
        slot: parse_engine_search_records(engine_logs[slot]) for slot in runtimes
    }
    game_record = {
        "game_index": game_index,
        "started_at": game_start,
        "finished_at": game_end,
        "duration_ms": int((game_end - game_start) * 1000),
        "opening": opening_record(opening),
        "black": {
            "slot": black_slot,
            "name": black_runtime.spec.name,
            "command": black_runtime.spec.command,
            "turn_timeout_ms": black_runtime.spec.turn_timeout_ms,
            "match_timeout_ms": black_runtime.spec.match_timeout_ms,
            "protocol_version": black_runtime.spec.protocol_version,
            "period_time_ms": black_runtime.spec.period_time_ms,
            "period_moves": black_runtime.spec.period_moves,
            "about": None if black_runtime.metadata is None else black_runtime.metadata.raw_about,
            "about_fields": {} if black_runtime.metadata is None else black_runtime.metadata.parsed_about,
        },
        "white": {
            "slot": white_slot,
            "name": white_runtime.spec.name,
            "command": white_runtime.spec.command,
            "turn_timeout_ms": white_runtime.spec.turn_timeout_ms,
            "match_timeout_ms": white_runtime.spec.match_timeout_ms,
            "protocol_version": white_runtime.spec.protocol_version,
            "period_time_ms": white_runtime.spec.period_time_ms,
            "period_moves": white_runtime.spec.period_moves,
            "about": None if white_runtime.metadata is None else white_runtime.metadata.raw_about,
            "about_fields": {} if white_runtime.metadata is None else white_runtime.metadata.parsed_about,
        },
        "winner_color": None if winner_color is None else COLOR_NAMES[winner_color],
        "winner_slot": winner_slot,
        "winner_name": None if winner_slot is None else runtimes[winner_slot].spec.name,
        "result": result_code(winner_color),
        "termination": termination,
        "reason": reason,
        "fatal_stop": fatal,
        "move_count": len(board.moves),
        "moves": [
            {
                "ply": move.ply,
                "color": COLOR_NAMES[move.color],
                "engine_slot": black_slot if move.color == BLACK else white_slot,
                "engine_name": runtimes[black_slot].spec.name if move.color == BLACK else runtimes[white_slot].spec.name,
                "x": move.x,
                "y": move.y,
            }
            for move in board.moves
        ],
        "engine_time_ms": {
            slot: {
                "name": runtimes[slot].spec.name,
                "elapsed_ms": per_engine_time_ms[slot],
            }
            for slot in runtimes
        },
        "engine_search_stats": {
            slot: summarize_engine_search_records(engine_search_records[slot]) for slot in runtimes
        },
        "engine_search_records": engine_search_records,
        "final_board_rows": board.final_board_rows(),
        "final_board_ascii": board.render_ascii(),
        "transcript": transcript,
        "engine_logs": engine_logs,
    }
    return game_record


def aggregate_summary(
    specs: dict[str, EngineSpec],
    scheduled_games: int,
    completed_games: list[dict[str, Any]],
    black_slots: list[str],
    log_dir: Path,
) -> dict[str, Any]:
    totals = {
        slot: {
            "name": specs[slot].name,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "total_time_ms": 0,
            "search_count": 0,
            "depth_total": 0,
            "max_search_depth": None,
            "black_games": 0,
            "white_games": 0,
        }
        for slot in specs
    }

    total_moves = 0
    total_duration_ms = 0
    stopped_early = len(completed_games) != scheduled_games
    fatal_games = [game for game in completed_games if game["fatal_stop"]]

    for game in completed_games:
        total_moves += game["move_count"]
        total_duration_ms += game["duration_ms"]
        black_slot = game["black"]["slot"]
        white_slot = game["white"]["slot"]
        totals[black_slot]["black_games"] += 1
        totals[white_slot]["white_games"] += 1

        for slot, engine_time in game["engine_time_ms"].items():
            totals[slot]["total_time_ms"] += engine_time["elapsed_ms"]

        for slot, search_stats in game["engine_search_stats"].items():
            totals[slot]["search_count"] += search_stats["search_count"]
            totals[slot]["depth_total"] += search_stats["depth_total"]
            if search_stats["max_depth"] is not None:
                prev_max = totals[slot]["max_search_depth"]
                totals[slot]["max_search_depth"] = (
                    search_stats["max_depth"]
                    if prev_max is None
                    else max(prev_max, search_stats["max_depth"])
                )

        if game["result"] == "draw":
            totals[black_slot]["draws"] += 1
            totals[white_slot]["draws"] += 1
            continue

        winner_slot = game["winner_slot"]
        loser_slot = other_slot(winner_slot)
        totals[winner_slot]["wins"] += 1
        totals[loser_slot]["losses"] += 1

    return {
        "scheduled_games": scheduled_games,
        "completed_games": len(completed_games),
        "stopped_early": stopped_early,
        "fatal_stop_detected": bool(fatal_games),
        "fatal_games": len(fatal_games),
        "last_failure_reason": None if not fatal_games else fatal_games[-1]["reason"],
        "black_slots": black_slots,
        "average_move_count": 0 if not completed_games else total_moves / len(completed_games),
        "average_duration_ms": 0 if not completed_games else total_duration_ms / len(completed_games),
        "engines": {
            slot: {
                **asdict(specs[slot]),
                **totals[slot],
                "avg_search_depth": (
                    None
                    if totals[slot].get("search_count", 0) == 0
                    else totals[slot]["depth_total"] / totals[slot]["search_count"]
                ),
            }
            for slot in specs
        },
        "log_dir": str(log_dir),
    }


def print_summary(summary: dict[str, Any]) -> None:
    print("")
    print("Tournament Summary")
    print(f"  Completed games: {summary['completed_games']}/{summary['scheduled_games']}")
    print(f"  Average moves: {summary['average_move_count']:.1f}")
    print(f"  Average duration: {summary['average_duration_ms']:.1f} ms")
    if summary["fatal_stop_detected"]:
        print(f"  Fatal stop: yes ({summary['last_failure_reason']})")
    for slot in ("a", "b"):
        engine = summary["engines"][slot]
        print(
            f"  {slot.upper()} {engine['name']}: "
            f"{engine['wins']}W {engine['losses']}L {engine['draws']}D "
            f"({engine['black_games']} black / {engine['white_games']} white)"
        )
        if engine.get("search_count", 0) > 0:
            print(
                f"    avg search depth: {engine['avg_search_depth']:.2f} "
                f"(max {engine['max_search_depth']}, {engine['search_count']} turns)"
            )
    print(f"  Logs: {summary['log_dir']}")


def main() -> int:
    args = parse_args()
    rng = random.Random(args.seed)
    log_dir = args.log_dir or default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    openings = [] if args.openings_file is None else load_openings_file(args.openings_file)

    spec_a, spec_b = make_engine_specs(args)
    specs = {
        "a": spec_a,
        "b": spec_b,
    }
    runtimes = {
        "a": EngineProcess(spec_a),
        "b": EngineProcess(spec_b),
    }
    black_slots = choose_black_slots(args, rng)
    opening_schedule = choose_openings_schedule(args.games, openings)
    games: list[dict[str, Any]] = []
    exit_code = 0

    try:
        initialize_engine(runtimes["a"], [])
        initialize_engine(runtimes["b"], [])

        for game_index in range(1, args.games + 1):
            transcript: list[dict[str, Any]] = []
            if game_index > 1:
                restart_engine(runtimes["a"], transcript)
                restart_engine(runtimes["b"], transcript)
            send_configuration(runtimes["a"], transcript)
            send_configuration(runtimes["b"], transcript)

            game_record = play_game(
                game_index=game_index,
                runtimes=runtimes,
                black_slot=black_slots[game_index - 1],
                transcript=transcript,
                opening=opening_schedule[game_index - 1],
            )
            games.append(game_record)
            write_json(log_dir / f"game_{game_index:03d}.json", game_record)
            append_jsonl(
                log_dir / "games.jsonl",
                {
                    "game_index": game_record["game_index"],
                    "result": game_record["result"],
                    "termination": game_record["termination"],
                    "reason": game_record["reason"],
                    "winner_slot": game_record["winner_slot"],
                    "winner_name": game_record["winner_name"],
                    "move_count": game_record["move_count"],
                    "duration_ms": game_record["duration_ms"],
                    "opening": game_record["opening"],
                    "black": game_record["black"],
                    "white": game_record["white"],
                    "engine_time_ms": game_record["engine_time_ms"],
                    "engine_search_stats": game_record["engine_search_stats"],
                },
            )
            print(summarize_game_line(game_record))

            if game_record["fatal_stop"]:
                exit_code = 1
                break

        summary = aggregate_summary(specs, args.games, games, black_slots, log_dir)
        summary["openings_file"] = None if args.openings_file is None else str(args.openings_file)
        summary["opening_count"] = len(openings)
        write_json(log_dir / "summary.json", summary)
        print_summary(summary)
        if summary["stopped_early"]:
            exit_code = 1
        return exit_code
    finally:
        runtimes["a"].shutdown()
        runtimes["b"].shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
