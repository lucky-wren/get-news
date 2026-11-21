# -*- coding: utf-8 -*-
"""线上 11-21 20:28 批次**全量**重判（56 条主表 + 344 条候选池）。

与 verify_online_quality.py 的区别：那个只抄了用户文档里的主表前 18 条做单元回归；
本脚本用从 GitHub raw 拉回的**完整 851K 线上产物**（随包
`data/online_batch_20261121_2028.xlsx`），逐条跑修复后的真实代码，回答用户那句
「必须达到我要求的数据质量效果」——到底哪些垃圾会被拦、哪些真新闻会被保住、
哪些格式会被修好、清洗后主表长什么样。

两层判定与线上流水线完全一致：
  1) polish_abstract_strict 修摘要（1–2 段 / ≤100 净字 / 整句 / 数字不改）
  2) normalize_source 修来源（渠道 · 渠道 / 裸域名 / 栏目标注）
  3) editorial_check(stage="candidate") 候选池层：只剔形态垃圾，摘要问题留池留痕
  4) editorial_check(stage="main") 主表层：摘要/格式不合格才拦下

产出（全部标「非新抓取」，不冒充实时数据）：
  output/online_batch_rejudge.json          逐条留痕与统计
  output/线上批次新规则重判对照.xlsx        主表逐条 / 候选池逐条 / 清洗后主表 / 汇总口径
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from openpyxl import Workbook, load_workbook  # noqa: E402
from openpyxl.reader.excel import ExcelReader  # noqa: E402
from openpyxl.styles import Alignment, Font, PatternFill  # noqa: E402
from news_pipeline import editorial_gate as eg  # noqa: E402

ExcelReader.read_custom = lambda self: None   # 本项目写入的自定义属性 openpyxl 读不了

DEFAULT_XLSX = BASE / "data" / "online_batch_20261121_2028.xlsx"
OUT_JSON = BASE / "output" / "online_batch_rejudge.json"
OUT_XLSX = BASE / "output" / "线上批次新规则重判对照.xlsx"
INDENT = "\u3000\u3000"


def _rows(ws):
    hdr = [c.value for c in ws[1]]
    return hdr, [dict(zip(hdr, [c.value for c in r]))
                 for r in ws.iter_rows(min_row=2) if r[0].value is not None]


def _item(r, abstract: str, source: str):
    return SimpleNamespace(
        title=str(r.get("标题") or ""), abstract=abstract, abstract_body="",
        source=source, record_type=str(r.get("记录类型") or ""),
        channel_id=str(r.get("事件主键") or ""), group=str(r.get("上位大类") or ""),
        tag=str(r.get("分类标签") or ""), url="", pub=None, seq=r.get("展示序"),
        raw={}, shape="独立原文", has_event=True, abstract_status="有效", evidence="B")


def _indent(t: str) -> str:
    return "\n".join(INDENT + p.lstrip("\u3000 ") for p in str(t or "").split("\n") if p.strip())


def judge(r: dict) -> dict:
    a0 = str(r.get("内容摘要") or "")
    s0 = str(r.get("来源") or "")
    title = str(r.get("标题") or "")
    a1, awhy = eg.polish_abstract_strict(a0, title)
    s1, snotes = eg.normalize_source(s0)
    it = _item(r, a1, s1)
    cok, cgate, cwhy = eg.editorial_check(it, stage="candidate")
    mok, mgate, mwhy = (eg.editorial_check(it, stage="main")
                        if cok else (False, "—", "候选池层已剔除"))
    return {
        "展示序": r.get("展示序"), "等级": r.get("等级"),
        "原标题": title, "原摘要": a0, "修后摘要": _indent(a1),
        "原来源": s0, "修后来源": s1,
        "摘要被修复": a1 != a0, "摘要修复依据": awhy if a1 != a0 else "—",
        "来源被规范化": s1 != s0, "来源规范化留痕": snotes,
        "候选层": "留池" if cok else "剔除", "命中闸": cgate if not cok else "—",
        "候选层依据": cwhy,
        "主表层": "通过" if mok else "拦下", "主表层命中闸": mgate if not mok else "—",
        "主表层依据": mwhy,
        "处置": ("剔除（形态垃圾）" if not cok else
                 ("保留进主表" if mok else "留池留痕、不进主表")),
        "_raw": r,
    }


def build_xlsx(main: list, cand: list, stats: dict, path: Path) -> None:
    wb = Workbook()
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="365F91")
    bad_fill = PatternFill("solid", fgColor="FCE4E4")
    ok_fill = PatternFill("solid", fgColor="E7F3E7")

    def sheet(name: str, cols: list, data: list, widths=None):
        ws = wb.create_sheet(name)
        ws.append(cols)
        for c in ws[1]:
            c.font, c.fill = head_font, head_fill
            c.alignment = Alignment(horizontal="center", vertical="center")
        for row in data:
            ws.append([row.get(k, "") if not isinstance(row.get(k), list)
                       else "；".join(str(x) for x in row[k]) for k in cols])
        for i, w in enumerate(widths or [], 1):
            ws.column_dimensions[ws.cell(1, i).column_letter].width = w
        ws.freeze_panes = "A2"
        return ws

    cols = ["展示序", "等级", "原标题", "原摘要", "修后摘要", "原来源", "修后来源",
            "摘要被修复", "来源被规范化", "候选层", "命中闸", "候选层依据",
            "主表层", "主表层命中闸", "处置"]
    ws1 = sheet("主表逐条重判", cols, main,
                [8, 7, 42, 52, 52, 40, 40, 11, 12, 10, 20, 44, 10, 20, 24])
    for i, d in enumerate(main, 2):
        ws1.cell(i, 15).fill = bad_fill if d["候选层"] == "剔除" else ok_fill
    ws2 = sheet("候选池逐条重判", cols, cand,
                [8, 7, 42, 52, 52, 40, 40, 11, 12, 10, 20, 44, 10, 20, 24])
    for i, d in enumerate(cand, 2):
        ws2.cell(i, 15).fill = bad_fill if d["候选层"] == "剔除" else ok_fill

    # 清洗后主表：12 列与原表一致，展示序重排；评级沿用原批次（仅内容清洗，不重算分数）
    ws3 = wb.create_sheet("清洗后主表")
    hdr = ["展示序", "Final_adj", "等级", "原始 Final", "分数序", "上位大类", "分类标签",
           "标题", "内容摘要", "来源", "事件主键", "记录类型"]
    ws3.append(hdr)
    for c in ws3[1]:
        c.font, c.fill = head_font, head_fill
    kept = [d for d in main if d["主表层"] == "通过"]
    for n, d in enumerate(kept, 1):
        r = d["_raw"]
        ws3.append([n, r.get("Final_adj"), r.get("等级"), r.get("原始 Final"),
                    r.get("分数序"), r.get("上位大类"), r.get("分类标签"),
                    r.get("标题"), d["修后摘要"], d["修后来源"],
                    r.get("事件主键"), r.get("记录类型")])
    for col, w in zip("ABCDEFGHIJKL", [8, 11, 7, 11, 9, 16, 26, 44, 60, 42, 12, 18]):
        ws3.column_dimensions[col].width = w
    ws3.freeze_panes = "A2"

    ws4 = wb.create_sheet("汇总与口径")
    lines = [
        ["【批次记录】", "线上 2026-11-21 20:28 Actions 批次（run 44903447029 / commit b72cea9）"],
        ["数据来源", "GitHub raw 拉取的 docs/downloads/latest.xlsx（851K，随包副本），非新抓取、非离线样例"],
        ["重判依据", "修复后的真实代码：polish_abstract_strict / normalize_source / editorial_check 两层"],
        ["", ""],
        ["主表原始条数", stats["主表原始"]],
        ["主表：形态垃圾剔除", stats["主表剔除"]],
        ["主表：摘要被修复", stats["主表摘要修复"]],
        ["主表：来源被规范化", stats["主表来源规范化"]],
        ["主表：清洗后保留", stats["主表保留"]],
        ["", ""],
        ["候选池原始条数", stats["候选池原始"]],
        ["候选池：形态垃圾剔除", stats["候选池剔除"]],
        ["候选池：留池留痕", stats["候选池留池"]],
        ["候选池：摘要未取得留池但被主表拦下", stats["候选池摘要留痕"]],
        ["候选池：摘要被修复", stats["候选池摘要修复"]],
        ["候选池：来源被规范化", stats["候选池来源规范化"]],
        ["", ""],
        ["边界 1", "本表是对既有线上批次按新规则重判，不是重新联网抓取；不应冒充今日新数据"],
        ["边界 2", "评级/分数沿用原批次，仅做内容清洗与准入重判，未重算 Final"],
        ["边界 3", "代码保证『不达标不交付』，不保证『每天都达标』；真达标要推包后由 CI 重跑"],
        ["边界 4", "被剔除条目逐条留痕（含命中闸与依据），不是静默删数据"],
        ["", ""],
        ["3 条必拦垃圾", "序2 劳动仲裁申请书（SEO）／序3 云工厂黄页广告／序17 GTA6 剧透视频"],
        ["必须保住的真新闻", "序1 德令哈 5.6 级地震（S）／序4 国产大模型／序9 LPR／序10·26·29·31·33 F10 来源的财经稿"],
    ]
    for row in lines:
        ws4.append(row)
    ws4.column_dimensions["A"].width = 40
    ws4.column_dimensions["B"].width = 96
    for c in ws4["A"]:
        c.font = Font(bold=True)
    for row in ws4.iter_rows():
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=True)
    del wb["Sheet"]
    wb.save(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", default=str(DEFAULT_XLSX))
    ap.add_argument("--json", default=str(OUT_JSON))
    ap.add_argument("--xlsx-out", default=str(OUT_XLSX))
    args = ap.parse_args()
    src = Path(args.xlsx)
    assert src.exists(), f"找不到线上真表：{src}（应随包提供）"
    wb = load_workbook(src, data_only=True)
    assert "处理后的数据" in wb.sheetnames and "窗口内全部数据" in wb.sheetnames
    _, mr = _rows(wb["处理后的数据"])
    _, cr = _rows(wb["窗口内全部数据"])
    main_j = [judge(r) for r in mr]
    cand_j = [judge(r) for r in cr]

    def s(j, key, pred=lambda d: True):
        return sum(1 for d in j if pred(d) and d[key])

    stats = {
        "主表原始": len(main_j), "主表剔除": s(main_j, "候选层", lambda d: d["候选层"] == "剔除"),
        "主表保留": sum(1 for d in main_j if d["主表层"] == "通过"),
        "主表摘要修复": s(main_j, "摘要被修复"), "主表来源规范化": s(main_j, "来源被规范化"),
        "候选池原始": len(cand_j), "候选池剔除": s(cand_j, "候选层", lambda d: d["候选层"] == "剔除"),
        "候选池留池": sum(1 for d in cand_j if d["候选层"] == "留池"),
        "候选池摘要留痕": sum(1 for d in cand_j if d["候选层"] == "留池" and d["主表层"] == "拦下"),
        "候选池摘要修复": s(cand_j, "摘要被修复"),
        "候选池来源规范化": s(cand_j, "来源被规范化"),
    }
    gates_m = Counter(d["命中闸"] for d in main_j if d["候选层"] == "剔除")
    gates_c = Counter(d["命中闸"] for d in cand_j if d["候选层"] == "剔除")
    stats["主表拦截闸门分布"] = dict(gates_m)
    stats["候选池拦截闸门分布"] = dict(gates_c)

    # ---- 硬断言：既拦垃圾，也不误杀真新闻（上一轮只验 18 条时漏掉过杀） ----
    checks: list[dict] = []

    def add(name, ok, detail=""):
        checks.append({"项": name, "结果": "PASS" if ok else "FAIL", "证据": str(detail)[:260]})
        print(f"[{'PASS' if ok else 'FAIL'}] {name} — {str(detail)[:150]}")

    by_seq = {int(d["展示序"]): d for d in main_j if str(d["展示序"]).isdigit()}
    for seq, label in [(2, "劳动仲裁 SEO 页"), (3, "云工厂黄页广告"), (17, "GTA6 剧透")]:
        d = by_seq.get(seq)
        add(f"必拦垃圾｜序{seq} {label}", bool(d) and d["候选层"] == "剔除",
            f"{d['命中闸'] if d else '缺行'}：{d['候选层依据'][:70] if d else ''}")
    for seq, label in [(1, "德令哈 5.6 级地震 S"), (4, "国产大模型"), (9, "LPR"),
                       (10, "AI液冷 F10 来源"), (29, "油轮遇袭 F10 来源"),
                       (31, "腊肉季 F10 来源")]:
        d = by_seq.get(seq)
        add(f"不误杀真新闻｜序{seq} {label}", bool(d) and d["主表层"] == "通过",
            f"处置={d['处置'] if d else '缺行'} | 摘要修复={d['摘要被修复'] if d else '—'}")
    add("摘要修复生效（多段/半句被修成 1–2 段整句）",
        stats["主表摘要修复"] >= 5 and stats["候选池摘要修复"] >= 30,
        f"主表 {stats['主表摘要修复']} 条 / 候选池 {stats['候选池摘要修复']} 条")
    add("来源列规范化生效（规格验收历史 FAIL 8 条）",
        stats["主表来源规范化"] >= 8,
        f"主表 {stats['主表来源规范化']} 条 / 候选池 {stats['候选池来源规范化']} 条")
    add("『摘要未取得』留在候选池、只被主表拦下（需求⑤）",
        stats["候选池摘要留痕"] >= 1 and all(
            d["候选层"] == "留池" for d in cand_j if "摘要未取得" in str(d["原摘要"])),
        f"留池留痕 {stats['候选池摘要留痕']} 条")
    add("清洗后主表仍是可用的成规模数据（不是删到只剩几条）",
        stats["主表保留"] >= 45,
        f"{stats['主表原始']} → {stats['主表保留']} 条（剔除 {stats['主表剔除']} 条垃圾）")
    add("宣传成就拼盘稿被拦（需求 §3 稳中向好类统统不要）",
        stats["候选池拦截闸门分布"].get("E3 宣传拼盘稿", 0) >= 3,
        f"拦下 {stats['候选池拦截闸门分布'].get('E3 宣传拼盘稿', 0)} 条")

    out = {
        "批次": "线上 2026-11-21 20:28 Actions 批次（非新抓取）",
        "输入": str(src.name), "统计": stats, "断言": checks,
        "主表逐条": [{k: v for k, v in d.items() if k != "_raw"} for d in main_j],
        "候选池逐条": [{k: v for k, v in d.items() if k != "_raw"} for d in cand_j],
        "口径": "两层判定与线上流水线一致；评级沿用原批次，仅内容清洗与准入重判",
    }
    Path(args.json).parent.mkdir(exist_ok=True)
    Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    build_xlsx(main_j, cand_j, stats, Path(args.xlsx_out))
    passed = sum(1 for c in checks if c["结果"] == "PASS")
    print(f"\n{passed}/{len(checks)} PASS → {args.json} | {args.xlsx_out}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
