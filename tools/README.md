# Standalone tools / 独立工具

Standalone scripts, **not** part of the 25 MCP tools. They run straight from a source checkout with
any Python >= 3.10 (standard library only). Most are offline; `live_wave_view.py` is the exception —
it listens to the simulator's live TAI broadcast while a test is running.

| Script | Job | Output |
|---|---|---|
| `make_lung_model.py` | Build new lung models from a `.vr3` template | `.vr3`, plus a matching `.sct` wrapper only when `--sct` is given |
| `build_script_index.py` | Index the vendor script library | 3 searchable CSV files |
| `build_wave_view.py` | Parse vendor `.rwb/.dtb/.brb` recording groups (**after** the run) | Self-contained offline ECharts HTML |
| `live_wave_view.py` | Live TAI broadcast / CSV tail / replay (**during** the run) | Local HTTP dashboard with ECharts |

`_paths.py` is shared path discovery. The first two scripts are independent of `asl_monitor`;
`build_wave_view.py` uses the read-only parser in `src/asl_monitor/vendor_data.py`, and
`live_wave_view.py` reuses `src/asl_monitor/monitor.py` for the broadcast frame decoder. All are
shipped in the source distribution (sdist), but are **not** installed as console commands by the wheel.

## Why they exist

TAI exposes ~20 commands but **cannot create or save `.vr3` files**, and parameters changed during a
run (IC/IM / `asl_set`) are never written back to disk. Meanwhile a real install easily has 1000+
`.vr3` / `.sct` / `.in` files. So: one tool creates model variants offline, the other finds scripts
without crawling directories by hand.

Data flow:

```
make_lung_model.py ──> model.vr3 [+ model.sct when --sct] ──> asl_load_script ──> asl_run ──> asl_set / asl_query_breath_params
build_script_index.py ──> script_index.csv ──> pick a scenario ──> asl_load_script
```

A bare `.vr3` cannot be loaded: `asl_run` expects an `.sct` scenario script (`ERROR(14)` otherwise).
That is a **runtime** requirement, not a generation requirement — writing the wrapper is optional
at generation time (`--sct`, off by default) and only needed when you intend to load the model
through `asl_load_script` / `asl_run` right away.
The first column of `.sct` is a repetition count (default 1000) — **not** a breath count and not a
number of seconds (section 6.3 of the vendor manual). The index therefore reports the summed
repetition count, not a duration.

## make_lung_model.py

Named for what it builds (a lung model and, optionally, the scenario script that loads it),
not for a file extension: outputs are `.vr3` models plus optional `.sct` wrappers.

`.vr3` is a plain TAB-separated variable table. The generator changes **only** the value field to the
right of `=`; TAB alignment, spaces, CRLF line endings and non-ASCII bytes are preserved byte-for-byte
(latin-1 round trip). Floats are written with 6 decimals to match the vendor style.

```bash
# List every variable in a template (no install root needed)
python tools/make_lung_model.py --template "<ASL_ROOT>/vars/scenarios/Adult_normal.vr3" --list

# Model only (default: no .sct is written)
python tools/make_lung_model.py --template "<ASL_ROOT>/vars/scenarios/Adult_normal.vr3" \
  --out "<scenario-dir>/hme_r21.vr3" --set r_trachea_in=21

# Model + its .sct wrapper (add --sct; relative --out resolves under <ASL_ROOT>/vars)
python tools/make_lung_model.py --template "<ASL_ROOT>/vars/scenarios/Adult_normal.vr3" \
  --out "<scenario-dir>/hme_r21.vr3" \
  --set r_trachea_in=21 --set r_trachea_out=23 --set spontaneous_breathing_rate=18 --sct

# Only the missing .sct wrapper for an ALREADY EXISTING .vr3 (--sct-only: no template
# needed; the model's bytes and mtime are untouched). Skips an existing wrapper
# unless --force. Also available per-item in batch mode: {"sct_only": true, "out": ...}
python tools/make_lung_model.py --sct-only --out "<scenario-dir>/legacy.vr3"
python tools/make_lung_model.py --sct-only --out "<scenario-dir>/legacy.vr3" --force

# Batch mode
python tools/make_lung_model.py --template "<ASL_ROOT>/vars/scenarios/Adult_normal.vr3" --from-json batch.json
```

`batch.json` is an array of `{"out": ..., "set": {...}, "sct": true, "reps": 1000}` objects. The
`sct` key is optional: omit it (or leave out `--sct`) and only the `.vr3` is written. Use
`--outdir <dir>` to write somewhere other than the vendor `vars/` directory (the `.sct` then
references an absolute path; keeping files under `vars/` is recommended).

`--sct-only` (or the per-item `"sct_only": true`) switches a job to wrapper-only mode: the target
`.vr3` must already exist, no template is read and the model file is never rewritten (its bytes and
mtime stay untouched — verified by tests). An existing `.sct` is skipped unless `--force`
(per-item `"force": true`); `--set` is rejected in this mode.

Typical use: sweep airway resistance / compliance / breathing rate for a CPAP or HME compensation
study, then load each variant through `asl_load_script` and compare waveforms.

## build_script_index.py

Scans `<ASL_ROOT>/vars` and `<ASL_ROOT>/breath`, parses naming conventions (BPM, I:E, R, C, P, Pmax,
FL, VT, MV, event count) plus file contents (`.sct` segments/summed repetition counts/referenced
models and whether those references resolve; `.vr3` rate/resistance/compliance/time-varying trajectory; `.in` sample
count and value range) and produces a 32-column index.

```bash
python tools/build_script_index.py --asl-root "<ASL_ROOT>" --out-dir ./my-index
python tools/build_script_index.py --official-before 2019-01-01
```

Outputs (UTF-8 BOM so Excel/WPS opens them correctly):

| File | Contents |
|---|---|
| `script_index.csv` | all scripts |
| `script_index_official.csv` | vendor-supplied scripts |
| `script_index_custom.csv` | user-created + runtime-generated scripts |

Default output is `tools/output/`, which is git-ignored. The official/custom split uses directory
names plus an install-date cutoff inferred from the ASL exe mtime; override it with
`--official-before`. Classification is heuristic, not an authoritative label.

The index also acts as a library health check: `.sct` files with broken `.vr3` references, `.vr3`
models missing their `.sct` wrapper, suspected duplicates and filenames with stray spaces all show up
in dedicated columns.

## Install root discovery

Both scripts accept `--asl-root <dir>`. Without it, `_paths.py` resolves in this order:

```
--asl-root  >  $ASL_ROOT  >  parent of $ASL_SW_PATH  >  installed asl_monitor.config
            >  common install locations  >  actionable error message
```

No drive letter, user name or absolute install path is baked into the source.

## Vendor recording viewer (`build_wave_view.py`)

```bash
python tools/build_wave_view.py                                  # default 3-group CPAP comparison
python tools/build_wave_view.py --names SIN10BPM SIN15BPM SIN20BPM
python tools/build_wave_view.py --all                           # every group (large HTML)
python tools/build_wave_view.py --data-dir <ASLdata> --out view.html
```

The ASLdata directory is auto-discovered via `_paths.py`, or set `$ASL_DATA_DIR` / `--data-dir`.
`tools/assets/echarts.min.js` is inlined so the result works offline with no CDN.

Vendor files are **self-describing**: the leading ASCII header states the header length, byte order
and column labels. The data body is a row-major 2D array of **big-endian float32** (using the wrong
endianness yields ~1e20 garbage). `src/asl_monitor/vendor_data.py` parses it with no dependencies:

| File | Header | Body | Content |
|---|---|---|---|
| `.rwb` | 500 B | N x 13 | Raw waveforms incl. `Time (sec)` |
| `.dtb` | 500 B | N x 16 | Same rows as `.rwb`, adds `Total Flow (L/min)` |
| `.brb` | 4096 B | M x 107 | Per-breath derived parameters |
| `.avb` | 2000 B | K x 93 | Model / setting snapshots |
| `.aux`, `.vol` | ASCII | TSV | Usually header only |
| `.tdms*` | TDMS | — | Metadata only, no waveform data |

Two vendor quirks matter in practice:

- `.dtb`'s 500-byte header is **truncated** — the last visible label is `Trac`, while the body has
  16 columns. Col 12 is unverified and col 13–15 are unlabeled duplicates/derivatives. The parser
  keeps them as `dtb_colNN` instead of inventing physical names; the viewer only exposes the
  header-confirmed channels.
- The sample rate is **not constant** across sessions (64 / 128 / 512 Hz observed). Always use
  `.rwb` col 0 `Time (sec)` as the X axis; never assume a fixed dt.

The viewer supports group/channel toggles, three X alignments (absolute time / within-breath time /
breath phase 0–100%), unit switches, wheel + box + slider zoom, crosshair readout, live window
statistics, a sortable per-breath table, CSV/PNG export and light/dark themes. Waveforms are
embedded as base64 float32 at full resolution — no downsampling.

## Live waveform viewer (`live_wave_view.py`)

Use this **while the simulator is running**. It watches the same TAI broadcast that the MCP
recorder taps:

```
ASL5000_SW3.6.exe
  ├─ TCP 6342  per-breath params (ASCII, one "name<TAB>value" per line)
  └─ TCP 6343  continuous waveform (TAB separated, header line, 64/128/512 Hz)
        │        each frame prefixed by a 4-byte BIG-ENDIAN int32 length
        └─ live_wave_view.py ──> http://127.0.0.1:8765  (ECharts)
```

```bash
python tools/live_wave_view.py                                   # direct broadcast (recommended)
python tools/live_wave_view.py --tail <recordings/.../waveform_001.csv>   # follow MCP recorder
python tools/live_wave_view.py --replay <waveform.csv> --speed 8 --no-browser  # offline check
python tools/live_wave_view.py --port 9000 --no-browser
```

Three data sources cover three situations: direct broadcast (no MCP recorder needed; multiple TCP
clients are allowed), tailing the CSV that `asl_start_recording` is writing, and replaying a past
CSV to reproduce / verify a session without hardware. The browser polls `/api/data?cursor=N` for
incremental rows; `/api/meta`, `/api/export?sec=N` expose channel metadata and CSV download.

Interaction: default **three panels** mirroring the Run Time Home layout — Pressure (all 5 pressure
traces), Flow (derived `Total Flow` + `Chamber 1/2 Flow`), Volume (3 traces) — plus a "＋ 添加波形面板"
button for unlimited extra panels. Every panel can toggle any channel via chips, has an editable
title (click it) and a ✕ close button; all panels share connected zoom/crosshair via
`echarts.connect`. Follow window 5/10/30/60 s or the whole buffer, pause with Space (data keeps
arriving in the background) then freely wheel/drag zoom, cmH₂O↔hPa, L/min↔mL/s and mL↔L unit
switches, breath-boundary mark lines, crosshair readout, live numeric cards from the 6342 breath
parameters, CSV export (derived flow columns included), light/dark themes. ECharts is inlined, so
no CDN is needed.

**Derived flow channels:** the 6343 broadcast has no flow columns, so the Hub computes
`Total/Chamber 1/2 Flow = dV/dt` (backward difference at the native sample rate + light EMA
smoothing). Validated against the 6342 per-breath `Peak Insp. Flow` (~27 L/min both) on a real
recording. Use the derived curves for shape inspection; for quantitative peak flow prefer the 6342
breath parameters or the vendor `.dtb`.

**Important differences from the vendor files:**

| | 6343 broadcast / recorder CSV | Vendor `.rwb` / `.dtb` |
|---|---|---|
| When | during the run | written by ASL software at the end of the run |
| Encoding | ASCII TSV | 500 B ASCII header + big-endian float32 |
| Flow column | **absent** | `Total Flow (L/min)` present in `.dtb` |
| Volume units | `mL` | `.rwb` labels them **`L`** (1000x smaller numbers); `.dtb` uses `mL` |
| Extra columns | `Recv Time`, `Breath Detection (#)` | — |

Row counts, timestamps, pressures, Aux and O₂ values are identical between the broadcast CSV and
`.rwb` (verified point-by-point on a 34 453-row session); only the volume scaling and the two extra
columns differ.

### Sample rate: broadcast == `.rwb`

Yes — for a given run they are the **same samples**, not a resampled copy. On the 34 453-row
11/13 session, the `Time (sec)` columns match with max diff `0.0`, both span 67.289550781 s,
both have median step `0.001953125 s` (= 1/512) and the same step histogram; pressures/Aux/O₂
differ only by float32→text round-off (~1e-15).

But the rate is **set per script, not fixed**: across the 19 local `.rwb` groups with waveforms,
11 run at 64 Hz, 3 at 128 Hz, 5 at 512 Hz. So "same as `.rwb`" means "same as that run's `.rwb`",
not "always 512 Hz".

Do **not** derive the rate from arrival timing. 6343 pushes ~100 rows per framed message: 34 120 of
34 452 `Recv Time` gaps are exactly zero, with 332 batch gaps >100 ms (max 0.442 s). `Recv Time` is a
wall-clock stamp for the whole batch, not a sample clock. Replay `--speed` changes arrival rate and
must not change the reported sample rate. `/api/meta.sample_rate_hz` therefore uses the reciprocal of
the median positive `Time (sec)` step over the last 1024 rows (512.0 Hz for this session); the badge
shows that value.

Minor real jitter: 34 445 steps are 1/512 s, 4 are 0.002441406 s and 3 are 0.001464844 s
(float32 quantization / clock jitter). Total-duration averaging gives 511.996 Hz; nominal is 512 Hz.

The live viewer deliberately does **not** draw a flow curve: a curve derived from `dV/dt` does not
reproduce the vendor `Total Flow` (it is not a plain derivative), and 6342 already provides real
peak inspiratory/expiratory flow per breath.

## Notes

- These tools read and write **vendor file formats and vendor broadcast streams only**. They do not include any `.vr3`, `.sct`,
  `.in`, vendor executable or vendor documentation. You must have a legitimate ASL 5000 install.
- Generated CSVs, models and scenario scripts are local data. They are git-ignored and must not be
  committed to this repository.
- Not a medical device. Research / test automation only.
