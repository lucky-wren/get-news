#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资讯获取 · 「非独立站点 / DNS 未解析 逐条解析」防回退验收。

用户 2026-11-20 质疑：「188 个非独立站点、107 个 DNS 未解析，你意思是解决不了？」
本脚本锁死：这两类必须逐条解析成可拉取通路，且关键通路真实实测可达。

  python scripts/verify_channel_resolve.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, why: str) -> None:
        checks.append((name, bool(ok), why))

    # 自己真跑一次（含真实 HTTP 请求），不依赖上一次落盘快照是否带实测。
    # 这是防回退的关键：只读取旧 json 会被 probe=False 的离线覆盖骗过去。
    from news_pipeline import channel_resolve as _cr
    try:
        _fresh = _cr.run(probe=True)
        add("⓪ 解析引擎真跑成功（含实测）",
            _fresh.get("零通路条数", 1) == 0,
            f"原两类 {_fresh.get('原两类合计')} → 已解析 {_fresh.get('已解析条数')}")
    except Exception as exc:
        add("⓪ 解析引擎真跑成功（含实测）", False, f"{type(exc).__name__}: {exc}")

    rp = BASE / "data" / "channel_resolved.json"
    routes = BASE / "data" / "channel_routes.yaml"
    reach = BASE / "news_pipeline" / "reach.py"
    flow = BASE / ".github" / "workflows" / "daily-news.yml"

    add("① channel_routes.yaml 存在且规则数 ≥ 25",
        routes.exists() and len(re.findall(r"^\s+- keys:", routes.read_text(encoding="utf-8"), re.M)) >= 25,
        f"规则数 {len(re.findall(r'^\s+- keys:', routes.read_text(encoding='utf-8'), re.M)) if routes.exists() else 0}")

    if not rp.exists():
        add("② channel_resolved.json 存在", False, "先跑 python -m news_pipeline.channel_resolve --probe")
        d = {}
    else:
        d = json.loads(rp.read_text(encoding="utf-8"))
        add("② channel_resolved.json 存在", True, f"生成于 {d.get('generated_at')}")
        add("③ 原两类合计 = 已解析条数 且 零通路 = 0",
            d.get("原两类合计", 0) == d.get("已解析条数", 0) > 0 and d.get("零通路条数", 1) == 0,
            f"原两类 {d.get('原两类合计')} | 已解析 {d.get('已解析条数')} | 零通路 {d.get('零通路条数')}")
        st = d.get("解析状态分布") or {}
        add("④ 两类各自都有解析结果（188 虚拟归并 / 112 DNS 替代）",
            any("虚拟渠道归并" in k for k in st) and any("DNS 替代回查" in k for k in st),
            f"状态分布 {st}")
        tp = d.get("通路类型分布") or {}
        need = {"mother_endpoint", "search", "official_api", "aggregate", "digest", "mirror"}
        add("⑤ 通路类型覆盖 ≥6 类（不是只写一句「换网络重测」）",
            len(need & set(tp)) >= 6, f"通路分布 {tp}")
        pr = d.get("实测") or {}
        rows = pr.get("明细") or []
        bing = [p for p in rows if "Bing site:" in str(p.get("名称", "")) and p.get("http") == 200]
        with_time = [p for p in bing if p.get("has_time")]
        add("⑥ DNS 失败域名 site: 回查实测 200 且带时间字段（决定性证据）",
            len(bing) >= 2 and len(with_time) >= 2,
            f"Bing 200 {len(bing)} 条 | 带 pubDate {len(with_time)} 条 | "
            f"全部 HTTP200 {pr.get('HTTP200')}/{pr.get('请求数')}")
        cctv = [p for p in rows if "央视网" in str(p.get("名称", "")) and p.get("http") == 200]
        add("⑦ 非独立站点归并到已在采时间流（央视 JSONP 200 带时间）",
            bool(cctv) and bool(cctv[0].get("has_time")),
            f"央视 {[(p.get('items'), p.get('has_time')) for p in cctv]}")
        rr = d.get("逐渠道") or []
        no_tf = [r for r in rr if not any(p.get("时间字段依据") for p in r.get("可拉取通路", []))]
        add("⑧ 逐渠道都写了时间字段依据与启用条件",
            len(rr) > 0 and not no_tf,
            f"逐渠道 {len(rr)} 条 | 缺时间字段依据 {len(no_tf)} 条")
        add("⑨ 诚实边界写明「已解析 ≠ 已抓到」",
            "不等于本批已抓到" in str(d.get("诚实边界", "")), str(d.get("诚实边界", ""))[:90])

    rt = reach.read_text(encoding="utf-8") if reach.exists() else ""
    add("⑩ reach.py 不再把「非独立站点」当终态（已接入解析）",
        "已解析（虚拟渠道归并）" in rt and "channel_resolve" in rt,
        f"含解析调用 {'是' if 'channel_resolve' in rt else '否'}")
    add("⑪ reach.py DNS 分支改为检索替代实测（不再只写换网络重测）",
        "已解析（DNS 替代回查）" in rt and "bing.com/search?q=" in rt,
        "DNS 分支含 Bing site: 模板")
    fl = flow.read_text(encoding="utf-8") if flow.exists() else ""
    pf = (BASE / "scripts" / "preflight.py").read_text(encoding="utf-8") \
        if (BASE / "scripts" / "preflight.py").exists() else ""
    add("⑫ CI 工作流含两类渠道解析步骤（调用点在并行预探测）",
        "两类渠道逐条解析" in fl and "news_pipeline.channel_resolve" in pf
        and '"--probe"' in pf and "scripts/preflight.py" in fl,
        "workflow 打印结果 / preflight.py 真发请求，不再串行重复探测")
    add("⑬ Excel 工程留痕 L 已落地",
        "工程留痕 L ·" in (BASE / "news_pipeline" / "exporter.py").read_text(encoding="utf-8"),
        "exporter.py 含留痕 L")

    bad = [n for n, ok, _ in checks if not ok]
    out = {"检查数": len(checks), "PASS": len(checks) - len(bad), "FAIL": len(bad),
           "失败项": bad,
           "明细": [{"项": n, "结果": "PASS" if ok else "FAIL", "依据": w} for n, ok, w in checks]}
    dest = BASE / "output" / "channel_resolve_verify.json"
    dest.parent.mkdir(exist_ok=True, parents=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("检查数", "PASS", "FAIL", "失败项")}, ensure_ascii=False))
    for n, ok, w in checks:
        if not ok:
            print(f"  FAIL | {n} | {w}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
