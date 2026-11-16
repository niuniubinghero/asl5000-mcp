# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- `asl_vendor_scan` / `asl_vendor_read` / `asl_vendor_export` (MCP tools 23–25):
  read-only parsing of the vendor recording groups via `vendor_data.py` —
  scan `ASLdata` directories (optional name filter, shallow/deep), inspect column
  labels / per-column stats / preview rows for `.rwb/.dtb/.brb/.avb`, and export
  selected columns to UTF-8-BOM CSV with an overwrite guard. The tool wrappers live
  in `server.py`; the reusable logic (`select_columns`, `stats_block`,
  `preview_block`) is in `vendor_data.py` and covered by the new hardware-free
  `tests/test_vendor.py` (93 tests total).
- `tools/make_lung_model.py --sct-only`: generate only the matching `.sct` wrapper
  for an already existing `.vr3`. `--template` is conditionally optional, the model
  file is never rewritten (bytes and mtime untouched, asserted by tests), existing
  wrappers are skipped unless `--force` (per-item `"force": true`), and
  `{"sct_only": true, "out": ...}` works in `--from-json` batch mode. 7 new tests
  in `TestSctOnly`.
- `asl_prepare_recording` (22nd MCP tool): read-only preflight for the vendor recording
  data group. Returns a recommended `<script>_<timestamp>` name, resolves the output
  directory, detects an existing same-named group (file count, size, header-only check)
  and offers overwrite / rename / `_N` suffix choices with ready-to-use `asl_run` kwargs.
- `src/asl_monitor/recording.py`: hardware-free naming, path resolution and conflict
  decision helpers, plus 33 new hardware-free unit tests (72 total).
- Documentation of the vendor's own `.dtb/.rwb/.brb/...` recording group and the fact
  that `.dtb` carries `Total Flow (L/min)`, absent from the 6343 broadcast CSV.

### Changed
- `asl_run` keeps `wf=1` by default (the vendor `RS` default `WF=0` saves no waveform
  files) and now defaults to `ow=0`: a same-name conflict returns `need_decision`
  without sending `RS` until the user explicitly picks overwrite / rename / suffix.
- `asl_export_waveform` follows the last successful run's NAME instead of the vendor
  default `data`, which otherwise pointed at the wrong data group.
- MCP instructions, the Skill (all three copies), README and PROTOCOL now require the
  Agent to ask the user for the recording name, conflict strategy and output directory
  before every run.
- Renamed `tools/make_vr3.py` to `tools/make_lung_model.py`. The old name implied the
  tool only produced `.vr3` files, while it also writes optional `.sct` scenario
  wrappers and will gain a `--sct-only` mode. All command examples, the Skill copies,
  the TODO entry and the hardware-free tests were updated. No compatibility alias is
  kept because 0.1.0 has not been published yet.

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
- `tools/make_vr3.py` (now `tools/make_lung_model.py`): offline generator for `.vr3`
  lung models plus the matching `.sct` wrapper required by `asl_run` (single model
  and JSON batch modes).
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
  generator; it now documents `tools/make_lung_model.py` (then `tools/make_vr3.py`) /
  `tools/build_script_index.py`, the need for
  a locally supplied `.vr3` template, and the requirement to write output under `vars/`.

### Notes
- This is an independent, unofficial client. It does not include ASL software,
  the TAI server, vendor documentation, or any lung-model/script files.
