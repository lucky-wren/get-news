# -*- coding: utf-8 -*-
"""get-news · 并行预探测（把 6 个串行网络探测 step 合成一次）。

事故：workflow 里 6 个增强 step 各自做一轮全网探测，串行最坏
10+10+8+8+8+5 = 49 分钟，且互相重复测同一批域名。本脚本在一个进程内
并行跑完，总硬上限 150 秒；每项独立超时、独立留痕，失败不阻断主链路。

输出（供后续 6 个「打印结论」step 与采集步复用，不再重复发请求）：
  logs/discover-endpoints.log / reach.log / channel-resolve.log /
  logs/star-retest.log / free-sources.log / keyless-probe.log /
  logs/hot-probe.log / logs/hot_probe.json（社交出口逐平台连通表）
  data/preflight_result.json（逐项状态、耗时、超时留痕）

用法：python scripts/preflight.py [--total 150] [--offline]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parents[1]
LOGS = BASE / "logs"
PY = sys.executable

# 阶段 A：彼此不写同一份 json，可并行
PHASE_A = [
    ("端点自动发现", [PY, "scripts/discover_endpoints.py", "--timeout", "4"],
     "logs/discover-endpoints.log", 50),
    ("全渠道可达性矩阵", [PY, "-m", "news_pipeline.reach", "--timeout", "4"],
     "logs/reach.log", 50),
    ("两类渠道逐条解析", [PY, "-m", "news_pipeline.channel_resolve", "--probe"],
     "logs/channel-resolve.log", 35),
    ("免费源池实测", [PY, "-m", "news_pipeline.free_sources", "--probe",
                  "--no-cache", "--timeout", "3"], "logs/free-sources.log", 35),
    ("无 Key 检索通道实测", [PY, "-c",
     "import json;from news_pipeline import keyless_compose as k;"
     "r=k.self_test();print(json.dumps({'通过':r['通过'],'真实探测':r['真实探测'],"
     "'结论':r['结论']},ensure_ascii=False,indent=2))"],
     "logs/keyless-probe.log", 30),
    ("社交与热榜出口探测", [PY, "scripts/modelscope_notebook.py", "--probe",
                  "--timeout", "4", "--out", "logs/hot_probe.json"],
     "logs/hot-probe.log", 40),
]
# 阶段 B：依赖阶段 A 写出的端点清单，串行执行
PHASE_B = [
    ("★ 必采缺口定向重测", [PY, "-m", "news_pipeline.star_retest", "--timeout", "4"],
     "logs/star-retest.log", 45),
]


def one(task, offline: bool) -> dict:
    name, cmd, log, budget = task
    t0 = time.monotonic()
    rec = {"项": name, "预算秒": budget, "日志": log}
    if offline:
        rec.update({"状态": "离线跳过", "耗时秒": 0.0})
        return rec
    try:
        r = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True,
                           timeout=budget, encoding="utf-8", errors="replace")
        body = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        rec.update({"状态": "完成" if r.returncode == 0 else f"rc={r.returncode}（降级复用已有结果）",
                    "耗时秒": round(time.monotonic() - t0, 1), "字节": len(body)})
    except subprocess.TimeoutExpired as e:
        body = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        body += f"\n[预算] {name} 超过 {budget}s，已终止；本批复用仓库已有结果，不阻断出表\n"
        rec.update({"状态": f"超时>{budget}s（短路）", "耗时秒": round(time.monotonic() - t0, 1)})
    except Exception as e:
        body = f"{type(e).__name__}: {e}\n"
        rec.update({"状态": "异常（降级）", "耗时秒": round(time.monotonic() - t0, 1)})
    try:
        (BASE / log).write_text(body, encoding="utf-8")
    except Exception as e:
        rec["写日志失败"] = str(e)[:100]
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--total", type=float, default=150.0)
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()
    LOGS.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    rows: list[dict] = []

    def batch(tasks, parallel: bool):
        nonlocal rows
        if parallel:
            with ThreadPoolExecutor(max_workers=min(6, len(tasks))) as ex:
                futs = {ex.submit(one, t, args.offline): t for t in tasks}
                for f in as_completed(futs, timeout=max(1.0, args.total - (time.monotonic() - t0))):
                    rows.append(f.result())
        else:
            for t in tasks:
                rows.append(one(t, args.offline))

    try:
        batch(PHASE_A, True)
    except Exception as e:                       # 总闸：阶段 A 卡死也不拖整批
        rows.append({"项": "阶段A总闸", "状态": f"{type(e).__name__}: {str(e)[:80]}", "耗时秒": 0.0})
    left = args.total - (time.monotonic() - t0)
    if left > 8:
        batch(PHASE_B, False)
    else:
        rows.append({"项": "★ 必采缺口定向重测", "状态": "总预算不足，短路（下批重测）", "耗时秒": 0.0})

    rep = {
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "总预算秒": args.total, "实际耗时秒": round(time.monotonic() - t0, 1),
        "离线": args.offline, "逐项": rows,
        "结论": ("并行完成；后续 6 个 step 只读本结果打印，不再重复发网络请求"
                 if not args.offline else "离线自检：未发请求"),
        "诚实边界": "超时 / 失败项复用仓库已有端点与体检结果，不编造数据；"
                    "采集成败由本批 run.py 的实时源触达率判定。",
    }
    (BASE / "data/preflight_result.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"耗时秒": rep["实际耗时秒"], "逐项": [
        {"项": r["项"], "状态": r["状态"], "耗时秒": r.get("耗时秒", 0)} for r in rows]},
        ensure_ascii=False, indent=1))
    print(f"[预探测] 总耗时 {rep['实际耗时秒']}s / 预算 {args.total}s | 报告 data/preflight_result.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
