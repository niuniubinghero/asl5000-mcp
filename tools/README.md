# Offline tools / 离线工具

Two standalone scripts. They do **not** talk to the simulator and are not part of the 22 MCP tools.
They run straight from a source checkout with any Python >= 3.10 (standard library only).

| Script | Job | Output |
|---|---|---|
| `make_lung_model.py` | Build new lung models from a `.vr3` template | `.vr3`, plus a matching `.sct` wrapper only when `--sct` is given |
| `build_script_index.py` | Index the vendor script library | 3 searchable CSV files |

`_paths.py` is shared path discovery. All three are independent of `asl_monitor` and are shipped in
the source distribution (sdist), but are **not** installed as console commands by the wheel.

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

# Batch mode
python tools/make_lung_model.py --template "<ASL_ROOT>/vars/scenarios/Adult_normal.vr3" --from-json batch.json
```

`batch.json` is an array of `{"out": ..., "set": {...}, "sct": true, "reps": 1000}` objects. The
`sct` key is optional: omit it (or leave out `--sct`) and only the `.vr3` is written. Use
`--outdir <dir>` to write somewhere other than the vendor `vars/` directory (the `.sct` then
references an absolute path; keeping files under `vars/` is recommended).

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

## Notes

- These tools read and write **vendor file formats only**. They do not include any `.vr3`, `.sct`,
  `.in`, vendor executable or vendor documentation. You must have a legitimate ASL 5000 install.
- Generated CSVs, models and scenario scripts are local data. They are git-ignored and must not be
  committed to this repository.
- Not a medical device. Research / test automation only.
