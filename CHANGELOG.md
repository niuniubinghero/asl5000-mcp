# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - 2026-11-12

### Added
- `asl-monitor` MCP server exposing 21 tools for ASL 5000 environment checks,
  broadcast monitoring, TAI control, per-breath analysis and live parameter changes.
- `asl-lung-sim` Agent Skill with the operating procedure, hard constraints and
  error-code reference.
- `asl-monitor install` CLI supporting pip-mode and source-mode installation for
  WorkBuddy, CodeBuddy, Claude Desktop, Cursor, Trae, Qoder and Codex.
- `asl-monitor check` / `asl-monitor paths` for environment self-check and
  machine-specific path configuration.
- Hardware-free unit tests for path discovery, TAI frame parsing and broadcast parsing.
- `tools/make_vr3.py`: offline generator for `.vr3` lung models plus the matching
  `.sct` wrapper required by `asl_run` (single model and JSON batch modes).
- `tools/build_script_index.py`: 32-column CSV index of the vendor `vars/` and
  `breath/` script libraries, including broken-reference and missing-wrapper checks.
- `tools/_paths.py`: shared install-root discovery (`--asl-root`, `ASL_ROOT`,
  `ASL_SW_PATH`); no machine-specific path in any tool.
- 15 additional hardware-free tests for the two offline tools (38 total).

### Changed
- Machine-specific absolute paths removed from the package. ASL software and TAI
  server locations are discovered from environment variables, a user config file,
  or common install locations, and can be supplied at runtime with `asl_set_paths`.
- Recordings are written to a user data directory instead of the package directory.

### Notes
- This is an independent, unofficial client. It does not include ASL software,
  the TAI server, vendor documentation, or any lung-model/script files.
