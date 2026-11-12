"""TAI frame / parameter parsing tests. No sockets are opened."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from asl_monitor import tai as tc  # noqa: E402


class TaiParsingTests(unittest.TestCase):
    def test_parse_kv_lines_extracts_multiple_pairs(self):
        out = tc.parse_kv_lines(["ASL_STATUS=IDLE SIM_STATUS=STOP SIM_BREATH_CNT=42"])
        self.assertEqual(out["ASL_STATUS"], "IDLE")
        self.assertEqual(out["SIM_STATUS"], "STOP")
        self.assertEqual(out["SIM_BREATH_CNT"], "42")

    def test_parse_kv_lines_accepts_tab_separated_rows(self):
        out = tc.parse_kv_lines(["P_PEAK\t12.3", "P_MEAN\t4.5"])
        self.assertEqual(out["P_PEAK"], "12.3")
        self.assertEqual(out["P_MEAN"], "4.5")

    def test_parse_kv_lines_skips_noise(self):
        out = tc.parse_kv_lines([">ASL1234:", "plain text without pairs"])
        self.assertEqual(out, {})

    def test_frame_detection(self):
        payload = b"SIM_STATUS=IDLE\r\n"
        framed = len(payload).to_bytes(4, "big") + payload
        self.assertTrue(tc.TAIClient._detect_framing(framed))
        self.assertFalse(tc.TAIClient._detect_framing(payload))
        self.assertFalse(tc.TAIClient._detect_framing(b"\x00\x00\x00\x05"))

    def test_ic_parameter_whitelist_has_core_groups(self):
        for name in ("C1", "C2", "R1", "RATE", "INSP_PMUS", "TRIGGERTIME",
                     "LOOP", "VTSET", "ETCO2SET", "PATH"):
            self.assertIn(name, tc.IC_ALL_PARAMS)

    def test_bp_default_set_is_cpas_oriented(self):
        defaults = set(tc.BP_DEFAULT_PARAMS)
        for name in ("P_PEAK", "P_PEEP", "P_PEEP_AUTO_1", "VOL_VENT_VT_INSP",
                     "TIME_TRIG_RESP", "WORK_PAT_INSP"):
            self.assertIn(name, defaults)

    def test_tc_fields_include_run_state(self):
        for name in ("ASL_STATUS", "SIM_STATUS", "SIM_BREATH_CNT", "SIM_TIME_REMAIN"):
            self.assertIn(name, tc.TC_FIELDS)

    def test_error_code_table_covers_common_failures(self):
        for code in ("03", "05", "08", "11", "14", "16"):
            self.assertIn(code, tc.ERROR_DESCRIPTIONS)


if __name__ == "__main__":
    unittest.main()
