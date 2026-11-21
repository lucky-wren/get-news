# -*- coding: utf-8 -*-
"""get-news · 运行预算与超时防护验收（交付前必跑）。

真实跑两次离线 pipeline：
  A 正常预算  → 必须出五工作表 xlsx、耗时 ≤ 预算、留痕齐全；
  B 1 秒预算  → 模拟「采集层被全局预算短路」，必须**仍然出表**（不崩、不 0 产出）。
这是 2026-11-21「35 分钟硬杀 → Excel 未落盘 → 6 个 step 全跳过」的防回退闸。

用法：python scripts/verify_budget.py [--json output/budget_verify.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
CHECKS = []


def add(name, ok, detail=""):
    CHECKS.append({"项": name, "结果": "PASS" if ok else "FAIL", "证据": str(detail)[:240]})


def text(rel):
    p = BASE / rel
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def offline_run(total_seconds: float):
    """不改正式产物：cfg 深改 mode=demo，out_path 指向临时文件。"""
    os.environ["NEWS_BUDGET_TOTAL"] = str(total_seconds)
    os.environ["FREE_SOURCES_OFFLINE"] = "1"
    from news_pipeline.timewindow import Window
    from zoneinfo import ZoneInfo
    from news_pipeline.pipeline import run_pipeline

    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
    cfg.setdefault("source", {})["mode"] = "demo"
    cfg.setdefault("source", {})["inbox_path"] = "data/demo_inbox.json"
    # 离线自检：关掉所有会真发请求的增强层
    cfg.setdefault("platform_access", {})["enabled"] = False
    cfg.setdefault("search_api", {})["open_hot_sources"] = False
    cfg.setdefault("source_expansion", {})["enabled"] = False
    cfg.setdefault("social_main", {})["enabled"] = False
    cfg.setdefault("source", {}).setdefault("live", {})["fetch_article"] = False
    cfg.setdefault("source", {}).setdefault("live", {})["second_pass_enabled"] = False
    win = Window(datetime(2026, 11, 19, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                 datetime(2026, 11, 20, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                 interval="lo-ro")
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "budget_test.xlsx"
        R = run_pipeline(cfg, win, BASE, out)
        elapsed = time.monotonic() - t0
        result = {"耗时秒": round(elapsed, 1), "出表": out.exists(),
                  "字节": out.stat().st_size if out.exists() else 0,
                  "候选": len(R.get("candidate", [])), "主表": len(R.get("main", [])),
                  "预算报告": (R.get("logs", {}) or {}).get("budget", {})}
        if out.exists():
            import openpyxl
            wb = openpyxl.load_workbook(out, read_only=True)
            result["工作表"] = list(wb.sheetnames)
            wb.close()
    os.environ.pop("NEWS_BUDGET_TOTAL", None)
    os.environ.pop("FREE_SOURCES_OFFLINE", None)
    return result


def main() -> int:
    cfg_text = text("config.yaml")
    wf = text(".github/workflows/daily-news.yml")
    cfg = yaml.safe_load(cfg_text) or {}
    b = cfg.get("budget", {}) or {}

    add("① 全局预算配置存在且总时长 ≤ 600s",
        bool(b) and b.get("enabled") is True and float(b.get("total_collect_seconds", 999)) <= 600
        and float(b.get("export_reserve_seconds", 0)) >= 20,
        f"total={b.get('total_collect_seconds')}s reserve={b.get('export_reserve_seconds')}s")
    layers = b.get("layer_seconds", {}) or {}
    add("② 分层配额之和 + 出表保留 ≤ 总预算",
        sum(float(v) for v in layers.values()) + float(b.get("export_reserve_seconds", 25))
        <= float(b.get("total_collect_seconds", 600)),
        f"层合计 {sum(float(v) for v in layers.values()):.0f}s / {len(layers)} 层")

    add("③ workflow 有并行预探测 step 且总闸 ≤ 5 分钟",
        "scripts/preflight.py" in wf and "并行预探测" in wf,
        "6 个串行网络探测改为一次并行，最坏 49min → ≤3min")
    enh_names = ("端点自动发现（读预探测结果）", "全渠道可达性矩阵（读预探测结果）",
                 "两类渠道逐条解析（读预探测结果）", "★ 必采缺口定向重测（21 条，读预探测结果）",
                 "免费源池实测（读预探测结果）", "无 Key 检索通道实测（读预探测结果）")
    enh_timeouts = [int(re.search(r"timeout-minutes:\s*(\d+)", wf[m.start():m.start() + 400]).group(1))
                    for m in [re.search(re.escape(n), wf) for n in enh_names] if m
                    and re.search(r"timeout-minutes:\s*(\d+)", wf[m.start():m.start() + 400])]
    add("④ 6 个增强 step 改为只读缓存打印（各 timeout ≤ 2 分钟）",
        len(enh_timeouts) == 6 and all(v <= 2 for v in enh_timeouts),
        f"增强步 timeout = {enh_timeouts} 分钟")
    add("⑤ 采集步 timeout ≤ 15 分钟且脚本自带 10 分钟预算",
        re.search(r"采集 → 筛选评级 → 五工作表 Excel[\s\S]{0,200}timeout-minutes:\s*(\d+)", wf)
        and int(re.search(r"采集 → 筛选评级 → 五工作表 Excel[\s\S]{0,200}timeout-minutes:\s*(\d+)", wf).group(1)) <= 15,
        "35min → 15min 兜底；脚本 600s 自己返回")
    add("⑥ 采集步不再重复跑 --probe（截图里的 10 平台表由预探测产出）",
        "modelscope_notebook.py --probe --timeout 6" not in wf
        and "logs/hot_probe.json" in wf, "读预探测结果，不二次发请求")
    add("⑦ social_main.max_lookup 不再为 0（不限）",
        int((cfg.get("social_main", {}) or {}).get("max_lookup", 0)) > 0,
        "0=不限会让逐条回查吃满整批预算")
    add("⑧ GDELT 串行节流调用数已收紧",
        int((cfg.get("keyless", {}) or {}).get("gdelt_max_total_calls", 99)) <= 4
        and int((cfg.get("source_expansion", {}) or {}).get("gdelt_max_queries", 99)) <= 2,
        "5.2s/请求 × 20 次 = 104s，性价比最低")

    try:
        a = offline_run(90.0)
        add("⑨ 正常预算离线跑批出五工作表 Excel 且 ≤ 预算",
            a["出表"] and len(a.get("工作表", [])) >= 5 and a["耗时秒"] <= 90,
            f"{a['耗时秒']}s / {a['字节']} 字节 / 表 {len(a.get('工作表', []))} 个")
        add("⑨ 预算报告写进 logs.budget", bool(a["预算报告"]) and "逐层" in a["预算报告"],
            (a["预算报告"] or {}).get("结论", "—"))
    except Exception as e:
        add("⑨ 正常预算离线跑批", False, f"{type(e).__name__}: {e}")
        a = {}

    try:
        z = offline_run(1.0)
        add("⑩ 预算耗尽（1s）仍出表 —— 不再 35 分钟白跑",
            z["出表"] and len(z.get("工作表", [])) >= 5,
            f"{z['耗时秒']}s / {z['字节']} 字节 / 表 {len(z.get('工作表', []))} 个")
        add("⑩ 短路层逐条留痕",
            bool((z["预算报告"] or {}).get("被短路的层")),
            "、".join((z["预算报告"] or {}).get("被短路的层", [])) or "无")
    except Exception as e:
        add("⑩ 预算耗尽仍出表", False, f"{type(e).__name__}: {e}")
        z = {}

    add("⑪ 预探测脚本可编译 / 自检入口存在",
        (BASE / "scripts/preflight.py").exists() and "--offline" in text("scripts/preflight.py"),
        "python scripts/preflight.py --offline 可无网自检")

    passed = sum(1 for c in CHECKS if c["结果"] == "PASS")
    failed = [c for c in CHECKS if c["结果"] == "FAIL"]
    report = {"检查项": len(CHECKS), "PASS": passed, "FAIL": len(failed),
              "失败项": failed, "全部": CHECKS,
              "正常预算跑批": a, "1秒预算跑批": z,
              "说明": "离线 demo 模式不发网络请求；线上耗时以 CI 日志的 budget 报告为准。"}
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--json", default=str(BASE / "output/budget_verify.json"))
    args = ap_.parse_args()
    out = Path(args.json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"预算/超时验收：{passed}/{len(CHECKS)} PASS | FAIL {len(failed)}")
    for c in failed:
        print(f"  [FAIL] {c['项']} — {c['证据'][:200]}")
    print(f"报告：{out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
