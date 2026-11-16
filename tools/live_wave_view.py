#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ASL 5000 **实时**波形看板 —— 边测边看，ECharts 可交互。

三种数据源（互不依赖，按场景选）::

    python tools/live_wave_view.py                         # 直连 6343/6342 TAI 广播（推荐）
    python tools/live_wave_view.py --tail <waveform.csv>   # 尾随正在写的录波 CSV
    python tools/live_wave_view.py --replay <waveform.csv> --speed 8   # 历史 CSV 回放（无硬件验证）
    python tools/live_wave_view.py --port 8765 --no-browser

启动后浏览器打开 http://127.0.0.1:8765 。Ctrl-C 结束。

背景：ASL5000_SW3.6.exe 运行时向所有 TCP 客户端广播两路数据（无需 TAI Server 授权）：
  - 6342  每次呼吸结束推全部呼吸参数（ASCII，"参数名<TAB>数值"）
  - 6343  连续波形流（TAB 分隔，首行列名，采样率随脚本 64/128/512 Hz）
每帧前有 4 字节大端 int32 长度头，且**一帧含约 100 行**（批量推送，到达时间不均匀）。
采样率必须读波形自带的 Time (sec) 列，不能用接收速率/Recv Time 推算；实测与厂商 .rwb
的 Time 列逐点相同。本工具与 asl-monitor MCP 的录波 CSV 是**同一份数据**，
只是把数字变成了图；厂商 .rwb/.dtb 是 ASL 软件自己落盘的另一套二进制留存（见 build_wave_view.py）。

注意：6343 广播**没有 Total Flow 列**（只有 .dtb 才有），所以本页不伪造流量曲线，
流量相关数值取自 6342 的逐呼吸参数（Peak Insp./Exp. Flow）。

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

FAMILY_UNIT = {"pressure": "cmH2O", "volume": "mL", "volt": "V", "count": "#"}


def match_channels(header: list[str]) -> tuple[list[dict], int, int]:
    """把 6343 表头映射成前端通道定义；返回 (channels, time_col, breath_col)。"""
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
    return channels, itime, bcol


# ---------------------------------------------------------------- 数据枢纽
class Hub:
    """线程安全的环形缓冲 + 增量游标。"""

    def __init__(self, source: str, max_rows: int = 240000):
        self.lock = threading.Lock()
        self.rows: deque[list[float]] = deque(maxlen=max_rows)
        self.header: list[str] = []
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

    def set_header(self, header: list[str]):
        with self.lock:
            if header == self.header:
                return
            self.header = list(header)
            self.channels, self.time_col, self.breath_col = match_channels(header)

    def add_rows(self, rows: list[list[float]]):
        if not rows:
            return
        with self.lock:
            self.rows.extend(rows)
            self.total += len(rows)
            self.last_wave_at = time.time()
            self.conn["waveform"] = True

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
            "header": self.header,
            "channels": self.channels,
            "time_col": self.time_col,
            "breath_col": self.breath_col,
        }

    def status(self) -> dict:
        with self.lock:
            return self._status_locked()

    def export(self, sec: float | None) -> tuple[list[str], list[list[float]]]:
        with self.lock:
            header = list(self.header)
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
def to_floats(rows: list[list[str]], width: int) -> list[list[float]]:
    out = []
    for r in rows:
        if len(r) < width:
            r = r + [""] * (width - len(r))
        v = []
        ok = True
        for s in r[:width]:
            try:
                v.append(float(s))
            except (ValueError, TypeError):
                v.append(float("nan"))
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


# ---------------------------------------------------------------- HTTP
PAGE = None  # 延迟填充，避免大字符串污染模块导入


class Handler(BaseHTTPRequestHandler):
    hub: Hub = None
    server_version = "ASLLiveView/1.0"

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
            lines += [",".join(f"{v:.9g}" for v in r) for r in rows]
            body = ("\n".join(lines) + "\n").encode("utf-8-sig")
            name = f"asl_live_{datetime.now():%Y%m%d_%H%M%S}.csv"
            self._send(body, "text/csv; charset=utf-8",
                       {"Content-Disposition": f'attachment; filename="{name}"'})
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
  --bg:#0f141b; --panel:#171e27; --panel2:#1d2631; --line:#2b3745;
  --fg:#e6edf3; --muted:#8b9bab; --accent:#4ea8ff; --chip:#222e3c;
  --chipon:#1d4f7c; --good:#39c07d; --warn:#ff8a65;
}
html[data-theme="light"]{
  --bg:#f4f6f9; --panel:#fff; --panel2:#f0f3f7; --line:#d6dee7;
  --fg:#1b2430; --muted:#63707f; --accent:#0b6bcb; --chip:#e8eef5;
  --chipon:#b9d9f8; --good:#12834f; --warn:#c94b20;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:13px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{display:flex;align-items:center;gap:12px;padding:11px 18px;
  border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:20}
h1{font-size:16px;margin:0;font-weight:650}
h1 small{font-weight:400;color:var(--muted);font-size:12px;margin-left:10px}
.spacer{flex:1}
button,select{background:var(--chip);color:var(--fg);border:1px solid var(--line);
  border-radius:6px;padding:5px 10px;cursor:pointer;font:inherit}
button:hover,select:hover{border-color:var(--accent)}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
.wrap{padding:12px 18px 26px;max-width:1920px;margin:0 auto}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:11px 14px;margin-bottom:11px}
.row{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:6px 0}
.row>label.tag{color:var(--muted);min-width:68px;font-size:12px}
.chip{background:var(--chip);border:1px solid var(--line);border-radius:20px;
  padding:4px 11px;cursor:pointer;user-select:none;font-size:12px;white-space:nowrap}
.chip.on{background:var(--chipon);border-color:var(--accent)}
.chip .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.seg button{border:0;border-radius:0;border-right:1px solid var(--line);padding:5px 10px}
.seg button:last-child{border-right:0}
.seg button.on{background:var(--accent);color:#fff}
.hint{color:var(--muted);font-size:12px}
.badge{background:var(--panel2);border:1px solid var(--line);border-radius:4px;
  padding:2px 7px;font-size:11px;color:var(--muted)}
.badge.live{color:var(--good);border-color:var(--good)}
.badge.pause{color:var(--warn);border-color:var(--warn)}
#chart{width:100%;height:520px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(126px,1fr));gap:8px}
.kv{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:7px 10px}
.kv .k{color:var(--muted);font-size:11px}
.kv .v{font-size:17px;font-weight:650;font-variant-numeric:tabular-nums;margin-top:2px}
.kv .u{font-size:11px;color:var(--muted);font-weight:400;margin-left:3px}
.warnbox{border-left:3px solid var(--warn);padding:7px 12px;background:var(--panel2);
  color:var(--muted);font-size:12px;margin-bottom:11px}
#errmsg{color:var(--warn);font-size:12px}
</style>
</head>
<body>
<header>
  <h1>ASL 5000 实时波形 <small>__SOURCE__</small></h1>
  <span class="badge live" id="liveBadge">● 连接中</span>
  <span class="badge" id="ptBadge">0 点</span>
  <span class="badge" id="rateBadge">— Hz</span>
  <div class="spacer"></div>
  <button id="btnPause" class="primary">⏸ 暂停</button>
  <button id="btnCsv">导出 CSV</button>
  <button id="btnTheme">切换主题</button>
</header>
<div class="wrap">

<div class="card">
  <div class="row"><label class="tag">跟随窗口</label>
    <div class="seg" id="winSeg">
      <button data-v="5">5 s</button><button data-v="10">10 s</button>
      <button data-v="30" class="on">30 s</button><button data-v="60">60 s</button>
      <button data-v="0">全部缓冲</button>
    </div>
    <span class="hint">跟随模式自动滚到最新；点「暂停」后可自由缩放/框选/平移查看历史，数据仍在后台接收</span>
  </div>
  <div class="row"><label class="tag">单位</label>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
      <span class="hint">压力</span><div class="seg" id="unitP">
        <button data-v="cmH2O" class="on">cmH₂O</button><button data-v="hPa">hPa</button></div>
      <span class="hint">容积</span><div class="seg" id="unitV">
        <button data-v="mL" class="on">mL</button><button data-v="L">L</button></div>
    </div>
    <span class="hint" id="errmsg"></span>
  </div>
  <div class="row"><label class="tag">通道</label><div id="chips" style="display:flex;flex-wrap:wrap;gap:6px"></div>
    <button id="btnOnlyP">只看压力</button><button id="btnAll">全选</button>
    <button id="btnReset">复位视图</button>
  </div>
</div>

<div class="warnbox">
  数据源：<b>6342/6343 TAI 广播</b>（与 asl-monitor 录波 CSV 同源）。广播<b>不含 Total Flow 列</b>，
  故本页不画流量曲线；峰值流量等取自逐呼吸参数卡片。厂商 <code>.rwb/.dtb</code> 是 ASL 软件另存的二进制留存，
  用 <code>build_wave_view.py</code> 做事后精细对比。<br>
  采样率由 <code>Time (sec)</code> 列的中位步长实测（本机 64/128/512 Hz，随脚本变），与 <code>.rwb</code> 逐点一致；
  <code>Recv Time</code> 只记录整批到达的墙钟时刻，<b>不能</b>拿它推算采样率。
</div>

<div class="card"><div class="cards" id="cards"></div></div>

<div class="card" style="padding:6px 8px"><div id="chart"></div></div>

</div>
<script>/*__ECHARTS__*/</script>
<script>
"use strict";
const CFG = /*__CONFIG__*/;
const PALETTE = ['#4ea8ff','#ffb454','#39c07d','#e879b9','#a78bfa','#f87171',
                 '#22d3ee','#facc15','#fb923c','#94a3b8','#2dd4bf','#c084fc','#f472b6'];
const UNIT_FACTOR = {pressure:{cmH2O:1,hPa:0.980665}, volume:{mL:1,L:0.001},
                     volt:{V:1}, count:{'#':1}};
const state = {
  channels: new Set(), units:{pressure:'cmH2O',volume:'mL'},
  window:30, paused:false, theme:'dark', cursor:0, rows:[],
  meta:null, lastBreath:{}, status:{}
};
let chart = null, lastRender = 0, pending = false;
let suppressZoom = false;
const MAXKEEP = 240000;
const $ = id => document.getElementById(id);
const CM = {};

function themeColors(){
  const cs = getComputedStyle(document.documentElement);
  return {fg:cs.getPropertyValue('--fg').trim(), muted:cs.getPropertyValue('--muted').trim(),
          line:cs.getPropertyValue('--line').trim(), panel:cs.getPropertyValue('--panel').trim()};
}
function unitOf(c){
  const f = c.family;
  return (f==='pressure'||f==='volume') ? state.units[f] : c.unit;
}
function factorOf(c){
  const t = UNIT_FACTOR[c.family] || {};
  return t[unitOf(c)] !== undefined ? t[unitOf(c)] : 1;
}
function initChips(){
  const box = $('chips'); box.innerHTML='';
  CFG.channels.forEach((c,i)=>{
    CM[c.key]=c;
    if(!CFG.ready && c.on) state.channels.add(c.key);
    const el=document.createElement('span');
    el.className='chip'+(state.channels.has(c.key)?' on':'');
    el.dataset.k=c.key;
    el.innerHTML='<span class="dot" style="background:'+PALETTE[i%PALETTE.length]+'"></span>'
                 + c.label.replace(/\s*\(.*$/,'');
    el.onclick=()=>{
      if(state.channels.has(c.key)) state.channels.delete(c.key);
      else state.channels.add(c.key);
      el.classList.toggle('on',state.channels.has(c.key));
      render(true);
    };
    box.appendChild(el);
  });
}
function syncChips(){
  document.querySelectorAll('#chips .chip').forEach(el=>
    el.classList.toggle('on',state.channels.has(el.dataset.k)));
}
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
function breathMarks(it, rows){
  const bc = state.meta ? state.meta.breath_col : -1;
  if(bc<0 || rows.length<2) return [];
  const out=[], tEnd=rows[rows.length-1][it];
  for(let i=1;i<rows.length;i++){
    if(rows[i][bc]!==rows[i-1][bc] && rows[i][it]>=tEnd-Math.max(state.window||1e9,5)){
      out.push({xAxis:rows[i][it]});
      if(out.length>40) out.shift();
    }
  }
  return out;
}
function buildOption(){
  const c = themeColors();
  const it = state.meta ? state.meta.time_col : 0;
  const rows = windowRows();
  const chosen = CFG.channels.filter(x=>state.channels.has(x.key));
  const hasP = chosen.some(x=>x.family==='pressure');
  const hasV = chosen.some(x=>x.family==='volume');
  const hasA = chosen.some(x=>x.family==='volt'||x.family==='count');
  const grids=[{left:74,right:hasV?76:26,top:52,height:hasA?300:378}];
  if(hasA) grids.push({left:74,right:hasV?76:26,top:392,height:80});
  const xAxes=[{type:'value',gridIndex:0,name:'时间 (s)',nameLocation:'middle',nameGap:25,
    min:'dataMin',max:'dataMax',nameTextStyle:{color:c.muted},
    axisLabel:{color:c.muted,formatter:v=>(+v).toFixed(2)},
    axisLine:{lineStyle:{color:c.line}},splitLine:{lineStyle:{color:c.line,opacity:.45}}}];
  if(hasA) xAxes.push({type:'value',gridIndex:1,min:'dataMin',max:'dataMax',
    axisLabel:{color:c.muted,formatter:v=>(+v).toFixed(1)},
    axisLine:{lineStyle:{color:c.line}},splitLine:{show:false}});
  const axisName={nameLocation:'middle',nameRotate:90,nameGap:46,
                  nameTextStyle:{color:c.muted,fontSize:11}};
  const yAxes=[Object.assign({type:'value',gridIndex:0,position:'left',
    name:'压力 '+state.units.pressure,axisLabel:{color:c.muted},
    axisLine:{show:true,lineStyle:{color:c.line}},
    splitLine:{lineStyle:{color:c.line,opacity:.45}}},axisName)];
  if(hasV) yAxes.push(Object.assign({type:'value',gridIndex:0,position:'right',
    name:'容积 '+state.units.volume,axisLabel:{color:c.muted},
    axisLine:{show:true,lineStyle:{color:c.line}},splitLine:{show:false}},
    axisName,{nameGap:50}));
  if(hasA) yAxes.push(Object.assign({type:'value',gridIndex:1,position:'left',
    name:'Aux / 检测',axisLabel:{color:c.muted},
    axisLine:{show:true,lineStyle:{color:c.line}},splitLine:{show:false}},axisName));

  const series=[], legend=[];
  chosen.forEach((ch,i)=>{
    const f=factorOf(ch), color=PALETTE[CFG.channels.indexOf(ch)%PALETTE.length];
    const aux = ch.family==='volt'||ch.family==='count';
    const short = ch.label.replace(/\s*\(.*$/,'');
    const name = short+' ('+unitOf(ch)+')';
    legend.push(name);
    const data = new Array(rows.length);
    for(let k=0;k<rows.length;k++){
      const v=rows[k][ch.col];
      data[k]=[rows[k][it], isFinite(v)?v*f:null];
    }
    series.push({name:name,type:'line',showSymbol:false,symbol:'none',
      xAxisIndex:aux?1:0,
      yAxisIndex:aux?(hasA?yAxes.length-1:0):(ch.family==='volume'?1:0),
      lineStyle:{width:1.4,color:color},itemStyle:{color:color},
      sampling:'lttb',animation:false,data:data,
      connectNulls:false});
  });
  if(series.length && it>=0){
    series[0].markLine={silent:true,symbol:'none',
      lineStyle:{color:PALETTE[2],type:'dashed',opacity:.42,width:1},
      label:{show:false},data:breathMarks(it,rows)};
  }
  // 暂停后强制重绘（切单位/通道）时保留用户当前的缩放区间
  let dzStart=0, dzEnd=100;
  if(state.paused && chart){
    try{ const dz=chart.getOption().dataZoom[0]; dzStart=dz.start; dzEnd=dz.end; }catch(e){}
  }
  return {
    backgroundColor:'transparent',
    textStyle:{color:c.fg},
    animation:false,
    grid:grids, xAxis:xAxes, yAxis:yAxes, series:series,
    legend:{type:'scroll',top:8,left:80,right:24,textStyle:{color:c.muted},
            pageTextStyle:{color:c.muted},itemWidth:18,itemHeight:9,data:legend},
    tooltip:{trigger:'axis',axisPointer:{type:'cross',label:{backgroundColor:c.panel}},
      backgroundColor:c.panel,borderColor:c.line,textStyle:{color:c.fg,fontSize:12},
      formatter:ps=>{
        if(!ps||!ps.length) return '';
        let s='<b>t = '+(+ps[0].value[0]).toFixed(3)+' s</b><br/>';
        ps.forEach(p=>{ if(p.value[1]!=null)
          s+=p.marker+p.seriesName+' : <b>'+(+p.value[1]).toFixed(3)+'</b><br/>'; });
        return s;
      }},
    dataZoom:[
      {type:'inside',xAxisIndex:hasA?[0,1]:[0],filterMode:'none',start:dzStart,end:dzEnd,
       zoomOnMouseWheel:true,moveOnMouseMove:true,moveOnMouseWheel:false},
      {type:'slider',xAxisIndex:hasA?[0,1]:[0],filterMode:'none',start:dzStart,end:dzEnd,
       bottom:8,height:18,borderColor:c.line,textStyle:{color:c.muted}}
    ]
  };
}
function render(force){
  if(!chart) return;
  const now=performance.now();
  if(!force && now-lastRender<350){ pending=true; return; }
  lastRender=now; pending=false;
  chart.setOption(buildOption(),{notMerge:true,lazyUpdate:true});
  const rows=windowRows();
  $('ptBadge').textContent=rows.length.toLocaleString()+' 点 / 缓冲 '+state.rows.length.toLocaleString();
}
function pick(d,...kws){
  // 先按「去掉单位括号后完全相等」精确匹配，避免 PEEP 命中 Auto-PEEP / Total PEEP
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
  const paw=last&&CM.paw?last[CM.paw.col]:null;
  const vol=last&&CM.vtot?last[CM.vtot.col]:null;
  const vf=state.units.volume==='L'?0.001:1, pf=state.units.pressure==='hPa'?0.980665:1;
  const items=[
    ['气道压 Paw', fmt(paw==null?null:paw*pf,2), state.units.pressure],
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
        CFG.channels.forEach(c=>{ if(c.on && !state.channels.has(c.key) && !CFG.userTouched)
          state.channels.add(c.key); });
        initChips();
      }
      state.meta=j.status;
    }
    const alive=state.status.wave_age_sec!=null && state.status.wave_age_sec<3;
    const badge=$('liveBadge');
    badge.className='badge '+(state.paused?'pause':(alive?'live':''));
    badge.textContent=state.paused?'⏸ 已暂停（数据仍在接收）':(alive?'● 实时':'○ 等待数据');
    // 采样率取 Time (sec) 列中位步长的倒数，与厂商 .rwb 同源；不用接收速率估算
    const hz=state.meta && state.meta.sample_rate_hz;
    $('rateBadge').textContent = alive
      ? ((hz?('采样 '+Math.round(hz)+' Hz'):'采样中')+' · '+state.window+' s 窗口')
      : '等待 6343 广播';
    $('errmsg').textContent=state.status.error||'';
    if(j.rows && j.rows.length) renderCards();
    if(!state.paused) render(false);
  }catch(e){
    $('liveBadge').className='badge pause';
    $('liveBadge').textContent='○ 服务未响应';
    $('errmsg').textContent=String(e);
  }
}
function setPaused(v){
  state.paused=v;
  $('btnPause').textContent=v?'▶ 继续跟随':'⏸ 暂停';
  $('btnPause').classList.toggle('primary',!v);
  const badge=$('liveBadge');
  badge.className='badge '+(v?'pause':'live');
  badge.textContent=v?'⏸ 已暂停（数据仍在接收）':'● 实时';
  if(!v){
    suppressZoom=true;
    chart.dispatchAction({type:'dataZoom',start:0,end:100});
    render(true);
    setTimeout(()=>{ suppressZoom=false; },60);
  }
}
function bind(){
  $('btnPause').onclick=()=>setPaused(!state.paused);
  $('btnTheme').onclick=()=>{
    state.theme=state.theme==='dark'?'light':'dark';
    document.documentElement.dataset.theme=state.theme;
    render(true);
  };
  $('btnCsv').onclick=()=>{ location.href='/api/export?sec='+(state.window||0); };
  $('btnOnlyP').onclick=()=>{
    CFG.userTouched=true;
    state.channels=new Set(CFG.channels.filter(c=>c.family==='pressure').map(c=>c.key));
    syncChips(); render(true);
  };
  $('btnAll').onclick=()=>{
    CFG.userTouched=true;
    state.channels=new Set(CFG.channels.map(c=>c.key));
    syncChips(); render(true);
  };
  $('btnReset').onclick=()=>{
    state.paused=false; setPaused(false);
  };
  document.querySelectorAll('#winSeg button').forEach(b=>b.onclick=()=>{
    state.window=+b.dataset.v;
    document.querySelectorAll('#winSeg button').forEach(x=>x.classList.toggle('on',x===b));
    render(true);
  });
  document.querySelectorAll('#unitP button').forEach(b=>b.onclick=()=>{
    state.units.pressure=b.dataset.v;
    document.querySelectorAll('#unitP button').forEach(x=>x.classList.toggle('on',x===b));
    renderCards(); render(true);
  });
  document.querySelectorAll('#unitV button').forEach(b=>b.onclick=()=>{
    state.units.volume=b.dataset.v;
    document.querySelectorAll('#unitV button').forEach(x=>x.classList.toggle('on',x===b));
    renderCards(); render(true);
  });
  chart.on('dataZoom',()=>{ /* 用户缩放后停止自动跟随，避免视图被拉回 */
    if(!suppressZoom && !state.paused) setPaused(true);
  });
  window.addEventListener('resize',()=>{ if(chart) chart.resize(); });
}
document.addEventListener('DOMContentLoaded',()=>{
  chart=echarts.init($('chart'),null,{renderer:'canvas'});
  initChips(); bind();
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
    cfg = {"channels": [], "ready": False, "signature": "", "userTouched": False,
           "source": source_label}
    html = HTML.replace("__SOURCE__", source_label)
    html = html.replace("/*__ECHARTS__*/", js)
    return html.replace("/*__CONFIG__*/", json.dumps(cfg, ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser(description="ASL 5000 实时波形看板 (ECharts)")
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
