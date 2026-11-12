# Communication notes / 通信说明

> These are implementation notes from our own client, written in our own words. They are **not** the vendor specification. For the authoritative command and parameter reference, consult `documentation/TAI Specification.pdf` shipped with your ASL installation. This repository does not redistribute that document.

The client talks to two independent parts of the vendor software. They do not depend on each other, which is why monitoring works even before a control session is established.

## Channels

| Channel | Default | Direction | Framing | Used by |
|---|---|---|---|---|
| TAI control | `127.0.0.1:6341` | request / response, synchronous | Plain ASCII lines with CRLF; the client auto-detects an optional 4-byte big-endian length prefix | `tai.py` |
| Breath parameters | `6342` | broadcast, once per breath | 4-byte big-endian length header, then TAB separated `name<TAB>value` lines | `monitor.py` |
| Waveform stream | `6343` | broadcast, continuous | 4-byte big-endian length header, then TAB separated rows; first line is the column header | `monitor.py` |

The control host/port are configurable with `ASL_TAI_HOST` / `ASL_TAI_PORT`. Broadcast ports follow the vendor software and are fixed in this client.

## Control command shape

Each control command is a two-letter mnemonic followed by `name=value` pairs, terminated by CRLF. A successful reply contains an echo, optional data lines, and a fresh prompt; a failure contains `ERROR(YY)` plus a short reason. Commands are **strictly synchronous**: wait for the reply before sending the next one. The client retries once automatically on `ERROR(11)` ("previous command unfinished").

Mnemonics used by the 21 MCP tools (described functionally, not copied from the vendor document):

| Tool | Purpose |
|---|---|
| `asl_connect` | Open an ASL software instance (`OA`). Must be the first control command after connecting |
| `asl_load_script` | Select a scenario file (`SF`) |
| `asl_run` | Start a simulation (`RS`), optionally recording waveforms |
| `asl_stop` / `asl_exit` | End the simulation (`ES`) / close the software and release the COM port (`EX`) |
| `asl_tai_status` | Query run state, elapsed/remaining time, breath count and versions (`TC`) |
| `asl_query_breath_params` | Per-breath analysis values (`BP`); the client supplies a CPAP-oriented default set |
| `asl_export_waveform` | Export one full breath as TAB separated text (`BW`), optionally raw ADC |
| `asl_init` | Simulation initialisation: FRC, connector resistance, tubing compensation (`SI`) |
| `asl_aux` | Auxiliary circuit compliance / inspiratory and expiratory resistance (`AC`) |
| `asl_set_interactive` | Enter / leave interactive mode (`IM` / `SM`) |
| `asl_set` | Change lung model, spontaneous effort or closed-loop targets while running (`IC`) |

The vendor interface has no command to create or save `.vr3` lung models, and interactive parameter changes are not persisted to disk. That is why `.sct` wrappers and external model files matter — see the Skill's section 5.

## Broadcast parsing

- `parse_breath_params` turns one `6342` message into a `{name: value}` dictionary.
- `parse_wave_message` splits a `6343` message into a header and rows, skipping repeated header lines inside a message.
- `wave_window_stats` computes PIP / PEEP / VT / Flow statistics over a recent time window, handling simulator time resets.

Column names come from the vendor software, so the parsers locate columns by keyword (`time`, `flow`, `pressure`, `volume`) rather than by fixed index.

## Safety notes for implementers

- Never parallelise control commands. Broadcast/monitoring calls are independent and may run concurrently.
- `EX` can crash the vendor software (see [TROUBLESHOOTING](TROUBLESHOOTING.md)); verify the real state afterwards.
- Do not log to stdout in an MCP stdio server; the client redirects all diagnostics to stderr.
