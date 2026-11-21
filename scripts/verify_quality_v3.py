#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资讯获取 · 质量 v3 防回退验收（用户 2026-11-21「很多渠道没数据 / 一条 A 都没有」）。

锁死四件事：
  ① 列表页新闻硬闸：32 条真实噪声样本全拦、4 条真新闻全放行
  ② 事件聚类：MinHash-LSH 把三源转载聚成一个事件（n_src=3）
  ③ 事件级热度：48h/24h 半衰期产生 H_c>0.5，可把 Final 抬进 A 档
  ④ 真跑离线回放：等级分布 S+A>0，且**不污染正式 docs/downloads/latest.xlsx**
  ⑤ 两类渠道解析在线上「状态被全改写」时不再 0→0 假绿

  python scripts/verify_quality_v3.py
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from news_pipeline import quality_v3 as qv3          # noqa: E402
from news_pipeline import channel_resolve as cr       # noqa: E402
import yaml                                          # noqa: E402


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, why: str) -> None:
        checks.append((name, bool(ok), why))

    # ---- ① 模块自测 ----
    st = qv3.selftest()
    add("① quality_v3 自测 6/6（噪声硬闸 + 三源聚类 + H_c>0.5）",
        st["FAIL"] == 0, f"{st['PASS']}/{st['检查数']} | 失败 {st['失败项']}")

    # ---- ② config ----
    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8")) or {}
    sc, heat = (cfg.get("scoring") or {}), (cfg.get("heat") or {})
    add("② config: h_channel=heat + 规则版本写死 + 48h/24h/最少2参与方",
        sc.get("h_channel") == "heat"
        and heat.get("rule_version") == "heat-v1-48h-halflife24h"
        and heat.get("window_hours") == 48 and heat.get("half_life_hours") == 24
        and heat.get("min_participants") == 2,
        f"h_channel={sc.get('h_channel')} | {heat.get('rule_version')} | "
        f"{heat.get('window_hours')}h/{heat.get('half_life_hours')}h/min={heat.get('min_participants')}")
    add("② config: grade 阈值 / V_MAX / W_H 未被改动（不靠调阈值凑 A）",
        "V_MAX = 82.32" in (BASE / "news_pipeline" / "scoring.py").read_text(encoding="utf-8")
        and "S≥85" in (BASE / "news_pipeline" / "exporter.py").read_text(encoding="utf-8"),
        "A 由事件级热度自然产生，不是放宽 grade")

    # ---- ③ 采集硬闸 / 窗口硬闸 / 打分读取已接入 ----
    col = (BASE / "news_pipeline" / "collectors.py").read_text(encoding="utf-8")
    tw = (BASE / "news_pipeline" / "timewindow.py").read_text(encoding="utf-8")
    pl = (BASE / "news_pipeline" / "pipeline.py").read_text(encoding="utf-8")
    sco = (BASE / "news_pipeline" / "scoring.py").read_text(encoding="utf-8")
    add("③ collect_html_lists 入库前调用 is_news_item 硬闸",
        "from .quality_v3 import is_news_item" in col and "self._skip" in col,
        "列表页导航/机构职能/JS模板/模型卡/旧公告在入库前拦下")
    add("③ 站内时间倒排无发布时间不再「时点推定」放行（旧闻出池）",
        "站内时间倒排条目发布时间未取得 → 不入池" in tw,
        "2026-03~10 的 CNCERT 旧漏洞公告不再进入 11-20 窗口")
    add("③ pipeline 在 compute_scores 之前做事件聚类+热度+写回",
        pl.find("qv3.apply_heat") < pl.find("compute_scores(admitted"),
        "修掉「打分时 _independent_sources 恒为 1 → A 恒空」的顺序 bug")
    add("③ scoring 读 event_H_c（事件级热度），不覆盖为 None",
        "use_heat" in sco and "event_H_c" in sco,
        "Final = V/V_MAX×100 + 100×W_H×H_c，公式与阈值不变")

    # ---- ④ 两类渠道解析：线上状态全改写也不假绿 ----
    reach_p = BASE / "data" / "channel_reach.json"
    if reach_p.exists():
        d = json.loads(reach_p.read_text(encoding="utf-8"))
        sim = json.loads(json.dumps(d))
        for r in sim["渠道"]:
            r["状态"] = "实证端点（已登记并本次采集）"
            r["状态码"] = "200"
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            tp, op = tdp / "reach.json", tdp / "resolved.json"
            tp.write_text(json.dumps(sim, ensure_ascii=False), encoding="utf-8")
            _r, _o = cr.REACH, cr.OUT
            cr.REACH, cr.OUT = tp, op
            try:
                rr = cr.run(probe=False)
            finally:
                cr.REACH, cr.OUT = _r, _o
        add("④ 线上「状态全改写」仍能解析虚拟渠道（不再 0→0 假绿）",
            rr["原两类合计"] > 0 and rr["已解析条数"] == rr["原两类合计"]
            and rr["零通路条数"] == 0,
            f"模拟线上 → {rr['原两类合计']} 条 / 已解析 {rr['已解析条数']} / 零通路 {rr['零通路条数']}")
    else:
        add("④ 两类渠道解析模拟", False, "channel_reach.json 缺失")

    # ---- ⑤ 真跑离线回放，验证 A 真的产生（且恢复正式产物） ----
    latest = BASE / "docs" / "downloads" / "latest.xlsx"
    backup = None
    replay = {"S_plus_A": 0, "等级分布": {}, "rc": -1}
    try:
        if latest.exists():
            backup = Path(tempfile.mkdtemp()) / "latest.bak.xlsx"
            shutil.copy(latest, backup)
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run(
                [sys.executable, "run.py", "--source", "target",
                 "--start", "2026-11-18 20:00", "--end", "2026-11-19 20:00",
                 "--interval", "lo-ro", "--json-summary",
                 "--output-dir", td],
                cwd=str(BASE), capture_output=True, text=True, timeout=300,
                env={**os.environ, "NEWS_SKIP_FIXED_OUTPUT": "1"})
            replay["rc"] = r.returncode
            js = sorted(glob.glob(str(Path(td) / "*_窗口期.json")),
                        key=os.path.getmtime, reverse=True)
            if js:
                j = json.loads(Path(js[0]).read_text(encoding="utf-8"))
                gd = {}
                for src in (j, j.get("采集质量", {}), j.get("运行指标", {})):
                    if isinstance(src, dict) and isinstance(src.get("等级分布"), dict):
                        gd = src["等级分布"]
                        break
                replay["等级分布"] = gd
                replay["S_plus_A"] = int(gd.get("S", 0)) + int(gd.get("A", 0))
                replay["主表条数"] = j.get("主表条数") or j.get("主表")
            xs = sorted(glob.glob(str(Path(td) / "*_窗口期.xlsx")),
                        key=os.path.getmtime, reverse=True)
            if xs:
                from openpyxl import load_workbook
                wb = load_workbook(xs[0], read_only=True, data_only=True)
                txt = "\n".join(" | ".join(str(c or "") for c in row)
                                for row in wb["口径与稿外信息"].iter_rows(values_only=True))
                replay["留痕M"] = "工程留痕 M · 质量 v3 事件级热度" in txt
                replay["留痕M_a"] = "工程留痕 M-a" in txt
                wb.close()
    except Exception as exc:
        replay["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if backup and backup.exists() and latest.exists():
            shutil.copy(backup, latest)     # 绝不把离线回放产物留给正式页面
    add("⑤ 离线回放 rc=0 且 S+A>0（A 档不再恒空）",
        replay["rc"] == 0 and replay["S_plus_A"] > 0,
        f"rc={replay['rc']} | S+A={replay['S_plus_A']} | 等级 {replay['等级分布']}")
    add("⑤ 回放产物写入工程留痕 M / M-a（A 档依据可逐格核对）",
        bool(replay.get("留痕M")) and bool(replay.get("留痕M_a")),
        f"M={replay.get('留痕M')} | M-a={replay.get('留痕M_a')}")
    add("⑤ 回放未污染正式 docs/downloads/latest.xlsx",
        latest.exists() and latest.stat().st_size > 100_000,
        f"恢复后 {latest.stat().st_size if latest.exists() else 0} 字节")

    bad = [n for n, ok, _ in checks if not ok]
    out = {"检查数": len(checks), "PASS": len(checks) - len(bad), "FAIL": len(bad),
           "失败项": bad, "回放": replay, "selftest": st,
           "明细": [{"项": n, "结果": "PASS" if ok else "FAIL", "依据": w}
                    for n, ok, w in checks]}
    dest = BASE / "output" / "quality_v3_verify.json"
    dest.parent.mkdir(exist_ok=True, parents=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("检查数", "PASS", "FAIL", "失败项")},
                     ensure_ascii=False))
    print(f"[回放] rc={replay['rc']} | S+A={replay['S_plus_A']} | {replay['等级分布']}")
    for n, ok, w in checks:
        if not ok:
            print(f"  FAIL | {n} | {w}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
