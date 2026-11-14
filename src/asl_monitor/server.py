#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ASL 5000 模拟肺通用 MCP Server (监控 + TAI 控制) —— 客户端无关, 可挂到任何 MCP 宿主。

适用于: CodeBuddy / WorkBuddy / Claude Desktop / CodeX / Trae / Cursor / Qoder 等
包内模块: monitor.py (广播接收) / tai.py (TAI 控制) / config.py (路径发现)。
所有路径来自环境变量、用户配置或自动探测, 不含机器相关硬编码:
  ASL_TAI_EXE    TAI server exe (optional; auto-discovered or via asl_set_paths)
  ASL_SW_PATH    ASL software exe (optional; auto-discovered or via asl_set_paths)
  ASL_TAI_HOST / ASL_TAI_PORT   控制通道地址 (默认 127.0.0.1:6341)

依赖: Python >= 3.10, mcp>=1.2 (随包安装)
运行: 由 MCP 客户端以 stdio 方式拉起, 无需手动运行。
各客户端配置见 configs/ 与 docs/INSTALL.md; 也可用 asl-monitor install 生成。

工具列表 (22 个):
  -- 接入自检与部署 --
  asl_env_check(detail_dir)         【会话首次必调】环境就绪度检测 + COM口清单 + 脚本库 + 下一步指引
  asl_set_paths(sw_path, tai_exe)   检测不到 ASL 时, 保存用户提供的路径 (立即生效, 免重启)
  -- 监控 (广播通道 6342/6343, 被动) --
  asl_check()                       检测 ASL 广播端口是否在线
  asl_get_status()                  当前呼吸参数 + 最近5秒波形统计 (PIP/PEEP/VT/Flow)
  asl_snapshot(wave_sec)            抓快照存盘, 返回摘要与文件路径
  asl_start_recording(...)          开始后台录波 (生成 CSV + latest_status.json)
  asl_stop_recording()              停止录波, 返回会话汇总
  asl_list_recordings()             列出历史录波会话
  -- 厂商原始数据预检 (只读, 无硬件依赖) --
  asl_prepare_recording(...)        【asl_run 之前必调】推荐名 + 同名冲突 + 覆盖/改名/_N 候选 + 目录选项
  -- 控制 (TAI 命令通道 6341, 主动, 见 tai.py) --
  asl_tai_start()                   确认/拉起 TAI 服务器 (ASL_TAI.exe)
  asl_connect(id, path, gui)        打开 ASL 软件实例 (OA): id=COM1-8/DEMO/序列号
  asl_load_script(name, path)       加载仿真脚本 .sct (SF)
  asl_run(name, wf, ow, path)       启动仿真 (RS), 默认 wf=1 保留厂商原始数据组
  asl_stop()                        结束仿真 (ES)
  asl_exit()                        退出 ASL 软件 (EX)
  asl_tai_status()                  查询仿真状态/版本/呼吸计数 (TC)
  asl_query_breath_params(...)      逐口呼吸分析参数 (BP, 80+ 项可选)
  asl_export_waveform(...)          导出某口呼吸完整波形为 tab 分隔文件 (BW), 默认跟随上次 run 的 NAME
  asl_init(...)                     仿真初始化 FRC/管路补偿 (SI)
  asl_aux(...)                      辅助回路阻抗/顺应性 (AC)
  asl_set_interactive(on)           进入/退出交互模式 (IM/SM)
  asl_set(params)                   仿真运行中实时改肺模型/呼吸用力/闭环 (IC)
"""

from __future__ import annotations

import json
import re
import socket
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from . import config as cfg
from . import monitor as am
from . import recording as rec
from . import tai as tc

# Log to stderr: stdout is reserved for the MCP stdio protocol.
am.log = lambda msg: print(f"[asl_monitor] {msg}", file=sys.stderr, flush=True)

from mcp.server.fastmcp import FastMCP  # noqa: E402

RECORDINGS_ROOT = cfg.recordings_dir()
STATS_WINDOW_SEC = 5.0

mcp = FastMCP(
    "asl-monitor",
    instructions=(
        "ASL 5000 模拟肺: 环境自检 + 实时监控 + TAI 主动控制。"
        "每个会话首次接入时必须先调用 asl_env_check 检测环境并获取下一步指引。"
        "典型自动化测试流程: "
        "1) asl_tai_start 确认 TAI 服务器在线 → "
        "2) asl_connect(id=<COM 口>) 打开 ASL 软件 (先 asl_env_check 查 COM 口; 无硬件时 id='DEMO') → "
        "3) asl_load_script 加载脚本 → "
        "4) 【必须】asl_prepare_recording 只读预检, 然后用提问工具让用户决定: "
        "录制数据文件名 (推荐名/自定义)、保存目录 (默认 \\ASLDATA/自定义)、"
        "同名已存在时 (覆盖/改名/末尾加 _1), 把用户选择原样传给 asl_run → "
        "5) asl_run(wf=1) 启动仿真, 默认保留厂商原始留存数据组 → "
        "6) 用监控工具 (asl_get_status/asl_snapshot/asl_start_recording) 收数据, "
        "或 asl_query_breath_params 取逐口分析参数 → "
        "7) asl_set_interactive(True) + asl_set 可在仿真运行中实时改肺模型/用力/闭环参数 → "
        "8) asl_stop / asl_exit 收尾。"
        "不许自行决定覆盖或改名, 不许跳过询问直接 asl_run; "
        "厂商原始数据由 ASL 软件写在 <ASL_ROOT>\\ASLdata\\<NAME>.* , 与广播 CSV 是两套独立数据。"
        "控制命令同步执行, 必须等上一条响应; 广播端口随 ASL 软件自动开启, 与控制通道互不依赖。"
    ),
)

# ---- 后台录波会话状态 (MCP server 进程内) ----
_rec_lock = threading.Lock()
_rec = {
    "stop": None,        # threading.Event
    "thread": None,      # 录波线程
    "outdir": None,
    "error": None,
}

# ---- 厂商录制数据的会话状态 (MCP server 进程内) ----
# script: 最近一次成功加载的脚本名 (用于生成推荐名)
# run_name: 最近一次成功启动仿真的 RS NAME (BW 导出默认跟随它)
_session = {"script": "", "run_name": ""}


def _j(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 录波线程

def _record_worker(outdir: Path, split_sec: float, wall_time: bool,
                   stop: threading.Event, host: str, breath_port: int, wave_port: int):
    recorder = am.Recorder(outdir, split_sec=split_sec, wall_time=wall_time)
    lock = threading.Lock()
    state = {
        "wave_header": None,
        "recent": deque(maxlen=8192),
        "last_breath": {},
        "last_breath_time": 0.0,
        "last_wave_time": 0.0,
        "breath_count": 0,
        "wave_msg_count": 0,
    }

    def breath_handler(msg):
        d = am.parse_breath_params(msg)
        if not d:
            return
        with lock:
            state["last_breath"] = d
            state["last_breath_time"] = time.time()
            state["breath_count"] += 1
        recorder.write_breath(d)

    def wave_handler(msg):
        header, rows = am.parse_wave_message(msg)
        if not rows:
            return
        with lock:
            state["wave_header"] = header
            state["recent"].extend(rows)
            state["last_wave_time"] = time.time()
            state["wave_msg_count"] += 1
        recorder.write_wave_rows(header, rows)

    def make_thread(name, port, handler):
        return threading.Thread(target=am.broadcast_worker,
                                args=(name, host, port, handler, state, stop),
                                daemon=True)

    threads = [
        make_thread("呼吸参数", breath_port, breath_handler),
        make_thread("波形", wave_port, wave_handler),
    ]
    for t in threads:
        t.start()

    t0 = time.time()
    try:
        while not stop.is_set():
            time.sleep(2.0)
            with lock:
                header = state["wave_header"]
                recent = list(state["recent"])
                last_breath = dict(state["last_breath"])
                lb_age = round(time.time() - state["last_breath_time"], 1) if state["last_breath_time"] else None
                lw_age = round(time.time() - state["last_wave_time"], 1) if state["last_wave_time"] else None
                b_cnt, w_cnt = state["breath_count"], state["wave_msg_count"]
            stats = am.wave_window_stats(header, recent, STATS_WINDOW_SEC)
            payload = {
                "tool": "asl-monitor",
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "elapsed_sec": round(time.time() - t0, 1),
                "connections": {
                    "breath": {"port": breath_port, "connected": threads[0].is_alive(),
                               "last_msg_age_sec": lb_age},
                    "waveform": {"port": wave_port, "connected": threads[1].is_alive(),
                                 "last_msg_age_sec": lw_age},
                },
                "breath_count": b_cnt,
                "wave_message_count": w_cnt,
                "last_breath_params": last_breath,
                f"waveform_stats_last_{int(STATS_WINDOW_SEC)}s": stats,
                "recording": recorder.status(),
            }
            am.write_json(outdir / "latest_status.json", payload)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=3.0)
        recorder.close()
        with lock:
            final_stats = am.wave_window_stats(state["wave_header"],
                                               list(state["recent"]), STATS_WINDOW_SEC)
        summary = {
            "tool": "asl-monitor",
            "session_dir": str(outdir),
            "started_at": datetime.fromtimestamp(t0).isoformat(timespec="seconds"),
            "ended_at": datetime.now().isoformat(timespec="seconds"),
            "duration_sec": round(time.time() - t0, 1),
            "breath_count": recorder.breath_rows,
            "waveform_rows": recorder.wave_rows_total,
            "files": recorder.files,
            "final_waveform_stats": final_stats,
        }
        am.write_json(outdir / "session_summary.json", summary)


# ---------------------------------------------------------------- 现场抓取(非录波时)

def _live_grab(host: str, breath_port: int, wave_port: int,
               wave_sec: float = 3.0, wait: float = 12.0) -> dict:
    """不落盘, 现场抓一条呼吸参数 + wave_sec 秒波形并返回统计。"""
    breath_d = None
    try:
        s = am.try_connect(host, breath_port)
        msg = am.recv_message(s, idle_timeout=wait)
        s.close()
        if msg:
            breath_d = am.parse_breath_params(msg)
    except OSError:
        breath_d = None

    header, rows = None, []
    try:
        s = am.try_connect(host, wave_port)
        t0 = time.time()
        while time.time() - t0 < wave_sec:
            msg = am.recv_message(s, idle_timeout=max(5.0, wave_sec))
            if msg is None:
                continue
            h, r = am.parse_wave_message(msg)
            if r:
                header = header or h
                rows.extend(r)
        s.close()
    except OSError:
        rows = []
    return {
        "breath_params": breath_d,
        "wave_rows": len(rows),
        "stats": am.wave_window_stats(header, rows, 1e9),
    }


# ---------------------------------------------------------------- MCP 工具

@mcp.tool()
def asl_check() -> str:
    """检测 ASL 5000 广播端口(6342呼吸参数/6343波形)是否在线。调试前先调用确认模拟器软件已运行。"""
    result = {}
    for name, port in (("breath_params", am.DEFAULT_BREATH_PORT),
                       ("waveform", am.DEFAULT_WAVE_PORT)):
        try:
            s = am.try_connect(am.DEFAULT_HOST, port)
            s.close()
            result[name] = {"port": port, "online": True}
        except OSError as e:
            result[name] = {"port": port, "online": False, "error": str(e)}
    result["ready"] = result["breath_params"]["online"] and result["waveform"]["online"]
    if not result["ready"]:
        result["hint"] = "请确认 ASL5000_SW3.6.exe 已启动 (广播随主软件自动开启)"
    return _j(result)


@mcp.tool()
def asl_get_status() -> str:
    """获取 ASL 当前实时状态: 最新一次呼吸的全部参数 + 最近5秒波形统计(PIP/PEEP/VT/Flow)。
    正在录波时直接读 latest_status.json; 否则现场抓取(约需3-15秒)。AI 每轮调参迭代后调用此工具对比效果。"""
    with _rec_lock:
        active = _rec["thread"] is not None and _rec["thread"].is_alive()
        outdir = _rec["outdir"]
    if active and outdir:
        f = outdir / "latest_status.json"
        for _ in range(10):
            if f.exists():
                try:
                    return f.read_text(encoding="utf-8")
                except OSError:
                    pass
            time.sleep(0.3)
    r = _live_grab(am.DEFAULT_HOST, am.DEFAULT_BREATH_PORT, am.DEFAULT_WAVE_PORT)
    return _j({
        "mode": "live_grab",
        "breath_params": r["breath_params"],
        "wave_rows": r["wave_rows"],
        "waveform_stats": r["stats"],
        "recording_active": False,
    })


@mcp.tool()
def asl_snapshot(wave_sec: float = 10.0) -> str:
    """抓取当前呼吸参数 + wave_sec 秒原始波形, 保存到 recordings/snapshot_* 目录后返回摘要与文件路径。
    适合调参前后各抓一份做对比。wave_sec 建议 10-30 秒(至少覆盖2次完整呼吸)。"""
    wave_sec = min(max(2.0, float(wave_sec)), 120.0)
    r = am.capture_snapshot(am.DEFAULT_HOST, am.DEFAULT_BREATH_PORT, am.DEFAULT_WAVE_PORT,
                            wave_sec=wave_sec, wait=30.0, outroot=RECORDINGS_ROOT)
    d = r["breath_params"] or {}
    return _j({
        "outdir": r["outdir"],
        "breath_number": am.fmt(am.pick(d, "breath number")) if d else None,
        "breath_type": am.pick(d, "breath type") if d else None,
        "breath_rate_bpm": am.fmt(am.pick(d, "breath rate")) if d else None,
        "wave_rows": r["wave_rows"],
        "waveform_stats_full_window": r["stats"],
        "files": {"breath_params": r["breath_file"], "waveform": r["wave_file"],
                  "summary": r["summary_file"]},
    })


@mcp.tool()
def asl_start_recording(split_sec: float = 300.0, wall_time: bool = True) -> str:
    """开始后台录波会话: 持续写波形CSV(每split_sec秒分段)、逐呼吸参数CSV、latest_status.json。
    同一时间只允许一个会话; 用 asl_stop_recording 结束。wall_time=True 时波形含本机接收时间列, 便于与 FreeMaster 日志对齐。"""
    with _rec_lock:
        if _rec["thread"] is not None and _rec["thread"].is_alive():
            return _j({"ok": False, "error": "已有录波会话在运行",
                       "outdir": str(_rec["outdir"]),
                       "hint": "先调用 asl_stop_recording, 或直接用 asl_get_status 读取实时状态"})
        outdir = RECORDINGS_ROOT / f"session_{datetime.now():%Y%m%d_%H%M%S}"
        stop = threading.Event()
        t = threading.Thread(target=_record_worker,
                             args=(outdir, max(1.0, float(split_sec)), bool(wall_time),
                                   stop, am.DEFAULT_HOST, am.DEFAULT_BREATH_PORT, am.DEFAULT_WAVE_PORT),
                             daemon=True)
        t.start()
        _rec["stop"] = stop
        _rec["thread"] = t
        _rec["outdir"] = outdir
        _rec["error"] = None
    return _j({"ok": True, "outdir": str(outdir),
               "hint": "录波已在后台运行; asl_get_status 可随时读取实时状态; asl_stop_recording 结束"})


@mcp.tool()
def asl_stop_recording() -> str:
    """停止后台录波会话, 返回会话汇总(时长/呼吸数/波形行数/文件清单/最终统计)。"""
    with _rec_lock:
        t = _rec["thread"]
        stop = _rec["stop"]
        outdir = _rec["outdir"]
        if t is None or not t.is_alive():
            _rec["thread"] = None
            return _j({"ok": False, "error": "当前没有正在运行的录波会话"})
        stop.set()
        t.join(timeout=10.0)
        _rec["thread"] = None
    summary_file = Path(str(outdir)) / "session_summary.json"
    if summary_file.exists():
        try:
            return summary_file.read_text(encoding="utf-8")
        except OSError:
            pass
    return _j({"ok": True, "outdir": str(outdir), "summary": None})


@mcp.tool()
def asl_list_recordings() -> str:
    """列出 recordings/ 下全部快照与录波会话及其摘要(时长/呼吸数/行数/最终统计)。"""
    items = []
    if RECORDINGS_ROOT.exists():
        for d in sorted(RECORDINGS_ROOT.iterdir()):
            if not d.is_dir():
                continue
            item = {"dir": str(d), "name": d.name}
            for fname in ("session_summary.json", "snapshot_summary.json"):
                f = d / fname
                if f.exists():
                    try:
                        item["summary"] = json.loads(f.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        pass
                    break
            files = [str(p.name) for p in d.iterdir() if p.is_file()]
            item["files"] = files
            items.append(item)
    return _j({"root": str(RECORDINGS_ROOT), "count": len(items), "items": items})


# ---------------------------------------------------------------- 厂商原始数据预检 (只读)

@mcp.tool()
def asl_prepare_recording(name: str = "", path: str = "", script: str = "") -> str:
    """【每次 asl_run 之前必调, 纯只读, 不发 TAI 命令/不写文件】为厂商原始留存数据做预检。

    返回 recommended_name (推荐名)、resolved_dir、conflict (是否已有同名数据组)、
    existing_group (旧文件清单/大小/是否只有空头)、conflict_options 与 default_run_kwargs。
    拿到结果后必须用提问工具让用户决定三件事, 不许自行决定:
      1) 录制数据文件名: 用推荐名, 还是自定义 (用户给出名字后带 name= 再调本工具重新预检);
      2) 保存目录: 默认 \\ASLDATA, 还是自定义 (用户给出路径后带 path= 再调本工具重新预检);
      3) 若 conflict=true: 覆盖 / 改名 / 末尾加 _1 (conflict_options 里有各选项对应的 run_kwargs)。
    最后把用户选中的 run_kwargs 原样传给 asl_run。script 留空时自动用最近加载的脚本名生成推荐名。
    wf 恒为 1: 厂商 RS 的 WF 默认是 0 (不保存波形), 本包默认保留厂商原始数据组。
    """
    return _j(rec.prepare(name=name, script=script.strip() or _session["script"], path=path))


# ================================================================ 环境自检 (接入即初始化)

def _list_com_ports() -> list[dict]:
    """用 winreg 读 HKLM\\HARDWARE\\DEVICEMAP\\SERIALCOMM 枚举本机串口, 标记 TAI 可用的 (COM1-8)。"""
    ports: list[dict] = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DEVICEMAP\SERIALCOMM") as key:
            i = 0
            while True:
                try:
                    name, val, _ = winreg.EnumValue(key, i)
                    i += 1
                except OSError:
                    break
                val = str(val).upper()
                ports.append({"device": str(name), "port": val,
                              "tai_usable": bool(re.fullmatch(r"COM[1-8]", val))})
    except (OSError, ImportError) as e:
        return [{"error": f"串口枚举失败: {e}", "tai_usable": False}]
    return sorted(ports, key=lambda p: p["port"])


def _script_inventory(detail_dir: str = "") -> dict:
    """扫描 ASL 目录 vars\\ 下的仿真脚本 (.sct)。detail_dir 非空时返回该子目录的完整清单。"""
    import fnmatch
    vd = cfg.vars_dir()
    inv: dict = {"root": str(vd) if vd else None,
                 "exists": bool(vd and vd.exists()), "dirs": {}, "root_scripts": []}
    if not inv["exists"]:
        return inv
    root = vd
    inv["root_scripts"] = [p.stem for p in sorted(root.glob("*.sct"))]
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        scts = sorted(d.rglob("*.sct"))
        if scts:
            if detail_dir and fnmatch.fnmatch(d.name.lower(), detail_dir.lower()):
                inv["dirs"][d.name] = {
                    "count": len(scts),
                    "scripts": [str(s.relative_to(d)) for s in scts]}
            else:
                inv["dirs"][d.name] = {"count": len(scts)}
    return inv


@mcp.tool()
def asl_env_check(detail_dir: str = "") -> str:
    """【每个会话首次接入时调用】检测本机 ASL 模拟肺环境就绪度并给出下一步指引:
    ASL 软件与 TAI 服务器是否安装、控制/广播端口是否在线、本机 COM 口清单 (标注 TAI 可用的 COM1-8)、
    vars\\ 脚本库清单。detail_dir 可传 vars\\ 下子目录名 (如 'scenarios*') 以列出该目录全部脚本名。
    全部 PASS 后按指引执行: asl_tai_start → asl_connect → asl_load_script → asl_run。"""
    import re as _re
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    sw_p, tai_p = cfg.sw_path(), cfg.tai_exe()
    sw = Path(sw_p) if sw_p else None
    tai = Path(tai_p) if tai_p else None
    c = tc.get_client()

    checks = {
        "asl_software_installed": bool(sw and sw.exists()),
        "tai_server_installed": bool(tai and tai.exists()),
        "tai_port_online": c.server_online(),
        "recordings_dir_writable": True,
    }
    try:
        RECORDINGS_ROOT.mkdir(parents=True, exist_ok=True)
        (RECORDINGS_ROOT / ".write_test").write_text("ok", encoding="ascii")
        (RECORDINGS_ROOT / ".write_test").unlink()
    except OSError:
        checks["recordings_dir_writable"] = False
    for name, port in (("broadcast_breath_6342", 6342), ("broadcast_wave_6343", 6343)):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.close()
            checks[name] = True
        except OSError:
            checks[name] = False

    com_ports = _list_com_ports()
    scripts = _script_inventory(detail_dir)

    # 就绪度与下一步指引
    steps = []
    if not checks["asl_software_installed"]:
        steps.append(f"未找到 ASL 软件 ({sw}): 【询问用户】ASL5000_SW3.6.exe 的实际路径, "
                     "然后用 asl_set_paths 保存 (无需重启客户端)")
    if not checks["tai_server_installed"]:
        steps.append(f"未找到 TAI 服务器 ({tai}): 【询问用户】ASL_TAI.exe 的实际路径, "
                     "然后用 asl_set_paths 保存")
    if checks["asl_software_installed"] and checks["tai_server_installed"]:
        steps.append("asl_tai_start 拉起 TAI 服务器")
        usable = [p["port"] for p in com_ports if p.get("tai_usable")]
        steps.append(f"asl_connect 连接模拟肺: 串口设备用其 COM 口 (本机 TAI 可用: {usable or '无 COM1-8, 需在设备管理器改口号'}); 无硬件演示用 id='DEMO'")
        steps.append("asl_load_script 加载 vars\\ 下的 .sct 脚本 → asl_run 启动仿真")
    if not (checks["broadcast_breath_6342"] and checks["broadcast_wave_6343"]):
        steps.append("广播端口未在线 = ASL 软件未运行, 连接后自动开启, 非故障")
    ready = checks["asl_software_installed"] and checks["tai_server_installed"] \
        and checks["recordings_dir_writable"]
    return _j({"ok": True, "ready": ready, "checks": checks,
               "python": py_ver, "com_ports": com_ports,
               "tai_com_ports_only": [p["port"] for p in com_ports if p.get("tai_usable")],
               "script_library": scripts, "next_steps": steps})


@mcp.tool()
def asl_set_paths(sw_path: str = "", tai_exe: str = "") -> str:
    """保存 ASL 软件/TAI 服务器路径 (asl_env_check 检测不到时, 向用户询问路径后调用本工具)。
    路径写入 mcp\\config.json 并立即生效, 无需重启客户端或 MCP。
    sw_path=ASL5000_SW3.6.exe 完整路径; tai_exe=ASL_TAI.exe 完整路径; 至少提供一个。"""
    if not sw_path and not tai_exe:
        return _j({"ok": False,
                   "error": "至少提供 sw_path 或 tai_exe 之一",
                   "example": {"sw_path": "<ASL_ROOT>/ASL5000_SW3.6.exe"}})
    problems = []
    if sw_path and not Path(sw_path).exists():
        problems.append(f"sw_path 不存在: {sw_path} —— 请与用户确认后重试")
    if tai_exe and not Path(tai_exe).exists():
        problems.append(f"tai_exe 不存在: {tai_exe} —— 请与用户确认后重试")
    if problems:
        return _j({"ok": False, "error": "; ".join(problems)})
    result = tc.save_paths(sw_path or None, tai_exe or None)
    return _j({"ok": True, "saved": result,
               "next": "重新调用 asl_env_check 确认 ready=true"})




def _tai_run(cmd: str, to_ms: int = 10000) -> str:
    """执行 TAI 命令并把结果包装成 JSON; TAIError/超时也返回 JSON (AI 可读)。"""
    try:
        lines = tc.get_client().command(cmd, to_ms=to_ms)
        return _j({"ok": True, "cmd": cmd, "data": lines})
    except tc.TAIError as e:
        return _j({"ok": False, "cmd": cmd, "error": str(e), "code": e.code})
    except (TimeoutError, OSError) as e:
        return _j({"ok": False, "cmd": cmd, "error": f"{type(e).__name__}: {e}"})


@mcp.tool()
def asl_tai_start() -> str:
    """确认 TAI 控制服务器 (ASL_TAI.exe, TCP 6341) 是否在线; 不在线则自动拉起并等待就绪。
    任何 TAI 控制 (连接/选脚本/跑仿真) 之前先调用此工具。不影响广播监控通道。"""
    c = tc.get_client()
    was_online = c.server_online()
    try:
        c.ensure_server()
    except tc.TAIError as e:
        return _j({"ok": False, "error": str(e)})
    return _j({"ok": True, "host": c.host, "port": c.port,
               "started_now": not was_online,
               "exe": c.tai_exe,
               "hint": "TAI 服务器就绪; 下一步 asl_connect 打开 ASL 软件实例"})


@mcp.tool()
def asl_connect(id: str = "", path: str = "", gui: int = 0) -> str:
    """打开 ASL 5000 软件实例 (TAI OA 命令)。串口连模拟肺时 id=所在 COM 口 (仅支持 COM1-COM8);
    无硬件演示用 id='DEMO'; 网口用设备序列号。path 为 ASL 软件 exe 路径 (默认已配好)。
    gui=0 隐藏 ASL 界面 (推荐, 防误操作); gui=1 显示。耗时可达 30 秒。
    注意: 若已手动打开 ASL 软件占用该 COM 口会报 ERROR(03), 请先关闭手动实例。"""
    if not id:
        return _j({"ok": False, "error": "未提供 id",
                   "hint": "先调用 asl_env_check 查看可用 COM 口, 再传 id='COM1'..'COM8'; 无硬件用 id='DEMO'"})
    args = [f"id={id}"]
    if path:
        args.append(f'path="{path}"')
    else:
        sw = cfg.sw_path()
        if not sw:
            return _j({"ok": False, "error": "未找到 ASL 软件路径",
                       "hint": "用 asl_set_paths 提供 ASL5000 软件 exe 的完整路径"})
        args.append(f'path="{sw}"')
    args.append(f"gui={gui}")
    return _tai_run("oa " + " ".join(args), to_ms=90000)


@mcp.tool()
def asl_load_script(name: str, path: str = "") -> str:
    """加载仿真脚本文件 .sct (TAI SF 命令)。name 为脚本名 (可不含扩展名);
    path 为脚本目录 (相对 ASL 路径需以 \\ 开头, 默认 \\VARS)。"""
    args = [f"name={name}"]
    if path:
        args.append(f"path={path}")
    out = _tai_run("sf " + " ".join(args))
    try:
        if json.loads(out).get("ok"):
            _session["script"] = name
    except json.JSONDecodeError:
        pass
    return out


@mcp.tool()
def asl_run(name: str = "data", wf: int = 1, ow: int = 0, path: str = "") -> str:
    """启动仿真 (TAI RS 命令) 并保留厂商原始留存数据组。

    wf=1 (默认): 保存波形。厂商 RS 的 WF 默认为 0 (不保存), 本包默认强制保留。
    name: 录制数据文件名 (不带扩展名), 由 asl_prepare_recording 预检 + 用户确认后传入。
    ow=0 (默认): 不覆盖。同名数据组已存在时直接返回 need_decision, 不发 RS,
          由用户选择「覆盖 (ow=1) / 改名 / 末尾加 _1」后再调用。
    path: 保存目录。空串 = 厂商默认 \\ASLDATA; 也接受绝对路径或 \\ 开头相对 ASL 安装根。
    必须先调 asl_prepare_recording 并让用户决定名字/冲突策略/目录; 活塞会真实运动, 需用户确认。
    仿真正常停止 (asl_stop/ES) 后, <NAME>.dtb/.rwb 等大文件才写全。
    """
    chk = rec.check_run(name, wf, ow, path)
    if chk["action"] != "run":
        block = {"ok": False, "need_decision": chk["action"] == "need_decision"}
        for key in ("error", "hint", "name", "resolved_dir", "existing_group", "options"):
            if key in chk:
                block[key] = chk[key]
        return _j(block)

    directory = Path(chk["resolved_dir"])
    if not chk["dir_exists"]:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            chk["notes"].append(f"已创建保存目录 {directory}")
        except OSError as e:
            return _j({"ok": False, "error": f"保存目录不可用: {e}",
                       "resolved_dir": str(directory)})

    args = [f"wf={chk['wf']}", f"ow={chk['ow']}", f"name={chk['name']}"]
    if path.strip():
        args.append(f'path="{path.strip()}"')
    try:
        payload = json.loads(_tai_run("rs " + " ".join(args)))
    except json.JSONDecodeError:
        payload = {"ok": False, "error": "RS 返回无法解析"}
    if payload.get("ok"):
        _session["run_name"] = chk["name"]

    existing = chk["existing_group"]
    payload["recording"] = {
        "name": chk["name"],
        "wf": chk["wf"],
        "ow": chk["ow"],
        "vendor_path": chk["vendor_path"],
        "resolved_dir": chk["resolved_dir"],
        "existing_group_before_run": existing if existing["exists"] else None,
        "expected_files": chk["expected_files"],
        "hint": "厂商原始数据由 ASL 软件写出; 正常 asl_stop(ES) 后大文件才完整",
    }
    if existing["exists"] and chk["ow"]:
        payload["warning"] = ("已覆盖原有整组厂商原始文件, 不可恢复: "
                              + ", ".join(f["name"] for f in existing["files"]))
    if chk["notes"]:
        payload["notes"] = chk["notes"]
    return _j(payload)


@mcp.tool()
def asl_stop() -> str:
    """结束当前仿真 (TAI ES 命令), 等价于 GUI 的 Simulation 按钮。"""
    return _tai_run("es")


@mcp.tool()
def asl_exit() -> str:
    """退出 ASL 5000 软件 (TAI EX 命令), 释放 COM 口。测试结束后调用。"""
    return _tai_run("ex")


@mcp.tool()
def asl_tai_status() -> str:
    """查询仿真状态 (TAI TC 命令): 运行/暂停/错误、脚本模式、已跑时间与剩余时间、
    当前第几口呼吸、当前 vr3 病人文件、ASL 软件/固件/TAI 版本。"""
    cmd = "tc " + " ".join(f"{f}=?" for f in tc.TC_FIELDS)
    try:
        lines = tc.get_client().command(cmd, to_ms=10000)
    except tc.TAIError as e:
        return _j({"ok": False, "error": str(e), "code": e.code})
    except (TimeoutError, OSError) as e:
        return _j({"ok": False, "error": f"{type(e).__name__}: {e}"})
    d = tc.parse_kv_lines(lines)
    if not d and len(lines) == 1 and "\t" in lines[0]:
        vals = [v.strip() for v in lines[0].split("\t")]
        d = dict(zip(tc.TC_FIELDS, vals))
    return _j({"ok": True, "status": d, "raw": lines})


@mcp.tool()
def asl_query_breath_params(params: str = "", breath_num: int = -1,
                            cond: str = "", prec: int = 0) -> str:
    """查询逐口呼吸分析参数 (TAI BP 命令)。
    params: 逗号分隔的参数名列表 (如 'P_PEAK,TIME_BR,WORK_PAT_INSP'); 留空返回常用缺省集
    (P_PEAK/P_MEAN/PEEP/AutoPEEP/Vt/峰流量/触发响应时间/病人吸气做功/呼吸频率等)。
    breath_num: 呼吸号, -1=最近一口; cond: 气体条件换算 AM/BTPS/STPD/SPD/ATPD;
    prec: 小数位数 (0=默认)。完整参数名清单见 documentation/TAI Specification.pdf BP 章节。"""
    names = [p.strip().upper() for p in params.split(",") if p.strip()] \
        or list(tc.BP_DEFAULT_PARAMS)
    args = []
    if breath_num != -1:
        args.append(f"breath_num={breath_num}")
    if cond:
        args.append(f"cond={cond}")
    if prec:
        args.append(f"prec={prec}")
    args.extend(f"{n}=?" for n in names)
    try:
        lines = tc.get_client().command("bp " + " ".join(args), to_ms=10000)
    except tc.TAIError as e:
        return _j({"ok": False, "error": str(e), "code": e.code,
                   "hint": "参数名非法时见 ERROR(04)/(05); 名单查 TAI 规范 BP 章节"})
    except (TimeoutError, OSError) as e:
        return _j({"ok": False, "error": f"{type(e).__name__}: {e}"})
    return _j({"ok": True, "breath_num": breath_num,
               "params": tc.parse_kv_lines(lines), "raw": lines})


@mcp.tool()
def asl_export_waveform(name: str = "", breath_num: int = -1,
                        out_name: str = "BW_OUTPUT", raw: bool = False) -> str:
    """导出某口呼吸的完整波形为 tab 分隔文本文件 (TAI BW 命令)。
    name=RS 录制时用的数据名; 留空自动跟随本次会话最近一次成功 asl_run 的 NAME (否则 data),
    避免厂商默认 data 指向错误的旧数据组。breath_num 呼吸号 (-1=最后一口);
    raw=True 输出原始 ADC 波形。返回文件路径 (默认在 ASL 目录 \\ASLDATA 下)。"""
    data_name = (rec.sanitize_name(name) if name.strip()
                 else (_session["run_name"] or "data"))
    args = [f"name={data_name}", f"breath_num={breath_num}",
            f"outname={out_name}", f"raw={'TRUE' if raw else 'FALSE'}"]
    try:
        payload = json.loads(_tai_run("bw " + " ".join(args)))
    except json.JSONDecodeError:
        payload = {"ok": False, "error": "BW 返回无法解析"}
    payload["used_name"] = data_name
    return _j(payload)


@mcp.tool()
def asl_init(urc: float | None = None, r_con: float | None = None,
             r_tubing: float | None = None, v_tubing: float | None = None) -> str:
    """配置仿真初始化 (TAI SI 命令, 仅在仿真启动前设置): urc=未补偿余气量 FRC 的一部分[L] (典型 0.5);
    r_con=连接器阻力[cmH2O/L/s] (典型 0.5); r_tubing/v_tubing=外接管路阻力/容积补偿。
    不传的参数保持原值。"""
    names = {"urc": urc, "r_con": r_con, "r_tubing": r_tubing, "v_tubing": v_tubing}
    args = [f"{k}={v}" for k, v in names.items() if v is not None]
    if not args:
        return _j({"ok": False, "error": "至少提供一个参数 (urc/r_con/r_tubing/v_tubing)"})
    return _tai_run("si " + " ".join(args))


@mcp.tool()
def asl_aux(c: float | None = None, exp_r: float | None = None,
            insp_r: float | None = None) -> str:
    """设置辅助回路参数 (TAI AC 命令), 模拟接在病人 Y 型口之前的额外管路负荷:
    c=顺应性[mL/cmH2O]; exp_r/insp_r=呼气/吸气相阻力[cmH2O/L/s]。不传的参数保持原值。"""
    names = {"c": c, "exp_r": exp_r, "insp_r": insp_r}
    args = [f"{k}={v}" for k, v in names.items() if v is not None]
    if not args:
        return _j({"ok": False, "error": "至少提供一个参数 (c/exp_r/insp_r)"})
    return _tai_run("ac " + " ".join(args))


@mcp.tool()
def asl_set_interactive(on: bool) -> str:
    """进入 (on=True, TAI IM 命令) 或退出 (on=False, TAI SM 命令) 交互模式。
    只有进入交互模式后 asl_set 才能在仿真运行中实时改肺参数; 退出后回到脚本文件控制。"""
    return _tai_run("im" if on else "sm")


@mcp.tool()
def asl_set(params: dict) -> str:
    """仿真运行中实时修改模拟肺参数 (TAI IC 命令, 需先 asl_set_interactive(True))。
    params 为 {参数名: 值} 对象, 支持三组:
    肺模型: RT 气管阻力, RT_OUT 呼气相气管阻力, C1/C2 顺应性[mL/cmH2O], R1/R2 阻力[cmH2O/L/s] (双肺模型左右支)
    自主呼吸用力: RATE 呼吸频率[bpm], INSP_PMUS/EXP_PMUS 吸/呼肌肉压力[cmH2O],
      INSP_RISETIME/INSP_HOLD/INSP_RELEASETIME 等 [%], TRIGGERTIME 触发时间[ms]
    闭环: LOOP=NO_LOOP/CONST_VT/CONST_MV/CO2Y, VTSET 目标潮气量[mL], MVSET 目标分钟通气量,
      ETCO2SET 目标 EtCO2[mmHg], PCASECO2=NORMAL/COPD_III/PULM_FIB/USER
    也可传 '参数名=?' 形式的值 (字符串) 来只读该参数当前值。
    示例: {"C1": 50, "R1": 10, "RATE": 20} 或 {"RT": "?"}"""
    if not params:
        return _j({"ok": False, "error": "params 为空"})
    unknown = [k for k in params if str(k).upper() not in tc.IC_ALL_PARAMS]
    if unknown:
        valid = ", ".join(sorted(tc.IC_ALL_PARAMS))
        return _j({"ok": False, "error": f"未知 IC 参数: {unknown}",
                   "valid_params": valid})
    args = [f"{k}={v}" for k, v in params.items()]
    try:
        lines = tc.get_client().command("ic " + " ".join(args), to_ms=60000)
    except tc.TAIError as e:
        return _j({"ok": False, "error": str(e), "code": e.code,
                   "hint": "ERROR(16) 通常是未进入交互模式或当前肺模型不支持该参数"})
    except (TimeoutError, OSError) as e:
        return _j({"ok": False, "error": f"{type(e).__name__}: {e}"})
    return _j({"ok": True, "applied": params,
               "current_values": tc.parse_kv_lines(lines), "raw": lines})





