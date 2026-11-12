# -*- coding: utf-8 -*-
"""ASL installation-root discovery shared by the standalone tools.

The ASL root is the vendor directory that contains ``vars/`` and ``breath/``.
Resolution order (no drive letter, user name or install path is hard-coded)::

    --asl-root > $ASL_ROOT > parent of $ASL_SW_PATH > installed
    asl_monitor.config > common install locations > actionable error

These tools can run straight from a source checkout without installing the
package; the optional import of :mod:`asl_monitor.config` simply adds one more
discovery source when the package *is* installed.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

DIR_HINTS = ("ASL Software 3.6", "ASL Software", "ASL5000")
SW_EXE_NAMES = ("ASL5000_SW3.6.exe", "ASL5000.exe")
FALLBACK_CUTOFF = "2019-01-01"


def _candidate_roots() -> list[Path]:
    bases: list[Path] = []
    for var in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432",
                "ProgramData", "LOCALAPPDATA"):
        value = os.environ.get(var)
        if value and Path(value).is_dir():
            bases.append(Path(value))
    if sys.platform == "darwin":
        bases.append(Path("/Applications"))
    elif sys.platform != "win32":
        bases.extend((Path("/opt"), Path("/usr/local")))
    out: list[Path] = []
    for base in bases:
        out.append(base)
        out.extend(base / hint for hint in DIR_HINTS)
    return out


def _check(root: Path, require_vars: bool) -> Path:
    if require_vars and not (root / "vars").is_dir():
        print("[warn] %s has no vars/ subdirectory; is this the ASL install root?"
              % root, file=sys.stderr)
    return root


def resolve_root(cli_value: str | None = None, *, require_vars: bool = True) -> Path:
    """Return the ASL installation root, or exit with an actionable message."""
    if cli_value:
        p = Path(cli_value).expanduser().resolve()
        if not p.is_dir():
            raise SystemExit("--asl-root is not a directory: %s" % p)
        return _check(p, require_vars)

    env_root = os.environ.get("ASL_ROOT")
    if env_root and Path(env_root).is_dir():
        return _check(Path(env_root).resolve(), require_vars)

    env_sw = os.environ.get("ASL_SW_PATH")
    if env_sw and Path(env_sw).is_file():
        return _check(Path(env_sw).parent.resolve(), require_vars)

    try:
        from asl_monitor import config as cfg  # optional: only when installed
        sw = cfg.sw_path()
        if sw and Path(sw).is_file():
            return _check(Path(sw).parent.resolve(), require_vars)
    except Exception:
        pass

    for candidate in _candidate_roots():
        if (candidate / "vars").is_dir():
            return candidate.resolve()

    raise SystemExit(
        "Cannot locate the ASL 5000 installation root (the directory containing\n"
        "vars/ and breath/). Do one of the following:\n"
        "  --asl-root <dir>          point at the install directory\n"
        "  ASL_ROOT=<dir>            environment variable\n"
        "  ASL_SW_PATH=<...exe>      full path to ASL5000_SW3.6.exe\n"
    )


def default_official_cutoff(root: Path) -> str:
    """Guess the vendor install date from the ASL software exe mtime.

    Files modified before this date inside vendor directories are classified as
    "official". Override with ``--official-before YYYY-MM-DD`` when the install
    date is atypical (e.g. files copied from an archive).
    """
    for name in SW_EXE_NAMES:
        exe = root / name
        if exe.is_file():
            day = datetime.fromtimestamp(exe.stat().st_mtime) + timedelta(days=1)
            return day.strftime("%Y-%m-%d")
    return FALLBACK_CUTOFF
