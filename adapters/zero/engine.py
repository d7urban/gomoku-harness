#!/usr/bin/env python3
"""Gomocup adapter for the GomokuZero-player engine.

Speaks the harness subset documented in ../../docs/protocol.md over stdin/stdout.
Wire format: (X, Y) zero-indexed, X = column, Y = row, origin top-left.
GomokuZero uses board[row, col] with the same orientation, so translation is a
single (X, Y) <-> (col, row) swap.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Silence TF chatter before import.
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
os.environ.setdefault("TF_FORCE_GPU_ALLOW_GROWTH", "true")

ZERO_REPO = Path(__file__).resolve().parents[2].parent / "GomokuZero-player"
sys.path.insert(0, str(ZERO_REPO))

# Load from the working directory of GomokuZero-player so weight discovery works.
os.chdir(ZERO_REPO)

from gomoku import BOARD_SIZE, EMPTY, PLAYER1, PLAYER2, GomokuGame  # noqa: E402
from entrypoint_shared import (  # noqa: E402
    AIPlayer,
    load_model_and_predict_fn,
    select_weights,
)

NAME = "GomokuZero-player"
VERSION = "0.1.0-gomocup"
AUTHOR = "GomokuZero-player"
COUNTRY = "—"

# GomokuZero's difficulty is *defined* as an MCTS sim count (250 = easy,
# 500 = medium, 2000 = hard). The adapter therefore takes --sims at launch as
# the primary control. INFO timeout_turn is honored only as an upper-bound
# safety cap -- if the timeout-derived sim count is lower than --sims, we
# clamp; otherwise we play the full configured sims.
#
# Calibration measured by adapters/zero/calibrate_sims.py on the target
# machine (RTX 3090). Throughput is affine (per-call setup ~30-80 ms + per-sim
# cost) and position-dependent: clustered midgame ~2.5 sims/ms, sparse
# wide-frontier ~0.4 sims/ms. We model the slow case so the cap doesn't push
# above wall-clock budget for typical positions; pathological spread positions
# may still overrun, in which case the harness's own timeout is authoritative.
SIMS_PER_MS = 1.0
SETUP_MS = 80
MIN_SIMS = 16
MAX_SIMS = 20000


def write_line(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def log(text: str) -> None:
    sys.stderr.write(f"[zero] {text}\n")
    sys.stderr.flush()


class ZeroEngine:
    def __init__(self, sims: int, weights: str | None) -> None:
        self.default_sims = sims
        self.timeout_turn_ms: int | None = None
        self.timeout_match_ms: int | None = None
        wf, label = (weights, "explicit") if weights else select_weights(mode="best")
        if not wf:
            raise SystemExit("No weights file found for GomokuZero.")
        log(f"loading weights: {wf} ({label})")
        t0 = time.time()
        self._model, predict_fn = load_model_and_predict_fn(wf)
        log(f"weights loaded in {time.time() - t0:.1f}s")
        self.ai = AIPlayer(predict_fn, simulations=sims, difficulty="harness")
        self.game = GomokuGame()

    # --- protocol ---------------------------------------------------------

    def cmd_start(self, size: int) -> None:
        if size != BOARD_SIZE:
            write_line(f"ERROR unsupported size {size} (only {BOARD_SIZE})")
            return
        self.game = GomokuGame()
        write_line("OK")

    def cmd_restart(self) -> None:
        self.game = GomokuGame()
        write_line("OK")

    def cmd_about(self) -> None:
        write_line(
            f'name="{NAME}", version="{VERSION}", author="{AUTHOR}", country="{COUNTRY}"'
        )

    def cmd_info(self, key: str, value: str) -> None:
        if key == "timeout_turn":
            ms = int(value)
            self.timeout_turn_ms = ms if ms > 0 else None
        elif key == "timeout_match":
            ms = int(value)
            self.timeout_match_ms = ms if ms > 0 else None
        # Unknown keys: silently ignored per spec.

    def cmd_begin(self) -> None:
        self._reply_with_move()

    def cmd_turn(self, x: int, y: int) -> None:
        if not self._apply_opponent_move(x, y):
            return
        self._reply_with_move()

    def cmd_board(self, lines: list[tuple[int, int, int]]) -> None:
        my_cells: list[tuple[int, int]] = []
        opp_cells: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        for x, y, field in lines:
            if not (0 <= x < BOARD_SIZE and 0 <= y < BOARD_SIZE):
                write_line(f"ERROR BOARD cell out of bounds: {x},{y}")
                return
            if field not in {1, 2, 3}:
                write_line(f"ERROR BOARD field must be 1, 2, or 3: {field}")
                return
            if field == 3:
                continue
            if (x, y) in seen:
                write_line(f"ERROR duplicate BOARD cell: {x},{y}")
                return
            seen.add((x, y))
            if field == 1:
                my_cells.append((x, y))
            else:
                opp_cells.append((x, y))

        # Brain is to move next, so my_count == opp_count means we are PLAYER1
        # (black), opp_count == my_count + 1 means we are PLAYER2 (white).
        if len(my_cells) == len(opp_cells):
            our = PLAYER1
        elif len(opp_cells) == len(my_cells) + 1:
            our = PLAYER2
        else:
            write_line(
                f"ERROR malformed BOARD: my={len(my_cells)} opp={len(opp_cells)}"
            )
            return
        opp = -our

        self.game = GomokuGame()
        for x, y in my_cells:
            self.game.board[y, x] = our
            self.game.move_history.append((y, x, our))
            self.game._update_frontier(y, x, 1)
        for x, y in opp_cells:
            self.game.board[y, x] = opp
            self.game.move_history.append((y, x, opp))
            self.game._update_frontier(y, x, 1)
        self.game.current_player = our
        self._reply_with_move()

    # --- move generation --------------------------------------------------

    def _apply_opponent_move(self, x: int, y: int) -> bool:
        if not (0 <= x < BOARD_SIZE and 0 <= y < BOARD_SIZE):
            write_line(f"ERROR opponent move out of bounds: {x},{y}")
            return False
        if self.game.board[y, x] != EMPTY:
            write_line(f"ERROR opponent move on occupied cell: {x},{y}")
            return False
        _, _ = self.game.make_move(y, x)
        return True

    def _reply_with_move(self) -> None:
        sims = self._sims_for_turn()
        prev_sims = self.ai.sims
        self.ai.sims = sims
        try:
            t0 = time.time()
            row, col, val = self.ai.get_move(self.game)
            elapsed_ms = int((time.time() - t0) * 1000)
        finally:
            self.ai.sims = prev_sims
        if self.game.board[row, col] != EMPTY:
            write_line(f"ERROR engine produced occupied cell: {col},{row}")
            return
        self.game.make_move(row, col)
        log(f"move={col},{row} sims={sims} eval={val:+.3f} t={elapsed_ms}ms")
        write_line(f"{col},{row}")

    def _sims_for_turn(self) -> int:
        if self.timeout_turn_ms is None:
            return self.default_sims
        cap = int(max(0, self.timeout_turn_ms - SETUP_MS) * SIMS_PER_MS)
        cap = max(MIN_SIMS, min(MAX_SIMS, cap))
        return min(self.default_sims, cap)


def parse_board_block() -> list[tuple[int, int, int]]:
    rows: list[tuple[int, int, int]] = []
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        if line.upper() == "DONE":
            return rows
        parts = line.split(",")
        if len(parts) != 3:
            write_line(f"ERROR malformed BOARD line: {line}")
            continue
        rows.append((int(parts[0]), int(parts[1]), int(parts[2])))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Gomocup adapter for GomokuZero")
    parser.add_argument(
        "--sims",
        type=int,
        default=500,
        help="Default MCTS sim count when no INFO timeout_turn is provided.",
    )
    parser.add_argument(
        "--weights",
        type=str,
        default=None,
        help="Optional explicit path to a .h5 weights file.",
    )
    args = parser.parse_args()

    engine = ZeroEngine(sims=args.sims, weights=args.weights)
    log("ready")

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        head, _, rest = line.partition(" ")
        cmd = head.upper()
        try:
            if cmd == "START":
                engine.cmd_start(int(rest))
            elif cmd == "RESTART":
                engine.cmd_restart()
            elif cmd == "BEGIN":
                engine.cmd_begin()
            elif cmd == "TURN":
                x_str, y_str = rest.split(",")
                engine.cmd_turn(int(x_str), int(y_str))
            elif cmd == "BOARD":
                rows = parse_board_block()
                engine.cmd_board(rows)
            elif cmd == "INFO":
                key, _, value = rest.partition(" ")
                engine.cmd_info(key.strip(), value.strip())
            elif cmd == "ABOUT":
                engine.cmd_about()
            elif cmd == "END":
                return 0
            else:
                write_line(f"UNKNOWN {line}")
        except Exception as exc:  # noqa: BLE001
            write_line(f"ERROR {cmd}: {exc}")
            log(f"exception handling {cmd!r}: {exc!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
