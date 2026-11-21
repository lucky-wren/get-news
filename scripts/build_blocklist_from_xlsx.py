"""从一份真实批次 Excel 生成「初始低质渠道跳采名单」。

用途：用户 2026-11-20「把低质量和过滤数据多的渠道直接去掉」。
不需要等下一批自己算账本——直接拿本批真实单元格，把 100% 摘要未取得、
或过滤率极高的渠道写进 data/channel_blocklist.yaml，下一轮采集端立刻跳采。

    python scripts/build_blocklist_from_xlsx.py /path/to/latest.xlsx
    python scripts/build_blocklist_from_xlsx.py /path/to/latest.xlsx --min-main 6 --max-abs 0.15

纯离线，不联网、不改原 xlsx。白名单 599 渠道不删除，只跳采并逐条申报。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))


def build(xlsx: str, min_main: int = 6, max_abs: float = 0.15,
          min_pool: int = 8, top: int = 40) -> dict:
    from openpyxl import load_workbook
    from openpyxl.reader.excel import ExcelReader
    ExcelReader.read_custom = lambda s: None
    wb = load_workbook(xlsx, read_only=True, data_only=True)

    def grab(sn):
        ws = wb[sn]
        h = [c.value for c in next(ws.iter_rows())]
        return [dict(zip(h, r)) for r in ws.iter_rows(min_row=2, values_only=True)]

    main = grab("处理后的数据")
    pool = grab("窗口内全部数据")
    wb.close()
    agg = defaultdict(lambda: {"主表": 0, "有效": 0, "候选": 0, "候选有效": 0})
    for r in main:
        s = str(r.get("来源") or "未知").strip()
        agg[s]["主表"] += 1
        if "摘要未取得" not in str(r.get("内容摘要") or ""):
            agg[s]["有效"] += 1
    for r in pool:
        s = str(r.get("来源") or "未知").strip()
        agg[s]["候选"] += 1
        if "摘要未取得" not in str(r.get("内容摘要") or ""):
            agg[s]["候选有效"] += 1

    blocked, rows = [], []
    for s, a in agg.items():
        n, ok = a["主表"], a["有效"]
        abs_ratio = ok / max(1, n)
        cand_abs = a["候选有效"] / max(1, a["候选"])
        reasons = []
        if n >= min_main and abs_ratio <= max_abs:
            reasons.append(f"主表 {n} 条中 {n-ok} 条「摘要未取得」（有效率 {abs_ratio:.0%} ≤ {max_abs:.0%}）")
        if a["候选"] >= min_pool and cand_abs <= max_abs and n == 0:
            reasons.append(f"候选池 {a['候选']} 条但主表 0 条、候选有效率 {cand_abs:.0%}")
        rows.append({"来源": s, "主表": n, "有效摘要": ok, "摘要有效率": round(abs_ratio, 4),
                     "候选池": a["候选"], "候选有效率": round(cand_abs, 4),
                     "淘汰": bool(reasons), "原因": "；".join(reasons)})
        if reasons:
            blocked.append({"channel_id": s, "source": s, "reason": "；".join(reasons),
                            "unblock": "补该站独立原文端点 / 配 JINA_API_KEY / 连续 2 批主表摘要有效率 ≥ 85% 后自动解禁",
                            "依据": f"真实批次 {Path(xlsx).name}"})
    rows.sort(key=lambda x: (-int(x["淘汰"]), -x["主表"], x["摘要有效率"]))
    blocked = blocked[:top]

    import yaml
    out = BASE / "data" / "channel_blocklist.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# 由 scripts/build_blocklist_from_xlsx.py 按真实批次单元格生成，勿手改\n"
        "# 采集端跳采；白名单 599 渠道仍逐条申报淘汰原因与自动解禁条件（需求②）\n"
        + yaml.safe_dump({"blocked": blocked}, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    q = {"来源": Path(xlsx).name, "汇总": {"来源数": len(rows), "淘汰数": len(blocked),
                                     "淘汰渠道主表条目": sum(r["主表"] for r in rows if r["淘汰"])},
         "阈值": {"min_main": min_main, "max_abs": max_abs, "min_pool": min_pool, "top": top},
         "逐来源": rows}
    (BASE / "data" / "channel_quality_from_upload.json").write_text(
        json.dumps(q, ensure_ascii=False, indent=1), encoding="utf-8")
    return q


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx", nargs="?", default=str(BASE / "docs/downloads/latest.xlsx"))
    ap.add_argument("--min-main", type=int, default=6)
    ap.add_argument("--max-abs", type=float, default=0.15)
    ap.add_argument("--min-pool", type=int, default=8)
    ap.add_argument("--top", type=int, default=40)
    a = ap.parse_args()
    r = build(a.xlsx, a.min_main, a.max_abs, a.min_pool, a.top)
    print("=" * 72)
    print(f"初始低质渠道跳采名单 | 来源 {r['汇总']['来源数']} / 淘汰 {r['汇总']['淘汰数']} "
          f"/ 涉及主表条目 {r['汇总']['淘汰渠道主表条目']}")
    print("=" * 72)
    for x in r["逐来源"][:20]:
        if x["淘汰"]:
            print(f"  [淘汰] {x['来源'][:22]:<24} 主表 {x['主表']:>3} 有效 {x['有效摘要']:>3} "
                  f"({x['摘要有效率']:.0%}) 候选 {x['候选池']:>4}")
    print("\n写入 data/channel_blocklist.yaml（下一轮采集端生效）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
