#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 ASL 5000 厂商 .rwb/.dtb/.brb 数据组做成**单文件离线 ECharts 波形对比页**。

    python tools/build_wave_view.py                       # 默认 3 组 CPAP 对比
    python tools/build_wave_view.py --names SIN10BPM SIN15BPM SIN20BPM
    python tools/build_wave_view.py --all                # 全部有波形的组（文件较大）
    python tools/build_wave_view.py --data-dir "D:\\ASLdata" --out view.html

生成的 HTML 内联 ECharts 库 + base64 编码的**全量** float32 波形（不降采样），
断网双击即可打开。交互：滚轮/框选/滑块缩放、通道与数据组自由勾选、多组叠加对比、
三种 X 轴对齐（绝对时间 / 呼吸内时间 / 呼吸相位）、单位切换、十字光标读数、
当前窗口统计、逐呼吸参数表、CSV/PNG 导出、亮暗主题。

依赖：仅标准库 + 本仓库 src/asl_monitor/vendor_data.py + tools/assets/echarts.min.js。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))

from asl_monitor.vendor_data import DataGroup, scan_dir  # noqa: E402
from _paths import resolve_root  # noqa: E402

ECHARTS_CANDIDATES = (HERE / "assets" / "echarts.min.js",)
DEFAULT_NAMES = ("CPAP_NOFPS_NOHME_10BPM", "CPAP_NOFPS_NOHME_15BPM",
                 "CPAP_NOFPS_NOHME_20BPM")

# key, 显示名, 来源, 列索引, 单位族, 默认勾选
# 只用头部明文可确认的列；.dtb 的 col12..15 名称被 500B 头截断，不纳入。
CHANNELS = (
    ("paw",    "Airway Pressure",    "dtb", 2,  "pressure", True),
    ("pmus",   "Muscle Pressure",    "dtb", 3,  "pressure", False),
    ("ptrach", "Tracheal Pressure",  "rwb", 3,  "pressure", False),
    ("p1",     "Chamber 1 Pressure", "dtb", 6,  "pressure", False),
    ("p2",     "Chamber 2 Pressure", "dtb", 7,  "pressure", False),
    ("vtot",   "Total Volume",       "dtb", 4,  "volume",   True),
    ("v1",     "Chamber 1 Volume",   "dtb", 8,  "volume",   False),
    ("v2",     "Chamber 2 Volume",   "dtb", 9,  "volume",   False),
    ("vcomp",  "Compressed Volume",  "dtb", 1,  "volume",   False),
    ("ftot",   "Total Flow",         "dtb", 5,  "flow",     True),
    ("f1",     "Chamber 1 Flow",     "dtb", 10, "flow",     False),
    ("f2",     "Chamber 2 Flow",     "dtb", 11, "flow",     False),
    ("aux1",   "Aux 1",              "rwb", 10, "volt",     False),
    ("aux2",   "Aux 2",              "rwb", 11, "volt",     False),
    ("o2",     "Oxygen Sensor",      "rwb", 12, "volt",     False),
)

SHORT_LABEL = {
    "paw": "Paw", "pmus": "Pmus", "ptrach": "Ptrach", "p1": "P1", "p2": "P2",
    "vtot": "Vtot", "v1": "V1", "v2": "V2", "vcomp": "Vcomp",
    "ftot": "Flow", "f1": "F1", "f2": "F2",
    "aux1": "Aux1", "aux2": "Aux2", "o2": "O2",
}

UNIT_TABLE = {
    "pressure": ("cmH2O", "hPa", 0.980665),
    "volume":   ("mL", "L", 0.001),
    "flow":     ("L/min", "mL/s", 1000.0 / 60.0),
    "volt":     ("V", "V", 1.0),
}

BREATH_COLS = (
    "Breath Num.", "I Time (s)", "E Time (s)", "Insp Vt (mL)", "Exp Vt (mL)",
    "Ppeak (cmH2O)", "Pmean (cmH2O)", "PEEP (cmH2O)", "Peak Flow (mL/s)",
    "Time to Trigger (ms)", "Insp Work (mJ)", "ftot (BPM)", "P_0.1 (cmH2O)",
)


def b64_f32(values) -> str:
    """float 序列 → 大端 float32 → base64（JS 端 DataView.getFloat32 还原）。"""
    return base64.b64encode(struct.pack(f">{len(values)}f", *values)).decode("ascii")


def breath_axes(time: list[float], numbers: list[float]):
    """按呼吸编号切段，算「呼吸内时间(s)」与「呼吸相位(%)」。"""
    n = len(time)
    if not n:
        return [], []
    bounds = [0]
    for i in range(1, n):
        if numbers[i] != numbers[i - 1]:
            bounds.append(i)
    bounds.append(n)
    # 段长用「下一段起点 - 本段起点」，最后一段用相邻段平均长度
    lengths = []
    for k in range(len(bounds) - 1):
        s, e = bounds[k], bounds[k + 1]
        lengths.append(time[min(e, n - 1)] - time[s])
    avg = sum(lengths) / len(lengths) if lengths else 1.0
    if lengths and lengths[-1] <= 0:
        lengths[-1] = avg or 1.0
    bt = [0.0] * n
    ph = [0.0] * n
    for k in range(len(bounds) - 1):
        s, e = bounds[k], bounds[k + 1]
        L = lengths[k] if lengths[k] > 0 else (avg or 1.0)
        for i in range(s, e):
            bt[i] = time[i] - time[s]
            ph[i] = min(bt[i] / L * 100.0, 100.0)
    return bt, ph


def build_group(name: str, data_dir: Path) -> dict:
    g = DataGroup(data_dir, name)
    rwb = g.wave("rwb")
    dtb = g.wave("dtb")
    if rwb.nrows != dtb.nrows:
        raise ValueError(f"{name}: .rwb({rwb.nrows}) 与 .dtb({dtb.nrows}) 行数不一致")
    time = rwb.column(0)
    numbers = dtb.column(0)
    bt, ph = breath_axes(time, numbers)
    cols = {}
    for key, _label, src, idx, _fam, _on in CHANNELS:
        arr = (dtb if src == "dtb" else rwb)
        cols[key] = b64_f32(arr.column(idx))

    breaths = {"cols": [], "rows": []}
    brb = g.breaths()
    if brb and brb.nrows:
        picks = []
        for label in BREATH_COLS:
            try:
                picks.append((label, brb.column_index(label)))
            except KeyError:
                continue
        breaths["cols"] = [p[0] for p in picks]
        breaths["rows"] = [[round(brb.values[i * brb.ncols + j], 4)
                            for j in (p[1] for p in picks)]
                           for i in range(brb.nrows)]

    meta = g.summary()
    import re as _re
    m = _re.search(r"(\d+BPM)$", name, _re.I)
    meta["short"] = m.group(1) if m else (name if len(name) <= 14 else name[-12:])

    return {
        "name": name,
        "meta": meta,
        "time": b64_f32(time),
        "breath_t": b64_f32(bt),
        "phase": b64_f32(ph),
        "ch": cols,
        "breaths": breaths,
    }


def discover_data_dir() -> Path:
    """优先环境变量，其次复用 tools/_paths.py 的 ASL 安装根自动发现。"""
    env = os.environ.get("ASL_DATA_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    return resolve_root(require_vars=False) / "ASLdata"


def find_echarts(explicit: str | None) -> Path:
    wanted = explicit or os.environ.get("ASL_ECHARTS_JS")
    if wanted:
        p = Path(wanted)
        if not p.is_file():
            raise SystemExit(f"找不到 ECharts 库: {p}")
        return p
    for p in ECHARTS_CANDIDATES:
        if p.is_file():
            return p
    raise SystemExit("找不到 echarts.min.js，请用 --echarts 指定路径或设置 ASL_ECHARTS_JS")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{
  --bg:#0f141b; --panel:#171e27; --panel2:#1d2631; --line:#2b3745;
  --fg:#e6edf3; --muted:#8b9bab; --accent:#4ea8ff; --accent2:#ffb454;
  --chip:#222e3c; --chipon:#1d4f7c; --good:#39c07d; --warn:#ff8a65;
}
html[data-theme="light"]{
  --bg:#f4f6f9; --panel:#ffffff; --panel2:#f0f3f7; --line:#d6dee7;
  --fg:#1b2430; --muted:#63707f; --accent:#0b6bcb; --accent2:#c26800;
  --chip:#e8eef5; --chipon:#b9d9f8; --good:#12834f; --warn:#c94b20;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:13px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{display:flex;align-items:center;gap:14px;padding:12px 18px;
  border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:20}
h1{font-size:16px;margin:0;font-weight:650;letter-spacing:.3px}
h1 small{font-weight:400;color:var(--muted);font-size:12px;margin-left:10px}
.spacer{flex:1}
button,select{background:var(--chip);color:var(--fg);border:1px solid var(--line);
  border-radius:6px;padding:5px 10px;cursor:pointer;font:inherit}
button:hover,select:hover{border-color:var(--accent)}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
.wrap{padding:14px 18px 30px;max-width:1920px;margin:0 auto}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:12px 14px;margin-bottom:12px}
.row{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:6px 0}
.row>label.tag{color:var(--muted);min-width:74px;font-size:12px}
.chip{background:var(--chip);border:1px solid var(--line);border-radius:20px;
  padding:4px 12px;cursor:pointer;user-select:none;font-size:12px;white-space:nowrap}
.chip.on{background:var(--chipon);border-color:var(--accent);color:var(--fg)}
.chip.off{opacity:.5}
.chip .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.seg button{border:0;border-radius:0;border-right:1px solid var(--line);padding:5px 10px}
.seg button:last-child{border-right:0}
.seg button.on{background:var(--accent);color:#fff}
.hint{color:var(--muted);font-size:12px}
#chart{width:100%;height:620px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:1100px){.grid2{grid-template-columns:1fr}}
table{border-collapse:collapse;width:100%;font-size:12px}
th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{cursor:pointer;color:var(--muted);font-weight:600;position:sticky;top:0;background:var(--panel)}
tbody tr:hover{background:var(--panel2)}
.scroll{max-height:300px;overflow:auto}
.kv{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:12px}
.kv b{color:var(--fg);font-weight:600}
.badge{background:var(--panel2);border:1px solid var(--line);border-radius:4px;
  padding:1px 6px;font-size:11px;color:var(--muted)}
.warnbox{border-left:3px solid var(--warn);padding:8px 12px;background:var(--panel2);
  color:var(--muted);font-size:12px;margin-bottom:12px}
</style>
</head>
<body>
<header>
  <h1>ASL 5000 录制波形对比 <small>__TITLE__</small></h1>
  <div class="spacer"></div>
  <span class="badge" id="ptcount">—</span>
  <button id="btnCsv">导出当前窗口 CSV</button>
  <button id="btnPng">保存图片</button>
  <button id="btnTheme">切换主题</button>
</header>
<div class="wrap">

<div class="card">
  <div class="row"><label class="tag">数据组</label><div id="groupChips" style="display:flex;flex-wrap:wrap;gap:6px"></div></div>
  <div class="row"><label class="tag">X 轴对齐</label>
    <div class="seg" id="alignSeg">
      <button data-v="abs" class="on">绝对时间 (s)</button>
      <button data-v="breath">呼吸内时间 (s)</button>
      <button data-v="phase">呼吸相位 (%)</button>
    </div>
    <span class="hint">相位模式把每口呼吸折叠到 0–100%，不同 BPM 可直接比形态</span>
  </div>
  <div class="row"><label class="tag">单位</label>
    <div id="unitSeg" style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
      <span class="hint">压力</span><div class="seg">
        <button data-k="pressure" data-v="cmH2O" class="on">cmH₂O</button>
        <button data-k="pressure" data-v="hPa">hPa</button></div>
      <span class="hint">容积</span><div class="seg">
        <button data-k="volume" data-v="mL" class="on">mL</button>
        <button data-k="volume" data-v="L">L</button></div>
      <span class="hint">流量</span><div class="seg">
        <button data-k="flow" data-v="L/min" class="on">L/min</button>
        <button data-k="flow" data-v="mL/s">mL/s</button></div>
    </div>
  </div>
  <div class="row"><label class="tag">快速缩放</label>
    <div class="seg" id="zoomSeg">
      <button data-s="0" data-e="100" class="on">全览</button>
      <button data-s="0" data-e="10">前 10%</button>
      <button data-s="0" data-e="4">前 4%</button>
      <button data-s="45" data-e="55">中段 10%</button>
      <button data-s="90" data-e="100">末段 10%</button>
    </div>
    <span class="hint">滚轮缩放 / 框选放大 / 滑块平移 / 双击图表复位；放大后可继续用鼠标拖动画面</span>
  </div>
  <div class="row"><label class="tag">通道</label><div id="chanChips" style="display:flex;flex-wrap:wrap;gap:6px"></div>
    <button id="btnOnlyP">只看 Paw</button><button id="btnOnlyF">只看 Flow</button>
    <button id="btnAll">全选通道</button><button id="btnReset">复位视图</button>
  </div>
</div>

<div class="warnbox" id="warnbox">
  数据源为厂商原始二进制（大端 float32、全量未降采样）。滚轮缩放 / 左键框选放大 / 底部滑块平移 / 双击复位；
  十字光标可读全部勾选通道的瞬时值。<b>.dtb 头部 500B 被厂商截断</b>，其 col12–15 名称无法确认，
  本页只展示头部明文可确认的通道。
</div>

<div class="card" style="padding:6px 8px">
  <div id="chart"></div>
  <div class="kv" id="winstat" style="padding:4px 10px 8px"></div>
</div>

<div class="grid2">
  <div class="card"><div style="font-weight:600;margin-bottom:8px">当前窗口统计 <span class="hint">（随缩放实时更新）</span></div>
    <div class="scroll"><table id="statTable"><thead></thead><tbody></tbody></table></div></div>
  <div class="card"><div style="font-weight:600;margin-bottom:8px">逐呼吸参数 <span class="hint">(.brb，点击表头排序)</span></div>
    <div class="scroll"><table id="breathTable"><thead></thead><tbody></tbody></table></div></div>
</div>

<div class="card"><div style="font-weight:600;margin-bottom:8px">本机全部录制组 <span class="hint">（灰色 = 未内嵌波形，用 --names 重新生成即可加载）</span></div>
  <div class="scroll"><table id="indexTable"><thead></thead><tbody></tbody></table></div>
  <div class="hint" style="margin-top:8px" id="cmdHint"></div>
</div>

</div>
<script>/*__ECHARTS__*/</script>
<script>
"use strict";
const P = /*__PAYLOAD__*/;
const CH = P.channels, GM = {};
P.groups.forEach(g => GM[g.name] = g);
const CM = {}; CH.forEach(c => CM[c.key] = c);
const state = {
  groups: new Set(P.groups.map(g=>g.name)),
  channels: new Set(CH.filter(c=>c.on).map(c=>c.key)),
  align: 'abs',
  units: {pressure:'cmH2O', volume:'mL', flow:'L/min', volt:'V'},
  theme: 'dark'
};
const PALETTE = ['#4ea8ff','#ffb454','#39c07d','#e879b9','#a78bfa','#f87171','#22d3ee','#facc15'];
const DASH = ['solid','dashed','dotted',[6,3],[2,3],[8,3,2,3]];
const cache = new Map();
const FULL = {};

function dec(b64){
  if(cache.has(b64)) return cache.get(b64);
  const bin = atob(b64), buf = new Uint8Array(bin.length);
  for(let i=0;i<bin.length;i++) buf[i]=bin.charCodeAt(i);
  const dv = new DataView(buf.buffer), out = new Float32Array(bin.length>>2);
  for(let i=0;i<out.length;i++) out[i]=dv.getFloat32(i*4,false);
  cache.set(b64,out);
  return out;
}
function xsOf(g){
  return dec(state.align==='phase'?g.phase:(state.align==='breath'?g.breath_t:g.time));
}
function ysOf(g,key){ return dec(g.ch[key]); }
function scaleOf(c){
  const u = c.unitAlt;
  if(state.units[c.family]===u) return c.factor;
  return 1;
}
function unitOf(c){
  return state.units[c.family];
}
function colorOf(gi,ci){ return PALETTE[gi%PALETTE.length]; }
function hasFlow(){ return [...state.channels].some(k=>CM[k].family==='flow'); }
function hasVol(){ return [...state.channels].some(k=>CM[k].family==='volume'); }
function hasVolt(){ return [...state.channels].some(k=>CM[k].family==='volt'); }

let chart = echarts.init(document.getElementById('chart'), null, {renderer:'canvas'});
let forceFull = false;

function buildOption(){
  const dark = state.theme==='dark';
  const fg = dark?'#e6edf3':'#1b2430', muted = dark?'#8b9bab':'#63707f';
  const line = dark?'#2b3745':'#d6dee7';
  const groups = [...state.groups], chans=[...state.channels];
  const flow = hasFlow();
  const onlyFlow = flow && !chans.some(k=>CM[k].family!=='flow');
  const dzAxes = onlyFlow?[0]:(flow?[0,1]:[0]);
  const rightPad = (hasVol()||hasVolt())?80:28;
  const xName = state.align==='phase'?'呼吸相位 (%)':
                (state.align==='breath'?'呼吸内时间 (s)':'时间 (s)');
  const axisName = {nameLocation:'middle',nameRotate:90,nameGap:46,
                    nameTextStyle:{color:fg,fontSize:11}};
  const xBase = {type:'value',min:'dataMin',max:'dataMax',
     axisLabel:{color:muted,formatter:v=>(+v).toFixed(state.align==='phase'?0:2)},
     axisLine:{lineStyle:{color:line}},splitLine:{lineStyle:{color:line,opacity:.4}}};
  const flowAxis = {type:'value',position:'left',name:'流量 '+state.units.flow,
     axisLabel:{color:muted},axisLine:{show:true,lineStyle:{color:line}},
     splitLine:{lineStyle:{color:line,opacity:.4}}};
  let grids,xAxes,yAxes;
  if(onlyFlow){
    // 只选流量通道时，让流量图占满整个画布，不留空白主图
    grids=[{left:76,right:28,top:66,height:470}];
    xAxes=[Object.assign({},xBase,{gridIndex:0,name:xName,nameLocation:'middle',nameGap:26})];
    yAxes=[Object.assign({},flowAxis,axisName,{gridIndex:0})];
  }else{
    grids=[{left:76,right:rightPad,top:66,height:flow?324:470}];
    if(flow) grids.push({left:76,right:rightPad,top:440,height:114});
    xAxes=[Object.assign({},xBase,{gridIndex:0,name:xName,nameLocation:'middle',nameGap:26})];
    if(flow) xAxes.push(Object.assign({},xBase,{gridIndex:1,splitLine:{show:false}}));
    yAxes=[Object.assign({type:'value',gridIndex:0,position:'left',
       name:'压力 '+state.units.pressure,axisLabel:{color:muted},
       axisLine:{show:true,lineStyle:{color:line}},
       splitLine:{lineStyle:{color:line,opacity:.4}}},axisName)];
    if(hasVol()||hasVolt()) yAxes.push(Object.assign({type:'value',gridIndex:0,
       position:'right',name:hasVol()?('容积 '+state.units.volume):'V',
       axisLabel:{color:muted},axisLine:{show:true,lineStyle:{color:line}},
       splitLine:{show:false}},axisName,{nameGap:50}));
    if(flow) yAxes.push(Object.assign({},flowAxis,axisName,{gridIndex:1}));
  }

  const series=[];
  groups.forEach((gn,gi)=>{
    const g=GM[gn], xs=xsOf(g);
    chans.forEach((key,ci)=>{
      const c=CM[key], ys=ysOf(g,key), s=scaleOf(c);
      const data=new Array(xs.length);
      for(let i=0;i<xs.length;i++) data[i]=[xs[i], ys[i]*s];
      const hasRight = hasVol()||hasVolt();
      const famIdx = onlyFlow ? 0 :
          (c.family==='flow' ? (hasRight?2:1)
           : (c.family==='volume'||c.family==='volt') ? 1 : 0);
      const xIdx = onlyFlow ? 0 : (c.family==='flow'?1:0);
      const sname = g.meta.short+' · '+c.short;
      FULL[sname] = gn+' · '+c.label+' ('+unitOf(c)+')';
      series.push({
        name: sname,
        type:'line', data:data, showSymbol:false, symbol:'none',
        xAxisIndex: xIdx,
        yAxisIndex: famIdx,
        sampling:'lttb', large:true, largeThreshold:1000,
        animation:false, silent:false,
        lineStyle:{width:1.35,type:DASH[ci%DASH.length],color:colorOf(gi,ci),opacity:.92},
        itemStyle:{color:colorOf(gi,ci)},
        emphasis:{disabled:true},
        meta:{group:gn,key:key}
      });
    });
  });

  const prevDz = chart.getOption()&&chart.getOption().dataZoom&&chart.getOption().dataZoom[0];
  const dzStart = forceFull ? 0 : (prevDz?prevDz.start:0);
  const dzEnd = forceFull ? 100 : (prevDz?prevDz.end:100);
  forceFull = false;

  return {
    backgroundColor:'transparent',
    textStyle:{color:fg,fontFamily:'-apple-system,Segoe UI,Microsoft YaHei,sans-serif'},
    color:PALETTE,
    grid:grids, xAxis:xAxes, yAxis:yAxes, series:series,
    legend:{type:'scroll',top:10,left:80,right:24,itemGap:10,
            textStyle:{color:muted,fontSize:11},
            itemWidth:16,itemHeight:9,pageTextStyle:{color:muted},selectedMode:true},
    tooltip:{trigger:'axis',axisPointer:{type:'cross',label:{backgroundColor:'#334155'},
             link:[{xAxisIndex:'all'}]},backgroundColor:dark?'#1b2430':'#ffffff',
             borderColor:line,textStyle:{color:fg,fontSize:12},
             confine:true,order:'valueDesc',
             formatter:function(ps){
               if(!ps||!ps.length) return '';
               let s='<b>X = '+(+ps[0].value[0]).toFixed(4)+'</b><br/>';
               ps.forEach(p=>{ s+=p.marker+(FULL[p.seriesName]||p.seriesName)+' : <b>'+
                 (+p.value[1]).toFixed(4)+'</b><br/>'; });
               return s;
             }},
    toolbox:{right:16,top:2,itemSize:14,iconStyle:{borderColor:muted},
      feature:{dataZoom:{yAxisIndex:'none',title:{zoom:'框选放大',back:'撤销'}},
        restore:{title:'复位'},saveAsImage:{title:'PNG',pixelRatio:2}}},
    dataZoom:[
      {type:'inside',xAxisIndex:dzAxes,filterMode:'none',start:dzStart,end:dzEnd,
       zoomOnMouseWheel:true,moveOnMouseMove:true,moveOnMouseWheel:false},
      {type:'slider',xAxisIndex:dzAxes,filterMode:'none',start:dzStart,end:dzEnd,
       bottom:10,height:20,borderColor:line,fillerColor:dark?'rgba(78,168,255,.18)':'rgba(11,107,203,.16)',
       handleStyle:{color:PALETTE[0]},textStyle:{color:muted},
       dataBackground:{lineStyle:{color:muted,opacity:.5},areaStyle:{color:line,opacity:.3}}}
    ]
  };
}

function render(){
  chart.setOption(buildOption(),{notMerge:true,lazyUpdate:false});
  updateStats();
  document.getElementById('ptcount').textContent =
    [...state.groups].reduce((s,n)=>s+xsOf(GM[n]).length,0).toLocaleString()+' 点/组 × '+
    state.channels.size+' 通道';
}
function renderChips(){
  const gc=document.getElementById('groupChips'); gc.innerHTML='';
  P.groups.forEach((g,i)=>{
    const el=document.createElement('span');
    el.className='chip '+(state.groups.has(g.name)?'on':'off');
    el.innerHTML='<span class="dot" style="background:'+PALETTE[i%PALETTE.length]+'"></span>'+
      g.name+' <span style="opacity:.65">'+Math.round(g.meta.sample_rate_hz||0)+'Hz · '+
      (+g.meta.duration_sec).toFixed(0)+'s</span>';
    el.onclick=()=>{ state.groups.has(g.name)?state.groups.delete(g.name):state.groups.add(g.name);
      if(!state.groups.size) state.groups.add(g.name); renderChips(); render(); renderBreaths(); };
    gc.appendChild(el);
  });
  const cc=document.getElementById('chanChips'); cc.innerHTML='';
  CH.forEach(c=>{
    const el=document.createElement('span');
    el.className='chip '+(state.channels.has(c.key)?'on':'off');
    el.textContent=c.label+' ('+c.unit+')';
    el.title=c.source.toUpperCase()+' 列 '+c.col+' · '+c.family;
    el.onclick=()=>{ state.channels.has(c.key)?state.channels.delete(c.key):state.channels.add(c.key);
      renderChips(); render(); };
    cc.appendChild(el);
  });
}
function bindSeg(){
  document.querySelectorAll('#alignSeg button').forEach(b=>b.onclick=()=>{
    state.align=b.dataset.v; forceFull=true;
    document.querySelectorAll('#alignSeg button').forEach(x=>x.classList.toggle('on',x===b));
    render(); });
  document.querySelectorAll('#unitSeg button').forEach(b=>b.onclick=()=>{
    const k=b.dataset.k; state.units[k]=b.dataset.v;
    document.querySelectorAll('#unitSeg button[data-k="'+k+'"]')
      .forEach(x=>x.classList.toggle('on',x===b));
    render(); });
}
let xrCache={key:'',lo:0,hi:1};
function xRangeAll(){
  const key=state.align+'|'+[...state.groups].join(',');
  if(xrCache.key===key) return [xrCache.lo,xrCache.hi];
  let lo=Infinity,hi=-Infinity;
  [...state.groups].forEach(gn=>{
    const xs=xsOf(GM[gn]);
    for(let i=0;i<xs.length;i++){ if(xs[i]<lo)lo=xs[i]; if(xs[i]>hi)hi=xs[i]; }
  });
  if(!isFinite(lo)){lo=0;hi=1;}
  xrCache={key:key,lo:lo,hi:hi};
  return [lo,hi];
}
function currentRange(){
  // 用「数据全局 X 范围 + dataZoom 百分比」，避免 setOption 后 axis.scale 未刷新导致的错值
  const [lo,hi]=xRangeAll();
  const o=chart.getOption();
  const dz=o&&o.dataZoom&&o.dataZoom[0];
  const s=dz?+dz.start:0, e=dz?+dz.end:100;
  return [lo+(hi-lo)*s/100, lo+(hi-lo)*e/100];
}
function updateStats(){
  const [xmin,xmax]=currentRange();
  const rows=[];
  [...state.groups].forEach(gn=>{
    const g=GM[gn], xs=xsOf(g);
    [...state.channels].forEach(k=>{
      const c=CM[k], ys=ysOf(g,k), s=scaleOf(c);
      let mn=Infinity,mx=-Infinity,sum=0,n=0;
      for(let i=0;i<xs.length;i++){
        if(xs[i]<xmin||xs[i]>xmax) continue;
        const v=ys[i]*s; if(!isFinite(v)) continue;
        if(v<mn)mn=v; if(v>mx)mx=v; sum+=v; n++;
      }
      if(n) rows.push([gn,c.label,unitOf(c),n,mn,mx,mx-mn,sum/n]);
    });
  });
  const t=document.getElementById('statTable');
  t.querySelector('thead').innerHTML='<tr><th>数据组</th><th>通道</th><th>单位</th><th>点数</th>'+
    '<th>最小</th><th>最大</th><th>峰峰</th><th>均值</th></tr>';
  t.querySelector('tbody').innerHTML=rows.map(r=>'<tr><td>'+r[0]+'</td><td>'+r[1]+'</td><td>'+r[2]+
    '</td><td>'+r[3].toLocaleString()+'</td><td>'+r[4].toFixed(3)+'</td><td>'+r[5].toFixed(3)+
    '</td><td>'+r[6].toFixed(3)+'</td><td>'+r[7].toFixed(3)+'</td></tr>').join('');
  const win=isFinite(xmin)&&isFinite(xmax)?(xmax-xmin).toFixed(3):'全量';
  document.getElementById('winstat').innerHTML='X 窗口 <b>'+ (+xmin).toFixed(3)+' → '+(+xmax).toFixed(3)+
    '</b>　宽度 <b>'+win+'</b>　组数 <b>'+state.groups.size+'</b>　通道 <b>'+state.channels.size+'</b>';
}
let sortCol=0,sortAsc=true;
function renderBreaths(){
  const g=GM[[...state.groups][0]];
  const b=g&&g.breaths;
  const t=document.getElementById('breathTable');
  if(!b||!b.cols.length){ t.querySelector('thead').innerHTML='';
    t.querySelector('tbody').innerHTML='<tr><td class="hint">该组无 .brb 逐呼吸数据</td></tr>'; return; }
  const rows=b.rows.slice().sort((a,c)=>{
    const x=a[sortCol],y=c[sortCol];
    return (typeof x==='number'&&typeof y==='number')?(sortAsc?x-y:y-x):0;
  });
  t.querySelector('thead').innerHTML='<tr><th>组</th>'+b.cols.map((c,i)=>
    '<th data-i="'+(i+1)+'">'+c+'</th>').join('')+'</tr>';
  t.querySelector('tbody').innerHTML=rows.map(r=>'<tr><td>'+g.name+'</td>'+
    r.map(v=>'<td>'+(typeof v==='number'?(+v.toFixed(3)):v)+'</td>').join('')+'</tr>').join('');
  t.querySelectorAll('thead th[data-i]').forEach(th=>th.onclick=()=>{
    const i=+th.dataset.i-1; sortAsc=(sortCol===i)?!sortAsc:true; sortCol=i; renderBreaths(); });
}
function bindZoom(){
  document.querySelectorAll('#zoomSeg button').forEach(b=>b.onclick=()=>{
    chart.dispatchAction({type:'dataZoom',start:+b.dataset.s,end:+b.dataset.e});
    document.querySelectorAll('#zoomSeg button').forEach(x=>x.classList.toggle('on',x===b));
    setTimeout(updateStats,80);
  });
}
function renderIndex(){
  const t=document.getElementById('indexTable');
  t.querySelector('thead').innerHTML='<tr><th>数据组</th><th>采样点</th><th>采样率 Hz</th>'+
    '<th>时长 s</th><th>呼吸数</th><th>.dtb 列</th><th>波形</th><th>状态</th></tr>';
  t.querySelector('tbody').innerHTML=P.all_groups.map(r=>{
    const loaded=!!GM[r.name];
    return '<tr'+(loaded?'':' style="opacity:.45"')+'><td>'+r.name+'</td><td>'+(r.rwb_rows||0)+
      '</td><td>'+(r.sample_rate_hz||0)+'</td><td>'+(r.duration_sec||0)+'</td><td>'+
      (r.breaths||0)+'</td><td>'+(r.dtb_cols||0)+'</td><td>'+(r.has_waveform?'是':'否')+
      '</td><td>'+(loaded?'已内嵌':'未加载')+'</td></tr>';
  }).join('');
  document.getElementById('cmdHint').textContent='更换数据组：python tools/build_wave_view.py --names '+
    [...state.groups].join(' ');
}
function exportCsv(){
  const [xmin,xmax]=currentRange();
  let out='group,channel,unit,x,y\n';
  [...state.groups].forEach(gn=>{
    const g=GM[gn], xs=xsOf(g);
    [...state.channels].forEach(k=>{
      const c=CM[k], ys=ysOf(g,k), s=scaleOf(c);
      for(let i=0;i<xs.length;i++){
        if(xs[i]<xmin||xs[i]>xmax) continue;
        out+=gn+','+c.label+','+unitOf(c)+','+xs[i].toFixed(6)+','+(ys[i]*s).toFixed(6)+'\n';
      }
    });
  });
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([out],{type:'text/csv;charset=utf-8'}));
  a.download='asl_wave_window.csv'; a.click(); URL.revokeObjectURL(a.href);
}
function bindButtons(){
  document.getElementById('btnCsv').onclick=exportCsv;
  document.getElementById('btnPng').onclick=()=>{
    const a=document.createElement('a'); a.href=chart.getDataURL({pixelRatio:2,backgroundColor:
      state.theme==='dark'?'#171e27':'#ffffff'}); a.download='asl_wave.png'; a.click(); };
  document.getElementById('btnTheme').onclick=()=>{
    state.theme=state.theme==='dark'?'light':'dark';
    document.documentElement.dataset.theme=state.theme; render(); };
  document.getElementById('btnOnlyP').onclick=()=>{ state.channels=new Set(['paw']); renderChips(); render(); };
  document.getElementById('btnOnlyF').onclick=()=>{ state.channels=new Set(['ftot']); renderChips(); render(); };
  document.getElementById('btnAll').onclick=()=>{ state.channels=new Set(CH.map(c=>c.key)); renderChips(); render(); };
  document.getElementById('btnReset').onclick=()=>{
    state.channels=new Set(CH.filter(c=>c.on).map(c=>c.key));
    state.align='abs'; state.units={pressure:'cmH2O',volume:'mL',flow:'L/min',volt:'V'};
    document.querySelectorAll('#alignSeg button').forEach(x=>x.classList.toggle('on',x.dataset.v==='abs'));
    document.querySelectorAll('#unitSeg button').forEach(x=>x.classList.toggle('on',
      (x.dataset.k==='pressure'&&x.dataset.v==='cmH2O')||
      (x.dataset.k==='volume'&&x.dataset.v==='mL')||
      (x.dataset.k==='flow'&&x.dataset.v==='L/min')));
    document.querySelectorAll('#alignSeg button').forEach(x=>x.classList.toggle('on',x.dataset.v==='abs'));
    document.querySelectorAll('#zoomSeg button').forEach(x=>x.classList.toggle('on',x.dataset.s==='0'&&x.dataset.e==='100'));
    forceFull=true;
    renderChips(); render(); renderBreaths(); };
}
renderChips(); bindSeg(); bindButtons(); bindZoom(); renderBreaths(); renderIndex(); render();
chart.on('datazoom',()=>{ clearTimeout(window.__st); window.__st=setTimeout(updateStats,120); });
chart.on('restore',()=>setTimeout(render,0));
window.addEventListener('resize',()=>chart.resize());
document.getElementById('chart').ondblclick=()=>{
  chart.dispatchAction({type:'dataZoom',start:0,end:100});
  document.querySelectorAll('#zoomSeg button').forEach(x=>
    x.classList.toggle('on',x.dataset.s==='0'&&x.dataset.e==='100'));
  render(); };
</script>
</body>
</html>
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ASL 5000 厂商波形 → 单文件 ECharts 对比页")
    ap.add_argument("--data-dir", default=None,
                    help="ASLdata 目录（默认自动发现，或 $ASL_DATA_DIR）")
    ap.add_argument("--names", nargs="+", default=list(DEFAULT_NAMES), help="要内嵌的数据组名")
    ap.add_argument("--all", action="store_true", help="内嵌全部有波形的数据组（HTML 会很大）")
    ap.add_argument("--out", default=str(HERE.parent / "output" / "asl_wave_view.html"))
    ap.add_argument("--echarts", default=None, help="echarts.min.js 路径")
    ap.add_argument("--title", default="ASL 5000 厂商录制数据 (.rwb / .dtb / .brb)")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir) if args.data_dir else discover_data_dir()
    if not data_dir.is_dir():
        raise SystemExit(f"数据目录不存在: {data_dir}（用 --data-dir 指定）")
    echarts = find_echarts(args.echarts)
    all_rows = scan_dir(data_dir)
    available = [r["name"] for r in all_rows if r.get("has_waveform")]
    names = available if args.all else args.names
    missing = [n for n in names if n not in available]
    if missing:
        print(f"[warn] 跳过无波形/不存在的组: {missing}", file=sys.stderr)
        names = [n for n in names if n in available]
    if not names:
        raise SystemExit("没有可内嵌的数据组")

    groups = []
    for n in names:
        try:
            g = build_group(n, data_dir)
            groups.append(g)
            print(f"[ok] {n}: {g['meta']['rwb_rows']} 点 / "
                  f"{g['meta']['sample_rate_hz']} Hz / {g['meta']['duration_sec']} s")
        except Exception as exc:
            print(f"[fail] {n}: {exc}", file=sys.stderr)

    channels = []
    for key, label, src, col, fam, on in CHANNELS:
        base, alt, factor = UNIT_TABLE[fam]
        channels.append({"key": key, "label": label, "short": SHORT_LABEL[key],
                         "source": src, "col": col,
                         "family": fam, "unit": base, "unitAlt": alt,
                         "factor": factor, "on": on})

    payload = {
        "channels": channels,
        "groups": groups,
        "all_groups": [{k: r.get(k) for k in
                        ("name", "rwb_rows", "sample_rate_hz", "duration_sec",
                         "breaths", "dtb_cols", "has_waveform")}
                       for r in all_rows],
        "data_dir": str(data_dir),
    }
    js = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    html = (HTML_TEMPLATE
            .replace("/*__ECHARTS__*/", echarts.read_text(encoding="utf-8"))
            .replace("/*__PAYLOAD__*/", js)
            .replace("__TITLE__", args.title))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"\n生成: {out}  ({out.stat().st_size/1024/1024:.2f} MB, 离线可用)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
