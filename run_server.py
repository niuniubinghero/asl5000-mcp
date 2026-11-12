#!/usr/bin/env python3
"""Source-mode entry point: run the MCP server directly from a cloned repo.

    python run_server.py

No installation is required. This adds ``src/`` to ``sys.path`` and delegates
to :mod:`asl_monitor`. MCP clients can point at this file with their own
Python interpreter; ``asl-monitor install --mode source`` generates that
configuration automatically.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from asl_monitor.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
