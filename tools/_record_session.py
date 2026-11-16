# -*- coding: utf-8 -*-
"""独立后台录波 (MCP 服务器僵死期间的替代方案)。
逻辑与 server.py::_record_worker 相同: 订阅 6342/6343 广播,
写分段波形 CSV + 逐呼吸 CSV + latest_status.json, Ctrl+C 结束时写 summary。
"""
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from asl_monitor import config as cfg
from asl_monitor import monitor as am

STATS_WINDOW_SEC = 5.0
SPLIT_SEC = 300.0


def main() -> int:
    outdir = cfg.recordings_dir() / f"session_{datetime.now():%Y%m%d_%H%M%S}"
    outdir.mkdir(parents=True, exist_ok=True)
    recorder = am.Recorder(outdir, split_sec=SPLIT_SEC, wall_time=True)
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

    stop = threading.Event()
    t_breath = threading.Thread(target=am.broadcast_worker,
                                args=("呼吸参数", am.DEFAULT_HOST,
                                      am.DEFAULT_BREATH_PORT,
                                      breath_handler, state, stop), daemon=True)
    t_wave = threading.Thread(target=am.broadcast_worker,
                              args=("波形", am.DEFAULT_HOST,
                                    am.DEFAULT_WAVE_PORT,
                                    wave_handler, state, stop), daemon=True)
    t_breath.start()
    t_wave.start()

    t0 = time.time()
    print(f"[record] session dir: {outdir}", flush=True)
    try:
        while True:
            time.sleep(2.0)
            with lock:
                header = state["wave_header"]
                recent = list(state["recent"])
                last_breath = dict(state["last_breath"])
                lb_age = (round(time.time() - state["last_breath_time"], 1)
                          if state["last_breath_time"] else None)
                lw_age = (round(time.time() - state["last_wave_time"], 1)
                          if state["last_wave_time"] else None)
                b_cnt, w_cnt = state["breath_count"], state["wave_msg_count"]
            stats = am.wave_window_stats(header, recent, STATS_WINDOW_SEC)
            payload = {
                "tool": "asl-monitor",
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "elapsed_sec": round(time.time() - t0, 1),
                "connections": {
                    "breath": {"port": am.DEFAULT_BREATH_PORT,
                               "connected": t_breath.is_alive(),
                               "last_msg_age_sec": lb_age},
                    "waveform": {"port": am.DEFAULT_WAVE_PORT,
                                 "connected": t_wave.is_alive(),
                                 "last_msg_age_sec": lw_age},
                },
                "breath_count": b_cnt,
                "wave_message_count": w_cnt,
                "last_breath_params": last_breath,
                f"waveform_stats_last_{int(STATS_WINDOW_SEC)}s": stats,
                "recording": recorder.status(),
            }
            am.write_json(outdir / "latest_status.json", payload)
            if b_cnt and b_cnt % 5 == 0:
                print(f"[record] breaths={b_cnt} wave_msgs={w_cnt} "
                      f"paw_mean={stats.get('paw_mean_cmh2o')}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for t in (t_breath, t_wave):
            t.join(timeout=3.0)
        recorder.close()
        with lock:
            final_stats = am.wave_window_stats(state["wave_header"],
                                               list(state["recent"]),
                                               STATS_WINDOW_SEC)
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
        print(f"[record] stopped, summary written", flush=True)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
