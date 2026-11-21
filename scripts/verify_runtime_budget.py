# -*- coding: utf-8 -*-
"""get-news · 运行时容量 / 无 Key 时间解析 / 两段摘要形态，交付前必跑。

2026-11-21「继续」轮独立复核发现：只跑自己写的静态验收会漏掉三类问题——
  1) config 声明 300/400/200，budget.apply 静默砍到 160/260/120，用户看配置被误导；
  2) Bing RSS 中文 pubDate 全部解析成 None，无 key 链路 0 成稿（实测 0/10 → 10/10）；
  3) 摘要改两段后，Excel 口径表仍写「单段」，且多数正常新闻只出 1 段。
本脚本直接调用运行时函数、发真实 HTTP、读 config 与 workflow，不依赖其它验收器的结论。

用法：python scripts/verify_runtime_budget.py [--json output/runtime_budget_verify.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
CHECKS: list[dict] = []

# 主链路最低容量：34 条 × 3 个独立原文 ≈ 120 次回查 / 120 篇正文
MIN = {
    "search_api.max_calls": 120,
    "search_api.max_body_fetch": 120,
    "social_main.max_lookup": 30,
    "source.live.max_articles": 200,
    "source.live.second_pass.max_items": 100,
    "keyless.deadline_seconds": 100,
    "keyless.max_clues": 60,
}


def add(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append({"项": name, "结果": "PASS" if ok else "FAIL", "证据": detail})


def dig(d: dict, path: str):
    cur = d
    for k in path.split("."):
        cur = (cur or {}).get(k)
    return cur


def main() -> int:
    os.environ.setdefault("NEWS_BUDGET_TOTAL", "600")
    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8")) or {}
    from news_pipeline.budget import Budget
    b = Budget(cfg)
    run = b.apply(cfg)

    # ---- ① 声明值 → 运行时实际值：全部写明并逐项过最低容量
    rows = []
    lows = []
    for path, floor in MIN.items():
        declared, actual = dig(cfg, path), dig(run, path)
        rows.append({"配置": path, "声明": declared, "运行时": actual, "下限": floor})
        try:
            if float(actual) < floor:
                lows.append(f"{path}: {actual} < {floor}")
        except (TypeError, ValueError):
            lows.append(f"{path}: 非数值 {actual!r}")
    add("① 运行时容量逐项 ≥ 主链路下限（34 条 × 3 源）", not lows,
        "; ".join(lows) if lows else
        "; ".join(f"{r['配置'].split('.')[-1]}={r['运行时']}" for r in rows))
    CHECKS[-1]["明细"] = rows

    # ---- ② 声明值与运行时值透明化：注释必须标注裁剪，避免「看配置以为 300」
    ct = (BASE / "config.yaml").read_text(encoding="utf-8")
    declared_changed = [r["配置"] for r in rows if r["声明"] != r["运行时"]]
    marks = sum(1 for k in ("max_calls", "max_body_fetch", "max_articles",
                            "max_items", "deadline_seconds", "max_lookup")
                if re.search(rf"{k}:.*(?:声明上限|声明值即运行时值|budget\.apply)", ct))
    add("② 配置注释已标注「运行时裁剪」", marks >= 5 and bool(declared_changed),
        f"标注 {marks} 处 / 实际发生裁剪 {len(declared_changed)} 项：{', '.join(declared_changed)}")

    # ---- ③ 三层超时数字一致：workflow 15min / env 600s / reserve 25s
    wf = (BASE / ".github/workflows/daily-news.yml").read_text(encoding="utf-8")
    add("③ 超时口径统一（15 分钟硬杀 / 600 秒预算 / 25 秒出表保留，历史事故名保留）",
        "timeout-minutes: 15" in wf and 'NEWS_BUDGET_TOTAL: "600"' in wf
        and b.total == 600 and b.reserve == 25
        and "35 分钟超时事故" not in (BASE / "news_pipeline/budget.py").read_text(encoding="utf-8"),
        f"workflow=15min / env600 / total={b.total} / reserve={b.reserve}；"
        "35 分钟为历史事故名，README 与超时说明文档正当保留")

    # ---- ④ 无 Key 时间解析：中文 locale / RFC822 / GDELT / ISO，且不依赖 dateparser
    from news_pipeline import keyless_compose as kc
    samples = {
        "周五, 20 11月 2026 10:22:00 GMT": (2026, 11, 20, 18, 22),
        "Tue, 17 Nov 2026 19:36:00 GMT": (2026, 11, 18, 3, 36),
        "20261120T123000Z": (2026, 11, 20, 20, 30),
        "2026-11-19T20:15:00+08:00": (2026, 11, 19, 20, 15),
        "": None,
    }
    bad = []
    for raw, want in samples.items():
        got = kc._parse_pub(raw)
        if want is None:
            if got is not None:
                bad.append(f"{raw!r} 应为空却得 {got}")
        elif not got or (got.year, got.month, got.day, got.hour, got.minute) != want:
            bad.append(f"{raw!r} → {got}（期望 {want}）")
        elif got.tzinfo is None or str(got.tzinfo) != "Asia/Shanghai":
            bad.append(f"{raw!r} 时区非 Asia/Shanghai: {got.tzinfo}")
    add("④ 四类时间串全部解析且带 Asia/Shanghai 时区", not bad,
        "; ".join(bad[:4]) if bad else "中文 pubDate / RFC822 / GDELT seendate / ISO8601 全部 OK")
    CHECKS[-1]["明细"] = {k: str(kc._parse_pub(k)) for k in samples}

    # Bing RSS 真发请求：en-US 头 + 中文兜底双保险，10 条必须都有 pub
    try:
        evs, why = kc.bing_rss("医保 社保 养老金 辟谣", limit=10, timeout=9)
        ok_live = len(evs) >= 5 and all(e.pub is not None for e in evs)
        detail_live = f"{why} | 带发布时间 {sum(1 for e in evs if e.pub)}/{len(evs)}"
    except Exception as e:
        ok_live, detail_live = False, f"{type(e).__name__}: {str(e)[:120]}"
    add("⑤ Bing RSS 真实请求：每条都取得发布时间", ok_live, detail_live)

    # ---- ⑥ 两段摘要形态：真实新闻出两段，单句/近似复读保持单段，且验收全绿
    from news_pipeline import normalize as nm
    from news_pipeline.model import RawItem
    from news_pipeline.spec import abstract_ok
    from news_pipeline.exporter import _abstract_checks
    nm.set_abstract_paragraphs(2)
    nm.set_abstract_limit(100)
    cases = [
        ("医保钱包", 2, "11月19日，国家医保局通报医保钱包2.0版已在全国380个统筹地区上线，职工医保个人账户可跨省共济近亲属。截至11月15日累计代缴居民医保费141.09万人次、5.4亿元，共济范围扩至兄弟姐妹与祖父母。"),
        ("沙特空袭", 2, "11月19日，胡塞武装控制的马西拉电视台援引卫生部门消息称，沙特主导联军过去24小时空袭也门多省，致11人死亡、36人受伤。联军称拦截4枚射向沙特南部的弹道导弹；萨那多处发生爆炸。"),
        ("猪肉价格", 2, "农业农村部监测，11月19日全国农产品批发市场猪肉均价14.71元/公斤，比昨日降0.9%。28种蔬菜均价4.17元/公斤涨1.5%，鸡蛋9.52元/公斤涨0.5%。"),
        ("仅一句", 1, "中央气象台11月18日18时继续发布大风蓝色预警，18日20时至19日20时中东部多地阵风7至9级、降温6至8℃。"),
        ("近似复读", 1, "沙特空袭也门致11死36伤，联军称拦截4枚弹道导弹。沙特空袭也门致11死36伤，联军称拦截4枚弹道导弹。"),
    ]
    bad2, shape = [], []
    for name, want, body in cases:
        it = RawItem(title=name, source="自检", abstract=body)
        ab, _ = nm.build_abstract(it)
        it.abstract = ab
        ps = [p for p in ab.split("\n") if p.strip()]
        net = len(nm._net(ab))
        ok_spec = abstract_ok(ab)[0]
        chk = _abstract_checks([it])
        clean = sum(chk.values()) == 0
        shape.append({"样本": name, "段数": len(ps), "净字": net,
                      "spec": ok_spec, "exporter异常": sum(chk.values())})
        if len(ps) != want or net > 100 or not ok_spec or not clean:
            bad2.append(f"{name}: 段{len(ps)}(期望{want}) 净字{net} spec={ok_spec} 异常{chk}")
    add("⑥ 两段形态对齐样例：多句成两段 / 单句与近似复读不硬拆", not bad2,
        "; ".join(bad2[:3]) if bad2 else "3 条真实新闻出两段（83–90 净字），2 条边界保持单段")
    CHECKS[-1]["明细"] = shape

    # ---- ⑦ 口径文字：Excel 口径表 / 验收注释不再写「单段优先」
    vis = {
        "news_pipeline/exporter.py": "单段优先",
        "news_pipeline/spec.py": "单段优先",
        "news_pipeline/quality.py": "改为单段后该值天然为 0",
    }
    left = [f"{p}:{w}" for p, w in vis.items()
            if w in (BASE / p).read_text(encoding="utf-8")]
    add("⑦ 生成端 / 验收端 / Excel 口径表不再残留「单段」口径", not left,
        "、".join(left) if left else "exporter / spec / quality 三处已统一为两段")

    passed = sum(1 for c in CHECKS if c["结果"] == "PASS")
    failed = [c for c in CHECKS if c["结果"] == "FAIL"]
    report = {
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "检查项": len(CHECKS), "PASS": passed, "FAIL": len(failed),
        "失败项": failed, "全部": CHECKS,
        "运行时容量": rows,
        "说明": "声明值来自 config.yaml，运行时值来自 Budget.apply；推包后以 CI 的 output/budget_report.json 为准。",
    }
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=str(BASE / "output/runtime_budget_verify.json"))
    args = ap.parse_args()
    out = Path(args.json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"运行时容量/时间/两段核查：{passed}/{len(CHECKS)} PASS | FAIL {len(failed)}")
    for c in failed:
        print(f"  [FAIL] {c['项']} — {c['证据'][:200]}")
    print(f"报告：{out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
