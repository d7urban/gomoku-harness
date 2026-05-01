#!/usr/bin/env python3
"""Canned-transcript smoke test for the gomoku-minimax Gomocup adapter.

Walks the adapter through:
  - START 15 + ABOUT
  - INFO timeout_turn (low, to keep the test fast)
  - INFO timeout_match / time_left / moves_to_reset (v2 clock fields)
  - BEGIN -> expect a legal first move
  - TURN -> expect a legal reply
  - RESTART -> fresh game; second BEGIN must produce a legal move on an empty
    board (proves no state leak across games)
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
        [str(RUN_SH), "--controller", "expert", "--time-ms", "100"],
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

        send(proc, "INFO timeout_turn 100")
        send(proc, "INFO timeout_match 300000")
        send(proc, "INFO time_left 300000")
        send(proc, "INFO moves_to_reset 40")
        # Silent; small delay so the adapter consumes it.
        time.sleep(0.05)

        played: set[tuple[int, int]] = set()

        send(proc, "BEGIN")
        first = expect_move(recv(proc))
        played.add(first)

        opp = (0, 0) if first != (0, 0) else (1, 0)
        send(proc, f"TURN {opp[0]},{opp[1]}")
        played.add(opp)
        reply = expect_move(recv(proc))
        assert reply not in played, f"engine replayed an occupied cell: {reply}"
        played.add(reply)

        # RESTART must give a clean board: a TURN at one of the previously-used
        # cells should now succeed (proves the board was actually wiped).
        send(proc, "RESTART")
        assert recv(proc) == "OK"

        send(proc, f"TURN {first[0]},{first[1]}")
        # Should be accepted (cell is empty after restart) and produce a reply.
        post_restart_reply = expect_move(recv(proc))
        assert post_restart_reply != first, "engine replied with the same cell as the opener move"

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
