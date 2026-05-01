#!/usr/bin/env python3
"""Tiny deterministic Gomocup test engine for coordinator smoke tests."""

from __future__ import annotations

import argparse
import sys

BOARD_SIZE = 15
EMPTY = 0
SELF = 1
OPP = 2


def write_line(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


class TestEngine:
    def __init__(self) -> None:
        self.board = [[EMPTY for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]

    def reset(self) -> None:
        self.board = [[EMPTY for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]

    def choose_move(self) -> tuple[int, int]:
        for y in range(BOARD_SIZE):
            for x in range(BOARD_SIZE):
                if self.board[y][x] == EMPTY:
                    self.board[y][x] = SELF
                    return x, y
        raise RuntimeError("board is full")

    def apply_opp(self, x: int, y: int) -> None:
        if not (0 <= x < BOARD_SIZE and 0 <= y < BOARD_SIZE):
            raise ValueError(f"out of bounds: {x},{y}")
        if self.board[y][x] != EMPTY:
            raise ValueError(f"occupied: {x},{y}")
        self.board[y][x] = OPP


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic Gomocup test engine")
    parser.add_argument("--name", default="test-engine")
    args = parser.parse_args()

    engine = TestEngine()

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        head, _, rest = line.partition(" ")
        cmd = head.upper()
        if cmd == "START":
            if int(rest) != BOARD_SIZE:
                write_line("ERROR unsupported size")
            else:
                engine.reset()
                write_line("OK")
        elif cmd == "RESTART":
            engine.reset()
            write_line("OK")
        elif cmd == "ABOUT":
            write_line(
                f'name="{args.name}", version="0.1.0", author="gomoku-harness", country="SE"'
            )
        elif cmd == "INFO":
            continue
        elif cmd == "BEGIN":
            x, y = engine.choose_move()
            write_line(f"{x},{y}")
        elif cmd == "TURN":
            x_str, y_str = rest.split(",")
            engine.apply_opp(int(x_str), int(y_str))
            x, y = engine.choose_move()
            write_line(f"{x},{y}")
        elif cmd == "BOARD":
            engine.reset()
            for board_raw in sys.stdin:
                board_line = board_raw.strip()
                if not board_line:
                    continue
                if board_line.upper() == "DONE":
                    break
                x_str, y_str, field_str = board_line.split(",")
                x = int(x_str)
                y = int(y_str)
                field = int(field_str)
                if field == SELF:
                    engine.board[y][x] = SELF
                elif field == OPP:
                    engine.board[y][x] = OPP
            x, y = engine.choose_move()
            write_line(f"{x},{y}")
        elif cmd == "END":
            return 0
        else:
            write_line(f"UNKNOWN {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
