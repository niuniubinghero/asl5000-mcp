"""Hardware-free tests for vendor recording-data naming / conflict preflight.

No ASL installation, TAI server, ``mcp`` package or network is needed: every
test builds a tiny fake install root and ASLdata directory in a temp folder.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from asl_monitor import recording as rec  # noqa: E402

WHEN = datetime(2026, 11, 13, 10, 30, 0)


def _group(directory: Path, name: str, with_waveform: bool = True) -> None:
    """Create a realistic vendor data group (8 files)."""
    directory.mkdir(parents=True, exist_ok=True)
    for ext in (".aux", ".vol", ".tdms", ".tdms_index", ".avb", ".brb"):
        (directory / f"{name}{ext}").write_bytes(b"header")
    for ext in (".dtb", ".rwb"):
        size = rec.HEADER_ONLY_BYTES + 4096 if with_waveform else rec.HEADER_ONLY_BYTES
        (directory / f"{name}{ext}").write_bytes(b"x" * size)


class TestNameHandling(unittest.TestCase):

    def test_sanitize_replaces_illegal_chars_and_spaces(self):
        self.assertEqual(rec.sanitize_name(r'  A/B:C*D  E?.tdms  '), "A_B_C_D_E_")

    def test_sanitize_strips_extension_and_limits_length(self):
        self.assertEqual(rec.sanitize_name("run_001.tdms"), "run_001")
        self.assertEqual(len(rec.sanitize_name("x" * 200)), 64)

    def test_sanitize_rejects_empty(self):
        for bad in ("", "   ", "..."):
            with self.assertRaises(ValueError):
                rec.sanitize_name(bad)

    def test_recommend_name_has_script_and_timestamp(self):
        self.assertEqual(rec.recommend_name("ADULT_NORMAL", WHEN),
                         "ADULT_NORMAL_20261113_103000")

    def test_recommend_name_without_script(self):
        self.assertEqual(rec.recommend_name("", WHEN), "ASL5000_20261113_103000")

    def test_recommend_name_is_unique_per_second(self):
        a = rec.recommend_name("s", WHEN)
        b = rec.recommend_name("s", datetime(2026, 11, 13, 10, 30, 1))
        self.assertNotEqual(a, b)


class TestDirectoryResolution(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="asl-rec-")
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_default_dir(self):
        self.assertEqual(rec.resolve_data_dir("", self.root), self.root / "ASLdata")

    def test_vendor_relative_path(self):
        self.assertEqual(rec.resolve_data_dir(r"\ASLDATA\sub", self.root),
                         self.root / "ASLDATA" / "sub")

    def test_absolute_custom_dir(self):
        custom = self.root / "my data"
        self.assertEqual(rec.resolve_data_dir(str(custom), self.root), custom)

    def test_relative_custom_dir(self):
        self.assertEqual(rec.resolve_data_dir("custom", self.root), self.root / "custom")

    def test_unresolved_root_returns_none(self):
        with patch.object(rec.cfg, "sw_path", return_value=None), \
             patch.object(rec.cfg, "vars_dir", return_value=None):
            self.assertIsNone(rec.resolve_data_dir("", None))

    def test_asl_root_from_explicit_value(self):
        self.assertEqual(rec.asl_root(self.root), self.root)


class TestGroupInspection(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="asl-rec-")
        self.dir = Path(self._tmp.name) / "ASLdata"
        self.dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_group(self):
        info = rec.inspect_group(self.dir, "NOPE")
        self.assertFalse(info["exists"])
        self.assertEqual(info["file_count"], 0)

    def test_group_is_case_insensitive(self):
        _group(self.dir, "RUN_1")
        self.assertEqual(len(rec.group_files(self.dir, "run_1")), 8)

    def test_group_detects_waveform_presence(self):
        _group(self.dir, "FULL", with_waveform=True)
        info = rec.inspect_group(self.dir, "FULL")
        self.assertTrue(info["exists"])
        self.assertEqual(info["file_count"], 8)
        self.assertTrue(info["has_waveform_data"])
        self.assertFalse(info["header_only"])

    def test_header_only_group(self):
        _group(self.dir, "EMPTY", with_waveform=False)
        info = rec.inspect_group(self.dir, "EMPTY")
        self.assertFalse(info["has_waveform_data"])
        self.assertTrue(info["header_only"])

    def test_other_names_are_not_a_conflict(self):
        _group(self.dir, "RUN")
        (self.dir / "RUN_PLUS.aux").write_bytes(b"x")
        self.assertEqual(len(rec.group_files(self.dir, "RUN")), 8)
        self.assertFalse(rec.inspect_group(self.dir, "NEW")["exists"])

    def test_next_available_name_skips_occupied_suffixes(self):
        _group(self.dir, "RUN")
        self.assertEqual(rec.next_available_name(self.dir, "RUN"), "RUN_1")
        _group(self.dir, "RUN_1")
        self.assertEqual(rec.next_available_name(self.dir, "RUN"), "RUN_2")
        self.assertEqual(rec.next_available_name(self.dir, "FREE"), "FREE")

    def test_conflict_choices_cover_three_user_options(self):
        choices = rec.conflict_choices("RUN", r"\ASLDATA", "RUN_1")
        self.assertEqual([c["id"] for c in choices], ["overwrite", "rename", "suffix"])
        self.assertEqual(choices[0]["run_kwargs"]["ow"], 1)
        self.assertIsNone(choices[1]["run_kwargs"])
        self.assertEqual(choices[2]["run_kwargs"]["name"], "RUN_1")
        self.assertEqual(choices[2]["run_kwargs"]["ow"], 0)
        self.assertTrue(all(c["run_kwargs"] is None or c["run_kwargs"]["wf"] == 1
                            for c in choices))


class TestCheckRun(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="asl-rec-")
        self.root = Path(self._tmp.name)
        self.dir = self.root / "ASLdata"
        self.dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_conflict_action_is_run(self):
        out = rec.check_run("NEW", 1, 0, "", self.root)
        self.assertEqual(out["action"], "run")
        self.assertEqual(out["name"], "NEW")
        self.assertEqual(out["wf"], 1)
        self.assertEqual(out["vendor_path"], rec.DEFAULT_VENDOR_PATH)
        self.assertTrue(out["dir_exists"])
        self.assertEqual(len(out["expected_files"]), len(rec.DATA_EXTS))

    def test_conflict_without_overwrite_needs_user_decision(self):
        _group(self.dir, "RUN")
        out = rec.check_run("RUN", 1, 0, "", self.root)
        self.assertEqual(out["action"], "need_decision")
        self.assertEqual(out["suffix"], "RUN_1")
        self.assertEqual(len(out["options"]), 3)
        self.assertIn("未发 RS", out["error"])

    def test_explicit_overwrite_is_allowed_but_recorded(self):
        _group(self.dir, "RUN")
        out = rec.check_run("RUN", 1, 1, "", self.root)
        self.assertEqual(out["action"], "run")
        self.assertEqual(out["ow"], 1)
        self.assertTrue(out["existing_group"]["exists"])

    def test_suffix_candidate_then_runs_clean(self):
        _group(self.dir, "RUN")
        first = rec.check_run("RUN", 1, 0, "", self.root)
        second = rec.check_run(first["options"][2]["run_kwargs"]["name"],
                               1, 0, "", self.root)
        self.assertEqual(second["action"], "run")
        self.assertEqual(second["name"], "RUN_1")

    def test_wf_zero_warns_about_missing_vendor_files(self):
        out = rec.check_run("NEW", 0, 0, "", self.root)
        self.assertEqual(out["action"], "run")
        self.assertTrue(any("wf!=1" in n for n in out["notes"]))

    def test_custom_dir(self):
        custom = self.root / "custom"
        custom.mkdir()
        out = rec.check_run("NEW", 1, 0, str(custom), self.root)
        self.assertEqual(out["vendor_path"], str(custom))
        self.assertEqual(out["resolved_dir"], str(custom))

    def test_missing_dir_is_reported_not_created(self):
        missing = self.root / "not_created"
        out = rec.check_run("NEW", 1, 0, str(missing), self.root)
        self.assertEqual(out["action"], "run")
        self.assertFalse(out["dir_exists"])
        self.assertFalse(missing.exists())

    def test_bad_name_is_error(self):
        self.assertEqual(rec.check_run("...", 1, 0, "", self.root)["action"], "error")

    def test_unresolved_root_is_error(self):
        with patch.object(rec.cfg, "sw_path", return_value=None), \
             patch.object(rec.cfg, "vars_dir", return_value=None):
            self.assertEqual(rec.check_run("NEW", 1, 0, "", None)["action"], "error")


class TestPrepare(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="asl-rec-")
        self.root = Path(self._tmp.name)
        self.dir = self.root / "ASLdata"
        self.dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_recommended_name_and_no_conflict(self):
        out = rec.prepare(script="Adult_normal", when=WHEN, root=self.root)
        self.assertTrue(out["ok"])
        self.assertEqual(out["name_source"], "recommended")
        self.assertEqual(out["requested_name"], "Adult_normal_20261113_103000")
        self.assertFalse(out["conflict"])
        self.assertEqual(out["default_run_kwargs"]["ow"], 0)
        self.assertEqual(out["default_run_kwargs"]["wf"], 1)

    def test_custom_name_conflict_offers_three_options(self):
        _group(self.dir, "Adult_normal")
        out = rec.prepare(name="Adult_normal", when=WHEN, root=self.root)
        self.assertEqual(out["name_source"], "user")
        self.assertTrue(out["conflict"])
        self.assertEqual(out["existing_group"]["file_count"], 8)
        self.assertEqual(len(out["conflict_options"]), 3)
        self.assertEqual(out["conflict_options"][2]["run_kwargs"]["name"],
                         "Adult_normal_1")

    def test_three_questions_are_returned_for_the_agent(self):
        out = rec.prepare(script="s", when=WHEN, root=self.root)
        self.assertEqual(len(out["ask_user"]), 3)
        self.assertEqual(out["ask_user"][2]["options"], ["覆盖", "改名", "末尾加 _1/_2"])

    def test_custom_path_is_reflected(self):
        out = rec.prepare(name="N", path=r"\ASLDATA\sub", when=WHEN, root=self.root)
        self.assertEqual(out["path_choice"], "custom")
        self.assertEqual(out["vendor_path_arg"], r"\ASLDATA\sub")
        self.assertEqual(out["resolved_dir"], str(self.root / "ASLDATA" / "sub"))

    def test_bad_custom_name_returns_error_with_recommendation(self):
        out = rec.prepare(name="...", script="s", when=WHEN, root=self.root)
        self.assertFalse(out["ok"])
        self.assertIn("recommended_name", out)


if __name__ == "__main__":
    unittest.main()
