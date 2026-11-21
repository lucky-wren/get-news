# -*- coding: utf-8 -*-
"""端到端冒烟：**真调 run_pipeline**，缩放时间模拟三种网络，实测是否按时出表。

之前十几轮的「全面核查」全是静态算式 / 字符串检查 / 离线回放，从没真跑过
run.py 测耗时，结果线上 15 分钟被硬杀（用户截图）。这个脚本补的就是那一环。

三个场景：
  worst —— 每个 HTTP 请求都挂到 timeout 才返回（最坏慢网络）；
  fast  —— 请求立即返回，验证 bounded 不误短路、产物不回退；
  hang  —— 把 source_expansion.collect 换成死等 60s（模拟某层完全卡住），
           验证 bounded 真截断、主线程仍出表。

每个场景断言：真跑无异常 / Excel 落盘 / 总耗时 ≤ 全局预算+出表余量 /
budget_report 写出 / 逐层实耗不超配额 / 主表非空。
SCALE 默认 0.02：真实 600s → 12s，超时比例与线上一致，结论可等比放大。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import yaml  # noqa: E402

SCALE = 0.02
SCENARIO = "worst"
TIME_KEYS = ("timeout", "deadline", "gap_seconds", "seconds", "total_collect", "reserve")


def scale_cfg(node, s: float, _parent: str = ""):
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            kl = str(k).lower()
            if _parent == "layer_seconds" and isinstance(v, (int, float)) \
                    and not isinstance(v, bool):
                out[k] = max(0.01, float(v) * s)
            elif isinstance(v, (int, float)) and not isinstance(v, bool) and any(
                    t in kl for t in TIME_KEYS):
                out[k] = max(0.01, float(v) * s)
            else:
                out[k] = scale_cfg(v, s, kl)
        return out
    if isinstance(node, list):
        return [scale_cfg(x, s) for x in node]
    return node


class SlowResp:
    status = 200

    def __init__(self, url, timeout):
        if SCENARIO == "fast":
            delay = 0.0
        else:
            delay = min(0.6, max(0.001, float(timeout or 0.2) * SCALE))
        if delay:
            time.sleep(delay)
        self._url = url
        self._text = ('<?xml version="1.0"?><rss version="2.0"><channel>'
                      '<item><title>医保政策发布 11月LPR报价出炉 维持不变</title>'
                      '<link>https://news.cctv.com/2026/11/20/a.html</link>'
                      '<pubDate>Fri, 20 Nov 2026 10:00:00 GMT</pubDate>'
                      '<description>11月20日,新增91种药品,预计减负400亿元。</description>'
                      '</item></channel></rss>')
        self._json = {"data": {"list": [
            {"title": "医保政策发布 新增91种药品", "focus_date": "2026-11-20 10:00:00",
             "url": "https://news.cctv.com/2026/11/20/a.html"}]}}
        class _H:
            def get_content_charset(self): return "utf-8"
        self.headers = _H()

    def read(self, *a): return self._text.encode("utf-8")
    def geturl(self): return self._url
    def raise_for_status(self): return None
    @property
    def text(self): return self._text
    def json(self): return self._json
    def __enter__(self): return self
    def __exit__(self, *a): return False


def slow_urlopen(req, timeout=None, **kw):
    return SlowResp(getattr(req, "full_url", str(req)), timeout)


def slow_requests_get(url, *, params=None, headers=None, timeout=None, **kw):
    return SlowResp(url, timeout)


def run_one(scenario: str, scale: float) -> dict:
    global SCALE, SCENARIO
    SCALE, SCENARIO = scale, scenario
    raw_cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8")) or {}
    cfg = scale_cfg(raw_cfg, scale)
    total = float(cfg.get("budget", {}).get("total_collect_seconds", 600 * scale))
    os.environ["NEWS_BUDGET_TOTAL"] = str(total)
    os.environ["NEWS_SKIP_FIXED_OUTPUT"] = "1"
    os.environ["FREE_SOURCES_OFFLINE"] = "0"

    smoke_dir = BASE / "output" / "smoke"
    smoke_dir.mkdir(parents=True, exist_ok=True)
    cfg.setdefault("output", {})["dir"] = str(smoke_dir)
    cfg["output"]["name"] = f"smoke_{scenario}_窗口期.xlsx"
    cfg["source"]["mode"] = "live"

    from news_pipeline.timewindow import build_window
    from news_pipeline.pipeline import run_pipeline
    win = build_window(start="2026-11-19 20:00", end="2026-11-20 20:00",
                       tz="Asia/Shanghai", interval="lo-ro")
    out = smoke_dir / f"smoke_{scenario}_窗口期.xlsx"
    if out.exists():
        out.unlink()
    br = BASE / "output" / "budget_report.json"
    if br.exists():
        br.unlink()

    # 立刻写一份初始报告：即使 python 被硬杀，也看得到预算与阶段
    t0 = time.monotonic()
    err, main_rows, cand_rows = None, -1, -1
    patches = [patch("urllib.request.urlopen", side_effect=slow_urlopen),
               patch("requests.get", side_effect=slow_requests_get),
               patch("requests.Session.get", side_effect=slow_requests_get)]
    if scenario == "hang":
        # 挂死**最大配额的主链路**（keyless 真实 185s），比挂 35s 的 expansion 更狠：
        # 旧版一旦卡在这里，整个 15min 都会被吃掉、Excel 永远落不了盘。
        patches.append(patch("news_pipeline.keyless_compose.batch_compose",
                             side_effect=lambda *a, **k: time.sleep(60)))
    from contextlib import ExitStack
    with ExitStack() as stack:
        for _p in patches:
            stack.enter_context(_p)
        try:
            R = run_pipeline(cfg, win, BASE, out)
            main_rows, cand_rows = len(R.get("ordered", [])), len(R.get("candidate", []))
        except Exception as e:
            import traceback
            err = f"{type(e).__name__}: {e}"
            traceback.print_exc()
    elapsed = time.monotonic() - t0

    reserve = float(cfg.get("budget", {}).get("export_reserve_seconds", 25 * scale))
    hard = total + max(1.0, reserve * 2)
    layers = []
    checks = []

    def ck(name, ok, detail=""):
        checks.append({"项": name, "结果": "PASS" if ok else "FAIL", "证据": str(detail)[:260]})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} — {str(detail)[:130]}")

    ck("真跑 run_pipeline 未抛异常", err is None, err or "ok")
    ck("五工作表 Excel 落盘（硬杀时这份文件根本不存在）",
       out.exists() and out.stat().st_size > 3000,
       f"{out.stat().st_size if out.exists() else 0} 字节")
    ck("总耗时 ≤ 全局预算 + 出表余量",
       elapsed <= hard,
       f"{elapsed:.2f}s / 预算 {total:.2f}s / 上限 {hard:.2f}s"
       f"（真实等效 {elapsed/scale:.0f}s / 600s）")
    ck("budget_report.json 已写出", br.exists(), str(br))
    if br.exists():
        d = json.loads(br.read_text(encoding="utf-8"))
        layers = d.get("逐层", [])
        over = [r for r in layers
                if float(r.get("实耗秒", 0)) > float(r.get("配额秒", 0)) * 1.5 + 0.5]
        ck("逐层实耗不超配额 1.5 倍（每层真限时）", not over,
           "超限:" + json.dumps(over, ensure_ascii=False)[:160] if over else
           "；".join(f"{r['层']}={r.get('实耗秒')}/{r.get('配额秒')}s" for r in layers)[:200])
        ck("报告实耗与真实耗时自洽", abs(float(d.get("实际耗时秒", 0)) - elapsed) < 2.0,
           f"报告 {d.get('实际耗时秒')}s / 实测 {elapsed:.2f}s | {d.get('结论')}")
        # 留一份场景独立报告，避免被下一场景覆盖
        (BASE / "output" / f"smoke_{scenario}_budget.json").write_text(
            json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    ck("主表与候选池非空（短路采集仍出表，不是空表交差）",
       main_rows > 0 and cand_rows > 0, f"主表 {main_rows} / 候选池 {cand_rows}")

    passed = sum(1 for c in checks if c["结果"] == "PASS")
    return {"场景": scenario, "SCALE": scale, "实耗秒": round(elapsed, 2),
            "真实等效秒": round(elapsed / scale, 1), "预算秒": round(total, 2),
            "Excel字节": out.stat().st_size if out.exists() else 0,
            "主表": main_rows, "候选池": cand_rows, "错误": err,
            "逐层": layers, "检查": checks, "通过": passed, "总计": len(checks)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=float, default=0.02)
    ap.add_argument("--scenarios", default="worst,fast,hang")
    ap.add_argument("--out", default="output/smoke_report.json")
    args = ap.parse_args()

    import warnings
    warnings.filterwarnings("ignore")
    all_runs = []
    for sc in [x.strip() for x in args.scenarios.split(",") if x.strip()]:
        print(f"\n===== 场景 {sc} (scale={args.scale}) =====")
        all_runs.append(run_one(sc, args.scale))

    total_p = sum(r["通过"] for r in all_runs)
    total_n = sum(r["总计"] for r in all_runs)
    failed = [f"{r['场景']}:{c['项']}" for r in all_runs
              for c in r["检查"] if c["结果"] == "FAIL"]
    rep = {"说明": "端到端真跑 run_pipeline（采集→筛选→评级→编排→五表出表），"
                   "缩放时间模拟最坏慢网络 / 正常快网络 / 单层挂死；非静态算式、非离线回放",
           "场景数": len(all_runs), "通过": total_p, "总计": total_n,
           "失败项": failed, "场景": all_runs}
    p = BASE / args.out
    p.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in all_runs:
        print(f"[{r['场景']}] {r['通过']}/{r['总计']} | 缩放 {r['实耗秒']}s "
              f"→ 真实等效 {r['真实等效秒']}s / 600s | 主表 {r['主表']} 条")
    print(f"\n{total_p}/{total_n} PASS → {p}")
    if failed:
        print("FAIL:", failed)
    # 清理冒烟产物，不污染真实交付目录
    for f in (BASE / "output" / "smoke").glob("smoke_*_窗口期.xlsx"):
        f.unlink()
    return 0 if total_p == total_n else 1


if __name__ == "__main__":
    raise SystemExit(main())
