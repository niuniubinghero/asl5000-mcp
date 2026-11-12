"""Machine-specific configuration and ASL installation discovery.

Nothing in this module hard-codes a drive letter, user name or install path.
Resolution order for each executable:

    environment variable > user config file > auto-discovery > None

When auto-discovery fails, the Agent Skill guides the user to call
``asl_set_paths`` (or ``asl-monitor paths``) with the real paths.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_NAME = "asl-monitor"

ENV_SW = "ASL_SW_PATH"
ENV_TAI = "ASL_TAI_EXE"
ENV_ROOT = "ASL_ROOT"
ENV_CONFIG = "ASL_MONITOR_CONFIG"
ENV_RECORDINGS = "ASL_MONITOR_RECORDINGS"
ENV_HOST = "ASL_TAI_HOST"
ENV_PORT = "ASL_TAI_PORT"

# Product directory names are used only as discovery hints, never as fixed paths.
SW_DIR_HINTS = ("ASL Software 3.6", "ASL Software", "ASL5000")
SW_EXE_NAMES = ("ASL5000_SW3.6.exe", "ASL5000.exe")
TAI_DIR_HINTS = ("ASL Test Automation Interface 1.0", "ASL Test Automation Interface")
TAI_EXE_NAMES = ("ASL_TAI.exe",)


def user_config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


def user_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def config_file() -> Path:
    override = os.environ.get(ENV_CONFIG)
    return Path(override) if override else user_config_dir() / "config.json"


def read_config() -> dict:
    f = config_file()
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_config(updates: dict) -> dict:
    cfg = read_config()
    cfg.update({k: v for k, v in updates.items() if v})
    f = config_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return cfg


def _env_path(var: str) -> str | None:
    value = os.environ.get(var)
    if not value:
        return None
    return value if Path(value).exists() else None


def candidate_roots() -> list[Path]:
    """Likely install roots, derived from environment variables only."""
    roots: list[Path] = []
    for var in (ENV_ROOT, "ProgramFiles(x86)", "ProgramFiles", "ProgramW6432",
                "LOCALAPPDATA", "ProgramData"):
        value = os.environ.get(var)
        if value and Path(value).is_dir():
            roots.append(Path(value))
    # Deduplicate while preserving order.
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        key = str(r).lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _discover(dir_hints: tuple[str, ...], exe_names: tuple[str, ...]) -> str | None:
    for root in candidate_roots():
        for hint in dir_hints:
            for exe in exe_names:
                candidate = root / hint / exe
                if candidate.is_file():
                    return str(candidate)
        for exe in exe_names:
            candidate = root / exe
            if candidate.is_file():
                return str(candidate)
    return None


def discover_sw_path() -> str | None:
    return _discover(SW_DIR_HINTS, SW_EXE_NAMES)


def discover_tai_exe() -> str | None:
    return _discover(TAI_DIR_HINTS, TAI_EXE_NAMES)


def sw_path() -> str | None:
    return _env_path(ENV_SW) or read_config().get("asl_sw_path") or discover_sw_path()


def tai_exe() -> str | None:
    return _env_path(ENV_TAI) or read_config().get("asl_tai_exe") or discover_tai_exe()


def vars_dir() -> Path | None:
    """Directory holding the vendor's ``vars/`` script library, if resolvable."""
    sw = sw_path()
    return Path(sw).parent / "vars" if sw else None


def tai_host() -> str:
    return os.environ.get(ENV_HOST, "127.0.0.1")


def tai_port() -> int:
    try:
        return int(os.environ.get(ENV_PORT, "6341"))
    except ValueError:
        return 6341


def recordings_dir() -> Path:
    override = os.environ.get(ENV_RECORDINGS)
    d = Path(override) if override else user_data_dir() / "recordings"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def save_paths(sw: str | None = None, tai: str | None = None) -> dict:
    """Persist user-supplied paths. Kept API-compatible with the legacy module."""
    updates = {}
    if sw:
        updates["asl_sw_path"] = sw
    if tai:
        updates["asl_tai_exe"] = tai
    write_config(updates)
    return {
        "asl_sw_path": sw_path(),
        "asl_tai_exe": tai_exe(),
        "config_file": str(config_file()),
        "note": ("Environment variables ASL_SW_PATH / ASL_TAI_EXE take priority "
                 "over the user config file."),
    }


def skill_text() -> str:
    """Return the packaged Agent Skill (shipped as package data)."""
    try:
        from importlib.resources import files
        return files(__package__).joinpath("data/SKILL.md").read_text(encoding="utf-8")
    except (ImportError, OSError, TypeError):
        return (Path(__file__).resolve().parent / "data" / "SKILL.md").read_text(
            encoding="utf-8")
