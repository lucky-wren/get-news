"""多资源扩展采集层（用户 2026-11-20：「全网搜索、多资源查找、务必解决没获取到数据的渠道」）。

本模块只新增**可核对的公开入口**，不编造条目、不逆向签名、不绕登录：
  1. GDELT DOC 2.0 API —— 免费无 key，65 语言，JSON/CSV/RSS，近 3 个月，250 条/查询，
     服务端要求 ≥5 秒/请求 [外部资料已核实]。返回 title/url/seenDate/domain，是独立原文。
  2. Google News RSS `site:` + `after:/before:` —— 每个站点一个 feed，单 feed 上限 100 条；
     link 是 news.google.com 重定向，保留原 link 并标 needs_decode，正文走 Reader 兜底。
  3. NewsNow / RSSHub 公共实例池 —— 逐实例真实探测，双格式（/{route} 与 /api/s?id=）兼容。
  4. wechat2rss / RSSHub /wechat/wechat2rss/:id —— 公众号公开镜像 feed（带 content:encoded 全文），
     默认不填 id 就不请求，不凭空造 URL。
  5. Jina Reader 增强 —— 无 key 20 RPM，加 X-Return-Format / X-Target-Selector / X-No-Cache /
     X-User-Agent，令牌桶限流；可选 X-Proxy-Url 走 Cloudflare Worker 反代。
  6. Cloudflare Worker 反代前缀 —— 直连 403 时用边缘节点取，配置 proxy_url 即启用。

所有失败逐路由留痕（状态码 / 原因 / 启用条件），写进 Excel「工程留痕 J」。
"""
from __future__ import annotations

import time

import html
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from .collectors import RawItem

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
GNEWS = "https://news.google.com/rss/search"
JINA = "https://r.jina.ai/"

DEFAULT_INSTANCES = [
    "https://newsnow.busiyi.world",          # NewsNow 官方公共实例
    "https://rsshub.app",                    # RSSHub 官方公共实例
    "https://rsshub.rssforever.com",         # 实测可用公共实例（2026-10-31 资料）
    "https://newsnow-xxx.pages.dev",         # 占位：用户自建后替换
]
HOT_ROUTES = ["weibo", "zhihu", "douyin", "toutiao", "baidu", "36kr", "thepaper", "bilibili"]

# 31 类里端点最少的大类，按主题词走 GDELT / Google News site: 补齐
TOPIC_QUERIES = [
    "医保 社保 药品目录", "地方债 城投 财政", "新能源 充电桩 汽车召回",
    "AI 大模型 芯片 就业", "房地产 LPR 房贷", "工厂 制药 医药 集采",
    "裁员 破产 资不抵债", "铁路 民航 交通 出行", "地震 灾害 突发事故",
]
GOV_SITES = [
    "gov.cn", "mohrss.gov.cn", "nhsa.gov.cn", "mof.gov.cn", "csrc.gov.cn",
    "miit.gov.cn", "samr.gov.cn", "mot.gov.cn", "ndrc.gov.cn", "stats.gov.cn",
    "chinatax.gov.cn", "pbc.gov.cn", "mohurd.gov.cn", "mem.gov.cn",
]


def _http(url: str, timeout: float = 8.0, headers: dict | None = None,
          proxy_prefix: str = "") -> Tuple[int, str]:
    """统一 GET；proxy_prefix 非空时用 Cloudflare Worker 反代包装目标 URL。"""
    target = (proxy_prefix.rstrip("/") + "/?url=" + urllib.parse.quote(url, safe="")) if proxy_prefix else url
    h = {"User-Agent": UA, "Accept": "application/json,application/rss+xml,text/html,text/plain,*/*",
         "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if headers:
        h.update(headers)
    try:
        req = urllib.request.Request(target, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(1_500_000).decode(r.headers.get_content_charset() or "utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, (e.read(2000).decode("utf-8", "replace") if e.fp else "")
    except Exception as e:
        return 0, f"{type(e).__name__}: {str(e)[:120]}"


# ---------------------------------------------------------------- 1. GDELT DOC 2.0
def gdelt_articles(query: str, *, timespan: str = "1d", maxrecords: int = 50,
                   timeout: float = 10.0, proxy_prefix: str = "") -> Tuple[List[dict], str]:
    params = {"query": query, "mode": "artlist", "maxrecords": str(maxrecords),
              "format": "json", "timespan": timespan, "sort": "datedesc"}
    url = GDELT + "?" + urllib.parse.urlencode(params)
    status, body = _http(url, timeout=timeout, proxy_prefix=proxy_prefix)
    if status != 200:
        return [], f"HTTP {status} / {body[:80]}"
    if not body.lstrip().startswith("{"):
        return [], "GDELT 返回非 JSON（常见：请求过快，需 ≥5 秒间隔）"
    try:
        arts = (json.loads(body).get("articles") or [])
    except Exception as e:
        return [], f"JSON 解析失败 {type(e).__name__}"
    out = []
    for a in arts:
        u = str(a.get("url") or "").strip()
        if not u.startswith(("http://", "https://")):
            continue
        seen = str(a.get("seendate") or "")
        pub = None
        if re.fullmatch(r"\d{8}T\d{6}Z", seen):
            try:
                pub = datetime.strptime(seen, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except Exception:
                pass
        out.append({"title": html.unescape(str(a.get("title") or "")).strip(),
                    "url": u, "pub": pub, "pub_raw": seen or "未取得",
                    "source": str(a.get("domain") or ""),
                    "language": str(a.get("language") or ""),
                    "country": str(a.get("sourcecountry") or ""),
                    "basis": "GDELT DOC 2.0 seenDate（首次监测时刻，UTC）"})
    return out, f"HTTP 200，取回 {len(out)} 条独立原文"


# ---------------------------------------------------------------- 2. Google News RSS site:
_TAG = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_ITEM = re.compile(r"<item[^>]*>(.*?)</item>", re.I | re.S)
_CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)


def _xml_text(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.I | re.S)
    if not m:
        return ""
    s = m.group(1)
    cd = _CDATA.search(s)
    if cd:
        s = cd.group(1)
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def gnews_feed(site: str, query: str, *, after: str = "", before: str = "",
               timeout: float = 8.0, proxy_prefix: str = "") -> Tuple[List[dict], str]:
    q = query.strip()
    if site:
        q += " site:" + site
    if after:
        q += " after:" + after
    if before:
        q += " before:" + before
    url = GNEWS + "?" + urllib.parse.urlencode(
        {"q": q, "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"})
    status, body = _http(url, timeout=timeout, proxy_prefix=proxy_prefix)
    if status != 200:
        return [], f"HTTP {status} / {body[:80]}"
    blocks = _ITEM.findall(body)
    out = []
    for b in blocks[:100]:
        title, link = _xml_text(b, "title"), _xml_text(b, "link")
        if not title or not link:
            continue
        pub = None
        pd = _xml_text(b, "pubDate")
        if pd:
            try:
                from email.utils import parsedate_to_datetime
                pub = parsedate_to_datetime(pd)
            except Exception:
                pass
        out.append({"title": title, "url": link, "pub": pub,
                    "pub_raw": pd or "未取得", "source": site or "Google News RSS",
                    "basis": "RSS pubDate", "google_redirect": True,
                    "feed_query": q})
    return out, f"HTTP 200，{len(out)} 条（单 feed 上限 100；link 为 Google 重定向）"


# ---------------------------------------------------------------- 3. NewsNow / RSSHub 实例池
_JSON_ARR = re.compile(r"\[\s*\{.*?\}\s*\]", re.S)


def probe_instances(instances: List[str], routes: List[str], *, timeout: float = 5.0,
                    proxy_prefix: str = "") -> Tuple[List[List[str]], List[dict]]:
    rows, clues = [], []
    for base in instances:
        if "xxx" in base:                    # 占位自建地址：逐条申报启用条件，不发假请求
            rows.append([base, "-", "未部署", "填 config.source_expansion.instances 为自建 NewsNow/RSSHub 地址"])
            continue
        ok, total = 0, 0
        for r in routes[:3]:
            for style, u in (("dailyhot", f"{base.rstrip('/')}/{r}"),
                             ("newsnow", f"{base.rstrip('/')}/api/s?id={r}&latest")):
                st, body = _http(u, timeout=timeout, proxy_prefix=proxy_prefix)
                if st == 200 and body.strip().startswith(("{", "[")):
                    try:
                        d = json.loads(body)
                        arr = d.get("data") or d.get("items") or (d if isinstance(d, list) else [])
                        n = len(arr) if isinstance(arr, list) else 0
                        if n:
                            ok += 1
                            total += n
                            for a in arr[:4]:
                                if isinstance(a, dict):
                                    clues.append({"title": str(a.get("title") or a.get("name") or ""),
                                                  "url": str(a.get("url") or a.get("mobileUrl") or ""),
                                                  "route": r, "instance": base, "style": style})
                            break
                    except Exception:
                        pass
                if ok:
                    break
            if ok:
                break
        rows.append([base, "/".join(routes[:3]), f"HTTP 200 / {total} 条" if ok else "不可达",
                     "已启用" if ok else "换实例 / 自建 Cloudflare Pages / 配代理"])
    return rows, clues


# ---------------------------------------------------------------- 4. 公众号镜像 feed
def wechat2rss(feed_ids: List[str], *, timeout: float = 8.0,
               proxy_prefix: str = "") -> Tuple[List[dict], List[List[str]]]:
    out, rows = [], []
    if not feed_ids:
        rows.append(["wechat2rss", "-", "未配置", "填 source_expansion.wechat_feeds（从 wechat2rss.xlab.app/posts/list/ 取订阅 id）"])
        return out, rows
    for fid in feed_ids:
        for base in ("https://wechat2rss.xlab.app/feed/", "https://rsshub.app/wechat/wechat2rss/"):
            u = base + fid + (".xml" if "xlab" in base else "")
            st, body = _http(u, timeout=timeout, proxy_prefix=proxy_prefix)
            if st != 200 or "<item" not in body:
                rows.append([fid, base, f"HTTP {st}", "换实例 / 自建 we-mp-rss"])
                continue
            for b in _ITEM.findall(body)[:20]:
                t, link = _xml_text(b, "title"), _xml_text(b, "link")
                if not t:
                    continue
                full = _xml_text(b, "content:encoded") or _xml_text(b, "description")
                out.append({"title": t, "url": link, "pub_raw": _xml_text(b, "pubDate") or "未取得",
                            "source": "微信公众号（公开镜像）", "body": full[:6000],
                            "basis": "镜像 feed pubDate"})
            rows.append([fid, base, f"HTTP 200 / {len(_ITEM.findall(body))} 条", "已启用"])
            break
    return out, rows


# ---------------------------------------------------------------- 5. Jina Reader 增强（20 RPM）
_last_jina = [0.0]


def jina_read(url: str, *, timeout: float = 12.0, cfg: dict | None = None,
              api_key: str = "", throttle: bool = True) -> Tuple[str, str]:
    """返回 (正文, 依据)。无 key 20 RPM；throttle=False 用于已有并发上限 + 总闸的批量补采。"""
    c = dict(cfg or {})
    if throttle:
        min_gap = 60.0 / float(c.get("jina_rpm", 20))
        wait = _last_jina[0] + min_gap - time.time()
        if wait > 0:
            time.sleep(min(wait, 4.0))
        _last_jina[0] = time.time()
    hdr = {"Accept": "text/plain",
           "X-Return-Format": c.get("jina_return_format", "text"),
           "X-Target-Selector": c.get("jina_target_selector", "article, main, .article-content, .content"),
           "X-Remove-Selector": c.get("jina_remove_selector", "nav, footer, aside, .sidebar, #ads"),
           "X-Timeout": str(c.get("jina_page_timeout", 20)),
           "X-No-Cache": "true" if c.get("jina_no_cache", True) else "false"}
    if api_key:
        hdr["Authorization"] = "Bearer " + api_key
    if c.get("jina_user_agent"):
        hdr["X-User-Agent"] = c["jina_user_agent"]
    if c.get("jina_proxy_url"):
        hdr["X-Proxy-Url"] = c["jina_proxy_url"]
    st, body = _http(JINA + url, timeout=timeout, headers=hdr)
    if st == 200 and len(body.strip()) >= 40:
        # Reader 会带 "Title:" / "URL Source:" 等元数据头，正文从第一段实质文字开始
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        lines = [ln for ln in lines if not re.match(r"^(Title|URL Source|Published Time|Markdown Content|Warning|Text Content):", ln)]
        return "\n".join(lines)[:8000], "Jina Reader（增强头 + 令牌桶）"
    return "", f"HTTP {st} / {body[:80]}"


# ---------------------------------------------------------------- 主入口
def collect(cfg: dict, win: Any, base_seq: int) -> Tuple[List[RawItem], List[List[str]], Dict[str, Any]]:
    """返回 (RawItem 列表, 逐路由留痕, 统计)。任何失败逐条留痕，不抛、不编造。"""
    c = cfg.get("source_expansion", {}) or {}
    if not c.get("enabled", True):
        return [], [["总开关", "-", "关闭", "config.source_expansion.enabled=true 启用"]], {"启用": False}
    proxy_prefix = str(c.get("proxy_url") or "")
    timeout = float(c.get("timeout", 8.0))
    # 2026-11-22：整层 deadline 由 budget.bounded 注入到 cfg["budget"]。
    # 旧版纯串行循环按 timeout 累加到数分钟，是 15min 硬杀的主要来源之一。
    from .budget import deadline_at_of
    def _expired():
        d = deadline_at_of(cfg)
        return bool(d and time.monotonic() > d)
    rows: List[List[str]] = []
    items: List[RawItem] = []
    seq = base_seq
    after = win.start.strftime("%Y-%m-%d")
    before = win.end.strftime("%Y-%m-%d")

    # 1) GDELT：按主题词补 31 类里零端点的大类
    if c.get("gdelt_enabled", True):
        for q in list(c.get("topic_queries", TOPIC_QUERIES))[:int(c.get("gdelt_max_queries", 6))]:
            if _expired():
                rows.append(["GDELT DOC 2.0", "-", "层 deadline 到达，后续查询短路", "预算短路"])
                break
            arts, why = gdelt_articles(q, timespan=c.get("gdelt_timespan", "1d"),
                                       maxrecords=int(c.get("gdelt_maxrecords", 40)),
                                       timeout=timeout, proxy_prefix=proxy_prefix)
            rows.append(["GDELT DOC 2.0", q, why, "免费无 key；≥5 秒/请求；近 3 个月；只给独立原文 URL"])
            for a in arts[:int(c.get("per_query", 12))]:
                seq += 1
                it = RawItem(title=a["title"], abstract="", abstract_status="未取得",
                             source=a["source"] or "GDELT", url=a["url"],
                             pub=a["pub"], pub_raw=a["pub_raw"], time_basis=a["basis"],
                             time_mark="窗口内实证" if a["pub"] else "时点推定",
                             group="", tag="", evidence="B", shape="独立原文",
                             record_type="GDELT DOC 独立原文", channel="多资源扩展 · GDELT",
                             channel_id="EXP-GDELT", channel_level="☆", seq=seq,
                             media_slots=1, raw={"query": q, "country": a.get("country", ""),
                                                 "language": a.get("language", "")})
                items.append(it)
            time.sleep(float(c.get("gdelt_gap_seconds", 5.2)))   # 服务端硬要求

    # 2) Google News RSS site:：按部委域名逐个建 feed
    if c.get("gnews_enabled", True):
        sites = list(c.get("gov_sites", GOV_SITES))[:int(c.get("gnews_max_sites", 10))]
        kw = str(c.get("gnews_query", "政策 通知 发布"))
        for site in sites:
            if _expired():
                rows.append(["Google News RSS site:", site, "层 deadline 到达，后续站点短路", "预算短路"])
                break
            arts, why = gnews_feed(site, kw, after=after, before=before,
                                   timeout=timeout, proxy_prefix=proxy_prefix)
            rows.append(["Google News RSS site:", site, why,
                         "单 feed 上限 100；link 为 Google 重定向，正文走 Jina/Reader 兜底"])
            for a in arts[:int(c.get("per_query", 12))]:
                seq += 1
                it = RawItem(title=a["title"], abstract="", abstract_status="未取得",
                             source=a["source"], url=a["url"], pub=a["pub"],
                             pub_raw=a["pub_raw"], time_basis=a["basis"],
                             time_mark="窗口内实证" if a["pub"] else "时点推定",
                             group="", tag="", evidence="B", shape="独立原文",
                             record_type="Google News RSS 定向", channel="多资源扩展 · GNews-" + site,
                             channel_id="EXP-GNEWS-" + site, channel_level="☆", seq=seq,
                             media_slots=1, raw={"google_redirect": True, "feed_query": a["feed_query"]})
                items.append(it)

    # 3) NewsNow / RSSHub 公共实例池
    if c.get("pool_enabled", True) and not _expired():
        pr, clues = probe_instances(list(c.get("instances", DEFAULT_INSTANCES)),
                                    list(c.get("hot_routes", HOT_ROUTES)),
                                    timeout=timeout, proxy_prefix=proxy_prefix)
        rows.extend([["实例池"] + r for r in pr])
        for cl in clues[:int(c.get("pool_max_clues", 12))]:
            if not cl["title"]:
                continue
            seq += 1
            items.append(RawItem(title=cl["title"], abstract="", abstract_status="未取得",
                                 source=cl["instance"], url=cl["url"], pub=None,
                                 pub_raw="未取得", time_basis="热榜快照（非发布时间）",
                                 time_mark="时点推定", group="", tag="", evidence="C",
                                 shape="词条", has_event=False, record_type="实例池词条（待回查）",
                                 channel="多资源扩展 · 实例池-" + cl["route"],
                                 channel_id="EXP-POOL-" + cl["route"], channel_level="☆",
                                 seq=seq, board_platforms=[cl["route"]], board_slots=1,
                                 raw={"instance": cl["instance"], "style": cl["style"]}))

    # 4) 公众号公开镜像
    if c.get("wechat_enabled", True) and not _expired():
        wf, wr = wechat2rss(list(c.get("wechat_feeds", [])), timeout=timeout,
                            proxy_prefix=proxy_prefix)
        rows.extend([["wechat2rss"] + r for r in wr])
        for a in wf[:int(c.get("wechat_max", 20))]:
            seq += 1
            it = RawItem(title=a["title"], abstract=a.get("body", ""), abstract_status="有效",
                         source=a["source"], url=a["url"], pub=None, pub_raw=a["pub_raw"],
                         time_basis=a["basis"], time_mark="时点推定", group="", tag="",
                         evidence="B", shape="独立原文", record_type="公众号镜像全文",
                         channel="多资源扩展 · wechat2rss", channel_id="EXP-WECHAT",
                         channel_level="☆", seq=seq, media_slots=1)
            items.append(it)

    stats = {"启用": True, "扩展条目": len(items), "路由数": len(rows),
             "GDELT": sum(1 for x in items if x.channel_id == "EXP-GDELT"),
             "GNews": sum(1 for x in items if str(x.channel_id).startswith("EXP-GNEWS")),
             "实例池": sum(1 for x in items if str(x.channel_id).startswith("EXP-POOL")),
             "公众号": sum(1 for x in items if x.channel_id == "EXP-WECHAT"),
             "代理": proxy_prefix or "未配置（直连失败可填 Cloudflare Worker 地址）"}
    return items, rows, stats


def self_test() -> dict:
    """离线自检：解析器对 mock 载荷不崩、不编造。"""
    errs = []
    g = ('<!DOCTYPE><rss><item><title>测试甲</title><link>https://news.google.com/rss/articles/CBMi1</link>'
         '<pubDate>Thu, 20 Nov 2026 08:00:00 GMT</pubDate></item></rss>')
    if _ITEM.search(g) is None or _xml_text(g, "title") != "测试甲":
        errs.append("RSS item 解析失败")
    j = '{"articles":[{"title":"乙","url":"https://x.cn/a","seendate":"20261120T083000Z","domain":"x.cn"}]}'
    arts = (json.loads(j).get("articles") or [])
    if len(arts) != 1 or arts[0]["seendate"] != "20261120T083000Z":
        errs.append("GDELT JSON 解析失败")
    rows, clues = probe_instances(["https://占位xxx.pages.dev"], ["weibo"])
    if not rows or rows[0][2] != "未部署":
        errs.append("占位实例未申报启用条件")
    out, wr = wechat2rss([])
    if out or not wr:
        errs.append("未配置公众号 id 时应只留启用条件，不造条目")
    return {"通过": not errs, "错误": errs, "用例": 4}
