"""唯一 CLI：python -m newsbrief [--mode offline|live] [--date YYYY-MM-DD]。

顺序：先流水线 → 出表 → 18 项规格验收 → 通过才覆盖固定地址并生成页面；
不达标返回 1，固定地址与页面都不动（保留上一批）。
--date 默认取 Asia/Shanghai 当天：每天 20:00 的窗口才是「昨 20:00 → 今 20:00」。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

# GitHub runner 预装的 urllib3 在无 PySocks 时会打一条 DependencyWarning；
# 它不影响采集，但挤在日志里像故障。只过滤这一条，不掩盖其它告警。
warnings.filterwarnings("ignore", message="SOCKS support in urllib3.*")

from . import appendix, pipeline, sheet, site

ROOT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))


def read_capture(path: Path, window: tuple) -> list[dict]:
    """把线上产物的『窗口内全部数据』逐条转成采集回执，带可核对时间依据。

    - 标题/摘要里显式出现的窗口外日期（9月16日、11月18/19/21日、2027年…）
      一律判超窗口，回炉时由时间硬闸剔除，不再混进主表。
    - 其余条目按「本批采集回执生成时刻」入窗；附录 18 逐条写明这是回执采集
      时刻、不是原文首发时刻，不推测、不冒充。
    """
    from openpyxl import load_workbook
    import re
    wb = load_workbook(path, data_only=True)
    ws = wb["窗口内全部数据"] if "窗口内全部数据" in wb.sheetnames else wb[wb.sheetnames[2]]
    end = window[1]
    captured = end.strftime("%Y-%m-%d")
    # 只认「明确早于窗口左界」的日期，正文里提到的未来生效日另作口径说明
    old_pat = re.compile(r"(9月\d+|11月1[0-9]日|11月2[01]日|2027年|2025年|2024年|"
                         r"\d+月\d+日至\d+日)")
    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not r or not r[7]:
            continue
        title = str(r[7]).strip()
        abstract = str(r[8] or "")
        text = title + " " + abstract
        m = old_pat.search(text)
# 诊断回放只验规则，绝不把回执时刻冒充原文发布时间。
        if m and ("9月" in m.group(0) or "11月1" in m.group(0)
                  or "11月2" in m.group(0) or "2027年" in m.group(0)
                  or "2025年" in m.group(0) or "2024年" in m.group(0)):
            pub = "2026-11-21 12:00"
            basis = f"标题/摘要显式出现窗口外日期「{m.group(0)}」，按超窗口留痕"
        else:
            # 无原文发布时间 = 时间依据缺失，交给时间硬闸剔除，不推测、不洗时间
            pub = ""
            basis = ("原文页面未取得可核对发布时间；诊断回放不按回执收录时刻填充，"
                     "由时间窗口硬闸逐条留痕（附录 18）")
        rows.append({
            "title": title, "abstract": abstract, "sources": str(r[9] or ""),
            "group": str(r[5] or "民生服务"), "tag": str(r[6] or "综合"),
            "record_type": str(r[11] or "采集回执独立原文"), "url": "",
            "published": pub, "time_basis": basis,
            "shape": "独立原文", "evidence": "A",
        })
    wb.close()
    return rows


def main() -> int:
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    ap = argparse.ArgumentParser(description="每日 20:00 大众向要闻简报")
    ap.add_argument("--mode", choices=["offline", "live"], default="offline")
    ap.add_argument("--date", default="",
                    help="不传时：live 取 Asia/Shanghai 当天；offline 固定回放 11-21 定稿语料")
    ap.add_argument("--out", default="")
    ap.add_argument("--channels", default="")
    ap.add_argument("--window-start", default="",
                    help="显式窗口左端 YYYY-MM-DD HH:MM（Asia/Shanghai，左开）")
    ap.add_argument("--window-end", default="",
                    help="显式窗口右端 YYYY-MM-DD HH:MM（Asia/Shanghai，右闭）")
    ap.add_argument("--replay-capture", default="",
                    help="【诊断工具，非生产路径】把某批 xlsx 的候选池当采集输入回炉验规则；"
                         "不覆盖固定下载地址、不重建页面、不作交付成果")
    args = ap.parse_args()
    # live 绝不写死历史日期；offline 是文档 §11 的定稿回放，窗口须与台账一致。
    date = args.date or ("2026-11-21" if args.mode == "offline" else today)
    # 显式窗口右端是哪天，批次日期/文件名就归哪天，避免「窗口是 23 日、文件名写 24 日」
    if not args.date and args.window_end and args.mode == "live":
        date = pipeline.parse_window_arg(args.window_end).strftime("%Y-%m-%d")

    win = None
    if args.window_start or args.window_end:
        s = (pipeline.parse_window_arg(args.window_start) if args.window_start
             else pipeline.window_of(date)[1][0])
        e = (pipeline.parse_window_arg(args.window_end) if args.window_end
             else pipeline.window_of(date)[1][1])
        if s >= e:
            ap.error("窗口左端必须早于右端")
        win = (s, e)

    capture = None
    if args.replay_capture:
        capture = read_capture(Path(args.replay_capture), win or pipeline.window_of(date)[1])

    cfg, channels = pipeline.load_config()
    if args.channels:
        channels = json.loads(Path(args.channels).read_text(encoding="utf-8"))
    batch = pipeline.run(mode=args.mode, cfg=cfg, channels=channels, date=date,
                         window=win, capture=capture)
    diagnose = bool(args.replay_capture)
    out = Path(args.out) if args.out else ROOT / "output" / \
        (f"diagnose_capture_{date.replace('-', '')}.xlsx" if diagnose
         else f"daily_news_{date.replace('-', '')}_20h.xlsx")
    sheet.build_workbook(batch, out, batch.koushi,
                         [it.trace_row() for it in batch.main])
    checks = pipeline.verify(batch, out)
    # 把逐项验收结果回写进「口径与稿外信息」附录 9 与结构校验结果项，
    # 避免口径表预写结论、附录 9 空表；重写后数据区与三张数据表逐格不变。
    if getattr(batch, "rule_trace", None) is not None:
        batch.koushi = appendix.build_koushi(batch, batch.rule_trace, checks=checks)
        sheet.build_workbook(batch, out, batch.koushi,
                             [it.trace_row() for it in batch.main])
    failed = [c for c in checks if c["结果"] == "FAIL"]
    hard = [c for c in failed if c.get("gate", True)]
    soft = [c for c in failed if not c.get("gate", True)]
    for c in failed:
        print(f"  {'FAIL' if c.get('gate', True) else '标注'} | {c['项']} | {c['证据']}")
    print(f"[规格验收] {'达标' if not hard else '不达标'}  "
          f"{len(checks) - len(failed)}/{len(checks)} PASS → {out.name}")
    print(f"[交付] 窗口 {batch.window} / 候选池 {len(batch.candidate)} / "
          f"主表 {len(batch.main)} / 31 类逐类申报 / 口径表 {len(batch.koushi)} 行")
    if soft:
        print(f"[产能] {len(soft)} 项编排/覆盖未达满窗，如实标注不阻断："
              + "、".join(c["项"].split()[0] for c in soft))
    if batch.mode == "live" and batch.failed:
        print(f"[产能] {len(batch.failed)} 个端点失败，已回吐旧缓存并逐类申报缺口："
              + "、".join(sorted(batch.failed)))
    if hard:
        print("::error::形态硬闸不达标，固定下载地址与页面均未覆盖", file=sys.stderr)
        if not batch.main:
            print("::error::本批主表 0 条：窗口内没有可交付的要闻。"
                  "不覆盖固定地址、不重建页面、保留上一批；"
                  "请检查采集端点可达性与准入剔除留痕（附录 2/15）。",
                  file=sys.stderr)
        return 1

    # 只有 live 批次才允许覆盖固定下载地址与线上页面；offline 是历史定稿回放，
    # 若也去覆盖，会把线上产物悄悄换成 11-21 旧批次（自检/演示场景尤其危险）。
    if batch.mode == "live" and not diagnose:
        latest = ROOT / cfg["output"]["latest"]
        latest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(out, latest)
        site.build_site(batch, checks, ROOT / "docs")
        print(f"[交付] {out.name} → 覆盖 {latest.relative_to(ROOT).as_posix()}")
        print("[页面] 已按本批 Batch 重建 docs/index.html、docs/news-data.js"
              f"（窗口 {batch.window}）")
    elif diagnose:
        print("[诊断回放] 本批仅用于验规则链路；固定下载地址与页面均未改动，"
              "此产物不是当日交付成果，不得作为成品展示条数。")
        print("[诊断回放] 生产路径是 python -m newsbrief --mode live（不带本参数），"
              "workflow 采集步骤与 CI 测试均走该路径。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
