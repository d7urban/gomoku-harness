#!/usr/bin/env python3
"""Canned-transcript smoke test for the GomokuZero Gomocup adapter.

Spawns adapters/zero/run.sh and walks through:
  - START 15 + ABOUT
  - INFO timeout_turn (low, to keep the test fast)
  - BEGIN -> expect a legal first move
  - TURN -> expect a legal reply
  - RESTART -> fresh game, BEGIN again
  - END

Exits 0 on success; nonzero with diagnostics on any failure.
"""

from __future__ import annotations

import subprocess
import sys
import time
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


def expect_move(line: str) -> tuple[int, int]:
    parts = line.split(",")
    assert len(parts) == 2, f"expected X,Y, got {line!r}"
    x, y = int(parts[0]), int(parts[1])
    assert 0 <= x < BOARD and 0 <= y < BOARD, f"move out of bounds: {line!r}"
    return x, y


def send(proc: subprocess.Popen[str], text: str) -> None:
    print(f">> {text}")
    proc.stdin.write(text + "\n")  # type: ignore[union-attr]
    proc.stdin.flush()  # type: ignore[union-attr]


def recv(proc: subprocess.Popen[str]) -> str:
    deadline = time.time() + READ_TIMEOUT_S
    line = read_line(proc, deadline)
    print(f"<< {line}")
    return line


def main() -> int:
    proc = subprocess.Popen(
        [str(RUN_SH), "--sims", "32"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        text=True,
        bufsize=1,
    )
    try:
        send(proc, "START 15")
        assert recv(proc) == "OK"

        send(proc, "ABOUT")
        about = recv(proc)
        assert about.startswith("name="), f"unexpected ABOUT: {about!r}"

        send(proc, "INFO timeout_turn 50")
        # No response expected; small delay to let the engine consume it.
        time.sleep(0.05)

        played: set[tuple[int, int]] = set()

        send(proc, "BEGIN")
        move = expect_move(recv(proc))
        played.add(move)

        # Send an opponent move that is definitely empty.
        opp = (0, 0) if move != (0, 0) else (1, 0)
        send(proc, f"TURN {opp[0]},{opp[1]}")
        played.add(opp)
        reply = expect_move(recv(proc))
        assert reply not in played, f"engine replayed an occupied cell: {reply}"
        played.add(reply)

        send(proc, "RESTART")
        assert recv(proc) == "OK"

        send(proc, "BEGIN")
        fresh_move = expect_move(recv(proc))
        # No state assertion here; just a legality check.
        assert 0 <= fresh_move[0] < BOARD and 0 <= fresh_move[1] < BOARD

        send(proc, "END")
        proc.wait(timeout=5)
        assert proc.returncode == 0, f"adapter exit code {proc.returncode}"
    except Exception as exc:  # noqa: BLE001
        print(f"SMOKE TEST FAILED: {exc}", file=sys.stderr)
        proc.kill()
        return 1

    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
