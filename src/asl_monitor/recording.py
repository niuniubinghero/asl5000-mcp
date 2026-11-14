#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""厂商原始留存数据的命名 / 目录 / 同名冲突预检（纯只读，无硬件依赖）。

TAI 的 RS 命令参数（见厂商 TAI Specification 第 29 页）::

    RS [WF] [OW] [NAME] [PATH] [TO]

``WF`` 默认 0（不保存波形），``OW`` 默认 0，``NAME`` 默认 ``data``，
``PATH`` 默认 ``\\ASLDATA``。本包默认 ``wf=1`` 保留厂商原始数据组，
本模块负责在发 RS 之前把「名字 / 冲突策略 / 目录」三个决策交给用户。

厂商数据组是一组同名不同扩展名的文件（.dtb/.rwb/.brb/.avb/.aux/.vol/
.tdms/.tdms_index），由 ASL 软件自己写到 <ASL_ROOT>\\ASLdata\\ 下，
与 asl-monitor 通过 6342/6343 广播旁听写出的 CSV 是两套独立数据。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from . import config as cfg

# 厂商录制数据组的文件扩展名（用于展示；冲突检测按「同名任意扩展名」判定）
DATA_EXTS = (".aux", ".avb", ".brb", ".dtb", ".rwb", ".vol",
             ".tdms", ".tdms_index")
OPTIONAL_EXTS = (".evt",)  # 事件文件，某些运行才会生成

# 相对 ASL 安装根的厂商默认保存目录；传给 RS 时必须以反斜杠开头
DEFAULT_VENDOR_PATH = r"\ASLDATA"

# .dtb / .rwb 是「500 字节头 + float 数组」，只有 500 字节说明没有波形数据体
HEADER_ONLY_BYTES = 500

_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def asl_root(root: str | Path | None = None) -> Path | None:
    """ASL 安装根目录。优先用显式传入值，其次由配置/自动探测得到。"""
    if root:
        return Path(root)
    sw = cfg.sw_path()
    if sw:
        return Path(sw).parent
    vd = cfg.vars_dir()
    return vd.parent if vd else None


def resolve_data_dir(path: str = "", root: str | Path | None = None) -> Path | None:
    """把 RS 的 PATH 参数解析成本机目录。

    - 空串 / ``\\ASLDATA`` / ``\\`` 开头 → 相对 ASL 安装根
    - 带盘符或 UNC 的绝对路径 → 原样使用
    - 其它相对路径 → 相对 ASL 安装根
    - 需要相对根但根未解析出来 → None（提示用户先 asl_set_paths）
    """
    base = asl_root(root)
    p = (path or "").strip()
    if not p:
        return base / "ASLdata" if base else None
    if p.startswith(("\\", "/")):
        rel = p.lstrip("\\/").replace("\\", "/")
        return base / rel if base else None
    if Path(p).is_absolute() or _DRIVE_PREFIX.match(p):
        return Path(p)
    return base / p if base else None


def sanitize_name(name: str) -> str:
    """把用户输入清成合法的录制数据名；非法字符换成下划线，并去掉扩展名尾巴。"""
    s = re.sub(r"\s+", "_", _INVALID_CHARS.sub("_", str(name).strip()))
    s = s.strip(" .")
    # 用户可能顺手填了 "xxx.tdms"；NAME 是不带扩展名的基名
    if "." in s:
        s = s.rsplit(".", 1)[0].strip(" .")
    s = s[:64].strip(" .")
    if not s:
        raise ValueError("录制数据文件名不能为空")
    return s


def recommend_name(script: str = "", when: datetime | None = None) -> str:
    """生成推荐名：<脚本名>_<YYYYMMDD_HHMMSS>，保证每次都不重名。"""
    when = when or datetime.now()
    base = sanitize_name(script) if script and script.strip() else "ASL5000"
    base = base[:40].strip(" ._") or "ASL5000"
    return f"{base}_{when:%Y%m%d_%H%M%S}"


def group_files(directory: Path | None, name: str) -> list[Path]:
    """找出同名数据组的所有文件（Windows 文件名大小写不敏感）。"""
    if directory is None or not directory.is_dir():
        return []
    prefix = name.casefold() + "."
    return sorted(p for p in directory.iterdir()
                  if p.is_file() and p.name.casefold().startswith(prefix))


def inspect_group(directory: Path | None, name: str) -> dict:
    """检查同名数据组是否已存在，并给出大小/时间/是否只有空头。"""
    files = group_files(directory, name)
    entries = []
    for p in files:
        try:
            st = p.stat()
            entries.append({
                "name": p.name,
                "size_bytes": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            })
        except OSError:
            entries.append({"name": p.name, "size_bytes": None, "modified": None})
    wave = [e for e in entries
            if e["name"].casefold().endswith((".dtb", ".rwb"))
            and e["size_bytes"] is not None]
    has_waveform = any(e["size_bytes"] > HEADER_ONLY_BYTES for e in wave)
    return {
        "exists": bool(files),
        "file_count": len(files),
        "total_bytes": sum(e["size_bytes"] or 0 for e in entries),
        "has_waveform_data": has_waveform,
        "header_only": bool(wave) and not has_waveform,
        "files": entries,
    }


def next_available_name(directory: Path | None, name: str) -> str:
    """同名已存在时给出末尾加 _1 / _2 … 的第一个可用名字。"""
    if not group_files(directory, name):
        return name
    for i in range(1, 1000):
        candidate = f"{name}_{i}"
        if not group_files(directory, candidate):
            return candidate
    raise RuntimeError("同名后缀 _1.._999 都已占用，请让用户换个名字")


def conflict_choices(name: str, vendor_path: str, suffix: str) -> list[dict]:
    """同名冲突时的三种用户选项，附各自可直接传给 ``asl_run`` 的参数。"""
    base = {"wf": 1, "path": vendor_path}
    return [
        {"id": "overwrite", "label": "覆盖同名数据", "choice": "覆盖",
         "run_kwargs": {**base, "name": name, "ow": 1},
         "warning": "会冲掉已有的整组厂商原始文件, 不可恢复"},
        {"id": "rename", "label": "改个名字", "choice": "改名",
         "run_kwargs": None,
         "note": "让用户填写新名字后重新调用预检"},
        {"id": "suffix", "label": f"末尾加后缀 → {suffix}", "choice": "末尾加后缀",
         "run_kwargs": {**base, "name": suffix, "ow": 0},
         "note": "自动找到第一个不冲突的 _N"},
    ]


def check_run(name: str, wf: int = 1, ow: int = 0, path: str = "",
              root: str | Path | None = None) -> dict:
    """``asl_run`` 的纯只读决策层：校验名字/目录、检测同名、决定能否发 RS。

    ``action`` = ``run`` / ``need_decision`` / ``error``。不写文件、不发 TAI 命令。
    ``need_decision`` 时 server 必须把 ``options`` 交给用户选择，不得自行决定。
    """
    try:
        clean = sanitize_name(name)
    except ValueError as e:
        return {"action": "error", "error": str(e),
                "hint": "先做预检拿推荐名或让用户填写"}
    directory = resolve_data_dir(path, root)
    if directory is None:
        return {"action": "error", "error": "无法解析保存目录",
                "hint": "先调用 asl_set_paths 提供 ASL 软件路径, 或传绝对目录"}
    vendor_path = path.strip() or DEFAULT_VENDOR_PATH
    existing = inspect_group(directory, clean)
    result = {
        "action": "run",
        "name": clean,
        "wf": int(bool(wf)),
        "ow": int(bool(ow)),
        "vendor_path": vendor_path,
        "resolved_dir": str(directory),
        "dir_exists": directory.is_dir(),
        "existing_group": existing,
        "expected_files": [f"{clean}{ext}" for ext in DATA_EXTS],
        "notes": [],
    }
    if result["wf"] != 1:
        result["notes"].append("wf!=1: 本次不保存厂商原始波形数据组 (.dtb/.rwb 等)")
    if existing["exists"] and not result["ow"]:
        suffix = next_available_name(directory, clean)
        result.update({
            "action": "need_decision",
            "error": "同名录制数据已存在, 未启动仿真 (未发 RS)",
            "suffix": suffix,
            "options": conflict_choices(clean, vendor_path, suffix),
            "hint": "必须先询问用户选择, 不能自行决定",
        })
    return result


def prepare(name: str = "", script: str = "", path: str = "",
            when: datetime | None = None,
            root: str | Path | None = None) -> dict:
    """只读预检：推荐名、同名检查、三种冲突处理候选、目录选项、待用户回答的问题。

    不写任何文件、不发任何 TAI 命令。供 MCP 工具 ``asl_prepare_recording`` 包装，
    由 Agent 用提问工具把决策交给用户后再调用 ``asl_run``。
    """
    when = when or datetime.now()
    recommended = recommend_name(script, when)
    custom = bool(name and name.strip())
    try:
        effective = sanitize_name(name) if custom else recommended
    except ValueError as e:
        return {"ok": False, "error": str(e), "recommended_name": recommended}

    directory = resolve_data_dir(path, root)
    vendor_path = path.strip() if path.strip() else DEFAULT_VENDOR_PATH
    existing = inspect_group(directory, effective)
    suffix = next_available_name(directory, effective) if directory else effective + "_1"

    base_kwargs = {"wf": 1, "path": vendor_path}
    options = conflict_choices(effective, vendor_path, suffix)

    return {
        "ok": True,
        "tool": "asl_prepare_recording",
        "timestamp": when.isoformat(timespec="seconds"),
        "recommended_name": recommended,
        "requested_name": effective,
        "name_source": "user" if custom else "recommended",
        "path_choice": "custom" if path.strip() else "default",
        "vendor_path_arg": vendor_path,
        "resolved_dir": str(directory) if directory else None,
        "dir_exists": bool(directory and directory.is_dir()),
        "dir_hint": None if directory else "ASL 安装根未解析，先调用 asl_set_paths 提供软件路径",
        "conflict": existing["exists"],
        "existing_group": existing,
        "conflict_options": options if existing["exists"] else [],
        "default_run_kwargs": {**base_kwargs, "name": effective, "ow": 0},
        "ask_user": [
            {"question": "录制数据文件名用哪个？",
             "options": [f"推荐名 {recommended}", "自定义填写（用户给出名字后重新预检）"]},
            {"question": "保存目录用哪个？",
             "options": [f"默认 {DEFAULT_VENDOR_PATH}（{(directory or Path('?'))}）",
                         "自定义路径（绝对路径，或 \\ 开头相对 ASL 安装根）"]},
            {"question": "若同名数据已存在，怎么处理？",
             "options": ["覆盖", "改名", "末尾加 _1/_2"],
             "only_if": "conflict=true"},
        ],
        "hint": ("wf=1 始终保留厂商原始数据；把用户最终选择的 run_kwargs 原样传给 asl_run。"
                 "BW 导出时要带同一个 name。"),
    }
