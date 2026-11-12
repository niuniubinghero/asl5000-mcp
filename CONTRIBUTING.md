# Contributing

## Development setup

```bash
git clone https://github.com/niuniubinghero/asl5000-mcp.git
cd asl5000-mcp
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -e ".[dev]"
python -m unittest discover -s tests -v
```

The tests use mocked sockets only. No ASL hardware, ASL software or TAI server is
required. The `mcp` dependency is imported lazily by the server module, so the
unit tests also run without it.

## Layout

| Path | Purpose |
|---|---|
| `src/asl_monitor/` | Installable Python package (server, monitor, TAI client, config, CLI) |
| `src/asl_monitor/data/SKILL.md` | Authoritative copy of the Agent Skill, shipped in the wheel |
| `skill/asl-lung-sim/SKILL.md` | Identical copy for manual installation / browsing |
| `configs/` | Zero-absolute-path MCP snippets for manual copy-paste (pip mode) |
| `docs/` | Installation, troubleshooting and protocol notes |
| `tests/` | Hardware-free unit tests |

**Keep the two SKILL.md copies identical.** After editing either one, sync them:

```bash
python -m asl_monitor.cli sync-skill     # from a source checkout with src/ on PYTHONPATH
```

or simply copy `src/asl_monitor/data/SKILL.md` to `skill/asl-lung-sim/SKILL.md`.

## Rules for changes

- Never commit machine-specific absolute paths, local `config.json`, recordings,
  `.vr3`/`.sct` lung models, or any vendor documentation.
- Do not redistribute ASL software, `ASL_TAI.exe`, or the vendor TAI specification.
  Describe the protocol in our own words in `docs/PROTOCOL.md` instead.
- TAI control commands are synchronous: never parallelise them in the server.
- Any new behaviour written into `SKILL.md` must be marked with a verified date;
  unverified guesses go under a clearly labelled "to verify" note.
- Add a hardware-free test for every parser / config change.

## Pull requests

Describe the hardware/firmware/ASL version you tested against (if any), include
the `asl-monitor check` output with paths redacted, and note any change to the
21 MCP tool signatures — downstream Skills depend on them.
