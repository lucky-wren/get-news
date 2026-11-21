"""候选池 / 主表「摘要未取得」定向二次补采（纯代码，不编造）。

2026-11-20 GitHub Actions 真实批次暴露的问题：无限制网络 + SERPER_API_KEY 下
渠道从 6 个涨到 31 个（候选池 58 个来源、112 源启用 / 94 触达），但主表
**41/56 条「摘要未取得」**（摘要有效率 0.27，门禁告警）。根因不是渠道少，是：

  * 搜索回查 / RSS 时间倒排拿到的第三方站点（Sohu、网易、汽车之家、证券之星、
    东方财富、凤凰网、英为财情…）正文页 4 秒超时不够，首轮补采失败；
  * `_fetch_articles` 的 400 篇配额按窗口内优先分配，很多「有 URL 但正文抽不到」
    的条目只试过 1 次就落空；Jina 回退 82 次全失败。

本模块在「候选池摘要规范化之后、准入深拷贝之前」对**已进候选池且状态为未取得**
的条目做第二轮定向补采：更长超时 + URL 变体 + Jina 回退 + 并发 + 总时限护栏。
只认从独立原文页抽到的正文；抽不到保持「未取得」，绝不用标题 / snippet 冒充。

可用 config 关闭：`source.live.second_pass: false`。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Tuple

from .article import fetch_article_text
from .normalize import build_abstract


def _variants(url: str) -> List[str]:
    """同一篇独立原文的几个合法变体，逐个试；不猜域名、不改路径语义。"""
    u = (url or "").strip()
    if not u.startswith(("http://", "https://")):
        return []
    out = [u]
    if u.startswith("http://"):
        out.append("https://" + u[7:])
    # AMP / 移动版：常见站点静态页，失败即返回空，不影响原 URL
    sep = "&" if "?" in u else "?"
    out.append(u + sep + "amp=1")
    if "://www." in u:
        out.append(u.replace("://www.", "://m.", 1))
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def _try_one(url: str, timeout: float, jina_timeout: float,
             jina_cfg: dict | None = None) -> Tuple[str, str]:
    """返回 (正文, 依据)；全部失败返回 ('', 失败原因)。

    Jina 走增强版：X-Target-Selector / X-Remove-Selector / X-No-Cache /
    X-User-Agent + 令牌桶（无 key 20 RPM）；增强失败再退到原轻量实现。
    """
    from . import free_sources, source_expansion
    for v in _variants(url):
        try:
            text = fetch_article_text(v, timeout=timeout, max_chars=6000)
            if text and len(text.strip()) >= 40:
                return text, "二次补采：" + v[:110]
        except Exception:
            pass
    try:
        text, why = source_expansion.jina_read(
            url, timeout=jina_timeout, cfg=jina_cfg or {}, throttle=False,
            api_key=str((jina_cfg or {}).get("_api_key") or ""))
        if len(text.strip()) >= 40:
            return text, "二次补采 Jina Reader（增强头）：" + url[:110]
    except Exception:
        pass
    try:
        text = free_sources.jina_reader_text(url, timeout=jina_timeout) or ""
        if len(text.strip()) >= 40:
            return text, "二次补采 Jina Reader（轻量）：" + url[:110]
    except Exception:
        pass
    return "", "URL 变体与 Jina 回退均未取得正文（源站未开放 / 反爬 / 超时）"


def rescue_abstracts(items: List[Any], cfg: dict, *,
                     max_items: int = 0) -> Dict[str, Any]:
    """就地修复 items 里的无效摘要；返回统计与逐条留痕（写进工程留痕 B）。"""
    live = (cfg.get("source", {}) or {}).get("live", {}) or {}
    sp = live.get("second_pass", {}) if isinstance(live.get("second_pass"), dict) else {}
    enabled = bool(live.get("second_pass_enabled", True))
    if not enabled:
        return {"启用": False, "尝试": 0, "救回": 0, "逐条": [],
                "说明": "config 关闭 source.live.second_pass_enabled"}
    limit = int(max_items or sp.get("max_items", 90))
    timeout = float(sp.get("timeout", 9.0))
    jina_timeout = float(sp.get("jina_timeout", 6.0))
    workers = int(sp.get("workers", 8))
    deadline = float(sp.get("deadline_seconds", 130.0))

    # 2026-11-20 Actions 批次实测：旧版按候选池原始顺序 [:90]，名额几乎全被
    # 「习近平会见… / 新闻联播要闻集锦 / 全会公报」这类时政拼盘吃掉，救回仅 2 条。
    # 改按「能否进主表」的代理信号降序排：有发布时间、非纯时政套话、标题未污染、
    # 来源可补、记录类型非 RSS 模板。只改重试顺序，不改任何准入判定或数据。
    _POLIT = ("习近平", "会见", "重要讲话", "全会公报", "新闻联播", "要闻集锦",
              "图文数据库", "时政", "微观察", "一见·", "大国外交", "总书记")
    def _priority(it) -> int:
        t = str(getattr(it, "title", "") or "")
        sc = 0
        if getattr(it, "pub", None): sc += 3
        if len(t) <= 60: sc += 2                      # 标题未被正文污染
        if not any(k in t for k in _POLIT): sc += 3   # 时政拼盘优先级最低
        if str(getattr(it, "record_type", "")) != "RSS 时间倒排": sc += 1
        if str(getattr(it, "source", "")) and "USGS" not in str(it.source): sc += 1
        return sc
    # 已被 channel_quality 淘汰的渠道不再消耗二次补采配额：Sohu / 凤凰网 /
    # 证券之星 / 汽车之家在上传批次里 100% 摘要未取得，90 个名额几乎全被它们吃掉。
    from . import channel_quality
    _blocked = channel_quality.load_blocked(cfg)
    pool = [it for it in items
            if getattr(it, "url", "") and
            str(getattr(it, "abstract_status", "") or "") in ("", "未取得")
            and getattr(it, "shape", "") != "词条"
            and str(getattr(it, "channel_id", "") or getattr(it, "channel", "")) not in _blocked]
    pool.sort(key=lambda x: (-_priority(x), getattr(x, "seq", 0)))
    targets = pool[:limit]
    if not targets:
        return {"启用": True, "尝试": 0, "救回": 0, "逐条": [],
                "排除已淘汰渠道": len(_blocked),
                "说明": "候选池无「摘要未取得」条目（或全部来自已淘汰渠道），无需二次补采"}

    jina_cfg = dict(cfg.get("source_expansion", {}) or {})
    jina_cfg["_api_key"] = str(jina_cfg.get("jina_api_key") or "")
    t0 = time.time()
    rows: List[List[str]] = []
    rescued = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(_try_one, it.url, timeout, jina_timeout, jina_cfg): it
                for it in targets}
        for fut in as_completed(futs):
            it = futs[fut]
            if time.time() - t0 > deadline:
                rows.append([str(it.title)[:80], "超时总闸", "整批二次补采到达时限，本条如实留空"])
                continue
            try:
                text, why = fut.result()
            except Exception as exc:
                text, why = "", f"二次补采异常 {type(exc).__name__}"
            if text:
                it.abstract_body = text[:6000]
                it.abstract, it.abstract_source = build_abstract(it)
                if str(it.abstract_status) in ("有效", "正文补采"):
                    rescued += 1
                    rows.append([str(it.title)[:80], "救回", why])
                    continue
            rows.append([str(it.title)[:80], "仍空白", why])

    return {
        "启用": True, "尝试": len(targets), "救回": rescued,
        "仍空白": len(targets) - rescued,
        "耗时秒": round(time.time() - t0, 1),
        "逐条": rows,
        "说明": ("二次补采只认独立原文页正文；URL 变体 / AMP / 移动版 / Jina 均未取得时"
                 "保持「摘要未取得」，不用标题或搜索 snippet 冒充。"),
    }
