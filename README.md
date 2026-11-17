# asl5000-mcp

**让任意 AI Agent 直接操控 ASL 5000 模拟肺** —— 一个 [MCP](https://modelcontextprotocol.io/) 服务器 + 一份 Agent Skill。

[English](#english) | [中文](#中文) | [License: MIT](#license)

> **Unofficial / 非官方项目。** 本仓库只包含我们自行实现的通信客户端，不包含 ASL 软件、`ASL_TAI.exe`、厂商 TAI 规范文档或任何肺模型文件。使用本项目需要你已合法安装 ASL 5000 软件与 Test Automation Interface。

---

## English

Talk to your Agent in plain language: *"connect to the lung simulator, run the Adult_normal scenario and compare PEEP before/after setting C1 to 50"*. The Agent calls `asl-monitor` MCP tools; the `asl-lung-sim` Skill keeps it from doing dangerous things.

### What you get

| Category | Tools | Purpose |
|---|---|---|
| Setup | `asl_env_check`, `asl_set_paths` | Detect ASL software / TAI server, list COM ports and the `vars/` script library |
| Passive monitoring | `asl_check`, `asl_get_status`, `asl_snapshot`, `asl_start/stop_recording`, `asl_list_recordings` | Breath parameters + waveform statistics, snapshots, long-running CSV recording |
| Recording preflight | `asl_prepare_recording` | Read-only naming / directory / same-name conflict check before every run |
| Active control | `asl_tai_start`, `asl_connect`, `asl_load_script`, `asl_run`, `asl_stop`, `asl_exit`, `asl_tai_status` | Start TAI, open ASL, load a scenario, run/stop a simulation |
| Analysis | `asl_query_breath_params`, `asl_export_waveform` | Per-breath parameters (PIP/PEEP/AutoPEEP/Vt/trigger delay/work of breathing), full waveform export |
| Configuration | `asl_init`, `asl_aux`, `asl_set_interactive`, `asl_set` | FRC / tubing compensation, auxiliary circuit, live changes to lung mechanics and closed-loop targets |
| Vendor data (read-only) | `asl_vendor_scan`, `asl_vendor_read`, `asl_vendor_export` | Scan the `ASLdata` recording groups; column labels, per-column stats and previews (.rwb/.dtb/.brb/.avb); CSV export |

25 tools in total. The Skill encodes the operating order, hard constraints (serial control commands, never call `asl_exit` unless asked, confirm before moving the piston) and the error-code recovery table.

### Vendor recording files (kept by default)

The vendor `RS` command defaults to `WF=0`, which saves **no** waveform files. This client
always starts simulations with `wf=1`, so the ASL software writes its own data group
(`<NAME>.dtb`, `.rwb`, `.brb`, `.avb`, `.aux`, `.vol`, `.tdms`, `.tdms_index`) into
`<ASL_ROOT>\ASLdata\` — the same files the ASL GUI produces, and a different dataset from
the broadcast CSV recordings. `.dtb` carries `Total Flow (L/min)`, which the 6343
broadcast stream does not include.

Before every `asl_run`, call `asl_prepare_recording` (read-only, no TAI command) and let the
user decide: the recording name (recommended `<script>_<timestamp>` or custom), the output
directory (default `\ASLDATA` or custom), and — if a same-named group already exists —
overwrite / rename / append `_1`. `asl_run` refuses to send `RS` on a conflict unless the
user explicitly chose overwrite. `asl_export_waveform` follows the last successful run's
name automatically.

### Requirements

- Windows with **ASL 5000 software** and **ASL Test Automation Interface (TAI)** installed (vendor products, not included).
- Python >= 3.10.
- A serial connection to the simulator; TAI only supports COM1–COM8. `DEMO` mode works without hardware.

### Quick start (pip)

```bash
pip install asl5000-mcp          # or: pip install git+https://github.com/niuniubinghero/asl5000-mcp.git
asl-monitor check                # self-check: paths, ports, COM ports, script library
asl-monitor install --client claude --write
```

`install` merges `{"mcpServers":{"asl-monitor":{"command":"asl-monitor","args":[]}}}` into the client's config and keeps a `.bak`. Omit `--write` for a dry run. Restart the client afterwards.

### Quick start (source, no install)

```bash
git clone https://github.com/niuniubinghero/asl5000-mcp
cd asl5000-mcp
pip install "mcp>=1.2"
asl-monitor install --client cursor --mode source --write
```

Source mode points the client at `run_server.py` with your current Python interpreter — no console script needed.

### Install the Skill

```bash
asl-monitor install --client workbuddy --skill --skills-root .workbuddy/skills --write
```

Or copy `skill/asl-lung-sim/SKILL.md` into your Agent's skills directory manually.

### Path configuration

Nothing is hard-coded. If auto-discovery fails:

```bash
asl-monitor paths --sw "<path-to-ASL5000_SW3.6.exe>" --tai "<path-to-ASL_TAI.exe>"
# or let the Agent call asl_set_paths during a session
```

Environment variables `ASL_SW_PATH`, `ASL_TAI_EXE`, `ASL_ROOT`, `ASL_TAI_HOST`, `ASL_TAI_PORT`, `ASL_MONITOR_CONFIG` and `ASL_MONITOR_RECORDINGS` override everything.

### Offline tools (source checkout)

TAI cannot create or save `.vr3` models, and a real install has 1000+ scripts. Two standard-library
scripts in `tools/` cover that gap (they do not talk to the simulator and are not part of the wheel):

```bash
python tools/make_lung_model.py --template "<ASL_ROOT>/vars/scenarios/Adult_normal.vr3" --list
python tools/build_script_index.py --asl-root "<ASL_ROOT>" --out-dir ./my-index
```

`make_lung_model.py` builds model variants; add `--sct` to also write the matching `.sct` wrapper
that `asl_run` requires (off by default).
`build_script_index.py` produces a 32-column searchable CSV index of `vars/` and `breath/`.
Both auto-detect the install root or take `--asl-root`.

A third tool, `tools/live_wave_view.py`, is a **live waveform dashboard** (browser-based, ECharts):
opens the simulator's TCP 6342 (per-breath parameters) and 6343 (512 Hz waveform) broadcasts,
renders multi-panel pressure/flow/volume traces with zoom/pan/crosshair linking, a Real Time
Analysis parameter panel, and an Interactive Control tab (TAI 6341) that reads/writes lung-model,
spontaneous-breath and closed-loop parameters with pump-mode detection. Run it alongside a live
simulation — it reads only, except through the Interactive tab you explicitly use:

```bash
python tools/live_wave_view.py            # then open the printed http://127.0.0.1:PORT
```

See [tools/README.md](tools/README.md) for the full feature list.

See [docs/INSTALL.md](docs/INSTALL.md), [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) and [docs/PROTOCOL.md](docs/PROTOCOL.md).

---

## 中文

用自然语言对 Agent 说：「连模拟肺，跑 Adult_normal 场景，把 C1 改成 50 前后各取一次 PEEP 对比」。Agent 通过 `asl-monitor` 的 25 个工具完成，`asl-lung-sim` Skill 负责约束操作顺序与安全边界。

### 能力一览

| 类别 | 工具 | 作用 |
|---|---|---|
| 部署自检 | `asl_env_check`、`asl_set_paths` | 检测软件/服务器、端口、COM 口清单与 `vars/` 脚本库 |
| 被动监控 | `asl_check`、`asl_get_status`、`asl_snapshot`、`asl_start/stop_recording`、`asl_list_recordings` | 呼吸参数 + 波形统计、快照、长程 CSV 录波 |
| 录制预检 | `asl_prepare_recording` | 每次启动前只读检查命名、目录与同名冲突 |
| 主动控制 | `asl_tai_start`、`asl_connect`、`asl_load_script`、`asl_run`、`asl_stop`、`asl_exit`、`asl_tai_status` | 拉起 TAI、连机、加载脚本、启动/停止仿真 |
| 参数分析 | `asl_query_breath_params`、`asl_export_waveform` | 逐口参数（PIP/PEEP/AutoPEEP/Vt/触发响应时间/做功）、完整波形导出 |
| 实时配置 | `asl_init`、`asl_aux`、`asl_set_interactive`、`asl_set` | FRC/管路补偿、辅助回路、运行中改肺模型与闭环目标 |
| 厂商数据解析 | `asl_vendor_scan`、`asl_vendor_read`、`asl_vendor_export` | 只读扫描 `ASLdata` 数据组、读列名/各列统计/预览行 (.rwb/.dtb/.brb/.avb)、导出 CSV |

### 厂商原始数据默认保留

TAI 的 `RS` 命令厂商默认 `WF=0`（**不保存波形文件**），本包统一用 `wf=1` 启动，让 ASL 软件自己把
`<NAME>.dtb/.rwb/.brb/.avb/.aux/.vol/.tdms/.tdms_index` 写到 `<ASL_ROOT>\ASLdata\` —— 与 GUI 手动录制是同一套文件，
和广播口旁听生成的 CSV 是两套独立数据；`.dtb` 里有 6343 广播流缺失的 `Total Flow (L/min)`。

每次 `asl_run` 之前必须调 `asl_prepare_recording`（只读、不发 TAI 命令），由**用户**决定三件事：
录制数据文件名（推荐 `<脚本名>_<时间戳>` 或自定义）、保存目录（默认 `\ASLDATA` 或自定义）、
同名数据已存在时（覆盖 / 改名 / 末尾加 `_1`）。同名冲突且用户未选覆盖时，`asl_run` 直接返回
`need_decision` 且**不发 RS**。`asl_export_waveform` 不传名字时自动跟随最近一次成功启动的 NAME。

### 两种安装方式

**pip 方式**（推荐）：

```bash
pip install asl5000-mcp
asl-monitor check
asl-monitor install --client workbuddy --write
```

仓库里的配置片段为 `{"mcpServers":{"asl-monitor":{"command":"asl-monitor","args":[]}}}`，不含任何绝对路径。`asl-monitor install` 在本机会自动解析 console script 的真实路径（例如 pip 装到用户目录、Scripts 不在 PATH 时），只把解析后的路径写进你自己的客户端配置。

**源码免安装**：

```bash
git clone https://github.com/niuniubinghero/asl5000-mcp && cd asl5000-mcp
pip install "mcp>=1.2"
asl-monitor install --client cursor --mode source --write
```

源码模式会用当前 Python 解释器指向仓库内的 `run_server.py`。

### Skill 安装

```bash
asl-monitor install --client workbuddy --skill --skills-root .workbuddy/skills --write
```

也可以直接把 `skill/asl-lung-sim/SKILL.md` 拷到客户端的 skills 目录。

### 路径配置

包内不含任何机器路径。自动探测失败时：

```bash
asl-monitor paths --sw "<ASL5000 软件 exe>" --tai "<ASL_TAI.exe>"
```

或在会话中让 Agent 调用 `asl_set_paths`。优先级：环境变量 > 用户配置文件 > 自动探测。

### 离线工具（源码方式）

TAI 没有生成/保存 `.vr3` 的能力，脚本库又常上千个文件。`tools/` 下有两个纯标准库脚本补齐这两块（不连设备、不随 wheel 分发）：

```bash
python tools/make_lung_model.py --template "<ASL_ROOT>/vars/scenarios/Adult_normal.vr3" --list
python tools/build_script_index.py --asl-root "<ASL_ROOT>" --out-dir ./my-index
```

`make_lung_model.py` 按模板批量造肺模型，显式加 `--sct` 才同时写出 `asl_run` 需要的 `.sct` 包装（默认只出模型、不写包装）；`build_script_index.py` 给 `vars/` 与 `breath/` 生成 32 列可检索清单。安装根自动探测，也可用 `--asl-root` 指定。

第三个工具 `tools/live_wave_view.py` 是**实时波形看板**（浏览器打开，ECharts）：接仿真的 TCP 6342（逐口参数）与 6343（512 Hz 波形）广播，多面板压力/流量/容积曲线，支持滚轮缩放横轴、Shift+滚轮缩放纵轴、左/右键拖动平移、十字光标联动、末端涟漪动点；带 Real Time Analysis 参数实时面板和 Interactive Control tab（TAI 6341，交互读写肺模型/自主呼吸/闭环参数，含泵模式识别）。随仿真运行时启动，仅监听只读——除非你主动在 Interactive tab 里写入：

```bash
python tools/live_wave_view.py            # 打印出的 http://127.0.0.1:端口 用浏览器打开
```

详见 [tools/README.md](tools/README.md)。

### 安全提醒

- 启动仿真会驱动真实活塞，Skill 要求 Agent 先获得你的确认。
- `asl_exit`(EX) 存在厂商软件崩溃、主机脱离 PC 继续运行的风险。测试结束默认停在 IDLE 保持连接，**不要自作主张 EX**；详见 [TROUBLESHOOTING](docs/TROUBLESHOOTING.md)。
- 本项目用于研发/测试自动化，不是医疗器械，不用于临床诊断或治疗决策。

---

## Layout / 目录结构

```
pyproject.toml          packaging + asl-monitor console script
src/asl_monitor/        server.py / monitor.py / tai.py / recording.py / vendor_data.py / config.py / cli.py
  data/SKILL.md         authoritative Agent Skill (shipped in the wheel)
skill/asl-lung-sim/     identical copy for manual installation
configs/                zero-absolute-path client snippets (pip mode)
run_server.py           source-mode entry point
docs/                   INSTALL / TROUBLESHOOTING / PROTOCOL
tools/                  offline .vr3 generator + script index + live waveform dashboard (source checkout)
tests/                  hardware-free unit tests
```

## License

MIT — see [LICENSE](LICENSE). Vendor software, vendor documentation and lung models are **not** covered by this license and are not distributed here.
