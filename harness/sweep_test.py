import argparse
import unittest

from harness.run_match import PROTOCOL_V1
from harness.sweep import build_cells, make_numeric_cell_spec


class SweepCellSpecTest(unittest.TestCase):
    def test_make_numeric_cell_spec_matches_play_cell_contract(self) -> None:
        spec = make_numeric_cell_spec(3, 750)

        self.assertEqual(spec["index"], 3)
        self.assertEqual(spec["label"], "750ms")
        self.assertEqual(spec["time_ms"], 750)
        self.assertEqual(spec["protocol_version"], PROTOCOL_V1)
        self.assertIsNone(spec["period_time_ms"])
        self.assertIsNone(spec["period_moves"])

    def test_build_numeric_cells_uses_same_shape_as_bisect_cells(self) -> None:
        args = argparse.Namespace(protocol_version=PROTOCOL_V1, times_ms="100,250")

        cells = build_cells(args)

        self.assertEqual(cells[0], make_numeric_cell_spec(0, 100))
        self.assertEqual(cells[1], make_numeric_cell_spec(1, 250))


if __name__ == "__main__":
    unittest.main()
