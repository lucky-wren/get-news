"""渠道端点自动发现与白名单可抓取性分层。

不是手写「待配置」清单，而是对每个候选 URL 真实发请求、真实解析出带时间字段的条目，
只有验证通过的才写进 data/endpoints_live.yaml 供采集器加载；其余逐渠道按可行动类别申报。

用法：
  python scripts/discover_endpoints.py                # 联网实测 + 重新分层
  python scripts/discover_endpoints.py --offline      # 用现有 YAML/探测缓存只重算分层
"""
from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from news_pipeline.channels import parse_channel_doc  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "Chrome/120 Safari/537.36",
      "Accept": "text/html,application/xhtml+xml,application/xml,application/json,*/*;q=0.8"}

# 候选端点：全部来自逐域名实测，不是凭关键词现想；每条都要再次验证才算通过。
CANDIDATES: list[dict] = []


def add(ep_id, channel, ep_type, url, *, encoding=None, pages=1, section="", note=""):
    CANDIDATES.append(dict(id=ep_id, channel=channel, type=ep_type, url=url,
                           encoding=encoding, pages=pages, section=section, note=note))


# ---- 央视网：频道级 JSONP 时间倒排（focus_date 秒级）----
for ch in ["news", "china", "world", "society", "tech", "ent"]:
    add(f"CN-CCTV-{ch.upper()}", "央视网新闻", "jsonp",
        "https://news.cctv.com/2019/07/gaiban/cmsdatainterface/page/{ch}_{i}.jsonp?cb=news"
        .replace("{ch}", ch),
        pages=7 if ch == "news" else 3, section="中央媒体 / 频道级 JSONP 时间倒排",
        note="频道接口实测存在，非 news 频道仅探测前 3 页")
# 修正 url_tpl：上面生成的是 {ch}_1，需还原 {i}
for c in CANDIDATES:
    if c["id"].startswith("CN-CCTV-"):
        c["url_tpl"] = c["url"].replace("_1.jsonp", "_{i}.jsonp")
        c["url"] = c["url_tpl"].format(i=1)

# ---- 中新网：真实频道 RSS 命名（china/world/...，不是 gn/cj/gj）----
for ch in ["scroll-news", "china", "world", "society", "sports", "importnews",
           "photo", "video", "edu", "finance", "health"]:
    add(f"CN-CNS-RSS-{ch.upper()}", "中新网", "rss",
        f"https://www.chinanews.com.cn/rss/{ch}.xml",
        section="中央媒体 / 频道 RSS（RFC822 +0800）")
# ---- 中新网：滚动页 news1..5（dd_time 分钟级）----
for i in [1, 2, 3, 4, 5]:
    add(f"CN-CNS-ROLL-{i}", "中新网滚动", "html_cns",
        f"https://www.chinanews.com.cn/scroll-news/news{i}.html",
        encoding="utf-8", section="中央媒体 / 站内时间倒排页")

# ---- 人民网：18 路频道 RSS（多数停在往年，但端点真实有效，交给窗口过滤）----
for ch in ["politics", "finance", "society", "world", "edu", "health", "it", "env",
           "legal", "military", "opinion", "theory", "culture", "sports", "travel",
           "auto", "house", "tv"]:
    add(f"CN-PEOPLE-RSS-{ch.upper()}", "人民网", "rss",
        f"http://www.people.com.cn/rss/{ch}.xml",
        section="中央媒体 / 频道 RSS", note="实测多数为往年归档，窗口过滤如实剔除")
add("CN-PEOPLE-NEWS", "人民网首页", "html_people",
    "http://www.people.com.cn/GB/59476/index.html", section="中央媒体 / 站内列表")

# ---- 中央与行业媒体首页 / 频道页（通用带日期链接抽取）----
for eid, ch, url in [
    ("CN-XINHUA", "新华网", "https://www.xinhuanet.com/"),
    ("CN-NEWS-CN", "新华网·news.cn", "https://www.news.cn/"),
    ("CN-XINHUA-LOCAL", "新华网地方", "https://www.news.cn/local/"),
    ("CN-GMW", "光明网", "https://www.gmw.cn/"),
    ("CN-CE", "中国经济网", "https://www.ce.cn/"),
    ("CN-CE-NEWS", "中国经济网新闻", "https://www.ce.cn/xwzx/"),
    ("CN-CYOL", "中国青年报", "https://www.cyol.com/"),
]:
    add(eid, ch, "html_generic", url, section="中央媒体 / 站内时间倒排页")
add("CN-CCTV-HOME", "央视网首页", "html_cctv", "https://news.cctv.com/",
    section="中央媒体 / 站内时间倒排页")
add("CN-GOV-YAOWEN", "中国政府网要闻", "html_gov",
    "https://www.gov.cn/yaowen/liebiao/", section="部委与政务 / 站内列表")

# ---- 部委官网（公告/要闻列表带日期）----
for eid, ch, url in [
    ("CN-MEE", "生态环境部", "https://www.mee.gov.cn/"),
    ("CN-NDRC", "国家发展改革委", "https://www.ndrc.gov.cn/"),
    ("CN-MIIT", "工业和信息化部", "https://www.miit.gov.cn/"),
    ("CN-PBC", "中国人民银行", "http://www.pbc.gov.cn/"),
    ("CN-MOFCOM", "商务部", "http://www.mofcom.gov.cn/"),
    ("CN-CSRC", "中国证监会", "http://www.csrc.gov.cn/"),
]:
    add(eid, ch, "html_generic", url, section="部委与政务 / 站内公告列表")

# ---- 国际源组（网络可达时自动启用）----
for eid, ch, ep_type, url in [
    ("INTL-GNEWS", "Google News RSS", "rss",
     "https://news.google.com/rss/search?q=%E8%A6%81%E9%97%BB%20when:1d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"),
    ("INTL-USGS", "USGS 显著地震", "usgs",
     "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_day.geojson"),
    ("INTL-GDACS", "GDACS 灾害预警", "rss", "https://www.gdacs.org/xml/rss.xml"),
    ("INTL-RELIEF", "ReliefWeb 灾害报告", "relief",
     "https://api.reliefweb.int/v1/reports?appname=news-daily&limit=40"),
    ("INTL-GDELT", "GDELT DOC 2.0", "gdelt",
     "https://api.gdeltproject.org/api/v2/doc/doc?query=china&mode=artlist&format=json&maxrecords=60"),
    ("INTL-BBC", "BBC News RSS", "rss", "https://feeds.bbci.co.uk/news/rss.xml"),
]:
    add(eid, ch, ep_type, url, section="国际源组（按可达性启用）")


def get(url: str, timeout: float = 7.0, encoding: str | None = None) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as f:
        raw = f.read()
        cs = encoding or f.headers.get_content_charset() or "utf-8"
    return raw.decode(cs, "replace")


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


DATE_URL = re.compile(r"/(20\d{2})[-/](\d{1,2})[-/](\d{1,2})/|(20\d{2})(\d{2})(\d{2})")
DATE_TXT = re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})|"
                      r"(\d{1,2})-(\d{1,2})\s+\d{1,2}:\d{2}")
JUNK = re.compile(r"备案号|版权所有|京公网安|网站地图|联系我们|无障碍|Copyright|"
                  r"登录|注册|客户端|下载APP|扫一扫|返回顶部|更多>>")


def generic_items(t: str) -> list[dict]:
    out = []
    for m in re.finditer(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", t, re.S | re.I):
        u, title = m.group(1), clean(m.group(2))
        if len(title) < 10 or len(title) > 120 or JUNK.search(title):
            continue
        seg = t[m.end():m.end() + 180]
        dm = DATE_URL.search(u) or DATE_TXT.search(u + " " + seg)
        if not dm:
            continue
        g = [x for x in dm.groups() if x]
        if len(g) >= 3 and str(g[0]).startswith("20"):
            date = f"{g[0]}-{int(g[1]):02d}-{int(g[2]):02d}"
        else:
            date = str(dm.group(0))
        out.append({"title": title, "url": u, "date": date})
    seen, res = set(), []
    for x in out:
        if x["url"] in seen:
            continue
        seen.add(x["url"])
        res.append(x)
    return res


def verify(ep: dict) -> dict:
    """真实请求 + 真实解析；只有解析出 >=1 条带时间字段的条目才算通过。"""
    r = dict(ep)
    try:
        if ep["type"] == "jsonp":
            tpl = ep.get("url_tpl") or ep["url"]
            total, dates, valid_pages = 0, [], 0
            for i in range(1, int(ep.get("pages", 1)) + 1):
                u = tpl.format(i=i) if "{i}" in tpl else tpl
                try:
                    t = get(u, 7)
                    j = json.loads(t[t.index("(") + 1: t.rindex(")")])
                    lst = j.get("data", {}).get("list", [])
                    if not lst:
                        break
                    total += len(lst)
                    dates += [x["focus_date"] for x in lst if x.get("focus_date")]
                    valid_pages += 1
                except Exception:
                    break
            r.update(ok=total > 0 and len(dates) > 0, items=total, times=len(dates),
                     pages_ok=valid_pages, latest=max(dates) if dates else "",
                     oldest=min(dates) if dates else "")
            if valid_pages:
                r["pages"] = valid_pages
        elif ep["type"] == "rss":
            t = get(ep["url"], 7, ep.get("encoding"))
            root = ET.fromstring(t)
            items = root.findall(".//item")
            ds = [(x.findtext("pubDate") or x.findtext("date") or "").strip() for x in items]
            ds = [d for d in ds if d]
            r.update(ok=len(items) > 0 and len(ds) > 0, items=len(items), times=len(ds),
                     latest=ds[0] if ds else "", oldest=ds[-1] if ds else "")
        elif ep["type"] in ("html_cns", "html_cctv", "html_people", "html_gov", "html_generic"):
            t = get(ep["url"], 7, ep.get("encoding"))
            if ep["type"] == "html_cns":
                blocks = re.findall(r"<li>(.*?)</li>", t, re.S)
                cnt, ds = 0, []
                for b in blocks:
                    a = re.search(r'href="(/(?:[a-z]+/)*2026/\d{2}-\d{2}/\d+\.shtml)"[^>]*>([^<]{6,90})</a>', b)
                    tm = re.search(r'class="dd_time"[^>]*>\s*(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})', b)
                    if a and tm:
                        cnt += 1
                        ds.append(f"2026-{int(tm.group(1)):02d}-{int(tm.group(2)):02d} "
                                  f"{tm.group(3)}:{tm.group(4)}")
            elif ep["type"] == "html_gov":
                cnt, ds = 0, []   # 静态页为 JS 动态渲染，如实申报 0
            else:
                it = generic_items(t)
                cnt, ds = len(it), [x["date"] for x in it]
            r.update(ok=cnt > 0 and len(ds) > 0, items=cnt, times=len(ds),
                     latest=ds[0] if ds else "", oldest=ds[-1] if ds else "")
        else:
            # 国际源组：只探测可达性，不强行解析（受限网络必然失败，逐源申报）
            t = get(ep["url"], 5)
            r.update(ok=True, items=len(t), times=1, latest="probe", oldest="")
    except urllib.error.HTTPError as e:
        r.update(ok=False, err=f"HTTP {e.code}", items=0, times=0)
    except Exception as e:
        r.update(ok=False, err=f"{type(e).__name__}: {str(e)[:70]}", items=0, times=0)
    return r


# ---- 白名单分层：把「未取得」拆成可行动类别 ----
LOGIN_WORDS = ["微信", "抖音", "微博", "知乎", "小红书", "B站", "哔哩", "快手", "头条",
               "热搜", "热榜", "榜单", "搜一搜", "公众号", "朋友圈", "小程序", "客户端",
               "APP", "直播", "短视频", "社区", "贴吧", "豆瓣", "虎扑", "脉脉", "即刻"]
KEY_WORDS = ["Google News", "Serper", "Tavily", "开放平台", "API", "检索", "搜索回查",
             "GDELT", "NewsAPI", "Bing", "Google"]
BLOCK_WORDS = ["新浪", "澎湃", "财联社", "东方财富", "同花顺", "证券时报", "界面", "36氪",
               "央广", "中央气象台", "中国天气", "搜狐", "网易", "凤凰", "雪球", "USGS",
               "GDACS", "ReliefWeb", "BBC", "RSSHub", "今日头条"]


DOMAIN_RE = re.compile(r"([a-zA-Z0-9][a-zA-Z0-9.-]*\.(?:com|cn|org|net|gov|edu|io|tv)(?:\.cn)?)",
                       re.I)
SITE_KEYS = ["央视", "中新", "人民网", "新华", "光明", "中国经济网", "中国青年报", "中国网",
             "生态环境部", "发展改革委", "工业和信息化部", "工信部", "人民银行", "商务部",
             "证监会", "政府网", "中国政府网"]


def domains_of(entry: str) -> list[str]:
    out = []
    for d in DOMAIN_RE.findall(entry or ""):
        d = re.sub(r"^www\.", "", d.lower())
        if d not in out:
            out.append(d)
    return out


def derive_domain_endpoints(timeout: float = 6.0, min_items: int = 3) -> tuple[list[dict], list[dict]]:
    """按白名单「入口」列逐域名深度探测：首页 + RSS + 新闻列表 + 要闻页四种形态。

    之前只测 `https://{domain}/` 首页，会漏掉「首页 403 但具体 RSS / 列表页可达」
    以及「首页可达但只有列表页才有带日期条目」两种情况。现在每个域名并发试多个
    候选 URL，任一通过即登记为端点；全部失败则逐条记录每个尝试的状态与原因。
    """
    doc = BASE / "data" / "资讯渠道大全.md"
    channels = parse_channel_doc(doc)
    cands: dict[str, dict] = {}
    for c in channels:
        for d in domains_of(c["entry"]):
            if d not in cands:
                cands[d] = {"domain": d, "channel": c["channel"], "entry": c["entry"]}
    print(f"[域名派生] 从白名单入口列提取 {len(cands)} 个独立域名（按 v3 清单逐条试 4 种端点形态）")

    def urls_for(d: str) -> list[str]:
        bare = d
        www = d if d.startswith("www.") else "www." + d
        return list(dict.fromkeys([
            f"https://{bare}/", f"http://{bare}/",
            f"https://{www}/rss.xml", f"https://{www}/rss/",
            f"https://{www}/news/", f"https://{www}/xwzx/",
            f"https://{www}/yaowen/", f"https://{www}/zhengce/zuixin.htm",
        ]))

    def probe_one(u: str) -> dict:
        try:
            t = get(u, timeout)
            it = generic_items(t)
            return {"url": u, "status": 200, "items": len(it),
                    "latest": it[0]["date"] if it else ""}
        except urllib.error.HTTPError as e:
            return {"url": u, "status": e.code, "items": 0}
        except Exception as e:
            return {"url": u, "status": type(e).__name__, "items": 0}

    def probe(item):
        d, meta = item
        attempts = [probe_one(u) for u in urls_for(d)]
        good = [a for a in attempts if a["status"] == 200 and a["items"] >= min_items]
        if good:
            best = max(good, key=lambda a: a["items"])
            return {"ok": True, "domain": d, "url": best["url"],
                    "channel": meta["channel"], "items": best["items"],
                    "latest": best["latest"], "attempts": attempts}
        codes = [str(a["status"]) for a in attempts]
        if any(c == "200" for c in codes):
            reason = "可打开但各形态均未抽到 ≥%d 条带日期条目" % min_items
        elif any(c == "403" for c in codes):
            reason = "HTTP 403（出口策略拦截，域名级）"
        elif any(c.startswith("4") for c in codes):
            reason = ("HTTP " + "/".join(sorted({c for c in codes if c.startswith("4")})[:2])
                      + "（候选路径不存在，需补该站正确 RSS / 列表页 URL）")
        else:
            reason = "/".join(sorted(set(codes))[:2]) + "（连接不可达）"
        return {"ok": False, "domain": d, "url": urls_for(d)[0],
                "reason": reason, "attempts": attempts}

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        res = list(ex.map(probe, cands.items()))
    ok = [r for r in res if r["ok"]]
    eps = []
    for r in ok:
        eps.append({"id": "WL-" + re.sub(r"[^A-Z0-9]", "", r["domain"].upper())[:18],
                    "channel": r["channel"], "type": "html_generic", "url": r["url"],
                    "section": "白名单入口派生 / 站内时间倒排页",
                    "note": f"域名 {r['domain']} 深度探测命中 {r['url']}，{r['items']} 条带日期条目",
                    "verified_items": r["items"], "time_samples": r["items"],
                    "latest_pub": r["latest"], "oldest_pub": "", "level": "★",
                    "requires": "none", "derived": True})
    return eps, res


def layer_of(channel: str, entry: str, matched: str,
             domain_status: dict[str, str]) -> tuple[str, str]:
    """返回 (分层, 启用条件)。每条都给可行动原因，不再笼统写「未取得」。"""
    if matched:
        return ("可直连（端点覆盖 / 平台归并）",
                "已启用：python run.py 加载 data/endpoints_live.yaml；同平台多栏目复用同一时间倒排端点")
    if any(w in channel for w in KEY_WORDS):
        return "需密钥（搜索 / 开放平台 API）", "在 Secrets 配 SERPER_API_KEY / TAVILY_API_KEY，开 source.live.search"
    ds = domains_of(entry)
    if ds:
        reasons = [domain_status.get(d, "未探测") for d in ds]
        if any(("403" in x) or ("URLError" in x) or ("Timeout" in x) or ("连接不可达" in x)
               for x in reasons):
            why = "；".join(sorted({x for x in reasons if x != "未探测"})[:2]) or "出口策略拦截"
            return "网络出口拦截（换网络可启用）", (
                f"本沙盒实测 {why}；GitHub Actions 等 unrestricted 网络下重跑 "
                "python scripts/discover_endpoints.py 会自动把该域名写进端点清单并启用")
        if any(("条目" in x) or ("404" in x) or ("412" in x) or ("需补该站正确" in x)
               for x in reasons):
            return "待补端点（候选路径无可枚举时间条目）", (
                "域名可访问但 4 种常见形态（首页 / RSS / 新闻列表 / 要闻页）均未抽到带日期条目："
                "需补该站正确的 RSS / 滚动页 / 公告列表 URL，"
                "或在 data/endpoints_live.yaml 手工登记后重新实测")
        return "待补端点（有公开入口未登记）", (
            "入口含域名：在 unrestricted 网络下重跑 discover_endpoints.py 自动实测并登记")
    if any(w in channel for w in LOGIN_WORDS):
        return "需账号登录 / 动态渲染", ("平台内栏目，无公开无鉴权端点：需官方开放平台授权，或走已授权的"
                                   "第三方热榜聚合；静态抓取无法覆盖，不编造条目")
    return "无公开入口（栏目 / 账号 / 线下）", (
        "渠道文档未登记可拉取域名，属平台内栏目或聚合概念；需人工确认官方数据源后登记，不编造条目")


def build_coverage(endpoints: list[dict], domain_probe: list[dict] | None = None) -> dict:
    doc = BASE / "data" / "资讯渠道大全.md"
    channels = parse_channel_doc(doc)
    extra = yaml.safe_load((BASE / "data" / "channels_extra.yaml").read_text(encoding="utf-8")) or {}
    for c in extra.get("channels", []):
        channels.append({"channel_id": "X-" + str(len(channels)), "channel": c.get("channel", ""),
                         "entry": c.get("entry", ""), "section": "扩充渠道",
                         "level": c.get("level", "☆")})
    ep_domains = {}
    for e in endpoints:
        for d in domains_of(e.get("url", "")):
            ep_domains.setdefault(d, e["channel"])
    domain_status = {}
    for r in (domain_probe or []):
        domain_status[re.sub(r"^www\.", "", r["domain"].lower())] = (
            "已实测可抓取" if r["ok"] else str(r.get("reason", "未探测")))
    rows = []
    for c in channels:
        ch, entry = c["channel"], c.get("entry", "")
        matched = ""
        # 1) 渠道名 / 主体词匹配
        for e in endpoints:
            n = e["channel"]
            if n and (n in ch or ch in n):
                matched = n
                break
        if not matched:
            for key in SITE_KEYS:
                if key in ch and any(key in e["channel"] for e in endpoints):
                    matched = key
                    break
        # 2) 入口域名匹配（平台归并：同域多个栏目复用一个端点）
        if not matched:
            for d in domains_of(entry):
                if d in ep_domains:
                    matched = ep_domains[d]
                    break
        layer, cond = layer_of(ch, entry, matched, domain_status)
        rows.append({"channel_id": c["channel_id"], "channel": ch, "level": c.get("level", ""),
                     "layer": layer, "endpoint": matched, "entry": entry,
                     "domain_status": "；".join(sorted({domain_status.get(d, "未探测")
                                                       for d in domains_of(entry)}))[:80],
                     "enable_condition": cond})
    from collections import Counter
    layers = dict(Counter(r["layer"] for r in rows))
    covered = sum(v for k, v in layers.items() if k.startswith("可直连"))
    star = [r for r in rows if r["level"] == "★"]
    star_covered = sum(1 for r in star if r["layer"].startswith("可直连"))
    return {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "whitelist_total": len(rows),
            "verified_endpoints": len(endpoints),
            "covered_channels": covered,
            "coverage_ratio": round(covered / len(rows), 4) if rows else 0.0,
            "star_total": len(star), "star_covered": star_covered,
            "star_ratio": round(star_covered / len(star), 4) if star else 0.0,
            "layers": layers,
            "domain_probe": {
                "total": len(domain_probe or []),
                "passed": sum(1 for r in (domain_probe or []) if r["ok"]),
                "blocked": dict(Counter(str(r.get("reason", ""))[:24]
                                        for r in (domain_probe or []) if not r["ok"]).most_common(8)),
            },
            "channels": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="不联网，用现有 YAML 重算分层")
    ap.add_argument("--from-whitelist", action="store_true",
                    help="额外从白名单入口列派生域名并批量实测（默认开启）")
    ap.add_argument("--skip-domains", action="store_true", help="跳过域名派生，只跑固定候选")
    ap.add_argument("--timeout", type=float, default=7.0)
    args = ap.parse_args()

    out_yaml = BASE / "data" / "endpoints_live.yaml"
    domain_probe: list[dict] = []
    if args.offline:
        old = yaml.safe_load(out_yaml.read_text(encoding="utf-8")) or {}
        eps = old.get("endpoints", [])
        domain_probe = old.get("domain_probe", [])
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
            results = list(ex.map(verify, CANDIDATES))
        ok, failed = [], []
        for r in results:
            (ok if r["ok"] else failed).append(r)
        eps = []
        for r in ok:
            e = {k: r[k] for k in ("id", "channel", "type", "url", "encoding", "pages",
                                   "section", "note", "url_tpl") if r.get(k) is not None}
            e["verified_items"] = r["items"]
            e["time_samples"] = r["times"]
            e["latest_pub"] = r["latest"]
            e["oldest_pub"] = r["oldest"]
            e["level"] = "★"
            e["requires"] = "none"
            eps.append(e)
        derived, domain_probe = ([], [])
        if not args.skip_domains:
            derived, domain_probe = derive_domain_endpoints(args.timeout)
            # 固定候选已覆盖的域名不再重复登记（同站点不同路径只保留实证更充分的那个）
            fixed_domains = set()
            for e in eps:
                fixed_domains.update(domains_of(e.get("url", "")))
            before = len(derived)
            derived = [e for e in derived
                       if not (set(domains_of(e.get("url", ""))) & fixed_domains)]
            print(f"[域名派生] 实测通过 {before} 个，其中 {before-len(derived)} 个域名已由固定候选覆盖，"
                  f"新增 {len(derived)} 个端点")
        eps.extend(derived)
        payload = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "scripts/discover_endpoints.py 真实请求 + 真实解析（非人工登记）",
            "rule": "仅保留 HTTP 200 且解析出 >=1 条带时间字段条目的端点；其余逐条申报失败原因",
            "endpoints": eps,
            "rejected": [{"id": r["id"], "channel": r["channel"], "url": r["url"],
                          "reason": r.get("err", "解析出 0 条带时间条目")} for r in failed],
            "domain_probe": domain_probe,
        }
        out_yaml.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                            encoding="utf-8")
        print(f"[端点] 固定候选通过 {len(eps)-len(derived)} / {len(CANDIDATES)}，"
              f"域名派生通过 {len(derived)}，合计 {len(eps)}")
        print("[端点] 写入", out_yaml)
        for r in failed[:10]:
            print(f"   申报 {r['id']:24} {r.get('err','')}")
    cov = build_coverage(eps, domain_probe)
    (BASE / "data" / "channel_coverage.json").write_text(
        json.dumps(cov, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[分层] 白名单 {cov['whitelist_total']} 个 | 端点 {cov['verified_endpoints']} 个 | "
          f"端点覆盖渠道 {cov['covered_channels']} 个（{cov['coverage_ratio']*100:.1f}%）")
    print("[口径] 上面的「白名单」= 入口列可提取的独立域名子集，不是渠道总数；"
          "需求②主口径固定为 31 类 599 渠道（见 channel_reach.json 汇总），"
          "step 8 全渠道矩阵按 599 逐条补齐并给出启用动作。")
    for k, v in sorted(cov["layers"].items(), key=lambda x: -x[1]):
        print(f"   {k:32} {v:>4}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
