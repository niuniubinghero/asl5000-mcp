#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ASL 5000 厂商录制数据组解析（纯标准库、只读、无硬件依赖）。

一次仿真会在 ``<ASL_ROOT>/ASLdata/`` 下写一组同名文件。这些文件是**自描述的**：
前若干字节是 ASCII 明文，写明格式版本、头长度、字节序和列名清单。实测格式：

===========  ======  =========  =========================================
扩展名       头长度  数据体      说明
===========  ======  =========  =========================================
.rwb         500     N x 13     512 Hz 原始波形，含 Time 轴与 13 列
.dtb         500     N x 16     与 .rwb 逐行对齐，含 Total Flow (L/min)
.brb         4096    M x 107    逐呼吸 107 项派生参数
.avb         2000    K x 93     模型/设置参数快照（每次变更一行）
.aux         -       ASCII      表头 + 可选 TSV 数据行（常为空）
.vol         -       ASCII      表头 + 可选 TSV 数据行（常为空）
.tdms        -       TDMS       仅元数据头，无波形数据
.tdms_index  -       TDMS       .tdms 的索引
===========  ======  =========  =========================================

所有二进制数据体都是 **大端（Motorola / network byte order）单精度 32 位浮点**，
按行主序存放的 2D 数组。用错字节序会得到 1e20 量级的垃圾值，是最常见的坑。

已知厂商缺陷：``.dtb`` 的 500 字节头**装不下全部列名**，最后一个标签被截成
``Trac``，实际数据体是 16 列。本模块对前 12 列使用头部明文标签，第 13 列标注为
被截断，最后 3 列只保留 ``dtb_colNN`` 索引名——实测它们与前几列数值高度重复，
物理含义无法从文件本身确认，因此**不做推测性命名**。

用法::

    from asl_monitor.vendor_data import DataGroup, scan_dir
    g = DataGroup(Path("ASLdata/CPAP_NOFPS_NOHME_10BPM.rwb"))
    g.wave("rwb").column("Airway Pressure (cmH2O)")   # list[float]
    g.wave("dtb").to_csv(Path("out.csv"))
    for meta in scan_dir(Path("ASLdata")): print(meta)
"""

from __future__ import annotations

import csv
import math
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "DATA_EXTS", "WAVE_EXTS", "HEADER_FALLBACK", "DEFAULT_COLUMNS",
    "VendorArray", "DataGroup", "parse_header", "scan_dir",
    "select_columns", "stats_block", "preview_block",
]

DATA_EXTS = (".rwb", ".dtb", ".brb", ".avb", ".aux", ".vol",
             ".tdms", ".tdms_index")
WAVE_EXTS = (".rwb", ".dtb", ".brb", ".avb")

# 头部声明缺失/损坏时的兜底（实测所有 ASL 3.x 文件都带明文声明，仅作防御）
HEADER_FALLBACK = {".rwb": 500, ".dtb": 500, ".brb": 4096, ".avb": 2000}

# 头部标签数与实际列数不一致时的兜底列数（.dtb 就是这种情形）
DEFAULT_COLUMNS = {".rwb": 13, ".dtb": 16, ".brb": 107, ".avb": 93}

# .dtb 头部被 500 字节截断后的补全标签。只有前 12 个是明文可确认的。
_DTB_TAIL = (
    "Tracheal Pressure (header truncated, unverified)",
    "dtb_col13 (unlabeled)",
    "dtb_col14 (unlabeled)",
    "dtb_col15 (unlabeled)",
)

_HEADER_RE = re.compile(rb"(\d+)\s*byte\s*header", re.I)
_LABEL_ANCHOR = "column labels"


def parse_header(blob: bytes) -> tuple[int | None, list[str], str]:
    """从 ASCII 头里解析 ``(头长度, 列名清单, 首段说明)``。

    头部以 CRLF/LF 分行，标签段在 ``The column labels are as follows:`` 之后，
    以全 ``x`` 填充行或超长说明行结束。解析不出来时返回 ``(None, [], "")``。
    """
    m = _HEADER_RE.search(blob[:300])
    if not m:
        return None, [], ""
    hs = int(m.group(1))
    if hs <= 0 or hs > len(blob):
        return None, [], ""
    text = blob[:hs].decode("latin1")
    lines = [ln.strip() for ln in re.split(r"[\r\n]+", text)]
    desc = " ".join(lines[:4])
    try:
        start = next(i for i, ln in enumerate(lines) if _LABEL_ANCHOR in ln.lower())
    except StopIteration:
        return hs, [], desc
    labels: list[str] = []
    for ln in lines[start + 1:]:
        if not ln:
            continue
        if set(ln) <= {"x"} or ln.startswith("The x's") or len(ln) > 70:
            break
        labels.append(ln)
    return hs, labels, desc


@dataclass
class VendorArray:
    """一个二进制数据体：``nrows x ncols`` 的大端 float32 数组。"""

    path: Path
    size: int
    header_bytes: int
    header_desc: str
    declared_labels: list[str]
    labels: list[str]
    ncols: int
    nrows: int
    values: tuple[float, ...]
    notes: list[str] = field(default_factory=list)

    @property
    def ext(self) -> str:
        return self.path.suffix.lower()

    def column(self, key: int | str) -> list[float]:
        """按列索引或列名取整列。列名支持精确匹配与大小写不敏感前缀匹配。"""
        j = self.column_index(key)
        return [self.values[i * self.ncols + j] for i in range(self.nrows)]

    def column_index(self, key: int | str) -> int:
        if isinstance(key, int):
            if not 0 <= key < self.ncols:
                raise IndexError(f"列索引 {key} 越界 (0..{self.ncols - 1})")
            return key
        for i, lab in enumerate(self.labels):
            if lab.casefold() == key.casefold():
                return i
        hits = [i for i, lab in enumerate(self.labels)
                if lab.casefold().startswith(key.casefold())]
        if len(hits) == 1:
            return hits[0]
        raise KeyError(f"找不到列 {key!r}；可用列: {self.labels}")

    def stats(self, key: int | str) -> dict:
        col = self.column(key)
        finite = [v for v in col if math.isfinite(v)]
        return {
            "n": len(col),
            "finite": len(finite),
            "min": min(finite) if finite else None,
            "max": max(finite) if finite else None,
            "mean": (sum(finite) / len(finite)) if finite else None,
        }

    def to_csv(self, out: Path | str, columns: list[int | str] | None = None) -> Path:
        """导出为 CSV（UTF-8 BOM，方便 Excel 直接打开）。"""
        idxs = ([self.column_index(c) for c in columns] if columns
                else list(range(self.ncols)))
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow([self.labels[j] for j in idxs])
            for i in range(self.nrows):
                base = i * self.ncols
                w.writerow([f"{self.values[base + j]:.7g}" for j in idxs])
        return out

    def summary(self) -> dict:
        return {
            "file": self.path.name,
            "size_bytes": self.size,
            "header_bytes": self.header_bytes,
            "declared_labels": len(self.declared_labels),
            "ncols": self.ncols,
            "nrows": self.nrows,
            "notes": list(self.notes),
        }


def read_array(path: Path | str, ncols_hint: int | None = None,
               nrows_hint: int | None = None) -> VendorArray:
    """读取一个厂商二进制文件。列数按「明文标签 → 行数对齐 → 兜底表」依次推断。"""
    path = Path(path)
    blob = path.read_bytes()
    ext = path.suffix.lower()
    hs, labels, desc = parse_header(blob)
    notes: list[str] = []
    if hs is None:
        hs = HEADER_FALLBACK.get(ext)
        if hs is None:
            raise ValueError(f"{path.name}: 不是受支持的厂商二进制文件（无头部声明）")
        notes.append(f"头部声明缺失，按 {ext} 兜底头长度 {hs}")
    body = blob[hs:]
    nfloat = len(body) // 4
    if len(body) % 4:
        notes.append(f"数据体末尾 {len(body) % 4} 字节不足一个 float，已忽略")

    ncols = 0
    if labels and nfloat % len(labels) == 0:
        ncols = len(labels)
    elif nrows_hint and nfloat % nrows_hint == 0:
        ncols = nfloat // nrows_hint
        notes.append(f"头部标签 {len(labels)} 个与数据体不符，按 .rwb 行数 {nrows_hint} 推定 {ncols} 列")
    elif ncols_hint and nfloat % ncols_hint == 0:
        ncols = ncols_hint
        notes.append(f"按兜底列数 {ncols} 解析")
    elif ext in DEFAULT_COLUMNS and nfloat % DEFAULT_COLUMNS[ext] == 0:
        ncols = DEFAULT_COLUMNS[ext]
        notes.append(f"按兜底列数 {ncols} 解析")
    else:
        raise ValueError(f"{path.name}: 无法推断列数 ({nfloat} floats, "
                         f"头部标签 {len(labels)} 个)")

    # 列名：明文标签优先；不足时按扩展名补全，绝不给未标注列编造物理名
    full = list(labels)
    if ext == ".dtb" and ncols >= 16 and len(labels) >= 12:
        full = labels[:12] + list(_DTB_TAIL)[:ncols - 12]
        notes.append("头部 500B 被截断：col12 名称未验证，col13..15 无标签，保留索引名")
    elif len(full) < ncols:
        full += [f"{ext[1:]}_col{j} (unlabeled)" for j in range(len(full), ncols)]
    elif len(full) > ncols:
        full = full[:ncols]
        notes.append(f"头部标签多于列数，截断到前 {ncols} 个")

    values = struct.unpack_from(f">{nfloat}f", body, 0)
    return VendorArray(path=path, size=len(blob), header_bytes=hs,
                       header_desc=desc, declared_labels=labels, labels=full,
                       ncols=ncols, nrows=nfloat // ncols, values=values,
                       notes=notes)


@dataclass
class DataGroup:
    """一组同名文件。懒加载：访问 ``wave()`` / ``breaths()`` 时才读盘。"""

    directory: Path
    name: str
    _cache: dict = field(default_factory=dict, repr=False)

    def path(self, ext: str) -> Path:
        return self.directory / f"{self.name}{ext}"

    def has(self, ext: str) -> bool:
        return self.path(ext).is_file()

    def _get(self, ext: str) -> VendorArray:
        if ext not in self._cache:
            hint = None
            if ext == ".dtb" and self.has(".rwb"):
                rwb = self._get(".rwb")
                hint = rwb.nrows
            self._cache[ext] = read_array(self.path(ext), nrows_hint=hint)
        return self._cache[ext]

    def wave(self, which: str = "rwb") -> VendorArray:
        ext = which if which.startswith(".") else "." + which
        if ext not in WAVE_EXTS:
            raise ValueError(f"不是波形文件: {ext}")
        if not self.has(ext):
            raise FileNotFoundError(self.path(ext))
        return self._get(ext)

    def breaths(self) -> VendorArray | None:
        return self._get(".brb") if self.has(".brb") else None

    def model(self) -> VendorArray | None:
        return self._get(".avb") if self.has(".avb") else None

    def time(self) -> list[float]:
        """.rwb 的第 0 列 Time (sec)，是整组数据（含 .dtb）共用的时间轴。"""
        return self.wave("rwb").column(0)

    def duration(self) -> float:
        t = self.time()
        return t[-1] if t else 0.0

    def ascii_table(self, ext: str) -> tuple[list[str], list[list[str]]]:
        """读 .aux / .vol 的 ASCII 表头与数据行（通常只有表头）。"""
        p = self.path(ext)
        if not p.is_file():
            return [], []
        lines = p.read_text(encoding="latin1").splitlines()
        rows = [ln.split("\t") for ln in lines[1:] if ln.strip()]
        head = lines[0].split("\t") if lines else []
        return head, rows

    def summary(self) -> dict:
        out = {"name": self.name, "directory": str(self.directory), "files": {}}
        for ext in DATA_EXTS:
            p = self.path(ext)
            if p.is_file():
                out["files"][ext] = p.stat().st_size
        try:
            rwb = self.wave("rwb")
            out["rwb_rows"] = rwb.nrows
            out["duration_sec"] = round(self.duration(), 3)
            out["sample_rate_hz"] = (round((rwb.nrows - 1) / self.duration(), 2)
                                     if self.duration() and rwb.nrows > 1 else None)
        except (FileNotFoundError, ValueError, ZeroDivisionError):
            out["rwb_rows"] = 0
            out["duration_sec"] = 0.0
            out["sample_rate_hz"] = None
        try:
            out["dtb_cols"] = self.wave("dtb").ncols
            out["dtb_rows"] = self.wave("dtb").nrows
        except (FileNotFoundError, ValueError):
            out["dtb_cols"] = out["dtb_rows"] = 0
        for key, ext in (("breaths", ".brb"), ("model_rows", ".avb")):
            try:
                a = self._get(ext) if self.has(ext) else None
                out[key] = a.nrows if a else 0
            except (ValueError, OSError):
                out[key] = 0
        out["has_waveform"] = out["rwb_rows"] > 0
        return out


def scan_dir(directory: Path | str, deep: bool = True) -> list[dict]:
    """扫描目录下所有同名数据组，返回元数据字典列表（按名称排序）。

    ``deep=False`` 时只读文件尺寸，不解析二进制，适合快速清点。
    """
    directory = Path(directory)
    names = sorted({p.stem for p in directory.iterdir()
                    if p.is_file() and p.suffix.lower() in DATA_EXTS},
                   key=str.casefold)
    result = []
    for name in names:
        g = DataGroup(directory, name)
        if deep:
            try:
                result.append(g.summary())
            except Exception as exc:  # 单个坏文件不该拖垮整次扫描
                result.append({"name": name, "error": str(exc),
                               "has_waveform": False})
        else:
            result.append({"name": name, "has_waveform":
                           any(g.has(e) and g.path(e).stat().st_size >
                               HEADER_FALLBACK.get(e, 0) for e in (".rwb", ".dtb"))})
    return result


# ---------------------------------------------------------------- MCP 助手（纯函数）

def select_columns(arr: VendorArray, spec: str = "") -> tuple[list[int] | None, str]:
    """把逗号分隔的列名/索引解析成索引列表；空串 = 全部列（返回 None）。

    返回 ``(索引列表或 None, 错误信息)``：出错时索引为 None，错误信息可直接透给调用方。
    索引按 0..ncols-1 校验，不支持负数。
    """
    spec = (spec or "").strip()
    if not spec:
        return None, ""
    idxs: list[int] = []
    for tok in (t.strip() for t in spec.split(",")):
        if not tok:
            continue
        if tok.lstrip("-").isdigit():
            j = int(tok)
            if not 0 <= j < arr.ncols:
                return None, f"列索引越界: {j} (0..{arr.ncols - 1})"
        else:
            try:
                j = arr.column_index(tok)
            except KeyError:
                return None, f"找不到列 {tok!r}; 可用列: {arr.labels}"
        idxs.append(j)
    if not idxs:
        return None, "columns 解析结果为空"
    return idxs, ""


def stats_block(arr: VendorArray, idxs: list[int]) -> dict:
    """选中列的统计 ``{列名: {n, finite, min, max, mean}}``。"""
    return {arr.labels[j]: arr.stats(j) for j in idxs}


def preview_block(arr: VendorArray, idxs: list[int], rows: int = 3) -> list[list[float]]:
    """前 rows 行、选中列的实际值（保留 6 位小数）；rows<=0 返回空表。"""
    rows = max(0, int(rows))
    out: list[list[float]] = []
    for i in range(min(rows, arr.nrows)):
        base = i * arr.ncols
        out.append([round(arr.values[base + j], 6) for j in idxs])
    return out
