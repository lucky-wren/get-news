"""无 key 多源成稿链路验收（可复跑，离线 mock + 真实探测）。

    python scripts/verify_keyless.py --json output/keyless_verify.json

不配任何 Secret；真实探测在受限网络下允许 403，只如实记录，不判失败。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402
from news_pipeline import keyless_compose as kc  # noqa: E402
from news_pipeline.timewindow import build_window  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
WIN = build_window(start="2026-11-18 20:00", end="2026-11-19 20:00", interval="lo-ro")


def checks() -> dict:
    out = {"检查项": [], "硬失败": []}

    def ck(name: str, ok: bool, detail: str = "") -> None:
        out["检查项"].append({"项": name, "结果": "PASS" if ok else "FAIL", "说明": detail})
        if not ok:
            out["硬失败"].append(name)

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    kl = cfg.get("keyless", {}) or {}
    sa = cfg.get("search_api", {}) or {}
    sm = cfg.get("social_main", {}) or {}

    ck("keyless 主链路启用", bool(kl.get("enabled", False)))
    ck("provider_order 含 keyless", "keyless" in str(sa.get("provider_order", "")),
       str(sa.get("provider_order")))
    ck("搜索预算 ≥300 次", int(sa.get("max_calls", 0)) >= 300, str(sa.get("max_calls")))
    ck("正文补采预算 ≥300 篇", int(sa.get("max_body_fetch", 0)) >= 300,
       str(sa.get("max_body_fetch")))
    _ml = int(sm.get("max_lookup", -1))
    ck("社交词条逐条回查受有限配额约束（0=不限会拖死整批）",
       1 <= _ml <= 40 and float(sm.get("lookup_deadline_seconds", 0)) > 0,
       f"max_lookup={_ml} deadline={sm.get('lookup_deadline_seconds')}s")
    ck("不依赖任何 key", all(k not in str(kl.get("provider_order", ""))
                            for k in ("serper", "tavily")),
       str(kl.get("provider_order")))
    _wf = (ROOT / ".github/workflows/daily-news.yml").read_text(encoding="utf-8")
    import re as _re
    _m = _re.search(r"- name: 采集 → 筛选评级.*?timeout-minutes:\s*(\d+)", _wf, _re.S)
    ck("workflow 采集步 10–15 分钟兜底 + 脚本自带全局预算",
       bool(_m) and 10 <= int(_m.group(1)) <= 15 and "NEWS_BUDGET_TOTAL" in _wf,
       f"timeout-minutes={_m.group(1) if _m else '缺失'} / NEWS_BUDGET_TOTAL={'NEWS_BUDGET_TOTAL' in _wf}")
    ck("pipeline 含 0a-0 主链路",
       "0a-0" in (ROOT / "news_pipeline/pipeline.py").read_text(encoding="utf-8"))
    _pl = (ROOT / "news_pipeline/pipeline.py").read_text(encoding="utf-8")
    ck("两段摘要保护存在（KEYLESS + EVENT_COMPOSER 成稿条目均不被压成单段）",
       '("KEYLESS", "EVENT_COMPOSER")' in _pl and "EVENT_COMPOSER" in _pl)

    # 模块自检（解析 / 两段 / 净字 / 去重 / 真实探测）
    st = kc.self_test()
    ck("模块自检", bool(st.get("通过")), "; ".join(st.get("错误", [])))
    out["真实探测"] = st.get("真实探测", {})
    out["摘要样例"] = st.get("摘要样例", "")
    out["摘要净字"] = st.get("摘要净字", 0)

    # 事件成稿形态：3 个独立域名 → 多渠道来源列 / 两段 / e 主键 / 记录类型
    evs = []
    bodies = [
        "11月19日，沙特主导联军空袭也门多省，致11人死亡、36人受伤。联军称拦截4枚弹道导弹。",
        "新华社另报1人死亡、4人受伤，两家口径并列标注。沙特民防部门证实萨那多处发生爆炸。",
        "马西拉电视台援引卫生部门消息称，过去24小时空袭致11人死亡、36人受伤，数字仍在核对。",
    ]
    titles = ["沙特空袭也门致11死36伤 联军称拦截4枚弹道导弹",
              "沙特空袭也门伤亡数字口径不一 新华社另报1死4伤",
              "也门多省遭空袭 马西拉电视台通报伤亡"]
    for i, (dom, body, title) in enumerate(zip(
            ("xinhuanet.com", "cctv.com", "thepaper.cn"), bodies, titles)):
        evs.append(kc.Evidence(
            url=f"https://{dom}/a{i}", title=title, domain=dom, source=dom,
            provider="gnews", pub=datetime(2026, 11, 19, 18, 0, tzinfo=TZ),
            pub_raw="2026-11-19 18:00:00", basis="RSS pubDate",
            body=body, body_from="独立原文正文"))

    def fake_body(e, timeout=9.0, use_jina=True, jina_timeout=9.0):
        return e.body

    with patch.object(kc, "_fetch_body", side_effect=fake_body):
        item, tr = kc.compose_event("沙特空袭也门", evs, win=WIN, cfg=cfg, seq=7,
                                    origin="主题词表")
    ck("事件成稿产出 RawItem", item is not None, tr.reason)
    if item:
        ck("来源列 ≥3 个独立渠道", item.source.count("·") >= 2, item.source)
        ck("两段摘要", "\n" in (item.abstract or ""), repr(item.abstract)[:120])
        _parts = (item.abstract or "").split("\n", 1)
        ck("两段不复读", len(_parts) == 2 and
           kc._bigram_jaccard(_parts[0], _parts[1]) <= 0.45,
           f"Jaccard={kc._bigram_jaccard(_parts[0], _parts[1]) if len(_parts) == 2 else 1:.2f}")
        ck("摘要净字 ≤100", kc._net(item.abstract) <= 100, str(kc._net(item.abstract)))
        ck("事件主键 e/s", getattr(item, "event_id", "").startswith(("e", "s")),
           getattr(item, "event_id", ""))
        ck("记录类型按证据构成", item.record_type in
           ("多渠道合并", "联网检索独立原文", "社交词条回查成稿"), item.record_type)
        ck("发布时间落窗口内", WIN.contains(item.pub), str(item.pub))
        ck("证据 URL 逐条留痕", len((item.raw or {}).get("证据来源", [])) >= 3)

    # 不足 2 独立域名 → 不成稿、不冒充
    with patch.object(kc, "_fetch_body", side_effect=fake_body):
        none, tr2 = kc.compose_event("孤立词条", evs[:1], win=WIN, cfg=cfg, seq=8)
    ck("独立源不足不成稿", none is None, tr2.reason)

    # search_api provider 接入（离线 mock，不联网）
    from news_pipeline import search_api
    search_api.reset()
    mock_evs = [kc.Evidence(url=f"https://xinhuanet.com/x{i}", title=f"医保钱包{i}",
                            domain="xinhuanet.com", source="新华网", provider="gnews",
                            pub=datetime(2026, 11, 19, 12, 0, tzinfo=TZ),
                            pub_raw="2026-11-19 12:00:00", basis="RSS pubDate")
                for i in range(3)]
    with patch.object(kc, "search_clue", return_value=(mock_evs, kc.ClueTrace("q", "主题词表", domains=3))):
        hit = search_api.search_best("医保钱包", win=WIN,
                                     cfg={"search_api": {"provider_order": "keyless",
                                                         "max_calls": 2,
                                                         "results_per_query": 8}})
    ck("search_api keyless 分支不报错且留痕",
       len(search_api.TRACE) >= 1, search_api.stats().__str__()[:160])

    out["通过"] = not out["硬失败"]
    out["统计"] = {"检查项": len(out["检查项"]),
                   "PASS": sum(1 for x in out["检查项"] if x["结果"] == "PASS"),
                   "FAIL": len(out["硬失败"])}
    out["结论"] = ("无 key 多源检索 + 事件级成稿链路验收通过"
                   if out["通过"] else "验收失败：" + "、".join(out["硬失败"]))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    r = checks()
    print(json.dumps(r, ensure_ascii=False, indent=2))
    if a.json:
        p = pathlib.Path(a.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[落盘] {p}")
    sys.exit(0 if r["通过"] else 1)
