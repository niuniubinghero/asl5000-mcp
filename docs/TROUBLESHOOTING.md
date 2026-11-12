# Troubleshooting / 排错

## Error codes from the TAI control channel

| Code | Meaning | Action |
|---|---|---|
| 03 | Command not available in the current state | `OA` → close any manually opened ASL window (COM port busy). `TC`/`BP` → call `asl_connect` first. **`RS` → a simulation is already running**; call `asl_get_status` and decide whether to reuse it or `asl_stop` |
| 05 | Illegal parameter value | Check `id` (COM1–8 / DEMO / serial number) and parameter names |
| 08 | Command timeout | The command **may already have taken effect**. Do not blindly retry. Verify with `asl_tai_status` / `asl_check` |
| 11 | Previous command not finished | The client reconnects and retries once automatically; if it persists, wait a few seconds |
| 13 | File exists / write failed | Pass `ow=1` to `asl_run` or choose another `name` |
| 14 | Script file not found (SF) | Check spelling and path separators (`\VARS\<subdir>`). For `.vr3` models see the `.sct` wrapper note below |
| 15 | TC: ASL software was closed externally | Normal confirmation that ASL exited and the COM port was released; reconnect with `asl_connect` |
| 16 | Cannot set parameter | Enter interactive mode with `asl_set_interactive(True)` first; the current lung model may not support that parameter |
| 18 | Parameter out of range | Breath number exceeds the total; use `-1` for the last breath |

## `asl_exit` (EX) can crash the vendor software

Observed behaviour on some installations: `EX` may return a timeout while ASL 5000 crashes with an access violation. When that happens the host **can keep running the loaded script autonomously** — the piston still moves, but the PC has no ASL instance (only `ASL_TAI.exe` remains; `TC` reports 03 or 15; broadcast ports 6342/6343 are gone).

Recovery is fixed and must be followed:

1. `asl_connect` (OA) — reconnect to the host.
2. `asl_stop` (ES) — stop the simulation.
3. `asl_tai_status` (TC) — confirm IDLE.

Never assume `EX` returning `ok` or timing out means the hardware stopped. Prefer stopping at IDLE and keeping the connection; only call `asl_exit` when the user explicitly asks to release the serial port.

## Common issues

| Symptom | Cause | Fix |
|---|---|---|
| `asl_env_check` reports software/TAI not installed | Auto-discovery could not find the vendor exes | Run `asl-monitor paths --sw ... --tai ...`, or let the Agent call `asl_set_paths` |
| All three ports offline | ASL software is not running | Normal. `asl_tai_start` + `asl_connect` bring up the control and broadcast ports |
| Simulator is on COM16 / above COM8 | TAI only accepts COM1–COM8 | Renumber the port in Windows Device Manager |
| Recordings cannot be written | Package installed into a read-only location | Recordings go to a user data directory by default; override with `ASL_MONITOR_RECORDINGS` |
| `asl_run` reports ERROR(14) for a `.vr3` | `RS` looks up `<name>.sct` internally | Create a same-name `.sct` wrapper in the same directory (single line, TAB separated, CRLF): `1000\t<ASLVarsDirectory>\<subdir>\<name>.vr3\r\n`, then load by name without extension |
| `asl_set` reports ERROR(16) | Interactive mode not active, or the parameter is unsupported by the loaded model | `asl_set_interactive(True)` first; check the IC parameter list in the vendor TAI specification |
| Agent issues control commands in parallel | TAI is synchronous | Skills enforce serial control calls; monitoring tools may run any time |
| Config snippet seems ignored after editing `mcp.json` | Client caches MCP servers | Restart the client; WorkBuddy may also need the server to be trusted in the connector panel |

## Getting useful diagnostics

```bash
asl-monitor check                 # paths, ports, COM ports, script count
```

In an Agent session, `asl_env_check(detail_dir="scenarios")` lists every script in a `vars/` subdirectory. `asl_get_status` takes a live sample without writing files; `asl_snapshot(wave_sec=10)` writes a comparable snapshot before/after a parameter change.
