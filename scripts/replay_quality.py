#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资讯获取 · 批次质量离线回放（用用户上传的 latest.xlsx 真实验证修复效果）。

不联网、不改原 xlsx：从「主表」工作表重建 RawItem，按本轮修复后的同一套代码
重跑「F5 → 主表摘要硬闸 → 主题簇聚类 → 六维 V + H 代理 → 等级映射」，
对照原表等级分布，证明 A 档不再数学上恒空，且 A 档满足可核对条件。

用法：
  python scripts/replay_quality.py /path/to/latest.xlsx [--json output/replay_quality.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import yaml                                    # noqa: E402
from openpyxl import load_workbook              # noqa: E402
from news_pipeline.model import RawItem, COLS_12   # noqa: E402
from news_pipeline.timewindow import Window        # noqa: E402
from news_pipeline import rules, cluster, scoring  # noqa: E402
from news_pipeline.rules import (noise_verdict, mass_verdict, intl_verdict,  # noqa: E402
                                f_verdict, bankruptcy_subject_verdict, ACHIEVEMENT_STRONG, FAR_FUTURE)


def read_main(path: Path, sheet: str | None = None) -> list[dict]:
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = None
    if sheet:
        for name in wb.sheetnames:
            if sheet in name:
                ws = wb[name]
                break
    if ws is None:
        for name in wb.sheetnames:
            if "主表" in name or "处理后的数据" in name:
                ws = wb[name]
                break
    if ws is None:
        ws = wb[wb.sheetnames[0]]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    hdr_i = None
    for i, r in enumerate(rows[:20]):
        cells = [str(x or "").strip() for x in r]
        if all(c in cells for c in COLS_12):
            hdr_i = i
            break
    if hdr_i is None:
        raise RuntimeError("未找到 12 列表头")
    hdr = [str(x or "").strip() for x in rows[hdr_i]]
    out = []
    for r in rows[hdr_i + 1:]:
        if not r or not r[0]:
            continue
        d = {hdr[i]: r[i] for i in range(min(len(hdr), len(r))) if hdr[i]}
        if not str(d.get("标题") or "").strip():
            continue
        out.append(d)
    return out


def build_items(rows: list[dict], win: Window) -> list[RawItem]:
    tz = ZoneInfo(win.tz)
    pub = win.end - timedelta(hours=2)
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=tz)
    items = []
    for i, d in enumerate(rows, 1):
        abstract = str(d.get("内容摘要") or "")
        missing = "摘要未取得" in abstract or abstract.strip() == ""
        it = RawItem(
            title=str(d.get("标题") or "").strip(),
            abstract="" if missing else abstract,
            abstract_body="" if missing else abstract,
            abstract_status="未取得" if missing else "有效",
            source=str(d.get("来源") or "").strip(),
            group=str(d.get("上位大类") or "").strip(),
            tag=str(d.get("分类标签") or "").strip(),
            eid=str(d.get("事件主键") or "").strip(),
            record_type=str(d.get("记录类型") or "独立原文").strip(),
            evidence="A", shape="独立原文", has_event=True,
            seq=i, channel=str(d.get("来源") or "").strip(),
            pub=pub, pub_raw="回放基准时间", time_basis="离线回放（原表未带发布时间列）",
        )
        items.append(it)
    return items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx", nargs="?", default="/data/inputs/latest.xlsx")
    ap.add_argument("--sheet", default=None,
                    help="工作表关键字；留空=主表（复现问题），填「窗口内全部数据」=按真实流水线候选池回放")
    ap.add_argument("--json", default=str(BASE / "output" / "replay_quality.json"))
    args = ap.parse_args()
    p = Path(args.xlsx)
    if not p.exists():
        print(f"FAIL: 找不到 {p}")
        return 1

    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8")) or {}
    win = Window(start=__import__("datetime").datetime(2026, 11, 19, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                 end=__import__("datetime").datetime(2026, 11, 20, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                 interval="lo-ro", tz="Asia/Shanghai")

    rows = read_main(p, args.sheet)
    print(f"[回放输入] 工作表关键字={args.sheet or '主表(默认)'} | 读入 {len(rows)} 条")
    before = {}
    for d in rows:
        g = str(d.get("等级") or "?").strip()
        before[g] = before.get(g, 0) + 1
    print(f"原表主表 {len(rows)} 条 | 等级分布 {before} | A 档 {before.get('A', 0)} 条")

    items = build_items(rows, win)
    missing = sum(1 for x in items if x.abstract_status == "未取得")

    # 1) 完整准入层（与 pipeline.py 第 3 段逐条一致，不能只跑 F5）
    #    2026-11-20 教训：第一版回放漏了 mass_verdict，结果财政部/商务部政务稿
    #    被主题簇聚成 58 条一簇、H 代理刷出 212 个假 S/A。必须跑 strict 贴近度。
    kept, blocked_abstract, blocked_noise, blocked_mass, blocked_other = [], [], [], [], []
    strictness = str((cfg.get("rules") or {}).get("mass_strictness", "strict"))
    intl_only = bool((cfg.get("rules") or {}).get("intl_three_only", True))
    for it in items:
        if it.abstract_status not in ("有效", "正文补采"):
            blocked_abstract.append(it.title)
            continue
        ok, rule, why = noise_verdict(it)
        if not ok:
            blocked_noise.append({"标题": it.title, "规则": rule, "依据": why})
            continue
        bok, btype, bwhy = bankruptcy_subject_verdict(it)
        if not bok:
            blocked_other.append({"标题": it.title, "阶段": "破产主体核验", "依据": bwhy})
            continue
        mok, _, _, mwhy = mass_verdict(it, strictness)
        if not mok:
            blocked_mass.append({"标题": it.title, "依据": mwhy})
            continue
        iok, _, _, iwhy = intl_verdict(it)
        if not iok and intl_only:
            blocked_other.append({"标题": it.title, "阶段": "国际三类门禁", "依据": iwhy})
            continue
        fok, frule, fwhy = f_verdict(it)
        if not fok:
            blocked_other.append({"标题": it.title, "阶段": f"低价值过滤 {frule}", "依据": fwhy})
            continue
        kept.append(it)
    print(f"摘要硬闸 {len(blocked_abstract)} | F5 {len(blocked_noise)} | "
          f"贴近度strict {len(blocked_mass)} | 其它 {len(blocked_other)} | 进入重聚类 {len(kept)} 条")

    # 2) 新主题簇聚类（LPR 等多来源归并）
    pairs, clusters = cluster.cluster_events(kept)
    multi = [c for c in clusters if c["size"] > 1]
    print(f"聚类：{len(kept)} 条 → {len(clusters)} 簇 | 多来源簇 {len(multi)} 个 | "
          f"最大簇 {max((c['size'] for c in clusters), default=0)} 条")
    lpr = [c for c in multi if any("LPR" in kept[i].title for i in c["idx"])]
    print(f"LPR 主题簇：{len(lpr)} 个 | 簇内条数 {[c['size'] for c in lpr]}")

    # 3) 重打分（H 代理按同簇不同来源数生效）
    info = scoring.compute_scores(kept, win, cfg)
    # 真实流水线在打分后按事件主键去重（merge_to_main），同簇只留 1 条代表稿；
    # 不跑这步会把「商品车出口专列」重复算成 3 个 A，虚增数量。
    main, _, cstats = cluster.merge_to_main(kept)
    after = {}
    for it in main:
        after[it.grade] = after.get(it.grade, 0) + 1
    a_items = [it for it in main if it.grade in ("S", "A")]
    print(f"按事件主键去重后主表 {len(main)} 条 | 等级分布 {after} | S+A 档 {len(a_items)} 条")

    # 4) A 档条件核查：必须摘要有效 + 命中用户①正面清单
    bad_a = []
    for it in a_items:
        m_ok, _, _, m_why = rules.mass_verdict(it, "strict")
        nsrc = int(getattr(it, "proxy_h_sources", 0) or 0)
        if it.abstract_status not in ("有效", "正文补采") or not m_ok:
            bad_a.append({"标题": it.title, "摘要状态": it.abstract_status,
                          "贴近度": m_why, "多渠道数": nsrc})
    print("A 档样例（前 8）：")
    for it in sorted(a_items, key=lambda x: -x.Final)[:8]:
        print(f"  {it.grade} Final={it.Final:.2f} 多渠道={getattr(it, 'proxy_h_sources', 0)} "
              f"| {it.title[:42]}")

    checks = [
        ("原表 A 档为 0（复现问题；候选池评级列全为—）", before.get("A", 0) == 0),
        ("修复后 S+A 档 > 0", len(a_items) > 0),
        ("H 代理在候选池回放生效" if args.sheet else "主表回放已去重，H 代理可为空",
         int(info.get("H代理通路", {}).get("生效条数", 0)) > 0 if args.sheet else True),
        ("存在多渠道归并簇（H代理有输入）", len(multi) > 0 and max((c["size"] for c in clusters), default=0) >= 2),
        ("LPR 同题条目被摘要硬闸拦下（不拿空摘要凑数）",
         sum(1 for x in blocked_abstract if "LPR" in x) >= 2),
        ("摘要未取得条目被硬闸拦下", len(blocked_abstract) > 0),
        ("F5 新词组拦下本批噪声", len(blocked_noise) > 0),
        ("A 档全部摘要有效且贴近度通过", len(bad_a) == 0),
        ("A 档不含稳中向好 / 远期未落地政务稿",
         not any(any(w in it.title for w in ACHIEVEMENT_STRONG + FAR_FUTURE) for it in a_items)),
    ]
    failed = [n for n, ok in checks if not ok]
    out = {
        "输入": str(p), "工作表": args.sheet or "主表(默认)",
        "原表条数": len(rows), "原表等级分布": before,
        "摘要未取得": missing, "摘要硬闸拦下": len(blocked_abstract),
        "F5 拦下": len(blocked_noise), "F5 样例": blocked_noise[:12],
        "贴近度strict拦下": len(blocked_mass), "贴近度样例": blocked_mass[:12],
        "其它准入拦下": len(blocked_other),
        "进入重聚类": len(kept), "聚类数": len(clusters),
        "多来源簇数": len(multi), "LPR 簇": [c["size"] for c in lpr],
        "最大簇条数": max((c["size"] for c in clusters), default=0),
        "去重后主表条数": len(main), "重打分等级分布": after, "S+A 档数": len(a_items),
        "H代理生效条数": int(info.get("H代理通路", {}).get("生效条数", 0)),
        "A档样例": [{"等级": it.grade, "Final": it.Final,
                   "多渠道数": getattr(it, "proxy_h_sources", 0),
                   "标题": it.title} for it in sorted(a_items, key=lambda x: -x.Final)[:15]],
        "A档不合格": bad_a, "检查": [{"项": n, "结果": "PASS" if ok else "FAIL"} for n, ok in checks],
        "FAIL": len(failed), "失败项": failed,
    }
    dest = Path(args.json)
    dest.parent.mkdir(exist_ok=True, parents=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"检查数": len(checks), "PASS": len(checks) - len(failed),
                      "FAIL": len(failed), "失败项": failed}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
