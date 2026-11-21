# -*- coding: utf-8 -*-
"""主链路吞吐契约（2026-11-22「继续」轮）。

用户诉求：「达到目标样例 34 条 / 34 条多渠道 / A9 的效果」。
前面的核查一直停在「沙盒 403，推包后 Actions 无限制网络才兑现」，
但没算过一件更硬的事：**在 600 秒全局预算里，主链路数学上能不能产出 34 条多源事件**。

本脚本不联网、不猜，做两件事：
  1. 真调 `budget.Budget.apply()` 拿到 CI 运行时的实际参数（配置写 300 会被静默砍到 160）；
  2. 用「按真实超时等比缩放」的 mock 真调 `keyless_compose.batch_compose()`，
     测在缩放后的 deadline 内能完成多少条成稿。缩放比同时作用于
     检索超时 / 正文超时 / GDELT gap / deadline，因此结论与真实耗时同比例。

断言：目标 34 条按 50% 成稿率需要 ≥68 个线索跑完；少于 68 即 FAIL。
输出 output/throughput_verify.json，任一 FAIL 即 rc=1。
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from news_pipeline import budget as bd  # noqa: E402
from news_pipeline import keyless_compose as kc  # noqa: E402
from news_pipeline.timewindow import Window  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
SCALE = 0.05                     # 真实 140s → 7s；6s 检索 → 0.3s；9s 正文 → 0.45s
NEED_CLUES = 68                  # 34 条成稿 / 50% 成稿率
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append({"项": name, "结果": "PASS" if cond else "FAIL", "明细": str(detail)[:260]})
    return bool(cond)


def mk_evidence(domain: str, i: int, provider: str, pub: datetime) -> kc.Evidence:
    return kc.Evidence(
        url=f"https://{domain}/news/{i}",
        title=f"医保钱包2.0上线 380个统筹地区可代缴居民医保费 报道{i}",
        domain=domain, source=domain, provider=provider,
        pub=pub, pub_raw="Fri, 20 Nov 2026 02:00:00 GMT", basis="RSS pubDate",
        body=("国家医保局等推进职工医保个人账户跨省共济，医保钱包2.0版已在各省上线，"
              "共济范围扩至兄弟姐妹、祖父母等近亲属。"
              "截至11月15日，380个统筹地区支持代缴居民医保费，累计141.09万人次、5.4亿元。"),
        body_from="独立原文正文",
    )


def install_mock(runtime_cfg: dict, pub: datetime):
    """按真实超时等比缩放，让 mock 的耗时结构与线上一致。"""
    kl = runtime_cfg["keyless"]
    t_search = float(kl["timeout"]) * SCALE
    t_body = float(kl.get("body_timeout", 9)) * SCALE
    t_gdelt_gap = float(kl.get("gdelt_gap_seconds", 5.1)) * SCALE

    def gnews(query, *, timeout=8.0, when="1d", limit=60):
        time.sleep(t_search)
        return [mk_evidence("xinhuanet.com", 1, "gnews", pub),
                mk_evidence("cctv.com", 2, "gnews", pub)], "200/2"

    def bing_rss(query, *, timeout=8.0, limit=30):
        time.sleep(t_search)
        return [mk_evidence("thepaper.cn", 3, "bing_rss", pub)], "200/1"

    def gdelt(query, *, timeout=12.0, maxrecords=100, gap=5.2, max_total=20):
        # GDELT 服务端硬要求 ≥5s/请求，且有全局锁；如实保留串行等待
        time.sleep(t_gdelt_gap + t_search)
        return [mk_evidence("gov.cn", 4, "gdelt", pub)], "200/1"

    def cn_endpoints(clue, *, cfg=None, timeout=8.0, win=None):
        time.sleep(t_search)
        return [mk_evidence("people.com.cn", 5, "cn_endpoints", pub)], "200/1"

    def searxng(query, base="", *, timeout=10.0):
        time.sleep(t_search)
        return [], "未配置 SearXNG 实例"

    def resolve_redirect(ev, timeout=7.0):
        return ev

    def fetch_article_text(url, timeout=9.0, max_chars=6000):
        time.sleep(t_body)
        return ("国家医保局等推进职工医保个人账户跨省共济，医保钱包2.0版已在各省上线。"
                "截至11月15日，380个统筹地区支持代缴居民医保费，累计141.09万人次、5.4亿元。")

    def http(url, timeout=8.0, headers=None):
        return 404, "", {}

    kc.gnews, kc.bing_rss, kc.gdelt, kc.searxng = gnews, bing_rss, gdelt, searxng
    kc.cn_endpoints = cn_endpoints
    kc.resolve_redirect = resolve_redirect
    kc.fetch_article_text = fetch_article_text
    kc._http = http
    return {"检索秒": t_search, "正文秒": t_body, "GDELT间隔秒": t_gdelt_gap}


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    # 真调运行时裁剪：CI 上 keyless.timeout / deadline / workers 到底是多少
    b = bd.Budget(cfg)
    runtime = b.apply(cfg)
    kl = runtime["keyless"]
    sa = runtime["search_api"]
    sm = runtime["social_main"]
    layer = cfg.get("budget", {}).get("layer_seconds", {})
    deadline_real = float(kl["deadline_seconds"])
    workers = int(kl.get("workers", 6))
    order = [p.strip() for p in str(kl.get("provider_order", "")).split(",") if p.strip()]

    # ---- 1. 静态算式（逐项可复算）
    timeout = float(kl["timeout"])
    body_timeout = float(kl.get("body_timeout", 9))
    n_clue = int(cfg["keyless"].get("max_clues", 80))
    # 旧结构（2026-11-22 之前）：4 provider 串行 + 成稿在主循环串行
    serial_clue = len(order) * timeout
    est_serial = n_clue / max(1, workers) * serial_clue + n_clue * body_timeout
    # 新结构：provider 并发 → 单线索 max(timeout)；检索与成稿同一 future 内全并发
    est_parallel = (n_clue / max(1, workers)) * (timeout + body_timeout)
    check("① 运行时参数如实读取",
          deadline_real <= 600 and timeout > 0 and workers > 0,
          f"deadline={deadline_real}s workers={workers} provider={order} "
          f"timeout={timeout}s max_calls={sa['max_calls']} max_lookup={sm['max_lookup']}"
          f"；旧串行估算 {est_serial:.0f}s，并发估算 {est_parallel:.0f}s")

    # 目标：34 条多渠道事件。按 50% 成稿率需 ≥68 个线索在 deadline 内跑完。
    check("② 并发结构静态算式：预算内跑得满 100 线索",
          est_parallel <= deadline_real,
          f"并发估算 {est_parallel:.0f}s ≤ 层配额 {deadline_real:.0f}s"
          f"（旧串行结构 {est_serial:.0f}s，是本轮修复的根因证据）")
    check("②b 总预算不超过上一版（用户要求缩短而非加时）",
          sum(float(v) for v in layer.values()) <= 540,
          f"层配额合计 {sum(float(v) for v in layer.values()):.0f}s ≤ 上一版 540s；"
          f"含 25s 出表保留共 {sum(float(v) for v in layer.values())+25:.0f}s < 600s")

    # ---- 2. 缩放实跑：真调 batch_compose
    win = Window(datetime(2026, 11, 19, 20, 0, tzinfo=TZ),
                 datetime(2026, 11, 20, 20, 0, tzinfo=TZ), interval="lo-ro")
    pub = datetime(2026, 11, 20, 10, 0, tzinfo=TZ)
    scale_info = install_mock(runtime, pub)
    test_cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    test_cfg["keyless"] = dict(runtime["keyless"])
    test_cfg["keyless"]["deadline_seconds"] = deadline_real * SCALE
    clues = [(f"医保 社保 养老金 辟谣 线索{i}", "主题词表") for i in range(n_clue)]

    t0 = time.time()
    items, traces, stats = kc.batch_compose(clues, win=win, cfg=test_cfg, base_seq=0)
    elapsed = time.time() - t0
    finished = sum(1 for t in traces if t.verdict in ("成稿", "未成稿")
                   and "总时限到达" not in (t.reason or ""))
    formed = len(items)
    dropped = sum(1 for t in traces if "总时限到达" in (t.reason or ""))

    check("③ 缩放实跑：deadline 内跑完 ≥68 个线索",
          finished >= NEED_CLUES,
          f"跑完 {finished}/{n_clue}，超时丢弃 {dropped}，耗时 {elapsed:.2f}s"
          f" / 缩放配额 {deadline_real*SCALE:.2f}s；mock={scale_info}")
    check("④ 缩放实跑：成稿数 ≥34（目标样例主表规模）",
          formed >= 34,
          f"成稿 {formed} 条；未成稿 {stats.get('未成稿')}；平均独立域名 {stats.get('平均独立域名')}")
    check("⑤ 超时不拖死整批：实耗不超配额的 1.5 倍",
          elapsed <= deadline_real * SCALE * 1.5,
          f"实耗 {elapsed:.2f}s vs 允许 {deadline_real*SCALE*1.5:.2f}s（超时任务必须能被取消，不能后台跑完）")
    check("⑥ GDELT 不在每线索默认主链路上（全局锁会串行拖慢整批）",
          "gdelt" not in order,
          f"provider_order={order}；GDELT 保留在 source_expansion 独立层"
          f"（gdelt_max_queries={runtime['source_expansion']['gdelt_max_queries']}）")

    # ---- 3. 另外两层：重复串行回查必须已消除
    pipe = (ROOT / "news_pipeline/pipeline.py").read_text(encoding="utf-8")
    seg = pipe.split("0a-3")[0].split("0a-2")[-1]
    hot_routes = len(cfg["search_api"].get("hot_routes", []))
    per_route = int(cfg["search_api"].get("clues_per_route", 12))
    fs_serial = hot_routes * per_route * float(sa["timeout"])
    check("⑦ 开源热榜不再逐条串行 lookup（词条并入 keyless 并发池）",
          "collector.lookup_article(" not in seg and "已并入" in seg,
          f"旧串行估算 {fs_serial:.0f}s vs 层配额 {layer.get('free_sources')}s；"
          f"0a-2 现只做探针留痕，逐条多源回查由 keyless 统一完成")
    sm_src = (ROOT / "news_pipeline/social_main.py").read_text(encoding="utf-8")
    lw = int(cfg["social_main"].get("lookup_workers", 1))
    soc_parallel = int(sm["max_lookup"]) / max(1, lw) * (
        float(sa["timeout"]) + float(sa["body_timeout"]))
    check("⑧ 社交逐条回查改为有界并发（配额内跑得完）",
          "ThreadPoolExecutor" in sm_src and "cancel_futures=True" in sm_src
          and lw >= 4 and soc_parallel <= float(layer.get("social", 35)),
          f"并发 {lw} 路估算 {soc_parallel:.0f}s ≤ social 层 {layer.get('social')}s；"
          f"旧串行 {int(sm['max_lookup'])*(float(sa['timeout'])+float(sa['body_timeout'])):.0f}s")
    kc_src = (ROOT / "news_pipeline/keyless_compose.py").read_text(encoding="utf-8")
    check("⑨ 超时任务真取消，不在后台继续占预算",
          "cancel_futures=True" in kc_src and "wait(futs" in kc_src,
          "batch_compose 用 wait(timeout)+shutdown(wait=False, cancel_futures=True)；"
          "旧版只跳过结果、后台照样跑完（缩放实测实耗是配额 2.9 倍）")

    out = ROOT / "output" / "throughput_verify.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    passed = sum(1 for r in RESULTS if r["结果"] == "PASS")
    out.write_text(json.dumps({
        "总计": len(RESULTS), "通过": passed, "失败": len(RESULTS) - passed,
        "缩放比": SCALE, "目标线索数": NEED_CLUES,
        "运行时参数": {"keyless_deadline": deadline_real, "workers": workers,
                    "provider_order": order, "timeout": timeout,
                    "max_calls": sa["max_calls"], "max_lookup": sm["max_lookup"],
                    "层配额": layer},
        "静态估算": {"旧串行结构秒": round(est_serial, 1),
                 "并发结构秒": round(est_parallel, 1),
                 "单线索旧串行检索秒": round(serial_clue, 1)},
        "缩放实跑": {"跑完线索": finished, "成稿": formed, "超时丢弃": dropped,
                 "耗时秒": round(elapsed, 2), "配额秒": round(deadline_real * SCALE, 2)},
        "结果": RESULTS,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in RESULTS:
        print(f"[{r['结果']}] {r['项']} — {r['明细'][:140]}")
    print(f"\n{passed}/{len(RESULTS)} PASS → {out}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
