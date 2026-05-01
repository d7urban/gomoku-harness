#!/usr/bin/env python3
"""Regression tests for known gomoku-minimax adapter protocol bugs.

These tests encode the *correct* protocol behavior and are therefore expected
to fail until the corresponding adapter bugs are fixed.
"""

from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUN_SH = HERE / "run.sh"
BOARD = 15
READ_TIMEOUT_S = 30.0


def read_line(proc: subprocess.Popen[str], deadline: float) -> str:
    while True:
        if proc.poll() is not None:
            raise RuntimeError(f"adapter exited early with code {proc.returncode}")
        if time.time() > deadline:
            raise RuntimeError("timed out waiting for adapter response")
        line = proc.stdout.readline()  # type: ignore[union-attr]
        if line:
            return line.rstrip("\n")
        time.sleep(0.01)


def expect_move(line: str) -> tuple[int, int]:
    parts = line.split(",")
    if len(parts) != 2:
        raise AssertionError(f"expected X,Y, got {line!r}")
    x, y = int(parts[0]), int(parts[1])
    if not (0 <= x < BOARD and 0 <= y < BOARD):
        raise AssertionError(f"move out of bounds: {line!r}")
    return x, y


class MinimaxAdapterRegressionTest(unittest.TestCase):
    def start_proc(self) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [str(RUN_SH), "--controller", "expert", "--time-ms", "100"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            bufsize=1,
        )

    def send(self, proc: subprocess.Popen[str], text: str) -> None:
        print(f">> {text}")
        proc.stdin.write(text + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

    def recv(self, proc: subprocess.Popen[str], timeout_s: float = READ_TIMEOUT_S) -> str:
        line = read_line(proc, time.time() + timeout_s)
        print(f"<< {line}")
        return line

    def stop_proc(self, proc: subprocess.Popen[str]) -> None:
        try:
            if proc.poll() is None:
                try:
                    self.send(proc, "END")
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    proc.kill()
                    proc.wait(timeout=5)
        finally:
            if proc.stdin is not None:
                proc.stdin.close()
            if proc.stdout is not None:
                proc.stdout.close()

    def run_board_case(self, board_lines: list[str]) -> tuple[int, int]:
        proc = self.start_proc()
        try:
            self.send(proc, "START 15")
            self.assertEqual(self.recv(proc), "OK")

            self.send(proc, "BOARD")
            for line in board_lines:
                self.send(proc, line)
            self.send(proc, "DONE")
            return expect_move(self.recv(proc))
        finally:
            self.stop_proc(proc)

    def test_info_timeout_turn_does_not_reset_live_game(self) -> None:
        proc = self.start_proc()
        try:
            self.send(proc, "START 15")
            self.assertEqual(self.recv(proc), "OK")

            self.send(proc, "BEGIN")
            opener = expect_move(self.recv(proc))

            self.send(proc, "INFO timeout_turn 1000")
            time.sleep(0.05)

            self.send(proc, f"TURN {opener[0]},{opener[1]}")
            line = self.recv(proc)
            self.assertTrue(
                line.startswith("ERROR "),
                f"changing timeout must not clear the board, got {line!r}",
            )
        finally:
            self.stop_proc(proc)

    def test_board_is_order_invariant(self) -> None:
        ordered_move = self.run_board_case(
            [
                "8,7,1",
                "7,7,2",
                "7,8,2",
            ]
        )
        permuted_move = self.run_board_case(
            [
                "8,7,1",
                "7,8,2",
                "7,7,2",
            ]
        )
        self.assertEqual(
            ordered_move,
            permuted_move,
            "BOARD reconstruction should depend only on the position, not line order",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
