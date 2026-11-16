"""Hardware-free tests for the vendor recording parser (``vendor_data.py``).

Builds synthetic self-describing groups in a temporary directory: an ASCII
header followed by a big-endian float32 body, shaped like the real vendor
files (500-byte .rwb/.dtb headers, .dtb with a truncated label list).
"""

from __future__ import annotations

import csv
import struct
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from asl_monitor import vendor_data as vd  # noqa: E402

HEADER_TEXT = (
    "ASL 5000 data file demo\r\n"
    "Version 3.0\r\n"
    "500 byte header\r\n"
    "The column labels are as follows:\r\n"
    "Time (sec)\r\n"
    "Airway Pressure (cmH2O)\r\n"
    "Volume (mL)\r\n"
)


def _header(labels: list[str], size: int = 500) -> bytes:
    text = ("ASL 5000 data file demo\r\nVersion 3.0\r\n"
            f"{size} byte header\r\n"
            "The column labels are as follows:\r\n"
            + "".join(ln + "\r\n" for ln in labels))
    assert len(text) < size, "labels must fit in the declared header"
    return text.encode("latin-1") + b"x" * (size - len(text))


def _body(ncols: int, nrows: int, scale: float = 1.0) -> bytes:
    vals = [float(i * ncols + j) * scale for i in range(nrows) for j in range(ncols)]
    return struct.pack(">%df" % len(vals), *vals)


def _write_group(directory: Path, name: str, labels: list[str],
                 ncols: int, nrows: int, scale: float = 1.0) -> None:
    (directory / f"{name}.rwb").write_bytes(
        _header(labels) + _body(ncols, nrows, scale))


RW_LABELS = ["Time (sec)", "Airway Pressure (cmH2O)", "Volume (mL)"]


class TestParseHeader(unittest.TestCase):

    def test_labels_and_size(self):
        hs, labels, desc = vd.parse_header(_header(RW_LABELS))
        self.assertEqual(hs, 500)
        self.assertEqual(labels, RW_LABELS)
        self.assertIn("demo", desc)

    def test_no_header_declaration(self):
        self.assertEqual(vd.parse_header(b"\x00" * 512), (None, [], ""))


class TestReadArray(unittest.TestCase):

    def test_roundtrip_and_stats(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "demo.rwb"
            p.write_bytes(_header(RW_LABELS) + _body(3, 4))
            arr = vd.read_array(p)
            self.assertEqual((arr.nrows, arr.ncols), (4, 3))
            self.assertEqual(arr.labels, RW_LABELS)
            col = arr.column("Airway")  # prefix match, case-insensitive
            self.assertEqual(col, [1.0, 4.0, 7.0, 10.0])
            s = arr.stats(0)
            self.assertAlmostEqual(s["min"], 0.0)
            self.assertAlmostEqual(s["max"], 9.0)
            self.assertAlmostEqual(s["mean"], 4.5)

    def test_header_only_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "empty.rwb"
            p.write_bytes(_header(RW_LABELS))
            arr = vd.read_array(p)
            self.assertEqual(arr.nrows, 0)
            self.assertEqual(arr.stats(0)["n"], 0)

    def test_dtb_truncated_header_gets_index_names(self):
        with tempfile.TemporaryDirectory() as d:
            # 12 declared labels but a 16-column body — the real .dtb defect.
            labels = [f"col{i}" for i in range(12)]
            p = Path(d) / "demo.dtb"
            p.write_bytes(_header(labels) + _body(16, 4))
            arr = vd.read_array(p, nrows_hint=4)
            self.assertEqual(arr.ncols, 16)
            self.assertEqual(arr.nrows, 4)
            self.assertEqual(arr.labels[:12], labels)
            self.assertTrue(arr.labels[12].startswith("Tracheal Pressure"))
            self.assertTrue(all("dtb_col" in lab for lab in arr.labels[13:]))
            self.assertTrue(any("截断" in n for n in arr.notes))


class TestDataGroup(unittest.TestCase):

    def _group(self, root: Path) -> vd.DataGroup:
        _write_group(root, "demo", RW_LABELS, 3, 4)
        return vd.DataGroup(root, "demo")

    def test_summary_and_scan(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._group(root)
            groups = vd.scan_dir(root)
            self.assertEqual(len(groups), 1)
            meta = groups[0]
            self.assertEqual(meta["rwb_rows"], 4)
            self.assertEqual(meta["dtb_rows"], 0)
            self.assertTrue(meta["has_waveform"])
            # col0 = [0, 3, 6, 9] -> duration 9 s, 3 steps -> 1/3 Hz
            self.assertAlmostEqual(meta["duration_sec"], 9.0, places=2)
            self.assertAlmostEqual(meta["sample_rate_hz"], 0.33, delta=0.01)

    def test_scan_shallow_skips_binary(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._group(root)
            groups = vd.scan_dir(root, deep=False)
            self.assertEqual([g["name"] for g in groups], ["demo"])
            self.assertNotIn("rwb_rows", groups[0])

    def test_dtb_aligns_with_rwb_row_hint(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write_group(root, "demo", RW_LABELS, 3, 4)
            labels = [f"col{i}" for i in range(12)]
            (root / "demo.dtb").write_bytes(_header(labels) + _body(16, 4))
            g = vd.DataGroup(root, "demo")
            self.assertEqual(g.wave("dtb").nrows, 4)
            self.assertEqual(g.time(), [0.0, 3.0, 6.0, 9.0])

    def test_to_csv_bom_and_columns(self):
        with tempfile.TemporaryDirectory() as d:
            g = self._group(Path(d))
            out = g.wave("rwb").to_csv(Path(d) / "out.csv", columns=[0, "Volume (mL)"])
            raw = out.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            with out.open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.reader(fh))
            self.assertEqual(rows[0], ["Time (sec)", "Volume (mL)"])
            self.assertEqual(len(rows), 5)  # header + 4 data rows
            self.assertEqual(rows[1], ["0", "2"])


class TestMcpHelpers(unittest.TestCase):

    def _arr(self) -> vd.VendorArray:
        import tempfile
        d = tempfile.mkdtemp()
        p = Path(d) / "demo.rwb"
        p.write_bytes(_header(RW_LABELS) + _body(3, 4))
        return vd.read_array(p)

    def test_select_columns_empty_means_all(self):
        arr = self._arr()
        idxs, err = vd.select_columns(arr, "")
        self.assertIsNone(idxs)
        self.assertEqual(err, "")

    def test_select_columns_names_and_indexes(self):
        arr = self._arr()
        idxs, err = vd.select_columns(arr, "Time (sec),2,airway")
        self.assertEqual(err, "")
        self.assertEqual(idxs, [0, 2, 1])

    def test_select_columns_errors(self):
        arr = self._arr()
        idxs, err = vd.select_columns(arr, "9")
        self.assertIsNone(idxs)
        self.assertIn("越界", err)
        idxs, err = vd.select_columns(arr, "no_such_column")
        self.assertIsNone(idxs)
        self.assertIn("找不到列", err)
        idxs, err = vd.select_columns(arr, " , ")
        self.assertIsNone(idxs)
        self.assertIn("为空", err)

    def test_stats_block_keys_follow_labels(self):
        arr = self._arr()
        idxs, _ = vd.select_columns(arr, "1,2")
        block = vd.stats_block(arr, idxs)
        self.assertEqual(list(block), [RW_LABELS[1], RW_LABELS[2]])
        self.assertAlmostEqual(block[RW_LABELS[1]]["mean"], 5.5, places=6)

    def test_preview_block_rows_and_values(self):
        arr = self._arr()
        idxs, _ = vd.select_columns(arr, "0,1")
        self.assertEqual(vd.preview_block(arr, idxs, 0), [])
        self.assertEqual(vd.preview_block(arr, idxs, 2),
                         [[0.0, 1.0], [3.0, 4.0]])
        self.assertEqual(len(vd.preview_block(arr, idxs, 99)), 4)


if __name__ == "__main__":
    unittest.main()
