# Installation guide / 安装指南

## 1. Prerequisites / 前置条件

| Item | Notes |
|---|---|
| OS | Windows (broadcast parsing and COM enumeration are Windows-oriented; other platforms degrade gracefully but the vendor software only runs on Windows) |
| Python | >= 3.10 |
| Vendor software | ASL 5000 software (`ASL5000_SW3.6.exe` or equivalent) **and** ASL Test Automation Interface (`ASL_TAI.exe`) |
| Hardware link | Serial port COM1–COM8, or `DEMO` mode without hardware |
| Python package | `mcp>=1.2` (installed automatically by `pip install asl5000-mcp`) |

The vendor software, the TAI server, `documentation/TAI Specification.pdf`, `.vr3` lung models and `.sct` scripts are **not** included in this repository.

## 2. Mode A — pip install (recommended)

```bash
pip install asl5000-mcp
# or straight from a checkout / git URL
pip install git+https://github.com/niuniubinghero/asl5000-mcp.git
pip install -e ".[dev]"          # from a source checkout, for development
```

Then register the MCP server with your client:

```bash
asl-monitor install --client workbuddy --write
asl-monitor install --client claude  --write
```

Supported `--client` values: `workbuddy`, `codebuddy`, `claude`, `cursor`, `trae`, `codex`, `qoder`.

| Client | Config file | Notes |
|---|---|---|
| WorkBuddy | `~/.workbuddy/mcp.json` | May require trusting the server in the connector panel |
| CodeBuddy | `~/.codebuddy/mcp.json` | |
| Claude Desktop | `%APPDATA%/Claude/claude_desktop_config.json` (Windows), `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) | |
| Cursor | `~/.cursor/mcp.json` | |
| Trae | `~/.trae/mcp.json` | Use `configs/cursor.mcp.json` — the snippet is identical |
| Codex | `~/.codex/config.toml` | TOML output; the command prints a snippet to append, it does not rewrite the file |
| Qoder | MCP settings page | No standard config path; paste the printed snippet |

The snippets in `configs/` use the console-script name and contain no absolute paths:

```json
{ "mcpServers": { "asl-monitor": { "command": "asl-monitor", "args": [] } } }
```

On your own machine `asl-monitor install` resolves that name with `shutil.which`, falling back to the user scripts directory (important on Windows when `pip --user` installs into a `Scripts` folder that is not on PATH). If it resolves to a full path, that path is what gets written into **your** client config — the repository snippets stay portable. `install --write` keeps a `.bak` of the original config. Without `--write` it only prints (dry run). **Restart the client** so it reloads MCP servers.

## 3. Mode B — source / no install

```bash
git clone https://github.com/niuniubinghero/asl5000-mcp
cd asl5000-mcp
pip install "mcp>=1.2"
asl-monitor install --client cursor --mode source --write
```

In source mode the CLI resolves `<checkout>/run_server.py` and uses `sys.executable` as the command. If the checkout is elsewhere, pass `--repo <checkout dir>` and optionally `--python <python exe>`.

You can also start the server by hand (useful for debugging):

```bash
python run_server.py
```

This speaks the MCP stdio protocol on stdout — do not run it interactively and expect a prompt.

## 4. Verify

```bash
asl-monitor check
```

Expected when the ASL software is not running: `asl_software` / `tai_server` resolved to real paths, `recordings_writable: true`, all three ports offline (normal), `com_ports` listing your serial devices, `ready: true`.

Then, from your Agent: *"check the lung simulator environment"* → the `asl_env_check` tool should return `ready=true` plus a COM port list and script inventory.

### If paths are not resolved

```bash
asl-monitor paths --sw "<full path to ASL5000_SW3.6.exe>" --tai "<full path to ASL_TAI.exe>"
```

or, inside an Agent session, let it call `asl_set_paths`. The config is written to a user-level directory (see `config_file` in `asl-monitor check` output), never into the repository.

Priority: environment variables > user config file > auto-discovery. Useful variables:

| Variable | Meaning |
|---|---|
| `ASL_SW_PATH` | ASL software exe |
| `ASL_TAI_EXE` | TAI server exe |
| `ASL_ROOT` | Extra install root to probe |
| `ASL_TAI_HOST` / `ASL_TAI_PORT` | TAI control address (default `127.0.0.1:6341`) |
| `ASL_MONITOR_CONFIG` | Override the user config file location |
| `ASL_MONITOR_RECORDINGS` | Override the recordings directory |

## 5. Install the Agent Skill

```bash
asl-monitor install --client workbuddy --skill --skills-root .workbuddy/skills --write
```

or copy `skill/asl-lung-sim/SKILL.md` into your client's skills directory:

| Client | Skills directory |
|---|---|
| WorkBuddy | `~/.workbuddy/skills/asl-lung-sim/` |
| Claude / Claude Code | `~/.claude/skills/asl-lung-sim/` |
| Cursor | `~/.cursor/skills/asl-lung-sim/` |
| CodeBuddy | `~/.codebuddy/skills/asl-lung-sim/` |

Skill changes only take effect in **new** sessions; a currently loaded conversation keeps its old copy.

## 6. Run the tests

```bash
python -m unittest discover -s tests -v
```

Tests use mocked data only. No hardware, ASL software or TAI server is needed.
