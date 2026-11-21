"""真实联网采集器：多源发现 → 时间归一化 → 独立原文回查 → 正文补采。

网络受限环境下按「可达性自适应」运行：
  · 国内结构化源组（央视网 JSONP / 中新网 RSS / 人民网 RSS / 站点时间倒排页）
    实测为白名单可达，始终启用；
  · 国际源组（Google News RSS / USGS / GDACS / ReliefWeb / GDELT / BBC）
    先探测，可达才启用；不可达逐源申报「出口策略拦截」，不拖长超时；
  · 有 SERPER/TAVILY key 时由 collectors 的搜索回查继续补。

每条都带真实发布时间（CCTV 秒级 focus_date / RSS RFC822 +0800），
窗口过滤、去重、分类、评分、摘要、出表全部复用主流水线，不编造条目。
"""
from __future__ import annotations

import html as _html
import json
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import quote, urljoin
from zoneinfo import ZoneInfo

import yaml

TZ_CN = ZoneInfo("Asia/Shanghai")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 news-daily/2.0"

# 国内源组：实测白名单可达
CN_FEEDS: List[dict] = [
    {"id": "CN-CCTV-JSON", "name": "央视网新闻", "type": "cctv_jsonp", "pages": 7,
     "url_tpl": "https://news.cctv.com/2019/07/gaiban/cmsdatainterface/page/news_{i}.jsonp?cb=news",
     "section": "中央媒体 / 结构化时间倒排", "level": "★"},
    {"id": "CN-CNS-SCROLL", "name": "中新网滚动", "type": "rss",
     "url": "https://www.chinanews.com.cn/rss/scroll-news.xml",
     "section": "中央媒体 / RSS", "level": "★"},
    {"id": "CN-CNS-FIN", "name": "中新网财经", "type": "rss",
     "url": "https://www.chinanews.com.cn/rss/finance.xml",
     "section": "中央媒体 / RSS", "level": "★"},
    {"id": "CN-CNS-HEALTH", "name": "中新网健康", "type": "rss",
     "url": "https://www.chinanews.com.cn/rss/health.xml",
     "section": "中央媒体 / RSS", "level": "★"},
    {"id": "CN-PEOPLE-POL", "name": "人民网时政", "type": "rss",
     "url": "http://www.people.com.cn/rss/politics.xml",
     "section": "中央媒体 / RSS", "level": "★"},
    {"id": "CN-PEOPLE-FIN", "name": "人民网财经", "type": "rss",
     "url": "http://www.people.com.cn/rss/finance.xml",
     "section": "中央媒体 / RSS", "level": "★"},
    {"id": "CN-PEOPLE-SOC", "name": "人民网社会", "type": "rss",
     "url": "http://www.people.com.cn/rss/society.xml",
     "section": "中央媒体 / RSS", "level": "★"},
    {"id": "CN-CNS-ROLL", "name": "中新网滚动页", "type": "html_cns",
     "url": "https://www.chinanews.com.cn/scroll-news/news1.html",
     "section": "中央媒体 / 站内时间倒排", "level": "★"},
    {"id": "CN-CCTV-HOME", "name": "央视网首页", "type": "html_cctv",
     "url": "https://news.cctv.com/", "section": "中央媒体 / 站内时间倒排", "level": "★"},
    {"id": "CN-PEOPLE-HOME", "name": "人民网首页", "type": "html_people",
     "url": "http://www.people.com.cn/", "section": "中央媒体 / 站内时间倒排", "level": "★"},
    {"id": "CN-GOV-YAOWEN", "name": "中国政府网要闻", "type": "html_gov",
     "url": "https://www.gov.cn/yaowen/liebiao/",
     "section": "部委与政务 / 站内列表", "level": "★"},
]

# 国际源组：GitHub Actions 等 unrestricted 网络可达；沙盒按实测申报
INTL_FEEDS: List[dict] = [
    {"id": "INTL-GNEWS", "name": "Google News RSS", "type": "rss",
     "url": "https://news.google.com/rss/search?q=%E8%A6%81%E9%97%BB%20when:1d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
     "section": "聚合 / RSS", "level": "★", "probe": True},
    {"id": "INTL-USGS", "name": "USGS 显著地震", "type": "usgs",
     "url": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_day.geojson",
     "section": "灾害 / 官方 API", "level": "★", "probe": True},
    {"id": "INTL-GDACS", "name": "GDACS 灾害预警", "type": "rss",
     "url": "https://www.gdacs.org/xml/rss.xml", "section": "灾害 / 官方 RSS", "level": "★", "probe": True},
    {"id": "INTL-RELIEF", "name": "ReliefWeb 灾害报告", "type": "relief",
     "url": "https://api.reliefweb.int/v1/reports?appname=news-daily&limit=40",
     "section": "灾害 / 官方 API", "level": "★", "probe": True},
    {"id": "INTL-GDELT", "name": "GDELT DOC 2.0", "type": "gdelt",
     "url": "https://api.gdeltproject.org/api/v2/doc/doc?query=china&mode=artlist&format=json&maxrecords=60",
     "section": "聚合 / 官方 API", "level": "☆", "probe": True},
    {"id": "INTL-BBC", "name": "BBC News RSS", "type": "rss",
     "url": "https://feeds.bbci.co.uk/news/rss.xml", "section": "国际媒体 / RSS", "level": "☆", "probe": True},
]

PROBE_URL = "https://news.google.com/rss/search?q=news%20when:1d&hl=en-US&gl=US&ceid=US:en"

# 真实源条目没有台账里的 group/tag 原值，按可核对主题词给分类提示；
# 最终仍由 rules.classify 校准。刻意不预置「国际动态」——境外常规会议/外交
# 会被国际三类门禁误杀，保持民生服务反而更诚实。
TOPIC_HINTS = {
    "重大突发与司法": ["受贿案", "一审宣判", "判处", "提起公诉", "判决", "事故", "遇难",
                 "伤亡", "火灾", "级地震", "坠机", "爆炸", "失联", "查处"],
    "重要赛事": ["世界杯", "亚运会", "奥运", "决赛", "夺冠", "联赛", "锦标赛"],
    "文娱与热榜": ["电影", "综艺", "演唱会", "热搜榜", "电视剧", "春晚"],
    "科技与产业": ["AI", "人工智能", "芯片", "算力", "大模型", "半导体", "卫星", "航天",
               "超算", "无人机", "机器人", "新能源", "动力电池", "光伏", "数据跨境",
               "5G", "量子", "科创", "数字化", "自研"],
    "财经市场": ["REITs", "A股", "沪指", "创业板", "专项债", "地方债", "央行", "证监会",
             "基金", "退市", "破产", "重整", "财报", "营收", "股价", "交易所", "证券",
             "黄金", "金价", "汇率", "CPI", "GDP", "融资", "上市", "税收", "保险", "银行"],
    "社会治理与养老": ["中考", "高考", "校园", "教师", "班主任", "养老", "社保", "医保",
                 "教育", "未成年人", "社区治理", "条例", "实施办法", "征求意见"],
}


def suggest_group(title: str, abstract: str = "") -> str:
    text = (title or "") + " " + (abstract or "")[:200]
    for g, words in TOPIC_HINTS.items():
        if any(w in text for w in words):
            return g
    return ""
BODY_HOSTS = ("news.cctv.com", "www.chinanews.com.cn", "politics.people.com.cn",
              "finance.people.com.cn", "society.people.com.cn", "www.people.com.cn",
              "www.xinhuanet.com", "www.news.cn", "www.gmw.cn", "www.ce.cn",
              "www.cyol.com", "www.gov.cn", "www.mee.gov.cn", "www.ndrc.gov.cn",
              "www.miit.gov.cn", "www.pbc.gov.cn", "www.mofcom.gov.cn", "www.csrc.gov.cn")


# 2026-11-22 事故根因之二：live_cn._get 完全不看全局预算，失败还会走
# reach.get_with_matrix 的 8 组指纹重试（注释自己写了「把整批拖到数分钟」）。
# Collector.run 的 _deadline_at 只保护 collectors._get（requests），管不到这里。
# 到点后停止发新请求，让 fetch_cn_sources 带着**已抓到的部分条目**正常返回。
_BUDGET_DEADLINE = [0.0]


def _budget_done() -> bool:
    d = _BUDGET_DEADLINE[0]
    return bool(d and time.monotonic() > d)


def _get(url: str, timeout: float = 8.0, encoding: str | None = None,
         matrix: bool = True) -> str:
    """默认指纹失败自动换请求指纹 / 协议重试（reach.py 全渠道补齐链第 2 步）。

    `matrix=False` 用于正文补采：指纹矩阵是给「渠道可达性探测」设计的（需 8 组
    UA/协议逐一试），正文补采是高频并发请求，单 URL 失败就该立即留空，否则一个
    挂住的站点会串行试 8 组 × timeout，把整批拖到数分钟（实测根因）。
    """
    if _budget_done():
        return ""              # 预算到点：不再发新请求，保留已有条目
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as f:
            raw = f.read()
            cs = encoding or f.headers.get_content_charset() or "utf-8"
        return raw.decode(cs, "replace")
    except Exception:
        if _budget_done() or not matrix:
            return ""          # 到点 / 正文补采失败：如实留空，不做 8 组指纹重试
        from .reach import get_with_matrix
        text = get_with_matrix(url, timeout)
        if encoding:
            try:
                return text.encode("utf-8", "replace").decode(encoding, "replace")
            except Exception:
                pass
        return text


def _clean(s: str) -> str:
    from .article import clean_html
    return re.sub(r"\s+", " ", clean_html(s)).strip()


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=TZ_CN) if dt.tzinfo is None else dt.astimezone(TZ_CN)


def _parse_time(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S"):
        try:
            return _aware(datetime.strptime(s, fmt))
        except ValueError:
            pass
    try:
        return _aware(parsedate_to_datetime(s))
    except Exception:
        return None


SOURCE_ALIAS = {
    "news.qq.com": "腾讯新闻", "www.qq.com": "腾讯新闻", "qq.com": "腾讯新闻",
    "www.xinhuanet.com": "新华网", "news.cn": "新华网", "www.gmw.cn": "光明网",
    "www.ce.cn": "中国经济网", "www.chinanews.com.cn": "中新网",
    "www.people.com.cn": "人民网", "news.cctv.com": "央视网新闻",
}


def _spec(*, feed: dict, title: str, abstract: str, url: str, pub: datetime | None,
          pub_raw: str, basis: str, evidence: str = "A", shape: str = "独立原文",
          record_type: str = "独立原文", **extra) -> dict:
    # 来源列只写可核对的媒体名，不写域名（01 第三/六节）
    src_name = SOURCE_ALIAS.get(str(feed["name"]), feed["name"])
    d = {
        "title": title, "abstract": abstract, "source": src_name, "url": url,
        "pub": pub, "pub_raw": pub_raw, "basis": basis,
        "mark": "窗口内实证" if pub else "时点推定", "evidence": evidence,
        "shape": shape, "has_event": True, "record_type": record_type,
        "group": "", "tag": "", "raw": {"feed_id": feed["id"], "feed_name": feed["name"]},
    }
    d.update(extra)
    return d


def _cctv_jsonp(feed: dict, win, timeout: float) -> Tuple[List[dict], dict]:
    out, pages, errors = [], [], []
    for i in range(1, int(feed.get("pages", 7)) + 1):
        u = feed["url_tpl"].format(i=i)
        try:
            t = _get(u, timeout)
            j = json.loads(t[t.index("(") + 1: t.rindex(")")])
            lst = j.get("data", {}).get("list", [])
            if not lst:
                break
            dates = []
            for x in lst:
                pub = _parse_time(str(x.get("focus_date") or ""))
                if pub:
                    dates.append(pub)
                out.append(_spec(
                    feed=feed, title=str(x.get("title") or ""),
                    abstract=_clean(str(x.get("brief") or "")),
                    url=str(x.get("url") or ""), pub=pub,
                    pub_raw=str(x.get("focus_date") or "未取得"),
                    basis="央视网 JSONP focus_date（秒级，实证）" if pub else "央视网未提供 focus_date",
                    record_type="联网检索独立原文"))
            oldest = min(dates) if dates else None
            pages.append({"页": i, "条数": len(lst), "最旧": str(oldest) if oldest else "—"})
            # 已翻到远早于窗口左界，停止翻页（避免无谓请求）
            if oldest and (win.start - oldest).total_seconds() > 36 * 3600:
                break
        except urllib.error.HTTPError as e:
            errors.append(f"page{i} HTTP {e.code}")
            break
        except Exception as e:
            errors.append(f"page{i} {type(e).__name__}: {str(e)[:70]}")
            break
    return out, {"分页": pages, "错误": errors}


def _rss(feed: dict, win, timeout: float, encoding: str | None = None) -> Tuple[List[dict], dict]:
    t = _get(feed["url"], timeout, encoding)
    root = ET.fromstring(t)
    items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    out, stale = [], 0
    for x in items:
        def txt(tag):
            el = x.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        title = _clean(txt("title"))
        if not title:
            continue
        link = txt("link") or (x.find("link").get("href") if x.find("link") is not None else "")
        raw_date = txt("pubDate") or txt("published") or txt("updated") or txt("date")
        pub = _parse_time(raw_date)
        desc = _clean(txt("description") or txt("summary") or txt("content"))
        if pub and (win.end - pub).days > 180:
            stale += 1
        out.append(_spec(
            feed=feed, title=title, abstract=desc, url=link, pub=pub,
            pub_raw=raw_date or "未取得",
            basis=(f"RSS pubDate（{feed['name']}，实证）" if pub else "RSS 无 pubDate"),
            record_type="联网检索独立原文"))
    return out, {"条目": len(out), "往年旧页（交由窗口过滤剔除）": stale}


def _html_cns(feed: dict, win, timeout: float) -> Tuple[List[dict], dict]:
    t = _get(feed["url"], timeout, "utf-8")
    blocks = re.findall(r"<li>(.*?)</li>", t, re.S)
    seen, out, with_time = set(), [], 0
    for b in blocks:
        a = re.search(r'href="(/(?:[a-z]+/)*2026/11-1[78]/\d+\.shtml)"[^>]*>([^<]{6,90})</a>', b)
        if not a:
            continue
        path, title = a.group(1), a.group(2)
        u = "https://www.chinanews.com.cn" + path
        if u in seen:
            continue
        seen.add(u)
        tm = re.search(r'class="dd_time"[^>]*>\s*(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})', b)
        if tm:
            mo, da, hh, mi = map(int, tm.groups())
            pub = _aware(datetime(2026, mo, da, hh, mi))
            pub_raw, basis = pub.strftime("%Y-%m-%d %H:%M"), "中新网滚动页 dd_time（分钟级，实证）"
            with_time += 1
        else:
            m = re.search(r"/2026/11-(\d{2})/", u)
            pub = _aware(datetime(2026, 11, int(m.group(1)), 12, 0)) if m else None
            pub_raw = f"2026-11-{m.group(1)}（站内日期线索，时点推定）" if m else "未取得"
            basis = "站内时间倒排页日期线索（时点推定，需回查原文）"
        out.append(_spec(feed=feed, title=_clean(title), abstract="", url=u, pub=pub,
                         pub_raw=pub_raw, basis=basis, evidence="B",
                         record_type="站内时间倒排"))
    return out, {"li 块": len(blocks), "去重后": len(out), "分钟级实证时间": with_time}


def _html_cctv(feed: dict, win, timeout: float) -> Tuple[List[dict], dict]:
    t = _get(feed["url"], timeout)
    rows = re.findall(r'href="(https?://news\.cctv\.com/2026/11/\d{2}/ARTI\w+\.shtml)"[^>]*>([^<]{6,90})</a>', t)
    seen, out = set(), []
    for u, title in rows:
        if u in seen:
            continue
        seen.add(u)
        m = re.search(r"/2026/11/(\d{2})/", u)
        pub = _aware(datetime(2026, 11, int(m.group(1)), 12, 0)) if m else None
        out.append(_spec(feed=feed, title=_clean(title), abstract="", url=u, pub=pub,
                         pub_raw=f"2026-11-{m.group(1)}（站内日期线索，时点推定）" if m else "未取得",
                         basis="央视网首页时间倒排（时点推定，需回查原文）",
                         evidence="B", record_type="站内时间倒排"))
    return out, {"链接": len(rows), "去重后": len(out)}


def _html_people(feed: dict, win, timeout: float) -> Tuple[List[dict], dict]:
    t = _get(feed["url"], timeout)
    rows = re.findall(r'href="(http://(?:politics|finance|society)\.people\.com\.cn/n1/2026/\d{4}/[^"]+\.html)"[^>]*>([^<]{6,90})</a>', t)
    rows += [(u, ti) for u, ti in re.findall(r'href="(/n1/2026/\d{4}/[^"]+\.html)"[^>]*>([^<]{6,90})</a>', t)]
    seen, out = set(), []
    for u, title in rows:
        if u.startswith("/"):
            u = "http://www.people.com.cn" + u
        if u in seen:
            continue
        seen.add(u)
        m = re.search(r"/n1/(\d{4})/(\d{2})(\d{2})/", u)
        pub = _aware(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 12, 0)) if m else None
        out.append(_spec(feed=feed, title=_clean(title), abstract="", url=u, pub=pub,
                         pub_raw=(f"{m.group(1)}-{m.group(2)}-{m.group(3)}（站内日期线索，时点推定）" if m else "未取得"),
                         basis="人民网首页时间倒排（时点推定，需回查原文）",
                         evidence="B", record_type="站内时间倒排"))
    return out, {"链接": len(rows), "去重后": len(out)}


def _html_gov(feed: dict, win, timeout: float) -> Tuple[List[dict], dict]:
    t = _get(feed["url"], timeout)
    # 中国政府网要闻为 JS 动态渲染（listArrP），静态页只有导航链接；如实申报
    dyn = "listArrP" in t or "listArr" in t
    return [], {"静态可解析条目": 0, "动态渲染": dyn,
                "说明": "要闻列表由 JS 动态加载，静态 HTML 无条目；不编造"}


def _usgs(feed: dict, win, timeout: float) -> Tuple[List[dict], dict]:
    t = _get(feed["url"], timeout)
    j = json.loads(t)
    out = []
    for f in j.get("features", []):
        p, pr = f.get("properties", {}), {}
        dt = datetime.fromtimestamp((p.get("time") or 0) / 1000, TZ_CN)
        out.append(_spec(feed=feed, title=_clean(p.get("title") or ""), abstract=_clean(p.get("place") or ""),
                         url=str(p.get("url") or ""), pub=dt, pub_raw=dt.strftime("%Y-%m-%d %H:%M:%S"),
                         basis="USGS GeoJSON properties.time（毫秒时间戳，实证）",
                         record_type="官方 API"))
    return out, {"features": len(out)}


def _relief(feed: dict, win, timeout: float) -> Tuple[List[dict], dict]:
    t = _get(feed["url"], timeout)
    j = json.loads(t)
    out = []
    for d in j.get("data", []):
        f = d.get("fields", {})
        pub = _parse_time(str(f.get("date", {}).get("created") or ""))
        out.append(_spec(feed=feed, title=_clean(f.get("title") or ""),
                         abstract=_clean(str(f.get("body") or "")[:600]),
                         url=str(f.get("url") or ""), pub=pub,
                         pub_raw=str(f.get("date", {}).get("created") or "未取得"),
                         basis="ReliefWeb date.created（实证）" if pub else "ReliefWeb 无时间字段",
                         record_type="官方 API"))
    return out, {"条目": len(out)}


def _gdelt(feed: dict, win, timeout: float) -> Tuple[List[dict], dict]:
    t = _get(feed["url"], timeout)
    j = json.loads(t)
    out = []
    for a in j.get("articles", []):
        pub = _parse_time(str(a.get("seendate") or "").replace("T", " "))
        out.append(_spec(feed=feed, title=_clean(a.get("title") or ""),
                         abstract=_clean(a.get("excerpt") or ""), url=str(a.get("url") or ""),
                         pub=pub, pub_raw=str(a.get("seendate") or "未取得"),
                         basis="GDELT seendate（实证）" if pub else "GDELT 无 seendate",
                         evidence="B", record_type="聚合 API"))
    return out, {"条目": len(out)}


def _html_generic(feed: dict, win, timeout: float) -> Tuple[List[dict], dict]:
    """白名单入口派生的站内时间倒排页：抽带日期的 <a>，相对链接补全为绝对 URL。"""
    t = _get(feed["url"], timeout, feed.get("encoding"))
    DATE = re.compile(r"/(20\d{2})[-/](\d{1,2})[-/](\d{1,2})/|(20\d{2})(\d{2})(\d{2})")
    JUNK = re.compile(r"备案号|版权所有|京公网安|网站地图|联系我们|无障碍|Copyright|"
                      r"登录|注册|客户端|下载APP|扫一扫|返回顶部|更多>>")
    seen, out, with_time = set(), [], 0
    for m in re.finditer(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", t, re.S | re.I):
        href, title = m.group(1), _clean(m.group(2))
        if len(title) < 10 or len(title) > 120 or JUNK.search(title):
            continue
        u = urljoin(feed["url"], href)
        if u in seen:
            continue
        seen.add(u)
        seg = t[m.end():m.end() + 180]
        dm = DATE.search(href) or re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", seg)
        if dm:
            g = [x for x in dm.groups() if x]
            try:
                pub = _aware(datetime(int(g[0]), int(g[1]), int(g[2]), 12, 0))
                pub_raw = f"{int(g[0]):04d}-{int(g[1]):02d}-{int(g[2]):02d}（站内日期线索，时点推定）"
                basis = "站内时间倒排页日期线索（时点推定，需回查原文）"
                with_time += 1
            except Exception:
                pub, pub_raw, basis = None, "未取得", "站内日期线索解析失败"
        else:
            pub, pub_raw, basis = None, "未取得", "无日期线索（交由窗口过滤 / 如实留痕）"
        out.append(_spec(feed=feed, title=title, abstract="", url=u, pub=pub,
                         pub_raw=pub_raw, basis=basis, evidence="B",
                         record_type="站内时间倒排"))
    # 带日期的优先补正文；单源限量，避免几百个导航链接吃光补采配额。
    out.sort(key=lambda x: 0 if x.get("pub") else 1)
    kept = out[:30]
    return kept, {"链接去重": len(seen), "带日期": with_time,
                  "无日期线索": len(kept) - sum(1 for x in kept if x.get("pub")),
                  "单源限量": 30, "截断前": len(out)}


PARSERS = {
    "cctv_jsonp": _cctv_jsonp, "rss": _rss, "html_cns": _html_cns,
    "html_cctv": _html_cctv, "html_people": _html_people, "html_gov": _html_gov,
    "html_generic": _html_generic,
    "usgs": _usgs, "relief": _relief, "gdelt": _gdelt,
}


def load_endpoints(cfg: dict) -> Tuple[List[dict], dict]:
    """加载 data/endpoints_live.yaml（由 discover_endpoints.py 实测生成）。

    只使用「真实请求 + 真实解析出带时间条目」的端点；文件不存在时回退内置 11 个源。
    """
    path = Path(__file__).resolve().parents[1] / "data" / "endpoints_live.yaml"
    if not path.exists():
        return list(CN_FEEDS), {"来源": "内置 11 源回退", "文件": "未生成"}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    eps = data.get("endpoints", []) or []
    # 合并 reach.py 指纹矩阵实证的端点（部委站 / 移动版 / http 降级等）
    reach_path = Path(__file__).resolve().parents[1] / "data" / "endpoints_reach.yaml"
    reach_n = 0
    if reach_path.exists():
        d2 = yaml.safe_load(reach_path.read_text(encoding="utf-8")) or {}
        extra = d2.get("endpoints", []) or []
        known = {str(e.get("url")) for e in eps}
        for e in extra:
            if str(e.get("url")) not in known:
                eps.append(e)
                reach_n += 1
    feeds = []
    for e in eps:
        t = e.get("type")
        if t == "jsonp":          # discover_endpoints.py 写的是简名，解析器注册键是 cctv_jsonp
            t = "cctv_jsonp"
        if t not in PARSERS:
            continue
        f = {"id": e["id"], "name": e.get("channel") or e["id"],
             "type": t, "url": e.get("url", ""),
             "url_tpl": e.get("url_tpl") or e.get("url", ""),
             "pages": int(e.get("pages", 1)),
             "encoding": e.get("encoding"),
             "section": e.get("section", "真实联网源"), "level": e.get("level", "★"),
             "verified_items": e.get("verified_items", 0),
             "latest_pub": e.get("latest_pub", ""), "derived": bool(e.get("derived"))}
        feeds.append(f)
    meta = {"来源": str(path.relative_to(path.parents[1])),
            "生成时间": data.get("generated_at", ""),
            "规则": data.get("rule", ""),
            "登记端点数": len(eps), "启用端点数": len(feeds),
            "指纹矩阵实证新增": reach_n,
            "申报未通过数": len(data.get("rejected", []) or [])}
    return feeds, meta

# ---------------- 独立原文正文补采 ----------------

def fetch_body(url: str, timeout: float = 6.0) -> str:
    # 不再限定 BODY_HOSTS：指纹矩阵实证的部委站 / 行业站 / 地方站同样要补正文，
    # 否则这些渠道虽抓到链接，却因摘要为空被字段校验剔除，主表来源只剩两三家。
    # 仍保留下方「无电头时 ≥2 段且 ≥120 字」与串标题过滤，防列表页冒充正文。
    try:
        t = _get(url, timeout, matrix=False)
    except Exception:
        return ""
    if not t:
        return ""
    ps = re.findall(r"<p[^>]*>(.*?)</p>", t, re.S)
    segs = []
    for p in ps:
        c = _html.unescape(re.sub(r"<[^>]+>", "", p)).strip()
        c = re.sub(r"#(\d{2,5});", lambda m: chr(int(m.group(1))), c)
        c = re.sub(r"#x([0-9a-fA-F]{2,4});", lambda m: chr(int(m.group(1), 16)), c)
        c = re.sub(r"\s+", " ", c)
        if len(c) < 15 or re.search(r"版权|责任编辑|扫一扫|扫描|关注我们|相关新闻|"
                                    r"点击进入|推荐阅读|上一篇|下一篇|原标题", c):
            continue
        wire = bool(re.search(r"中新网.{0,12}电|中新社|央视网消息|新华社|记者\s", c[:50]))
        if not wire:
            # 图片/聚合频道页把「相关新闻」标题塞进 <p>：无句读或疑问/感叹收尾的一律剔除
            if not re.search(r"[，,。；;]", c) or c.rstrip().endswith(("？", "?", "！", "!")):
                continue
        segs.append(c)
    has_wire = any(re.search(r"中新网.{0,12}电|中新社|央视网消息|新华社|记者\s", s[:50])
                   for s in segs)
    # 无电头时按「段落数 + 总字数」判列表/聚合页。部委公告、行业媒体稿常无电头，
    # 之前要求 ≥3 段且 ≥200 字过严，导致这些源补采全失败、条目被字段校验剔除。
    # 放宽到 ≥2 段且 ≥120 字；「无句读短段落剔除」仍在前置循环里防串标题。
    if not has_wire and not (len(segs) >= 2 and sum(map(len, segs)) >= 120):
        return ""
    seen, out = set(), []
    for s in segs:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return "\n".join(out[:6])


def enrich_bodies(specs: List[dict], win, cfg: dict) -> dict:
    """只给窗口内、缺摘要或摘要过短的条目回查独立原文正文（并发、限量）。

    配额按「来源轮询」分配，而不是 specs 顺序截断：之前 CCTV/中新网前三个源会
    独占全部名额，导致新华网、光明网、部委站等源虽抓到窗口内条目却补不到正文、
    被字段校验剔除，主表来源只剩 3 个。每源先取 per_source 条，余量再按序补齐。
    """
    live = (cfg.get("source", {}).get("live", {}) or {})
    if not live.get("fetch_article", True):
        return {"启用": False}
    cap = int(live.get("max_articles", 60))
    timeout = float(live.get("article_timeout", 6))
    pool = [s for s in specs
            if s.get("url")
            and (s.get("pub") is None or win.contains(s["pub"]))
            and len(re.sub(r"\s+", "", s.get("abstract") or "")) < 60]
    by_source: Dict[str, List[dict]] = {}
    for s in pool:
        key = str(s.get("raw", {}).get("feed_id") or s.get("source") or "?")
        by_source.setdefault(key, []).append(s)
    n_src = max(1, len(by_source))
    per_source = max(2, cap // n_src)
    todo, used = [], set()
    # 第一轮：每个源各取 per_source 条
    for key, lst in by_source.items():
        for s in lst[:per_source]:
            todo.append(s)
            used.add(id(s))
    # 第二轮：余下配额按原始顺序补齐（优先仍缺摘要的源）
    if len(todo) < cap:
        for s in pool:
            if len(todo) >= cap:
                break
            if id(s) not in used:
                todo.append(s)
                used.add(id(s))
    todo = todo[:cap]
    done, failed = 0, 0
    per_source_ok: Dict[str, int] = {}
    deadline = float(live.get("body_deadline_seconds", 75))
    timed_out = 0
    if todo:
        from concurrent.futures import wait, FIRST_COMPLETED
        ex = ThreadPoolExecutor(max_workers=max(1, int(live.get("body_workers", 8))))
        try:
            futs = {ex.submit(fetch_body, s["url"], timeout): s for s in todo}
            pending = set(futs)
            t0 = time.time()
            # 整批硬上限：某个源站 socket 读挂住时不让整批被拖死；
            # 超时任务取消并如实计「失败」，绝不编正文。
            while pending and (time.time() - t0) < deadline:
                fin, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                for fu in fin:
                    s = futs[fu]
                    try:
                        body = fu.result()
                    except Exception:
                        body = ""
                    if body:
                        s["abstract"] = body
                        s["raw"]["body_fetched"] = True
                        s["basis"] = s.get("basis", "") + " / 独立原文正文补采"
                        done += 1
                        k = str(s.get("raw", {}).get("feed_id") or s.get("source") or "?")
                        per_source_ok[k] = per_source_ok.get(k, 0) + 1
                    else:
                        failed += 1
            for fu in pending:
                fu.cancel()
                timed_out += 1
                failed += 1
        finally:
            # 不能用 with：`__exit__` 会 shutdown(wait=True) 把主流程重新阻塞在
            # 挂住的线程上，前面的 deadline 就白设了。
            ex.shutdown(wait=False, cancel_futures=True)
    return {"启用": True, "候选": len(todo), "正文补采成功": done,
            "补采失败（如实留空）": failed, "批量超时取消": timed_out,
            "批量时限(秒)": deadline, "并发": 8,
            "参与来源数": len(by_source), "每源基础配额": per_source,
            "逐源补采成功": per_source_ok}


# ---------------- 社交平台热榜（✗ 仅作热度参考，不进要闻主表） ----------------
# 渠道大全 v3 §14：微博 / 抖音等社交平台「仅作热度参考，默认不入池」。
# 热搜词条不是新闻，上榜时间不是资讯发布时间；本层只做原样快照 + 逐路径可达性申报，
# 不进 specs（因此不进候选池 / 不参与评级 / 不影响主表与质量门禁）。
# 若要把某个词条变成要闻，必须走 search 回查到带事实的独立原文（collectors.collect_hotboards）。
WEIBO_BAND_URL = "https://weibo.com/ajax/statuses/hot_band"
WEIBO_SIDE_URL = "https://weibo.com/ajax/side/hotSearch"
DOUYIN_PATHS = [
    "https://www.douyin.com/aweme/v1/web/hot/search/list/?device_platform=webapp&aid=6383",
    "https://www.douyin.com/aweme/v1/web/hot/search/list/",
    "https://www.douyin.com/aweme/v1/web/hot/search/billboard/",
    "https://www.douyin.com/hot",
    "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/",
    "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/aweme/",
    "https://aweme.snssdk.com/aweme/v1/hot/search/list/",
    "https://api.amemv.com/aweme/v1/hot/search/list/",
    "https://douhot.douyin.com/",
    "https://m.douyin.com/",
]
SOCIAL_OTHER = [
    ("百度热搜", "https://top.baidu.com/board?tab=realtime"),
    ("知乎热榜", "https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total?limit=50"),
    ("B站热门", "https://api.bilibili.com/x/web-interface/popular?ps=20"),
    ("今日头条热榜", "https://www.toutiao.com/hot-event/hot-board/"),
    ("快手", "https://www.kuaishou.com/?isHome=1"),
    ("小红书", "https://www.xiaohongshu.com/explore"),
]
_SOCIAL_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120 Safari/537.36"),
    "Accept": "application/json,text/html,*/*;q=0.8",
    "Referer": "https://weibo.com/",
}


def _get_social(url: str, timeout: float) -> str:
    try:
        req = urllib.request.Request(url, headers=_SOCIAL_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as f:
            return f.read().decode(f.headers.get_content_charset() or "utf-8", "replace")
    except Exception:
        from .reach import get_with_matrix
        return get_with_matrix(url, timeout)


def _probe_url(url: str, timeout: float) -> dict:
    try:
        t = _get_social(url, timeout)
        return {"url": url, "code": 200, "bytes": len(t)}
    except urllib.error.HTTPError as e:
        return {"url": url, "code": e.code}
    except Exception as e:
        return {"url": url, "code": type(e).__name__}


def collect_social_hotboards(cfg: dict, win, timeout: float = 7.0) -> dict:
    """逐平台真实探测；微博可达则出完整热搜快照，其余逐路径申报失败原因。"""
    live = (cfg.get("source", {}).get("live", {}) or {})
    if not live.get("social_hotboards", True):
        return {"启用": False, "快照": [], "逐平台": [], "抖音逐路径实测": [],
                "其它平台对照": [], "口径": "config 关闭 social_hotboards"}
    rows: List[dict] = []
    platforms: List[dict] = []
    # ---- 微博：官方隐藏 JSON，实测无需登录，带 onboard_time 真实上榜时间戳 ----
    try:
        j = json.loads(_get_social(WEIBO_BAND_URL, timeout))
        bl = (j.get("data", {}) or {}).get("band_list", []) or []
        for x in bl:
            ts = x.get("onboard_time")
            pub = datetime.fromtimestamp(int(ts), TZ_CN) if ts else None
            word = str(x.get("word") or x.get("note") or "").strip()
            if not word:
                continue
            scheme = str(x.get("word_scheme") or word)
            rows.append({
                "排名": int(x.get("realpos") or (int(x.get("rank") or 0) + 1) or len(rows) + 1),
                "平台": "微博", "词条": word,
                "热度值": int(x.get("num") or 0),
                "分类": str(x.get("category") or ""),
                "标签": str(x.get("label_name") or x.get("icon_desc") or ""),
                "上榜时间": pub.strftime("%Y-%m-%d %H:%M:%S") if pub else "未取得",
                "窗口内上榜": "是" if (pub and win.contains(pub)) else "否",
                "事件线索": str(x.get("subject_querys") or ""),
                "榜单链接": "https://s.weibo.com/weibo?q=" + quote(scheme),
                "快照来源": WEIBO_BAND_URL,
            })
        # 侧栏接口只做冗余确认，不重复列入快照
        try:
            j2 = json.loads(_get_social(WEIBO_SIDE_URL, timeout))
            side_n = len((j2.get("data", {}) or {}).get("realtime", []) or [])
        except Exception:
            side_n = 0
        platforms.append({"平台": "微博", "状态": "已触达", "条目数": len(rows),
                          "端点": "weibo.com/ajax/statuses/hot_band（官方 JSON，无鉴权）",
                          "说明": f"完整快照 {len(rows)} 条，带秒级上榜时间/热度值/分类；"
                                  f"侧栏接口冗余确认 {side_n} 条。"
                                  "按需求②登记为社交主干发现入口；词条须逐条回查独立原文才成行（social_main.py）"})
    except Exception as e:
        platforms.append({"平台": "微博", "状态": "未取得", "条目数": 0,
                          "端点": WEIBO_BAND_URL,
                          "说明": f"{type(e).__name__}: {str(e)[:90]}"})
    # ---- 抖音：10 个官方/历史路径逐个实测，全部失败则如实申报 ----
    dy = [_probe_url(u, min(timeout, 6)) for u in DOUYIN_PATHS]
    dy_ok = [r for r in dy if r["code"] == 200]
    platforms.append({"平台": "抖音", "状态": "已触达" if dy_ok else "未取得",
                      "条目数": 0, "端点": f"{len(DOUYIN_PATHS)} 个官方/历史路径",
                      "说明": ("本环境全部被拒（域名级 403 / 需 X-Bogus + a_bogus 签名 + 设备指纹）；"
                               "逐路径状态见附录 23b。合规启用路径：抖音开放平台「热门视频/话题榜单」"
                               "能力申请，或授权第三方热榜 API；unrestricted 网络下本脚本会自动重测")})
    # ---- 其它社交/热榜平台对照 ----
    other = []
    for name, u in SOCIAL_OTHER:
        r = _probe_url(u, min(timeout, 6))
        other.append({"平台": name, "端点": u, "状态": r["code"],
                      "结论": "可达" if r["code"] == 200 else "本环境被拒 / 需鉴权"})
    return {
        "启用": True,
        "口径": ("需求②：微博 / 百度 / 抖音 / 头条 / 知乎 / B站 / 小红书 / 快手 / 贴吧 / "
                 "微信搜一搜是主干发现入口，不是补充、也不是仅作热度参考。"
                 "但热搜词条不是新闻，onboard_time 是上榜时间而非资讯发布时间："
                 "词条必须逐条回查到带事实的独立原文才成行，只有话题名+热度进附录 15；"
                 "快照本身不进候选池 / 主表、不吃 snippet（social_main.py 实现）。"),
        "快照": rows,
        "快照条数": len(rows),
        "窗口内上榜": sum(1 for r in rows if r["窗口内上榜"] == "是"),
        "逐平台": platforms,
        "抖音逐路径实测": dy,
        "其它平台对照": other,
    }


# ---------------- 主入口 ----------------

def probe_intl(timeout: float = 4.0) -> dict:
    try:
        t = _get(PROBE_URL, timeout)
        return {"ok": "<rss" in t or "<item" in t, "reason": "国际源组可达" if "<rss" in t else "响应非 RSS"}
    except urllib.error.HTTPError as e:
        return {"ok": False, "reason": f"探测端点 HTTP {e.code}（出口策略 / 反爬）"}
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {str(e)[:80]}"}


def fetch_cn_sources(cfg: dict, win) -> Tuple[List[dict], dict]:
    live = (cfg.get("source", {}).get("live", {}) or {})
    timeout = float(live.get("request_timeout", 8))
    _BUDGET_DEADLINE[0] = float((cfg.get("budget") or {}).get("_deadline_at") or 0)
    probe = probe_intl(float(live.get("probe_timeout", 3.0)))
    feeds, ep_meta = load_endpoints(cfg)
    # 国际源组：清单里已验证通过的直接用；未通过但本次探测可达时补上（unrestricted 网络）
    intl_ids = {f["id"] for f in feeds if str(f["id"]).startswith("INTL-")}
    extra_intl = ([f for f in INTL_FEEDS if f["id"] not in intl_ids]
                  if probe.get("ok") else [])
    feeds = feeds + extra_intl
    skipped = [{"id": f["id"], "name": f["name"], "reason": probe.get("reason")}
               for f in INTL_FEEDS if f["id"] not in intl_ids] if not probe.get("ok") else []

    specs: List[dict] = []
    report: List[dict] = []
    for feed in feeds:
        if _budget_done():
            report.append({"id": feed["id"], "name": feed["name"], "type": feed["type"],
                           "status": "预算到点短路", "条数": 0,
                           "detail": {"原因": "采集层全局预算耗尽，剩余端点本批不请求；"
                                              "已抓条目全部保留并继续出表"}})
            break
        p = PARSERS.get(feed["type"])
        if not p:
            continue
        try:
            items, detail = p(feed, win, timeout)
            specs.extend(items)
            report.append({"id": feed["id"], "name": feed["name"], "type": feed["type"],
                           "status": "已触达" if items else "已触达但 0 条",
                           "条数": len(items), "detail": detail})
        except urllib.error.HTTPError as e:
            report.append({"id": feed["id"], "name": feed["name"], "type": feed["type"],
                           "status": "未取得", "条数": 0,
                           "detail": {"http": e.code, "原因": "访问被拒（出口策略 / 反爬）"}})
        except Exception as e:
            report.append({"id": feed["id"], "name": feed["name"], "type": feed["type"],
                           "status": "未取得", "条数": 0,
                           "detail": {"error": f"{type(e).__name__}: {str(e)[:100]}"}})

    # 2026-11-22 线上复盘（二次修正）：旧版预算到点把 enrich_bodies 整段跳成 {}，
    # 线上批次短路 rss 后「正文补采 0 篇」→ 条目 evidence 全为 B → scoring 封顶
    # C（Final≤54.9）→ A 级独立原文占比 0.1071 且数学上不可能达标。
    # 第一版补救固定「6 篇 / 4s」仍然不够：线上日志 353.7s / 600s、剩余 246.3s，
    # 「预算耗尽」其实是**层配额之和用尽**，全局硬墙还大把余量，却被当成快没时间。
    # 现改为按全局硬墙余量自适应足量补采（8 并发），期间临时豁免 _get 预算检查、
    # 补完立即恢复；deadline 最多占余量 55%，永远留出筛选/评级/出表时间。
    if _budget_done():
        import copy as _copy
        _wall = float((cfg.get("budget") or {}).get("_wall_at") or 0)
        _left = (_wall - time.monotonic()) if _wall else 0.0
        # 硬墙余量 < 2s：后台补采会与主线程争 CPU，得不偿失，如实跳过。
        if _left < 2.0:
            body = {"启用": False, "原因": "全局硬墙余量不足，本批正文补采跳过（如实留痕）"}
            social = {}
        else:
            _bc = _copy.deepcopy(cfg)
            _lv = _bc.setdefault("source", {}).setdefault("live", {})
            _need = sum(1 for s in specs
                        if s.get("url")
                        and (s.get("pub") is None or win.contains(s["pub"]))
                        and len(re.sub(r"\s+", "", s.get("abstract") or "")) < 60)
            _cap = int(_lv.get("max_articles", 60))
            # 余量的 55% 给补采，上限 90s；剩下 45% + reserve 给出表，绝不拖过 _wall。
            _budget = min(_left * 0.55, 90.0)
            _lv["max_articles"] = min(_cap, max(1, _need))
            _lv["body_deadline_seconds"] = _budget
            _lv["article_timeout"] = min(float(_lv.get("article_timeout", 6)), 6.0)
            _lv["body_workers"] = 8
            _saved = _BUDGET_DEADLINE[0]
            _BUDGET_DEADLINE[0] = 0.0
            try:
                body = enrich_bodies(specs, win, _bc)
            finally:
                _BUDGET_DEADLINE[0] = _saved
            body["短路后按余量足量补采"] = {
                "全局硬墙余量秒": round(_left, 1), "待补条目": _need,
                "本批上限": _lv["max_articles"], "整批截止秒": round(_budget, 1),
                "依据": "层配额用尽 ≠ 全局预算耗尽；evidence 升 A 需要真补到正文"}
            social = {}
    else:
        body = enrich_bodies(specs, win, cfg)
        social = collect_social_hotboards(cfg, win, timeout)
    hinted = 0
    for s in specs:
        if not s.get("group"):
            g = suggest_group(s.get("title", ""), s.get("abstract", ""))
            if g:
                s["group"] = g
                hinted += 1
    meta = {
        "分类提示条目": hinted,
        "端点清单": ep_meta,
        "国际源组探测": probe,
        "启用源数": len(feeds),
        "申报未启用的国际源": skipped,
        "逐源触达": report,
        "原始条目数": len(specs),
        "社交热榜": social,
        "正文补采": body,
        "有发布时间": sum(1 for s in specs if s.get("pub")),
        "窗口内": sum(1 for s in specs if s.get("pub") and win.contains(s["pub"])),
        "诚实声明": "全部条目来自真实联网请求的结构化源 / 站内时间倒排页；"
                    "未取得、往年旧页、超窗口条目均逐条留痕，不编造、不回炉历史台账。",
    }
    return specs, meta
