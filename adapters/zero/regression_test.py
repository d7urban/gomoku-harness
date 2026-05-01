#!/usr/bin/env python3
"""Regression tests for known GomokuZero adapter protocol bugs.

These tests encode the *correct* protocol behavior and are therefore expected
to fail until the corresponding adapter bugs are fixed.
"""

from __future__ import annotations

import select
import subprocess
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUN_SH = HERE / "run.sh"
BOARD = 15
READ_TIMEOUT_S = 60.0


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


def try_read_line(proc: subprocess.Popen[str], timeout_s: float) -> str | None:
    if proc.poll() is not None:
        raise RuntimeError(f"adapter exited early with code {proc.returncode}")
    ready, _, _ = select.select([proc.stdout], [], [], timeout_s)  # type: ignore[arg-type]
    if not ready:
        return None
    line = proc.stdout.readline()  # type: ignore[union-attr]
    return line.rstrip("\n") if line else None


def expect_move(line: str) -> tuple[int, int]:
    parts = line.split(",")
    if len(parts) != 2:
        raise AssertionError(f"expected X,Y, got {line!r}")
    x, y = int(parts[0]), int(parts[1])
    if not (0 <= x < BOARD and 0 <= y < BOARD):
        raise AssertionError(f"move out of bounds: {line!r}")
    return x, y


class ZeroAdapterRegressionTest(unittest.TestCase):
    def start_proc(self) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [str(RUN_SH), "--sims", "32"],
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

    def test_illegal_turn_does_not_trigger_reply_move(self) -> None:
        proc = self.start_proc()
        try:
            self.send(proc, "START 15")
            self.assertEqual(self.recv(proc), "OK")

            self.send(proc, "BEGIN")
            opener = expect_move(self.recv(proc))

            self.send(proc, f"TURN {opener[0]},{opener[1]}")
            line = self.recv(proc)
            self.assertTrue(
                line.startswith("ERROR "),
                f"illegal TURN should be rejected, got {line!r}",
            )

            extra = try_read_line(proc, timeout_s=0.5)
            self.assertIsNone(
                extra,
                f"adapter must not emit a move after rejecting TURN, got {extra!r}",
            )
        finally:
            self.stop_proc(proc)

    def test_board_rejects_out_of_bounds_cells(self) -> None:
        proc = self.start_proc()
        try:
            self.send(proc, "START 15")
            self.assertEqual(self.recv(proc), "OK")

            self.send(proc, "BOARD")
            self.send(proc, "-1,0,1")
            self.send(proc, "0,0,2")
            self.send(proc, "DONE")

            line = self.recv(proc)
            self.assertTrue(
                line.startswith("ERROR "),
                f"BOARD with out-of-bounds cells must fail, got {line!r}",
            )
        finally:
            self.stop_proc(proc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
