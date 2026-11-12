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
- `.sct` wrapper generation is documented as explicitly optional (`--sct`, off by
  default). README, tools/README, the CLI help and both Skill copies now distinguish
  the optional generation step from the wrapper that `asl_run` requires at runtime.
  A `--sct-only` mode is recorded as a TODO in CONTRIBUTING.md (not implemented).

### Fixed
- `build_script_index.py`: the `.sct` first column is a repetition count, not seconds. It was
  previously summed as seconds and divided by 60 into a misleading `sct总时长(分)` column
  (a single-row wrapper with `1000` showed `16.7` minutes). The column is now
  `sct首列合计(重复次数)` and reports the integer sum. Column count stays at 32.
- `asl-lung-sim` Skill: removed the stale claim that this repository ships no batch model
  generator; it now documents `tools/make_vr3.py` / `tools/build_script_index.py`, the need for
  a locally supplied `.vr3` template, and the requirement to write output under `vars/`.

### Notes
- This is an independent, unofficial client. It does not include ASL software,
  the TAI server, vendor documentation, or any lung-model/script files.
