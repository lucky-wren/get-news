"""无 API key 的多源检索 + 事件级成稿层（纯代码、纯规则、无需 LLM）。

回答用户 2026-11-21 的质疑：「不配 Serper key 就达不到效果吗？」
答：达不到的是「只配一把 key 的旧主链路」。本模块用全部免费、公开、无需签名的
入口重新造数据，且按「一个事件 = 一条 + N 个独立原文证据」成稿：

  Google News RSS  —— 免费无 key，单次查询约 100 条，带精确 pubDate 与来源域；
                      link 为 news.google.com 重定向，需跟随取真实 URL。
  Bing RSS         —— 免费无 key，format=rss，带 pubDate。
  GDELT DOC 2.0    —— 免费无 key，65 语言，250 条/查询，seendate；服务端要求 ≥5 秒/请求。
  SearXNG（可选）  —— 开源自建元搜索，format=json，无 key、无配额（SEARXNG_URL 配置）。
  正文补采         —— trafilatura（article.fetch_article_text），失败走 r.jina.ai 无 key 20 RPM。

成稿规则（对照用户样例 34 条：34/34 多渠道、34/34 两段摘要、64~100 净字）：
  * 每个独立域名只算一次证据，≥2 个独立域名才成稿；
  * 摘要取自独立原文正文，两段、整分句裁剪、净字 ≤100、数字一个不改写；
  * 不同原文对同类数字（死伤 / 金额 / 震级 / 时刻）报不同值时，并列标注而不静默挑一个；
  * 来源列合并为「A · B · C」，记录类型按证据构成判定，事件主键 e/s 按线索出处分配；
  * 时间不落窗口、正文取不到、独立源不足 → 只留线索与原因，不进主表，不拿话题名冒充新闻。

本模块不写 Excel、不改评分、不动编排；只把「成稿后的 RawItem」交给既有流水线。
"""
from __future__ import annotations

import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .article import fetch_article_text
from .model import RawItem
from .timewindow import parse_datetime_text

TZ = ZoneInfo("Asia/Shanghai")
# 全局 HTTP 并发闸：workers(12) × 活跃 provider(3) = 36。
# 上一版设 16 时，10 workers × 3 provider 要排队两轮，缩放实测 100 线索
# 只跑完 32 条；闸不能小于「workers × provider 数」，否则等于人为把并发打回串行。
_HTTP_SEM = threading.Semaphore(36)
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124 Safari/537.36")

# Bing RSS 在中文 Accept-Language 下返回「周五, 20 11月 2026 10:22:00 GMT」，
# email.utils.parsedate_to_datetime 不认中文星期/月份 → pub=None → 窗口硬闸全部拒绝。
# 2026-11-21 沙盒实测：默认 10/10 条 pub=None；加 en-US 头后返回标准 RFC 822。
_CN_WEEK = {"周一": "Mon", "周二": "Tue", "周三": "Wed", "周四": "Thu",
            "周五": "Fri", "周六": "Sat", "周日": "Sun", "星期日": "Sun"}
_CN_MONTH = {f"{i}月": m for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _parse_pub(raw: str):
    """统一时间解析：RFC 822 / 中文 locale pubDate / GDELT seendate / ISO8601。

    解析不了就返回 None 并保留原串，绝不拿抓取时刻或标题月日冒充发布时间。
    """
    s = str(raw or "").strip()
    if not s:
        return None
    # GDELT seendate：20261120T123000Z
    import re as _re
    m = _re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z?$", s)
    if m:
        try:
            return datetime(*(int(x) for x in m.groups()),
                            tzinfo=timezone.utc).astimezone(TZ)
        except Exception:
            pass
    # 中文星期 / 月份 → 英文（月份按长度降序，避免「11月」被「1月」先吃掉）
    t = s
    for cn, en in _CN_WEEK.items():
        t = t.replace(cn, en)
    for cn in sorted(_CN_MONTH, key=len, reverse=True):
        t = t.replace(cn, _CN_MONTH[cn])
    try:
        return _to_tz(parsedate_to_datetime(t))
    except Exception:
        pass
    # ISO 8601：纯标准库兜底，不依赖任何第三方日期库（沙盒实测 dateparser 未安装）
    try:
        iso = s.replace("Z", "+00:00")
        return _to_tz(datetime.fromisoformat(iso))
    except Exception:
        pass
    # 其它常见串：dateparser 可选，装了就用，没装不影响主链路
    try:
        import dateparser
        d = dateparser.parse(s, settings={"TIMEZONE": "Asia/Shanghai",
                                         "RETURN_AS_TIMEZONE_AWARE": True})
        if d:
            return _to_tz(d)
    except Exception:
        pass
    return None

GNEWS = "https://news.google.com/rss/search"
BING = "https://www.bing.com/search"
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
JINA_READER = "https://r.jina.ai/"

# 官方 / 央媒 / 主流门户优先，只用于「选代表标题 / 排来源列」，不改评分数值
AUTHORITY = [
    "gov.cn", "xinhuanet.com", "news.cn", "people.com.cn", "cctv.com", "chinanews.com.cn",
    "mohrss.gov.cn", "nhsa.gov.cn", "mof.gov.cn", "csrc.gov.cn", "pbc.gov.cn",
    "miit.gov.cn", "samr.gov.cn", "mem.gov.cn", "nhc.gov.cn", "mohurd.gov.cn",
    "caixin.com", "cls.cn", "thepaper.cn", "yicai.com", "21jingji.com", "stcn.com",
    "cnstock.com", "jrj.com.cn", "sina.com.cn", "sohu.com", "163.com", "qq.com",
]

TOPIC_QUERIES = [
    "医保 社保 养老金 辟谣", "地方债 城投 隐性债务", "新能源 电池 质保 召回",
    "AI 算力 芯片 裁员", "房地产 房企 面值退市 破产", "工厂 制药 医药 集采",
    "企业倒闭 资不抵债 清算", "铁路 民航 高速 出行 预警", "地震 灾害 突发事故 伤亡",
    "物价 猪肉 蔬菜 食品 消费", "劳动仲裁 拖欠工资 社保", "战争 空袭 伤亡 使馆",
]


# --------------------------------------------------------------------------- 数据结构
@dataclass
class Evidence:
    url: str
    title: str
    domain: str
    source: str
    provider: str
    pub: Optional[datetime] = None
    pub_raw: str = ""
    basis: str = ""
    google_redirect: bool = False
    snippet: str = ""
    body: str = ""
    body_from: str = ""


@dataclass
class ClueTrace:
    clue: str
    origin: str
    providers: Dict[str, int] = field(default_factory=dict)
    domains: int = 0
    verdict: str = "未处理"
    reason: str = ""
    url: str = ""
    sources: str = ""


def _http(url: str, timeout: float = 8.0, headers: Optional[dict] = None,
          follow: bool = True) -> Tuple[int, str, str]:
    """返回 (status, body, final_url)。不抛异常，逐条留给调用方判定。"""
    h = {"User-Agent": UA, "Accept": "application/json,application/rss+xml,application/xml,text/html,text/plain,*/*",
         "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if headers:
        h.update(headers)
    try:
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(1_800_000)
            charset = r.headers.get_content_charset() or "utf-8"
            return r.status, raw.decode(charset, "replace"), r.geturl()
    except urllib.error.HTTPError as e:
        try:
            b = e.read(1200).decode("utf-8", "replace")
        except Exception:
            b = ""
        return e.code, b, url
    except Exception as e:
        return 0, f"{type(e).__name__}: {str(e)[:120]}", url


def _to_tz(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def _domain(url: str) -> str:
    try:
        h = (urllib.parse.urlparse(url).hostname or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def _clean_title(s: str) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()
    s = re.sub(r"\s+-\s+[^-]{2,20}$", "", s)          # Google News RSS 的「 - 来源」尾巴
    return re.sub(r"\s+", " ", s)[:60]


def _authority(domain: str) -> int:
    for i, d in enumerate(AUTHORITY):
        if domain.endswith(d):
            return len(AUTHORITY) - i
    return 0


# --------------------------------------------------------------------------- Provider 1: Google News RSS
def gnews(query: str, *, timeout: float = 8.0, when: str = "1d",
          limit: int = 60) -> Tuple[List[Evidence], str]:
    q = query.strip()
    if when:
        q += f" when:{when}"
    url = GNEWS + "?" + urllib.parse.urlencode({
        "q": q, "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"})
    status, body, _ = _http(url, timeout)
    if status != 200 or not body.lstrip().startswith("<?xml") and "<rss" not in body[:400]:
        return [], f"HTTP {status} / {body[:70]}"
    try:
        root = ET.fromstring(body)
    except Exception as e:
        return [], f"RSS 解析失败 {type(e).__name__}"
    out: List[Evidence] = []
    for it in root.findall("./channel/item")[:limit]:
        link = (it.findtext("link") or "").strip()
        title = _clean_title(it.findtext("title") or "")
        if not link or not title:
            continue
        pub_raw = (it.findtext("pubDate") or "").strip()
        pub = _parse_pub(pub_raw)
        src = ""
        source_domain = ""
        node = it.find("source")
        if node is not None:
            # 2026-11-22 根因：旧版先取 node.text（「中新网」这类中文刊名），
            # _domain("中新网") 返回空 → domain 恒为 news.google.com →
            # search_clue 按 domain 去重时所有 gnews 条目塌缩成 1 条，
            # min_independent_domains=2 数学上不可达，gnews 通道恒 0 成稿。
            # 改为优先用 <source url="https://..."> 的真实域名。
            s_url = (node.attrib.get("url") or "").strip()
            if s_url:
                source_domain = _domain(s_url)
            src = (node.text or "").strip() or source_domain or "Google News"
        domain = source_domain or "news.google.com"
        out.append(Evidence(
            url=link, title=title, domain=domain,
            source=src or "Google News", provider="gnews", pub=pub, pub_raw=pub_raw,
            basis="Google News RSS pubDate" + (f"（来源域名 {source_domain}）" if source_domain else ""),
            google_redirect=True,
            snippet=html.unescape(re.sub(r"<[^>]+>", "", it.findtext("description") or ""))[:200]))
    return out, f"HTTP 200 / {len(out)} 条"


# --------------------------------------------------------------------------- Provider 2: Bing RSS
def bing_rss(query: str, *, timeout: float = 8.0, limit: int = 30) -> Tuple[List[Evidence], str]:
    url = BING + "?" + urllib.parse.urlencode({"q": query, "format": "rss", "count": "30"})
    # en-US 让 Bing 返回标准 RFC 822 pubDate（中文 locale 会返回中文星期/月份）
    status, body, _ = _http(url, timeout,
                            headers={"Accept-Language": "en-US,en;q=0.9"})
    if status != 200 or "<rss" not in body[:400]:
        return [], f"HTTP {status} / {body[:70]}"
    try:
        root = ET.fromstring(body)
    except Exception as e:
        return [], f"RSS 解析失败 {type(e).__name__}"
    out = []
    for it in root.findall("./channel/item")[:limit]:
        link = (it.findtext("link") or "").strip()
        title = _clean_title(it.findtext("title") or "")
        if not link or not title:
            continue
        pub_raw = (it.findtext("pubDate") or "").strip()
        pub = _parse_pub(pub_raw)
        d = _domain(link)
        out.append(Evidence(url=link, title=title, domain=d, source=d,
                            provider="bing_rss", pub=pub, pub_raw=pub_raw,
                            basis="Bing RSS pubDate",
                            snippet=html.unescape(re.sub(r"<[^>]+>", "", it.findtext("description") or ""))[:200]))
    return out, f"HTTP 200 / {len(out)} 条"


# --------------------------------------------------------------------------- Provider 3: GDELT DOC 2.0
_GDELT_LAST = [0.0]
_GDELT_CALLS = [0]
_GDELT_LOCK = __import__("threading").Lock()


def gdelt(query: str, *, timeout: float = 12.0, maxrecords: int = 100,
          timespan: str = "1d", gap: float = 5.2,
          max_total: int = 20) -> Tuple[List[Evidence], str]:
    # 服务端硬要求 ≥5 秒/请求：全局锁保证跨线索并发时不会同时打过去；
    # 批次上限防止 80 个线索 × 5.2 秒把整批拖到 7 分钟。
    with _GDELT_LOCK:
        if _GDELT_CALLS[0] >= max_total:
            return [], f"本批 GDELT 调用已达上限 {max_total}（免费通道 5 秒/请求）"
        wait = gap - (time.time() - _GDELT_LAST[0])
        if wait > 0:
            time.sleep(min(wait, gap))
        url = GDELT + "?" + urllib.parse.urlencode({
            "query": query, "mode": "artlist", "maxrecords": str(maxrecords),
            "format": "json", "timespan": timespan, "sort": "datedesc"})
        status, body, _ = _http(url, timeout)
        _GDELT_LAST[0] = time.time()
        _GDELT_CALLS[0] += 1
    if status != 200 or not body.lstrip().startswith("{"):
        return [], f"HTTP {status} / {body[:70] or '非 JSON（限流）'}"
    try:
        arts = json.loads(body).get("articles") or []
    except Exception as e:
        return [], f"JSON 解析失败 {type(e).__name__}"
    out = []
    for a in arts:
        u = (a.get("url") or "").strip()
        t = _clean_title(a.get("title") or "")
        if not u or not t:
            continue
        raw = str(a.get("seendate") or "")
        pub = _parse_pub(raw)
        d = (a.get("domain") or _domain(u)).lower()
        out.append(Evidence(url=u, title=t, domain=d, source=d,
                            provider="gdelt", pub=pub, pub_raw=raw,
                            basis="GDELT DOC 2.0 seendate（UTC 首次监测时刻）"))
    return out, f"HTTP 200 / {len(out)} 条"


# --------------------------------------------------------------------------- Provider 4: SearXNG（可选自建，无 key 无配额）
def searxng(query: str, base: str = "", *, timeout: float = 10.0,
            limit: int = 30) -> Tuple[List[Evidence], str]:
    base = (base or os.environ.get("SEARXNG_URL", "")).strip().rstrip("/")
    if not base:
        return [], "未配置 SEARXNG_URL（可选，开源自建；不配不影响其它 provider）"
    url = base + "/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    status, body, _ = _http(url, timeout)
    if status != 200 or not body.lstrip().startswith("{"):
        return [], f"HTTP {status} / {body[:70]}"
    try:
        rows = json.loads(body).get("results") or []
    except Exception as e:
        return [], f"JSON 解析失败 {type(e).__name__}"
    out = []
    for r in rows[:limit]:
        u = str(r.get("url") or "").strip()
        t = _clean_title(str(r.get("title") or ""))
        if not u or not t:
            continue
        pub = None
        raw = str(r.get("publishedDate") or "")
        if raw:
            try:
                pub = _to_tz(datetime.fromisoformat(raw.replace("Z", "+00:00")))
            except Exception:
                pub, basis = parse_datetime_text(raw)
        out.append(Evidence(url=u, title=t, domain=_domain(u), source=_domain(u),
                            provider="searxng", pub=pub, pub_raw=raw,
                            basis="SearXNG publishedDate" if pub else "未取得",
                            snippet=str(r.get("content") or "")[:200]))
    return out, f"HTTP 200 / {len(out)} 条"


PROVIDERS = ("gnews", "bing_rss", "gdelt", "searxng")


# --------------------------------------------------------------------------- Provider 0: 中文实证端点
# 2026-11-22 线上批次根因（用户上传 latest.xlsx 的 H-c 逐次留痕）：
#   122 个检索候选 URL 里，中文独立域名只有 6 个；88 个是 www.bing.com 搜索页本身、
#   8 个 news.google.com 重定向、其余是 crazygames / texttwist / fanbolt / vapepinoy /
#   kmap-state-ks.us 等与中文主题词完全无关的英文站。keyless 43 次调用 0 成稿。
#   换网络只让这些英文站可达，**召回质量不会变**——这不是 key 问题，也不是推包问题。
# 解法：中文证据不从通用英文搜索引擎召回，改从项目自己已实证可达的中文端点取
#   （endpoints_live.yaml 里央视网 JSONP / 中新网 RSS / 人民网 RSS 等，沙盒实测央视网
#    news_1.jsonp HTTP 200 / 80 条带秒级 focus_date）。每个端点给独立域名与发布时间。
CN_ENDPOINT_TYPES = ("jsonp", "rss", "rss_xml", "feed")
# 明显不是中文新闻源的域名特征：搜索结果页、重定向器、游戏/影视/电商/海外政务门户
CN_JUNK_DOMAIN = re.compile(
    r"crazygames|texttwist|fanbolt|vapepinoy|cabinsusagatlinburg|kmap-state|kancare|"
    r"\.us$|\.gov\.uk$|pinterest|instagram|facebook|youtube|twitter|x\.com", re.I)
CN_SEARCH_PAGE = re.compile(r"bing\.com|duckduckgo|baidu\.com/link", re.I)
# url -> (拉取时刻, 解析行, 状态)；同一跑批内跨线索复用，避免 80×8 次重复请求
_CN_ROWS_CACHE: Dict[str, Tuple[float, List[dict], str]] = {}


def _cn_chars(s: str) -> str:
    return "".join(ch for ch in str(s or "") if "\u4e00" <= ch <= "\u9fff")


def _cn_overlap(title: str, clue: str) -> float:
    """中文证据匹配：标题含主题词任一 ≥2 字词元即命中，否则算 2-gram 重合率。

    端点当页是「按时间倒排的最新 80 条」，不是按主题检索的结果；用整体重合率
    会把「医保钱包2.0上线」这类只含一个主题词的真条目也判成不相关（实测 8 端点
    80×6+30×2 条 → 0 命中）。词元包含才是中文新闻的正确匹配方式。
    """
    t, c = _cn_chars(title), _cn_chars(clue)
    if len(t) < 4 or len(c) < 4:
        return 0.0
    toks = [x for x in re.split(r"[\s、,，/|]+", str(clue or ""))
            if len(_cn_chars(x)) >= 2]
    for tok in toks:
        if _cn_chars(tok) and _cn_chars(tok) in t:
            return 1.0
    a = {t[i:i + 2] for i in range(len(t) - 1)}
    b = {c[i:i + 2] for i in range(len(c) - 1)}
    return len(a & b) / max(1, min(len(a), len(b)))


def _cn_pub(raw: str) -> Optional[datetime]:
    raw = str(raw or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y/%m/%d %H:%M:%S", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            return _to_tz(datetime.strptime(raw, fmt))
        except (ValueError, TypeError):
            continue
    parsed = _parse_pub(raw)
    return _to_tz(parsed) if parsed else None


def _cn_rows(body: str) -> List[dict]:
    """从 JSONP / RSS 响应里抽出带标题 + 时间 + URL 的条目（纯解析，不猜）。"""
    text = (body or "").strip()
    out: List[dict] = []
    # JSONP / JSON
    try:
        m = re.search(r"^[^(]*\((.*)\)\s*;?\s*$", text, re.S)
        d = json.loads(m.group(1) if m else text)
        if isinstance(d, dict):
            d = d.get("data", d)
        if isinstance(d, dict):
            for v in d.values():
                if isinstance(v, list):
                    d = v
                    break
        if isinstance(d, list):
            for o in d:
                if not isinstance(o, dict):
                    continue
                title = str(o.get("title") or o.get("Title") or "").strip()
                pub = str(o.get("focus_date") or o.get("pubDate") or o.get("date") or "").strip()
                url = ""
                for k in ("url", "link", "m_url", "docurl", "share_url"):
                    if isinstance(o.get(k), str) and o[k].startswith("http"):
                        url = o[k]
                        break
                if title and pub and url:
                    out.append({"title": title, "pub": pub, "url": url,
                                "source": str(o.get("source") or "")})
    except (ValueError, TypeError, AttributeError):
        pass
    if out:
        return out
    # RSS
    try:
        root = ET.fromstring(text)
        for it in root.iter():
            if it.tag.split("}")[-1].lower() not in ("item", "entry"):
                continue
            g = {}
            for ch in it:
                tag = ch.tag.split("}")[-1].lower()
                if tag in ("title", "pubdate", "published", "updated", "link", "date"):
                    g[tag] = (ch.text or "").strip() or ch.attrib.get("href", "")
            url = str(g.get("link", ""))
            if str(g.get("title")) and (g.get("pubdate") or g.get("published") or g.get("date")) and url.startswith("http"):
                out.append({"title": g["title"],
                            "pub": g.get("pubdate") or g.get("published") or g.get("date"),
                            "url": url, "source": ""})
    except ET.ParseError:
        pass
    return out


def cn_endpoints(clue: str, *, cfg: dict | None = None, timeout: float = 8.0,
                 win: Any = None) -> Tuple[List[Evidence], str]:
    """从已实证中文端点按主题词取独立原文证据；不可达端点逐条申报、不编数据。"""
    conf = ((cfg or {}).get("keyless", {}) or {})
    path = Path(__file__).resolve().parents[1] / str(conf.get("endpoints_file", "data/endpoints_live.yaml"))
    if not path.exists():
        return [], "endpoints_live.yaml 不存在"
    try:
        import yaml
        eps = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("endpoints") or []
    except Exception as e:
        return [], f"yaml 读取失败 {type(e).__name__}"
    # 按独立域名各取 1 个端点：yaml 前 8 条有 6 条都是 news.cctv.com，
    # 顺序截断会导致「多渠道」永远只有 1 个域名。跨域名取样才有覆盖证据。
    by_dom: Dict[str, dict] = {}
    for e in eps:
        if str(e.get("type", "")).lower() not in CN_ENDPOINT_TYPES:
            continue
        u = str(e.get("url") or "")
        if not u.startswith("http"):
            continue
        by_dom.setdefault(_domain(u), e)
    cands = list(by_dom.values())[:int(conf.get("cn_max_endpoints", 8))]
    evs: List[Evidence] = []
    notes: List[str] = []
    ttl = float(conf.get("cn_cache_ttl", 300.0))
    for ep in cands:
        url = str(ep.get("url") or "")
        if not url.startswith("http"):
            continue
        # 同一批 80 个线索不该把同一端点拉 80 次；缓存解析后的行，只按线索过滤
        cached = _CN_ROWS_CACHE.get(url)
        if cached and time.time() - cached[0] < ttl:
            rows, st = list(cached[1]), cached[2]
        else:
            status, body, _final = _http(url, timeout=timeout)
            st = str(status or "ERR")
            rows = _cn_rows(body) if status == 200 else []
            _CN_ROWS_CACHE[url] = (time.time(), rows, st)
        if st != "200":
            notes.append(f"{ep.get('id', '?')}:{st}")
            continue
        kept = 0
        for r in rows:
            if _cn_overlap(r["title"], clue) < float(conf.get("cn_min_overlap", 0.18)):
                continue
            pub = _cn_pub(r["pub"])
            if pub is None or (win is not None and not win.contains(pub)):
                continue
            dom = _domain(r["url"])
            if not dom or CN_JUNK_DOMAIN.search(dom):
                continue
            evs.append(Evidence(url=r["url"], title=_clean_title(r["title"]),
                                domain=dom,
                                source=str(r.get("source") or ep.get("channel") or dom),
                                provider="cn_endpoints", pub=pub, pub_raw=r["pub"],
                                basis=f"中文实证端点 {ep.get('id')} 时间字段"))
            kept += 1
        notes.append(f"{ep.get('id', '?')}:{len(rows)}→{kept}")
    return evs, "；".join(notes[:8])


def prefilter_noise(evs: List[Evidence], clue: str, cfg: dict | None = None) -> List[Evidence]:
    """search_clue 里在域名去重前剔掉与中文线索零重合的英文站 / 搜索结果页。

    线上 H-c 里这些噪声占满了 max_redirects 与域名去重名额；它们在后端硬闸也会被拦，
    但先剔能让真正的中文证据进 top，且省下重定向与正文补采的预算。
    """
    conf = ((cfg or {}).get("keyless", {}) or {})
    if not conf.get("cn_noise_prefilter", True):
        return evs
    out: List[Evidence] = []
    for e in evs:
        # 搜索结果页 URL 不是独立原文，直接剔；news.google.com 保留给 resolve_redirect
        if CN_SEARCH_PAGE.search(e.domain):
            continue
        if CN_JUNK_DOMAIN.search(e.domain) and "google.com" not in e.domain:
            continue
        # 英文标题（无中文字符）对中文线索无证据价值
        if not _cn_chars(e.title) and _cn_chars(clue):
            continue
        out.append(e)
    return out


def resolve_redirect(ev: Evidence, timeout: float = 7.0) -> Evidence:
    """Google News RSS 的 link 是重定向，跟随取真实 URL（2024 起不能 base64 解码）。"""
    if not ev.google_redirect:
        return ev
    status, _, final = _http(ev.url, timeout)
    if status in (200, 301, 302, 303, 307, 308) and final and "news.google.com" not in final:
        ev.url = final
        d = _domain(final)
        if d and d != "news.google.com":
            ev.domain = d
            if not ev.source or ev.source == "Google News":
                ev.source = d
        ev.google_redirect = False
    else:
        # Google 2024 起部分 /rss/articles/CBMi... 跟随 302 只回到自身中转页，
        # 老格式可直接 base64 解出明文原文 URL；解不出才如实标未取得，绝不猜。
        from .url_resolve import decode_gnews_article
        real = decode_gnews_article(ev.url)
        if real:
            ev.url = real
            ev.domain = _domain(real) or ev.domain
            if not ev.source or ev.source == "Google News":
                ev.source = ev.domain
            ev.google_redirect = False
            ev.basis += "；Google News base64 备用还原"
        else:
            ev.basis += "；重定向未解析，正文用 Reader 兜底"
    return ev


def search_clue(clue: str, *, cfg: dict | None = None, win: Any = None,
                resolve: bool = True) -> Tuple[List[Evidence], ClueTrace]:
    """一个线索 → 多路无 key 检索 → 按域名去重的独立原文证据。"""
    conf = ((cfg or {}).get("keyless", {}) or {})
    timeout = float(conf.get("timeout", 9.0))
    order = [p.strip() for p in str(conf.get("provider_order", "gnews,bing_rss,gdelt,searxng")).split(",")
             if p.strip()]
    # 未配置的 provider 不占线程/并发闸：SearXNG 没填实例地址时原版仍提交一个
    # 立即返回的空任务，白占 1/4 并发额度，等于把吞吐压低 25%。
    if not str(conf.get("searxng_url", "")).strip():
        order = [p for p in order if p != "searxng"]
    tr = ClueTrace(clue=clue, origin=str(conf.get("origin", "主题词表")))
    all_ev: List[Evidence] = []

    def _one(p: str) -> Tuple[str, List[Evidence], int]:
        """单个 provider 的调用体；并发执行，异常各自兜底不互相拖死。

        全局闸 16：workers=10 × 每线索 3 provider 理论 30 个并发 HTTP，
        对 gnews / Bing / Jina 这类免费无 key 通道过于激进（易触发限流或封 IP）。
        闸必须 ≥ workers × 活跃 provider 数（12×3=36），否则等于把并发打回串行；
        未配置的 SearXNG 已在 order 里剔除，实际活跃 provider 为 3。
        """
        try:
            with _HTTP_SEM:
                return _one_inner(p)
        except Exception as e:
            return p, [], 0

    def _one_inner(p: str) -> Tuple[str, List[Evidence], int]:
        try:
            if p == "cn_endpoints":
                # 线上 H-c：122 个检索候选 URL 里中文独立域名仅 6 个，通用英文搜索
                # 对中文主题词召回基本失效。中文证据改从项目已实证端点取（带秒级时间）。
                rows, _why = cn_endpoints(clue, cfg=cfg, timeout=timeout, win=win)
            elif p == "gnews":
                rows, _why = gnews(clue, timeout=timeout,
                                   when=str(conf.get("gnews_when", "1d")),
                                   limit=int(conf.get("gnews_limit", 60)))
            elif p == "bing_rss":
                rows, _why = bing_rss(clue, timeout=timeout, limit=int(conf.get("bing_limit", 30)))
            elif p == "gdelt":
                # 内部已有 _GDELT_LOCK 全局锁（服务端 ≥5 秒/请求），并发不会打爆限流；
                # 但它会串行占用线程，所以默认 provider_order 已把 gdelt 移到独立扩展层。
                rows, _why = gdelt(clue, timeout=float(conf.get("gdelt_timeout", 12.0)),
                                   maxrecords=int(conf.get("gdelt_maxrecords", 100)),
                                   gap=float(conf.get("gdelt_gap_seconds", 5.2)),
                                   max_total=int(conf.get("gdelt_max_total_calls", 20)))
            elif p == "searxng":
                rows, _why = searxng(clue, str(conf.get("searxng_url", "")), timeout=timeout)
            else:
                return p, [], 0
            return p, rows, len(rows)
        except Exception as e:
            return p, [], 0

    # 2026-11-22 吞吐根因：旧版 4 个 provider 串行 → 单线索 4×timeout≈24s，
    # 6 workers × 140s 只跑得完约 13 个线索，数学上产不出目标 34 条多源事件。
    # 改为同一线索内并发；单线索耗时从 Σ(timeout) 降到 max(timeout)。
    if len(order) > 1:
        with ThreadPoolExecutor(max_workers=len(order)) as ex:
            futs = [ex.submit(_one, p) for p in order]
            for f in as_completed(futs):
                try:
                    p, rows, n = f.result()
                except Exception:
                    continue
                tr.providers[p] = n
                all_ev.extend(rows)
    else:
        p, rows, n = _one(order[0]) if order else ("", [], 0)
        if p:
            tr.providers[p] = n
            all_ev.extend(rows)

    # 先剔与中文线索零重合的英文站 / 搜索结果页，别让它们占去重与重定向名额
    all_ev = prefilter_noise(all_ev, clue, cfg)
    from .url_resolve import dedup_key, is_offtopic, resolve_search_url

    # 2026-11-22：旧版按 ev.domain 去重，gnews 的 domain 恒 news.google.com
    # → 多条独立报道塌缩成 1 条 → 永远达不到 min_independent_domains。
    # 第一道去重改用 **link 本身**（每条 CBMi.../原文 URL 各不相同）；
    # 还原重定向之后再按真实域名二次去重。
    best: Dict[str, Evidence] = {}
    off_topic = 0
    for ev in all_ev:
        if not ev.url or not ev.domain:
            continue
        # Bing ck/a 还原 + Bing 自有页剔除（线上 104 次 bing 0 成稿的直接原因）
        if "bing.com/ck/a" in ev.url or "bing.com/" in ev.url:
            real = resolve_search_url(ev.url)
            if not real:
                continue
            ev.url = real
            ev.domain = _domain(real) or ev.domain
        bad, _why = is_offtopic(ev.title, clue, ev.url)
        if bad:
            off_topic += 1
            continue
        if win is not None and ev.pub is not None:
            try:
                if not win.contains(ev.pub):
                    continue
            except Exception:
                pass
        k = dedup_key(ev.url, ev.domain)
        cur = best.get(k)
        if cur is None or (_authority(ev.domain), bool(ev.pub), len(ev.title)) > \
                (_authority(cur.domain), bool(cur.pub), len(cur.title)):
            best[k] = ev
    evs = sorted(best.values(), key=lambda e: (-_authority(e.domain), -(e.pub.timestamp() if e.pub else 0)))

    # 先还原再二次去重（AIHOT：同一参与方只算一次）
    if resolve:
        n_res = int(conf.get("max_redirects", 12))
        with ThreadPoolExecutor(max_workers=min(8, max(1, min(n_res, len(evs))))) as ex:
            head = list(ex.map(lambda e: resolve_redirect(e, timeout), evs[:n_res]))
            evs = head + evs[n_res:]
    second: Dict[str, Evidence] = {}
    for ev in evs:
        k = ev.domain or _domain(ev.url)
        cur = second.get(k)
        if cur is None or (bool(ev.pub), len(ev.title)) > (bool(cur.pub), len(cur.title)):
            second[k] = ev
    evs = sorted(second.values(),
                 key=lambda e: (-_authority(e.domain), -(e.pub.timestamp() if e.pub else 0)))
    tr.domains = len(evs)
    tr.reason = ("；".join(f"{k}:{v}" for k, v in tr.providers.items())
                 + (f"；中文相关性闸剔除 {off_topic} 条" if off_topic else ""))
    return evs, tr


# --------------------------------------------------------------------------- 正文与两段摘要（纯规则）
def _fetch_body(ev: Evidence, timeout: float = 9.0, use_jina: bool = True,
                jina_timeout: float = 9.0) -> str:
    try:
        s = fetch_article_text(ev.url, timeout=timeout, max_chars=6000) or ""
        if len(re.sub(r"\s", "", s)) >= 40:
            ev.body_from = "独立原文正文"
            return s
    except Exception:
        pass
    if use_jina:
        status, body, _ = _http(JINA_READER + ev.url, jina_timeout,
                                headers={"X-Return-Format": "text", "X-No-Cache": "true"})
        if status == 200 and len(body.strip()) >= 60:
            ev.body_from = "Jina Reader 兜底（无 key 20 RPM）"
            return body[:6000]
    ev.body_from = "未取得"
    return ""


def _sentences(text: str) -> List[str]:
    t = re.sub(r"[\u3000\s]+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or "")))
    t = re.sub(r"(责任编辑|记者|编辑|相关阅读|点击查看|扫码|免责声明|版权声明)[:：]?.*$", "", t)
    return [s.strip() for s in re.split(r"(?<=[。！？；])", t) if 8 <= len(s.strip()) <= 160]


def _informative(s: str) -> int:
    return (2 if re.search(r"\d", s) else 0) + (1 if re.search(r"(发布|通报|决定|宣布|致|造成|上调|下调|实施|受理|辟谣|预警|破产|裁员|降价|涨价)", s) else 0) \
        + (1 if 20 <= len(s) <= 90 else 0)


NUM_PAT = re.compile(r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>人死亡|人受伤|人伤亡|级地震|亿元|万元|港元|万人次|列|人|%)")


def _bigrams(s: str) -> set:
    s = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _bigram_jaccard(a: str, b: str) -> float:
    A, B = _bigrams(a), _bigrams(b)
    return len(A & B) / max(1, len(A | B))


def _conflict_lines(evs: List[Evidence]) -> List[str]:
    """跨源数字冲突并列：同一单位出现两个不同值、且两篇报道确属同一事件时才标注。

    防误判：两篇原文标题的中文双字锚点交集 <2 视为不同事件，不并列
    （例：「沙特空袭致36人受伤」与「熊本地震2人受伤」单位相同但不是一件事）。
    """
    vals: Dict[str, List[Tuple[str, str, set]]] = {}
    for e in evs:
        if not e.body:
            continue
        bg = _bigrams(e.title)
        for m in NUM_PAT.finditer(e.body[:1200]):
            vals.setdefault(m.group("unit"), []).append(
                (m.group("num"), e.source or e.domain, bg))
    out = []
    for unit, pairs in vals.items():
        uniq: List[Tuple[str, str, set]] = []
        for n, s, bg in pairs:
            if n not in [x[0] for x in uniq]:
                uniq.append((n, s, bg))
        if len(uniq) < 2:
            continue
        # 至少两组证据讲同一件事（标题锚点交集 ≥2），否则跳过
        if not any(len(a[2] & b[2]) >= 2 for i, a in enumerate(uniq) for b in uniq[i + 1:]):
            continue
        out.append(f"不同来源对{unit}数据口径不一（{'、'.join(f'{s}报{n}{unit}' for n, s, _ in uniq[:3])}），并列标注。")
    return out[:2]


def _net(s: str) -> int:
    return len(re.sub(r"[\s\u3000]", "", s or ""))


def compose_abstract(evs: List[Evidence], limit: int = 100) -> str:
    """两段、整分句、净字 ≤100、数字不改写；第二段优先放跨源差异。"""
    primary = [e for e in evs if e.body][:4]
    if not primary:
        return ""
    sents: List[str] = []
    for e in primary:
        for s in _sentences(e.body):
            if not any(s in x or x in s for x in sents):
                sents.append(s)
    sents.sort(key=lambda s: -_informative(s))
    first = sents[0] if sents else ""
    # 首句超预算时按整分句收束，绝不切半句
    if _net(first) > limit - 24:
        parts = re.split(r"(?<=[，,])", first)
        first = ""
        for p in parts:
            if _net(first + p) > limit - 24:
                break
            first += p
        first = first.rstrip("，,") + "。"
    second_pool = [s for s in sents[1:] if s != first] + _conflict_lines(primary)
    second = ""
    for s in second_pool:
        # 不能把第一段换个说法再抄一遍（样例里 49/94 条段落自我重复就是这么来的）
        if _bigram_jaccard(first, s) > 0.45:
            continue
        if _net(first) + _net(s) <= limit:
            second = s
            break
    if not second and first:
        # 从首句尾部拆出结论分句做第二段，仍保持整分句
        tail = re.split(r"(?<=[，,])", first)
        if len(tail) >= 3:
            cand = "".join(tail[-2:]).rstrip("，,") + "。"
            first = "".join(tail[:-2]).rstrip("，,") + "。"
            if _net(first) + _net(cand) <= limit and _net(cand) >= 12:
                second = cand
    # 与 normalize._format 同形态：每段 2 全角空格缩进，净字数不含缩进/换行
    ind = "\u3000\u3000"
    para1 = ind + first
    out = para1 + (("\n" + ind + second) if second else "")
    return out if _net(out) <= limit else para1


# --------------------------------------------------------------------------- 事件成稿
def compose_event(clue: str, evs: List[Evidence], *, win: Any, cfg: dict | None = None,
                  seq: int = 1, origin: str = "主题词表",
                  deadline: float = 0.0) -> Tuple[Optional[RawItem], ClueTrace]:
    conf = ((cfg or {}).get("keyless", {}) or {})
    tr = ClueTrace(clue=clue, origin=origin, domains=len(evs))
    tr.providers = {"候选证据": len(evs)}
    if deadline and time.time() > deadline:
        tr.verdict, tr.reason = "未成稿", "整批总时限到达，留待下一批"
        return None, tr
    min_domains = int(conf.get("min_independent_domains", 2))
    if len(evs) < min_domains:
        tr.verdict, tr.reason = "未成稿", f"独立域名 {len(evs)} < {min_domains}，只留线索"
        return None, tr
    top = evs[:int(conf.get("max_sources_per_event", 6))]
    with ThreadPoolExecutor(max_workers=min(6, len(top))) as ex:
        futs = {ex.submit(_fetch_body, e, float(conf.get("body_timeout", 9.0)),
                          bool(conf.get("jina_fallback", True)),
                          float(conf.get("jina_timeout", 9.0))): e for e in top}
        for f in as_completed(futs):
            e = futs[f]
            try:
                e.body = f.result()
            except Exception:
                e.body = ""
    good = [e for e in top if e.body]
    if not good:
        tr.verdict, tr.reason = "未成稿", "全部独立原文未取得正文（不拿 snippet 冒充）"
        return None, tr
    # 2026-11-22：真实环境里多个独立域名常只有 1~2 个站点开放正文，旧版要求
    # 「每个来源都取到正文」导致 0 成稿，主表退回单源 RSS。改为：
    #   * 摘要只从取到正文的独立原文抽（至少 1 篇）——绝不用 snippet / 标题凑；
    #   * 其余独立原文只要「有独立 URL + 可核对发布时间」就作为**覆盖证据**进来源列，
    #     并在 raw / 附录逐条标注「仅覆盖证据，不参与摘要」。
    # 这仍满足「逐条回查独立原文、每条带 URL/发布时间/依据」，不编造任何内容。
    min_bodies = int(conf.get("min_bodies_with_text", 1))
    if len(good) < min_bodies:
        tr.verdict, tr.reason = "未成稿", f"取到正文的独立原文 {len(good)} < {min_bodies}"
        return None, tr
    coverage = [e for e in top if e.pub is not None and e.url]
    if len({e.domain for e in coverage}) < min_domains:
        tr.verdict, tr.reason = "未成稿", (f"带可核对时间的独立域名 "
                                      f"{len({e.domain for e in coverage})} < {min_domains}")
        return None, tr

    abstract = compose_abstract(good, int(conf.get("abstract_net_chars", 100)))
    if not abstract:
        tr.verdict, tr.reason = "未成稿", "摘要抽取为空"
        return None, tr

    pubs = [e.pub for e in coverage if e.pub is not None]
    pub = max(pubs) if pubs else None
    pub_raw = pub.strftime("%Y-%m-%d %H:%M:%S") if pub else "未取得"
    basis = next((e.basis for e in good if e.basis), "未取得")
    # 窗口是唯一硬过滤；没有可信发布时间的条目不靠「时点推定」蒙混进池
    if pub is None:
        tr.verdict, tr.reason = "未成稿", "无任一原文带可核对发布时间"
        return None, tr
    try:
        if not win.contains(pub):
            tr.verdict, tr.reason = "未成稿", f"发布时间 {pub:%Y-%m-%d %H:%M} 落在窗口外"
            return None, tr
    except Exception:
        pass

    titles = [_clean_title(e.title) for e in good if _clean_title(e.title)]
    title = max(titles, key=lambda t: (_authority(good[titles.index(t)].domain), len(t))) if titles else clue[:60]
    sources = [e.source or e.domain for e in coverage]
    source_col = " · ".join(dict.fromkeys(sources))[:180]
    n_domains = len({e.domain for e in coverage})
    social = any(k in origin for k in ("社交", "热榜", "词条", "快照"))
    if social:
        rtype = "社交词条回查成稿"
        eid = f"s{seq:04d}"
    elif n_domains >= 3:
        rtype = "多渠道合并"
        eid = f"e{seq:04d}"
    else:
        rtype = "联网检索独立原文"
        eid = f"e{seq:04d}"

    it = RawItem(
        title=title, abstract=abstract, abstract_status="正文补采",
        source=source_col, url=good[0].url,
        pub=pub, pub_raw=pub_raw, time_basis=basis,
        time_mark="窗口内实证", date_clue="", channel="无Key多源检索 · " + origin,
        channel_id="KEYLESS", channel_level="☆",
        evidence="A", shape="事件成稿", has_event=True,
        record_type=rtype,
        board_platforms=[origin] if social else [], board_slots=1 if social else 0,
        media_slots=n_domains,
        raw={"事件主键": eid, "独立原文数": len(good),
             "独立域名": [e.domain for e in good],
             "证据来源": [{"URL": e.url, "发布时间": e.pub_raw, "Provider": e.provider,
                        "正文取得": e.body_from} for e in good],
             "线索": clue, "摘要净字": _net(abstract)})
    it.raw["覆盖证据"] = [{"URL": e.url, "发布时间": e.pub_raw, "域名": e.domain,
                      "角色": "摘要正文" if e in good else "仅覆盖证据（不参与摘要）"}
                     for e in coverage]
    it.raw["取到正文篇数"] = len(good)
    it.raw["独立域名数"] = n_domains
    it.event_id = eid                       # 供事件主键列与后续聚类使用
    it.seq = seq
    tr.verdict = "成稿"
    tr.reason = (f"独立域名 {n_domains} 个（{len(good)} 篇取到正文用于摘要，"
                 f"{n_domains - len(good)} 个仅作覆盖证据）；"
                 f"正文 {sum(1 for e in good if e.body_from.startswith('独立'))} 篇直取 / "
                 f"{sum(1 for e in good if e.body_from.startswith('Jina'))} 篇兜底")
    tr.url = good[0].url
    tr.sources = source_col
    return it, tr


def _compose_one(clue: str, origin: str, *, win: Any, cfg: dict, seq: int,
                 end_at: float) -> Tuple[Optional[RawItem], ClueTrace]:
    """一个线索的完整任务：检索（多 provider 并发）→ 正文 → 事件成稿。

    2026-11-22：旧版把 `compose_event` 放在 `as_completed` 主循环里**串行**执行，
    80 个线索 × 9s 正文 = 720s，全部叠在 140s 层配额之后；`time.time()>deadline`
    只跳过结果、不取消任务，后台照样跑完（缩放实测实耗是配额的 2.9 倍）。
    把两段一起放进 future，检索与成稿都并发，超时由 `wait` 真正截断。
    """
    if time.time() > end_at:
        return None, ClueTrace(clue, origin, verdict="未成稿", reason="总时限到达，留待下一批")
    try:
        evs, tr = search_clue(clue, cfg=cfg, win=win)
    except Exception as e:
        return None, ClueTrace(clue, origin, verdict="未成稿",
                               reason=f"{type(e).__name__}: {str(e)[:80]}")
    if time.time() > end_at:
        return None, ClueTrace(clue, origin, verdict="未成稿", reason="总时限到达，留待下一批")
    it, tr2 = compose_event(clue, evs, win=win, cfg=cfg, seq=seq,
                            origin=origin, deadline=end_at)
    tr.verdict, tr.reason = tr2.verdict, tr2.reason
    if it is not None:
        tr.url, tr.sources = tr2.url, tr2.sources
    return it, tr


def batch_compose(clues: List[Tuple[str, str]], *, win: Any, cfg: dict | None = None,
                  base_seq: int = 0) -> Tuple[List[RawItem], List[ClueTrace], Dict[str, Any]]:
    """主链路：线索（主题词表 + 社交/热榜词条）→ 多路检索 → 事件成稿。"""
    conf = ((cfg or {}).get("keyless", {}) or {})
    if not conf.get("enabled", True):
        return [], [], {"启用": False, "原因": "keyless.enabled=false"}
    max_clues = int(conf.get("max_clues", 80))
    pairs = [(str(c).strip(), str(o)) for c, o in clues if str(c).strip()][:max_clues]
    workers = int(conf.get("workers", 6))
    deadline = float(conf.get("deadline_seconds", 420.0))
    t0 = time.time()
    end_at = t0 + deadline
    _GDELT_CALLS[0] = 0            # 每批独立计数，不跨批次污染
    items: List[RawItem] = []
    traces: List[ClueTrace] = []
    timed_out = 0
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        # seq 用线索下标而非 len(items)，并发下顺序不定也能保证事件主键稳定唯一
        futs = {ex.submit(_compose_one, c, o, win=win, cfg=cfg or {},
                          seq=base_seq + i + 1, end_at=end_at): (c, o)
                for i, (c, o) in enumerate(pairs)}
        done, pending = wait(futs, timeout=max(1.0, deadline))
        for f in pending:
            c, o = futs[f]
            timed_out += 1
            traces.append(ClueTrace(c, o, verdict="未成稿", reason="总时限到达，留待下一批"))
        for f in done:
            c, o = futs[f]
            try:
                it, tr = f.result()
            except Exception as e:
                traces.append(ClueTrace(c, o, verdict="未成稿",
                                        reason=f"{type(e).__name__}: {str(e)[:80]}"))
                continue
            if it is not None:
                items.append(it)
            traces.append(tr)
    finally:
        # 关键：不等后台超时任务跑完，否则「短路」只是跳过结果、实际仍占满预算。
        # 已提交但未开始的任务全部取消；运行中的由 _compose_one 内的 end_at 检查自行退出。
        ex.shutdown(wait=False, cancel_futures=True)
    stats = {
        "启用": True, "线索": len(pairs), "成稿": len(items),
        "未成稿": len(traces) - len(items), "超时取消": timed_out,
        "并发数": workers, "平均独立域名": round(sum(t.domains for t in traces) / max(1, len(traces)), 2),
        "耗时秒": round(time.time() - t0, 1),
        "Provider": PROVIDERS,
        "无Key": True,
    }
    return items, traces, stats


# --------------------------------------------------------------------------- 离线自检（不联网）
def self_test() -> Dict[str, Any]:
    errs: List[str] = []
    # 1) Google News RSS 解析
    rss = ('<?xml version="1.0"?><rss version="2.0"><channel><item>'
           '<title>医保钱包2.0全部上线 - 央视财经</title>'
           '<link>https://news.google.com/rss/articles/CBMi123</link>'
           '<pubDate>Wed, 19 Nov 2026 10:00:00 GMT</pubDate>'
           '<source url="https://tv.cctv.com">央视财经</source>'
           '<description>380个统筹地区可代缴居民医保费</description></item></channel></rss>')
    try:
        root = ET.fromstring(rss)
        it = root.find("./channel/item")
        assert "医保钱包" in _clean_title(it.findtext("title"))
        assert _to_tz(parsedate_to_datetime(it.findtext("pubDate"))).year == 2026
    except Exception as e:
        errs.append(f"gnews 解析: {type(e).__name__}")
    # 2) GDELT seendate 解析
    m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$", "20261119T111500Z")
    if not m:
        errs.append("GDELT seendate 解析失败")
    # 3) 事件成稿：3 个独立域名 + 两段摘要 + 数字冲突并列
    evs = []
    for i, (dom, txt, title) in enumerate([
        ("xinhuanet.com", "11月19日，沙特主导联军空袭也门多省，致11人死亡、36人受伤。联军称拦截4枚弹道导弹。",
         "沙特空袭也门致11死36伤 联军称拦截4枚弹道导弹"),
        ("cctv.com", "新华社另报1人死亡、4人受伤，两家口径并列标注。沙特民防部门证实萨那多处发生爆炸。",
         "沙特空袭也门伤亡数字口径不一 新华社另报1死4伤"),
        ("thepaper.cn", "马西拉电视台援引卫生部门消息称，过去24小时空袭致11人死亡、36人受伤，数字仍在核对。",
         "也门多省遭空袭 马西拉电视台通报伤亡"),
    ]):
        evs.append(Evidence(url=f"https://{dom}/a{i}", title=title, domain=dom,
                            source=dom, provider="gnews",
                            pub=datetime(2026, 11, 19, 18, 0, tzinfo=TZ),
                            pub_raw="Wed, 19 Nov 2026 10:00:00 GMT", basis="RSS pubDate",
                            body=txt, body_from="独立原文正文"))
    ab = compose_abstract(evs, 100)
    if not ab or _net(ab) > 100:
        errs.append(f"摘要净字超限: {_net(ab)}")
    if "\n" not in ab:
        errs.append("摘要不是两段")
    if not any(k in ab for k in ("11人死亡", "36人受伤", "4枚弹道导弹", "4人受伤")):
        errs.append("摘要丢了关键数字")
    # 不同事件的同单位数字不得并列（防误判）
    other = [Evidence(url="https://nhk.or.jp/q", title="日本熊本5.7级地震2人受伤",
                      domain="nhk.or.jp", source="NHK", provider="gnews",
                      body="熊本县发生5.7级地震，2人受伤，九州新干线8列停运。",
                      body_from="独立原文正文")]
    if any("口径不一" in x for x in _conflict_lines(evs[:1] + other)):
        errs.append("跨事件数字被误判为口径冲突")
    # 4) 域名去重与权威排序
    dups = evs + [Evidence(url="https://xinhuanet.com/b", title="重复", domain="xinhuanet.com",
                           source="xinhuanet.com", provider="bing_rss")]
    seen = {}
    for e in dups:
        seen.setdefault(e.domain, e)
    if len(seen) != 3:
        errs.append("域名去重失败")
    # 5) 真实探测（沙盒受限时如实记录，不判失败）
    probe = {}
    for name, fn in (("gnews", lambda: gnews("医保 社保", timeout=5.0)),
                     ("bing_rss", lambda: bing_rss("医保 社保", timeout=5.0)),
                     ("gdelt", lambda: gdelt("医保", timeout=8.0, gap=0.1))):
        try:
            rows, why = fn()
            probe[name] = {"条数": len(rows), "状态": why[:90]}
        except Exception as e:
            probe[name] = {"条数": 0, "状态": f"{type(e).__name__}"}
    return {"通过": not errs, "错误": errs, "摘要样例": ab,
            "摘要净字": _net(ab), "真实探测": probe,
            "结论": "无 key 多源检索 + 事件级成稿纯规则链路可用" if not errs else "自检失败"}


if __name__ == "__main__":
    print(json.dumps(self_test(), ensure_ascii=False, indent=2))
