#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ASL 5000 TAI 控制通道客户端 (TCP 6341, Test Automation Interface)

原理 (见 documentation/TAI Specification.pdf V8.0):
  ASL_TAI.exe 独立于 ASL 软件运行, 监听 TCP 6341, 接收 ASCII 命令 (CRLF 结尾):
    - 命令为两字母指令 + "参数=值", 同步执行 (必须等响应再发下一条)
    - 成功: 回显 + 数据行(可选) + 新提示符 ">ASLxxxx:"
    - 失败: "ASLxxxx: ERROR(YY) ZZ: 描述" + 新提示符
  注意: 广播口 6342/6343 实测带 4 字节大端长度头; 6341 控制口可能是
  裸 ASCII 流或同样带长度头, 本客户端自动探测两种帧格式。

依赖: 本机已安装 ASL_TAI.exe; 路径由 config.discover_tai_exe() / 环境变量 ASL_TAI_EXE 提供
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

from . import config as cfg

DEFAULT_TAI_HOST = cfg.tai_host()
DEFAULT_TAI_PORT = cfg.tai_port()

TAI_EXE = cfg.tai_exe()
DEFAULT_SW_PATH = cfg.sw_path()


def save_paths(sw_path: str | None = None, tai_exe: str | None = None) -> dict:
    """Persist user-supplied ASL paths; takes effect immediately."""
    global TAI_EXE, DEFAULT_SW_PATH
    result = cfg.save_paths(sw_path, tai_exe)
    TAI_EXE = result["asl_tai_exe"]
    DEFAULT_SW_PATH = result["asl_sw_path"]
    return result


PROMPT_RE = re.compile(r"^\s*[>!]?\s*ASL\w{0,8}:\s*$")
LEADING_PROMPT_RE = re.compile(r"^\s*[<>!]?\s*(ASL\w{0,8}):\s*")
ERROR_RE = re.compile(r"ERROR\((\d+)\)\s*([A-Z]{2})?:?\s*(.*)")

ERROR_DESCRIPTIONS = {
    "01": "未知错误", "02": "未知命令", "03": "命令不可用 (ASL 软件已在运行?)",
    "04": "未知参数", "05": "参数值非法", "06": "参数临时非法 (同 ID 已开实例?)",
    "07": "缺少必选参数", "08": "命令超时", "09": "通信错误",
    "10": "ASL 软件处于错误状态", "11": "上一条命令未完成",
    "12": "操作失败 (OA 路径无效?)", "13": "文件已存在/写失败 (需 ow=1?)",
    "14": "文件不存在或不可读", "15": "ASL 软件被外部关闭",
    "16": "无法设置参数 (只读参数或当前肺模型不支持)", "17": "无法读取参数",
    "18": "参数越界 (如呼吸号超出总呼吸数)", "19": "固件不匹配",
    "20": "脚本错误 (引用了不存在的 vr3)", "99": "命令未在本版本实现",
}


class TAIError(Exception):
    """TAI 命令执行失败, 携带错误码与命令名。"""

    def __init__(self, code: str, cmd: str, detail: str):
        self.code = code
        self.cmd = cmd
        self.detail = detail
        desc = ERROR_DESCRIPTIONS.get(code, "未知错误码")
        super().__init__(f"ERROR({code}) {cmd}: {detail} —— {desc}")


class TAIClient:
    """TAI 6341 控制通道客户端。线程安全 (内部互斥锁), 命令同步执行。"""

    def __init__(self, host: str = DEFAULT_TAI_HOST, port: int = DEFAULT_TAI_PORT):
        self.host = host
        self.port = port
        self.tai_exe = TAI_EXE
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._framed: bool | None = None  # None=未探测

    # ------------------------------------------------------------ 连接管理
    def _probe(self, timeout: float = 1.0) -> bool:
        try:
            s = socket.create_connection((self.host, self.port), timeout=timeout)
            s.close()
            return True
        except OSError:
            return False

    def server_online(self) -> bool:
        return self._probe()

    def ensure_server(self, wait_sec: float = 25.0) -> None:
        """确认 TAI 服务器在线; 不在线则尝试启动 ASL_TAI.exe 并等待。"""
        if self.server_online():
            return
        exe = Path(self.tai_exe)
        if not exe.exists():
            raise TAIError("01", "TAI", f"TAI 服务器未运行且找不到 {exe} "
                                        "(可用环境变量 ASL_TAI_EXE 指定路径)")
        subprocess.Popen([str(exe)], cwd=str(exe.parent),
                         creationflags=0x00000008 | 0x00000200)  # DETACHED | NEW_GROUP
        t0 = time.time()
        while time.time() - t0 < wait_sec:
            if self.server_online():
                time.sleep(0.5)
                return
            time.sleep(0.5)
        raise TAIError("09", "TAI", f"ASL_TAI.exe 已启动但 {self.port} 端口 {wait_sec}s 内未就绪")

    def _ensure_connected(self) -> None:
        if self._sock is not None:
            return
        self.ensure_server()
        self._sock = socket.create_connection((self.host, self.port), timeout=5.0)
        self._framed = None
        self._buf = b""
        self._drain_greeting()

    def _drain_greeting(self) -> None:
        """连接后服务器立即发送问候提示符 '>ASL0000:', 必须先消费掉,
        否则第一条命令的解析会被它错位 (实测 OA 前发过命令还会引发 ERROR(11)/服务器异常)。"""
        self._sock.settimeout(3.0)
        buf = b""
        try:
            while True:
                chunk = self._sock.recv(256)
                if not chunk:
                    break
                buf += chunk
                last = buf.splitlines()[-1].decode("ascii", errors="replace").strip()
                if PROMPT_RE.match(last):
                    break
        except socket.timeout:
            pass

    def reconnect(self) -> None:
        self.close()
        time.sleep(0.5)
        self._ensure_connected()

    def close(self) -> None:
        try:
            if self._sock is not None:
                self._sock.close()
        except OSError:
            pass
        self._sock = None

    # ------------------------------------------------------------ 命令执行
    def command(self, cmd: str, to_ms: int = 10000) -> list[str]:
        """发送一条 TAI 命令, 阻塞至收到响应。返回数据行列表 (不含回显/提示符)。

        失败抛 TAIError (含错误码) 或 TimeoutError。
        ERROR(11)=上一条执行未完成: 自动重连并重试一次。"""
        try:
            return self._command_once(cmd, to_ms)
        except TAIError as e:
            if e.code == "11":
                self.reconnect()
                return self._command_once(cmd, to_ms)
            raise

    # ------------------------------------------------------------ 帧解析
    @staticmethod
    def _detect_framing(buf: bytes) -> bool:
        """广播口(6342/6343)实测为 4 字节大端长度头; 控制口(6341)实测为裸 ASCII 流。
        保留自动探测, 防止固件/版本差异。"""
        if len(buf) < 5:
            return False
        n = int.from_bytes(buf[:4], "big")
        return 0 < n <= 1_000_000 and 0x20 <= buf[4] <= 0x7E

    def _drain_framed(self) -> list[str]:
        lines = []
        while len(self._buf) >= 4:
            n = int.from_bytes(self._buf[:4], "big")
            if n <= 0 or n > 1_000_000:
                self._framed = False  # 帧错位, 退回裸流模式
                return self._drain_raw()
            if len(self._buf) < 4 + n:
                break
            msg = self._buf[4:4 + n]
            self._buf = self._buf[4 + n:]
            lines.extend(msg.decode("ascii", errors="replace").splitlines())
        return lines

    def _drain_raw(self) -> list[str]:
        lines = []
        while b"\n" in self._buf:
            raw, _, self._buf = self._buf.partition(b"\n")
            lines.append(raw.decode("ascii", errors="replace").rstrip("\r"))
        return lines

    def _command_once(self, cmd: str, to_ms: int) -> list[str]:
        cmd = cmd.strip()
        with self._lock:
            self._ensure_connected()
            deadline = time.time() + max(0.5, to_ms / 1000.0) + 5.0
            self._sock.sendall((cmd + "\r\n").encode("ascii"))
            data: list[str] = []
            while time.time() < deadline:
                self._sock.settimeout(min(2.0, max(0.2, deadline - time.time())))
                try:
                    chunk = self._sock.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    if cmd.upper() == "QT":
                        return data  # QT 会主动断开连接, 属预期行为
                    raise ConnectionError("TAI 服务器关闭了连接")
                self._buf += chunk
                if self._framed is None:
                    self._framed = self._detect_framing(self._buf)
                lines = self._drain_framed() if self._framed else self._drain_raw()
                for ln in lines:
                    s = ln.strip()
                    if not s:
                        continue
                    # 去掉行首提示符后比对回显
                    body = LEADING_PROMPT_RE.sub("", s)
                    if body.lower() == cmd.lower() or s == cmd:
                        continue
                    m = ERROR_RE.search(s)
                    if m:
                        code, cmdname, detail = m.group(1), m.group(2) or "?", m.group(3)
                        raise TAIError(code, cmdname, detail.strip())
                    if PROMPT_RE.match(s):
                        return data
                    data.append(s)
            raise TimeoutError(f"TAI 命令超时 ({to_ms}ms): {cmd!r}; 已收到: {data}")


# ---------------------------------------------------------------- 参数解析

KV_PAIR_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_\.]*)\s*=\s*([^\s=]+)")


def parse_kv_lines(lines: list[str]) -> dict:
    """TAI 响应数据行 → {参数: 值}。
    优先提取行内所有 name=value 对 (一行可含多对, 如 TC 响应), 否则按 TAB/空格两列解析。"""
    d: dict[str, str] = {}
    for ln in lines:
        pairs = KV_PAIR_RE.findall(ln)
        if pairs:
            for k, v in pairs:
                d[k] = v
            continue
        if "\t" in ln:
            k, _, v = ln.partition("\t")
        else:
            parts = ln.split()
            if len(parts) == 2 and not parts[0][0].isdigit():
                k, v = parts
            else:
                continue
        k = k.strip()
        if k:
            d[k] = v.strip()
    return d


# ---------------------------------------------------------------- IC 参数白名单

IC_PARAM_GROUPS: dict[str, tuple[str, ...]] = {
    "闭环控制": ("LOOP", "VTSET", "PVTUPPER", "PVTLOWER",
                 "MVSET", "PMVUPPER", "PMVLOWER", "FMVUPPER", "FMVLOWER",
                 "PCASECO2", "ETCO2SET", "CO2FLOW",
                 "PCO2UPPER", "PCO2LOWER", "FCO2UPPER", "FCO2LOWER"),
    "肺模型": ("RT", "RT_OUT", "C1", "C2", "R1", "R2", "R1_OUT", "R2_OUT"),
    "自主呼吸用力": ("RATE", "PAUSE", "INSP_PMUS", "INSP_RISETIME", "INSP_HOLD",
                     "INSP_RELEASETIME", "EXP_PMUS", "EXP_RISETIME", "EXP_HOLD",
                     "EXP_RELEASETIME", "TRIGGERTIME"),
    "病人库": ("PATH", "INITIAL_SETTINGS"),
}
IC_ALL_PARAMS = {p for ps in IC_PARAM_GROUPS.values() for p in ps}

TC_FIELDS = ("ASL_STATUS", "SIM_STATUS", "SIM_MODE", "SIM_TIME", "SIM_TIME_REMAIN",
             "SIM_BREATH_SCT", "SIM_BREATH_CNT", "SIM_VARFILE", "COMM_DELAY",
             "SW_VERSION", "FW_VERSION", "TAI_VERSION")

# BP 常用参数缺省集 (面向 CPAP/无创通气调试)
BP_DEFAULT_PARAMS = ("BREATH_TYPE", "TIME_BR", "TIME_IE_RATIO", "TIME_TRIG_RESP",
                     "P_PEAK", "P_MEAN", "P_PEEP", "P_PEEP_AUTO_1",
                     "P_DROP_MAX_DURING_TRIG",
                     "VOL_VENT_VT_INSP", "VOL_VENT_VT_EXP",
                     "FLOW_PEAK_INSP", "FLOW_PEAK_EXP", "WORK_PAT_INSP")


# ---------------------------------------------------------------- 全局单例

_client: TAIClient | None = None
_client_lock = threading.Lock()


def get_client() -> TAIClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = TAIClient()
        return _client


def fmt_cmd(cmd: str, to_ms: int | None = None) -> str:
    return f"{cmd} to={to_ms}" if to_ms else cmd
