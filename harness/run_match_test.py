#!/usr/bin/env python3
"""Unit tests for harness.run_match clock helpers."""

from __future__ import annotations

import unittest

from harness.run_match import (
    EngineSpec,
    PROTOCOL_V1,
    PROTOCOL_V2,
    charge_period_clock,
    make_period_clock,
    parse_minimax_search_log,
    summarize_engine_search_logs,
)


class RunMatchClockTest(unittest.TestCase):
    def test_make_period_clock_disabled_for_v1(self) -> None:
        spec = EngineSpec(
            slot="a",
            name="engine-a",
            command="true",
            turn_timeout_ms=1000,
            match_timeout_ms=None,
            max_memory_bytes=None,
            protocol_version=PROTOCOL_V1,
            period_time_ms=300_000,
            period_moves=40,
        )
        self.assertIsNone(make_period_clock(spec))

    def test_period_clock_resets_after_last_move_in_period(self) -> None:
        spec = EngineSpec(
            slot="a",
            name="engine-a",
            command="true",
            turn_timeout_ms=None,
            match_timeout_ms=300_000,
            max_memory_bytes=None,
            protocol_version=PROTOCOL_V2,
            period_time_ms=300_000,
            period_moves=2,
        )
        clock = make_period_clock(spec)
        assert clock is not None

        charge_period_clock(clock, 10_000)
        self.assertEqual(clock.time_left_ms, 290_000)
        self.assertEqual(clock.moves_to_reset, 1)

        charge_period_clock(clock, 20_000)
        self.assertEqual(clock.time_left_ms, 300_000)
        self.assertEqual(clock.moves_to_reset, 2)

    def test_parse_legacy_minimax_search_log(self) -> None:
        parsed = parse_minimax_search_log("[minimax] move=4,9 depth=5 score=-28 nodes=1036520 t=9957ms")

        assert parsed is not None
        self.assertEqual(parsed["move"], {"x": 4, "y": 9})
        self.assertEqual(parsed["depth"], 5)
        self.assertEqual(parsed["score"], -28)
        self.assertEqual(parsed["nodes"], 1_036_520)
        self.assertEqual(parsed["t"], 9_957)

    def test_parse_extended_minimax_search_log(self) -> None:
        parsed = parse_minimax_search_log(
            "[minimax] move=4,9 depth=5 score=-28 nodes=1036520 t=9957ms "
            "maxply=8 complete=1 root=24 src=search nps=104095 tt=12 "
            "threat_nodes=30 vcf_nodes=40 win_nodes=50 vcf_hits=1 winv=2 "
            "soft=750 hard=1000 maxnodes=500000 threads=4 book=0 thseq=1 panic=0 "
            "stop_reason=affordability last_iter_ms=300 next_iter_est_ms=900 "
            "def_before=24 def_after=2 def_applied=1 def_reason=simple_four "
            "def_removed=1,1|2,2 nofilter=1,1 nofilter_diff=1 nofilter_in_before=1 filtered_best=1 pv=4,9|5,9"
        )

        assert parsed is not None
        self.assertEqual(parsed["src"], "search")
        self.assertEqual(parsed["complete"], 1)
        self.assertEqual(parsed["root"], 24)
        self.assertEqual(parsed["maxply"], 8)
        self.assertEqual(parsed["threads"], 4)
        self.assertEqual(parsed["book"], 0)
        self.assertEqual(parsed["thseq"], 1)
        self.assertEqual(parsed["panic"], 0)
        self.assertEqual(parsed["stop_reason"], "affordability")
        self.assertEqual(parsed["last_iter_ms"], 300)
        self.assertEqual(parsed["next_iter_est_ms"], 900)
        self.assertEqual(parsed["def_before"], 24)
        self.assertEqual(parsed["def_after"], 2)
        self.assertEqual(parsed["def_applied"], 1)
        self.assertEqual(parsed["def_reason"], "simple_four")
        self.assertEqual(parsed["def_removed"], "1,1|2,2")
        self.assertEqual(parsed["nofilter"], "1,1")
        self.assertEqual(parsed["nofilter_diff"], 1)
        self.assertEqual(parsed["nofilter_in_before"], 1)
        self.assertEqual(parsed["filtered_best"], 1)
        self.assertEqual(parsed["pv"], "4,9|5,9")

    def test_summarize_extended_search_logs(self) -> None:
        stats = summarize_engine_search_logs(
            [
                {"line": "[minimax] move=4,9 depth=5 score=-28 nodes=1000000 t=10000ms maxply=8 complete=1 root=24 src=search nps=100000 threads=4 stop_reason=soft_limit last_iter_ms=3000 next_iter_est_ms=9000"},
                {"line": "[minimax] move=5,9 depth=0 score=12 nodes=0 t=0ms maxply=0 complete=1 root=1 src=opening_book nps=0 threads=4 stop_reason=opening_book"},
            ]
        )

        self.assertEqual(stats["search_count"], 2)
        self.assertEqual(stats["depth0_count"], 1)
        self.assertEqual(stats["normal_search_count"], 1)
        self.assertEqual(stats["avg_normal_depth"], 5)
        self.assertEqual(stats["avg_root"], 12.5)
        self.assertEqual(stats["book_count"], 0)
        self.assertEqual(stats["threat_sequence_count"], 0)
        self.assertEqual(stats["panic_count"], 0)
        self.assertEqual(stats["def_filter_applied_count"], 0)
        self.assertEqual(stats["filtered_best_count"], 0)
        self.assertEqual(stats["stop_reason_counts"], {"soft_limit": 1, "opening_book": 1})
        self.assertEqual(stats["avg_last_iter_ms"], 3000)
        self.assertEqual(stats["avg_next_iter_est_ms"], 9000)
        self.assertEqual(stats["source_counts"], {"search": 1, "opening_book": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
