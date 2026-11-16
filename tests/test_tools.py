"""Hardware-free tests for the standalone tools in ``tools/``.

No ASL installation, vendor files or network access are required: the tests
build a tiny fake install root in a temporary directory.
"""

from __future__ import annotations

import contextlib
import csv
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import _paths  # noqa: E402
import build_script_index as bsi  # noqa: E402
import make_lung_model  # noqa: E402

# One real-shaped .vr3 line: module / description / type / name / = / value / unit.
VR3_LINE = ("  (lung_model)\t r_trachea_in             \t (float)   \t"
            "              r_trachea_in\t =\t6.000000\t (cm-H2O/(l/sec)   \t ()\r\n")


def _fake_root(root: Path) -> None:
    (root / "vars" / "scenarios").mkdir(parents=True)
    (root / "breath").mkdir(parents=True)
    (root / "ASL5000_SW3.6.exe").write_bytes(b"")
    (root / "vars" / "scenarios" / "Adult_normal.vr3").write_bytes(
        VR3_LINE.encode("latin-1"))
    (root / "vars" / "scenarios" / "demo.sct").write_bytes(
        b"1000\t<ASLVarsDirectory>\\scenarios\\Adult_normal.vr3\r\n")
    (root / "breath" / "wave.in").write_text("0.10\n0.20\n0.30\n", encoding="utf-8")


class TestPathDiscovery(unittest.TestCase):

    def test_cli_value_wins_over_environment(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            (Path(a) / "vars").mkdir()
            (Path(b) / "vars").mkdir()
            with patch.dict(os.environ, {"ASL_ROOT": b}):
                self.assertEqual(_paths.resolve_root(a), Path(a).resolve())

    def test_env_root(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "vars").mkdir()
            with patch.dict(os.environ, {"ASL_ROOT": d, "ASL_SW_PATH": ""}):
                self.assertEqual(_paths.resolve_root(), Path(d).resolve())

    def test_sw_path_parent(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "install"
            (root / "vars").mkdir(parents=True)
            exe = root / "ASL5000_SW3.6.exe"
            exe.write_bytes(b"")
            with patch.dict(os.environ, {"ASL_ROOT": "", "ASL_SW_PATH": str(exe)}):
                self.assertEqual(_paths.resolve_root(), root.resolve())

    def test_cutoff_fallback_and_inferred(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertEqual(_paths.default_official_cutoff(root), "2019-01-01")
            (root / "ASL5000_SW3.6.exe").write_bytes(b"")
            self.assertRegex(_paths.default_official_cutoff(root), r"^\d{4}-\d{2}-\d{2}$")


class TestMakeVr3(unittest.TestCase):

    def test_set_vars_preserves_layout(self):
        new_lines, applied, missing = make_lung_model.set_vars([VR3_LINE], {"r_trachea_in": 21})
        self.assertEqual(len(applied), 1)
        self.assertEqual(missing, [])
        self.assertEqual(applied[0][1], "6.000000")
        self.assertEqual(applied[0][2], "21.000000")
        out = new_lines[0]
        self.assertTrue(out.endswith("\r\n"))
        self.assertEqual(out.count("\t"), VR3_LINE.count("\t"))
        self.assertIn("\t21.000000\t", out)
        self.assertIn("(float)", out)  # untouched columns survive

    def test_set_vars_case_insensitive_and_reports_unknown(self):
        _lines, applied, missing = make_lung_model.set_vars(
            [VR3_LINE], {"R_TRACHEA_IN": 30, "not_a_variable": 1})
        self.assertEqual(len(applied), 1)
        self.assertEqual(missing, ["not_a_variable"])

    def test_list_vars(self):
        mod, name, val, unit = make_lung_model.list_vars([VR3_LINE])[0]
        self.assertEqual((mod, name, val), ("lung_model", "r_trachea_in", "6.000000"))
        self.assertTrue(unit.startswith("("))

    def test_resolve_out_uses_vars_dir(self):
        with tempfile.TemporaryDirectory() as d:
            vars_dir = Path(d) / "vars"
            self.assertEqual(
                Path(make_lung_model.resolve_out("scenarios/x.vr3", vars_dir)),
                vars_dir / "scenarios" / "x.vr3")
            self.assertEqual(
                Path(make_lung_model.resolve_out("x", vars_dir, outdir=d)),
                Path(d) / "x.vr3")

    def test_sct_wrapper_handles_cross_drive(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            model = root / "vars" / "model.vr3"
            model.parent.mkdir(parents=True)
            model.write_bytes(b"x")
            with patch("make_lung_model.os.path.relpath",
                       side_effect=ValueError("path is on another drive")):
                sct = Path(make_lung_model.make_sct(str(model), root / "vars"))
            data = sct.read_bytes()
            self.assertNotIn(b"<ASLVarsDirectory>", data)
            self.assertIn(b"model.vr3", data)
            self.assertTrue(data.endswith(b"\r\n"))

    def test_sct_wrapper_format(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            model = root / "vars" / "scenarios" / "model.vr3"
            model.parent.mkdir(parents=True)
            model.write_bytes(b"x")
            sct = Path(make_lung_model.make_sct(str(model), root / "vars", reps=1000))
            data = sct.read_bytes()
            self.assertTrue(data.startswith(b"1000\t"))
            self.assertIn(b"<ASLVarsDirectory>", data)
            self.assertTrue(data.endswith(b"\r\n"))
            self.assertEqual(sct.name, "model.sct")


class TestSctOnly(unittest.TestCase):
    """--sct-only: wrap an existing .vr3 without reading a template or rewriting it."""

    MTIME = 1600000000

    def _write_model(self, root: Path) -> Path:
        model = root / "vars" / "scenarios" / "legacy.vr3"
        model.parent.mkdir(parents=True)
        model.write_bytes(VR3_LINE.encode("latin-1"))
        os.utime(model, (self.MTIME, self.MTIME))
        return model

    def _run(self, *argv):
        out = io.StringIO()
        with patch("sys.argv", ["make_lung_model.py", *argv]), \
                contextlib.redirect_stdout(out):
            code = make_lung_model.main()
        return code, out.getvalue()

    def test_creates_wrapper_without_touching_model(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            model = self._write_model(root)
            before = model.read_bytes()
            code, stdout = self._run("--sct-only", "--out", str(model),
                                     "--asl-root", str(root))
            self.assertEqual(code, 0, stdout)
            sct = model.with_suffix(".sct")
            self.assertTrue(sct.is_file())
            data = sct.read_bytes()
            self.assertTrue(data.startswith(b"1000\t"))
            self.assertIn(b"<ASLVarsDirectory>", data)
            self.assertTrue(data.endswith(b"\r\n"))
            # model bytes AND mtime must be untouched
            self.assertEqual(model.read_bytes(), before)
            self.assertEqual(model.stat().st_mtime, self.MTIME)

    def test_missing_model_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "vars").mkdir()
            target = root / "vars" / "scenarios" / "nope.vr3"
            code, stdout = self._run("--sct-only", "--out", str(target),
                                     "--asl-root", str(root))
            self.assertEqual(code, 1)
            self.assertIn("不存在", stdout)
            self.assertFalse(target.with_suffix(".sct").exists())

    def test_existing_wrapper_skipped_without_force(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            model = self._write_model(root)
            sct = model.with_suffix(".sct")
            sct.write_bytes(b"555\tOLD\r\n")
            code, stdout = self._run("--sct-only", "--out", str(model),
                                     "--asl-root", str(root))
            self.assertEqual(code, 0, stdout)
            self.assertIn("跳过", stdout)
            self.assertEqual(sct.read_bytes(), b"555\tOLD\r\n")

    def test_force_overwrites_existing_wrapper(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            model = self._write_model(root)
            sct = model.with_suffix(".sct")
            sct.write_bytes(b"555\tOLD\r\n")
            code, stdout = self._run("--sct-only", "--out", str(model),
                                     "--force", "--asl-root", str(root))
            self.assertEqual(code, 0, stdout)
            self.assertTrue(sct.read_bytes().startswith(b"1000\t"))

    def test_set_rejected_in_sct_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            model = self._write_model(root)
            code, stdout = self._run("--sct-only", "--out", str(model),
                                     "--set", "r_trachea_in=21",
                                     "--asl-root", str(root))
            self.assertEqual(code, 1)
            self.assertIn("--set", stdout)

    def test_batch_sct_only_without_template(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._write_model(root)
            batch = Path(d) / "batch.json"
            batch.write_text('[{"sct_only": true, "out": "scenarios/legacy.vr3"}]',
                             encoding="utf-8")
            code, stdout = self._run("--from-json", str(batch),
                                     "--asl-root", str(root))
            self.assertEqual(code, 0, stdout)
            sct = root / "vars" / "scenarios" / "legacy.sct"
            self.assertTrue(sct.is_file())
            self.assertIn(b"<ASLVarsDirectory>", sct.read_bytes())

    def test_template_still_required_for_normal_mode(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "vars").mkdir()
            code, stdout = self._run("--out", "x.vr3", "--asl-root", str(root))
            self.assertEqual(code, 1)
            self.assertIn("--template", stdout)


class TestScriptIndex(unittest.TestCase):

    def test_parse_name(self):
        info = bsi.parse_name("15bpm_IER1_2_R20_C50.sct")
        self.assertEqual(info.get("bpm"), "15")
        self.assertEqual(info.get("ier"), "1:2")
        self.assertEqual(info.get("r"), "20")
        self.assertEqual(info.get("c"), "50")

    def test_total_compliance(self):
        self.assertEqual(bsi.total_compliance(
            {"c_left_s0_a1": "40", "c_right_s0_a1": "40"}), "50.0")

    def test_classify_source_uses_cutoff(self):
        self.assertEqual(bsi.classify_source("vars/scenarios/a.vr3",
                                             "2018-01-01 00:00", "2019-01-01"), "官方")
        self.assertEqual(bsi.classify_source("vars/scenarios/a.vr3",
                                             "2020-01-01 00:00", "2019-01-01"), "自制")
        self.assertEqual(bsi.classify_source("vars/current.sct",
                                             "2020-01-01 00:00", "2019-01-01"), "运行时")

    def test_parse_sct_sums_repetitions_not_seconds(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vd = root / "vars" / "scenarios"
            vd.mkdir(parents=True)
            (vd / "model.vr3").write_bytes(b"x")
            sct = vd / "multi.sct"
            # Vendor-shaped 3-segment scenario. The first column is a repetition
            # count, not a duration (values like 1000 / 150 / 11 are common).
            sct.write_bytes(
                b"1000\t<ASLVarsDirectory>\\scenarios\\model.vr3\r\n"
                b"150\t<ASLVarsDirectory>\\scenarios\\model.vr3\r\n"
                b"11\t<ASLVarsDirectory>\\scenarios\\model.vr3\r\n")
            segs, reps, refs, missing = bsi.parse_sct(str(sct), root / "vars")
            self.assertEqual((segs, reps, missing), (3, 1161, 0))
            self.assertEqual(len(refs), 1)
            self.assertEqual(int(reps), reps)  # integral repetition count, not a duration

    def test_end_to_end_index(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "install"
            out = Path(d) / "index"
            _fake_root(root)
            with contextlib.redirect_stdout(io.StringIO()):
                bsi.main(root, str(out), "2019-01-01")
            for name in bsi.CSV_NAMES.values():
                self.assertTrue((out / name).is_file(), name)
            with (out / "script_index.csv").open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 3)  # one .vr3, one .sct, one .in
            self.assertEqual(len(rows[0]), 32)
            sct_rows = [r for r in rows if r["文件名"] == "demo.sct"]
            self.assertEqual(len(sct_rows), 1)
            self.assertEqual(sct_rows[0]["sct引用检查"], "正常")
            self.assertIn("sct首列合计(重复次数)", rows[0])
            self.assertNotIn("sct总时长(分)", rows[0])
            self.assertEqual(sct_rows[0]["sct首列合计(重复次数)"], "1000")
            vr3_rows = [r for r in rows if r["文件名"] == "Adult_normal.vr3"]
            self.assertEqual(vr3_rows[0]["vr3气道阻力(in/out)"], "6.000000/")


class TestNoMachinePaths(unittest.TestCase):

    def test_sources_have_no_hardcoded_install_paths(self):
        # Match real machine paths, not the literal "\n" inside a source string
        # (e.g. "...following:\n" would otherwise look like a drive letter).
        # Needles are split so this test file itself does not look like a leak.
        needles = "|".join(("928" + "58", "yu" + "well", "asl_ai" + "_tool",
                            "Python" + "314", "test-" + "BiPAP", "HU " + "YUFENG"))
        pat = re.compile(
            r"[A-Za-z]:[\\/]{1,2}(?:Program Files|Users|Windows|Python|ai_tools)"
            + "|" + needles)
        for f in TOOLS_DIR.glob("*.py"):
            text = f.read_text(encoding="utf-8")
            self.assertIsNone(pat.search(text), "%s contains a machine-specific string" % f.name)


if __name__ == "__main__":
    unittest.main()
