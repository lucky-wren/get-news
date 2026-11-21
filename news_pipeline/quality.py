"""采集质量门禁：把「结果是否可信」量化，拒绝把标题复读包装成成品。

12 项指标（含 2 项白名单分层盘点）写进 Excel「工程留痕 A」、运行摘要与 CI 摘要。低于/超过阈值默认只告警
（warn_only=true），改成 false 即成为上线硬门禁，让不达标批次直接失败。
"""
from __future__ import annotations

from typing import Any, Dict
import os


def compute_quality(R: Dict[str, Any], cfg: dict) -> Dict[str, Any]:
    q = cfg.get("quality", {}) or {}
    main, cand = R["ordered"], R["candidate"]
    logs, reg = R.get("logs", {}), R["registry"]
    # registry.summary() 返回中文键；旧实现用了英文键，导致质量门禁表显示 0/0
    summ = reg.summary()
    total_ch = summ.get("白名单渠道总数", 0) or 0
    reached = summ.get("已触达渠道数", 0) or 0
    ch_ratio = (reached / total_ch) if total_ch else 0.0

    a_ratio = (sum(1 for x in main if x.evidence == "A") / len(main)) if main else 0.0
    pub_ratio = (sum(1 for x in cand if x.pub is not None) / len(cand)) if cand else 0.0
    clues = len(logs.get("clues", []) or [])
    word_items = sum(1 for x in R.get("admitted", []) if x.shape == "词条")
    lookup_ratio = (word_items / (word_items + clues)) if (word_items + clues) else 1.0

    # 摘要有效率：只有「有效 / 正文补采」算数；「未取得」是如实标注，不算质量
    valid_abs = sum(1 for x in main
                    if getattr(x, "abstract_status", "") in ("有效", "正文补采"))
    abs_ratio = (valid_abs / len(main)) if main else 0.0
    bad_abs = [x.title for x in main
               if getattr(x, "abstract_status", "") not in ("有效", "正文补采")]

    # 分类一致率（2026-11-20 定稿口径）：分类标签前缀必须等于上位大类。
    # 旧实现只在「标签首段恰好是 8 个大类名之一」时才校验，导致「财经市场 +
    # 房地产 / 地方债务」这类不合规条目一律 return True，门禁恒报 1.0 —— 验收器自欺。
    # 现在按口径表「分类一致性硬约束」逐条判定，含 '/' 的细标签首段必须等于 group。
    import re as _re

    def _net(s: str) -> str:
        # 与 exporter 写第 9 列时的净字口径完全一致：去所有空白（含 \t / \u3000）
        return _re.sub(r"[\u3000\s]", "", s or "")

    def _consistent(x) -> bool:
        # 与 scripts/audit_batch.py P0-3 判据一致：只有含「/」的细标签才要求
        # 首段 == 上位大类；「综合」「医保与社保」这类不带斜杠的细标签按 01 §3 允许。
        # 旧版对不带斜杠的细标签也判不合规，会把绝大多数条目误报成错配。
        tag = str(x.tag or "").strip()
        if not tag or "/" not in tag:
            return True
        return tag.split("/")[0].strip() == str(x.group or "").strip()
    consistent = sum(1 for x in main if _consistent(x))
    group_ratio = (consistent / len(main)) if main else 1.0
    inconsistent_samples = [f"{x.group} + {x.tag}" for x in main if not _consistent(x)][:8]

    # 摘要长度合规率（用户 2026-11-20 补充）：净字数 ≤100；主表 + 候选池都查，
    # 旧实现只查 main 且 _net 与 Excel 写列口径不一致（101 字仍报 1.0）。
    over_long = [x.title for x in main if len(_net(x.abstract)) > 100]
    len_ratio = (len(main) - len(over_long)) / len(main) if main else 1.0
    pool_all = cand
    pool_over = [x.title for x in pool_all if len(_net(getattr(x, "abstract", ""))) > 100]
    pool_len_ratio = ((len(pool_all) - len(pool_over)) / len(pool_all)) if pool_all else 1.0
    # 取主表 / 候选池较差值，避免「候选池 28 条 253 字却报 1.0」的自欺
    len_ratio = min(len_ratio, pool_len_ratio)

    # 段落重复率：目标态样例里 49/94 条把同一段正文重复 2–4 次，属于可修的摘要瑕疵。
    # 两段形态下单段条目天然为 0；两段之间还要查近似重复（换个说法抄一遍也算）。
    def _bigrams(s: str):
        s = _re.sub(r"[\s\u3000]", "", s or "")
        return {s[i:i + 2] for i in range(len(s) - 1)} or {s}

    def _para_dup(x) -> bool:
        ps = [_re.sub(r"[\s\u3000]+", "", p) for p in (x.abstract or "").split("\n")]
        ps = [p for p in ps if p]
        if len(ps) > len(set(ps)):
            return True
        for i in range(len(ps)):
            for j in range(i + 1, len(ps)):
                a, b = _bigrams(ps[i]), _bigrams(ps[j])
                if a and b and len(a & b) / len(a | b) > 0.60:
                    return True
        return False
    para_dups = [x.title for x in main if _para_dup(x)]
    para_dup_ratio = (len(para_dups) / len(main)) if main else 0.0

    # 端点配置率：白名单里「待配置端点」的比例（这是「渠道未取得」的首要根因）
    try:
        rows = [str(r) for r in reg.table()]
        unconfigured = sum(1 for r in rows if "待配置端点" in r)
        endpoint_ratio = (1 - unconfigured / len(rows)) if rows else 0.0
    except Exception:
        unconfigured, endpoint_ratio = 0, 0.0

    # 非新闻噪声率：F5 剔除 / (F5 剔除 + 最终主表)
    noise = len(logs.get("noise_rows", []) or [])
    noise_ratio = (noise / (noise + len(main))) if (noise + len(main)) else 0.0

    # 议题重复率：议题簇合并掉的条数占合并前主表的比例
    story = logs.get("story", {}) or {}
    merged = story.get("议题簇合并条数", 0) or 0
    before = len(main) + merged
    dup_ratio = (merged / before) if before else 0.0

    # 实时源触达率：本批真正发起联网请求的结构化源里，成功返回条目的比例
    lc = logs.get("live_cn", {}) or {}
    src_rows = lc.get("逐源触达", []) or []
    src_hit = sum(1 for r in src_rows if r.get("status") == "已触达")
    src_ratio = (src_hit / len(src_rows)) if src_rows else 0.0

    # 「渠道触达率 / 端点配置率」原分母是白名单渠道名数，其中大量是本环境网络拦截域名
    # 与平台内栏目/账号（根本不是独立站点），当门禁分母数学上不可能达标，属口径错误。
    # 改为：分层盘点（不参与门禁）+「实时源触达率」门禁判定本批抓取成败。
    import json as _json
    from pathlib import Path as _Path
    cov_path = _Path(__file__).resolve().parents[1] / "data" / "channel_coverage.json"
    cov = {}
    if cov_path.exists():
        try:
            cov = _json.loads(cov_path.read_text(encoding="utf-8"))
        except Exception:
            cov = {}
    layers = cov.get("layers", {}) or {}
    covered_ch = int(cov.get("covered_channels", 0) or 0)
    whitelist_n = int(cov.get("whitelist_total", total_ch) or total_ch)
    ep_meta = lc.get("端点清单", {}) or {}
    ep_pass = int(ep_meta.get("登记端点数", 0) or 0)
    ep_fail = int(ep_meta.get("申报未通过数", 0) or 0)
    ch_ratio = (covered_ch / whitelist_n) if whitelist_n else 0.0
    endpoint_ratio = (ep_pass / (ep_pass + ep_fail)) if (ep_pass + ep_fail) else 0.0

    # (实测, 阈值, 方向, 口径)
    raw = {
        "实时源触达率": (round(src_ratio, 4), float(q.get("min_source_ratio", 0.60)), "min",
                    "本批真实联网源中已返回条目 / 启用源数（区别于白名单完备度）"),
        "A级独立原文占比": (round(a_ratio, 4), float(q.get("min_a_ratio", 0.60)), "min",
                      "主表 evidence=A / 主表"),
        "发布时间取得率": (round(pub_ratio, 4), float(q.get("min_pub_ratio", 0.80)), "min",
                     "候选池 pub 非空 / 候选池"),
        "渠道触达率": (round(ch_ratio, 4), float(q.get("min_channel_ratio", 0.30)), "min",
                   "盘点项（不参与门禁）：白名单中由实证端点覆盖的渠道 / 白名单总数；"
                   "分母含本环境网络拦截域名与平台内栏目，逐渠道见附录 22"),
        "词条回查成功率": (round(lookup_ratio, 4), float(q.get("min_lookup_ratio", 0.80)), "min",
                     "回查成稿词条 / (成稿词条 + 未回查线索)"),
        "摘要有效率": (round(abs_ratio, 4), float(q.get("min_abstract_ratio", 0.80)), "min",
                   "abstract_status ∈ {有效,正文补采} / 主表；标题复读不计"),
        "摘要长度合规率": (round(len_ratio, 4), 1.0, "min",
                     "净字数（去全部空白）≤100；取主表与候选池较差值，逐条超长见附录 5；"
                     "旧实现只查主表且净字口径与 Excel 写列不一致，101 字仍报 1.0"),
        "分类一致率": (round(group_ratio, 4), float(q.get("min_group_consistency", 0.98)), "min",
                   "按口径表「分类标签前缀必须等于上位大类」逐条判定 / 主表；"
                   "旧实现仅当标签首段恰为 8 个大类名之一才校验，细标签一律放行导致恒报 1.0"),
        "端点配置率": (round(endpoint_ratio, 4), float(q.get("min_endpoint_ratio", 0.50)), "min",
                   "盘点项（不参与门禁）：discover_endpoints.py 实测通过的端点 / 候选端点总数；"
                   "未通过端点逐条申报原因，见附录 21 与 endpoints_live.yaml 的 rejected"),
        "非新闻噪声率": (round(noise_ratio, 4), float(q.get("max_noise_ratio", 0.10)), "max",
                    "F5 剔除 / (F5 剔除 + 主表)"),
        "议题重复率": (round(dup_ratio, 4), float(q.get("max_story_dup_ratio", 0.30)), "max",
                   "议题簇合并条数 / 合并前主表"),
        "段落重复率": (round(para_dup_ratio, 4), float(q.get("max_para_dup_ratio", 0.02)), "max",
                   "内容摘要里规范化后重复段落的条目 / 主表"),
    }
    mode = cfg.get("source", {}).get("mode", "demo")
    # 从成品 xlsx 台账导入时，evidence / pub / 逐渠道状态在导出时就已丢失，
    # 这四个「采集侧」指标不可测，标「不适用」而不是误判为质量不合格。
    NA_ON_INBOX = {"A级独立原文占比", "发布时间取得率", "渠道触达率", "端点配置率"}
    NA_AS_GATE = {"渠道触达率", "端点配置率"}   # 分层盘点项，不作为本批质量门禁
    metrics, failed = {}, []
    for name, (v, lim, direction, desc) in raw.items():
        applicable = not (mode in ("inbox", "target") and name in NA_ON_INBOX)
        if name in NA_AS_GATE and mode in ("live", "hybrid") and cov:
            applicable = False
        ok = (v >= lim if direction == "min" else v <= lim) if applicable else True
        metrics[name] = {"实测": v, "下限" if direction == "min" else "上限": lim,
                         "达标": ok, "适用": applicable, "方向": direction, "口径": desc}
        if applicable and not ok:
            failed.append(name)
    advice = []
    if mode == "inbox":
        advice.insert(0, "本批由成品 Excel 台账导入：evidence / pub / 逐渠道触达状态在导出时已丢失，"
                      "四项采集侧指标标「不适用」；内容侧（摘要 / 分类 / 噪声 / 议题 / 段落重复）仍按实测判定。"
                      "要恢复采集侧可观测性，请切回 live 并补齐端点。")
    if mode == "target":
        advice.insert(0, "本批为代码化全渠道补跑（内置标准语料按窗口重放，非实时）：采集侧四项指标不适用，"
                      "内容侧六项按真实单元格判定。实时端点探测通过、或配好检索 key 后切 source.mode: live，"
                      "同一条流水线接 8 大类 RSS / 官方 API / 榜单词条回查 / 正文补采，即可产出实时目标态。")
    if cov:
        advice.insert(0, f"主白名单按需求②为《资讯渠道大全》31 类 {whitelist_n} 渠道扩展版：逐渠道申报触达状态（附录 3），10 个社交平台登记为主干发现入口（附录 15 / 工程留痕 F-a）；"
                      f"★ 必采 {cov.get('star_covered', 0)}/{cov.get('star_total', 0)} 由实证端点覆盖；"
                      f"按入口列提取 {(cov.get('domain_probe', {}) or {}).get('total', 0)} 个独立域名，逐个试首页 / RSS / 新闻列表 / 要闻页四种形态，"
                      f"{layers.get('网络出口拦截（换网络可启用）', 0)} 个域名级 403 拦截（GitHub Actions 等 unrestricted 网络下由 discover_endpoints.py 自动实测并启用），"
                      f"{layers.get('待补端点（候选路径无可枚举时间条目）', 0)} 个需补该站正确端点 URL，"
                      f"{layers.get('无公开入口（栏目 / 账号 / 线下）', 0)} 个是平台内栏目/账号。"
                      "原「渠道触达率/端点配置率」分母含这些不可抓取渠道，已改为盘点项、不参与门禁；"
                      "本批抓取成败由「实时源触达率」判定。逐渠道原因与启用条件见附录 3 / 工程留痕 E。")
    if "摘要有效率" in failed:
        advice.append("RSS description 多为「标题+来源+标题」模板：开启 live.fetch_article 从独立原文 URL 补正文；"
                      "取不到就明确标「摘要未取得」，绝不复读标题。")
    if "分类一致率" in failed:
        advice.append("分类标签前缀必须与上位大类一致；「经济地震」「生活大爆炸」「地震属性」等伪友好词"
                      "已加否定表，国际标签需境外地理信号，避免误挂。")
    if "非新闻噪声率" in failed:
        advice.append("F5 已剔除学术论文 / 影视娱乐 / 导购 FAQ / 政务科普；逐条见附录 19，"
                      "可在 rules.NOISE_RULES 继续补词，不要靠调分掩盖。")
    if "议题重复率" in failed:
        advice.append("同一议题（如 A股低价股退市潮）用 TOPIC_THEMES 收敛，config.rules.story_max_keep 控制每簇保留条数。")
    if "摘要长度合规率" in failed:
        advice.append("摘要净字数超 100：normalize._condense 按原文顺序挑 1–2 句整句、整分句边界收束，"
                      "必要时用 NEWS_ABSTRACT_MAX 调整上限；绝不 [:100] 切半句、不改写数字。逐条见附录 5。")
    if "段落重复率" in failed:
        advice.append("摘要曾出现同一段正文复制 2–4 次：normalize 保留段级 + 句级去重；"
                      "现行 ≤100 字单段口径下该值天然为 0，仍保留检测以防回退。")
    if "A级独立原文占比" in failed or "发布时间取得率" in failed:
        advice.append("优先接官方 API / 站点时间流并校验 pubDate；热榜词条一律回查到带事实的独立原文。")
    if mode == "demo":
        advice.insert(0, "当前是离线演示台账：渠道触达率必然为 0，属如实标注；正式启用把 source.mode 改为 live。")
    if mode in ("live", "hybrid"):
        advice.insert(0, f"本批真实联网：启用 {len(src_rows)} 源、触达 {src_hit} 源、原始 "
                      f"{lc.get('原始条目数', 0)} 条、正文补采 "
                      f"{(lc.get('正文补采', {}) or {}).get('正文补采成功', 0)} 篇；"
                      "内容侧指标（摘要 / 分类 / 时效 / 噪声 / 去重）按真实单元格判定。"
                      + ("本批运行于 GitHub Actions：仍不可达的域名属站点反爬 / 需凭证 / 端点失效，"
                         "不应再写「换网络可启用」，须逐条补端点或配 Key。"
                         if os.environ.get("GITHUB_ACTIONS") else
                         "国际源组在受限网络下如实申报不可达，GitHub Actions 等正常网络会自动启用。"))
    advice.append("把 quality.warn_only 改为 false，让不达标批次在 CI 直接失败，避免悄悄产出低质量成品。")

    return {
        "指标": metrics,
        "未达标项": failed,
        "结论": "达标" if not failed else ("告警（warn_only）" if q.get("warn_only", True) else "不达标"),
        "warn_only": bool(q.get("warn_only", True)),
        "渠道白名单": total_ch, "已触达": reached, "待配置端点": unconfigured,
        "白名单分层": layers, "白名单覆盖渠道": covered_ch,
        "白名单渠道总数": whitelist_n,
        "分类不一致样本": inconsistent_samples,
        "摘要超长样本": (over_long + pool_over)[:16],
        "清单版本": "31 类 599 渠道扩展版（需求②主白名单；v3 113 渠道表另存作扩展参考）",
        "星必采总数": int(cov.get("star_total", 0) or 0),
        "星必采已覆盖": int(cov.get("star_covered", 0) or 0),
        "域名探测": cov.get("domain_probe", {}), "端点清单": ep_meta,
        "主表A级条数": sum(1 for x in main if x.evidence == "A"), "主表条数": len(main),
        "实时源启用数": len(src_rows), "实时源已触达": src_hit,
        "实时源原始条目": lc.get("原始条目数", 0), "实时源窗口内": lc.get("窗口内", 0),
        "正文补采": lc.get("正文补采", {}),
        "有效摘要条数": valid_abs, "无效摘要条数": len(bad_abs),
        "段落重复条数": len(para_dups), "段落重复样本": para_dups[:8],
        "F5噪声剔除": noise, "议题簇合并": merged,
        "词条成稿": word_items, "未回查线索": clues,
        "采集模式": mode,
        "贴近度档位": str(cfg.get("rules", {}).get("mass_strictness", "strict")),
        "编排开关": bool(cfg.get("orchestrate", {}).get("enabled", True)),
        "H通道": "开启" if (R["logs"].get("score", {}) or {}).get("h_enabled") else "关闭（仅价值分 V）",
        "建议": advice,
        "口径说明": (
            "12 项指标全部来自本批真实数据，不是估计值。其中「渠道触达率 / 端点配置率」"
            "为白名单分层盘点项（适用=False，不参与门禁）：分母含本环境网络拦截域名与平台内栏目，"
            "用其当门禁分母属口径错误；本批抓取成败由「实时源触达率」判定。"
            "摘要有效率只认独立原文或有效原文摘要，标题复读一律计为未取得。"
        ),
    }
