---
name: asl-lung-sim
description: ASL 5000 模拟肺控制与测试引导（配合 asl-monitor MCP）。当用户要求连接/操作模拟肺、运行呼吸仿真脚本、采集呼吸参数或波形、CPAP/BiPAP/呼吸机测试、实时修改模拟肺参数时使用。关键词: 模拟肺, ASL, 仿真, 呼吸模拟, TAI, 录波, asl-monitor。
---

# ASL 5000 模拟肺操作规程 (asl-monitor MCP)

本机已安装 `asl-monitor` MCP 服务，工具名均以 `asl_` 前缀。严格按以下规程操作，报错时按第 3 节错误码表处理。

本包是独立的非官方客户端，只负责与本机的 ASL 软件及 TAI 服务器通信；不分发 ASL 软件、`ASL_TAI.exe`、厂商 TAI 规范文档或任何肺模型文件。完整参数名清单请参考本机安装目录下的 `documentation/TAI Specification.pdf` 的 BP / IC 章节。

## 0. 每个会话首次接入：先自检

调用 `asl_env_check`：

- `ready=true` 才继续后续控制操作。
- 若 `asl_software_installed` / `tai_server_installed` 为 false，向用户询问 `ASL5000` 软件 exe 与 `ASL_TAI.exe` 的实际路径，用 `asl_set_paths` 保存（立即生效，无需重启）；也可让用户在终端执行 `asl-monitor paths --sw <exe> --tai <exe>`。
- TAI 控制端口未在线不阻塞，后续 `asl_tai_start` 会自动拉起 TAI 服务器。
- 广播端口未在线只说明 ASL 软件没开，属正常。
- 从返回的 `com_ports` / `tai_com_ports_only` 确认模拟肺所在 COM 口；用 `detail_dir` 参数查看 `vars/` 下的脚本库。

## 1. 标准测试流程

1. `asl_tai_start` — 确认 TAI 服务器在线。
2. `asl_connect(id="COMx")` — `id` 用模拟肺实际占用的 COM 口（TAI 仅支持 COM1–COM8）；无硬件演示用 `id="DEMO"`。
   - **OA 必须是连接后的首条控制命令。**
   - 调用前确认用户已关闭手动打开的 ASL 窗口，否则 COM 口被占用会报 ERROR(03)。
   - 默认 `gui=0` 隐藏 ASL 界面；用户想看界面才传 `gui=1`。
3. `asl_load_script(name)` — 脚本位于 ASL 安装目录的 `vars/` 下，官方场景通常在 `vars/scenarios/`。
   - 若脚本是 `.vr3` 纯肺模型文件，必须先按第 5 节生成同名 `.sct` 包装，否则 `asl_run` 报 ERROR(14)。
4. **启动前必查状态**：调用 `asl_get_status` 确认当前是否已有仿真在运行。
   - 若返回了正常呼吸数据（Breath Number 在增长）= 仿真已在跑，**禁止直接 `asl_run`**（会报 ERROR(03)）；先问用户是沿用当前仿真，还是 `asl_stop` 后重新加载脚本。
   - 若无数据 / 超时 = 空闲态，可安全启动。
5. `asl_run(wf=1, name="数据名")` — `wf=1` 录波形；**启动仿真前必须经用户确认**（活塞会真实运动）。
6. 数据采集（与控制通道独立，仿真运行中随时可用）：
   - `asl_get_status` 单次取当前呼吸参数 + 波形统计。
   - `asl_start_recording` / `asl_stop_recording` 长程录波，CSV 落盘到用户数据目录的 `recordings/`。
   - `asl_query_breath_params` 逐口分析参数（缺省集已含 CPAP 调试关键项：P_PEAK / PEEP / AutoPEEP / Vt / 触发响应时间 / 做功）。
   - `asl_export_waveform` 导出某一口完整波形为文本。
7. 运行中实时改模拟肺参数：`asl_set_interactive(True)` → `asl_set({"C1":50,"RATE":20,...})` → 观察效果 → `asl_set_interactive(False)`。
8. 收尾：`asl_stop` 停在 IDLE，**默认保持连接**。仅当用户明确要求"断开模拟肺 / 释放串口"时才追加 `asl_exit`，且必须按第 2 节核实主机真停。

## 2. 硬约束

- TAI 控制命令**同步执行**：控制类工具（connect / load_script / run / stop / exit / set …）必须按顺序调用，禁止并行发起或循环轰炸；监控类工具可随时调用。
- 永远不要尝试退出 TAI 服务器（无对应工具，也不应退出）。
- 改肺参数前必须先进入交互模式，否则 ERROR(16)。
- `asl_exit`（EX）会关闭 ASL 软件并释放 COM 口：**仅在用户明确要求断开时调用**。测试结束后默认保持连接、停在 IDLE 即可。
- **EX 有两种结果（已验证行为）**：EX 返回 `ok` = 干净退出，主机停止、COM 口释放、ASL 进程消失；EX 返回超时（ERROR 08）= 厂商软件崩溃，此时主机**可能脱离 PC 自主继续跑已加载脚本**（活塞仍动，但 PC 侧已无 ASL 实例：TC 报 03「无软件实例」或 15「被外部关闭」，广播端口检测不到）。**唯一停法**：重新 `asl_connect`(OA) 连回 → `asl_stop`(ES) → `asl_tai_status` 确认 IDLE。EX 之后必须核实真实状态，不能假定「退出成功 = 硬件停了」。
- 仿真启动会让真实活塞运动，操作前必须获得用户确认。

## 3. 错误码速查

| 码 | 含义 | 处理 |
|---|---|---|
| 03 | 命令不可用（状态不对） | OA 报 03 = 先关手动 ASL 实例；TC/BP 报 03 = 先 `asl_connect`；**RS 报 03 = 仿真已在运行**，先 `asl_get_status` 查状态 |
| 05 | 参数值非法 | 检查 id（COM1-8 / DEMO / 序列号）与参数名 |
| 11 | 上一条命令未完成 | 客户端已自动重连重试一次；仍报则等几秒重试 |
| 13 | 文件已存在 / 写失败 | `asl_run` 传 `ow=1` 或换 name |
| 14 | SF: 脚本文件不存在 | 检查 name/path 拼写；路径分隔符用 `\`；`.vr3` 见第 5 节生成 .sct 包装 |
| 16 | 无法设置参数 | 先 `asl_set_interactive(True)`；该参数当前肺模型可能不支持 |
| 18 | 参数越界 | 呼吸号超出总口数；用 -1 取最后一口 |
| 08 | 命令超时 | 命令**可能已实际生效**，别盲目重试；用 `asl_tai_status` / `asl_check` 核实真实状态 |
| 15 | TC: ASL 软件被外部关闭 | 这是 ASL 已退出、COM 口已释放的正常确认态；要再用重新 `asl_connect` 即可 |

## 4. 数据分析提示

- 对比调参效果：改参数前后各调一次 `asl_query_breath_params` 或抓 `asl_snapshot`，对比 PEEP / Vt / 触发响应时间 / 做功变化。
- 波形 CSV 可直接用 pandas 分析；录波会话在 `recordings/<会话>/`，路径由 `asl-monitor check` 输出。
- 长程录波建议分段（`split_sec`），并开启 `wall_time` 以便与其它日志按时间对齐。

## 5. `.vr3` 模型文件必须配 `.sct` 包装

`vars/` 下大量 `.vr3` 是纯肺模型文件。TAI 行为（已验证）：

- `asl_load_script(name="xxx.vr3")` 可直接加载成功；
- 但 `asl_run`(RS) 内部会按 `name + ".sct"` **重新找一次文件**，找不到就报 ERROR(14)（报错路径形如 `...XXX.VR3.SCT`）。

**正确做法**：加载 `.vr3` 前，先在**同目录**生成同名 `.sct` 包装（单行、TAB 分隔、CRLF 结尾）：

```
1000\t<ASLVarsDirectory>\<子目录>\<脚本名>.vr3\r\n
```

然后 `asl_load_script(name="<脚本名>", path="\\VARS\\<子目录>")`（不带扩展名），再 `asl_run`。官方场景目录里 `.sct` 与同名 `.vr3` 成对存在，可作为格式参考。

**批量造模型请用仓库自带的离线工具**：`tools/make_vr3.py` 按模板改写 `.vr3` 变量；**显式加 `--sct` 才同时写出同名 `.sct` 包装，默认只出模型**，支持单个 `--set` 与 `--from-json` 批量模式；安装根由 `--asl-root` / `ASL_ROOT` 自动探测。用法与参数见 `tools/README.md`（源码方式运行，不随 wheel 安装、**不在 21 个 MCP 工具里**）。

- 模板需自备：从本机 `<ASL_ROOT>/vars/scenarios/` 挑一个官方 `.vr3`。本仓库出于合规与体积考虑**不分发** `.vr3` 模型、脚本清单 CSV、厂商可执行文件或厂商文档。
- 生成阶段写 `.sct` 是**可选**的（省略 `--sct` 就只出 `.vr3`）；但**加载阶段**必须有同名 `.sct`，否则 `asl_run` 报 ERROR(14)。两者别混淆。
- 产物必须写在 `<ASL_ROOT>/vars/` 下（工具默认输出根就是它），否则 `.sct` 里的 `<ASLVarsDirectory>` 引用会退化成绝对路径，`asl_load_script` 的 `\VARS\...` 也找不到文件。
- 上例首列 `1000` 是重复次数（repetitions），**既不等同呼吸数、也不是秒数**（厂商手册 6.3 节）；脚本索引 CSV 因此只统计「首列合计(重复次数)」，不提供时长列。
- 找脚本用 `tools/build_script_index.py`，它扫描本机 `vars/` 与 `breath/` 生成可检索清单并顺带检查 `.sct` 引用失效 / `.vr3` 缺包装。

## 6. 路径与安装

- 所有本机路径由 `config.py` 解析，优先级：环境变量 `ASL_SW_PATH` / `ASL_TAI_EXE` > 用户配置文件 > 常见安装位置自动探测 > 由用户通过 `asl_set_paths` 提供。
- 环境变量 `ASL_TAI_HOST` / `ASL_TAI_PORT` 可覆盖控制通道地址（默认 `127.0.0.1:6341`）；`ASL_MONITOR_RECORDINGS` 可覆盖录波目录。
- 终端自检：`asl-monitor check`；保存路径：`asl-monitor paths --sw <exe> --tai <exe>`。
- 本包支持 pip 安装与源码直跑两种方式，配置片段与安装说明见仓库 `docs/INSTALL.md`。

## 7. 自进化条款

本文件是活文档。当一次操作中出现**本文未覆盖**的新行为 / 新错误码 / 新坑，且已通过实测验证结论时：

1. 立即更新本文件：新错误码进第 3 节表格；新机制新增同级小节并标注验证日期；流程缺陷直接修第 1 节。
2. 未经实测验证的猜测**不许写入**——宁可写「待验证」并单独标注。
3. 同时向用户简报改了什么（技能是用户的资产，改动要透明）。
4. 包内 `src/asl_monitor/data/SKILL.md` 与仓库 `skill/asl-lung-sim/SKILL.md` 必须保持一致（用 `asl-monitor sync-skill` 同步）。
5. 技能更新对**当前已加载**的会话不生效（内容在会话开始时载入），只影响之后的会话。
