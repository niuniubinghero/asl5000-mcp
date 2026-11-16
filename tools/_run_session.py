# -*- coding: utf-8 -*-
"""直驱 TAI 启动仿真 (绕过僵死的 MCP 服务器, 使用同一 tai 模块)。
串行发送: OA(COM1) -> SF(Adult_normal) -> TC 校验 -> RS(wf=1)。
"""
import json
import sys

from asl_monitor import tai as tc

SW = r"C:\Program Files (x86)\ASL Software 3.6\ASL5000_SW3.6.exe"


def emit(tag, payload):
    print(f"=== {tag} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    sys.stdout.flush()


client = tc.TAIClient()
client.ensure_server(wait_sec=25)
emit("tai", {"ok": True, "online": client.server_online()})

# 1) OA 打开软件实例 (COM1, 隐藏 GUI)
try:
    lines = client.command(
        f'oa id=COM1 path="{SW}" gui=0 to=90000', to_ms=95000)
    emit("oa", {"lines": lines})
except tc.TAIError as exc:
    emit("oa_error", {"code": exc.code, "detail": exc.detail})
    sys.exit(1)

# 2) 加载脚本
try:
    lines = client.command("sf name=Adult_normal to=30000", to_ms=35000)
    emit("sf", {"lines": lines})
except tc.TAIError as exc:
    emit("sf_error", {"code": exc.code, "detail": exc.detail})
    sys.exit(1)

# 3) TC 校验实例与脚本就绪
try:
    lines = client.command("tc to=15000", to_ms=20000)
    status = tc.parse_kv_lines(lines)
    emit("tc", {"status": {k: status.get(k) for k in tc.TC_FIELDS}})
except tc.TAIError as exc:
    emit("tc_error", {"code": exc.code, "detail": exc.detail})
    sys.exit(1)

# 4) RS 启动仿真 (保留厂商原始数据)
try:
    lines = client.command(
        "rs wf=1 ow=0 name=AdultNormal_20261116 to=30000", to_ms=35000)
    emit("rs", {"lines": lines})
except tc.TAIError as exc:
    emit("rs_error", {"code": exc.code, "detail": exc.detail})
    sys.exit(1)

emit("done", {"ok": True})
