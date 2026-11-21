"""流水线总控：采集 → 窗口过滤 → 候选池 → 准入 → 聚类 → 打分 → 编排 → 五工作表。

窗口期是唯一硬过滤：只有发布时间落在 win.interval 指定开闭区间的条目才进候选池；
超左界 / 超右界 / 往年旧页逐条写明时间证据，不回溯补采、不推测补写。
"""
from __future__ import annotations

import time

import copy
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

from .channels import ChannelRegistry
from .budget import Budget
from .cluster import cluster_events, merge_to_main, story_dedup
from .collectors import Collector
from .exporter import export_excel
from .model import RawItem
from .normalize import (build_abstract, dedup_candidate, normalize_item,
                        polish_abstract, set_abstract_limit,
                        set_abstract_paragraphs, validate_fields)
from .orchestrate import orchestrate
from . import runlog
from .rules import (f_verdict, hits, intl_verdict, mass_verdict, noise_verdict,
                    bankruptcy_subject_verdict, INTL_GEO_SIGNAL, INTL_THREE)
from .scoring import compute_scores
from . import quality_v3 as qv3
from . import quality_gate as qgate
from . import editorial_gate as egate
from .timewindow import Window, window_verdict, parse_datetime_text
from .social_main import collect_social_main
from .platform_access import run_platform_access


def _live_mode_early(cfg: dict) -> bool:
    return str(cfg.get("source", {}).get("mode", "demo")) in ("live", "hybrid")


def run_pipeline(cfg: dict, win: Window, base_dir: Path, out_path: Path) -> Dict[str, Any]:
    base_dir = Path(base_dir)
    # 全局运行预算：先裁剪各层 deadline / 调用上限，再逐层 gate。
    # 预算耗尽只短路采集层，筛选 / 评级 / 五工作表出表照常执行（防 35min 硬杀白跑）。
    budget = Budget(cfg)
    cfg = budget.apply(cfg)
    runlog.log("INFO", "流水线启动", 窗口=win.label, 端点开闭=win.interval_name,
               总预算秒=budget.total,
               跨度小时=round(win.span_hours, 2),
               数据源=cfg.get("source", {}).get("mode", "demo"),
               输出=str(out_path))
    reg = ChannelRegistry(
        base_dir / cfg.get("channels", {}).get("doc", "data/资讯渠道大全.md"),
        base_dir / cfg.get("channels", {}).get("extra", "data/channels_extra.yaml"),
    )
    from . import search_api
    search_api.reset()          # 每批独立配额 / 缓存 / 留痕，不跨批次污染
    collector = Collector(cfg, reg, win, base_dir)
    # 2026-11-22：每层调用都由 budget.bounded 真限时（daemon 线程 + join(timeout)），
    # 到点立即短路、主线程继续出表；旧版 gate() 只判断「能否开始」，从不限制跑多久。
    _mode = cfg.get("source", {}).get("mode", "demo")
    _inbox = cfg.get("source", {}).get("inbox_path")
    # 超时不能丢已抓条目：Collector.run 内部遇 BudgetExpired 会保留 self.items，
    # bounded 超时时后台线程可能仍在 append，这里取它的当前快照继续出表。
    _rss_limit = budget.slice("rss")
    budget.bounded("rss", collector.run, _mode, _inbox, fallback=None,
                   grace=min(_rss_limit * 0.35, 2.0))
    raw = list(collector.items)
    budget.write_report(base_dir)   # 增量落盘：即使后续被硬杀也留得下逐层诊断

    # ---- 0-pre. 低质渠道跳采（用上一批生成的 channel_blocklist.yaml）----
    # 用户 2026-11-20：「把低质量和过滤数据多的渠道直接去掉」。
    # Sohu 26/26、凤凰网 9/9、证券之星 8/8 等 RSS 倒排渠道 100% 摘要未取得，
    # 却吃满正文补采配额并灌进主表。这里在采集端直接跳采，逐条留淘汰原因；
    # 白名单 599 渠道不删除，仍逐条申报启用条件（需求②）。
    from . import channel_quality
    raw, block_traces, block_hit = channel_quality.apply_to_raw(raw, cfg)
    if block_traces:
        runlog.log("INFO", "低质渠道跳采", 渠道数=len(block_hit), 条目数=len(block_traces))

    # ---- 0a-0. 无 key 多源检索 + 事件级成稿（2026-11-21 主链路，不是兜底）----
    # 线索 = 主题词表 + 社交/热榜词条；逐条走 Google News RSS / Bing RSS / GDELT / SearXNG，
    # 全部免费公开无需签名。一个事件合并 ≥2 个独立域名原文，产出两段摘要与「A · B · C」来源列。
    # 这是样例「34/34 多渠道、34/34 两段」形态的生产层；RSS 单源倒排只是补充证据。
    kl_items: List[RawItem] = []
    kl_traces: List[Any] = []
    kl_stats: Dict[str, Any] = {}
    _kl = cfg.get("keyless", {}) or {}
    _sconf = cfg.get("search_api", {}) or {}
    if budget.gate("keyless", cfg) and _live_mode_early(cfg) and _kl.get("enabled", True):
        from . import keyless_compose as kc
        # 2026-11-22 线上 H-c 根因：泛主题词（「医保 社保 养老金 辟谣」）走 Bing/Google
        # 召回 122 个候选里中文独立域名仅 6 个，keyless 43 次 0 成稿。沙盒实测改用
        # **具体事件标题**后，同一 Bing RSS 返回 10/10 中文标题、4 个独立中文域名
        #（「11月LPR报价出炉…」→ chnfund / eastmoney / focus.cn / jingji.cctv.cn）。
        # 目标样例 34/34 多渠道正是「具体事件线索 → 多站回查」形成的，不是泛词表。
        event_clues: List[tuple] = []
        if _kl.get("use_raw_event_clues", True):
            import re as _re
            seen_sig: set = set()
            for _r in raw:
                t = str(getattr(_r, "title", "") or "").strip()
                cn = "".join(ch for ch in t if "\u4e00" <= ch <= "\u9fff")
                if not (10 <= len(t) <= 46 and len(cn) >= 8):
                    continue
                # 会务/机构动态不作回查线索（用户①明确排除，避免又灌满 APEC 会见）
                if any(k in t for k in ("会见", "APEC", "大国外交", "启播", "开播",
                                        "获奖名单", "总决赛", "定点帮扶", "双开")):
                    continue
                # 长数字串会让 Bing 退化成英文站召回（实测「医保钱包2.0…380个…」→ 0 中文）
                q = _re.sub(r"\d{5,}", " ", t)[:34].strip()
                sig = "".join(sorted(set(cn[i:i + 3] for i in range(max(0, len(cn) - 2)))))
                if len(q) < 10 or sig in seen_sig:
                    continue
                seen_sig.add(sig)
                event_clues.append((q, "本批中文端点 · 具体事件线索"))
                if len(event_clues) >= int(_kl.get("max_event_clues", 48)):
                    break
        # 具体事件线索优先；泛主题词表只作补充，不再独占前排
        clues: List[tuple] = event_clues + [
            (str(q), "主题词表") for q in (_kl.get("topic_queries") or [])]
        runlog.metric(具体事件线索=len(event_clues), 泛主题词线索=len(clues) - len(event_clues))
        if _kl.get("use_social_clues", True):
            try:
                from .free_sources import collect_free
                for route in list(_sconf.get("hot_routes", [])):
                    cs, _why = collect_free(route, float(_sconf.get("hot_timeout", 4.0)))
                    for c in cs[:int(_sconf.get("clues_per_route", 12))]:
                        w = str((c or {}).get("title") or "").strip()
                        if w:
                            clues.append((w, "社交热榜 · " + route))
            except Exception as exc:
                runlog.log("WARN", "无Key线索扩充失败",
                           原因=f"{type(exc).__name__}: {str(exc)[:80]}")
        kl_items, kl_traces, kl_stats = budget.bounded(
            "keyless", kc.batch_compose, clues, win=win, cfg=cfg,
            base_seq=len(raw),
            fallback=([], [], {"启用": False, "原因": "层超时短路，主线程继续出表"}))
        for _it in kl_items:
            raw.append(_it)
        # 0a-0 已做事件级成稿（多源来源列 + 两段摘要），后续 search_api 的 keyless
        # provider 不再逐条选单个 URL：省下检索配额，也避免单源结果稀释多渠道率。
        if kl_stats.get("启用"):
            cfg.setdefault("keyless", {})["batch_active"] = True
        runlog.log("INFO", "无Key多源成稿",
                   **{k: v for k, v in kl_stats.items() if isinstance(v, (int, float))})

    # ---- 0a. 受限平台取数（抖音 / 小红书 / 微信搜一搜等）：四条通路逐条申报 ----
    # 凭证齐备时取到的是「词条线索」，必须逐条回查带事实的独立原文才成行（01 §3.3）；
    # 回查不到只留词条行 + 附录 15，绝不拿话题名 + 热度值冒充新闻。
    _pa_conf = cfg.get("platform_access", {}) or {}
    _pa_on = (budget.gate("platform", cfg) and _live_mode_early(cfg)
              and _pa_conf.get("enabled", True))
    _pa_default = {"平台": [], "线索": [], "结论": "离线模式或运行预算短路（不阻断出表）"}
    pa = (budget.bounded("platform", run_platform_access, cfg,
                         float(_pa_conf.get("timeout", 4)), fallback=_pa_default)
          if _pa_on else _pa_default)
    pa_items: List[RawItem] = []
    for _pr in pa.get("平台", []):
        for _clue in _pr.get("线索", []):
            _hit = collector.lookup_article(_clue.title)
            _pub, _basis = (parse_datetime_text(_hit.get("date") or "", hint_year=win.end.year)
                            if _hit and _hit.get("date") else (None, "未取得"))
            _it = RawItem(
                title=(_hit or {}).get("title") or _clue.title,
                abstract=(_hit or {}).get("snippet", ""),
                abstract_status="正文补采" if _hit else "未取得",
                source=(_hit or {}).get("source") or _pr["平台"],
                url=(_hit or {}).get("url") or _clue.url,
                pub=_pub, pub_raw=(_hit or {}).get("date") or _clue.onboard_time or "未取得",
                time_basis=_basis, time_mark="窗口内实证" if _pub else "时点推定",
                date_clue=_clue.onboard_time, channel=_pr["平台"],
                channel_id="SOC-" + _pr["平台"], channel_level="★",
                evidence="A" if _hit else "C", shape="词条回查成稿" if _hit else "词条",
                has_event=bool(_hit),
                record_type="社交平台词条回查成稿" if _hit else "词条（待回查）",
                board_platforms=[_pr["平台"]], board_slots=1,
                media_slots=1 if _hit else 0,
                raw={"通路": _clue.tier, "热度": _clue.hot, "榜位": _clue.rank})
            if not _hit:
                collector.clues.append({"词条": _clue.title, "榜单": _pr["平台"],
                                        "快照页": _clue.url,
                                        "原因": "平台取数成功但未回查到带事实的独立原文"})
            _it.seq = len(raw) + 1
            raw.append(_it)
            pa_items.append(_it)
    if pa_items:
        runlog.log("INFO", "受限平台取数回查", 线索=len(pa_items),
                   成稿=sum(1 for x in pa_items if x.has_event))

    # ---- 0a-2. 开源热榜源探针留痕（词条已在 0a-0 并入 keyless 并发池）----
    # 2026-11-22 吞吐根因：旧版这里对每个词条串行调 collector.lookup_article，
    # 7 路由 × 12 词条 × 5s = 420s，而 free_sources 层配额只有 15~25s；
    # 更糟的是它与 0a-0 的 `use_social_clues` 抓的是同一批 hot_routes 词条，
    # 属于纯重复回查，还抢 search_api.max_calls。现改为只留探针与词条快照，
    # 逐条多源回查统一由 keyless（10 workers、同 future 内检索+成稿）完成。
    sconf = cfg.get("search_api", {}) or {}
    free_rows: List[List[Any]] = []
    free_items: List[RawItem] = []
    def _free_probe():
        from . import free_sources
        from .budget import deadline_at_of
        routes = list(sconf.get("hot_routes", ["weibo", "zhihu", "toutiao", "thepaper", "36kr"]))
        per_route = int(sconf.get("clues_per_route", 6))
        for route in routes:
            if deadline_at_of(cfg) and time.monotonic() > deadline_at_of(cfg):
                free_rows.append([route, 0, "层 deadline 到达，剩余路由短路"])
                break
            try:
                clues, why = free_sources.collect_free(
                    route, float(sconf.get("hot_timeout", 4.0)))
            except Exception as exc:
                clues, why = [], f"{type(exc).__name__}: {str(exc)[:80]}"
            free_rows.append([route, len(clues), why[:150]])
            for c in clues[:per_route]:
                word = str(c.get("title") or "").strip()
                if not word:
                    continue
                it = RawItem(
                    title=word, abstract="", abstract_status="未取得",
                    source=route, url=c.get("url", ""),
                    pub=None, pub_raw=c.get("time") or "未取得",
                    time_basis="未取得", time_mark="时点推定",
                    date_clue=c.get("time", ""), channel="开源热榜源 · " + route,
                    channel_id="OSS-" + route, channel_level="☆",
                    evidence="C", shape="词条", has_event=False,
                    record_type="词条（已并入无Key多源成稿层）",
                    board_platforms=[route], board_slots=1, media_slots=0,
                    raw={"开源源": route, "热度": c.get("hot", ""), "榜位": c.get("index", ""),
                         "去向": "词条已并入 0a-0 keyless 并发池，逐条多源回查；"
                                 "本行仅作快照留痕，不单独 lookup、不吃 snippet"})
                collector.clues.append({"词条": word, "榜单": "开源热榜源 · " + route,
                                        "快照页": c.get("url", ""),
                                        "原因": "已移交 keyless 并发池做多源回查；"
                                                "本行不重复 lookup，成稿与否见无Key多源成稿留痕"})
                it.seq = len(raw) + 1
                raw.append(it)
                free_items.append(it)
        runlog.log("INFO", "开源热榜源发现", 路由数=len(routes), 线索=len(free_items),
                   已移交多源成稿=len(free_items))

    if (budget.gate("free_sources", cfg) and _live_mode_early(cfg)
            and sconf.get("open_hot_sources", True)):
        budget.bounded("free_sources", _free_probe, cfg)

    # ---- 0a-3. 多资源扩展（GDELT / Google News site: / 实例池 / 公众号镜像）----
    # 用户 2026-11-20：「想办法解决没获取到数据的渠道，全网搜索、多资源查找」。
    # 全部为公开无需签名的入口，失败逐路由留痕，不编造、不绕登录。
    from . import source_expansion
    _live_mode = _live_mode_early(cfg)
    if (_live_mode and budget.gate("expansion", cfg)
            and (cfg.get("source_expansion", {}) or {}).get("enabled", True)):
        exp_items, exp_rows, exp_stats = budget.bounded(
            "expansion", source_expansion.collect, cfg, win, len(raw),
            fallback=([], [["总开关", "-", "层超时短路",
                            "多资源扩展层超过 budget.layer_seconds.expansion，主线程继续出表"]],
                      {"启用": False, "原因": "层超时短路"}))
    else:
        exp_items, exp_rows, exp_stats = [], [["总开关", "-", "离线模式跳过",
                                               "source.mode=live 时启用多资源扩展（不联网的 demo 不发请求）"]], {"启用": False}
    for _it in exp_items:
        raw.append(_it)
        if getattr(_it, "shape", "") != "词条":
            free_items.append(_it)      # 统一走正文补采，不吃 snippet / 话题名
    if exp_items:
        runlog.log("INFO", "多资源扩展发现", **{k: v for k, v in exp_stats.items()
                                                if isinstance(v, (int, float))})
    # 开源源成稿条目同样需要补独立原文正文（不吃 snippet）。
    # 词条快照行（shape=词条）没有独立原文 URL，不再发无谓请求；
    # 它们已在 0a-0 由 keyless 逐条多源回查并补正文。
    free_body: Dict[str, Any] = {}
    _free_need_body = [x for x in free_items if x.url and x.shape != "词条"]
    if _free_need_body and cfg.get("source", {}).get("live", {}).get("fetch_article", True):
        free_body = collector._fetch_search_bodies(items=_free_need_body)

    # ---- 0. 需求②：社交主干 + 快讯/拼盘全量拆条 + 逐条回查独立原文 ----
    social_snap = ((getattr(collector, "cn_meta", {}) or {}).get("社交热榜") or {})
    _soc_on = budget.gate("social", cfg) and _live_mode_early(cfg)         and (cfg.get("social_main", {}) or {}).get("enabled", True)
    _soc_default = {"items": [], "digest_rows": [], "platforms": [], "clues": [],
                    "stats": {"主干平台数": 0, "拼盘源数": 0, "拆出词条": 0, "母行数": 0},
                    "结论": "离线模式或运行预算短路（不阻断出表）"}
    soc = (budget.bounded("social", collect_social_main, cfg, win, raw, social_snap,
                          fallback=_soc_default) if _soc_on else _soc_default)
    for _it in soc["items"]:
        _it.seq = len(raw) + 1
        raw.append(_it)
    runlog.log("INFO", "社交主干与拼盘拆条", **soc.get("stats", {}))
    ch = reg.summary()
    runlog.metric(原始采集条数=len(raw), 渠道白名单=ch.get("total"), 已触达=ch.get("reached"))
    runlog.checkpoint("采集与渠道枚举", 原始条数=len(raw), 渠道触达=ch.get("reached"))
    failures = collector.failures or []
    art = getattr(collector, "article_stats", {})
    if art:
        runlog.log("INFO", "独立原文正文补采", **art)
    if failures:
        runlog.log("WARN", "渠道未取得/失败", 数量=len(failures))
        cats = Counter(f.get("category", "其他") for f in failures)
        for c, n in cats.most_common(8):
            runlog.log("WARN", "渠道失败分类", 类别=c, 数量=n)
        for f in failures[:10]:
            runlog.log("WARN", "渠道失败明细", 渠道=str(f)[:180])
        if len(failures) > 10:
            runlog.log("WARN", "渠道失败明细省略", 剩余=len(failures) - 10)

    # ---- 1. 字段规范化 + 时间回填 ----
    legacy = bool(cfg.get("rules", {}).get("legacy_groups", False))
    for it in raw:
        normalize_item(it, win, legacy_groups=legacy)
    runlog.checkpoint("字段规范化与时间回填", 条数=len(raw))

    # ---- 2. 窗口过滤（端点开闭由 win.interval 决定）+ 字段校验 → 候选池 ----
    excluded: List[List[Any]] = []
    estimated: List[List[Any]] = []
    field_reject: List[List[Any]] = []
    contract_reject: List[dict] = []
    passing: List[RawItem] = []
    strict = bool(cfg.get("window", {}).get("strict_window", False))
    allow_est = bool(cfg.get("window", {}).get("allow_estimated", True))
    qg_on = bool((cfg.get("quality_gate") or {}).get("enabled", True))
    for it in raw:
        if not it.admitted:   # resolve_pub 已判为往年旧页
            excluded.append([it.seq, it.title, it.pub.strftime("%Y-%m-%d %H:%M") if it.pub else "未取得",
                             it.time_basis, "剔除", it.reject_reason])
            continue
        ok, reason = window_verdict(it, win, allow_estimated=allow_est, strict_window=strict)
        if not ok:
            excluded.append([it.seq, it.title, it.pub.strftime("%Y-%m-%d %H:%M") if it.pub else "未取得",
                             it.time_basis, "窗口外排除", reason])
            continue
        if it.pub is None:
            estimated.append([it.seq, it.title, it.time_basis, it.time_mark])
        if it.shape == "词条" and not it.has_event:
            field_reject.append([it.seq, it.title, "仅话题名 + 快照，未回查到带事实独立原文（附录 15）"])
            continue
        ok2, why = validate_fields(it)
        if not ok2:
            field_reject.append([it.seq, it.title, why])
            continue
        # 需求⑤「未经筛选」不等于「字段不齐 / 分类不合规 / 跨事件串味也照收」。
        # 三闸：占位摘要 → 标题摘要错配 → 破产主体不可核对 → 社交话题不合规。
        if qg_on:
            cok, gate, cwhy = qgate.contract_check(it)
            if not cok:
                contract_reject.append({"展示序": it.seq, "标题": it.title,
                                        "来源": it.source, "命中闸": gate, "依据": cwhy,
                                        "阶段": "采集条目"})
                continue
        passing.append(it)

    candidate, dedup_dropped = dedup_candidate(passing)
    # ---- 2.5 事件成稿层（URL 级 → 事件级；目标样例候选池本身就是事件，不是 URL）----
    # 2026-11-22 根因：线上候选池 327 个 URL / 0 条多渠道；目标候选池 73 个事件 / 34 条全多渠道。
    # 筛选、评分、摘要都发生在成稿之后，因此必须在准入之前把「同一件事的 N 篇报道」合成一条。
    # 纯规则：3-gram LSH + 锚点倒排聚类、独立来源合并、跨源两段摘要、数字冲突并列、e/s 主键。
    from . import event_composer as _ec
    ec_traces: List[dict] = []
    if (cfg.get("event_composer", {}) or {}).get("enabled", True):
        _before = len(candidate)
        candidate, ec_traces = _ec.compose_events(candidate, cfg=cfg, win=win)
        runlog.metric(事件成稿输入=_before, 事件成稿输出=len(candidate),
                      事件成稿逐簇留痕=len(ec_traces))
    # 摘要上限由 config.yaml abstract.max_net_chars 注入（默认 100，用户拍板口径）
    _abs_limit = set_abstract_limit((cfg.get("abstract", {}) or {}).get("max_net_chars"))
    set_abstract_paragraphs((cfg.get("abstract", {}) or {}).get("paragraphs"))
    # 候选池摘要规范化（评级列仍全「—」，不做筛选 / 合并 / 编排）
    # 2026-11-22 线上规格验收 56/56 FAIL「段落首行未缩进 2 个全角空格」：
    # polish_abstract 会 strip 掉 build_abstract 加的缩进，成稿层又绕过 build_abstract，
    # 于是整批摘要都缺缩进。这里统一补回，且用编辑部硬闸做整句/坏标点/净字复核。
    _IND = "\u3000\u3000"
    def _ensure_indent(t: str) -> str:
        return "\n".join(
            (p if p.startswith(_IND) else _IND + p.lstrip("\u3000 "))
            for p in str(t or "").split("\n") if p.strip())
    for it in candidate:
        # 无 key 事件成稿已按「两段 / 整分句 / ≤100 净字 / 数字不改写」产出，
        # 不能再过一次单段抽取把它压成一段（样例 34/34 两段）。
        if (getattr(it, "channel_id", "") in ("KEYLESS", "EVENT_COMPOSER")
                and "\n" in (it.abstract or "")):
            it.abstract = polish_abstract(it.abstract)
            it.abstract_source = "多源事件成稿（两段、整分句、数字未改写、格式已清洗）"
        else:
            it.abstract, it.abstract_source = build_abstract(it)
            it.abstract = polish_abstract(it.abstract)
        # 2026-11-22 全量线上重判：所有条目统一走 strict 收口（不只成稿层）。
        # 线上 56 条主表有 4 条 3–4 段、1 条「（。」半句、候选池 43 条多段，
        # 旧版只对成稿层修、其余直接判「段落超限」删条，误杀 LPR / 大模型等真新闻。
        # 现在先修（1–2 段 / ≤100 净字 / 整句 / 去标题复读 / 数字不改），修不了才留痕。
        fixed, fwhy = egate.polish_abstract_strict(it.abstract, it.title)
        it.abstract = _ensure_indent(fixed)
        raw = getattr(it, "raw", None)
        if isinstance(raw, dict) and fixed != _ensure_indent(polish_abstract(it.abstract or "")):
            raw["摘要修复"] = fwhy
        # 编辑部硬闸：半句 / 坏标点 / 净字超限 / 占位句 一律不算有效摘要，
        # 主表硬闸据此拦下，绝不把「(北纬37.14度。（。」这类残片摆进交付表。
        ok_abs, gate_abs, why_abs = egate.abstract_verdict(it.abstract)
        if not ok_abs:
            it.abstract_status = "未取得"
            raw = getattr(it, "raw", None)
            if isinstance(raw, dict):
                raw["摘要硬闸"] = f"{gate_abs}：{why_abs}"
    # 2026-11-20：Actions 真实批次 41/56 条「摘要未取得」。对已进候选池的
    # 无效摘要做定向二次补采（更长超时 + URL 变体 + Jina + 并发），只认正文。
    from .second_pass import rescue_abstracts
    _sp_on = (budget.gate("second_pass", cfg)
              and cfg.get("source", {}).get("live", {}).get("second_pass_enabled", True)
              and _live_mode_early(cfg))
    _sp_default = {"尝试": 0, "救回": 0, "仍空白": 0, "耗时秒": 0,
                   "说明": "离线模式或运行预算短路（不阻断出表）"}
    sp_stats = (budget.bounded("second_pass", rescue_abstracts, candidate, cfg,
                               fallback=_sp_default) if _sp_on else _sp_default)
    if sp_stats.get("尝试"):
        runlog.log("INFO", "摘要二次补采", 尝试=sp_stats["尝试"], 救回=sp_stats["救回"],
                   仍空白=sp_stats.get("仍空白", 0), 耗时秒=sp_stats.get("耗时秒", 0))
    # 事件成稿会重写标题 / 摘要 / 来源列，必须在成稿之后重跑一次契约三闸
    # （截图1 的跨事件串味正是成稿层产生的，采集条目级校验覆盖不到）。
    if qg_on:
        candidate, ec_rejected = qgate.contract_check_many(candidate)
        for r in ec_rejected:
            r["阶段"] = "事件成稿后"
        contract_reject.extend(ec_rejected)
    # 2026-11-22 线上复盘：契约三闸只查「字段齐备/错配/主体/社交定义」，
    # 查不出 SEO 法律科普页、企业黄页广告、GTA6 剧透视频、假多渠道合并。
    # 编辑部硬闸对成稿后的候选池再过一遍（主表是其子集，同步生效），逐条留痕。
    eg_on = bool((cfg.get("quality_gate") or {}).get("editorial", True))
    if eg_on:
        # 候选池层：只剔形态垃圾；摘要未取得 / 格式问题留池留痕（需求⑤）。
        candidate, eg_rejected = egate.editorial_check_many(candidate, stage="candidate")
        for r in eg_rejected:
            r["阶段"] = "编辑部硬闸·候选池"
        contract_reject.extend(eg_rejected)
    runlog.metric(候选池条数=len(candidate), 窗口外排除=len(excluded), 时点推定=len(estimated),
                  字段不合格=len(field_reject), 契约拦截=len(contract_reject),
                  采集层去重=len(dedup_dropped))
    runlog.checkpoint("窗口过滤与候选池", 候选池=len(candidate), 窗口外排除=len(excluded),
                      时点推定=len(estimated), 字段不合格=len(field_reject),
                      契约拦截=len(contract_reject), 端点开闭=win.interval_name)

    # ---- 3. 准入层（在候选池副本上执行，候选池本身不改动） ----
    work = [copy.deepcopy(it) for it in candidate]
    mass_rows: List[dict] = []
    f_rows: List[List[Any]] = []
    intl_rows: List[List[Any]] = []
    noise_rows: List[List[Any]] = []
    bankruptcy_rows: List[List[Any]] = []
    intl_counts: Dict[str, int] = {k: 0 for k in INTL_THREE}
    intl_keep = intl_drop = intl_important = 0
    excluded_groups = set(cfg.get("rules", {}).get("excluded_groups", []) or [])
    gated_groups = dict(cfg.get("rules", {}).get("gated_groups", {}) or {})
    strictness = cfg.get("rules", {}).get("mass_strictness", "strict")
    intl_only = bool(cfg.get("rules", {}).get("intl_three_only", True))
    admitted: List[RawItem] = []

    require_abstract = bool(cfg.get("rules", {}).get("main_requires_abstract", True))
    abstract_blocked = 0
    eg_main_on = bool((cfg.get("quality_gate") or {}).get("editorial", True))
    for it in work:
        # 主表层编辑部硬闸：候选池里留痕的「摘要无效 / 格式不合规」在这里拦下，
        # 形态垃圾已在候选池层剔除；两层分离，既不丢留痕也不让垃圾进主表。
        if eg_main_on:
            mok, mgate, mwhy = egate.editorial_check(it, stage="main")
            if not mok:
                it.admitted, it.reject_stage = False, mgate
                it.reject_reason = mwhy
                mass_rows.append({"标题": it.title, "keep": [], "reject": [mgate],
                                  "判定": "删除", "依据": mwhy})
                continue
        # 3.0a 主表摘要硬闸（2026-11-20 用户「质量还是不行」）
        # 用户⑤：「窗口内全部数据」= 标题/摘要/来源齐备的候选池；主表是它的子集。
        # 旧版 normalize 明写「摘要未取得不剔条」，结果 40+ 条 RSS 倒排占位文本
        # 灌进主表、吃满质量惩罚、把 Final 压到 50 以下，A 档更不可能出现。
        # 这里只拦「进主表」：条目仍在候选池、仍在附录 1 逐条留痕，不丢数据。
        if require_abstract and str(getattr(it, "abstract_status", "")) not in ("有效", "正文补采"):
            abstract_blocked += 1
            it.admitted, it.reject_stage = False, "主表摘要未取得"
            it.reject_reason = ("摘要状态「未取得」：源站仅返回标题复读或正文未开放，"
                               "按需求⑤主表须标题/摘要/来源齐备；保留在候选池并留痕")
            mass_rows.append({"标题": it.title, "keep": [], "reject": ["主表摘要未取得"],
                              "判定": "删除", "依据": it.reject_reason})
            continue
        # 3.0 F5 非新闻 / 关键词误命中（最前置，避免噪声进入评分与编排）
        nok, nrule, nwhy = noise_verdict(it)
        if not nok:
            noise_rows.append([it.seq, it.title, it.source, it.group, it.tag, nrule, nwhy])
            it.admitted, it.reject_stage, it.reject_reason = False, nrule, nwhy
            continue
        # 3.0b 需求④：破产倒闭只留上市 / 知名 / 规模可核对主体
        if (cfg.get("rank", {}) or {}).get("bankruptcy_subject_check", True):
            bok, btype, bwhy = bankruptcy_subject_verdict(it)
            if btype != "—":
                bankruptcy_rows.append([it.seq, it.title, btype, bwhy])
            if not bok:
                it.admitted, it.reject_stage, it.reject_reason = False, "破产倒闭主体核验", bwhy
                continue
        geo = "、".join(hits(it.title, INTL_GEO_SIGNAL)[:3]) or "—"
        # 3.1 大类级准入
        if it.group in excluded_groups or (it.group in gated_groups and "涉台" not in it.text()
                                           and "中国使馆" not in it.text()):
            it.admitted, it.reject_stage = False, "大类级准入"
            it.reject_reason = f"大类 {it.group} 被用户口径清空 / 未命中特别重要事实字段"
            mass_rows.append({"标题": it.title, "keep": [], "reject": [it.reject_stage],
                              "判定": "删除", "依据": it.reject_reason})
            continue
        # 3.2 大众贴近度（先 reject 再 keep）
        ok, keep, reject, why = mass_verdict(it, strictness)
        mass_rows.append({"标题": it.title, "keep": keep[:6], "reject": reject[:6],
                          "判定": "通过" if ok else "删除", "依据": why})
        if not ok:
            it.admitted, it.reject_stage, it.reject_reason = False, "大众贴近度", why
            continue
        # 3.3 国际动态仅三类重大突发
        intl_ok, tag, important, intl_why = intl_verdict(it)
        it.intl_important = important
        if tag in INTL_THREE:
            it.intl_tag = tag
        if intl_ok and important:
            intl_keep += 1
            intl_important += 1
            if tag in INTL_THREE:
                intl_counts[tag] += 1
        elif not intl_ok:
            intl_drop += 1
        intl_rows.append([it.seq, it.title, it.source, geo,
                          "是" if important else "否", tag or "—",
                          "保留" if intl_ok else "剔除", intl_why,
                          "" if intl_ok else intl_why])
        if not intl_ok and intl_only:
            it.admitted, it.reject_stage, it.reject_reason = False, "国际门禁", intl_why
            continue
        # 3.4 F1–F4
        fok, rule, fwhy = f_verdict(it)
        if not fok:
            f_rows.append([it.title, rule, fwhy])
            it.admitted, it.reject_stage, it.reject_reason = False, f"低价值过滤 {rule}", fwhy
            continue
        admitted.append(it)

    reject_reasons: Counter = Counter(
        (it.reject_stage or "—") for it in work if not it.admitted)
    runlog.metric(准入通过=len(admitted), 准入剔除=len(work) - len(admitted),
                  主表摘要未取得剔除=abstract_blocked,
                  贴近度档位=strictness, 国际保留=intl_keep, 国际剔除=intl_drop)
    runlog.checkpoint("准入层", 通过=len(admitted), 剔除=len(work) - len(admitted),
                      主表摘要未取得=abstract_blocked, F5噪声剔除=len(noise_rows))
    if reject_reasons:
        runlog.log("INFO", "准入剔除原因分布", **dict(reject_reasons.most_common(8)))
    if noise_rows:
        runlog.log("WARN", "F5 非新闻 / 关键词误命中剔除", 数量=len(noise_rows))

    # ---- 4. 聚类 / 多渠道 / 议题簇 ----
    pairs, clusters = cluster_events(admitted)
    # 2026-11-21 质量 v3（行业做法：AIHOT heat-v1-48h-halflife24h / Alethman
    # MinHash 事件簇 / Google News freshness+authority）：
    # 旧版 compute_scores 在 merge_to_main 之前跑，而 _independent_sources(it)
    # 读单条 source 恒为 1 → h_proxy_min_sources=3 永不成立 → A(≥70) 恒空。
    # 现在打分前先把转载聚成事件、按 48h 窗口/24h 半衰期算事件级 H_c 并写回
    # 每个成员；grade 阈值 / V_MAX / W_H 一律不动，A 由真实多源覆盖自然产生。
    heat_cfg = (cfg.get("heat") or {})
    ev_cluster = qv3.event_cluster(
        admitted, jaccard=float(heat_cfg.get("jaccard", 0.70)),
        hamming_max=int(heat_cfg.get("hamming_max", 3)),
        min_group=int(heat_cfg.get("min_participants", 2)))
    ev_heat = qv3.event_heat(
        ev_cluster, now=win.end,
        window_hours=float(heat_cfg.get("window_hours", 48)),
        half_life_hours=float(heat_cfg.get("half_life_hours", 24)),
        min_participants=int(heat_cfg.get("min_participants", 2)),
        cap=float(heat_cfg.get("cap", 5.0)))
    heat_applied = qv3.apply_heat(admitted, ev_heat, ev_cluster)
    runlog.metric(事件簇数=len(ev_cluster["簇"]),
                  多源事件数=ev_heat["汇总"]["多源事件数"],
                  Hc最大=ev_heat["汇总"]["H_c最大"], 热度写回条数=heat_applied)
    score_info = compute_scores(admitted, win, cfg)
    runlog.metric(等级分布=score_info.get("grade_dist", {}),
                  H通道="开启" if score_info.get("h_enabled") else "关闭(仅价值分V)")
    runlog.checkpoint("事件聚类与六维价值分 V / H 通道合成", 相似对=len(pairs), 聚类数=len(clusters))
    main, merged_log, cluster_stats = merge_to_main(admitted)
    story_rows: List[dict] = []
    story_stats: Dict[str, Any] = {"议题簇数": 0, "议题簇合并条数": 0}
    if cfg.get("rules", {}).get("story_dedup", True):
        main, story_rows, story_stats = story_dedup(
            main, True, int(cfg.get("rules", {}).get("story_max_keep", 1)))

    # ---- 5. 编排（只改展示序） ----
    # SKILL §1.3「分数序 = 纯 Final_adj 基准序」：compute_scores 在 admitted 全量台账上
    # 赋过一次序，聚类 / 议题簇去重后主表只剩子集，必须在编排前重排为连续唯一 1..N，
    # 否则会出现「主表 135 条、分数序最大 154」这种无法逐条复算的基准序。
    for _i, _it in enumerate(sorted(main, key=lambda x: (-x.Final_adj, -x.Final, x.seq)), 1):
        _it.score_order = _i
    ordered, orch_log = orchestrate(main, cfg)
    cluster_stats["前 10 吃到多渠道加分的条数"] = orch_log.get("前10吃到多渠道加分的条数", 0)
    runlog.metric(主表条数=len(ordered), 前10覆盖大类=orch_log.get("前10覆盖大类数"))
    runlog.checkpoint("编排层", 主表=len(ordered), 议题簇合并=story_stats.get("议题簇合并条数"))

    # 工程留痕 H 是诊断信息，绝不能成为出表的致命依赖。
    # 2026-11-20 Actions 实测：trace_rows() 里一条脏留痕抛 TypeError，导致
    # 12m11s 的采集全部跑完后 Excel 未落盘、页面未更新。此处兜底 + runlog 留痕。
    try:
        _sa = {"stats": search_api.stats(),
               "trace": search_api.trace_rows(),
               "providers": search_api.provider_summary(),
               "free_routes": free_rows,
               "free_bodies": free_body,
               "free_items": sum(1 for x in free_items if x.has_event)}
    except Exception as exc:         # noqa: BLE001
        _sa = {"stats": {}, "trace": [], "providers": [],
               "free_routes": free_rows, "free_bodies": free_body,
               "free_items": sum(1 for x in free_items if x.has_event),
               "留痕异常": f"{type(exc).__name__}: {exc}"}
        runlog.metric(留痕异常=str(exc))

    logs = {
        "mass": mass_rows, "f_rows": f_rows, "intl_rows": intl_rows,
        "noise_rows": noise_rows,
        "bankruptcy_rows": bankruptcy_rows,
        "social_main": soc,
        "platform_access": pa,
        "digest_rows": soc["digest_rows"],
        "intl_counts": intl_counts,
        "intl": {"国际资讯": intl_keep + intl_drop, "保留": intl_keep, "剔除": intl_drop,
                 "重要": intl_important, "三类逐类": intl_counts},
        "excluded": excluded, "estimated": estimated, "field_reject": field_reject,
        "contract_reject": contract_reject,
        "dedup_dropped": dedup_dropped, "clues": collector.clues,
        "pairs": pairs, "merged": merged_log, "story_rows": story_rows,
        "score": score_info, "orch": orch_log, "cluster": cluster_stats,
        "story": story_stats, "failures": collector.failures,
        "article": getattr(collector, "article_stats", {}),
        "second_pass": sp_stats,
        "search_bodies": getattr(collector, "search_body_stats", {}),
        "search_api": _sa,
        "source_expansion": {"stats": exp_stats, "rows": exp_rows},
        "heat": {"cluster": ev_cluster, "heat": ev_heat, "applied": heat_applied},
        "skipped": getattr(collector, "skipped", []),
        "channel_block_traces": block_traces,
        "channel_block_hit": block_hit,
        "target": getattr(collector, "target_meta", {}),
        "live_cn": getattr(collector, "cn_meta", {}),
        "failure_categories": dict(Counter(
            f.get("category", "其他") for f in collector.failures)),
        "budget": budget.report(),
        "event_composer": ec_traces,
    }
    # 预算报告落盘，供 CI 摘要与 verify_budget.py 复算；不因写文件失败阻断出表
    try:
        import json as _json
        _bp = base_dir / "output" / "budget_report.json"
        _bp.parent.mkdir(parents=True, exist_ok=True)
        _bp.write_text(_json.dumps(budget.report(), ensure_ascii=False, indent=1),
                       encoding="utf-8")
    except Exception as _e:
        runlog.metric(预算报告写盘=str(_e))
    R = {"window": win, "cfg": cfg, "registry": reg, "candidate": candidate,
         "main": main, "ordered": ordered, "logs": logs, "raw_count": len(raw),
         "admitted": admitted}
    from .quality import compute_quality
    R["quality"] = compute_quality(R, cfg)
    q = R["quality"]
    # 本批渠道质量账本 → 生成下一批的跳采名单，并写进 Excel「工程留痕 I」
    cq = channel_quality.compute(raw, candidate, ordered, cfg)
    logs["channel_quality"] = cq
    runlog.log("INFO", "渠道质量账本", 渠道数=cq["汇总"]["渠道数"],
               淘汰=cq["汇总"]["淘汰数"], 淘汰原始条目=cq["汇总"]["淘汰渠道原始条目"])
    runlog.checkpoint("采集质量门禁", 结论=q["结论"],
                      未达标="、".join(q["未达标项"]) or "无")
    if q["未达标项"]:
        runlog.log("WARN", "采集质量未达标（如实标注，不伪造成品）",
                   未达标="、".join(q["未达标项"]), 处置=q["结论"])
    export_excel(out_path, R)
    R["path"] = out_path
    # 出表之后再覆盖写一次：报告里的「实际耗时秒」必须包含出表段，
    # 否则会像线上那样「采集 10.2s 达标」而整批实际 664s / 15min 被硬杀。
    # logs 里的 budget 快照也同步成最终值，避免 Excel 与 JSON 口径打架。
    logs["budget"] = budget.report()
    budget.write_report(base_dir)
    budget.close()
    runlog.checkpoint("五工作表出表", 文件=out_path.name,
                      字节=out_path.stat().st_size if out_path.exists() else 0)
    runlog.log("INFO", "流水线完成", 主表=len(ordered), 候选池=len(candidate),
               原始=len(raw), 产物=str(out_path))
    return R
