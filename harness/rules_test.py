#!/usr/bin/env python3
"""Unit tests for the harness referee rules."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.rules import BLACK, RefereeBoard, WHITE


class RefereeBoardTest(unittest.TestCase):
    def test_rejects_wrong_turn_and_occupied_cells(self) -> None:
        board = RefereeBoard()
        wrong_turn = board.place(WHITE, 7, 7)
        self.assertFalse(wrong_turn.legal)
        self.assertIn("wrong side to move", wrong_turn.reason or "")

        first = board.place(BLACK, 7, 7)
        self.assertTrue(first.legal)

        occupied = board.place(WHITE, 7, 7)
        self.assertFalse(occupied.legal)
        self.assertIn("occupied", occupied.reason or "")

    def test_detects_horizontal_five(self) -> None:
        board = RefereeBoard()
        sequence = [
            (BLACK, 3, 7),
            (WHITE, 0, 0),
            (BLACK, 4, 7),
            (WHITE, 0, 1),
            (BLACK, 5, 7),
            (WHITE, 0, 2),
            (BLACK, 6, 7),
            (WHITE, 0, 3),
            (BLACK, 7, 7),
        ]
        result = None
        for color, x, y in sequence:
            result = board.place(color, x, y)
            self.assertTrue(result.legal)
        assert result is not None
        self.assertEqual(result.winner, BLACK)

    def test_detects_diagonal_five(self) -> None:
        board = RefereeBoard()
        sequence = [
            (BLACK, 2, 2),
            (WHITE, 0, 0),
            (BLACK, 3, 3),
            (WHITE, 0, 1),
            (BLACK, 4, 4),
            (WHITE, 0, 2),
            (BLACK, 5, 5),
            (WHITE, 0, 3),
            (BLACK, 6, 6),
        ]
        result = None
        for color, x, y in sequence:
            result = board.place(color, x, y)
            self.assertTrue(result.legal)
        assert result is not None
        self.assertEqual(result.winner, BLACK)


if __name__ == "__main__":
    unittest.main(verbosity=2)
