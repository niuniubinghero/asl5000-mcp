#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ASL 5000 实时监控与录波工具 (基于 TAI 广播通道, 无需 TAI Server 授权)

原理 (见 documentation/TAI Specification.pdf):
  ASL5000_SW3.6.exe 运行时向所有连接的 TCP 客户端广播数据:
    - 6342 : 每次呼吸结束推送全部呼吸参数 (ASCII, 行格式 "参数名<TAB>数值")
    - 6343 : 连续波形流 (TAB 分隔, 首行为列名, ~64 Hz)
  每条 TCP 消息前有 4 字节 Big-Endian int32 长度头 (不含头自身)。

子命令:
  check     检测 ASL 广播端口是否在线
  snapshot  一次性抓取当前呼吸参数 + N 秒波形, 输出快照后退出 (适合 AI 单次取数)
  monitor   实时监控 + 录波 (Ctrl-C 或 --dur 结束)

输出文件 (AI 友好, 均为纯文本):
  waveform_001.csv     波形原始数据, 列名与 ASL GUI 导出一致 (--wall-time 可加本机接收时间列)
  breath_params.csv    每次呼吸一行, 全部广播参数
  latest_status.json   持续刷新: 最新呼吸参数 + 最近 N 秒波形统计 (PIP/PEEP/VT/Flow)
  session_summary.json 会话结束时的汇总 (时长/呼吸数/行数/文件清单/最终统计)

示例:
  python -m asl_monitor.monitor check
  python -m asl_monitor.monitor snapshot --wave-sec 10
  python -m asl_monitor.monitor monitor --dur 60
  python -m asl_monitor.monitor monitor --wall-time --split-sec 60 --out ./recordings
"""

from __future__ import annotations

from . import config as _cfg

import argparse
import csv
import json
import socket
import struct
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_BREATH_PORT = 6342
DEFAULT_WAVE_PORT = 6343
HEADER_LEN = 4
MAX_MSG_BYTES = 32 * 1024 * 1024
DEFAULT_OUT = _cfg.recordings_dir()


def now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_str()}] {msg}", flush=True)


# ---------------------------------------------------------------- TCP 广播接收

def recv_exact(sock: socket.socket, n: int, deadline: float) -> bytes:
    buf = b""
    while len(buf) < n:
        remain = deadline - time.time()
        if remain <= 0:
            raise TimeoutError("recv timeout")
        sock.settimeout(remain)
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            raise ConnectionError("ASL server closed the connection")
        buf += chunk
    return buf


def recv_message(sock: socket.socket, idle_timeout: float = 120.0):
    """读一条完整广播消息 (4字节大端长度头 + 正文)。超时返回 None, 断线抛 OSError。"""
    deadline = time.time() + idle_timeout
    try:
        hdr = recv_exact(sock, HEADER_LEN, deadline)
    except TimeoutError:
        return None
    (n,) = struct.unpack(">i", hdr)
    if n <= 0 or n > MAX_MSG_BYTES:
        raise ValueError(f"invalid message length: {n}")
    body = recv_exact(sock, n, time.time() + 120.0)
    return body.decode("ascii", errors="replace")


def parse_breath_params(text: str) -> dict:
    """6342 消息 → {参数名: 数值字符串}"""
    d = {}
    for line in text.splitlines():
        if "\t" not in line:
            continue
        name, _, val = line.partition("\t")
        name = name.strip()
        if name:
            d[name] = val.strip()
    return d


def parse_wave_message(text: str):
    """6343 消息 → (列名列表, 数据行列表)。跳过消息内重复的表头行。"""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None, []
    header = lines[0].split("\t")
    rows = [ln.split("\t") for ln in lines[1:] if ln != lines[0]]
    return header, rows


def col_index(header, *keywords):
    """按小写子串匹配找列下标, 找不到返回 None"""
    if not header:
        return None
    for i, name in enumerate(header):
        low = str(name).lower()
        if all(k.lower() in low for k in keywords):
            return i
    return None


def _floats(rows, idx):
    out = []
    if idx is None:
        return out
    for r in rows:
        if idx < len(r):
            try:
                out.append(float(r[idx]))
            except ValueError:
                pass
    return out


def wave_window_stats(header, rows, window_sec: float) -> dict:
    """对最近 window_sec 秒的波形行做统计 (按 Time 列截取, 计时复位时自动回退)。"""
    if not header or not rows:
        return {}
    rows = rows[-20000:]
    it = col_index(header, "time")
    if it is not None and rows:
        try:
            t_last = float(rows[-1][it])
        except (ValueError, IndexError):
            t_last = None
        if t_last is not None:
            sel = []
            for r in reversed(rows):
                try:
                    t = float(r[it])
                except (ValueError, IndexError):
                    continue
                if t_last - t > 1e6:  # ASL 时钟复位
                    break
                if t_last - t <= window_sec:
                    sel.append(r)
                else:
                    break
            sel.reverse()
            rows = sel or rows[-max(1, int(window_sec * 64)):]
    st = {"samples": len(rows)}
    paw = _floats(rows, col_index(header, "airway pressure"))
    vol = _floats(rows, col_index(header, "total volume"))
    flow = _floats(rows, col_index(header, "total flow"))
    if paw:
        st["paw_max_cmh2o"] = round(max(paw), 3)   # 近似 PIP
        st["paw_min_cmh2o"] = round(min(paw), 3)   # 近似 PEEP
        st["paw_mean_cmh2o"] = round(sum(paw) / len(paw), 3)
    if vol:
        st["vol_max_ml"] = round(max(vol), 2)
        st["vol_min_ml"] = round(min(vol), 2)
        st["vt_est_ml"] = round(max(vol) - min(vol), 2)  # 潮气量估计
    if flow:
        st["flow_max_lpm"] = round(max(flow), 2)
        st["flow_min_lpm"] = round(min(flow), 2)
    if it is not None and rows:
        ts = _floats(rows, it)
        if len(ts) >= 2:
            st["window_sec"] = round(ts[-1] - ts[0], 3)
    return st


def pick(d: dict, *keywords) -> str:
    for k, v in d.items():
        low = k.lower()
        if all(x in low for x in keywords):
            return v
    return ""


def fmt(v) -> str:
    """科学计数法字符串 → 紧凑可读数字"""
    try:
        f = float(v)
        if abs(f) >= 1e6:
            return str(v)
        return f"{f:.4g}"
    except (TypeError, ValueError):
        return str(v)


# ---------------------------------------------------------------- 录波器

class Recorder:
    """会话录波: 波形 CSV (定时分段) + 呼吸参数 CSV (列可动态扩展)"""

    def __init__(self, outdir: Path, split_sec: float = 300.0, wall_time: bool = False):
        self.dir = Path(outdir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.split_sec = max(1.0, split_sec)
        self.wall_time = wall_time
        self.files = []
        self.start_time = time.time()

        self._wf = None
        self._ww = None
        self._wave_idx = 0
        self._wave_header_written = None
        self._wave_opened_at = 0.0
        self.wave_rows_total = 0

        self._bf = None
        self._bw = None
        self._breath_fields = []
        self._breath_data = []  # 内存备份, 供列扩展时重写文件
        self.breath_rows = 0

    # ---- 波形 ----
    def _rotate_wave(self, header):
        if self._wf:
            self._wf.close()
        self._wave_idx += 1
        name = f"waveform_{self._wave_idx:03d}.csv"
        self._wf = open(self.dir / name, "w", newline="", encoding="utf-8-sig")
        self._ww = csv.writer(self._wf)
        self._wave_header_written = list(header)
        if self.wall_time:
            self._ww.writerow(["Recv Time"] + self._wave_header_written)
        else:
            self._ww.writerow(self._wave_header_written)
        self.files.append(str(self.dir / name))
        self._wave_opened_at = time.time()

    def write_wave_rows(self, header, rows):
        if not rows:
            return
        if self._wf is None or (time.time() - self._wave_opened_at) >= self.split_sec:
            self._rotate_wave(header)
        if self.wall_time:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            self._ww.writerows([[ts] + r for r in rows])
        else:
            self._ww.writerows(rows)
        self.wave_rows_total += len(rows)
        self._wf.flush()

    # ---- 呼吸参数 ----
    def _open_breath_file(self):
        name = "breath_params.csv"
        self._bf = open(self.dir / name, "w", newline="", encoding="utf-8-sig")
        self._bw = csv.DictWriter(self._bf, fieldnames=self._breath_fields,
                                  extrasaction="ignore", restval="")
        self._bw.writeheader()
        self.files.append(str(self.dir / name))

    def _rewrite_breath_file(self):
        """出现新参数列时, 用内存备份整体重写 (保证表头完整)"""
        if self._bf:
            self._bf.close()
            self.files = [f for f in self.files if not f.endswith("breath_params.csv")]
        self._bf = None
        self._open_breath_file()
        self._bw.writeheader()
        self._bw.writerows(self._breath_data)
        self._bf.flush()

    def write_breath(self, d: dict):
        new_keys = [k for k in d if k not in self._breath_fields]
        if new_keys:
            self._breath_fields.extend(new_keys)
        if self._bf is None:
            self._open_breath_file()
        elif new_keys:
            self._rewrite_breath_file()
        self._bw.writerow(d)
        self._bf.flush()
        self._breath_data.append(dict(d))
        self.breath_rows += 1

    # ---- 收尾 ----
    def status(self) -> dict:
        return {
            "active": True,
            "outdir": str(self.dir),
            "waveform_rows": self.wave_rows_total,
            "breath_rows": self.breath_rows,
            "files": list(self.files),
        }

    def close(self):
        if self._wf:
            self._wf.close()
            self._wf = None
        if self._bf:
            self._bf.close()
            self._bf = None


# ---------------------------------------------------------------- 通用 I/O

def write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def try_connect(host: str, port: int, timeout: float = 3.0):
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock


# ---------------------------------------------------------------- 子命令: check

def cmd_check(args) -> int:
    ok = True
    for label, port in (("呼吸参数广播", args.breath_port), ("波形广播", args.wave_port)):
        try:
            s = try_connect(args.host, port)
            s.close()
            log(f"{label} {args.host}:{port}  在线")
        except OSError as e:
            ok = False
            log(f"{label} {args.host}:{port}  离线 ({e})")
    if ok:
        log("ASL 软件在运行, 可以开始监控。")
    else:
        log("请确认 ASL5000_SW3.6.exe 已启动 (广播随主软件一起开启)。")
    return 0 if ok else 1


# ---------------------------------------------------------------- 子命令: snapshot

def capture_snapshot(host: str, breath_port: int, wave_port: int,
                     wave_sec: float = 10.0, wait: float = 30.0,
                     outroot=None) -> dict:
    """抓取一次呼吸参数 + wave_sec 秒波形, 保存快照目录, 返回结果 dict (不打印)。

    返回: {"outdir", "breath_params":dict|None, "wave_rows":int, "stats":dict,
           "breath_file":str|None, "wave_file":str|None, "summary_file":str}
    """
    outdir = Path(outroot or DEFAULT_OUT) / f"snapshot_{datetime.now():%Y%m%d_%H%M%S}"
    outdir.mkdir(parents=True, exist_ok=True)

    # 1) 呼吸参数: 读取一条消息
    breath_d = None
    try:
        s = try_connect(host, breath_port)
        msg = recv_message(s, idle_timeout=wait)
        s.close()
        if msg:
            breath_d = parse_breath_params(msg)
    except OSError:
        breath_d = None

    # 2) 波形: 连续采集 wave_sec 秒
    header, rows = None, []
    try:
        s = try_connect(host, wave_port)
        t0 = time.time()
        while time.time() - t0 < wave_sec:
            msg = recv_message(s, idle_timeout=max(5.0, wave_sec))
            if msg is None:
                continue
            h, r = parse_wave_message(msg)
            if r:
                header = header or h
                rows.extend(r)
        s.close()
    except OSError:
        rows = []

    stats = wave_window_stats(header, rows, 1e9)  # 全窗口统计
    summary = {
        "tool": "asl_monitor snapshot",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "host": host,
        "breath_port": breath_port,
        "wave_port": wave_port,
        "breath_params_received": breath_d is not None,
        "waveform_received": bool(rows),
        "waveform_rows": len(rows),
        "waveform_stats_full_window": stats,
    }
    breath_file = wave_file = None
    if breath_d is not None:
        breath_file = str(outdir / "breath_params.json")
        write_json(outdir / "breath_params.json", breath_d)
    if rows and header:
        wave_file = str(outdir / "waveform.csv")
        with open(outdir / "waveform.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
    summary_file = str(outdir / "snapshot_summary.json")
    write_json(outdir / "snapshot_summary.json", summary)
    return {
        "outdir": str(outdir),
        "breath_params": breath_d,
        "wave_rows": len(rows),
        "stats": stats,
        "breath_file": breath_file,
        "wave_file": wave_file,
        "summary_file": summary_file,
    }


def cmd_snapshot(args) -> int:
    r = capture_snapshot(args.host, args.breath_port, args.wave_port,
                         wave_sec=args.wave_sec, wait=args.wait, outroot=args.out)
    log(f"快照已保存: {r['outdir']}")
    if r["breath_params"]:
        d = r["breath_params"]
        log(f"  呼吸 #{fmt(pick(d, 'breath number'))}  "
            f"RR {fmt(pick(d, 'breath rate'))}  类型 {pick(d, 'breath type')}")
    if r["stats"]:
        st = r["stats"]
        log(f"  波形 {r['wave_rows']} 行 | PIP~{st.get('paw_max_cmh2o', '?')} | "
            f"PEEP~{st.get('paw_min_cmh2o', '?')} | VT~{st.get('vt_est_ml', '?')} mL | "
            f"Flow {st.get('flow_min_lpm', '?')}~{st.get('flow_max_lpm', '?')} L/min")
    return 0


# ---------------------------------------------------------------- 子命令: monitor

def broadcast_worker(name, host, port, handler, state, stop: threading.Event):
    """单通道后台线程: 自动重连, 每条消息交给 handler"""
    state[name + "_conn"] = False
    while not stop.is_set():
        sock = None
        try:
            sock = try_connect(host, port)
            state[name + "_conn"] = True
            log(f"{name} 已连接 {host}:{port}")
            while not stop.is_set():
                msg = recv_message(sock, idle_timeout=120.0)
                if msg is None:
                    continue  # 仿真暂停时长时间无数据, 保持连接等待
                handler(msg)
        except OSError as e:
            if not stop.is_set():
                state[name + "_conn"] = False
                log(f"{name} 连接断开: {e}, 2 秒后重连")
                stop.wait(2.0)
        finally:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
    state[name + "_conn"] = False


def cmd_monitor(args) -> int:
    outdir = Path(args.out) / f"session_{datetime.now():%Y%m%d_%H%M%S}"
    recorder = Recorder(outdir, split_sec=args.split_sec, wall_time=args.wall_time)
    log(f"输出目录: {outdir}")

    stop = threading.Event()
    lock = threading.Lock()
    state = {
        "last_breath": {},
        "last_breath_time": 0.0,
        "last_wave_time": 0.0,
        "breath_count": 0,
        "wave_msg_count": 0,
        "wave_header": None,
        "recent": deque(maxlen=8192),
    }

    def breath_handler(msg):
        d = parse_breath_params(msg)
        if not d:
            return
        with lock:
            state["last_breath"] = d
            state["last_breath_time"] = time.time()
            state["breath_count"] += 1
        recorder.write_breath(d)
        if not args.quiet:
            log(f"呼吸 #{fmt(pick(d, 'breath number'))} | RR {fmt(pick(d, 'breath rate'))} | "
                f"类型 {pick(d, 'breath type')} | 已录波形 {recorder.wave_rows_total} 行")

    def wave_handler(msg):
        header, rows = parse_wave_message(msg)
        if not rows:
            return
        with lock:
            state["wave_header"] = header
            state["recent"].extend(rows)
            state["last_wave_time"] = time.time()
            state["wave_msg_count"] += 1
        recorder.write_wave_rows(header, rows)

    threads = [
        threading.Thread(target=broadcast_worker,
                         args=("呼吸参数", args.host, args.breath_port, breath_handler, state, stop),
                         daemon=True),
        threading.Thread(target=broadcast_worker,
                         args=("波形", args.host, args.wave_port, wave_handler, state, stop),
                         daemon=True),
    ]
    for t in threads:
        t.start()

    t0 = time.time()
    last_stats_print = 0.0
    try:
        while True:
            time.sleep(max(0.5, args.status_interval))
            elapsed = time.time() - t0
            with lock:
                header = state["wave_header"]
                recent = list(state["recent"])
                last_breath = dict(state["last_breath"])
                last_breath_age = round(time.time() - state["last_breath_time"], 1) if state["last_breath_time"] else None
                last_wave_age = round(time.time() - state["last_wave_time"], 1) if state["last_wave_time"] else None
                breath_count = state["breath_count"]
                wave_msg_count = state["wave_msg_count"]
            stats = wave_window_stats(header, recent, args.stats_window)
            payload = {
                "tool": "asl_monitor",
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "elapsed_sec": round(elapsed, 1),
                "connections": {
                    "breath": {"port": args.breath_port, "connected": any(t.is_alive() for t in threads[:1]),
                               "last_msg_age_sec": last_breath_age},
                    "waveform": {"port": args.wave_port, "connected": any(t.is_alive() for t in threads[1:]),
                                 "last_msg_age_sec": last_wave_age},
                },
                "breath_count": breath_count,
                "wave_message_count": wave_msg_count,
                "last_breath_params": last_breath,
                f"waveform_stats_last_{int(args.stats_window)}s": stats,
                "recording": recorder.status(),
            }
            write_json(outdir / "latest_status.json", payload)

            if not args.quiet and stats and elapsed - last_stats_print >= 10.0:
                last_stats_print = elapsed
                log(f"近 {stats.get('window_sec', args.stats_window)}s: "
                    f"PIP~{stats.get('paw_max_cmh2o', '?')} | PEEP~{stats.get('paw_min_cmh2o', '?')} | "
                    f"VT~{stats.get('vt_est_ml', '?')} mL | "
                    f"Flow {stats.get('flow_min_lpm', '?')}~{stats.get('flow_max_lpm', '?')} L/min")

            if args.dur and elapsed >= args.dur:
                break
    except KeyboardInterrupt:
        log("收到 Ctrl-C, 正在停止...")
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=3.0)
        recorder.close()
        with lock:
            final_stats = wave_window_stats(state["wave_header"], list(state["recent"]), args.stats_window)
        summary = {
            "tool": "asl_monitor",
            "session_dir": str(outdir),
            "started_at": datetime.fromtimestamp(t0).isoformat(timespec="seconds"),
            "ended_at": datetime.now().isoformat(timespec="seconds"),
            "duration_sec": round(time.time() - t0, 1),
            "breath_count": recorder.breath_rows,
            "waveform_rows": recorder.wave_rows_total,
            "files": recorder.files,
            "final_waveform_stats": final_stats,
        }
        write_json(outdir / "session_summary.json", summary)
        log(f"录制完成: {recorder.breath_rows} 次呼吸, {recorder.wave_rows_total} 行波形, "
            f"输出在 {outdir}")
    return 0


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="asl_monitor",
        description="ASL 5000 实时监控与录波工具 (TAI 广播通道 6342/6343)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python -m asl_monitor.monitor check\n"
               "  python -m asl_monitor.monitor snapshot --wave-sec 10\n"
               "  python -m asl_monitor.monitor monitor --dur 60\n"
               "  python -m asl_monitor.monitor monitor --wall-time --split-sec 60 --out ./recordings\n",
    )
    p.add_argument("--host", default=DEFAULT_HOST, help=f"ASL 软件所在主机 (默认 {DEFAULT_HOST})")
    p.add_argument("--breath-port", type=int, default=DEFAULT_BREATH_PORT)
    p.add_argument("--wave-port", type=int, default=DEFAULT_WAVE_PORT)
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("check", help="检测 ASL 广播端口是否在线")

    ps = sub.add_parser("snapshot", help="抓取当前呼吸参数 + N 秒波形后退出 (适合 AI 单次取数)")
    ps.add_argument("--wave-sec", type=float, default=10.0, help="波形采集时长秒 (默认 10)")
    ps.add_argument("--wait", type=float, default=30.0, help="等待呼吸参数的超时秒数")
    ps.add_argument("--out", default=str(DEFAULT_OUT), help="输出根目录")

    pm = sub.add_parser("monitor", help="实时监控 + 录波 (Ctrl-C 或 --dur 结束)")
    pm.add_argument("--out", default=str(DEFAULT_OUT), help="输出根目录")
    pm.add_argument("--dur", type=float, default=0, help="录制时长秒, 0 = 直到 Ctrl-C")
    pm.add_argument("--split-sec", type=float, default=300.0, help="波形文件分段时长秒 (默认 300)")
    pm.add_argument("--status-interval", type=float, default=2.0, help="latest_status.json 刷新间隔秒")
    pm.add_argument("--stats-window", type=float, default=5.0, help="滚动波形统计窗口秒")
    pm.add_argument("--wall-time", action="store_true", help="波形 CSV 增加本机接收时间列")
    pm.add_argument("--quiet", action="store_true", help="不打印逐呼吸控制台信息")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "snapshot":
        return cmd_snapshot(args)
    if args.cmd == "monitor":
        return cmd_monitor(args)
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", flush=True)
        sys.exit(130)
