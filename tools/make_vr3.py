# -*- coding: utf-8 -*-
"""模拟肺 .vr3 肺模型生成器 / Lung-model generator (ASL 5000 / TAI)

按模板 .vr3 生成新模型文件, 仅改写指定变量的值字段, 其余字节(TAB/空格对齐/CRLF)原样保留;
可同时生成配套 .sct 包装(单行 TAB 分隔文本: 第一列重复次数, 第二列指向
<ASLVarsDirectory> 下的 .vr3 文件, CRLF 结尾), 供 asl_load_script + asl_run 使用。

背景:
- .vr3 是纯文本 TAB 分隔变量表: (模块) 描述 (类型) 变量名 = 值 (单位) (min) (max)
- TAI 无生成/保存 .vr3 的命令, 运行中 IC/IM 改参不落盘; 批量造模型只能靠本工具这类脚本
- .vr3 必须配同名 .sct 包装才能 asl_run, 否则报 ERROR(14)

安装根(含 vars/ 与 breath/)由 --asl-root / ASL_ROOT / ASL_SW_PATH 自动探测, 见 _paths.py。

用法:
    # 列出模板全部变量 (无需定位安装根)
    python make_vr3.py --template "<ASL_ROOT>/vars/scenarios/Adult_normal.vr3" --list

    # 生成单个模型 + 同名 .sct 包装 (--out 相对路径按 ASL vars 目录解析)
    python make_vr3.py --template "<ASL_ROOT>/vars/scenarios/Adult_normal.vr3" --out "<scenario-dir>/hme_r21.vr3" --set r_trachea_in=21 --sct

    # 批量生成 (JSON 数组, out 相对 --outdir 或 vars 目录)
    python make_vr3.py --template "<ASL_ROOT>/vars/scenarios/Adult_normal.vr3" --from-json batch.json

batch.json 示例:
    [
      {"out": "<scenario-dir>/m1.vr3", "set": {"r_trachea_in": 21}, "sct": true, "reps": 1000},
      {"out": "<scenario-dir>/m2.vr3", "set": {"function_residual_capacity": 0.8}, "sct": true}
    ]

注意:
- 值按原样写入; 浮点数统一 6 位小数(与官方文件风格一致), 整数/字符串原样
- 复合值字段(spontaneous_amplitude_flag 等)请整体传原始字符串, 例如
  --set spontaneous_amplitude_flag="5 i1.000000 e1.000000 d0 p0.000000"
- .sct 首列为重复次数(repetitions), 官方手册 6.3 节注明不等同呼吸数, 默认 1000 与官方单模型场景一致
"""

import argparse
import json
import os
import sys

try:
    from _paths import resolve_root
except ImportError:  # when imported as tools.make_vr3
    from ._paths import resolve_root

TOKEN = "<ASLVarsDirectory>"

# .vr3 用 latin-1 逐字节读写, 保证非 ASCII 不被破坏; 行尾 CRLF 原样保留
ENC = "latin-1"


def load_vr3(path):
    """读入 .vr3, 返回 (行列表, 原始换行风格)。行尾保持原样(含 \\r\\n)。"""
    with open(path, "r", encoding=ENC, newline="") as f:
        text = f.read()
    lines = text.splitlines(keepends=True)
    return lines


def save_vr3(path, lines):
    """按原换行风格写出 .vr3。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding=ENC, newline="") as f:
        f.write("".join(lines))


def split_fields(line):
    """按 TAB 拆字段, 返回 (字段列表, 行尾)。行尾 = 最后一个含换行的字段的换行部分。"""
    body = line
    ending = ""
    for nl in ("\r\n", "\n", "\r"):
        if body.endswith(nl):
            body = body[: -len(nl)]
            ending = nl
            break
    return body.split("\t"), ending


def find_value_index(parts):
    """定位 '=' 字段下标; 返回 (eq, name) 或 (None, None)。name 取 '=' 左侧首个非空且非 (type)/(module) 字段。"""
    eq = None
    for i, p in enumerate(parts):
        if p.strip() == "=":
            eq = i
            break
    if eq is None or eq == 0:
        return None, None
    name = None
    for j in range(eq - 1, -1, -1):
        s = parts[j].strip()
        if s and not s.startswith("("):
            name = s
            break
    return eq, name


def list_vars(lines):
    """列出全部变量: [(模块, 变量名, 值, 单位)]。"""
    out = []
    for line in lines:
        parts, _ = split_fields(line)
        eq, name = find_value_index(parts)
        if eq is None:
            continue
        mod = parts[0].strip().strip("()") if parts else ""
        val = parts[eq + 1].strip() if eq + 1 < len(parts) else ""
        unit = parts[eq + 2].strip() if eq + 2 < len(parts) else ""
        out.append((mod, name, val, unit))
    return out


def find_type(parts, eq):
    """从 '=' 左侧找 (float)/(int)/(char)/(string) 类型字段, 返回小写类型名或 None。"""
    for j in range(eq - 1, -1, -1):
        s = parts[j].strip().strip("()").lower()
        if s in ("float", "double", "int", "char", "string"):
            return s
    return None


def fmt_value(v, typ=None):
    """值格式化: float 类型统一 6 位小数(官方风格), int/char 原样, 其余原样。"""
    if isinstance(v, bool):
        return str(int(v))
    if typ in ("float", "double"):
        try:
            return "%.6f" % float(v)
        except (TypeError, ValueError):
            return str(v)  # 非数值(如复合字段)原样
    if isinstance(v, float):
        return "%.6f" % v
    return str(v)


def set_vars(lines, params):
    """按 {变量名: 新值} 改写值字段。返回 (新行列表, 已改[(名, 旧, 新)], 未找到[名])。

    仅替换 '=' 右侧第一个字段内容, 对齐/空格/换行全部保留。
    变量名大小写不敏感; 同名多行时全部替换(实际文件中不会出现)。
    """
    want = {k.lower(): v for k, v in params.items()}
    applied = []
    not_found = []
    seen = set()
    new_lines = []
    for line in lines:
        parts, ending = split_fields(line)
        eq, name = find_value_index(parts)
        if eq is not None and name and name.lower() in want:
            key = name.lower()
            old = parts[eq + 1] if eq + 1 < len(parts) else ""
            if eq + 1 >= len(parts):
                parts.extend([""] * (eq + 2 - len(parts)))
            parts[eq + 1] = fmt_value(want[key], find_type(parts, eq))
            applied.append((name, old.strip(), parts[eq + 1]))
            seen.add(key)
            line = "\t".join(parts) + ending
        new_lines.append(line)
    for key in want:
        if key not in seen:
            not_found.append(key)
    return new_lines, applied, not_found


def make_sct(vr3_path, vars_dir, reps=1000, sct_path=None):
    """生成配套 .sct 包装(单行): "reps\\t<引用路径>.vr3\\r\\n"。

    vr3 位于 ASL vars 目录下时引用路径用 <ASLVarsDirectory>\\子目录\\文件名.vr3,
    否则用绝对路径(反斜杠)。返回写出的 sct 路径。
    """
    vr3_abs = os.path.abspath(vr3_path)
    stem = os.path.splitext(os.path.basename(vr3_abs))[0]
    if sct_path is None:
        sct_path = os.path.join(os.path.dirname(vr3_abs), stem + ".sct")
    try:
        rel = os.path.relpath(vr3_abs, str(vars_dir))
    except ValueError:  # Windows: the model is on a different drive from vars/
        rel = vr3_abs
    if rel.startswith("..") or os.path.isabs(rel):
        ref = vr3_abs  # vars 目录之外, 用绝对路径(ASL 是否接受待验证, 建议放 vars 下)
    else:
        ref = TOKEN + "\\" + rel.replace("/", "\\")
    content = "%d\t%s\r\n" % (reps, ref)
    os.makedirs(os.path.dirname(os.path.abspath(sct_path)), exist_ok=True)
    with open(sct_path, "w", encoding=ENC, newline="") as f:
        f.write(content)
    return sct_path


def resolve_out(out, vars_dir, outdir=None):
    """--out 相对路径按 outdir(缺省 ASL vars 目录)解析; 缺 .vr3 后缀自动补全。"""
    if not out.lower().endswith(".vr3"):
        out += ".vr3"
    if os.path.isabs(out):
        return out
    base = outdir if outdir else str(vars_dir)
    return os.path.join(base, out.replace("/", "\\"))


def report(applied, not_found, out_path, sct_path):
    print("生成: %s" % out_path)
    if applied:
        print("  变量改写 %d 个:" % len(applied))
        for name, old, new in applied:
            print("    %-38s %s -> %s" % (name, old if old else "(空)", new))
    else:
        print("  (未改写任何变量, 与模板相同)")
    if not_found:
        print("  !! 模板中未找到(已忽略): %s" % ", ".join(not_found))
    if sct_path:
        print("  包装: %s" % sct_path)


def main():
    ap = argparse.ArgumentParser(description="按模板生成 .vr3 肺模型 + .sct 包装")
    ap.add_argument("--template", required=True, help="模板 .vr3 路径")
    ap.add_argument("--asl-root", help="ASL 安装根目录(含 vars/); 缺省自动探测")
    ap.add_argument("--out", help="输出 .vr3 路径(相对路径按 ASL vars 目录解析)")
    ap.add_argument("--outdir", help="输出根目录(缺省 ASL vars 目录)")
    ap.add_argument("--set", action="append", default=[], metavar="名=值",
                    help="改写变量, 可多次; 值含空格时整体加引号")
    ap.add_argument("--from-json", help="批量模式: JSON 数组文件")
    ap.add_argument("--sct", action="store_true", help="同时生成同名 .sct 包装")
    ap.add_argument("--reps", type=int, default=1000, help=".sct 首列重复次数(默认 1000)")
    ap.add_argument("--list", action="store_true", help="只列出模板全部变量, 不生成")
    args = ap.parse_args()

    if not os.path.exists(args.template):
        print("模板不存在: %s" % args.template)
        return 1
    lines = load_vr3(args.template)

    if args.list:
        print("模板 %s 共 %d 个变量:" % (args.template, len(list_vars(lines))))
        for mod, name, val, unit in list_vars(lines):
            print("  (%s) %-38s = %-24s %s" % (mod, name, val, unit))
        return 0

    root = resolve_root(args.asl_root)
    vars_dir = root / "vars"

    # 单个模式的 --set 解析
    single = {}
    for kv in args.set:
        if "=" not in kv:
            print("--set 格式应为 名=值: %s" % kv)
            return 1
        k, v = kv.split("=", 1)
        single[k.strip()] = v

    jobs = []
    if args.from_json:
        with open(args.from_json, "r", encoding="utf-8") as f:
            batch = json.load(f)
        if not isinstance(batch, list):
            print("--from-json 需为 JSON 数组")
            return 1
        for item in batch:
            jobs.append({
                "out": item["out"],
                "set": item.get("set", {}),
                "sct": bool(item.get("sct", args.sct)),
                "reps": int(item.get("reps", args.reps)),
            })
    elif args.out:
        jobs.append({"out": args.out, "set": single, "sct": args.sct, "reps": args.reps})
    else:
        print("需要 --out 或 --from-json (或用 --list 查看模板)")
        return 1

    any_missing = False
    for job in jobs:
        out_path = resolve_out(job["out"], vars_dir, args.outdir)
        new_lines, applied, not_found = set_vars(lines, job["set"])
        if not_found:
            any_missing = True
        sct_path = (make_sct(out_path, vars_dir, reps=job["reps"])
                    if job["sct"] else None)
        save_vr3(out_path, new_lines)
        report(applied, not_found, out_path, sct_path)
    return 2 if any_missing else 0


if __name__ == "__main__":
    sys.exit(main())
