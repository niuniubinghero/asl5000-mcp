# -*- coding: utf-8 -*-
"""模拟肺脚本清单生成器 / Script library indexer (ASL 5000 / TAI)

扫描 ASL 安装目录下 vars/ 与 breath/ 的全部脚本, 解析命名参数与文件内容,
生成可检索 CSV 清单 (UTF-8 BOM, Excel/WPS 直接双击打开)。

安装根由 --asl-root / ASL_ROOT / ASL_SW_PATH 自动探测, 见 _paths.py。
输出目录由 --out-dir 指定, 默认为本脚本旁的 output/ (已被 .gitignore 排除)。

用法:
    python build_script_index.py --asl-root "<ASL_ROOT>" --out-dir ./my-index
    python build_script_index.py --official-before 2019-01-01

可重复运行以刷新清单。官方/自制判据 = 目录 + 安装日期; 日期默认从 ASL 软件
exe 的修改时间推断, 可用 --official-before 覆盖。
"""

import csv
import os
import re
import sys
from datetime import datetime

try:
    from _paths import resolve_root, default_official_cutoff
except ImportError:  # when imported as tools.build_script_index
    from ._paths import resolve_root, default_official_cutoff

CSV_NAMES = {
    "all": "script_index.csv",
    "official": "script_index_official.csv",
    "custom": "script_index_custom.csv",
}
DEFAULT_OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

# 从 .vr3 内容里抽取的变量 (第4列为变量名, "=" 后为值)
VR3_WANT = {
    "spontaneous_breathing_rate",
    "spontaneous_breathing_flag",
    "r_trachea_in",
    "r_trachea_out",
    "c_left_s0_a1",
    "c_right_s0_a1",
    "read_spont_breath_file_flag",
    "spont_breath_input_file_name",
}


def parse_name(fname):
    """从文件名解析命名参数 (启发式, 取第一个匹配)."""
    info = {}
    base = os.path.splitext(fname)[0]

    m = re.search(r"(\d+)\s*bpm", base, re.I) or re.search(r"(\d+)nor", base, re.I)
    if m:
        info["bpm"] = m.group(1)

    m = re.search(r"IER1[_/]?2", base, re.I)
    if m:
        info["ier"] = "1:2"
    else:
        m = re.search(r"IER(\d+)", base, re.I)
        if m:
            info["ier"] = "1:" + m.group(1)

    # R/C 必须不紧邻字母(避免 IER12->R12, XSC15->C15 误捕获); 数字限3位+可选小数
    m = re.search(r"(?<![A-Za-z])R(\d{1,3}(?:\.\d+)?)(?!\d)", base)
    if m:
        info["r"] = m.group(1)
    m = re.search(r"(?<![A-Za-z])C(\d{1,3}(?:\.\d+)?)(?!\d)", base)
    if m:
        info["c"] = m.group(1)

    pm = re.search(r"Pmax(\d+(?:\.\d+)?)", base, re.I)
    if pm:
        info["pmax"] = pm.group(1)
    m = re.search(r"(?<![AaMm])P(\d+(?:\.\d+)?)", base)
    if m:
        info["p"] = m.group(1)

    m = re.search(r"class[_ ]?(\d)", base, re.I)
    if m:
        info["fl"] = "class" + m.group(1)
    else:
        m = re.search(r"FL\((\d+(?:\.\d+)?)\s*%\)", base, re.I)
        if m:
            info["fl"] = m.group(1) + "%"

    m = re.search(r"VT=?(\d+)", base, re.I)
    if m:
        info["vt"] = m.group(1)
    elif re.search(r"BTPS(\d+)", base, re.I):
        info["vt"] = re.search(r"BTPS(\d+)", base, re.I).group(1)
    m = re.search(r"MV=?(\d+)", base, re.I)
    if m:
        info["mv"] = m.group(1)

    m = re.search(r"(\d+)\s*event", base, re.I)
    if m:
        info["events"] = m.group(1)
    return info


def classify(relpath):
    """按目录+文件名给用途分类 (启发式, 规则按优先级匹配)."""
    p = relpath.replace("\\", "/").lower()
    name = os.path.basename(p)
    d = os.path.dirname(p)

    if d.startswith("vars/scenarios"):
        return "\u5b98\u65b9\u4e34\u5e8a\u573a\u666f"
    if d.startswith("vars/examples"):
        return "\u5b98\u65b9\u793a\u4f8b"
    if d.startswith("vars/tests"):
        return "\u6821\u51c6/\u6027\u80fd\u6d4b\u8bd5"
    if d.startswith("vars/pumps"):
        return "\u6cf5/\u96fe\u5316\u6a21\u578b"
    if d.startswith("vars/algorithm"):
        return "\u7b97\u6cd5\u9a8c\u8bc1"
    if "gb9706" in name or "stand test" in d:
        return "\u56fd\u6807\u6d4b\u8bd5(GB9706.279)"
    if "ahitest" in name or "ahi test" in name or "ahi+" in name:
        return "AHI\u4e8b\u4ef6\u6d4b\u8bd5"
    if "rera" in d or "rera" in name:
        return "RERA\u6d4b\u8bd5"
    if "/csr" in d or d.endswith("csr") or re.search(r"(^|[^a-z])(ca|ch)\(\d", name):
        return "CSR/\u4e2d\u67a2\u4e8b\u4ef6"
    if d.endswith("/si") or "/si/" in d or "+si" in name:
        return "鼾声识别(SI)测试"
    if "/fl" in d or d.endswith("fl") or "fl_class" in name or "fl(" in name or "fl_" in name or "fl-" in name:
        return "\u6d41\u91cf\u53d7\u9650(FL)"
    if "autoramp" in name or "autoramp" in d:
        return "\u5347\u538b(AutoRamp)\u6d4b\u8bd5"
    if "apap" in name or "apap" in d or "/auto" in d or "autos" in d:
        return "\u81ea\u52a8\u8c03\u538b\u6d4b\u8bd5"
    if "breath event" in d or "breath event" in name or "event-" in name:
        return "\u547c\u5438\u4e8b\u4ef6"
    if ("sinewave" in name or "sin wave" in d or "sinwave" in name or "sinus" in name
            or "sinewave" in d or "sinwave" in d or "sinus" in d):
        return "\u6b63\u5f26\u6ce2\u547c\u5438"
    if "co2" in d or "co2" in name:
        return "CO2\u6d4b\u8bd5"
    if "vgps" in d or "vgps" in name:
        return "VGPS\u6d4b\u8bd5"
    if "asv" in d or "asv" in name or "asvpsmax" in name:
        return "ASV\u6d4b\u8bd5"
    if "i-time" in d or "i-time" in name:
        return "\u5438\u6c14\u65f6\u95f4\u6d4b\u8bd5"
    if "bpm" in d and ("vt=" in name or "btps" in name):
        return "\u6f6e\u6c14\u91cf/\u901a\u6c14\u91cf\u6d4b\u8bd5"
    if "vt=" in name or "btps" in name or re.search(r"vt\d", name):
        return "\u6f6e\u6c14\u91cf\u6d4b\u8bd5"
    if "mv=" in name:
        return "\u901a\u6c14\u91cf\u6d4b\u8bd5"
    if "trigger" in d or "cycle" in d or "trigger" in name or "cycle" in name:
        return "\u89e6\u53d1/\u5207\u6362\u6d4b\u8bd5"
    if "leak" in name:
        return "\u6f0f\u6c14\u6d4b\u8bd5"
    if re.search(r"(^|[^a-z])(pcal|cal|rdd)", name):
        return "\u6807\u5b9a\u6d4b\u8bd5"
    if "passive" in name:
        return "\u88ab\u52a8\u80ba\u6a21\u578b"
    if "pmax" in name or "starling" in d or "aslr" in d or "/aslr" in d or "aslr" in name:
        return "\u7b97\u6cd5\u9a8c\u8bc1"
    if relpath.lower().endswith(".vr3"):
        return "\u901a\u7528\u80ba\u6a21\u578b(\u53c2\u6570\u53d8\u4f53)"
    return "\u5176\u4ed6/\u5f85\u5f52\u7c7b"


def parse_sct(path, vars_dir):
    """sct 场景脚本: 段数, 首列合计(重复次数), 引用的 vr3 (去重), 引用有效性.

    首列是 ASL 的 repetitions(重复次数), 不是秒数也不等同呼吸数(厂商手册 6.3 节);
    实测取值为 1/8/11/75/150/1000/11000/20000 等。早期版本把它当秒累加再除以 60
    写入 "sct总时长(分)" 列, 会给出 1000 -> 16.7 分这类无意义数值, 现改为如实合计次数。
    """
    segs = 0
    reps_total = 0.0
    refs = []
    missing = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\r\n")
                if not line.strip():
                    continue
                parts = line.split("\t")
                segs += 1
                try:
                    reps_total += float(parts[0])
                except ValueError:
                    pass
                if len(parts) > 1:
                    ref = parts[1].strip()
                    if ref and ref not in refs:
                        refs.append(ref)
                    rp = re.sub(r"<aslvarsdirectory>", str(vars_dir).replace("\\", "\\\\"), ref,
                                flags=re.I)
                    rp = rp.replace("\\", os.sep)
                    if not os.path.isabs(rp):
                        rp = os.path.join(os.path.dirname(path), rp)
                    if not os.path.exists(rp):
                        missing += 1
    except OSError:
        pass
    return segs, reps_total, refs, missing


def parse_vr3(path):
    """vr3 肺模型: 抽取关键变量值 + 是否含时变参数(vp_)."""
    vals = {}
    has_vp = False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "=" not in line:
                    continue
                if "vp_" in line:
                    has_vp = True
                parts = line.split("\t")
                eq = None
                for i, p in enumerate(parts):
                    if p.strip() == "=":
                        eq = i
                        break
                if eq is None or eq == 0:
                    continue
                name = None
                for j in range(eq - 1, -1, -1):
                    s = parts[j].strip()
                    if s and not s.startswith("("):
                        name = s
                        break
                if name in VR3_WANT and name not in vals:
                    vals[name] = parts[eq + 1].strip() if eq + 1 < len(parts) else ""
    except OSError:
        pass
    return vals, has_vp


def total_compliance(vals):
    """双肺并联: C总(mL/cmH2O) = 1000*(1/E左 + 1/E右)."""
    try:
        el = float(vals.get("c_left_s0_a1", "0") or 0)
        er = float(vals.get("c_right_s0_a1", "0") or 0)
    except ValueError:
        return ""
    if el <= 0 and er <= 0:
        return ""
    c = 0.0
    if el > 0:
        c += 1000.0 / el
    if er > 0:
        c += 1000.0 / er
    return "%.1f" % c


def parse_in(path):
    """in 波形数据: 采样点数, 数值范围."""
    n = 0
    lo = None
    hi = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                n += 1
                try:
                    v = float(s)
                except ValueError:
                    continue
                lo = v if lo is None or v < lo else lo
                hi = v if hi is None or v > hi else hi
    except OSError:
        pass
    rng = "%.3f~%.3f" % (lo, hi) if lo is not None else ""
    return n, rng


def classify_type(ext, fname):
    e = ext.lower()
    if e == ".vr3":
        return "\u80ba\u6a21\u578b(vr3)"
    if e == ".sct":
        return "\u573a\u666f\u811a\u672c(sct)"
    if e == ".in":
        return "\u6ce2\u5f62\u6570\u636e(in)"
    if fname.lower().endswith("sct"):
        return "\u5176\u4ed6(\u7591\u4f3c.sct)"
    return "\u5176\u4ed6"


def remarks(fname, relpath, ext):
    tags = []
    low = fname.lower()
    if "copy" in low or re.search(r"\(\d\)", fname):
        tags.append("\u7591\u4f3c\u526f\u672c")
    if fname != fname.strip() or "  " in fname:
        tags.append("\u540d\u79f0\u542b\u7a7a\u683c")
    if ext == "" and not fname.lower().endswith("sct"):
        tags.append("\u65e0\u6269\u5c55\u540d")
    if ext == "" and fname.lower().endswith("sct"):
        tags.append("\u6269\u5c55\u540d\u7f3a\u5931")
    if ext.lower() == ".vr3" and fname.upper().endswith(".VR3"):
        pass
    if relpath.replace("\\", "/").lower() in ("vars/current.sct", "vars/interactive.vr3",
                                             "breath/current.in", "breath/breath.out"):
        tags.append("\u8fd0\u884c\u65f6\u751f\u6210")
    return "/".join(tags)


def classify_source(rel, mtime_str, cutoff):
    """来源分类: 官方(安装包自带) / 自制(团队自建) / 运行时(ASL运行中生成).

    判据 = 目录 + 修改时间: 官方目录(scenarios/examples/tests/pumps/CO2/xmls,
    breath/ 波形, vars 根目录系统文件) 且修改时间早于安装期(cutoff) 视为官方;
    官方目录内安装期后新增的文件视为自制放入; 其他自建目录一律自制。
    """
    p = rel.replace("\\", "/").lower()
    parts = p.split("/")
    date = mtime_str[:10] if mtime_str else ""
    is_old = bool(date) and bool(cutoff) and date < cutoff

    if p in ("vars/current.sct", "vars/interactive.vr3",
             "breath/current.in", "breath/breath.out"):
        return "\u8fd0\u884c\u65f6"

    if parts[0] == "breath":
        return "\u5b98\u65b9" if is_old else "\u81ea\u5236"
    if parts[0] == "vars":
        if len(parts) == 2:  # vars 根目录散文件
            return "\u5b98\u65b9" if is_old else "\u81ea\u5236"
        if parts[1] in ("scenarios", "examples", "tests", "pumps", "co2", "xmls"):
            return "\u5b98\u65b9" if is_old else "\u81ea\u5236"
    return "\u81ea\u5236"


def main(root, out_dir, cutoff):
    rows = []
    idx = 0
    os.makedirs(out_dir, exist_ok=True)
    scan_dirs = [("vars", root / "vars"), ("breath", root / "breath")]
    for _label, scan_dir in scan_dirs:
        if not scan_dir.is_dir():
            print("[warn] missing directory, skipped: %s" % scan_dir, file=sys.stderr)
            continue
        for dirpath, _dirnames, filenames in os.walk(str(scan_dir)):
            for fname in sorted(filenames):
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, str(root))
                ext = os.path.splitext(fname)[1]
                ftype = classify_type(ext, fname)
                nameinfo = parse_name(fname)
                purpose = classify(rel)
                try:
                    st = os.stat(full)
                    size_kb = "%.1f" % (st.st_size / 1024.0)
                    mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
                except OSError:
                    size_kb, mtime = "", ""
                source = classify_source(rel, mtime, cutoff)

                segs = reps_col = refs_col = ref_chk = ""
                vr3_sct = vr3_rate = vr3_r = vr3_c = vr3_ctot = vr3_in = vr3_vp = ""
                in_pts = in_rng = ""

                if ftype.startswith("\u573a\u666f"):
                    segs_n, reps_total, refs, missing = parse_sct(full, root / "vars")
                    segs = str(segs_n)
                    reps_col = "%d" % int(reps_total) if reps_total else "0"
                    refs_col = "|".join(os.path.basename(r) for r in refs)
                    ref_chk = "\u6b63\u5e38" if missing == 0 else "\u5931\u6548%d\u5904" % missing
                    # 与同名 vr3 配对情况
                    base = os.path.splitext(full)[0]
                    vr3_sct = "\u6709(vr3)" if (os.path.exists(base + ".vr3") or os.path.exists(base + ".VR3")) else "\u65e0(vr3)"
                elif ftype.startswith("\u80ba\u6a21\u578b"):
                    vals, has_vp = parse_vr3(full)
                    base = os.path.splitext(full)[0]
                    vr3_sct = "\u6709(sct)" if (os.path.exists(base + ".sct") or os.path.exists(base + ".SCT")) else "\u65e0(sct,\u9700\u5305\u88c5)"
                    vr3_rate = vals.get("spontaneous_breathing_rate", "").split(".")[0]
                    r_in = vals.get("r_trachea_in", "")
                    r_out = vals.get("r_trachea_out", "")
                    vr3_r = ("%s/%s" % (r_in, r_out)) if r_in or r_out else ""
                    a1l = vals.get("c_left_s0_a1", "")
                    a1r = vals.get("c_right_s0_a1", "")
                    vr3_c = ("%s/%s" % (a1l, a1r)) if a1l or a1r else ""
                    vr3_ctot = total_compliance(vals)
                    if vals.get("read_spont_breath_file_flag", "0").startswith("1"):
                        vr3_in = vals.get("spont_breath_input_file_name", "")
                    vr3_vp = "\u6709" if has_vp else ""
                elif ftype.startswith("\u6ce2\u5f62"):
                    n, rng = parse_in(full)
                    in_pts = str(n)
                    in_rng = rng

                idx += 1
                rows.append([
                    idx,
                    os.path.dirname(rel),
                    fname,
                    ftype,
                    purpose,
                    source,
                    nameinfo.get("bpm", ""),
                    nameinfo.get("ier", ""),
                    nameinfo.get("r", ""),
                    nameinfo.get("c", ""),
                    nameinfo.get("p", ""),
                    nameinfo.get("pmax", ""),
                    nameinfo.get("fl", ""),
                    nameinfo.get("vt", ""),
                    nameinfo.get("mv", ""),
                    nameinfo.get("events", ""),
                    segs,
                    reps_col,
                    refs_col,
                    ref_chk,
                    vr3_sct,
                    vr3_rate,
                    vr3_r,
                    vr3_c,
                    vr3_ctot,
                    vr3_in,
                    vr3_vp,
                    in_pts,
                    in_rng,
                    size_kb,
                    mtime,
                    remarks(fname, rel, ext),
                ])

    header = [
        "\u5e8f\u53f7", "\u76ee\u5f55", "\u6587\u4ef6\u540d", "\u7c7b\u578b", "\u7528\u9014\u5206\u7c7b", "\u6765\u6e90",
        "BPM(\u547d\u540d)", "I:E(\u547d\u540d)", "R(\u547d\u540d)", "C(\u547d\u540d)", "P(\u547d\u540d)",
        "Pmax(\u547d\u540d)", "FL(\u547d\u540d)", "VT(\u547d\u540d)", "MV(\u547d\u540d)", "\u4e8b\u4ef6\u6570(\u547d\u540d)",
        "sct\u6bb5\u6570", "sct\u9996\u5217\u5408\u8ba1(\u91cd\u590d\u6b21\u6570)", "sct\u5f15\u7528vr3",         "sct\u5f15\u7528\u68c0\u67e5",
        "sct/vr3\u540c\u540d\u914d\u5bf9", "vr3\u547c\u5438\u9891\u7387bpm", "vr3\u6c14\u9053\u963b\u529b(in/out)",
        "vr3\u5f39\u6027a1(\u5de6/\u53f3,cmH2O/L)", "vr3\u603b\u987a\u5e94\u6027(mL/cmH2O)",
        "vr3\u5f15\u7528\u6ce2\u5f62", "vr3\u542b\u65f6\u53c2\u8f68\u8ff9", "\u6ce2\u5f62\u91c7\u6837\u70b9\u6570",
        "\u6ce2\u5f62\u6570\u503c\u8303\u56f4", "\u5927\u5c0fKB", "\u4fee\u6539\u65f6\u95f4", "\u5907\u6ce8",
    ]

    official_rows = [r for r in rows if r[5] == "官方"]
    custom_rows = [r for r in rows if r[5] != "官方"]  # 自制 + 运行时(来源列可区分)

    def write_csv(path, data):
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(data)
            return path
        except PermissionError:
            return path + " (被占用跳过, 关闭后重跑可刷新)"

    n_rt = sum(1 for r in rows if r[5] == "运行时")
    print("root=%s  official-before=%s" % (root, cutoff))
    print("rows=%d (官方=%d, 自制=%d, 运行时=%d) ->" % (
        len(rows), len(official_rows), len(custom_rows) - n_rt, n_rt))
    for key, data in (("all", rows), ("official", official_rows), ("custom", custom_rows)):
        print("  %s" % write_csv(os.path.join(out_dir, CSV_NAMES[key]), data))


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Build a searchable index of the ASL script library")
    ap.add_argument("--asl-root", help="ASL install root (contains vars/); auto-detected by default")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                    help="where to write the CSV files (default: tools/output/)")
    ap.add_argument("--official-before",
                    help="vendor install cutoff date YYYY-MM-DD; default: inferred from the ASL exe mtime")
    a = ap.parse_args()
    root = resolve_root(a.asl_root)
    cutoff = a.official_before or default_official_cutoff(root)
    sys.exit(main(root, a.out_dir, cutoff))
