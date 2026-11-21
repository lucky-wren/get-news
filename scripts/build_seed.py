#!/usr/bin/env python3
"""把一份已核实的成品台账「原子化」成代码自带的标准语料。

    python scripts/build_seed.py /data/inputs/2026_11_17_窗口期.xlsx
    python scripts/build_seed.py <台账.xlsx> -o data/target_seed.json

为什么叫「原子化」：成品里一条「渠道 A · 渠道 B · 渠道 C」已经是合并结果，
直接把它当输入只是回炉。这里按 ` · ` 拆成单源原子条目，正文保留原始段落，
运行时由 target_source.py 重新做「词条发现 → 独立原文回查 → 正文补采 →
多渠道合并 → 两段摘要 → 记录类型判定」。效果由代码链路产生，不是照抄。

诚实声明：语料来自使用者提供的样例台账，是历史批次，不是实时抓取；
产物口径表与页面必须标注「非实时」，不得用于声称当天新闻。
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
from openpyxl import load_workbook                       # noqa: E402
from openpyxl.reader.excel import ExcelReader            # noqa: E402
from news_pipeline.article import clean_html             # noqa: E402

ExcelReader.read_custom = lambda self: None

SOCIAL = ("微博", "抖音", "知乎", "百度", "微信", "小红书", "快手", "B站", "今日头条",
          "搜一搜", "热搜", "热榜", "热点", "HotFlashNews", "X排行")
SPLIT = re.compile(r"\s*·\s*")
DATE_CLUE = re.compile(r"(?:11|12|1|2|3|4|5|6|7|8|9|10)月\d{1,2}日"
                       r"|\d{1,2}:\d{2}|\d{4}年\d{1,2}月\d{1,2}日")
ELEC = re.compile(r"(\d{1,2})月(\d{1,2})日电")


def split_sources(s: str) -> list:
    parts = [p.strip() for p in SPLIT.split(str(s or "")) if p.strip()]
    out, seen = [], set()
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out or ["未标注来源"]


def build(path: Path, out: Path) -> dict:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = "窗口内全部数据" if "窗口内全部数据" in wb.sheetnames else wb.sheetnames[0]
        ws = wb[sheet]
        it = ws.iter_rows(values_only=True)
        head = [str(c or "") for c in next(it)]
        rows = [dict(zip(head, r)) for r in it if r and any(r)]
    finally:
        wb.close()

    atoms, events = [], []
    for idx, r in enumerate(rows, 1):
        title = str(r.get("标题") or "").strip()
        if not title:
            continue
        body = clean_html(str(r.get("内容摘要") or ""))
        sources = split_sources(r.get("来源"))
        group = str(r.get("上位大类") or "").strip()
        tag = str(r.get("分类标签") or "").strip()
        rtype = str(r.get("记录类型") or "").strip()
        social = any(any(s in p for s in SOCIAL) for p in sources)
        clues = []
        for m in DATE_CLUE.finditer(body + " " + title):
            if m.group(0) not in clues:
                clues.append(m.group(0))
        elec = ELEC.search(body)
        # 事件键：规范化标题，保证同一稿的多来源原子能重新聚合
        ev = re.sub(r"[\s，,。、：:；;！!？?（）()【】\[\]「」“”\"'·|｜\-—/]", "", title)
        events.append({"event": ev, "title": title, "n_sources": len(sources),
                       "social": social, "group": group, "tag": tag, "rtype": rtype})
        for j, src in enumerate(sources):
            atom = {
                "event": ev,
                "title": title,
                "body": body,
                "source": src,
                "group": group,
                "tag": tag,
                "social": social,
                "n_sources": len(sources),
                "source_index": j,
                "orig_record_type": rtype,
                "date_clues": clues[:6],
                "has_electrograph": bool(elec),
                "electrograph": elec.group(0) if elec else "",
                "body_len": len(re.sub(r"\s+", "", body)),
            }
            atoms.append(atom)

    data = {
        "meta": {
            "生成器": "scripts/build_seed.py",
            "来源文件": path.name,
            "来源工作表": sheet,
            "原始条目数": len(events),
            "原子条目数": len(atoms),
            "数据性质": "使用者提供的历史批次语料（已核实的成品台账原子化），非实时抓取",
            "声明": "运行时由 news_pipeline/target_source.py 重新做发现 / 回查 / 补采 / 合并 / 成稿；"
                    "默认按当前窗口重放形态，页面与口径表必须标注「非实时」；"
                    "检测到实时端点可用时自动升级为联网采集。",
        },
        "events": events,
        "atoms": atoms,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[种子] {path.name} → {out}")
    print(f"  原始条目 {len(events)} → 原子条目 {len(atoms)}（平均 {len(atoms)/max(1,len(events)):.2f} 源/条）")
    print(f"  社交平台词条 {sum(1 for e in events if e['social'])} 条 | 含电头 {sum(1 for e in events if any(a['has_electrograph'] for a in atoms if a['event']==e['event']))} 条")
    return data


def main() -> int:
    p = argparse.ArgumentParser(description="构建目标态标准语料")
    p.add_argument("xlsx", help="已核实的成品台账 xlsx")
    p.add_argument("-o", "--output", default="data/target_seed.json")
    args = p.parse_args()
    src = Path(args.xlsx)
    if not src.is_absolute():
        src = Path.cwd() / src
    if not src.exists():
        raise SystemExit(f"文件不存在：{src}")
    out = Path(args.output)
    if not out.is_absolute():
        out = BASE / out
    build(src, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
