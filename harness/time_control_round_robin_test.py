#!/usr/bin/env python3
"""Unit tests for time-control round-robin scheduling."""

from __future__ import annotations

import unittest

from harness.time_control_round_robin import DEFAULT_ENGINE_CMD, build_pair_game_plan, format_duration_ms


class TimeControlRoundRobinScheduleTest(unittest.TestCase):
    def test_openings_are_mirrored_with_colors_swapped(self) -> None:
        openings = [{"id": "opening-a"}, {"id": "opening-b"}]

        plan = build_pair_game_plan(6, openings)

        self.assertEqual([item["black_slot"] for item in plan], ["a", "b", "a", "b", "a", "b"])
        self.assertEqual(
            [item["opening"]["id"] for item in plan],
            ["opening-a", "opening-a", "opening-b", "opening-b", "opening-a", "opening-a"],
        )
        self.assertEqual([item["opening_pair_index"] for item in plan], [1, 1, 2, 2, 3, 3])

    def test_rejects_odd_games_per_pair(self) -> None:
        with self.assertRaises(ValueError):
            build_pair_game_plan(5, [{"id": "opening-a"}])

    def test_default_engine_command_uses_fixed_threads(self) -> None:
        self.assertIn("--threads 4", DEFAULT_ENGINE_CMD)

    def test_format_duration_ms(self) -> None:
        self.assertEqual(format_duration_ms(2_400), "2s")
        self.assertEqual(format_duration_ms(125_000), "2m05s")
        self.assertEqual(format_duration_ms(3_725_000), "1h02m05s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
