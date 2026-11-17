#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ASL 5000 **实时**波形看板 —— 多面板 ECharts，边测边看。

三种数据源（互不依赖，按场景选）::

    python tools/live_wave_view.py                         # 直连 6343/6342 TAI 广播（推荐）
    python tools/live_wave_view.py --tail <waveform.csv>   # 尾随正在写的录波 CSV
    python tools/live_wave_view.py --replay <waveform.csv> --speed 8   # 历史 CSV 回放（无硬件验证）
    python tools/live_wave_view.py --port 8765 --no-browser

启动后浏览器打开 http://127.0.0.1:8765 。Ctrl-C 结束。

界面：默认三个波形面板（压力 / 流量 / 容积，对应 ASL 软件 Run Time Home 的三张图），
每个面板可自由勾选显示任意通道，面板标题可点击编辑，可 ✕ 删除；
底部「＋ 添加波形面板」可无限追加。所有面板缩放/悬停联动（echarts.connect）。

背景：ASL5000_SW3.6.exe 运行时向所有 TCP 客户端广播两路数据（无需 TAI Server 授权）：
  - 6342  每次呼吸结束推全部呼吸参数（ASCII，"参数名<TAB>数值"）
  - 6343  连续波形流（TAB 分隔，首行列名，采样率随脚本 64/128/512 Hz）
每帧前有 4 字节大端 int32 长度头，且**一帧含约 100 行**（批量推送，到达时间不均匀）。
采样率必须读波形自带的 Time (sec) 列，不能用接收速率/Recv Time 推算；实测与厂商 .rwb
的 Time 列逐点相同。本工具与 asl-monitor MCP 的录波 CSV 是**同一份数据**，
只是把数字变成了图；厂商 .rwb/.dtb 是 ASL 软件自己落盘的另一套二进制留存（见 build_wave_view.py）。

流量说明：6343 广播**没有 Total Flow 列**（只有厂商 .dtb 才有）。本工具在 Hub 层对
Total/Chamber1/2 Volume 做后向差分得到派生流量通道（512 Hz 下噪声约 0.4 L/min 峰峰，
再做轻度 EMA 平滑），标签为 "Total Flow (L/min)" 等；其数值仅供波形观察，
**峰值流量等定量结论仍以 6342 的逐呼吸参数（Peak Insp./Exp. Flow）或 .dtb 为准**。

依赖：仅标准库 + 本仓库 src/asl_monitor/monitor.py + tools/assets/echarts.min.js。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import socket
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from asl_monitor import monitor as am  # noqa: E402
from asl_monitor import tai as tai_m  # noqa: E402  TAI 6341 控制口客户端 (Interactive Control)

# ---------------------------------------------------------------- 通道定义
# key, 列名匹配关键词, 单位族, 默认勾选。列名来自 6343 首行表头，按小写子串匹配。
CHANNEL_DEFS = (
    ("paw",    ("airway pressure",),    "pressure", True),
    ("pmus",   ("muscle pressure",),    "pressure", False),
    ("ptrach", ("tracheal pressure",),  "pressure", False),
    ("p1",     ("chamber 1 pressure",), "pressure", False),
    ("p2",     ("chamber 2 pressure",), "pressure", False),
    ("vtot",   ("total volume",),       "volume",   True),
    ("v1",     ("chamber 1 volume",),   "volume",   False),
    ("v2",     ("chamber 2 volume",),   "volume",   False),
    ("aux1",   ("aux 1",),              "volt",     False),
    ("aux2",   ("aux 2",),              "volt",     False),
    ("o2",     ("oxygen sensor",),      "volt",     False),
    ("bnum",   ("breath file number",), "count",    False),
    ("bdet",   ("breath detection",),   "count",    False),
)

# 派生流量通道：(key, 源容积通道 key, 显示标签)。在表头后追加列，值为容积的后向差分。
DERIVED_FLOW = (
    ("ftot", "vtot", "Total Flow (L/min)"),
    ("f1",   "v1",   "Chamber 1 Flow (L/min)"),
    ("f2",   "v2",   "Chamber 2 Flow (L/min)"),
)

FLOW_SMOOTH = 0.45   # EMA 新值权重（0~1，越小越平滑、滞后越大）
ML_S_PER_LMIN = 1000.0 / 60.0   # mL/s → L/min

FAMILY_UNIT = {"pressure": "cmH2O", "flow": "L/min", "volume": "mL",
               "volt": "V", "count": "#"}


def match_channels(header: list[str]) -> tuple[list[dict], int, int]:
    """把 6343 表头映射成前端通道定义；返回 (channels, time_col, breath_col)。

    派生流量通道追加在原始列之后（col = len(header)+k），随 rows 一同下发。"""
    lows = [h.strip().lower() for h in header]

    def find(*keywords, exact=None):
        if exact:
            for i, s in enumerate(lows):
                if s == exact:
                    return i
        for i, s in enumerate(lows):
            if all(k in s for k in keywords):
                return i
        return -1

    itime = find(exact="time (sec)")
    if itime < 0:
        itime = find("time")
    bcol = find("breath file number")
    if bcol < 0:
        bcol = find("breath detection")
    channels = []
    for key, kws, family, on in CHANNEL_DEFS:
        idx = find(*kws)
        if idx >= 0:
            channels.append({"key": key, "label": header[idx].strip(),
                             "family": family, "unit": FAMILY_UNIT[family],
                             "col": idx, "on": on})
    # 派生流量：需要源容积列存在
    srcmap = {c["key"]: c["col"] for c in channels}
    if itime >= 0:
        for k, (key, srckey, label) in enumerate(DERIVED_FLOW):
            if srckey in srcmap:
                channels.append({"key": key, "label": label,
                                 "family": "flow", "unit": FAMILY_UNIT["flow"],
                                 "col": len(header) + k, "on": False,
                                 "derived": True, "src_col": srcmap[srckey]})
    return channels, itime, bcol


# ---------------------------------------------------------------- 数据枢纽
class Hub:
    """线程安全的环形缓冲 + 增量游标。行宽 = 原始列 + 派生流量列。"""

    def __init__(self, source: str, max_rows: int = 240000):
        self.lock = threading.Lock()
        self.rows: deque[list] = deque(maxlen=max_rows)
        self.header: list[str] = []
        self._header_full: list[str] = []
        self._derive_srcs: list[int] = []
        self.channels: list[dict] = []
        self.time_col = 0
        self.breath_col = -1
        self.total = 0
        self.last_breath: dict = {}
        self.breath_count = 0
        self.last_wave_at = 0.0
        self.last_breath_at = 0.0
        self.conn = {"waveform": False, "breath": False}
        self.source = source
        self.started = time.time()
        self.error = ""
        self._prev_row: list | None = None   # 上一行（差分用）
        self._prev_flow: dict[int, float] = {}

    def set_header(self, header: list[str]):
        with self.lock:
            if header == self.header:
                return
            self.header = list(header)
            self.channels, self.time_col, self.breath_col = match_channels(header)
            derived = [c for c in self.channels if c.get("derived")]
            self._derive_srcs = [c["src_col"] for c in derived]
            self._header_full = list(header) + [c["label"] for c in derived]
            self._prev_row = None
            self._prev_flow = {}

    def add_rows(self, rows: list[list[float]]):
        if not rows:
            return
        with self.lock:
            if self._derive_srcs and self.time_col >= 0:
                rows = self._derive_flow(rows)
            self.rows.extend(rows)
            self.total += len(rows)
            self.last_wave_at = time.time()
            self.conn["waveform"] = True

    def _derive_flow(self, rows: list[list[float]]) -> list[list]:
        """后向差分 + EMA 生成派生流量列；时间复位/缺数据处断开为 None。"""
        it = self.time_col
        srcs = self._derive_srcs
        prev = self._prev_row
        pf = self._prev_flow
        out = []
        for r in rows:
            rr = list(r)
            for sc in srcs:
                f = None
                if (prev is not None and it < len(r) and it < len(prev)
                        and sc < len(r) and sc < len(prev)
                        and isinstance(r[it], float) and isinstance(prev[it], float)
                        and isinstance(r[sc], float) and isinstance(prev[sc], float)):
                    dt = r[it] - prev[it]
                    dv = r[sc] - prev[sc]
                    if isinstance(dt, float) and isinstance(dv, float) and dt > 0:
                        f = dv / dt / ML_S_PER_LMIN
                        p = pf.get(sc)
                        if p is not None:
                            f = p * (1.0 - FLOW_SMOOTH) + f * FLOW_SMOOTH
                pf[sc] = f
                rr.append(f)
            out.append(rr)
            prev = r
        self._prev_row = prev
        return out

    def add_breath(self, d: dict):
        with self.lock:
            self.last_breath = d
            self.breath_count += 1
            self.last_breath_at = time.time()
            self.conn["breath"] = True

    def since(self, cursor: int, limit: int = 20000) -> dict:
        with self.lock:
            available = len(self.rows)
            behind = self.total - cursor
            if behind > available or cursor > self.total:
                data, reset = list(self.rows), True
            else:
                start = max(0, available - behind)
                data = list(self.rows)[start:]
                reset = False
            if len(data) > limit:
                data = data[-limit:]
            return {
                "rows": data, "cursor": self.total, "reset": reset,
                "truncated": behind > available,
                "status": self._status_locked(),
                "breath": self.last_breath,
            }

    def _sample_rate_locked(self) -> float | None:
        """从 Time (sec) 列的中位步长取倒数 —— 设备侧真实采样率。

        不能用「接收行数 / 墙钟时间」估算：6343 是批量推送（一帧约 100 行，
        同批 Recv Time 相同），回放倍速、网络抖动都会让那个数失真。
        调用方必须已持有 self.lock。
        """
        it = self.time_col
        if it < 0 or len(self.rows) < 3:
            return None
        tail = list(self.rows)[-1024:]
        steps = []
        for a, b in zip(tail, tail[1:]):
            if it < len(a) and it < len(b):
                d = b[it] - a[it]
                if d > 0 and math.isfinite(d):
                    steps.append(d)
        if not steps:
            return None
        steps.sort()
        return round(1.0 / steps[len(steps) // 2], 3)

    def _status_locked(self) -> dict:
        now = time.time()
        return {
            "source": self.source,
            "sample_rate_hz": self._sample_rate_locked(),
            "uptime_sec": round(now - self.started, 1),
            "total_rows": self.total,
            "buffered_rows": len(self.rows),
            "breath_count": self.breath_count,
            "wave_age_sec": round(now - self.last_wave_at, 1) if self.last_wave_at else None,
            "breath_age_sec": round(now - self.last_breath_at, 1) if self.last_breath_at else None,
            "connected": dict(self.conn),
            "error": self.error,
            "header": self._header_full or self.header,
            "channels": self.channels,
            "time_col": self.time_col,
            "breath_col": self.breath_col,
            "breath_keys": sorted(self.last_breath.keys()),
        }

    def status(self) -> dict:
        with self.lock:
            return self._status_locked()

    def export(self, sec: float | None) -> tuple[list[str], list]:
        with self.lock:
            header = list(self._header_full or self.header)
            it = self.time_col
            rows = list(self.rows)
            if sec and rows and it >= 0 and len(rows[0]) > it:
                t_end = rows[-1][it]
                cut = len(rows)
                for i in range(len(rows) - 1, -1, -1):
                    if t_end - rows[i][it] > sec:
                        cut = i + 1
                        break
                rows = rows[cut:]
            return header, rows


# ---------------------------------------------------------------- 数据源
def to_floats(rows: list[list[str]], width: int) -> list[list]:
    out = []
    for r in rows:
        if len(r) < width:
            r = r + [""] * (width - len(r))
        v = []
        for s in r[:width]:
            try:
                f = float(s)
                v.append(f if math.isfinite(f) else None)
            except (ValueError, TypeError):
                v.append(None)
        out.append(v)
    return out


def start_broadcast(hub: Hub, stop: threading.Event, host: str,
                    bport: int, wport: int):
    """直连 6342/6343。复用 monitor.broadcast_worker 的自动重连逻辑。"""
    state = {}

    def wave_handler(msg):
        h, rows = am.parse_wave_message(msg)
        if not rows:
            return
        hub.set_header(h)
        hub.add_rows(to_floats(rows, len(h)))

    def breath_handler(msg):
        d = am.parse_breath_params(msg)
        if d:
            hub.add_breath(d)

    threads = [
        threading.Thread(target=am.broadcast_worker,
                         args=("波形", host, wport, wave_handler, state, stop), daemon=True),
        threading.Thread(target=am.broadcast_worker,
                         args=("呼吸参数", host, bport, breath_handler, state, stop), daemon=True),
    ]
    for t in threads:
        t.start()
    return threads


def read_csv_rows(path: Path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f)
        header = next(rd)
        # 录波 CSV 首列可能是 "Recv Time"（本机接收时间），去掉后再对齐
        if header and header[0].strip().lower() == "recv time":
            header = header[1:]
            for r in rd:
                yield header, (r[1:] if r else [])
        else:
            for r in rd:
                yield header, r


def start_replay(hub: Hub, stop: threading.Event, path: Path, speed: float,
                 breath_csv: Path | None):
    """按真实时间节奏回放历史 CSV（speed 倍速）。用于无硬件时验证页面。"""
    def run():
        # 预加载
        gen = read_csv_rows(path)
        try:
            header, _ = next(gen)
        except StopIteration:
            hub.error = f"空 CSV: {path}"
            return
        hub.set_header(header)
        hub.conn.update(waveform=True, breath=bool(breath_csv and breath_csv.is_file()))
        chunks = []
        buf = []
        for _, row in gen:
            buf.append(row)
            if len(buf) >= 128:
                chunks.append(buf)
                buf = []
        if buf:
            chunks.append(buf)

        params = []
        if breath_csv and breath_csv.is_file():
            with open(breath_csv, "r", encoding="utf-8-sig", newline="") as f:
                params = list(csv.DictReader(f))

        t0 = time.time()
        last_t = 0.0
        pidx = 0
        for k, chunk in enumerate(chunks):
            if stop.is_set():
                return
            rows = to_floats(chunk, len(header))
            hub.add_rows(rows)
            it = hub.time_col
            if it >= 0 and rows and len(rows[0]) > it:
                last_t = rows[-1][it]
                # 按 ASL 时间轴对齐到墙钟（speed 倍速）
                target = t0 + last_t / max(0.01, speed)
                wait = target - time.time()
                if wait > 0:
                    stop.wait(min(wait, 1.0))
            if params:
                want = int((k + 1) / max(1, len(chunks)) * len(params))
                while pidx < want:
                    hub.add_breath({x: y for x, y in params[pidx].items() if y not in (None, "")})
                    pidx += 1
        hub.error = ""
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return [t]


def start_tail(hub: Hub, stop: threading.Event, path: Path):
    """尾随正在被 asl_start_recording 写入的 waveform_NNN.csv。"""
    def run():
        while not stop.is_set():
            try:
                with open(path, "r", encoding="utf-8-sig", newline="") as f:
                    rd = csv.reader(f)
                    header = next(rd)
                    if header and header[0].strip().lower() == "recv time":
                        header = header[1:]
                        strip = True
                    else:
                        strip = False
                    hub.set_header(header)
                    hub.conn["waveform"] = True
                    while not stop.is_set():
                        line = f.readline()
                        if not line:
                            time.sleep(0.15)
                            continue
                        try:
                            r = next(csv.reader([line]))
                        except (csv.Error, StopIteration):
                            continue
                        if strip:
                            r = r[1:]
                        hub.add_rows(to_floats([r], len(header)))
            except FileNotFoundError:
                hub.error = f"等待文件出现: {path}"
                stop.wait(1.0)
            except StopIteration:
                stop.wait(0.5)
            except OSError as e:
                hub.error = str(e)
                stop.wait(1.0)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return [t]


# ---------------------------------------------------------------- TAI 控制口 (Interactive Control)
# 复刻 Standard Window Manager 的 Interactive Control Panel:
#   IM 进入交互模式(退出脚本模式) → IC 参数=值 实时改模型 → IC 参数=? 回读 → SM 回脚本模式
IC_READ_GROUPS: dict[str, tuple[str, ...]] = {
    "肺模型 (R / C)": ("RT", "RT_OUT", "C1", "C2", "R1", "R1_OUT", "R2", "R2_OUT"),
    "自主呼吸用力 (Pmus 剖面)": ("RATE", "PAUSE", "INSP_PMUS", "EXP_PMUS", "TRIGGERTIME",
                     "INSP_RISETIME", "INSP_HOLD", "INSP_RELEASETIME",
                     "EXP_RISETIME", "EXP_HOLD", "EXP_RELEASETIME"),
    "闭环控制": ("LOOP", "VTSET", "PVTUPPER", "PVTLOWER", "MVSET",
                 "PMVUPPER", "PMVLOWER", "FMVUPPER", "FMVLOWER", "ETCO2SET",
                 "CO2FLOW", "PCASECO2", "PCO2UPPER", "PCO2LOWER",
                 "FCO2UPPER", "FCO2LOWER"),
}


def tai_call(cmd: str, to_ms: int = 10000) -> dict:
    """同步执行一条 TAI 命令；任何失败都转成 {ok:False,error}，不向上抛。"""
    try:
        lines = tai_m.get_client().command(cmd, to_ms=to_ms)
        return {"ok": True, "lines": lines, "kv": tai_m.parse_kv_lines(lines)}
    except tai_m.TAIError as e:
        return {"ok": False, "error": str(e), "code": e.code}
    except Exception as e:  # TimeoutError / OSError / TAI exe 缺失
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def tai_read_model() -> dict:
    """IC 参数回读。先试一条合并命令，失败则逐参数读（容忍单参数 ERROR 16/17）。"""
    allp = [p for ps in IC_READ_GROUPS.values() for p in ps]
    comb = tai_call("ic " + " ".join(f"{p}=?" for p in allp), to_ms=20000)
    if comb["ok"]:
        return {"ok": True, "values": comb["kv"], "notes": {}}
    values: dict[str, str] = {}
    notes: dict[str, str] = {}
    for p in allp:
        r = tai_call(f"ic {p}=?", to_ms=6000)
        if r["ok"]:
            kv = r["kv"]
            values[p] = kv.get(p) or (r["lines"][0] if len(r["lines"]) == 1 else "")
        else:
            notes[p] = r["error"]
    return {"ok": True, "values": values, "notes": notes,
            "combined_error": comb["error"]}


# ---------------------------------------------------------------- HTTP
PAGE = None  # 延迟填充，避免大字符串污染模块导入


class Handler(BaseHTTPRequestHandler):
    hub: Hub = None
    server_version = "ASLLiveView/2.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj):
        self._send(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?")[0]
        qs = {}
        if "?" in self.path:
            for part in self.path.split("?", 1)[1].split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    qs[k] = v
        hub = self.hub
        if path in ("/", "/index.html"):
            self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/meta":
            self._json(hub.status())
        elif path == "/api/data":
            try:
                cursor = int(qs.get("cursor", "0"))
            except ValueError:
                cursor = 0
            self._json(hub.since(cursor))
        elif path == "/api/export":
            try:
                sec = float(qs.get("sec", "0")) or None
            except ValueError:
                sec = None
            header, rows = hub.export(sec)
            lines = [",".join(f'"{h}"' for h in header)]
            lines += [",".join("" if v is None else f"{v:.9g}" for v in r) for r in rows]
            body = ("\n".join(lines) + "\n").encode("utf-8-sig")
            name = f"asl_live_{datetime.now():%Y%m%d_%H%M%S}.csv"
            self._send(body, "text/csv; charset=utf-8",
                       {"Content-Disposition": f'attachment; filename="{name}"'})
        elif path == "/api/tai/status":
            self._json(tai_call("tc", to_ms=8000))
        elif path == "/api/tai/model":
            self._json(tai_read_model())
        else:
            self.send_error(404)

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            body = self._read_body()
        except Exception as e:
            return self._json({"ok": False, "error": f"请求体解析失败: {e}"})
        if path == "/api/tai/interactive":
            on = bool(body.get("on"))
            r = tai_call("im" if on else "sm", to_ms=20000)
            if r["ok"]:
                st = tai_call("tc", to_ms=8000)
                r["status"] = st.get("kv", {}) if st["ok"] else {}
                r["mode"] = "interactive" if on else "script"
            self._json(r)
        elif path == "/api/tai/set":
            params = body.get("params") or {}
            if not isinstance(params, dict) or not params:
                return self._json({"ok": False, "error": "params 为空"})
            unknown = [k for k in params if k.upper() not in tai_m.IC_ALL_PARAMS]
            if unknown:
                return self._json({"ok": False,
                                   "error": "未知 IC 参数: " + ", ".join(unknown),
                                   "valid": sorted(tai_m.IC_ALL_PARAMS)})
            args = " ".join(f"{k}={v}" for k, v in params.items())
            r = tai_call("ic " + args, to_ms=30000)
            if not r["ok"]:
                return self._json(r)
            readback = {}
            for k in params:
                rr = tai_call(f"ic {k}=?", to_ms=6000)
                if rr["ok"] and rr["kv"].get(k) is not None:
                    readback[k] = rr["kv"][k]
            r["applied"] = params
            r["readback"] = readback
            self._json(r)
        else:
            self.send_error(404)


# ---------------------------------------------------------------- 页面模板
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ASL 5000 实时波形</title>
<style>
:root{
  --bg:#0e1319; --panel:#151c26; --panel2:#1c2531; --line:#2b3745;
  --fg:#e6edf3; --muted:#8b9bab; --accent:#4ea8ff; --accent2:#7cc4ff;
  --chip:#222e3c; --chipon:#1d4f7c; --good:#39c07d; --warn:#ff8a65;
  --shadow:0 3px 16px rgba(0,0,0,.35);
}
html[data-theme="light"]{
  --bg:#eef2f7; --panel:#ffffff; --panel2:#f0f4f9; --line:#d3dde8;
  --fg:#1b2430; --muted:#63707f; --accent:#0b6bcb; --accent2:#3b93e6;
  --chip:#e8eef5; --chipon:#b9d9f8; --good:#12834f; --warn:#c94b20;
  --shadow:0 3px 14px rgba(30,50,80,.12);
}
*{box-sizing:border-box}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--line);border-radius:5px}
::-webkit-scrollbar-track{background:transparent}
body{margin:0;background:var(--bg);color:var(--fg);
  font:13px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{display:flex;align-items:center;gap:12px;padding:10px 18px;
  border-bottom:1px solid var(--line);position:sticky;top:0;z-index:20;
  background:color-mix(in srgb,var(--panel) 88%,transparent);
  backdrop-filter:blur(10px) saturate(1.2)}
h1{font-size:16px;margin:0;font-weight:700;letter-spacing:.3px}
h1 small{font-weight:400;color:var(--muted);font-size:12px;margin-left:10px}
.spacer{flex:1}
button,select{background:var(--chip);color:var(--fg);border:1px solid var(--line);
  border-radius:8px;padding:5px 11px;cursor:pointer;font:inherit;transition:all .15s}
button:hover{border-color:var(--accent);transform:translateY(-1px)}
button.primary{background:linear-gradient(135deg,var(--accent),var(--accent2));
  color:#fff;border-color:transparent;font-weight:600}
button.primary:hover{filter:brightness(1.1)}
.wrap{padding:12px 18px 30px;max-width:1920px;margin:0 auto}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:11px 14px;margin-bottom:11px;box-shadow:var(--shadow)}
.row{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:6px 0}
.row>label.tag{color:var(--muted);min-width:68px;font-size:12px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{border:0;border-radius:0;border-right:1px solid var(--line);padding:5px 11px}
.seg button:last-child{border-right:0}
.seg button.on{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff}
.hint{color:var(--muted);font-size:12px}
.badge{background:var(--panel2);border:1px solid var(--line);border-radius:5px;
  padding:2px 8px;font-size:11px;color:var(--muted)}
.badge.live{color:var(--good);border-color:var(--good)}
.badge.pause{color:var(--warn);border-color:var(--warn)}
#winSlider{width:150px;accent-color:var(--accent);cursor:pointer}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(126px,1fr));gap:8px}
.rtahead{display:flex;align-items:baseline;gap:10px;margin-bottom:8px;flex-wrap:wrap}
.rtatile{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:10px 12px;cursor:pointer;transition:border-color .15s}
.rtatile:hover{border-color:color-mix(in srgb,var(--accent) 55%,var(--line))}
.rtatile .k{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rtatile .v{font-size:20px;font-weight:600;margin-top:3px}
.rtatile .u{font-size:11px;color:var(--muted);margin-left:4px}
.rtatile select{width:100%;font:inherit;background:var(--bg);color:var(--fg);
  border:1px solid var(--line);border-radius:8px;padding:4px}
.kv{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:7px 11px;
  transition:border-color .15s}
.kv:hover{border-color:var(--accent)}
.kv .k{color:var(--muted);font-size:11px}
.kv .v{font-size:17px;font-weight:650;font-variant-numeric:tabular-nums;margin-top:2px}
.kv .u{font-size:11px;color:var(--muted);font-weight:400;margin-left:3px}
.warnbox{border-left:3px solid var(--warn);padding:7px 12px;background:var(--panel2);
  color:var(--muted);font-size:12px;margin-bottom:11px;border-radius:0 8px 8px 0}
#errmsg{color:var(--warn);font-size:12px}

/* ---- 波形面板 ---- */
#panels{display:flex;flex-direction:column;gap:11px}
.pcard{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:9px 12px 6px;box-shadow:var(--shadow);transition:border-color .2s,box-shadow .2s}
.pcard:hover{border-color:color-mix(in srgb,var(--accent) 55%,var(--line))}
.phead{display:flex;align-items:center;gap:8px;margin-bottom:4px}
.picon{width:10px;height:10px;border-radius:3px;background:linear-gradient(135deg,var(--accent),var(--accent2));
  flex:none}
.ptitle{font-size:14px;font-weight:650;padding:1px 7px;border-radius:6px;cursor:text;
  border:1px dashed transparent;min-width:40px;outline:none}
.ptitle:hover,.ptitle:focus{border-color:var(--line);background:var(--panel2)}
.pchips{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:2px 0 4px}
.chip{background:var(--chip);border:1px solid var(--line);border-radius:20px;
  padding:3px 11px;cursor:pointer;user-select:none;font-size:12px;white-space:nowrap;
  transition:all .12s;opacity:.55}
.chip.on{background:var(--chipon);border-color:var(--accent);opacity:1;
  box-shadow:0 0 6px color-mix(in srgb,var(--accent) 35%,transparent)}
.chip .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.pbtn{padding:2px 9px;font-size:13px;border-radius:7px}
.pmeta{color:var(--muted);font-size:11px}
.pchart{width:100%;height:250px}
.addcard{border:1.5px dashed var(--line);border-radius:12px;padding:13px;text-align:center;
  color:var(--muted);cursor:pointer;font-size:13px;background:transparent;
  transition:all .18s;margin-bottom:11px;user-select:none}
.addcard:hover{border-color:var(--accent);color:var(--accent);background:var(--panel)}
.foot{color:var(--muted);font-size:12px;text-align:center;margin-top:4px}
kbd{background:var(--panel2);border:1px solid var(--line);border-radius:4px;
  padding:0 5px;font-size:11px;font-family:inherit}
.tabs{display:flex;gap:6px;margin-left:10px}
.tabs button{border-radius:9px;padding:6px 14px;font-size:13px}
.tabs button.on{background:linear-gradient(135deg,var(--accent),var(--accent2));
  color:#fff;border-color:transparent;font-weight:600}
/* ---- ICP 子 tab（对齐 GUI Interactive Control Panel 的 8 个子 tab 结构）---- */
.icptabs{display:flex;gap:6px;margin:14px 0 10px;align-items:center;flex-wrap:wrap}
.icptabs button{border-radius:8px;padding:6px 16px;font-size:13px}
.icptabs button.on{background:linear-gradient(135deg,var(--accent),var(--accent2));
  color:#fff;border-color:transparent;font-weight:600}
.icprow{display:flex;gap:12px;align-items:stretch;flex-wrap:wrap}
.icprow>.card{margin:0}
.icp-diag{flex:0 0 400px;min-width:320px}
.icp-knobs{flex:1;min-width:520px}
.kngrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(108px,1fr));gap:10px}
.kngrid .mghead{grid-column:1/-1;font-weight:600;font-size:12.5px;color:var(--muted);
  margin:4px 0 0;border-bottom:1px dashed var(--line);padding-bottom:4px}
.kcell{display:flex;flex-direction:column;align-items:center;gap:4px;background:var(--panel2);
  border:1px solid var(--line);border-radius:10px;padding:8px 6px 7px}
.kcell.pending{border-color:var(--warn);box-shadow:0 0 0 1px var(--warn) inset}
.kcell .klab{font-size:11.5px;text-align:center;line-height:1.25}
.kcell .klab b{font-weight:600}
.kcell .kunit{display:block;color:var(--muted);font-size:9.5px}
.kcell svg{display:block;touch-action:none;cursor:ns-resize}
.kcell input,.kcell select{width:90%;background:var(--bg);color:var(--fg);
  border:1px solid var(--line);border-radius:6px;padding:3px 4px;font:inherit;
  font-size:12.5px;text-align:center}
.kcell input:disabled,.kcell select:disabled{opacity:.45}
.kcell.kdis svg{opacity:.35;cursor:not-allowed}
/* ---- 手册风格 Two/Single Compartment 示意图 ---- */
#modelDiagram svg{width:100%;height:auto;display:block}
#modelDiagram .dframe{fill:none;stroke:var(--fg);stroke-width:1.6}
#modelDiagram .dpipe{stroke:var(--fg);stroke-width:2.8;fill:none;stroke-linecap:round}
#modelDiagram .dhead{fill:var(--fg)}
#modelDiagram .dcirc{fill:var(--panel);stroke:var(--fg);stroke-width:2.4}
#modelDiagram .darr{stroke:var(--muted);stroke-width:1.5;fill:none}
#modelDiagram .darrow{fill:var(--muted)}
#modelDiagram .dlab{fill:var(--fg);font-size:14px;font-weight:600}
#modelDiagram .dval{fill:var(--accent);font-size:12.5px;font-weight:600}
#modelDiagram .dcap{fill:var(--fg);font-size:13px;font-weight:600}
#modelDiagram g.pending .dpipe,#modelDiagram g.pending .dcirc{
  stroke:var(--warn)!important;stroke-dasharray:6 4}
#modelDiagram g.pending .dval{fill:var(--warn)}
#modelDiagram g.pending .dlab{fill:var(--warn)}
/* ---- 自主呼吸 Pmus 剖面预览（Fig 7-7 右上的 Pressure 图）---- */
#pmusPrev svg{width:100%;max-width:900px;height:auto;display:block;margin:0 auto}
#pmusPrev .pgrid{stroke:var(--line);stroke-width:1;opacity:.7}
#pmusPrev .paxis{stroke:var(--fg);stroke-width:1.2}
#pmusPrev .ptick{fill:var(--muted);font-size:10.5px}
#pmusPrev .plab{fill:var(--fg);font-size:12px;font-weight:600}
#pmusPrev .pcurve{stroke:#e0c23c;stroke-width:2.6;fill:none;stroke-linejoin:round;stroke-linecap:round}
#pmusPrev .pzero{stroke:var(--muted);stroke-width:1;stroke-dasharray:4 4}
</style>
</head>
<body>
<header>
  <h1>ASL 5000 实时看板 <small>__SOURCE__</small></h1>
  <nav class="tabs">
    <button id="tabWaveBtn" class="on">📊 波形</button>
    <button id="tabModelBtn">🫁 肺模型 · Interactive</button>
  </nav>
  <span class="badge live" id="liveBadge">● 连接中</span>
  <span class="badge" id="ptBadge">0 点</span>
  <span class="badge" id="rateBadge">— Hz</span>
  <div class="spacer"></div>
  <button id="btnPause" class="primary">⏸ 暂停</button>
  <button id="btnCsv">导出 CSV</button>
  <button id="btnTheme">切换主题</button>
</header>
<div class="wrap" id="pageWave">

<div class="card">
  <div class="row"><label class="tag">跟随窗口</label>
    <div class="seg" id="winSeg">
      <button data-v="5">5 s</button><button data-v="10">10 s</button>
      <button data-v="30" class="on">30 s</button><button data-v="60">60 s</button>
      <button data-v="custom">自定义</button><button data-v="0">全部缓冲</button>
    </div>
    <input type="range" id="winSlider" min="2" max="600" step="1" value="30"
      title="拖动自定义窗口时长 (s)" style="display:none">
    <span class="badge" id="winVal">30 s</span>
    <span class="hint">选「自定义」出现滑块，拖动设定 2–600 s 窗口（受缓冲上限约束）；跟随模式自动滚到最新；<kbd>空格</kbd> 或点「暂停」后可滚轮缩放 / 拖动平移（各面板联动），<kbd>Shift</kbd>+滚轮缩放纵轴，右键拖动平移查看（拖动自动暂停）</span>
  </div>
  <div class="row"><label class="tag">单位</label>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
      <span class="hint">压力</span><div class="seg" id="unitP">
        <button data-v="cmH2O" class="on">cmH₂O</button><button data-v="hPa">hPa</button></div>
      <span class="hint">流量</span><div class="seg" id="unitF">
        <button data-v="L/min" class="on">L/min</button><button data-v="mL/s">mL/s</button></div>
      <span class="hint">容积</span><div class="seg" id="unitV">
        <button data-v="mL" class="on">mL</button><button data-v="L">L</button></div>
    </div>
    <span class="hint" id="errmsg"></span>
  </div>
</div>

<div class="warnbox">
  数据源：<b>6342/6343 TAI 广播</b>（与 asl-monitor 录波 CSV 同源）。广播<b>不含原始流量列</b>，
  「Total Flow / Chamber Flow」为<b>容积差分派生通道</b>（后向差分 + 轻度 EMA，观察波形用；
  峰值流量等定量结论以逐呼吸参数卡片或厂商 <code>.dtb</code> 为准）。
  厂商 <code>.rwb/.dtb</code> 用 <code>build_wave_view.py</code> 做事后精细对比。<br>
  采样率由 <code>Time (sec)</code> 列中位步长实测（本机 64/128/512 Hz），与 <code>.rwb</code> 逐点一致；
  <code>Recv Time</code> 不能用于推算采样率。所有面板缩放/悬停<b>联动</b>；双击标题可改名。
</div>

<div class="card"><div class="cards" id="cards"></div></div>

<div class="card">
  <div class="rtahead"><b>Real Time Analysis 分析参数</b>
    <span class="hint">与 ASL 软件 Real Time Analysis 界面同源（6342 逐呼吸广播，92 个参数）；
    点击任意瓦片可换成想要的参数，选择自动记忆</span></div>
  <div class="cards" id="rtacards"></div>
</div>

<div id="panels"></div>

<div class="addcard" id="addCard">＋ 添加波形面板</div>

<div class="foot hint">
  滚轮缩放横轴 · <kbd>Shift</kbd>+滚轮缩放纵轴 · 左键/右键拖动平移 · 悬停读数 · 面板间十字光标联动 ·
  点击面板上方通道 chips 增减曲线 · 点击标题改名 · ✕ 删除面板 · ⤢ 复位缩放
</div>

</div>

<div class="wrap" id="pageModel" style="display:none">
<div class="warnbox">
  本 Tab 通过 <b>TAI 6341 控制口</b>复刻 Standard Window Manager 的 <b>Interactive Control</b> 能力：
  「进入交互模式 (IM)」等效于 GUI 里 Go To Interactive Control Panel（<b>会退出脚本模式</b>，SM 恢复）；
  写入经 <code>IC 参数=值</code> 即时生效并自动回读确认，直接作用于正在运行的仿真。
  广播波形与参数卡片在后台继续接收，切回「📊 波形」即见。
</div>

<div class="card">
  <div class="row">
    <b>TAI 控制口</b>
    <span class="badge" id="taiBadge">○ 未查询</span>
    <span class="badge" id="modeBadge">—</span>
    <button id="taiRefresh">刷新状态</button>
    <span class="hint" id="taiStatusLine"></span>
  </div>
  <div class="row">
    <button id="btnIM" class="primary">进入交互模式 (IM)</button>
    <button id="btnSM">退出交互 (SM 回脚本)</button>
    <button id="modelRefresh" class="primary">读取模型参数</button>
  </div>
  <div class="row"><span class="hint" id="taiMsg"></span></div>
</div>

<div class="warnbox" id="pumpWarn" style="display:none"></div>

<div class="icptabs" id="icpTabs">
  <button data-st="lung" class="on">🫁 肺模型</button>
  <button data-st="spont">💨 自主呼吸 Pmus</button>
  <button data-st="loop">🎯 闭环控制</button>
  <span class="spacer"></span>
  <button id="modelRefresh" class="primary">读取模型参数</button>
</div>

<div class="icprow" id="stLung">
  <div class="card icp-diag">
    <div class="rtahead"><b>模型示意图</b>
      <span class="hint">对齐手册 Fig 7-6「Two Compartment」画法（R2 左支 · R1 右支）；
      <b style="color:var(--warn)">橙色虚线</b> = 未应用的修改（旧值 → 新值）</span></div>
    <div id="modelDiagram"></div>
  </div>
  <div class="card icp-knobs">
    <div class="rtahead"><b>肺模型参数旋钮</b>
      <span class="hint">上下拖动旋钮或滚轮微调，也可直接输入数值；空 = 当前模型不支持 (ERROR 16) 或尚未进入交互模式。
      改完点「应用」——只提交改过的项，写入后自动回读确认</span></div>
    <div class="kngrid" id="knobsLung"></div>
    <div class="row" style="margin-top:10px">
      <button class="primary" data-apply="lung">应用肺模型参数</button>
      <span class="hint">对应 GUI Lung Model Param. tab 的 8 个旋钮</span></div>
  </div>
</div>

<div id="stSpont" style="display:none">
  <div class="card">
    <div class="rtahead"><b>自主呼吸压力剖面预览</b>
      <span class="hint">按 ASL 绘图惯例画 <b>−Δp<sub>mus</sub></b>（吸气向下，同手册 Fig 7-7 右上 Pressure 图）；
      吸气窗按呼吸周期 40% 示意；修改右侧参数即时重绘预览</span></div>
    <div id="pmusPrev"></div>
  </div>
  <div class="card">
    <div class="rtahead"><b>自主呼吸用力旋钮</b>
      <span class="hint">对应 GUI Spont. Breath Param. tab (Fig 7-7)</span></div>
    <div class="kngrid" id="knobsSpont"></div>
    <div class="row" style="margin-top:10px">
      <button class="primary" data-apply="spont">应用自主呼吸参数</button>
      <span class="hint">RATE / Pmus / 各段百分比即时生效</span></div>
  </div>
</div>

<div id="stLoop" style="display:none">
  <div class="card">
    <div class="rtahead"><b>闭环控制旋钮</b>
      <span class="hint">对应 GUI Closed Loop Vt / MV / CO2Y 三个子 tab；LOOP 用下拉防敲错枚举</span></div>
    <div class="kngrid" id="knobsLoop"></div>
    <div class="row" style="margin-top:10px">
      <button class="primary" data-apply="loop">应用闭环参数</button>
      <span class="hint">切换 LOOP 时目标值 (VTSET/MVSET/ETCO2SET) 需与模式匹配</span></div>
  </div>
</div>
</div>
<script>/*__ECHARTS__*/</script>
<script>
"use strict";
const CFG = /*__CONFIG__*/;
CFG.userTouched = false;
const PALETTE = ['#4ea8ff','#ffb454','#39c07d','#e879b9','#a78bfa','#f87171',
                 '#22d3ee','#facc15','#fb923c','#94a3b8','#2dd4bf','#c084fc','#f472b6','#60a5fa'];
const FAM_LABEL = {pressure:'压力', flow:'流量', volume:'容积', volt:'Aux/检测', count:'检测'};
const UNIT_LABEL = {cmH2O:'cmH₂O', hPa:'hPa', 'L/min':'L/min', 'mL/s':'mL/s', mL:'mL', L:'L', V:'V', '#':'#'};
const UNIT_FACTOR = {pressure:{cmH2O:1,hPa:0.980665}, flow:{'L/min':1,'mL/s':1000/60},
                     volume:{mL:1,L:0.001}, volt:{V:1}, count:{'#':1}};
const DEFAULT_PANELS = [
  {title:'压力 Pressure', fams:['paw','pmus','ptrach','p1','p2']},
  {title:'流量 Flow',     fams:['ftot','f1','f2']},
  {title:'容积 Volume',   fams:['vtot','v1','v2']},
];
const state = {
  units:{pressure:'cmH2O', flow:'L/min', volume:'mL'},
  window:30, paused:false, theme:'dark', cursor:0, rows:[],
  meta:null, lastBreath:{}, status:{}, tab:'wave'
};
const MAXKEEP = 240000;
const $ = id => document.getElementById(id);
const CM = {};                 // key -> channel
const COL = {};                // key -> color
let panels = [], pidSeq = 0;
let lastRender = 0, pending = false, suppressZoom = false;
let rowsCache = [];
const esc = s => String(s).replace(/[&<>"]/g, ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));
const shortOf = c => c.label.replace(/\s*\(.+$/,'');

function themeColors(){
  const cs = getComputedStyle(document.documentElement);
  return {fg:cs.getPropertyValue('--fg').trim(), muted:cs.getPropertyValue('--muted').trim(),
          line:cs.getPropertyValue('--line').trim(), panel:cs.getPropertyValue('--panel').trim(),
          accent:cs.getPropertyValue('--accent').trim()};
}
const rgba=(hex,a)=>{
  const n=parseInt(hex.slice(1),16);
  return 'rgba('+(n>>16&255)+','+(n>>8&255)+','+(n&255)+','+a+')';
};
function unitOf(fam){ return state.units[fam] || ({volt:'V',count:'#'})[fam] || ''; }
function factorOf(fam){
  const t = UNIT_FACTOR[fam]||{}; const u = unitOf(fam);
  return t[u]!==undefined ? t[u] : 1;
}
function grad(color){
  return new echarts.graphic.LinearGradient(0,0,0,1,[
    {offset:0,color:rgba(color,.16)},{offset:1,color:rgba(color,0)}]);
}

/* ------------------------------------------------ 面板管理 */
function makePanel(cfg){
  const p = {id:++pidSeq, title:cfg.title, chans:new Set((cfg.fams||[]).filter(k=>CM[k]))};  const el = document.createElement('div');
  el.className='pcard'; el.dataset.pid=p.id;
  el.innerHTML =
    '<div class="phead">'+
      '<span class="picon"></span>'+
      '<span class="ptitle" contenteditable spellcheck="false">'+esc(p.title)+'</span>'+
      '<span class="pmeta" id="pmeta-'+p.id+'"></span>'+
      '<span class="spacer"></span>'+
      '<button class="pbtn pfit" title="复位缩放">⤢</button>'+
      '<button class="pbtn pclose" title="删除面板">✕</button>'+
    '</div>'+
    '<div class="pchips"></div>'+
    '<div class="pchart"></div>';
  $('panels').appendChild(el);
  // 通道 chips
  const box = el.querySelector('.pchips');
  CFG.channels.forEach(c=>{
    const on = p.chans.has(c.key);
    const ch=document.createElement('span');
    ch.className='chip'+(on?' on':'');
    ch.dataset.k=c.key;
    ch.innerHTML='<span class="dot" style="background:'+COL[c.key]+'"></span>'+esc(shortOf(c));
    ch.onclick=()=>{
      CFG.userTouched=true;
      if(p.chans.has(c.key)) p.chans.delete(c.key); else p.chans.add(c.key);
      ch.classList.toggle('on', p.chans.has(c.key));
      renderPanel(p, rowsCache, true);
    };
    box.appendChild(ch);
  });
  // 标题编辑
  const tEl = el.querySelector('.ptitle');
  tEl.addEventListener('keydown', e=>{ if(e.key==='Enter'){ e.preventDefault(); tEl.blur(); }});
  tEl.addEventListener('blur', ()=>{
    const v = tEl.textContent.trim();
    p.title = v || p.title;
    tEl.textContent = p.title;
  });
  // 按钮
  el.querySelector('.pfit').onclick=()=>{
    suppressZoom=true;
    p.chart.dispatchAction({type:'dataZoom',dataZoomIndex:0,start:0,end:100});
    p.chart.dispatchAction({type:'dataZoom',dataZoomIndex:1,start:0,end:100});
    renderPanel(p, rowsCache, true);
    setTimeout(()=>{suppressZoom=false;},80);
  };
  el.querySelector('.pclose').onclick=()=>{
    if(panels.length<=1){ flashMsg('至少保留一个面板'); return; }
    CFG.userTouched=true;
    p.chart.dispose(); el.remove();
    panels = panels.filter(x=>x!==p);
    if(panels.length && !panels[0].chart.isDisposed()) hookFirstZoom();
  };
  p.chart = echarts.init(el.querySelector('.pchart'), null, {renderer:'canvas'});
  p.chart.group = 'asl';
  p.el = el;
  panels.push(p);
  echarts.connect('asl');
  // Shift/Alt + 滚轮：Y 轴缩放（普通滚轮 = X 轴缩放，由 ECharts inside dataZoom 处理）。
  // capture 阶段拦截，阻止 zrender 收到同一事件重复缩 X。
  el.addEventListener('wheel', e=>{
    if(!e.shiftKey && !e.altKey) return;
    e.preventDefault(); e.stopPropagation();
    const yz=(p.chart.getOption().dataZoom||[])[1]||{start:0,end:100};
    suppressZoom=true;                      // Y 缩放不触发"缩放即暂停"
    const f=e.deltaY<0?0.88:1/0.88;         // 上滚放大、下滚缩小
    const span=(yz.end-yz.start)*f;
    if(span>=100){                          // 放大超过全域 → 复位
      p.chart.dispatchAction({type:'dataZoom',dataZoomIndex:1,start:0,end:100});
      setTimeout(()=>{suppressZoom=false;},80); return;
    }
    const mid=(yz.start+yz.end)/2;          // 以视口中心为锚
    let s=mid-span/2, t=mid+span/2;
    if(s<0){t-=s;s=0;} if(t>100){s-=t-100;t=100;} if(s<0){s=0;}
    p.chart.dispatchAction({type:'dataZoom',dataZoomIndex:1,start:s,end:t});
    setTimeout(()=>{suppressZoom=false;},80);
  },{capture:true,passive:false});
  // 右键拖动：平移查看。横向=平移时间轴；纵向已缩放时=平移纵轴。
  // 拖动即暂停跟随（与左键拖动一致），否则下一次 render 会把窗口刷回跟随态。
  el.addEventListener('contextmenu', e=>{ e.preventDefault(); });
  let rdrag=null;
  const cEl=el.querySelector('.pchart');
  cEl.addEventListener('mousedown', e=>{
    if(e.button!==2) return;
    e.preventDefault();
    const o=p.chart.getOption().dataZoom||[];
    rdrag={x0:e.clientX, y0:e.clientY,
      xs:(o[0]||{start:0}).start, xe:(o[0]||{end:100}).end,
      ys:(o[1]||{start:0}).start, ye:(o[1]||{end:100}).end,
      W:p.chart.getWidth(), H:p.chart.getHeight(), moved:false};
    const mv=ev=>{
      if(!rdrag) return;
      if(!rdrag.moved){                        // 拖动超过阈值才暂停跟随（纯右击不暂停）
        if(Math.hypot(ev.clientX-rdrag.x0, ev.clientY-rdrag.y0)<4) return;
        rdrag.moved=true; setPaused(true);
      }
      // 横向：拖右看更早数据（窗口随拖动反向移动）
      const span=rdrag.xe-rdrag.xs;
      let xs=rdrag.xs-(ev.clientX-rdrag.x0)/rdrag.W*span;
      let xe=rdrag.xe-(ev.clientX-rdrag.x0)/rdrag.W*span;
      if(xs<0){xe-=xs;xs=0;} if(xe>100){xs-=xe-100;xe=100;} if(xs<0){xs=0;}
      p.chart.dispatchAction({type:'dataZoom',dataZoomIndex:0,start:xs,end:xe});
      // 纵向：仅在 y 已缩放（窗口<全域）时平移
      const yspan=rdrag.ye-rdrag.ys;
      if(yspan<100 && rdrag.H>0){
        let ys=rdrag.ys+(ev.clientY-rdrag.y0)/rdrag.H*yspan;
        let ye=rdrag.ye+(ev.clientY-rdrag.y0)/rdrag.H*yspan;
        if(ys<0){ye-=ys;ys=0;} if(ye>100){ys-=ye-100;ye=100;} if(ys<0){ys=0;}
        p.chart.dispatchAction({type:'dataZoom',dataZoomIndex:1,start:ys,end:ye});
      }
    };
    const up=()=>{ rdrag=null;
      window.removeEventListener('mousemove',mv);
      window.removeEventListener('mouseup',up); };
    window.addEventListener('mousemove',mv);
    window.addEventListener('mouseup',up);
  });
  renderPanel(p, rowsCache, true);
  hookFirstZoom();
  return p;
}
function hookFirstZoom(){
  // 用户在任一面板缩放 -> 全局暂停；echarts.connect 已把 dataZoom 广播到所有实例，
  // 只需在仍存活的首个实例上监听一次
  const p0 = panels.find(p=>p.chart && !p.chart.isDisposed());
  if(!p0) return;
  p0.chart.off('dataZoom');
  p0.chart.on('dataZoom', ()=>{ if(!suppressZoom && !state.paused) setPaused(true); });
}
function buildDefaultPanels(){
  panels.forEach(p=>{ p.chart.dispose(); p.el.remove(); });
  panels = [];
  DEFAULT_PANELS.forEach(d=>makePanel(d));
}
function rebuildChips(){
  panels.forEach(p=>{
    const box = p.el.querySelector('.pchips'); box.innerHTML='';
    CFG.channels.forEach(c=>{
      const on = p.chans.has(c.key);
      const ch=document.createElement('span');
      ch.className='chip'+(on?' on':'');
      ch.dataset.k=c.key;
      ch.innerHTML='<span class="dot" style="background:'+COL[c.key]+'"></span>'+esc(shortOf(c));
      ch.onclick=()=>{
        CFG.userTouched=true;
        if(p.chans.has(c.key)) p.chans.delete(c.key); else p.chans.add(c.key);
        ch.classList.toggle('on', p.chans.has(c.key));
        renderPanel(p, rowsCache, true);
      };
      box.appendChild(ch);
    });
  });
}
function flashMsg(s){ const el=$('errmsg'); el.textContent=s; setTimeout(()=>{ if(el.textContent===s) el.textContent=''; }, 2500); }

/* ------------------------------------------------ 数据切片 */
function windowRows(){
  const it = state.meta ? state.meta.time_col : 0;
  const rows = state.rows;
  if(!rows.length || !state.window || it<0) return rows;
  const tEnd = rows[rows.length-1][it];
  if(!isFinite(tEnd)) return rows;
  let lo=0, hi=rows.length;
  while(lo<hi){ const m=(lo+hi)>>1; if(rows[m][it] < tEnd-state.window) lo=m+1; else hi=m; }
  return rows.slice(Math.max(0,lo-1));
}
function breathML(rows, it, c){
  const bc = state.meta ? state.meta.breath_col : -1;
  if(bc<0 || rows.length<2 || it<0) return undefined;
  const out=[]; const tEnd=rows[rows.length-1][it];
  const keep = Math.max(state.window||1e9, 5);
  for(let i=1;i<rows.length;i++){
    if(rows[i][bc]!==rows[i-1][bc] && rows[i][bc]!=null && rows[i][it]>=tEnd-keep){
      out.push({xAxis:rows[i][it]});
      if(out.length>60) out.shift();
    }
  }
  if(!out.length) return undefined;
  return {silent:true,symbol:'none',
    lineStyle:{color:'#39c07d',type:'dashed',opacity:.35,width:1},
    label:{show:false}, data:out};
}

/* ------------------------------------------------ 单面板渲染 */
function renderPanel(p, rows, force){
  if(!p.chart || p.chart.isDisposed()) return;
  const c = themeColors();
  const it = state.meta ? state.meta.time_col : 0;
  const chosen = [...p.chans].map(k=>CM[k]).filter(Boolean);
  const meta = $('pmeta-'+p.id);
  if(meta) meta.textContent = chosen.length ? chosen.length+' 条曲线' : '';
  if(!chosen.length){
    p.chart.setOption({backgroundColor:'transparent', animation:false,
      title:{text:'点击上方通道 chips 选择要显示的波形', left:'center', top:'middle',
             textStyle:{color:c.muted, fontSize:13, fontWeight:'normal'}}},
      {notMerge:true});
    return;
  }
  // 家族 -> y 轴（第一个族左轴，其余右轴依次 offset）
  const fams=[]; chosen.forEach(ch=>{ if(!fams.includes(ch.family)) fams.push(ch.family); });
  const famAxis={}; fams.forEach((f,i)=>famAxis[f]=i);
  const yAxes = fams.map((f,i)=>{
    return {type:'value', position:i===0?'left':'right',
      offset:i>=2?(i-1)*46:0,
      name:FAM_LABEL[f]+'·'+(UNIT_LABEL[unitOf(f)]||unitOf(f)),
      nameLocation:'middle', nameRotate:90, nameGap:42,
      nameTextStyle:{color:c.muted,fontSize:11},
      axisLabel:{color:c.muted, fontSize:11,
                 formatter:v=>Math.abs(v)>=1000?(v/1000).toFixed(1)+'k':(+v).toFixed(Math.abs(v)<10?1:0)},
      axisLine:{show:true,lineStyle:{color:c.line}},
      splitLine:{show:i===0, lineStyle:{color:c.line, opacity:.4}}};
  });
  const right = fams.length>1 ? 64+(fams.length-2)*46 : 26;
  const xAxes=[{type:'value', min:'dataMin', max:'dataMax',
    name:'时间 (s)', nameLocation:'middle', nameGap:22,
    nameTextStyle:{color:c.muted,fontSize:11},
    axisLabel:{color:c.muted, fontSize:11, formatter:v=>(+v).toFixed(1)},
    axisLine:{lineStyle:{color:c.line}},
    splitLine:{lineStyle:{color:c.line, opacity:.4}}}];
  const series = [];
  chosen.forEach(ch=>{
    const f = factorOf(ch.family), color = COL[ch.key]||'#888';
    const data = new Array(rows.length);
    let lastPt = null;                       // 末端动态大点坐标
    for(let k=0;k<rows.length;k++){
      const t = rows[k][it], v = rows[k][ch.col];
      const tv = (t!=null && isFinite(t)) ? t : null;
      const vv = (v!=null && isFinite(v)) ? v*f : null;
      data[k] = [tv, vv];
      if(tv!=null && vv!=null) lastPt = [tv, vv];
    }
    series.push({name:shortOf(ch), type:'line', showSymbol:false, symbol:'none',
      yAxisIndex:famAxis[ch.family],
      lineStyle:{width:1.8, color}, itemStyle:{color}, sampling:'lttb',
      animation:false, connectNulls:false, data:data,
      areaStyle:{color:grad(color)}});
    // 滚动更新端：最新点用 effectScatter 涟漪标记，让"正在流入"更醒目
    if(lastPt) series.push({name:'', type:'effectScatter',
      yAxisIndex:famAxis[ch.family], data:[lastPt], symbolSize:6.5,
      itemStyle:{color}, rippleEffect:{scale:3.4, brushType:'stroke', period:2.6},
      silent:true, z:12, animation:false});
  });
  if(series.length && it>=0){
    const ml = breathML(rows, it, c);
    if(ml) series[0].markLine = ml;
  }
  // 保留用户缩放：x 只在暂停时保留（跟随模式始终全窗）；y 始终保留（跨实时刷新）
  let dz=[0,100], ydz=[0,100];
  try{ const o=p.chart.getOption();
    if(state.paused && !force && o.dataZoom && o.dataZoom[0])
      dz=[o.dataZoom[0].start, o.dataZoom[0].end];
    if(!force && o.dataZoom && o.dataZoom[1])
      ydz=[o.dataZoom[1].start, o.dataZoom[1].end];
  }catch(e){}
  p.chart.setOption({
    backgroundColor:'transparent',
    textStyle:{color:c.fg}, animation:false,
    grid:{left:60, right:right, top:24, bottom:38},
    xAxis:xAxes, yAxis:yAxes, series:series,
    tooltip:{trigger:'axis', axisPointer:{type:'cross',
        label:{backgroundColor:c.panel, borderColor:c.line, precision:3}},
      backgroundColor:c.panel, borderColor:c.line, textStyle:{color:c.fg,fontSize:12},
      formatter:ps=>{
        if(!ps||!ps.length) return '';
        let s='<b>t = '+(+ps[0].value[0]).toFixed(3)+' s</b><br/>';
        ps.forEach(p2=>{ if(p2.value[1]!=null && p2.seriesName)   // 空名=末端涟漪点，不进 tooltip
          s+=p2.marker+p2.seriesName+' : <b>'+(+p2.value[1]).toFixed(3)+'</b><br/>'; });
        return s;
      }},
    dataZoom:[
      {type:'inside', xAxisIndex:0, filterMode:'none', start:dz[0], end:dz[1],
       zoomOnMouseWheel:true, moveOnMouseMove:true, moveOnMouseWheel:false},
      {type:'inside', yAxisIndex:yAxes.map((_,i)=>i), filterMode:'none',
       start:ydz[0], end:ydz[1],
       zoomOnMouseWheel:false, moveOnMouseMove:false, moveOnMouseWheel:false}]
  }, {notMerge:true, lazyUpdate:true});
}

/* ------------------------------------------------ 主渲染循环 */
function render(force){
  if(state.tab!=='wave') return;      // 模型 Tab 下不重绘隐藏的图表，切回时 resize+render
  const now = performance.now();
  if(!force && now-lastRender<380){ pending=true; return; }
  lastRender=now; pending=false;
  rowsCache = windowRows();
  panels.forEach(p=>renderPanel(p, rowsCache, false));
  $('ptBadge').textContent = rowsCache.length.toLocaleString()+' 点 / 缓冲 '+state.rows.length.toLocaleString()
    +' / '+panels.length+' 面板';
}

/* ------------------------------------------------ 参数卡片 */
function pick(d,...kws){
  for(const k in d){
    const base=k.split('(')[0].trim().toLowerCase();
    if(kws.every(x=>base===x||base.includes(x)) && base===kws[0]) return d[k];
  }
  for(const k in d){ const l=k.toLowerCase(); if(kws.every(x=>l.includes(x))) return d[k]; }
  return null;
}
function num(v){ const f=parseFloat(v); return isFinite(f)?f:null; }
function fmt(v,n){ return v==null?'—':(+v).toFixed(n===undefined?2:n); }
function renderCards(){
  const b=state.lastBreath||{}, rows=state.rows, it=state.meta?state.meta.time_col:0;
  const last=rows.length?rows[rows.length-1]:null;
  const pf=state.units.pressure==='hPa'?0.980665:1;
  const vf=state.units.volume==='L'?0.001:1;
  const ff=UNIT_FACTOR.flow[state.units.flow]||1;
  const paw=last&&CM.paw?last[CM.paw.col]:null;
  const vol=last&&CM.vtot?last[CM.vtot.col]:null;
  const fl=last&&CM.ftot?last[CM.ftot.col]:null;
  const items=[
    ['气道压 Paw', fmt(paw==null?null:paw*pf,2), state.units.pressure==='hPa'?'hPa':'cmH₂O'],
    ['总流量 Q·派生', fmt(fl==null?null:fl*ff,2), state.units.flow],
    ['总容积 Vtot', fmt(vol==null?null:vol*vf,1), state.units.volume],
    ['呼吸号', fmt(num(pick(b,'breath number')),0), ''],
    ['呼吸频率', fmt(num(pick(b,'breath rate')),1), 'BPM'],
    ['P_peak', fmt(num(pick(b,'p_peak')),2), 'cmH₂O'],
    ['PEEP', fmt(num(pick(b,'peep')),2), 'cmH₂O'],
    ['吸/呼潮气量', fmt(num(pick(b,'patient insp vt')),0)+' / '+fmt(num(pick(b,'patient exp vt')),0), 'mL'],
    ['峰吸/呼流量', fmt(num(pick(b,'peak insp. flow')),1)+' / '+fmt(num(pick(b,'peak exp. flow')),1), 'L/min'],
  ];
  $('cards').innerHTML=items.map(x=>
    '<div class="kv"><div class="k">'+x[0]+'</div><div class="v">'+x[1]+
    (x[2]?'<span class="u">'+x[2]+'</span>':'')+'</div></div>').join('');
}

/* ------------------------------------------------ RTA 分析参数面板 */
const RTA_DEFAULT = ['Patient Exp Vt (mL)','Breath Rate (BPM)','I/E','PEEP (cmH2O)',
  'P_peak (cmH2O)','P_mean (cmH2O)','Trig Resp Time (ms)','Pat Insp Work (mJ)'];
let rtaKeys = (()=>{
  try{
    const s=JSON.parse(localStorage.getItem('asl_rta_keys')||'null');
    if(Array.isArray(s) && s.length && s.every(x=>typeof x==='string')) return s.slice(0,12);
  }catch(e){}
  return RTA_DEFAULT.slice();
})();
function saveRta(){ try{ localStorage.setItem('asl_rta_keys', JSON.stringify(rtaKeys)); }catch(e){} }
function breathKeys(){ return (state.meta && state.meta.breath_keys) || []; }
function unitOfKey(k){ const m=k.match(/\(([^()]*)\)\s*$/); return m?m[1]:''; }
function renderRTA(){
  const box=$('rtacards'); if(!box) return;
  const b=state.lastBreath||{};
  box.innerHTML=rtaKeys.map((k,i)=>{
    const v=num(b[k]);
    const u=unitOfKey(k);
    const name=k.replace(/\s*\([^()]*\)\s*$/,'')||k;
    const dec=(v!=null && Math.abs(v)>=100)?1:2;
    return '<div class="rtatile" data-i="'+i+'" title="点击更换参数">'
      +'<div class="k">'+esc(name)+'</div>'
      +'<div class="v">'+fmt(v,dec)+(u?'<span class="u">'+esc(u)+'</span>':'')+'</div></div>';
  }).join('');
  box.querySelectorAll('.rtatile').forEach(el=>{
    el.onclick=()=>openRtaPicker(+el.dataset.i, el);
  });
}
function openRtaPicker(i, tile){
  const keys=breathKeys();
  if(!keys.length){ flashMsg('尚无呼吸参数广播，无法选择'); return; }
  const cur=rtaKeys[i];
  const sel=document.createElement('select');
  sel.innerHTML=keys.map(k=>
    '<option value="'+esc(k)+'"'+(k===cur?' selected':'')+'>'+esc(k)+'</option>').join('');
  sel.value=cur;
  tile.innerHTML=''; tile.appendChild(sel); sel.focus();
  const commit=()=>{ rtaKeys[i]=sel.value||cur; saveRta(); renderRTA(); };
  sel.onchange=commit;
  sel.onblur=()=>setTimeout(()=>{ if(document.activeElement!==sel) renderRTA(); },60);
}

/* ------------------------------------------------ 模型示意图 */
let pendingVals={};
function fmtVal(v){ return (v==null||v==='')?'—':String(v); }
function diagText(k){                    // 参数在示意图上的显示文本（含待应用对比）
  const p=pendingVals[k], v=modelVals[k];
  const cur=fmtVal(v);
  return (p!=null&&p!==''&&cur!==p) ? (cur+' → '+p) : cur;
}
function renderDiagram(){
  const box=$('modelDiagram'); if(!box) return;
  const dual = modelVals.R1!=null && modelVals.R1!=='';
  const darr=(x1,y1,x2,y2)=>{        // 双向对角箭头（手册四角箭头）
    const dx=x2-x1, dy=y2-y1, L=Math.hypot(dx,dy)||1;
    const ux=dx/L, uy=dy/L, s=7, w=3;
    const hx1=x2-ux*s+(-uy)*w, hy1=y2-uy*s+ux*w;
    const hx2=x2-ux*s-(-uy)*w, hy2=y2-uy*s-ux*w;
    const gx1=x1+ux*s+(-uy)*w, gy1=y1+uy*s+ux*w;
    const gx2=x1+ux*s-(-uy)*w, gy2=y1+uy*s-ux*w;
    return `<line class="darr" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/>
      <polygon class="darrow" points="${x2},${y2} ${hx1},${hy1} ${hx2},${hy2}"/>
      <polygon class="darrow" points="${x1},${y1} ${gx1},${gy1} ${gx2},${gy2}"/>`;
  };
  const caption=(txt)=>`<text class="dcap" x="290" y="${dual?420:330}" text-anchor="middle">${txt}</text>`;
  // 分支：曲线管道 + 到肺腔的实心箭头
  const branch=(x2,y2,grp)=>`
    <g data-diaggrp="${grp}">
      <path class="dpipe" d="M 290 128 C 290 158 ${x2+ (x2<290?-4:4)} 158 ${x2} ${y2-46}"/>
      <polygon class="dhead" points="${x2},${y2-40} ${x2-6},${y2-52} ${x2+6},${y2-52}"/>
    </g>`;
  let svg;
  const pumpLike=!dual && LUNG_KEYS.every(k=>!(modelVals[k]!=null&&modelVals[k]!==''));
  if(dual){
    svg=`
    <svg viewBox="0 0 580 446" xmlns="http://www.w3.org/2000/svg">
      <rect class="dframe" x="30" y="18" width="520" height="356"/>
      ${darr(52,42,86,76)} ${darr(528,42,494,76)}
      ${darr(52,350,86,316)} ${darr(528,350,494,316)}
      <path class="dpipe" d="M 290 18 V 128"/>
      <g data-diaggrp="RT">
        <text class="dlab" x="278" y="66" text-anchor="end">R</text>
        <text class="dval" data-diag="RT" x="278" y="84" text-anchor="end"></text>
      </g>
      <g data-diaggrp="RT_OUT">
        <text class="dlab" x="302" y="66" text-anchor="start">R out</text>
        <text class="dval" data-diag="RT_OUT" x="302" y="84" text-anchor="start"></text>
      </g>
      ${branch(196,258,'R2')}
      ${branch(384,258,'R1')}
      <g data-diaggrp="R2">
        <text class="dlab" x="222" y="150" text-anchor="middle">R2</text>
        <text class="dval" data-diag="R2" x="222" y="168" text-anchor="middle"></text>
      </g>
      <g data-diaggrp="R1">
        <text class="dlab" x="358" y="150" text-anchor="middle">R1</text>
        <text class="dval" data-diag="R1" x="358" y="168" text-anchor="middle"></text>
      </g>
      <g data-diaggrp="C2">
        <circle class="dcirc" cx="196" cy="300" r="44"/>
        <text class="dlab" x="196" y="296" text-anchor="middle">C2</text>
        <text class="dval" data-diag="C2" x="196" y="316" text-anchor="middle"></text>
      </g>
      <g data-diaggrp="C1">
        <circle class="dcirc" cx="384" cy="300" r="44"/>
        <text class="dlab" x="384" y="296" text-anchor="middle">C1</text>
        <text class="dval" data-diag="C1" x="384" y="316" text-anchor="middle"></text>
      </g>
      ${caption('Two Compartment')}
    </svg>`;
  }else{
    svg=`
    <svg viewBox="0 0 580 360" xmlns="http://www.w3.org/2000/svg">
      <rect class="dframe" x="30" y="18" width="520" height="270"/>
      ${darr(52,42,86,76)} ${darr(528,42,494,76)}
      ${darr(52,264,86,230)} ${darr(528,264,494,230)}
      <path class="dpipe" d="M 290 18 V 172"/>
      <g data-diaggrp="RT">
        <text class="dlab" x="278" y="66" text-anchor="end">R</text>
        <text class="dval" data-diag="RT" x="278" y="84" text-anchor="end"></text>
      </g>
      <g data-diaggrp="RT_OUT">
        <text class="dlab" x="302" y="66" text-anchor="start">R out</text>
        <text class="dval" data-diag="RT_OUT" x="302" y="84" text-anchor="start"></text>
      </g>
      <polygon class="dhead" points="290,180 284,168 296,168"/>
      <g data-diaggrp="C1">
        <circle class="dcirc" cx="290" cy="224" r="46"/>
        <text class="dlab" x="290" y="220" text-anchor="middle">C</text>
        <text class="dval" data-diag="C1" x="290" y="240" text-anchor="middle"></text>
      </g>
      ${caption(pumpLike?'Single Compartment · 无 R/C（疑似泵模式）':'Single Compartment')}
    </svg>`;
  }
  box.innerHTML=svg;
  updateDiagram();
}
function updateDiagram(){
  const diag=$('modelDiagram'); if(!diag) return;
  const apply=(k,grp)=>{
    const el=diag.querySelector('[data-diag="'+k+'"]');
    if(el) el.textContent=diagText(k);
    const g=grp?diag.querySelector('[data-diaggrp="'+grp+'"]'):null;
    if(g){
      const p=pendingVals[k];
      const pend=p!=null&&p!==''&&String(modelVals[k]||'').trim()!==p;
      g.classList.toggle('pending',pend);
    }
  };
  apply('RT','RT'); apply('RT_OUT','RT_OUT');
  apply('R1','R1'); apply('R2','R2');
  apply('C1','C1'); apply('C2','C2');
}

/* ------------------------------------------------ Tab: 肺模型 Interactive Control */
/* 子 tab 结构对齐 GUI Interactive Control Panel (手册 Fig 7-5 ~ 7-11) */
const ICP_TABS = {
  lung:{ groups:[
    {g:'气管与支气管阻力 · 顺应性 (Lung Model Param.)', items:[
      ['RT','气管阻力 In','cmH₂O/(L/s)'],['RT_OUT','气管阻力 Out','cmH₂O/(L/s)'],
      ['R1','支气管 1 In','cmH₂O/(L/s)'],['R1_OUT','支气管 1 Out','cmH₂O/(L/s)'],
      ['R2','支气管 2 In','cmH₂O/(L/s)'],['R2_OUT','支气管 2 Out','cmH₂O/(L/s)'],
      ['C1','顺应性 1','mL/cmH₂O'],['C2','顺应性 2','mL/cmH₂O']]}]},
  spont:{ groups:[
    {g:'自主呼吸用力 (Spont. Breath Param., Fig 7-7)', items:[
      ['RATE','呼吸频率','bpm'],['PAUSE','暂停','%',
      ],
      ['INSP_PMUS','吸气肌压','cmH₂O'],['EXP_PMUS','呼气肌压','cmH₂O'],
      ['TRIGGERTIME','触发时间','ms'],
      ['INSP_RISETIME','吸气上升','%'],['INSP_HOLD','吸气保持','%'],['INSP_RELEASETIME','吸气释放','%'],
      ['EXP_RISETIME','呼气上升','%'],['EXP_HOLD','呼气保持','%'],['EXP_RELEASETIME','呼气释放','%']]}]},
  loop:{ groups:[
    {g:'闭环目标 (Closed Loop Vt / MV / CO2Y)', items:[
      ['LOOP','闭环模式',''],['VTSET','目标潮气量','mL'],['MVSET','目标分钟通气量','mL/min'],
      ['ETCO2SET','目标 EtCO₂','mmHg']]},
    {g:'闭环限值', items:[
      ['PVTUPPER','Vt 环压力上限','cmH₂O'],['PVTLOWER','Vt 环压力下限','cmH₂O'],
      ['FMVUPPER','MV 环频率上限','bpm'],['FMVLOWER','MV 环频率下限','bpm'],
      ['PMVUPPER','MV 环压力上限','cmH₂O'],['PMVLOWER','MV 环压力下限','cmH₂O']]},
    {g:'闭环 CO2Y', items:[
      ['CO2FLOW','CO₂ 流量','mL/min'],['PCASECO2','Case CO₂','mmHg'],
      ['PCO2UPPER','pCO₂ 上限','mmHg'],['PCO2LOWER','pCO₂ 下限','mmHg'],
      ['FCO2UPPER','FrCO₂ 上限','%'],['FCO2LOWER','FrCO₂ 下限','%']]}]},
};
/* 旋钮量程 [min, max, step] —— 取自手册 Fig 7-6 / 7-7 刻度 */
const KNOB_RANGE = {
  RT:[0,100,0.5], RT_OUT:[0,100,0.5], R1:[0,100,0.5], R1_OUT:[0,100,0.5],
  R2:[0,100,0.5], R2_OUT:[0,100,0.5], C1:[0,250,1], C2:[0,250,1],
  RATE:[3,150,0.1], PAUSE:[0,100,1], INSP_PMUS:[0,150,0.5], EXP_PMUS:[-60,0,0.5],
  TRIGGERTIME:[0,2000,10],
  INSP_RISETIME:[0,100,1], INSP_HOLD:[0,100,1], INSP_RELEASETIME:[0,100,1],
  EXP_RISETIME:[0,100,1], EXP_HOLD:[0,100,1], EXP_RELEASETIME:[0,100,1],
  VTSET:[0,2000,10], MVSET:[0,30000,100], ETCO2SET:[0,60,1],
  PVTUPPER:[-20,60,1], PVTLOWER:[-20,60,1], FMVUPPER:[5,60,1], FMVLOWER:[5,60,1],
  PMVUPPER:[-20,60,1], PMVLOWER:[-20,60,1],
  CO2FLOW:[0,1000,10], PCASECO2:[0,60,1], PCO2UPPER:[0,100,1], PCO2LOWER:[0,100,1],
  FCO2UPPER:[0,10,0.5], FCO2LOWER:[0,10,0.5],
};
let icpSub='lung';
let modelVals={}, modelNotes={};

function showTab(w){
  state.tab=w;
  $('pageWave').style.display = w==='wave'?'':'none';
  $('pageModel').style.display = w==='model'?'':'none';
  $('tabWaveBtn').classList.toggle('on', w==='wave');
  $('tabModelBtn').classList.toggle('on', w==='model');
  if(w==='wave'){
    panels.forEach(p=>{ if(!p.chart.isDisposed()) p.chart.resize(); });
    render(true);
  }else{
    loadTaiStatus(); loadModel();
  }
}
let taiKv={};                 // 最近一次 TC 状态 (SIM_MODE / SIM_VARFILE 等)
function loadTaiStatus(){
  fetch('/api/tai/status').then(r=>r.json()).then(j=>{
    const b=$('taiBadge');
    if(j.ok){
      b.className='badge live'; b.textContent='● TAI 在线';
      const kv=j.kv||{}; taiKv=kv;
      const file=(kv.SIM_VARFILE||'').split(/[\\/]/).pop()||'?';
      $('taiStatusLine').textContent=(kv.SIM_STATUS||'?')+' ['+(kv.SIM_MODE||'?')+'] · 呼吸 '
        +(kv.SIM_BREATH_CNT||'?')+' · '+file+(kv.SIM_TIME?(' · '+kv.SIM_TIME):'');
      const im=/interact/i.test(kv.SIM_MODE||'');
      $('modeBadge').textContent=im?'交互模式 (IM)':'脚本模式';
      $('btnIM').classList.toggle('primary', !im);
      $('btnSM').classList.toggle('primary', im);
    }else{
      taiKv={};
      b.className='badge pause';
      b.textContent=(j.code==='03')?'○ TAI 在线 · ASL 软件未运行':'○ TAI 不可用';
      $('modeBadge').textContent='—';
      $('taiStatusLine').textContent=j.error||'';
    }
    updatePumpWarn();
  }).catch(e=>{ $('taiStatusLine').textContent=String(e); });
}
/* 泵模式识别：已进交互模式但 R/C 全不可读 → 疑似 Flow/Volume Pump（无呼吸力学参数），
   提示"肺模型 Tab 为空属正常"，避免误判成读取失败 */
const LUNG_KEYS=['RT','RT_OUT','C1','C2','R1','R1_OUT','R2','R2_OUT'];
function updatePumpWarn(){
  const w=$('pumpWarn'); if(!w) return;
  const im=/interact/i.test(taiKv.SIM_MODE||'');
  const lungN=LUNG_KEYS.filter(k=>modelVals[k]!=null&&modelVals[k]!=='').length;
  const allN=Object.keys(modelVals).length;
  if(!im || lungN>0){ w.style.display='none'; return; }
  w.style.display='';
  w.innerHTML = allN===0
    ? '💡 <b>当前仿真疑似 Flow / Volume Pump 泵模式</b>：已进入交互模式 (IM)，但 R/C、Pmus、闭环 IC 参数全部不可读 (ERROR 16)。'
      +'泵模式直接按预设流量/容积波形驱动活塞，<b>不存在呼吸力学参数</b>，肺模型 Tab 为空属正常，不是故障；'
      +'波形模式设置见 GUI 仿真编辑器 Step 3。'
    : '💡 <b>肺模型 R/C 参数不可读 (ERROR 16)</b>：已进入交互模式，但 8 个 R/C 参数均为空'
      +'（Pmus/闭环部分可读）。当前可能为泵模式（无 R/C），请结合 GUI 确认模型类型。';
}
function loadModel(){
  $('taiMsg').textContent='读取模型参数…';
  fetch('/api/tai/model').then(r=>r.json()).then(j=>{
    if(j.ok){
      modelVals=j.values||{}; modelNotes=j.notes||{};
      renderModel();
      const n=Object.keys(modelVals).length;
      $('taiMsg').textContent=n?('已读取 '+n+' 个参数'):'未读到参数（可能尚未进入交互模式）';
    }else $('taiMsg').textContent='读取失败: '+(j.error||'');
    updatePumpWarn();
  }).catch(e=>{ $('taiMsg').textContent=String(e); });
}
function polar(cx,cy,r,deg){
  const a=deg*Math.PI/180;
  return [cx+r*Math.sin(a), cy-r*Math.cos(a)];
}
/* 旋钮 SVG：270° 刻度弧 + 指针，对齐 GUI 绿色旋钮 */
function knobSVG(k, known, val){
  const rng=KNOB_RANGE[k]||[0,100,1];
  const mn=rng[0], mx=rng[1];
  let frac=0;
  if(known && val!=null && isFinite(+val)) frac=Math.max(0,Math.min(1,(+val-mn)/(mx-mn)));
  const cx=42, cy=42, R=24;
  let ticks='';
  for(let i=0;i<=8;i++){
    const th=-135+270*i/8;
    const [x1,y1]=polar(cx,cy,R+2,th), [x2,y2]=polar(cx,cy,R+7,th);
    ticks+='<line x1="'+x1.toFixed(1)+'" y1="'+y1.toFixed(1)
      +'" x2="'+x2.toFixed(1)+'" y2="'+y2.toFixed(1)
      +'" stroke="var(--muted)" stroke-width="1"/>';
  }
  const [nx,ny]=polar(cx,cy,R+1,-135+270*frac);
  const needle=known
    ? '<line class="kneedle" data-mn="'+mn+'" data-mx="'+mx+'" x1="'+cx+'" y1="'+cy
      +'" x2="'+nx.toFixed(1)+'" y2="'+ny.toFixed(1)+'" stroke="var(--accent)" stroke-width="3.4" stroke-linecap="round"/>'
    : '';
  const bodyCol=known?'var(--accent)':'var(--line)';
  return '<svg viewBox="0 0 84 80" width="84" height="80">'
    + ticks
    + '<circle cx="'+cx+'" cy="'+cy+'" r="'+R+'" fill="var(--panel)" stroke="'+bodyCol+'" stroke-width="2.4"/>'
    + needle
    + '<circle cx="'+cx+'" cy="'+cy+'" r="3.4" fill="'+(known?'var(--fg)':'var(--muted)')+'"/>'
    + '</svg>';
}
function knobCell(k, name, unit){
  const v=modelVals[k], note=modelNotes[k];
  const known=v!=null && v!=='';
  const dis=!known;
  let ctl;
  if(k==='LOOP'){
    const cur=(v||'').toUpperCase();
    ctl='<select data-k="LOOP"'+(dis?' disabled':'')+'>'
      +(dis?'<option value=""></option>':'')
      +['NO_LOOP','CONST_VT','CONST_MV','CO2Y'].map(o=>
        '<option value="'+o+'"'+(cur===o?' selected':'')+'>'+o+'</option>').join('')
      +'</select>';
  }else{
    ctl='<input data-k="'+esc(k)+'" value="'+(known?esc(String(v)):'')+'"'
      +(dis?' disabled placeholder="不可读"':'')
      +(note?(' title="'+esc(note)+'"'):'')+'>';
  }
  return '<div class="kcell'+(dis?' kdis':'')+'" data-kcell="'+esc(k)+'">'
    +'<div class="klab"><b>'+esc(name)+'</b><span class="kunit">'+esc(unit||'')+' · '+esc(k)+'</span></div>'
    +(k==='LOOP'?'':knobSVG(k, known, v))
    + ctl +'</div>';
}
function setKnobValue(k, v, inp){
  const rng=KNOB_RANGE[k];
  if(rng){
    const [mn,mx,st]=rng;
    let nv=Math.max(mn, Math.min(mx, v));
    nv=Math.round((nv-mn)/st)*st+mn;
    nv=+nv.toFixed(4);
    inp.value=String(nv);
  }else inp.value=String(Math.round(v*100)/100);
  inp.dispatchEvent(new Event('input',{bubbles:true}));
}
function refreshKnobNeedle(k){
  const cell=document.querySelector('.kcell[data-kcell="'+k+'"]');
  if(!cell) return;
  const nd=cell.querySelector('.kneedle');
  if(nd){
    const mn=+nd.dataset.mn, mx=+nd.dataset.mx;
    const v=+(cell.querySelector('[data-k]').value||mn);
    const [nx,ny]=polar(42,42,25,-135+270*Math.max(0,Math.min(1,(v-mn)/(mx-mn))));
    nd.setAttribute('x2',nx.toFixed(1)); nd.setAttribute('y2',ny.toFixed(1));
  }
  const p=pendingVals[k];
  const pend=p!=null&&p!==''&&String(modelVals[k]||'').trim()!==p;
  cell.classList.toggle('pending',pend);
}
function attachKnobEvents(box){
  box.querySelectorAll('.kcell').forEach(cell=>{
    const k=cell.dataset.kcell;
    const inp=cell.querySelector('[data-k]');
    if(!inp) return;
    const rng=KNOB_RANGE[k];
    const svg=cell.querySelector('svg');
    if(svg && rng){
      let dragging=false, sy=0, sv=0;
      const [mn,mx]=rng;
      svg.addEventListener('pointerdown',e=>{
        if(inp.disabled) return;
        dragging=true; sy=e.clientY;
        sv=parseFloat(inp.value)||mn;
        svg.setPointerCapture(e.pointerId);
        e.preventDefault();
      });
      svg.addEventListener('pointermove',e=>{
        if(!dragging) return;
        setKnobValue(k, sv+(sy-e.clientY)/160*(mx-mn), inp);
      });
      svg.addEventListener('pointerup',()=>{dragging=false;});
      svg.addEventListener('pointercancel',()=>{dragging=false;});
      svg.addEventListener('wheel',e=>{
        if(inp.disabled) return;
        e.preventDefault();
        const cur=parseFloat(inp.value)||mn;
        setKnobValue(k, cur+(e.deltaY<0?1:-1)*rng[2], inp);
      },{passive:false});
    }
    inp.addEventListener('input',()=>{
      CFG.userTouched=true;
      pendingVals[k]=String(inp.value).trim();
      refreshKnobNeedle(k);
      updateDiagram();
      renderPmusPreview();
    });
  });
}
function renderModel(){
  ['Lung','Spont','Loop'].forEach(tail=>{
    const box=$('knobs'+tail); if(!box) return;
    const st=tail.toLowerCase();
    let html='';
    ICP_TABS[st].groups.forEach(g=>{
      html+='<div class="mghead">'+esc(g.g)+'</div>';
      g.items.forEach(it=>html+=knobCell(it[0],it[1],it[2]));
    });
    box.innerHTML=html;
    attachKnobEvents(box);
  });
  pendingVals={};
  renderDiagram();
  renderPmusPreview();
}
/* ---- 自主呼吸 Pmus 剖面预览（−Δp_mus 惯例，同 Fig 7-7）---- */
function renderPmusPreview(){
  const box=$('pmusPrev'); if(!box) return;
  const gv=k=>{
    const p=pendingVals[k];
    if(p!=null&&p!==''&&isFinite(+p)) return +p;
    const v=modelVals[k];
    return (v!=null&&v!==''&&isFinite(+v))?+v:null;
  };
  const rate=Math.max(3,(gv('RATE')!=null?gv('RATE'):15));
  const T=60/rate;
  const ip=Math.abs(gv('INSP_PMUS')||0);
  const epRaw=gv('EXP_PMUS')||0;
  const ep=-Math.abs(epRaw);                     // 呼气肌压约定为负
  const ri=(gv('INSP_RISETIME')!=null?gv('INSP_RISETIME'):20)/100;
  const ho=(gv('INSP_HOLD')!=null?gv('INSP_HOLD'):0)/100;
  const rl=(gv('INSP_RELEASETIME')!=null?gv('INSP_RELEASETIME'):30)/100;
  const W=860,H=210,L=64,Rr=18,Tp=36,B=42;
  const pw=W-L-Rr, ph=H-Tp-B;
  const yMax=(epRaw<0? (-epRaw)*1.25+0.5 : 0.8); // 呼气为负→画在 0 线上方
  const yMin=-(ip*1.18+0.5);
  const yOf=v=>Tp+ph*(yMax-v)/(yMax-yMin);
  const smooth=t=>t*t*(3-2*t);                   // smoothstep 上升/释放沿
  const Ti=0.4*T;                                // 吸气窗示意取周期 40%
  const pOf=t=>{
    if(t<=Ti){
      const u=t/Ti;
      const a=ri, b=Math.min(1,ri+ho), c=Math.min(1,b+rl);
      if(u<a) return ip*smooth(u/Math.max(1e-6,a));
      if(u<b) return ip;
      if(u<c) return ip*(1-smooth((u-b)/Math.max(1e-6,c-b)));
      return 0;
    }
    const u=(t-Ti)/(T-Ti);                       // 呼气：中段平台 ep，两端归零
    if(u<0.15) return ep*smooth(u/0.15);
    if(u<0.85) return ep;
    return ep*(1-smooth((u-0.85)/0.15));
  };
  const N=260; let pts='';
  for(let i=0;i<=N;i++){
    const t=T*i/N;
    pts+=(i?' L ':'M ')+(L+pw*t/T).toFixed(1)+' '+yOf(-pOf(t)).toFixed(1);   // 画 −Δp_mus
  }
  // y 轴刻度：nice step
  const span=Math.max(yMax, -yMin);
  const stp=span<=2?0.5:span<=6?1:span<=12?2:5;
  let grid='';
  for(let v=Math.ceil(yMin/stp)*stp; v<=yMax+1e-9; v+=stp){
    const y=yOf(v);
    grid+='<line class="pgrid" x1="'+L+'" y1="'+y.toFixed(1)+'" x2="'+(W-Rr)+'" y2="'+y.toFixed(1)+'"/>'
      +'<text class="ptick" x="'+(L-7)+'" y="'+(y+3.5).toFixed(1)+'" text-anchor="end">'
      +(+v.toFixed(2))+'</text>';
  }
  let xt='';
  for(let i=0;i<=8;i++){
    const x=L+pw*i/8;
    xt+='<line class="pgrid" x1="'+x.toFixed(1)+'" y1="'+Tp+'" x2="'+x.toFixed(1)+'" y2="'+(Tp+ph)+'"/>'
      +'<text class="ptick" x="'+x.toFixed(1)+'" y="'+(Tp+ph+15)+'" text-anchor="middle">'
      +(T*i/8).toFixed(1)+'</text>';
  }
  box.innerHTML=`
  <svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">
    ${grid}${xt}
    <rect class="paxis" x="${L}" y="${Tp}" width="${pw}" height="${ph}" fill="none"/>
    <line class="pzero" x1="${L}" y1="${yOf(0).toFixed(1)}" x2="${W-Rr}" y2="${yOf(0).toFixed(1)}"/>
    <text class="plab" x="${L}" y="20">Pressure [cmH₂O]（−Δp_mus）</text>
    <text class="plab" x="${W-Rr}" y="20" text-anchor="end">周期 T = ${T.toFixed(2)} s · RATE ${rate}</text>
    <path class="pcurve" d="${pts}"/>
    <text class="ptick" x="${L+pw/2}" y="${H-6}" text-anchor="middle">Time (s)</text>
  </svg>`;
}
function taiInteractive(on){
  const msg=$('taiMsg');
  msg.textContent=on?'发送 IM（进入交互模式）…':'发送 SM（回脚本模式）…';
  fetch('/api/tai/interactive',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({on})})
    .then(r=>r.json()).then(j=>{
      msg.textContent=j.ok
        ? (on?'✓ 已进入交互模式（脚本模式已退出）':'✓ 已退出交互，回到脚本模式')
        : '✗ '+(j.error||'');
      loadTaiStatus();
      if(j.ok) loadModel();
    }).catch(e=>{ msg.textContent=String(e); });
}
function applySub(st){
  const ICP=ICP_TABS[st]; if(!ICP) return;
  const items=[]; ICP.groups.forEach(g=>g.items.forEach(it=>items.push(it[0])));
  const root=$('st'+st.charAt(0).toUpperCase()+st.slice(1));
  const params={};
  root.querySelectorAll('[data-k]').forEach(inp=>{
    const k=inp.dataset.k;
    if(!items.includes(k)) return;
    const nv=String(inp.value).trim();
    if(nv==='') return;
    const ov=modelVals[k];
    if(ov!=null && String(ov).trim()===nv) return;   // 未改动
    params[k]=nv;
  });
  if(!Object.keys(params).length){ flashMsg('该页没有修改项'); return; }
  const msg=$('taiMsg');
  msg.textContent='写入 '+Object.keys(params).join(', ')+' …';
  fetch('/api/tai/set',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({params})})
    .then(r=>r.json()).then(j=>{
      if(j.ok){
        Object.assign(modelVals, j.readback||{});
        renderModel();
        msg.textContent='✓ 已写入并回读: '+Object.entries(j.readback||{})
          .map(([k,v])=>k+'='+v).join('　');
        loadTaiStatus();
      }else msg.textContent='✗ '+(j.error||'写入失败');
    }).catch(e=>{ msg.textContent=String(e); });
}

/* ------------------------------------------------ 轮询 */
async function poll(){
  try{
    const r=await fetch('/api/data?cursor='+state.cursor,{cache:'no-store'});
    const j=await r.json();
    if(j.reset) state.rows=[];
    if(j.rows && j.rows.length) state.rows.push(...j.rows);
    if(state.rows.length>MAXKEEP) state.rows=state.rows.slice(-MAXKEEP);
    state.cursor=j.cursor;
    state.status=j.status||{};
    if(j.breath && Object.keys(j.breath).length) state.lastBreath=j.breath;
    if(j.status && j.status.channels && j.status.channels.length){
      const sig=j.status.channels.map(c=>c.key).join(',');
      if(sig!==CFG.signature){
        CFG.signature=sig; CFG.channels=j.status.channels; CFG.ready=true;
        state.meta=j.status;
        CFG.channels.forEach((c,i)=>{ CM[c.key]=c; if(!COL[c.key]) COL[c.key]=PALETTE[i%PALETTE.length]; });
        if(!CFG.userTouched) buildDefaultPanels(); else rebuildChips();
      }
      state.meta=j.status;
    }
    const alive=state.status.wave_age_sec!=null && state.status.wave_age_sec<3;
    const badge=$('liveBadge');
    badge.className='badge '+(state.paused?'pause':(alive?'live':''));
    badge.textContent=state.paused?'⏸ 已暂停（数据仍在接收）':(alive?'● 实时':'○ 等待数据');
    const hz=state.meta && state.meta.sample_rate_hz;
    $('rateBadge').textContent = alive
      ? ((hz?('采样 '+Math.round(hz)+' Hz'):'采样中')+' · '
         +(state.window?state.window+' s 窗口':'全部缓冲'))
      : '等待 6343 广播';
    $('errmsg').textContent=state.status.error||'';
    if(j.rows && j.rows.length) renderCards();
    const rsig=(state.status.breath_count||0)+'|'+breathKeys().length+'|'
      +Object.keys(state.lastBreath).length;
    if(rsig!==state.rtaSig){ state.rtaSig=rsig; renderRTA(); }
    if(!state.paused) render(false);
  }catch(e){
    $('liveBadge').className='badge pause';
    $('liveBadge').textContent='○ 服务未响应';
    $('errmsg').textContent=String(e);
  }
}

/* ------------------------------------------------ 交互绑定 */
function setPaused(v){
  if(state.paused===v) return;          // dataZoom 联动会重复触发，幂等保护
  state.paused=v;
  $('btnPause').textContent=v?'▶ 继续跟随':'⏸ 暂停';
  $('btnPause').classList.toggle('primary',!v);
  const badge=$('liveBadge');
  badge.className='badge '+(v?'pause':'live');
  badge.textContent=v?'⏸ 已暂停（数据仍在接收）':'● 实时';
  if(!v){
    suppressZoom=true;
    panels.forEach(p=>{ if(!p.chart.isDisposed())
      p.chart.dispatchAction({type:'dataZoom',start:0,end:100}); });
    render(true);
    setTimeout(()=>{ suppressZoom=false; },80);
  }
}
function bind(){
  $('btnPause').onclick=()=>setPaused(!state.paused);
  $('tabWaveBtn').onclick=()=>showTab('wave');
  $('tabModelBtn').onclick=()=>showTab('model');
  $('taiRefresh').onclick=loadTaiStatus;
  $('modelRefresh').onclick=loadModel;
  $('btnIM').onclick=()=>taiInteractive(true);
  $('btnSM').onclick=()=>taiInteractive(false);
  document.querySelectorAll('#icpTabs [data-st]').forEach(b=>b.onclick=()=>{
    icpSub=b.dataset.st;
    document.querySelectorAll('#icpTabs [data-st]').forEach(x=>x.classList.toggle('on',x===b));
    $('stLung').style.display = icpSub==='lung'?'':'none';
    $('stSpont').style.display= icpSub==='spont'?'':'none';
    $('stLoop').style.display = icpSub==='loop'?'':'none';
    renderPmusPreview();
  });
  document.querySelectorAll('[data-apply]').forEach(b=>b.onclick=()=>applySub(b.dataset.apply));
  document.addEventListener('keydown', e=>{
    if(e.code==='Space' && !e.repeat &&
       !/INPUT|TEXTAREA/.test(document.activeElement.tagName) &&
       !document.activeElement.isContentEditable){
      e.preventDefault(); setPaused(!state.paused);
    }
  });
  $('btnTheme').onclick=()=>{
    state.theme=state.theme==='dark'?'light':'dark';
    document.documentElement.dataset.theme=state.theme;
    render(true);
  };
  $('btnCsv').onclick=()=>{ location.href='/api/export?sec='+(state.window||0); };
  $('addCard').onclick=()=>{
    CFG.userTouched=true;
    makePanel({title:'面板 '+(panels.length+1), fams:[]});
    render(true);
  };
  const ws=$('winSlider'), wv=$('winVal');
  const segBtns=()=>document.querySelectorAll('#winSeg button');
  document.querySelectorAll('#winSeg button').forEach(b=>b.onclick=()=>{
    segBtns().forEach(x=>x.classList.toggle('on',x===b));
    if(b.dataset.v==='custom'){          // 仅自定义模式显示滑块，沿用上次滑块值
      state.window=+ws.value||30;
      wv.textContent=state.window+' s';
      ws.style.display='';
    }else{
      state.window=+b.dataset.v;
      ws.style.display='none';
      wv.textContent = state.window>0 ? state.window+' s' : '全部缓冲';
    }
    render(true);
  });
  ws.addEventListener('input',()=>{
    state.window=+ws.value;
    wv.textContent=state.window+' s';
    segBtns().forEach(x=>x.classList.toggle('on',x.dataset.v==='custom'));
    render(true);
  });
  const unitBind=(id, key, fn)=>{
    document.querySelectorAll('#'+id+' button').forEach(b=>b.onclick=()=>{
      state.units[key]=b.dataset.v;
      document.querySelectorAll('#'+id+' button').forEach(x=>x.classList.toggle('on',x===b));
      renderCards(); render(true);
    });
  };
  unitBind('unitP','pressure'); unitBind('unitF','flow'); unitBind('unitV','volume');
  window.addEventListener('resize',()=>panels.forEach(p=>{
    if(!p.chart.isDisposed()) p.chart.resize(); }));
}
document.addEventListener('DOMContentLoaded',()=>{
  bind();
  buildDefaultPanels();          // 先建 3 个默认面板（通道到位后自动填充 chips）
  const qs=new URLSearchParams(location.search);
  if(qs.get('tab')==='model'){
    showTab('model');
    const sub=qs.get('sub');
    if(sub && document.querySelector('#icpTabs [data-st="'+sub+'"]'))
      document.querySelector('#icpTabs [data-st="'+sub+'"]').click();
  }
  render(true); renderCards();
  poll(); setInterval(poll,250);
  setInterval(()=>{ if(pending && !state.paused) render(true); },400);
});
</script>
</body>
</html>
"""


def find_echarts() -> Path:
    p = HERE / "assets" / "echarts.min.js"
    if not p.is_file():
        raise SystemExit(f"找不到 ECharts 库: {p}")
    return p


def build_page(hub: Hub, source_label: str) -> str:
    js = find_echarts().read_text(encoding="utf-8")
    cfg = {"channels": [], "ready": False, "signature": "", "source": source_label}
    html = HTML.replace("__SOURCE__", source_label)
    html = html.replace("/*__ECHARTS__*/", js)
    return html.replace("/*__CONFIG__*/", json.dumps(cfg, ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser(description="ASL 5000 实时波形看板 (ECharts 多面板)")
    ap.add_argument("--host", default="127.0.0.1", help="ASL 广播主机")
    ap.add_argument("--breath-port", type=int, default=am.DEFAULT_BREATH_PORT)
    ap.add_argument("--wave-port", type=int, default=am.DEFAULT_WAVE_PORT)
    ap.add_argument("--http-host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765, help="看板 HTTP 端口")
    ap.add_argument("--replay", default="", help="回放历史 waveform CSV（无硬件验证）")
    ap.add_argument("--tail", default="", help="尾随正在写入的 waveform CSV")
    ap.add_argument("--speed", type=float, default=1.0, help="回放倍速")
    ap.add_argument("--breath-csv", default="", help="回放时配套的 breath_params.csv")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--max-rows", type=int, default=240000)
    args = ap.parse_args()

    stop = threading.Event()
    if args.replay:
        path = Path(args.replay)
        if not path.is_file():
            raise SystemExit(f"回放文件不存在: {path}")
        bcsv = Path(args.breath_csv) if args.breath_csv else (
            path.parent / "breath_params.csv")
        label = f"回放 {path.name} ×{args.speed:g}"
        hub = Hub(label, args.max_rows)
        threads = start_replay(hub, stop, path, args.speed, bcsv)
    elif args.tail:
        path = Path(args.tail)
        label = f"尾随 {path.name}"
        hub = Hub(label, args.max_rows)
        threads = start_tail(hub, stop, path)
    else:
        label = f"TAI 广播 {args.host}:{args.wave_port}/{args.breath_port}"
        hub = Hub(label, args.max_rows)
        threads = start_broadcast(hub, stop, args.host,
                                  args.breath_port, args.wave_port)

    global PAGE
    PAGE = build_page(hub, label)
    Handler.hub = hub
    srv = ThreadingHTTPServer((args.http_host, args.port), Handler)
    srv.daemon_threads = True
    url = f"http://{args.http_host}:{args.port}/"
    print(f"ASL 5000 实时波形看板已启动: {url}", flush=True)
    print(f"数据源: {label}", flush=True)
    print("Ctrl-C 结束", flush=True)
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n正在停止…", flush=True)
    finally:
        stop.set()
        srv.shutdown()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
