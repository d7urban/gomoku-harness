#!/usr/bin/env python3
"""Integration smoke test for the M3 coordinator."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class HarnessSmokeTest(unittest.TestCase):
    def test_two_game_tournament_with_restart_and_logs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gomoku_harness_smoke_") as tmp_dir:
            log_dir = Path(tmp_dir) / "logs"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "harness" / "run_match.py"),
                    "--engine-a-name",
                    "stub-a",
                    "--engine-a-cmd",
                    f"{sys.executable} harness/test_engine.py --name stub-a",
                    "--engine-b-name",
                    "stub-b",
                    "--engine-b-cmd",
                    f"{sys.executable} harness/test_engine.py --name stub-b",
                    "--games",
                    "2",
                    "--swap-colors",
                    "--time-ms",
                    "50",
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            if proc.returncode != 0:
                self.fail(f"run_match failed with code {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")

            summary_path = log_dir / "summary.json"
            games_path = log_dir / "games.jsonl"
            self.assertTrue(summary_path.exists(), "summary.json was not written")
            self.assertTrue(games_path.exists(), "games.jsonl was not written")
            self.assertTrue((log_dir / "game_001.json").exists(), "game_001.json was not written")
            self.assertTrue((log_dir / "game_002.json").exists(), "game_002.json was not written")

            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["completed_games"], 2)
            self.assertFalse(summary["stopped_early"])
            self.assertEqual(summary["engines"]["a"]["search_count"], 0)
            self.assertIsNone(summary["engines"]["a"]["avg_search_depth"])

            lines = games_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            first_game = json.loads(lines[0])
            self.assertIn("engine_search_stats", first_game)

    def test_opening_seed_file_starts_from_seeded_position(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gomoku_harness_opening_smoke_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            log_dir = tmp_path / "logs"
            openings_path = tmp_path / "openings.json"
            openings_path.write_text(
                json.dumps(
                    {
                        "format": "gomoku-harness-openings",
                        "version": 1,
                        "openings": [
                            {
                                "id": "seed-a",
                                "name": "Seed A",
                                "moves": [
                                    {"x": 0, "y": 0},
                                    {"x": 1, "y": 0},
                                    {"x": 0, "y": 1},
                                ],
                            }
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            proc = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "harness" / "run_match.py"),
                    "--engine-a-name",
                    "stub-a",
                    "--engine-a-cmd",
                    f"{sys.executable} harness/test_engine.py --name stub-a",
                    "--engine-b-name",
                    "stub-b",
                    "--engine-b-cmd",
                    f"{sys.executable} harness/test_engine.py --name stub-b",
                    "--games",
                    "1",
                    "--time-ms",
                    "50",
                    "--openings-file",
                    str(openings_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            if proc.returncode != 0:
                self.fail(f"run_match with openings failed with code {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")

            summary = json.loads((log_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["opening_count"], 1)
            self.assertEqual(summary["openings_file"], str(openings_path))

            game = json.loads((log_dir / "game_001.json").read_text(encoding="utf-8"))
            self.assertEqual(game["opening"]["id"], "seed-a")
            self.assertEqual([(move["x"], move["y"]) for move in game["moves"][:3]], [(0, 0), (1, 0), (0, 1)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
