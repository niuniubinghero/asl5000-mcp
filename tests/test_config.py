"""Path discovery and user config tests. No hardware or vendor software needed."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from asl_monitor import config as cfg  # noqa: E402


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="asl-config-"))
        self.sw = self.tmp / "ASL5000_SW3.6.exe"
        self.tai = self.tmp / "ASL_TAI.exe"
        self.sw.write_text("stub", encoding="ascii")
        self.tai.write_text("stub", encoding="ascii")
        self.env = {
            "ASL_MONITOR_CONFIG": str(self.tmp / "config.json"),
            "ASL_MONITOR_RECORDINGS": str(self.tmp / "recordings"),
            "ASL_SW_PATH": "",
            "ASL_TAI_EXE": "",
            "ASL_ROOT": str(self.tmp),
        }
        self.patcher = patch.dict(os.environ, self.env)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_config_file_uses_override(self):
        self.assertEqual(cfg.config_file(), Path(self.env["ASL_MONITOR_CONFIG"]))

    def test_environment_variable_has_priority(self):
        with patch.dict(os.environ, {"ASL_SW_PATH": str(self.sw)}):
            self.assertEqual(cfg.sw_path(), str(self.sw))

    def test_user_config_is_used_when_env_is_empty(self):
        cfg.write_config({"asl_sw_path": str(self.sw), "asl_tai_exe": str(self.tai)})
        self.assertEqual(cfg.sw_path(), str(self.sw))
        self.assertEqual(cfg.tai_exe(), str(self.tai))

    def test_save_paths_persists_and_returns_effective_values(self):
        out = cfg.save_paths(str(self.sw), str(self.tai))
        self.assertEqual(out["asl_sw_path"], str(self.sw))
        self.assertEqual(out["config_file"], str(cfg.config_file()))
        self.assertEqual(cfg.read_config()["asl_tai_exe"], str(self.tai))

    def test_missing_env_path_is_ignored(self):
        with patch.dict(os.environ, {"ASL_SW_PATH": str(self.tmp / "missing.exe")}):
            self.assertIsNone(cfg._env_path("ASL_SW_PATH"))

    def test_vars_dir_follows_software_path(self):
        with patch.dict(os.environ, {"ASL_SW_PATH": str(self.sw)}):
            self.assertEqual(cfg.vars_dir(), self.tmp / "vars")

    def test_recordings_dir_override(self):
        self.assertEqual(cfg.recordings_dir(), Path(self.env["ASL_MONITOR_RECORDINGS"]))
        self.assertTrue(cfg.recordings_dir().exists())

    def test_tai_address_override(self):
        with patch.dict(os.environ, {"ASL_TAI_HOST": "192.0.2.1", "ASL_TAI_PORT": "7001"}):
            self.assertEqual(cfg.tai_host(), "192.0.2.1")
            self.assertEqual(cfg.tai_port(), 7001)
        with patch.dict(os.environ, {"ASL_TAI_PORT": "not-a-number"}):
            self.assertEqual(cfg.tai_port(), 6341)

    def test_no_hardcoded_drive_letters_in_source(self):
        import re

        source = Path(cfg.__file__).read_text(encoding="utf-8")
        self.assertFalse(re.search(r"[A-Za-z]:\\\\", source),
                         "config.py must not contain machine-specific absolute paths")


if __name__ == "__main__":
    unittest.main()
