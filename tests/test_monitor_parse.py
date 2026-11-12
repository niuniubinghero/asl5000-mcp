"""Broadcast message parsing tests. No network access."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from asl_monitor import monitor as am  # noqa: E402


class BroadcastParsingTests(unittest.TestCase):
    def test_parse_breath_params(self):
        text = "Breath Number\t12\nBreath Rate\t15.0\n\nnot-a-pair\n\t\n"
        out = am.parse_breath_params(text)
        self.assertEqual(out["Breath Number"], "12")
        self.assertEqual(out["Breath Rate"], "15.0")
        self.assertNotIn("", out)

    def test_parse_wave_message_skips_repeated_header(self):
        header = "Time\tAirway Pressure\tTotal Volume\tFlow"
        text = "\n".join([header, "0.0\t4.0\t100\t10", header, "0.1\t4.5\t110\t12"])
        h, rows = am.parse_wave_message(text)
        self.assertEqual(h, header.split("\t"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][1], "4.5")

    def test_parse_wave_message_empty(self):
        h, rows = am.parse_wave_message("")
        self.assertIsNone(h)
        self.assertEqual(rows, [])

    def test_col_index_is_case_insensitive(self):
        header = ["Time", "Airway Pressure", "Total Volume", "Flow"]
        self.assertEqual(am.col_index(header, "time"), 0)
        self.assertEqual(am.col_index(header, "airway", "pressure"), 1)
        self.assertEqual(am.col_index(header, "total volume"), 2)
        self.assertIsNone(am.col_index(header, "missing"))
        self.assertIsNone(am.col_index(None, "time"))

    def test_wave_window_stats_limits_samples_by_time(self):
        header = ["Time", "Airway Pressure", "Total Volume", "Flow"]
        rows = [[str(i / 10), str(4 + i), str(100 + i), str(10 + i)] for i in range(20)]
        stats = am.wave_window_stats(header, rows, 0.5)
        self.assertIn("samples", stats)
        self.assertLessEqual(stats["samples"], 6)
        self.assertGreater(stats["samples"], 0)

    def test_wave_window_stats_handles_empty_input(self):
        self.assertEqual(am.wave_window_stats(None, [], 5.0), {})
        self.assertEqual(am.wave_window_stats(["Time"], [], 5.0), {})


if __name__ == "__main__":
    unittest.main()
