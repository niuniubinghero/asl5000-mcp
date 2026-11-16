"""Command-line helper for asl5000-mcp.

    asl-monitor                     start the MCP server (stdio)
    asl-monitor check               machine/environment self-check
    asl-monitor paths --sw ...      save machine-specific ASL paths
    asl-monitor install --client X  print (or write) a client config snippet
    asl-monitor sync-skill          sync the packaged Skill into a source checkout

``install`` prints a snippet by default. It only writes to a client config file
when ``--write`` is given explicitly, and always keeps a ``.bak`` copy first.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import sysconfig
from pathlib import Path

from . import __version__, config as cfg

SERVER_KEY = "asl-monitor"

# client name -> (label, config file relative to home, target format)
CLIENTS = {
    "workbuddy": ("WorkBuddy", ".workbuddy/mcp.json", "json"),
    "codebuddy": ("CodeBuddy", ".codebuddy/mcp.json", "json"),
    "claude": ("Claude Desktop", "AppData/Roaming/Claude/claude_desktop_config.json", "json"),
    "cursor": ("Cursor", ".cursor/mcp.json", "json"),
    "trae": ("Trae", ".trae/mcp.json", "json"),
    "codex": ("Codex", ".codex/config.toml", "toml"),
    "qoder": ("Qoder", None, "json"),
}


def _port_online(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _com_ports() -> list[dict]:
    try:
        import winreg
    except ImportError:
        return [{"note": "COM port enumeration requires Windows"}]
    ports: list[dict] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DEVICEMAP\SERIALCOMM") as key:
            i = 0
            while True:
                try:
                    _, value, _ = winreg.EnumValue(key, i)
                    i += 1
                except OSError:
                    break
                name = str(value).upper()
                ports.append({"port": name,
                              "tai_usable": name in {f"COM{i}" for i in range(1, 9)}})
    except OSError as exc:
        return [{"note": "COM enumeration failed: %s" % exc}]
    return sorted(ports, key=lambda p: p["port"])


def _script_count() -> int | None:
    vd = cfg.vars_dir()
    if not vd or not vd.exists():
        return None
    return len(list(vd.rglob("*.sct")))


def cmd_check(_args: argparse.Namespace) -> int:
    sw, tai = cfg.sw_path(), cfg.tai_exe()
    rec = cfg.recordings_dir()
    writable = os.access(rec, os.W_OK)
    result = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "asl_software": {"resolved": bool(sw), "path": sw},
        "tai_server": {"resolved": bool(tai), "path": tai},
        "config_file": str(cfg.config_file()),
        "recordings_dir": str(rec),
        "recordings_writable": writable,
        "ports": {
            "tai_control_6341": _port_online(cfg.tai_port(), cfg.tai_host()),
            "broadcast_breath_6342": _port_online(6342),
            "broadcast_wave_6343": _port_online(6343),
        },
        "com_ports": _com_ports(),
        "vars_script_count": _script_count(),
        "ready": bool(sw and tai and writable),
        "hint": ("Ports being offline is normal while the ASL software is not running. "
                 "If ASL paths are not resolved, run: asl-monitor paths --sw <exe> --tai <exe>"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready"] else 1


def cmd_paths(args: argparse.Namespace) -> int:
    if not args.sw and not args.tai:
        print("Nothing to save. Provide --sw and/or --tai.", file=sys.stderr)
        return 2
    print(json.dumps(cfg.save_paths(args.sw, args.tai), ensure_ascii=False, indent=2))
    return 0


def _pip_entry() -> dict:
    """Resolve the console script to a full path when it is not on PATH."""
    found = shutil.which("asl-monitor")
    if found:
        return {"command": found, "args": []}
    scheme = sysconfig.get_path(
        "scripts", "nt_user" if sys.platform == "win32" else "posix_user")
    if scheme:
        name = "asl-monitor.exe" if sys.platform == "win32" else "asl-monitor"
        candidate = Path(scheme) / name
        if candidate.is_file():
            return {"command": str(candidate), "args": []}
    return {"command": "asl-monitor", "args": []}


def _source_entry(args: argparse.Namespace) -> dict:
    repo = Path(args.repo).resolve() if args.repo else Path(__file__).resolve().parents[2]
    entry = repo / "run_server.py"
    if not entry.is_file():
        raise SystemExit("run_server.py not found under %s; pass --repo <checkout dir>" % repo)
    return {"command": args.python or sys.executable, "args": [str(entry)]}


def _client_config_path(client: str) -> Path | None:
    _, rel, _ = CLIENTS[client]
    if rel is None:
        return None
    if client == "claude" and sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "Claude" / "claude_desktop_config.json"
    if client == "claude" and sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path.home() / rel


def cmd_install(args: argparse.Namespace) -> int:
    if args.client not in CLIENTS:
        raise SystemExit("Unknown client. Choose from: %s" % ", ".join(sorted(CLIENTS)))
    label, _, fmt = CLIENTS[args.client]
    entry = _pip_entry() if args.mode == "pip" else _source_entry(args)
    snippet = {"mcpServers": {SERVER_KEY: entry}}
    if args.mode == "pip" and entry["command"] != "asl-monitor":
        print("# Resolved console script to a full path (not on PATH on this machine).")

    if fmt == "toml":
        rendered = ("[mcp_servers.%s]\ncommand = %s\nargs = %s\n" % (
            SERVER_KEY, json.dumps(entry["command"]),
            json.dumps(entry["args"])))
        print("# %s: append to ~/.codex/config.toml" % label)
        print(rendered, end="")
        return 0

    print("# %s config snippet (%s mode)" % (label, args.mode))
    print(json.dumps(snippet, ensure_ascii=False, indent=2))

    target = _client_config_path(args.client)
    if args.write and target:
        existing = {}
        if target.is_file():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                print("Existing config is not valid JSON; aborting write.", file=sys.stderr)
                return 2
            backup = target.with_suffix(target.suffix + ".bak")
            backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
            print("# backup written: %s" % backup)
        existing.setdefault("mcpServers", {}).update(snippet["mcpServers"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
        print("# written: %s" % target)
    elif args.write:
        print("# %s has no standard config path; paste the snippet in its MCP settings."
              % label)
    else:
        print("# Dry run. Add --write to merge this into the client config (a .bak is kept).")

    if args.skill:
        skills_dir = (Path.home() / args.skills_root / "asl-lung-sim"
                      if args.skills_root else None)
        if not skills_dir:
            print("# Pass --skills-root to install the Skill into a client skills directory.")
        else:
            print("# Skill would be installed to: %s" % skills_dir)
            if args.write:
                skills_dir.mkdir(parents=True, exist_ok=True)
                (skills_dir / "SKILL.md").write_text(cfg.skill_text(),
                                                     encoding="utf-8", newline="\n")
                print("# Skill written.")
    return 0


def cmd_sync_skill(_args: argparse.Namespace) -> int:
    repo = Path(__file__).resolve().parents[2]
    dest = repo / "skill" / "asl-lung-sim" / "SKILL.md"
    if not (repo / "run_server.py").is_file():
        raise SystemExit("Not a source checkout: %s" % repo)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n": keep LF endings so the copy stays byte-identical to the package data
    dest.write_text(cfg.skill_text(), encoding="utf-8", newline="\n")
    print("Skill synced to %s" % dest)
    return 0


def cmd_serve(_args: argparse.Namespace) -> int:
    from .server import mcp

    mcp.run(transport="stdio")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="asl-monitor", description=__doc__)
    parser.add_argument("--version", action="version", version="asl5000-mcp " + __version__)
    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser("check", help="environment self-check (no hardware needed)")
    p_check.set_defaults(func=cmd_check)

    p_paths = sub.add_parser("paths", help="save ASL software / TAI server paths")
    p_paths.add_argument("--sw", help="path to ASL5000 software exe")
    p_paths.add_argument("--tai", help="path to ASL_TAI.exe")
    p_paths.set_defaults(func=cmd_paths)

    p_install = sub.add_parser("install", help="print or write a client MCP config snippet")
    p_install.add_argument("--client", default="workbuddy", help="target client name")
    p_install.add_argument("--mode", choices=("pip", "source"), default="pip")
    p_install.add_argument("--python", help="python executable (source mode)")
    p_install.add_argument("--repo", help="source checkout directory (source mode)")
    p_install.add_argument("--write", action="store_true", help="actually merge into the config file")
    p_install.add_argument("--skill", action="store_true", help="also install the Agent Skill")
    p_install.add_argument("--skills-root", help="client skills directory relative to home")
    p_install.set_defaults(func=cmd_install)

    p_sync = sub.add_parser("sync-skill", help="sync packaged Skill into a source checkout")
    p_sync.set_defaults(func=cmd_sync_skill)

    p_serve = sub.add_parser("serve", help="run the MCP server over stdio")
    p_serve.set_defaults(func=cmd_serve)

    if not argv:
        return cmd_serve(argparse.Namespace())
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
