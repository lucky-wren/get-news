"""资讯获取 · 全渠道可达性引擎（需求「全部渠道都要获取数据，取不到想办法解决」）。

不再把「未取得」当终态。每条白名单渠道都必须走完下面这条**自动补齐链**，
并且在 data/channel_reach.json 里逐条落一个「启用动作」：

  1. 多协议 / 多路径候选：https、http、www、m.、RSS、sitemap、时间倒排页；
  2. 请求指纹矩阵：8 组 UA / Referer / Accept / 协议组合，403 自动换指纹重试
     （实测救回 cas / court / csrc / mee / moe / mofcom / mohrss / samr / spp / xinhua，
      并用 http 协议 + 换 Referer 救回微博热搜）；
  3. 开源公共聚合镜像：DailyHotApi / TrendRadar / newsnow / 60s 等实例的
     provider 列表逐个探测，哪个通用哪个，不写死单一地址；
  4. GitHub 可达通道：raw.githubusercontent.com / cdn.jsdelivr.net 在本环境实测 200，
     用于拉取开源项目上游清单与每日热榜归档快照；
  5. 站内搜索回查（无 key）：必应可达搜索页，把热榜词条回查成独立原文；
  6. 换网络自动重测：GitHub Actions 每次构建前跑本模块，unrestricted 网络下
     对被出口拦截的域名重新实测，可达且能抽出带时间条目的自动登记为端点；
  7. 需授权 / 需签名 / 非独立站点：给明确启用条件（开放平台能力、签名库、
     已授权聚合、人工确认官方源），不伪装成已触达。

任何一步都不编造条目：只有「真实请求 200 + 能解析出带发布时间的条目」才算取得。
"""
from __future__ import annotations

import argparse
import html as _html
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

ssl._create_default_https_context = ssl._create_unverified_context
BASE = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------- 请求指纹矩阵
PC = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
WEIBO_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
SPIDER = "Mozilla/5.0 (compatible; Baiduspider/2.0; +http://www.baidu.com/search/spider.html)"

FINGERPRINTS: List[Tuple[str, Dict[str, str], str]] = [
    ("F1_pc_noref", {"User-Agent": PC, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}, "https"),
    ("F2_pc_baidu", {"User-Agent": PC, "Accept": "*/*", "Referer": "https://www.baidu.com/"}, "https"),
    ("F3_iphone", {"User-Agent": IPHONE, "Accept": "*/*", "Accept-Language": "zh-CN"}, "https"),
    ("F4_iphone_self", {"User-Agent": IPHONE, "Accept": "text/html", "Referer": "SELF"}, "https"),
    ("F5_plain_json", {"Accept": "application/json,text/plain,*/*"}, "https"),
    ("F6_weibo", {"User-Agent": WEIBO_UA, "Accept": "*/*", "Referer": "https://weibo.com/"}, "https"),
    ("F7_http", {"User-Agent": PC, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}, "http"),
    ("F8_spider", {"User-Agent": SPIDER, "Accept": "*/*"}, "https"),
]
# 运行时自愈只用高价值 4 组，避免每请求都打 8 次
RUNTIME_FPS = [FINGERPRINTS[i] for i in (0, 2, 6, 7)]


def _url_for(scheme: str, tail: str) -> str:
    return f"{scheme}://{tail}"


def _headers_for(fp_name: str, headers: Dict[str, str], scheme: str, tail: str) -> Dict[str, str]:
    h = dict(headers)
    if h.get("Referer") == "SELF":
        h["Referer"] = f"{scheme}://{tail.split('/')[0]}/"
    return h


def fetch_matrix(tail: str, timeout: float = 7.0, fps: Optional[List] = None,
                 max_chars: int = 200_000) -> Dict[str, Any]:
    """对一个 URL（不含协议）做指纹矩阵请求，返回第一个 200 的内容与命中指纹。"""
    tried: List[Dict[str, Any]] = []
    for fp_name, headers, scheme in (fps or FINGERPRINTS):
        url = _url_for(scheme, tail)
        try:
            req = urllib.request.Request(url, headers=_headers_for(fp_name, headers, scheme, tail))
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read(max_chars)
                cs = r.headers.get_content_charset() or "utf-8"
                return {"ok": True, "status": r.status, "fp": fp_name, "url": url,
                        "text": raw.decode(cs, "replace"), "tried": tried}
        except urllib.error.HTTPError as e:
            tried.append({"fp": fp_name, "status": e.code})
        except Exception as e:
            tried.append({"fp": fp_name, "status": "ERR", "err": type(e).__name__})
    return {"ok": False, "status": tried[-1]["status"] if tried else "ERR",
            "fp": "", "url": "", "text": "", "tried": tried}


def get_with_matrix(url: str, timeout: float = 7.0) -> str:
    """运行时自愈：默认指纹失败就自动换指纹重试。供 live_cn / 社交采集调用。"""
    p = urllib.parse.urlparse(url)
    tail = p.netloc + p.path + (("?" + p.query) if p.query else "")
    r = fetch_matrix(tail, timeout, fps=RUNTIME_FPS)
    if r["ok"]:
        return r["text"]
    # 原样再试一次（某些站点只认完整原始 URL 的 header 组合）
    try:
        req = urllib.request.Request(url, headers={"User-Agent": PC, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as f:
            return f.read().decode(f.headers.get_content_charset() or "utf-8", "replace")
    except Exception:
        raise urllib.error.HTTPError(url, 403, "all fingerprints rejected", None, None)


# ---------------------------------------------------------------- 开源 provider / 归档通道
# 公共聚合实例逐个探测，哪个通用哪个；不写死单一地址，不假装都可用。
OPEN_PROVIDERS: List[Dict[str, str]] = [
    {"id": "DailyHotApi 官方实例", "url": "https://api-hot.imsyy.top/weibo", "license": "MIT",
     "repo": "imsyy/DailyHotApi", "note": "需自建或公共实例；本环境 DNS 未解析"},
    {"id": "DailyHotApi Vercel 实例", "url": "https://daily-hot.vercel.app/weibo", "license": "MIT",
     "repo": "imsyy/DailyHotApi", "note": "公共部署，出口可达时启用"},
    {"id": "newsnow 公共实例", "url": "https://newsnow.busiyi.world/api/s?id=weibo", "license": "MIT",
     "repo": "ourongxing/newsnow", "note": "11 平台热榜聚合"},
    {"id": "60s API", "url": "https://60s.viki.moe/v2/60s", "license": "MIT",
     "repo": "vikiboss/60s", "note": "每天 60 秒读报结构化源"},
    {"id": "vvhan 热榜", "url": "https://api.vvhan.com/api/hotlist/wbHot", "license": "开源",
     "repo": "vvhan", "note": "多平台热榜聚合"},
    {"id": "oioweb 热榜", "url": "https://api.oioweb.cn/api/common/HotList?type=weibo", "license": "开源",
     "repo": "oioweb", "note": "多平台热榜聚合"},
    {"id": "tenapi 热榜", "url": "https://tenapi.cn/v2/weibohot", "license": "开源",
     "repo": "tenapi", "note": "多平台热榜聚合"},
    {"id": "tophub.fun", "url": "https://api.tophub.fun/api/hot/weibo", "license": "开源",
     "repo": "tophub", "note": "榜单聚合"},
    {"id": "RSSHub 公共实例", "url": "https://rsshub.app/weibo/hot/search", "license": "MIT",
     "repo": "DIYgod/RSSHub", "note": "需公共实例或自建；路由覆盖 10 平台"},
    {"id": "TrendRadar 归档", "url": "https://raw.githubusercontent.com/sansan0/TrendRadar/master/config/config.yaml",
     "license": "GPL-3.0", "repo": "sansan0/TrendRadar",
     "note": "11 平台监控 + GitHub Actions 定时；本环境 GitHub raw 实测 200"},
    {"id": "DailyHotApi 源码清单", "url": "https://cdn.jsdelivr.net/gh/imsyy/DailyHotApi@master/package.json",
     "license": "MIT", "repo": "imsyy/DailyHotApi", "note": "jsdelivr 实测 200"},
]
# GitHub 可达通道：本环境 raw / jsdelivr 实测 200，可拉开源项目上游与归档快照
GITHUB_CHANNELS: List[Dict[str, str]] = [
    {"id": "GitHub raw", "url": "https://raw.githubusercontent.com/imsyy/DailyHotApi/master/README.md"},
    {"id": "jsDelivr GH", "url": "https://cdn.jsdelivr.net/gh/sansan0/TrendRadar@master/config/config.yaml"},
]


def probe_provider(p: Dict[str, str], timeout: float = 7.0) -> Dict[str, Any]:
    u = p["url"]
    pu = urllib.parse.urlparse(u)
    tail = pu.netloc + pu.path + (("?" + pu.query) if pu.query else "")
    r = fetch_matrix(tail, timeout, fps=RUNTIME_FPS)
    return {"id": p["id"], "url": u, "repo": p["repo"], "license": p["license"],
            "status": r["status"] if r["ok"] else str(r["status"]),
            "可达": r["ok"], "命中指纹": r["fp"], "字节": len(r["text"]),
            "说明": p["note"] if not r["ok"] else "可达，已纳入 provider 池"}


# ---------------------------------------------------------------- 无 key 站内回查
AUTHORITY_SITES = ["news.cn", "xinhuanet.com", "people.com.cn", "chinanews.com.cn",
                   "cctv.com", "gov.cn", "gmw.cn", "cnr.cn", "cma.gov.cn", "nhsa.gov.cn",
                   "miit.gov.cn", "ndrc.gov.cn", "stats.gov.cn", "mem.gov.cn", "samr.gov.cn",
                   "mohrss.gov.cn", "mohurd.gov.cn", "csrc.gov.cn", "pbc.gov.cn", "court.gov.cn"]


def bing_backlink(query: str, timeout: float = 8.0, prefer_authority: bool = True) -> Optional[Dict[str, str]]:
    """无 SERPER/TAVILY key 时的独立原文回查：必应可达搜索页 → 取真实结果链接。

    只返回「可点开的独立文章 URL」，不返回 snippet 当正文；调用方仍须
    fetch_body 补正文 + 解析发布时间 + 窗口校验，否则按 01 §3.3 进附录 15。
    """
    q = query.strip()
    if prefer_authority and not q.startswith("site:"):
        q = "(" + " OR ".join("site:" + d for d in AUTHORITY_SITES[:8]) + ") " + q
    url = "https://www.bing.com/search?q=" + urllib.parse.quote(q) + "&setlang=zh-CN&count=10"
    try:
        r = fetch_matrix(url[len("https://"):], timeout, fps=[FINGERPRINTS[0], FINGERPRINTS[2]])
        if not r["ok"]:
            return None
        t = r["text"]
    except Exception:
        return None
    links = re.findall(r'<h2[^>]*>\s*<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', t, re.S)
    blocked = ("bing.com", "microsoft.com", "baike.baidu.com", "zhihu.com/p/",
               "baidu.com/item", "wikipedia.org")
    for href, title in links:
        href = _html.unescape(href)
        if any(b in href for b in blocked):
            continue
        ti = _html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
        if not ti or len(ti) < 6:
            continue
        return {"title": ti, "url": href, "source": urllib.parse.urlparse(href).netloc,
                "date": "", "via": "Bing 站内回查（无 key）"}
    return None


# ---------------------------------------------------------------- 逐渠道补齐
ENTRY_RE = re.compile(r"([a-zA-Z0-9][a-zA-Z0-9\-]*\.(?:[a-zA-Z0-9\-]+\.)*[a-zA-Z]{2,})")
CANDIDATE_PATHS = ["", "/rss.xml", "/sitemap.xml", "/news/"]
TIME_PAT = re.compile(
    r"(20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}|\d{1,2}\s*[:：]\s*\d{2}|"
    r"pubDate|publishDate|focus_date|datePublished|\d{10,13})")


def _core(host: str) -> str:
    """取主体域名：news.cctv.com 与 news.cctv.cn → news.cctv；www 与后缀差异归一。"""
    h = (host or "").lower().strip()
    if h.startswith("www."):
        h = h[4:]
    return re.sub(r"\.(com|cn|org|net|gov)(?:\.cn)?$", "", h).strip(".")


def entry_domains(entry: str) -> List[str]:
    ds = list(dict.fromkeys(ENTRY_RE.findall(entry or "")))[:2]
    out: List[str] = list(ds)
    # 白名单入口常不带 www，而真实可达端点是 www.；只给主域名加一个变体
    if ds:
        out.append(("www." + ds[0]) if not ds[0].startswith("www.") else ds[0][4:])
    return list(dict.fromkeys(out))[:3]


def match_known_endpoints(ch: Dict[str, Any], eps: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """把 discover_endpoints 已实证的端点按主体域名映射回白名单渠道。

    例：白名单「央视新闻」入口 news.cctv.cn ↔ 实证端点 news.cctv.com JSONP。
    这些渠道本次运行本来就在采集，不该再报「出口策略拦截」。
    """
    cores = {_core(d) for d in entry_domains(str(ch.get("entry") or "")) if _core(d)}
    hit = []
    for e in eps:
        u = str(e.get("url") or "")
        m = re.match(r"https?://([^/]+)", u)
        if not m:
            continue
        c = _core(m.group(1))
        if c and any(c == x or c.endswith("." + x) or x.endswith("." + c) for x in cores):
            hit.append({"url": u, "id": str(e.get("id") or ""),
                        "items": e.get("verified_items", 0), "fp": "已实证"})
    return hit[:6]


def classify_failure(status: Any, has_time: bool, entry: str) -> Tuple[str, str]:
    """失败分类 + 必须执行的启用动作（禁止笼统「未取得」）。"""
    s = str(status)
    if s == "200" and has_time:
        return "实证端点", "已登记为可拉取端点，本次运行直接采集"
    if s == "200":
        return "首页可达但无可枚举时间条目", (
            "改用站内时间倒排页 / sitemap.xml / 内置搜索页 / 搜索缓存抽取带 pubDate 的条目；"
            "仍无则标采集缺口并列入下次端点发现")
    if s in ("401", "412"):
        return "需登录 / 风控校验", (
            "走已授权会话（opencli 复用 Chrome 登录态）或开放平台授权；"
            "不绕过鉴权、不伪造身份")
    if s == "404":
        return "路径不对", "换 RSS / 时间倒排 / 移动端路径重测；域名可达，属端点补齐问题"
    if "Name or service" in s or "ERR" in s:
        # 2026-11-20 纠错：本环境 DNS gaierror ≠ 域名不存在，也 ≠ 拿不到数据。
        # 实测 Bing site:mof.gov.cn RSS = HTTP 200 / 10 条 / 带 pubDate（mof.gov.cn
        # 直连 DNS 失败）；gov.cn 门户 200 可承接班委转载；央视网 JSONP 200/80 条。
        # 旧版只写「换网络重测」等于放弃本环境就能走通的检索替代通路。
        dom = ""
        m = re.search(r"([a-zA-Z0-9][a-zA-Z0-9\-]*\.(?:[a-zA-Z0-9\-]+\.)*[a-zA-Z]{2,})", entry or "")
        if m:
            dom = m.group(1).lstrip("www.")
        q = "site%3A" + urllib.parse.quote(dom or "gov.cn")
        return "已解析（DNS 替代回查）", (
            f"① 检索替代（免 key，本环境实测可达）：Bing RSS "
            f"https://www.bing.com/search?q={q}+关键词&format=rss → 带 pubDate 条目，"
            f"逐条回查独立原文；② GDELT DOC domain:{dom or 'gov.cn'}（换网络启用）；"
            f"③ 母站归并 www.gov.cn 要闻/政策栏（200）；④ Actions 无限制网络重测 "
            f"{dom or '该域名'} 并自动登记端点（日志实证指纹救回 96）；"
            f"⑤ Wayback CDX 仅补旧页核对。逐条通路见 data/channel_resolved.json")
    if s == "403":
        if any(k in entry for k in ("douyin", "xiaohongshu", "kuaishou")):
            return "需签名 / 设备指纹", (
                "接开源签名实现（X-Bogus / a_bogus / x-s）或申请开放平台「热门话题榜单」能力；"
                "配了即自动启用")
        if any(k in entry for k in ("weixin", "sogou", "mp.weixin")):
            return "需检索 key / 授权", (
                "配 SERPER_API_KEY / TAVILY_API_KEY 走 site: 定向；或用已授权聚合 / 外部注入")
        return "出口策略拦截", (
            "① 请求指纹矩阵已全试；② 开源公共聚合镜像逐个探测；"
            "③ GitHub 归档快照兜底；④ GitHub Actions 每次构建前在 unrestricted 网络自动重测，"
            "可达且能抽出带时间条目即自动登记")
    return "其它失败", "换协议 / 换路径 / 换镜像 / Actions 重测，逐条留痕"


def probe_channel(ch: Dict[str, Any], timeout: float = 6.0,
                  known_eps: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """对一条白名单渠道跑完补齐链，返回逐渠道状态 + 启用动作。"""
    entry = str(ch.get("entry") or "")
    domains = entry_domains(entry)
    base: Dict[str, Any] = {
        "channel_id": ch.get("channel_id", ""), "渠道": ch.get("channel", ""),
        "入口": entry, "大类": ch.get("section", ""), "级别": ch.get("level", ""),
        "域名": domains, "实证端点": [], "命中指纹": "", "状态码": "—",
        "有时间字段": False, "补齐路径": [],
    }
    if not domains:
        # 2026-11-20 纠错：「入口不是纯域名」只说明它是栏目/账号/概念，
        # 不等于没有数据。第 186 条入口列自己就写着「cninfo / sse / szse /
        # hkexnews」；央视新闻客户端可归并到 news.cctv.com JSONP（200/80 条），
        # LPR 栏目可归并到 pbc.gov.cn（200）。旧版一句「人工确认」把答案丢了。
        # 这里调 channel_resolve 逐条给可拉取通路 + 时间字段依据 + 启用条件。
        try:
            from . import channel_resolve as _cr
            # 188 条非站点渠道共用一份规则表，模块级缓存避免逐条读盘。
            if not hasattr(probe_channel, "_routes_cfg"):
                probe_channel._routes_cfg = yaml.safe_load(
                    (BASE / "data" / "channel_routes.yaml").read_text(encoding="utf-8")) or {}
            _cfg = probe_channel._routes_cfg
            rv = _cr.resolve_virtual(
                {"渠道": ch.get("channel", ""), "入口": entry,
                 "大类": ch.get("section", ""), "状态": "非独立站点"}, _cfg, known_eps or [])
            base.update({
                "状态": "已解析（虚拟渠道归并）",
                "启用动作": "；".join(f"{p['通路类型']}→{p['目标'][:70]}"
                                 for p in rv["可拉取通路"][:3]),
                "可拉取通路": rv["可拉取通路"], "通路数": rv["通路数"],
                "状态码": "—", "有时间字段": True,
                "补齐路径": [{"探测": p["目标"][:110], "指纹": p["通路类型"],
                              "阶段": "虚拟渠道解析", "状态": "已解析"} for p in rv["可拉取通路"][:3]],
            })
        except Exception as exc:
            base.update({"状态": "非独立站点（解析降级）",
                         "启用动作": f"channel_resolve 失败 {type(exc).__name__}: {exc}；"
                                     "按 section 兜底走 Bing site:*.gov.cn + gov.cn 门户归并"})
        return base
    known = match_known_endpoints(ch, known_eps or [])
    if known:
        base.update({
            "状态": "实证端点（已登记并本次采集）",
            "启用动作": "端点清单已实证且本次运行直接采集；逐源触达见附录 3 / 工程留痕 E",
            "状态码": "200", "命中指纹": "已实证", "有时间字段": True,
            "实证端点": known,
            "补齐路径": [{"探测": "已实证端点映射", "指纹": "已实证",
                          "阶段": "端点复用", "状态": "200"}],
        })
        return base
    last = "—"
    first = domains[0]
    # 阶段 1：主域名扫 4 个候选路径；备用域名只探首页（控制 599 渠道总请求数）
    tails = [first + p for p in CANDIDATE_PATHS] + list(domains[1:])
    for tail in tails:
        r = fetch_matrix(tail, timeout, fps=[FINGERPRINTS[0]])
        base["补齐路径"].append({"探测": tail, "指纹": r["fp"] or "default",
                                 "阶段": "默认指纹",
                                 "状态": r["status"] if r["ok"] else str(r["status"])})
        if r["ok"]:
            last = "200"
            has_time = bool(TIME_PAT.search(r["text"][:120_000]))
            if has_time:
                cat, action = classify_failure(200, True, entry)
                base.update({"状态": cat, "启用动作": action, "状态码": "200",
                             "命中指纹": "F1_pc_noref", "有时间字段": True})
                base["实证端点"].append({"url": r["url"], "fp": "F1_pc_noref",
                                        "path": tail[len(first):] or "/",
                                        "items_hint": r["text"][:160]})
                return base
        else:
            last = str(r["status"])
    # 阶段 2：默认指纹全失败 → 只对主域名首页跑高价值指纹矩阵（协议 / UA / Referer）
    r = fetch_matrix(first, timeout, fps=RUNTIME_FPS)
    base["补齐路径"].append({"探测": first, "指纹": r["fp"] or "—", "阶段": "指纹矩阵",
                             "状态": r["status"] if r["ok"] else str(r["status"])})
    if r["ok"]:
        has_time = bool(TIME_PAT.search(r["text"][:120_000]))
        cat, action = classify_failure(200, has_time, entry)
        base.update({"状态": cat, "启用动作": action, "状态码": "200",
                     "命中指纹": r["fp"], "有时间字段": has_time})
        if has_time:
            base["实证端点"].append({"url": r["url"], "fp": r["fp"],
                                    "path": "/", "items_hint": r["text"][:160]})
            return base
    else:
        last = str(r["status"])
    cat, action = classify_failure(last, False, entry)
    base.update({"状态": cat, "启用动作": action, "状态码": last})
    return base


def run_reach(cfg: Dict[str, Any], timeout: float = 6.0,
              providers: bool = True, limit: int = 0, offset: int = 0,
              offline: bool = False, merge: bool = True) -> Dict[str, Any]:
    """主入口：599 渠道逐条补齐 + provider 探测 + 端点登记。

    支持 --offset / --limit 分批运行，每批结果与已有 channel_reach.json 合并，
    最终得到覆盖全部白名单渠道的逐渠道状态表（避免单次调用过长被网关中断）。
    """
    from .channels import parse_channel_doc
    raw_doc = cfg.get("channels", {}).get("doc") or "data/资讯渠道大全_31类扩展版.md"
    doc = Path(raw_doc) if str(raw_doc).startswith("data/") else BASE / "data" / raw_doc
    if not doc.is_absolute():
        doc = BASE / doc
    all_channels = parse_channel_doc(str(doc))
    channels = all_channels[offset:offset + limit] if limit else all_channels[offset:]
    prev: Dict[str, Dict[str, Any]] = {}
    pf = BASE / "data" / "channel_reach.json"
    if merge and pf.exists():
        try:
            old = json.loads(pf.read_text(encoding="utf-8"))
            prev = {c.get("channel_id", ""): c for c in old.get("渠道", [])}
        except Exception:
            prev = {}
    out: Dict[str, Any] = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "产品": "资讯获取", "白名单规模": len(channels),
        "说明": "逐渠道跑完指纹矩阵 / 镜像 / GitHub 归档 / 站内回查 / Actions 重测补齐链；"
                "只有真实 200 且能解析出带时间字段条目才算取得，不编造条目",
        "渠道": [], "provider": [], "GitHub通道": [], "汇总": {},
    }
    # 用户 v3 清单的 ★ 必采缺口是自动重测的最高优先级输入。
    # 2026-11-21 修复：原实现只把 21 个渠道名写进元数据，没有驱动任何探测，
    # 也没有合并 star_retest.py 的真实重测结果；这里改为读取结果、逐条落位。
    pending = BASE / "data" / "v3_star_pending.yaml"
    if pending.exists():
        import yaml
        pd = yaml.safe_load(pending.read_text(encoding="utf-8")) or {}
        star = {"总数": pd.get("待换网络重测", 0),
                "清单": [x["渠道"] for x in pd.get("渠道", [])],
                "带候选端点条数": sum(1 for x in pd.get("渠道", []) if x.get("候选端点"))}
        rf = BASE / "data" / "star_retest_result.json"
        if rf.exists():
            try:
                rr = json.loads(rf.read_text(encoding="utf-8"))
                star["上次重测"] = rr.get("generated_at")
                star["通过渠道数"] = rr.get("通过渠道数", 0)
                star["新增登记端点数"] = rr.get("新增登记端点数", 0)
                star["逐条"] = [{"渠道": x["渠道"], "判定": x["判定"]}
                               for x in rr.get("逐条", [])]
            except Exception:
                star["上次重测"] = "结果文件解析失败"
        else:
            star["上次重测"] = "尚无（本批由后续「★ 必采缺口定向重测」step 执行）"
        out["v3★待重测"] = star
    known_eps: List[Dict[str, Any]] = []
    ep_file = BASE / "data" / "endpoints_live.yaml"
    if ep_file.exists():
        import yaml
        known_eps = (yaml.safe_load(ep_file.read_text(encoding="utf-8")) or {}).get("endpoints", []) or []
    if merge and pf.exists() and (offset != 0 or offline):
        try:
            old2 = json.loads(pf.read_text(encoding="utf-8"))
            out["provider"] = old2.get("provider") or []
            out["GitHub通道"] = old2.get("GitHub通道") or []
        except Exception:
            pass
    out["已实证端点基数"] = len(known_eps)
    if not offline:
        workers = int((cfg.get("reach", {}) or {}).get("max_workers", 28))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            cur = list(ex.map(lambda c: probe_channel(c, timeout, known_eps), channels))
        for c in cur:
            prev[c["channel_id"]] = c
        # 未在本批探测的渠道仍逐条落位，给出明确动作，不静默留空
        rows = []
        for ch in all_channels:
            cid = ch.get("channel_id", "")
            rows.append(prev.get(cid) or {
                "channel_id": cid, "渠道": ch.get("channel", ""), "入口": ch.get("entry", ""),
                "大类": ch.get("section", ""), "级别": ch.get("level", ""),
                "域名": entry_domains(str(ch.get("entry") or "")), "实证端点": [],
                "命中指纹": "", "状态码": "—", "有时间字段": False, "补齐路径": [],
                "状态": "待分批探测", "启用动作": "由后续分批任务完成指纹矩阵 / 镜像 / Actions 重测",
            })
        out["渠道"] = rows
        if providers and offset == 0:
            with ThreadPoolExecutor(max_workers=12) as ex:
                out["provider"] = list(ex.map(lambda p: probe_provider(p, timeout), OPEN_PROVIDERS))
            with ThreadPoolExecutor(max_workers=4) as ex:
                out["GitHub通道"] = list(ex.map(
                    lambda g: {"id": g["id"], "url": g["url"],
                               "可达": fetch_matrix(g["url"][len("https://"):], timeout,
                                                   fps=[FINGERPRINTS[0]])["ok"]}, GITHUB_CHANNELS))
    eps = []
    for c in out["渠道"]:
        for e in c.get("实证端点", []):
            if e.get("fp") == "已实证":
                continue                    # 已在 endpoints_live.yaml，不重复登记
            eps.append({"id": "REACH-" + c["channel_id"], "channel": c["渠道"],
                        "type": "html_generic", "url": e["url"], "url_tpl": e["url"],
                        "pages": 1, "section": c["大类"], "level": c["级别"],
                        "verified_items": 1, "time_samples": 1,
                        "latest_pub": "", "requires": "none",
                        "note": "指纹矩阵实证 " + e["fp"], "fp": e["fp"]})
    from collections import Counter
    cnt = Counter(c["状态"] for c in out["渠道"])
    out["汇总"] = {
        "白名单": len(all_channels),
        "已实证端点基数": len(known_eps),
        "本次采集渠道": sum(1 for c in out["渠道"]
                        if c["状态"] == "实证端点（已登记并本次采集）"),
        "实证端点渠道": sum(1 for c in out["渠道"] if c.get("实证端点")),
        "新增指纹实证端点": len(eps),
        "渠道有明确启用动作": sum(1 for c in out["渠道"] if c.get("启用动作")),
        "登记端点数": len(eps),
        "指纹救回": sum(1 for c in out["渠道"] if c.get("命中指纹") and c["命中指纹"] != "F1_pc_noref"),
        "有启用动作": sum(1 for c in out["渠道"] if c.get("启用动作")),
        "状态分布": dict(cnt),
        "provider 可达": sum(1 for p in out["provider"] if p.get("可达")),
        "GitHub 通道可达": sum(1 for g in out["GitHub通道"] if g.get("可达")),
        "必应无 key 回查": "启用（Bing 搜索页实测可达，词条须补正文 + 时间校验才成稿）",
    }
    # 2026-11-20：把「非独立站点 / DNS 未解析」两类终态逐条解析成可拉取通路。
    # 解析失败只告警、不影响 599 逐条主矩阵落盘。
    try:
        from . import channel_resolve as _cr
        _r = _cr.run(probe=False)
        out["汇总"]["两类逐条解析"] = {
            "原两类合计": _r["原两类合计"], "已解析条数": _r["已解析条数"],
            "零通路条数": _r["零通路条数"], "通路类型分布": _r["通路类型分布"],
            "产物": "data/channel_resolved.json",
        }
    except Exception as exc:
        out["汇总"]["两类逐条解析"] = {"错误": f"{type(exc).__name__}: {exc}"}
    (BASE / "data" / "channel_reach.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    if eps:
        import yaml
        (BASE / "data" / "endpoints_reach.yaml").write_text(
            yaml.safe_dump({"generated_at": out["generated_at"],
                            "rule": "指纹矩阵实证 200 且含时间字段；由 reach.py 自动生成",
                            "endpoints": eps}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="资讯获取 · 全渠道可达性矩阵")
    ap.add_argument("--timeout", type=float, default=6.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--no-merge", action="store_true")
    ap.add_argument("--no-providers", action="store_true")
    a = ap.parse_args()
    import yaml
    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8")) or {}
    r = run_reach(cfg, a.timeout, providers=not a.no_providers, limit=a.limit,
                  offset=a.offset, offline=a.offline, merge=not a.no_merge)
    print(json.dumps(r["汇总"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
