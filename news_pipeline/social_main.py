"""需求②：社交平台主干 + 快讯/拼盘源全量拆条 + 逐条回查独立原文。

与「仅作热度参考、原样留快照」的旧行为区别：

* 10 个社交平台登记为**主干发现入口**（不是补充、不是仅热度参考）；
* 60 秒早报 / 新闻联播 / 财联社电报 / 澎湃快讯 / 热榜快照按 01 §3.4
  「多事件拼盘稿：拆分优先」**全量拆条**，母行与被拆词条逐条进附录 15，
  绝不整页当拼盘丢掉；
* 每个词条必须回查到**可点开的独立原文 URL** 才成行（01 §3.3）；
  只有话题名 + 热度值、或回查不到的，进「线索待原文」留痕，不进候选池；
* 标题里的月日只是**日期线索**，写 date_clue，绝不反推发布时间（00 §7 坑 3/4/5）。

本模块不吃 snippet：回查成功后摘要取独立原文正文（fetch_body / trafilatura），
失败就如实标「未取得」，不拿热度值或话题名凑摘要。
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from .model import RawItem
from .timewindow import parse_datetime_text

# 需求②：10 个平台，一一登记，逐平台申报触达状态
SOCIAL_MAIN_PLATFORMS: List[Tuple[str, str, str]] = [
    ("微博·热搜", "https://weibo.com/ajax/statuses/hot_band", "官方 JSON，无鉴权"),
    ("百度·热搜", "https://top.baidu.com/board?tab=realtime", "榜单 HTML"),
    ("抖音·热点", "https://www.douyin.com/aweme/v1/web/hot/search/list/", "官方端点，需签名"),
    ("今日头条·热榜", "https://www.toutiao.com/hot-event/hot-board/", "榜单端点"),
    ("知乎·热榜", "https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total?limit=50", "API"),
    ("B站·热门", "https://api.bilibili.com/x/web-interface/popular?ps=20", "API"),
    ("小红书·热点", "https://www.xiaohongshu.com/explore", "动态渲染"),
    ("快手·热点", "https://www.kuaishou.com/?isHome=1", "动态渲染"),
    ("百度贴吧·热议", "https://tieba.baidu.com/hottopic/browse/topicList", "榜单页"),
    ("微信·搜一搜·搜索发现", "site:mp.weixin.qq.com 定向检索", "需 SERPER/TAVILY key"),
]

# 需求②：5 类必须全量拆条的快讯/拼盘源
DIGEST_SOURCES = ["每天60秒读报", "新闻联播", "财联社电报", "澎湃快讯", "热榜快照"]

# 列举导语 / 罗列尾巴（01 §3.4）
_LIST_LEAD = re.compile(
    r"(资讯早餐|早报|要闻速览|今日(?:头条|三条)|列出|如下|以下|赛果[:：]|"
    r"体育要闻[:：]|文娱影视[:：]|三条分别为|快讯汇总)")
_TAIL = re.compile(r"(同榜还有|另有|同日还报|还有[:：]|其他(?:热点|快讯)|相关词条)")
# 叙述性尾巴：不切成假词条（01 §3.4）
_NARRATIVE = re.compile(
    r"^(援引|报道称|据悉|提振|维持在|跌幅|收于|同比|环比|预计|分析认为|"
    r"截至目前|数据显示|受此影响|业内预计)")

# 编号 / 分号 / 换行 / 序号词 → 拆条
_NUM = re.compile(r"^\s*(?:\d+[、.．)）]|[①②③④⑤⑥⑦⑧⑨⑩]|第[一二三四五六七八九十]+[条则]|"
                  r"【[^】]{1,18}】)\s*")
_SPLIT = re.compile(r"[\r\n]+|；|;(?=\S)|\s{2,}|\s*\|\s*")
_HEAT = re.compile(r"\d+(?:\.\d+)?\s*万?(?:热度|阅读|讨论|播放|次)?|#\S+#|话题号[:：]\d+")
_MONTHDAY = re.compile(r"(?:20\d{2}年)?\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{1,2}[-/]\d{1,2}")


def split_digest(text: str, source_name: str) -> List[dict]:
    """把一条拼盘稿**全量**拆成独立词条；返回母行 + N 个词条，一条不丢。

    只做机械切分：列举导语裁掉、罗列尾巴每个词条都成行、叙述性尾巴不切成假词条。
    裁剪回退到上一个完整句末，不留半句。
    """
    text = (text or "").strip()
    if not text or len(text) < 20:
        return []   # 源名 / 栏目名不是拼盘正文，不拆假词条
    lead = _LIST_LEAD.search(text[:80])
    body = text[lead.end():] if lead else text
    # 罗列尾巴标记之后的内容仍要拆，不整段丢弃
    tail = _TAIL.search(body)
    tail_text = body[tail.end():] if tail else ""
    head_text = body[:tail.start()] if tail else body
    chunks: List[str] = []
    for seg in (head_text, tail_text):
        if not seg.strip():
            continue
        for piece in _SPLIT.split(seg):
            piece = piece.strip()
            if not piece:
                continue
            # 回退到上一个完整句末：结尾没有句末标点则补「。」（不切半句）
            if piece[-1] not in "。！？!?…":
                piece += "。"
            if _NARRATIVE.match(piece):
                continue  # 叙述性尾巴是主体事件补充数据，不是独立词条
            chunks.append(piece)
    out = [{"类型": "母行", "词条": text[:120], "来源": source_name,
            "独立原文": "—", "处置": "母行保留，词条全量拆出（01 §3.4）"}]
    for i, c in enumerate(chunks, 1):
        clean = _NUM.sub("", c)
        clean = _HEAT.sub("", clean).strip()
        if len(clean) < 6:
            continue
        md = _MONTHDAY.search(clean)
        out.append({
            "类型": "拆出词条", "序号": i, "词条": clean[:120], "来源": source_name,
            "日期线索": md.group(0) if md else "",     # 只留痕，不反推发布时间
            "独立原文": "待回查", "处置": "逐条回查独立原文后成行；未取得进附录 15",
        })
    return out


def _match_in_index(word: str, index: List[RawItem]) -> Optional[RawItem]:
    """站内回查：在本次真实抓取到的独立原文里按标题实词做保守匹配。"""
    keys = [w for w in re.split(r"[^\u4e00-\u9fa5A-Za-z0-9]+", word) if len(w) >= 2]
    if not keys:
        return None
    best, score = None, 0
    for it in index:
        if not it.url or not it.title:
            continue
        n = sum(1 for k in keys if k in it.title)
        if n > score:
            best, score = it, n
    return best if score >= max(2, len(keys) // 2) else None


def _fetch_body(url: str, timeout: float) -> str:
    try:
        from .live_cn import fetch_body
        return fetch_body(url, timeout) or ""
    except Exception:
        return ""


def lookup_independent(word: str, platform: str, cfg: dict,
                       index: List[RawItem], win) -> Tuple[Optional[RawItem], str]:
    """逐条回查独立原文。返回 (成行条目 or None, 留痕原因)。

    顺序：已抓独立原文索引 → 独立原文 URL 直取正文 → 搜索 API（配 key 时）。
    任何一步拿不到「可点开的独立文章 + 事件正文」，一律如实留痕，不吃 snippet。
    """
    hit = _match_in_index(word, index)
    if hit and hit.url:
        body = hit.abstract_body or _fetch_body(hit.url, float(
            cfg.get("source", {}).get("live", {}).get("article_timeout", 6)) + 2)
        if body:
            it = RawItem(
                title=hit.title or word.strip(), abstract=body[:600], abstract_body=body[:600],
                abstract_status="正文补采", source=hit.source or platform, url=hit.url,
                pub=hit.pub, pub_raw=hit.pub_raw, time_basis=hit.time_basis,
                time_mark=hit.time_mark, shape="拆出词条", has_event=True,
                record_type="拼盘拆出词条·回查成稿", evidence="A",
                board_platforms=[platform], channel=platform,
                date_clue=(_MONTHDAY.search(word).group(0) if _MONTHDAY.search(word) else ""),
            )
            return it, "回查成功：命中本次真实抓取的独立原文"
    # 搜索 API（统一搜索层：Serper / Tavily / Bing 兜底，带可复算硬闸）。
    # 旧版 import 了 collectors 里根本不存在的 `_lookup_via_search`，每条词条都抛
    # ImportError，except 又直接 return —— 结果 50 条微博真实词条 100% 死在这一行，
    # 连下面的必应站内兜底都走不到。这是 §29 社交 0 成稿的一个真代码 bug。
    live = cfg.get("source", {}).get("live", {})
    search_err = ""
    if live.get("search"):
        try:
            from . import search_api
            hit = search_api.search_best(word, win=win, cfg=cfg)
            if hit and hit.url:
                # 硬约束：搜索只给 URL，不吃 snippet；必须从独立原文页补到正文才成行
                body = _fetch_body(hit.url, float(
                    cfg.get("source", {}).get("live", {}).get("article_timeout", 6)) + 2)
                if body:
                    pub, basis = parse_datetime_text(hit.date or "", hint_year=win.end.year)
                    it = RawItem(
                        title=hit.title or word.strip(), abstract=body[:600],
                        abstract_body=body[:600], abstract_status="正文补采",
                        source=hit.source or platform, url=hit.url, pub=pub,
                        pub_raw=hit.date or "未取得", time_basis=basis or "搜索 API date 字段",
                        time_mark="窗口内实证" if pub and win.contains(pub) else "时点待核",
                        shape="拆出词条", has_event=True,
                        record_type="拼盘拆出词条·回查成稿", evidence="A",
                        board_platforms=[platform], channel=platform,
                        date_clue=(_MONTHDAY.search(word).group(0) if _MONTHDAY.search(word) else ""),
                    )
                    return it, "回查成功：搜索 API 独立原文 → 正文补采"
                search_err = "搜索命中但独立原文正文补采未取得；"
        except Exception as exc:
            # 不再中断：统一搜索层异常也要继续走下面的无 key 站内兜底
            search_err = f"搜索 API 异常 {type(exc).__name__}（继续走站内兜底）；"
    # 无 key 兜底：必应可达搜索页 → 独立原文 URL → 正文补采（不吃 snippet）。
    # fetch_body 只对白名单权威域名返回正文，百科 / 聚合 / 营销页自动落空，
    # 落空就如实留痕，不拿话题名或热度值凑摘要。
    try:
        from .reach import bing_backlink
        r = bing_backlink(word)
        if r and r.get("url"):
            body = _fetch_body(r["url"], float(
                cfg.get("source", {}).get("live", {}).get("article_timeout", 6)) + 2)
            if body:
                pub, basis = parse_datetime_text(r.get("date", ""), hint_year=win.end.year)
                it = RawItem(
                    title=r.get("title") or word.strip(), abstract=body[:600],
                    abstract_body=body[:600], abstract_status="正文补采",
                    source=r.get("source") or platform, url=r["url"], pub=pub,
                    pub_raw=r.get("date") or "未取得",
                    time_basis=basis or "必应站内回查（发布时间未取得，标时点推定）",
                    time_mark="窗口内实证" if pub and win.contains(pub) else "时点推定",
                    shape="拆出词条", has_event=True,
                    record_type="拼盘拆出词条·回查成稿", evidence="A",
                    board_platforms=[platform], channel=platform,
                    date_clue=(_MONTHDAY.search(word).group(0) if _MONTHDAY.search(word) else ""),
                )
                return it, "回查成功：必应站内回查 → 权威独立原文正文补采（无 key）"
    except Exception as exc:
        return None, f"站内回查失败：{type(exc).__name__}"
    return None, (search_err +
                  "无 key 兜底亦未取得独立原文正文："
                  "词条只有话题名与热度值，按 01 §3.3 独立原文准入剔除，进附录 15 线索待原文；"
                  "GitHub Actions 换网络后由 reach.py 自动重测并启用")


def collect_social_main(cfg: dict, win, raw: List[RawItem],
                        social_snapshot: dict | None = None) -> dict:
    """主干采集入口：10 平台申报 + 5 类拼盘全量拆条 + 逐条回查。

    social_snapshot 为 live_cn.collect_social_hotboards 的真实请求结果（可为空）。
    返回 dict：items（可进候选池）/ digest_rows（附录 15）/ platforms（逐平台申报）/ stats。
    """
    conf = cfg.get("social_main", {}) or {}
    if not conf.get("enabled", True):
        return {"items": [], "digest_rows": [], "platforms": [], "stats": {"开关": "关闭"},
                "clues": []}
    platforms: List[dict] = []
    words: List[Tuple[str, str]] = []      # (词条, 发现平台)
    index = [x for x in raw if x.url and x.shape != "词条"]

    # 1) 10 个平台逐条登记为主干入口，并采集真实词条
    snap = social_snapshot or {}
    # live_cn 真实快照：snap["快照"] 为微博逐条，snap["其它平台对照"] 为逐平台探测
    for r in (snap.get("快照") or []):
        if isinstance(r, dict) and r.get("词条"):
            words.append((str(r["词条"]), "微博·热搜"))
    others = snap.get("其它平台对照") or []
    alias = {"百度热搜": "百度·热搜", "知乎热榜": "知乎·热榜", "B站热门": "B站·热门",
             "今日头条热榜": "今日头条·热榜", "快手": "快手·热点", "小红书": "小红书·热点",
             "抖音": "抖音·热点"}
    ok_platform = {alias.get(str(o.get("平台", "")), str(o.get("平台", "")))
                    for o in others if isinstance(o, dict) and o.get("状态") == 200}
    for name, url, method in SOCIAL_MAIN_PLATFORMS:
        got = sum(1 for _, p in words if p == name)
        if name == "微博·热搜":
            state = f"已触达（{got} 条真实词条，带秒级上榜时间）" if got else "未取得：端点返回空"
        elif name == "微信·搜一搜·搜索发现":
            state = "未触达：需 SERPER/TAVILY key 走 site: 定向检索（词条须逐条回查）"
        elif name in ok_platform:
            state = "已触达（榜单端点 200）"
        else:
            state = "未取得：403 / 动态渲染 / 需签名，逐路径实测见工程留痕 F"
        platforms.append({"平台": name, "入口": url, "采集方式": method,
                          "定位": "主干发现入口", "词条数": got, "状态": state})

    # 2) 5 类快讯/拼盘源全量拆条（母行不丢、词条逐条留痕）
    digest_rows: List[dict] = []
    missing_digest = 0
    for src in conf.get("digest_sources", DIGEST_SOURCES):
        mother = next((x for x in raw if src in (x.title + x.channel)), None)
        text = ((mother.abstract_body or mother.abstract) if mother else "") or ""
        # 没有真实母行正文就如实申报"本批未取得"，绝不把源名当文本拆出假词条
        if not text.strip() or len(text.strip()) < 20:
            missing_digest += 1
            digest_rows.append({
                "类型": "母行", "词条": f"本批未取得「{src}」拼盘稿原文（不拆假词条）",
                "来源": src, "日期线索": "", "独立原文": "未取得",
                "处置": "源站未提供可枚举、带事件正文的快讯流；如实申报采集缺口，不编造词条凑数",
            })
            continue
        rows = split_digest(text, src)
        digest_rows.extend(rows)
        for r in rows:
            if r["类型"] == "拆出词条":
                words.append((r["词条"], src))

    # 3) 逐条回查独立原文（不吃 snippet）
    # 2026-11-21：无 key 多源检索层（keyless_compose）接管逐条回查，配额由
    # keyless.max_clues / deadline_seconds 控制；max_lookup=0 表示「不限，逐条回查」。
    # 旧注释「保护主线搜索额度」是主次倒置：逐条回查本身就是主链路，不是兜底。
    max_lookup = int(conf.get("max_lookup", 40))
    lookup_workers = int(conf.get("lookup_workers", 8))
    deadline_s = float(conf.get("lookup_deadline_seconds", 35))
    # 按「可成稿价值」排序：含数字 / 主体动作 / 短而具体的词条优先，
    # 而不是让榜单前排的宣传话题吃满全部回查预算。
    def _priority(w: tuple) -> tuple:
        t = w[0] or ""
        return (bool(re.search(r"\d|预警|通报|辟谣|破产|裁员|降价|上线|停运|伤亡|事故|地震", t)),
                -abs(len(t) - 24))
    words = sorted(words, key=_priority, reverse=True)
    items: List[RawItem] = []
    clues: List[dict] = []
    seen: set = set()
    ok = 0

    # 2026-11-22 吞吐根因：旧版逐条串行 lookup_independent，
    # 40 条 ×（5s 检索 + 6s 正文）= 440s，而 social 层配额只有 35~45s，
    # 实测只能跑完 3~4 条。改为有界并发 + wait 真截断；
    # 注意社交/热榜词条已在 0a-0 由 keyless 做过多源成稿，这里是拼盘拆条的补充回查。
    def _clue(word, platform, reason):
        return {"词条": word[:120], "发现渠道": platform,
                "日期线索": (_MONTHDAY.search(word).group(0) if _MONTHDAY.search(word) else ""),
                "未成行原因": reason, "去向": "附录 15 线索待原文（不进候选池）"}

    todo = []
    for word, platform in words:
        key = word[:60]
        if key in seen:
            continue
        seen.add(key)
        if max_lookup > 0 and len(todo) >= max_lookup:
            clues.append(_clue(word, platform,
                               f"回查配额上限 {max_lookup}（有界并发已满），本批不逐条回查"))
            continue
        todo.append((word, platform, key))

    results = []
    ex = ThreadPoolExecutor(max_workers=lookup_workers)
    try:
        futs = {ex.submit(lookup_independent, w, p, cfg, index, win): (w, p, k)
                for w, p, k in todo}
        done, pending = wait(futs, timeout=max(1.0, deadline_s))
        for f in pending:
            w, p, _k = futs[f]
            clues.append(_clue(w, p, "社交回查运行预算耗尽，短路该层但本批仍出表"))
        for f in done:
            w, p, k = futs[f]
            try:
                it, why = f.result()
            except Exception as exc:
                it, why = None, f"回查异常 {type(exc).__name__}: {str(exc)[:60]}"
            results.append((w, p, k, it, why))
    finally:
        # 不等后台任务跑完，否则「短路」只跳过结果、实际仍占满预算
        ex.shutdown(wait=False, cancel_futures=True)

    for word, platform, key, it, why in results:
        if it:
            items.append(it)
            ok += 1
        else:
            clues.append(_clue(word, platform, why))
        for r in digest_rows:
            if r["类型"] == "拆出词条" and r["词条"][:60] == key:
                r["独立原文"] = it.url if it else "未取得"
                r["处置"] = why
        for p in platforms:
            if p["平台"] == platform and it:
                p["回查成稿"] = p.get("回查成稿", 0) + 1
    for p in platforms:
        p.setdefault("回查成稿", 0)

    return {
        "items": items, "digest_rows": digest_rows, "platforms": platforms, "clues": clues,
        "stats": {"主干平台数": len(platforms), "拼盘源数": len(conf.get("digest_sources", DIGEST_SOURCES)),
                  "拆出词条": sum(1 for r in digest_rows if r["类型"] == "拆出词条"),
                  "母行数": sum(1 for r in digest_rows if r["类型"] == "母行"),
                  "拼盘源未取得": missing_digest,
                  "回查成稿": ok, "线索待原文": len(clues),
                  "回查并发数": lookup_workers, "回查并发提交": len(todo),
                  "回查层配额秒": deadline_s,
                  "快照本身进主表": bool(conf.get("snapshot_in_main", False))},
    }
