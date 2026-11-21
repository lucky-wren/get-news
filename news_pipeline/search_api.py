"""统一搜索 API 层（开源热榜源 + 搜索 API → 高质量独立原文）。

设计目标（用户 2026-11-20：「结合开源仓库方案、搜索 API，要高质量的结果数据」）：

1. **不是「取第一条」**：每个 query 拉多个候选，按可复算规则打分选最优。
   分数 = 权威域名 + 窗口内发布时间 + snippet 事实性 + query 贴合度 − 噪声 URL。
   旧 `collectors.lookup_article` 只 `return` 第一条结果，质量完全碰运气。
2. **多 provider 优雅降级**：Serper（有 key）→ Tavily（有 key）→ **Bing 站内 HTML
   （无 key，本沙盒实测 HTTP 200 且可解析 `<li class="b_algo">`）** → 搜狗。
   无 key 也能真回查，不再直接返回 None；每个 provider 的成败逐条留痕。
3. **不吃 snippet**（规格硬约束）：搜索结果只提供 URL 与「候选事实」，
   必须由 `article.fetch_article_text` / `free_sources.jina_reader_text` 从独立原文
   URL 补正文；补不到就标「摘要未取得」，绝不拿搜索摘要当正文。
4. **发布时间必须落在窗口内**：Bing 结果常带「2025年12月26日」这类旧页，
   往年内容一律剔除并留痕，绝不为了凑数把旧闻当当日新闻。
5. **配额 + 缓存**：Serper 免费 2,500 次一次性；同 query 批次内只调一次，
   `max_calls` 硬上限，超出走缓存或跳过并留痕。
6. **纯代码**：所有打分是词组/正则/数值规则，不调用任何模型语义服务。

模块级 `TRACE` 收集逐次调用证据，由 pipeline 写进 Excel「工程留痕 H」。
"""
from __future__ import annotations

import html
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# ---------------------------------------------------------------- 权威域名
# 与需求①「跟衣食住行 / 医保社保 / 工厂制药医药 / AI / 新能源 / 裁员 / 地方债 /
# 房地产 / 倒闭 / 社会议题贴近」相称的一手信源；命中即加分（可复算，不靠语义判断）。
AUTHORITY: List[Tuple[str, float]] = [
    # 政策原文与部委
    ("gov.cn", 30), ("nhc.gov.cn", 28), ("nhsa.gov.cn", 28), ("mohrss.gov.cn", 28),
    ("nmpa.gov.cn", 28), ("ndrc.gov.cn", 27), ("mof.gov.cn", 27), ("pbc.gov.cn", 27),
    ("csrc.gov.cn", 27), ("mohurd.gov.cn", 26), ("mot.gov.cn", 26), ("mem.gov.cn", 26),
    ("samr.gov.cn", 26), ("nea.gov.cn", 26), ("miit.gov.cn", 26), ("stats.gov.cn", 26),
    ("cma.gov.cn", 26), ("chinatax.gov.cn", 25), ("mofcom.gov.cn", 25),
    ("court.gov.cn", 25), ("spp.gov.cn", 25), ("mps.gov.cn", 25), ("ccdi.gov.cn", 24),
    # 央媒与主流媒体
    ("xinhuanet.com", 26), ("news.cn", 26), ("people.com.cn", 26), ("cctv.com", 25),
    ("cctv.cn", 25), ("chinanews.com.cn", 24), ("chinanews.com", 24),
    ("gmw.cn", 22), ("ce.cn", 22), ("cyol.com", 22), ("thepaper.cn", 22),
    ("cls.cn", 21), ("yicai.com", 21), ("stcn.com", 20), ("cnstock.com", 20),
    ("sina.com.cn", 15), ("163.com", 14), ("qq.com", 14), ("sohu.com", 12),
    ("jd.com", 4), ("taobao.com", 4),
]
# 明确低质 / 非独立原文的 URL 形态
BAD_URL = re.compile(
    r"\.(pdf|doc|docx|xls|xlsx|ppt|zip|rar)(?:\?|$)|\.pdf/|/search\?|/s\?|"
    r"baike\.|zhihu\.com/question|weibo\.com/\d|tieba\.baidu|douyin\.com/video|"
    r"xiaohongshu\.com/explore|/tag/|/topic/|/special/|javascript:|"
    r"/(?:xzt|zt|kpbd|baike|wiki|subject|special|column|lanmu)/|"
    r"/(?:index|default|home|list|column|channel)\.(?:html?|shtml|jsp|php)$", re.I)
# 文章页特征：路径里出现 6 位以上连续数字（日期 / 文章 ID）或 /content、/art、/detail。
# 注意：单纯 .html 结尾 **不算** 文章页——实测 Bing 会返回
# nmc.cn/publish/country/warning/cold.html 这类常设栏目页冒充新闻。
ARTICLE_URL = re.compile(
    r"/20\d{2}[-/_]\d{1,2}|/\d{4,}|/content[_-]|/art[_-]|/detail", re.I)
# UGC 专栏 / 聚合号：不是独立权威原文，实测会被 Bing 选中（zhuanlan.zhihu.com/p/…）
UGC_HOST = re.compile(
    r"zhuanlan\.zhihu\.com|zhihu\.com/(?:question|p)/|baijiahao\.baidu\.com|"
    r"weibo\.com/|tieba\.baidu\.com|douyin\.com|xiaohongshu\.com|"
    r"sohu\.com/a/|163\.com/dy/|toutiao\.com/(?:w|i)\d|"
    # 本批实测漏网：百度知道问答、CSDN/博客园/简书等技术博客，均非新闻原文
    r"zhidao\.baidu\.com|baike\.baidu\.com|wenku\.baidu\.com|"
    r"blog\.csdn\.net|\.csdn\.net|blog\.51cto\.com|cnblogs\.com|"
    r"jianshu\.com|segmentfault\.com|juejin\.cn", re.I)


def _is_generic_title(title: str) -> bool:
    """「寒潮 -中国气象局政府门户网站」这类栏目 / 百科名不是新闻标题：
    短、无数字、无主体动作词。命中即拒绝，避免靠权威域名蒙混过关。"""
    t = (title or "").strip()
    if not t or len(t) > 24:
        return False
    if re.search(r"\d", t):
        return False
    action = re.search(
        r"发布|公布|宣布|决定|发生|造成|导致|下调|上调|调整|停产|裁员|破产|获批|"
        r"实施|召回|预警|启动|暂停|恢复|通报|回应|预计|同比|环比|新增|减少|关闭|"
        r"通车|涨价|降价|补贴|缴费|报销|投产|签署|通过|落地|受理|立案|判决|处罚|"
        r"疏散|救援|死亡|受伤|失联|开售|开工|竣工|问世|上市|获批|通知|出台", t)
    return not action


def _is_index_url(url: str) -> bool:
    """域名根 / 栏目首页不是独立原文；实测 Bing 会返回 nhsa.gov.cn 官网首页充数。"""
    try:
        p = urllib.parse.urlparse(url)
        path = (p.path or "/").rstrip("/")
        if not path:
            return True
        segs = [x for x in path.split("/") if x]
        if len(segs) <= 1 and not ARTICLE_URL.search(path):
            return True
        if ARTICLE_URL.search(path):
            return False
        # 静态 .html 但路径无数字 ID（如 /publish/country/warning/cold.html）→ 常设栏目页
        if re.search(r"\.s?html?$", path, re.I):
            return True
        # 多段但全是纯字母栏目名、且无数字 ID → 栏目页
        if len(segs) <= 3 and all(re.fullmatch(r"[A-Za-z_-]{1,20}", x) for x in segs):
            return True
        return False
    except Exception:
        return True
# snippet 里的「事实锚点」：数字 / 金额 / 人数 / 主体动作 / 结果
FACT = re.compile(
    r"\d|％|%|万|亿|元|人|起|次|岁|吨|公里|平方|发布|公布|宣布|决定|发生|造成|导致|"
    r"下调|上调|调整|停产|裁员|破产|获批|实施|开工|竣工|召回|预警|启动|暂停|恢复|"
    r"通报|回应|要求|预计|同比|环比|新增|减少|关闭|通车|涨价|降价|补贴|缴费|报销|"
    r"投产|签署|通过|落地|受理|立案|判决|处罚|疏散|救援|死亡|受伤|失联")
# 聚合 / 导航 / 占位句
PLACEHOLDER = re.compile(
    r"详情以原文为准|具体内容以.*?为准|点击[查阅]*原文|阅读原文|责任编辑|"
    r"原标题|相关新闻|延伸阅读|更多报道|图片来源|订阅|下载客户端|返回顶部")
DATE_CN = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
DATE_NUM = re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})")
REL_DATE = re.compile(r"(\d{1,2})\s*(小时|天|分钟)前")

# ---------------------------------------------------------------- 数据结构
@dataclass
class SearchHit:
    title: str = ""
    url: str = ""
    snippet: str = ""
    source: str = ""
    date: str = ""
    provider: str = ""
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)
    candidates: int = 0

    def as_legacy(self) -> Dict[str, Any]:
        """兼容 collectors.lookup_article 原有返回格式。"""
        return {"title": self.title, "url": self.url, "snippet": self.snippet,
                "source": self.source, "date": self.date,
                "provider": self.provider, "score": round(self.score, 2),
                "candidates": self.candidates}


@dataclass
class TraceRow:
    query: str = ""
    provider: str = ""
    status: str = ""            # 成功 / 无结果 / HTTP错误 / 超时 / 跳过配额
    candidates: int = 0
    chosen_url: str = ""
    chosen_score: float = 0.0
    reason: str = ""
    elapsed: float = 0.0

    # 2026-11-20 Actions 实测：缓存命中「空结果」时旧代码把 chosen_url 写成 int 0
    # （`hits[0].url if hits else 0`），12 分钟采集跑完后 pipeline 调 trace_rows()
    # 在 `self.chosen_url[:100]` 抛 TypeError: 'int' object is not subscriptable，
    # Excel 未落盘、整批白跑。留痕是诊断信息，任何脏字段都不该让主链路致命。
    @staticmethod
    def _s(v: Any, n: int) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v[:n]
        if isinstance(v, (int, float, bool)):
            return str(v)[:n]          # 如实转写数值占位，不伪装成 URL
        return ""

    @staticmethod
    def _num(v: Any) -> float:
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    def as_row(self) -> List[Any]:
        return [self._s(self.query, 60), self._s(self.provider, 40),
                self._s(self.status, 40), int(self._num(self.candidates)),
                self._s(self.chosen_url, 100), round(self._num(self.chosen_score), 2),
                self._s(self.reason, 120), round(self._num(self.elapsed), 3)]


TRACE: List[TraceRow] = []
_CACHE: Dict[str, List[SearchHit]] = {}
_CALLS = {"serper": 0, "tavily": 0, "keyless": 0, "bing": 0, "sogou": 0, "total": 0}


def reset() -> None:
    TRACE.clear()
    _CACHE.clear()
    for k in _CALLS:
        _CALLS[k] = 0


def stats() -> Dict[str, Any]:
    # 「成稿」只数真正选中了 URL 的留痕；provider「拉回候选」那条 status 也是成功，
    # 但不该重复计入成稿数。
    made = sum(1 for t in TRACE
               if getattr(t, "status", "") == "成功" and getattr(t, "chosen_url", ""))
    return {"调用": dict(_CALLS), "缓存命中": len(_CACHE),
            "逐次留痕": len(TRACE), "成稿": made,
            "失败": sum(1 for t in TRACE
                       if getattr(t, "status", "") not in ("成功", "跳过", "cache"))}


# ---------------------------------------------------------------- HTTP
def _http(url: str, *, timeout: float = 8.0, method: str = "GET",
          payload: dict | None = None, headers: dict | None = None) -> Tuple[int, str, str]:
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9",
         "Accept": "application/json,text/html,*/*"}
    if headers:
        h.update(headers)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    try:
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            raw = r.read(400000)
            cs = r.headers.get_content_charset() or "utf-8"
            return r.status, raw.decode(cs, "replace"), ""
    except urllib.error.HTTPError as e:
        return e.code, "", f"HTTP {e.code}"
    except Exception as e:
        return 0, "", f"{type(e).__name__}: {str(e)[:80]}"


def _text(s: str) -> str:
    s = re.sub(r"<script[^>]*>.*?</script>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else set()


def _strip_query_date(s: str) -> str:
    """贴合度计算前去掉 query 尾部的日期串：'医保目录 2026-11-19' 里的
    数字 2-gram 会稀释主题词，导致真实新闻标题被误判「几乎无关」。"""
    return re.sub(r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}", " ", s or "").strip()


def _domain(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return ""


# ---------------------------------------------------------------- Providers
def _p_serper(query: str, key: str, timeout: float, num: int) -> Tuple[List[Dict], str, str]:
    """Serper /news 优先（带 date），失败再退 /search。两条都留痕。"""
    rows: List[Dict] = []
    for path, field in (("news", "news"), ("search", "organic")):
        code, text, err = _http(
            f"https://google.serper.dev/{path}",
            method="POST", timeout=timeout,
            payload={"q": query, "num": num},
            headers={"X-API-KEY": key, "Content-Type": "application/json"})
        if code != 200:
            if not rows:
                return [], "HTTP错误", f"{path} {err or code}"
            continue
        try:
            for a in (json.loads(text).get(field) or [])[:num]:
                rows.append({"title": a.get("title", ""), "url": a.get("link") or a.get("url", ""),
                             "snippet": a.get("snippet") or a.get("description", ""),
                             "source": a.get("source", ""), "date": a.get("date", "")})
        except Exception as e:
            return [], "解析失败", f"{type(e).__name__}: {str(e)[:60]}"
        if rows:
            break
    return (rows, "成功", "") if rows else ([], "无结果", "")


def _p_tavily(query: str, key: str, timeout: float, num: int) -> Tuple[List[Dict], str, str]:
    code, text, err = _http(
        "https://api.tavily.com/search", method="POST", timeout=timeout,
        payload={"api_key": key, "query": query, "topic": "news",
                 "days": 2, "max_results": num, "include_answer": False},
        headers={"Content-Type": "application/json"})
    if code != 200:
        return [], "HTTP错误", err or f"HTTP {code}"
    try:
        rows = [{"title": a.get("title", ""), "url": a.get("url", ""),
                 "snippet": a.get("content", ""), "source": _domain(a.get("url", "")),
                 "date": a.get("published_date", "")}
                for a in (json.loads(text).get("results") or [])[:num]]
        return (rows, "成功", "") if rows else ([], "无结果", "")
    except Exception as e:
        return [], "解析失败", f"{type(e).__name__}: {str(e)[:60]}"


def _parse_bing(text: str, num: int) -> List[Dict]:
    """Bing 结果页 `<li class="b_algo">`：实测 HTTP 200，10 条带日期，无 key 可用。"""
    out: List[Dict] = []
    blocks = re.findall(r'<li\s+class="b_algo".*?</li>', text, re.S | re.I)
    if not blocks:
        blocks = re.findall(r'<li\s+class="[^"]*b_algo[^"]*".*?</li>', text, re.S | re.I)
    for b in blocks[:num]:
        h2 = re.search(r"<h2[^>]*>(.*?)</h2>", b, re.S | re.I)
        link = re.search(r'<h2[^>]*>\s*<a[^>]+href="(https?://[^"]+)"', b, re.S | re.I)
        if not link:
            link = re.search(r'href="(https?://[^"]+)"', b)
        title = _text(h2.group(1)) if h2 else ""
        url = html.unescape(link.group(1)) if link else ""
        if not title or not url:
            continue
        # 2026-11-22 线上 H-c：Bing 返回 bing.com/ck/a?!&&p=... 追踪壳 /
        # DefaultSearchEngine / version / RelatedSearch 等自有页。不还原的话
        # 硬闸把 104 次 bing 调用全判「候选不合格」→ 0 成稿。纯解码，不猜 URL。
        from .url_resolve import resolve_search_url
        url = resolve_search_url(url)
        if not url:
            continue
        # 摘要：优先 <p>，其次整块去掉标题后的文本
        p = re.search(r"<p[^>]*>(.*?)</p>", b, re.S | re.I)
        snip = _text(p.group(1)) if p else ""
        if not snip:
            snip = _text(b)[len(title):].strip()[:300]
        # 日期：块内中文年月日 / 数字日期 / 「N小时前」
        dm = DATE_CN.search(b) or DATE_NUM.search(b)
        date = (f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
                if dm else "")
        if not date:
            um = re.search(r"/(20\d{2})[-/]?(\d{2})[-/]?(\d{2})", url)
            if um:
                date = f"{um.group(1)}-{um.group(2)}-{um.group(3)}"
        src = _domain(url)
        out.append({"title": title, "url": url, "snippet": snip,
                    "source": src, "date": date})
    return out


def _p_bing(query: str, timeout: float, num: int, site: str = "") -> Tuple[List[Dict], str, str]:
    q = f"{query} site:{site}" if site else query
    url = ("https://www.bing.com/search?q=" + urllib.parse.quote(q)
           + f"&count={num}&setlang=zh-CN&ensearch=0")
    code, text, err = _http(url, timeout=timeout,
                            headers={"Referer": "https://www.bing.com/"})
    if code != 200:
        return [], "HTTP错误", err or f"HTTP {code}"
    rows = _parse_bing(text, num)
    return (rows, "成功", "") if rows else ([], "无结果", "页面无 b_algo 结果块")


def _p_sogou(query: str, timeout: float, num: int) -> Tuple[List[Dict], str, str]:
    url = "https://www.sogou.com/web?query=" + urllib.parse.quote(query)
    code, text, err = _http(url, timeout=timeout)
    if code != 200:
        return [], "HTTP错误", err or f"HTTP {code}"
    out = []
    for b in re.findall(r'<div[^>]+class="[^"]*(?:vrwrap|rb|results)[^"]*".*?</div>\s*</div>',
                        text, re.S | re.I)[:num]:
        h = re.search(r"<h3[^>]*>(.*?)</h3>", b, re.S | re.I)
        l = re.search(r'href="(https?://[^"]+)"', b)
        if h and l:
            out.append({"title": _text(h.group(1)), "url": html.unescape(l.group(1)),
                        "snippet": _text(b)[:220], "source": _domain(html.unescape(l.group(1))),
                        "date": ""})
    return (out, "成功", "") if out else ([], "无结果", "搜狗返回反爬壳页")


# ---------------------------------------------------------------- 质量打分
def score_hit(hit: SearchHit, query: str, win: Any) -> SearchHit:
    """可复算规则打分。所有加减项都写进 reasons，附录逐条可查。"""
    s, why = 0.0, []
    dom = _domain(hit.url)

    # 1) 权威域名（最长后缀匹配，避免 gov.cn 被子域误判）
    best, who = 0.0, ""
    for suffix, pts in AUTHORITY:
        if dom == suffix or dom.endswith("." + suffix):
            if pts > best:
                best, who = pts, suffix
    if best:
        s += best
        why.append(f"权威域名 {who} +{best}")
    elif dom:
        s += 2
        why.append(f"其他域名 {dom} +2")

    # 2) 发布时间：窗口内大加分；往年重罚；未取得小罚
    d = _naive(_effective_date(hit))
    if d and win is not None:
        ws, we = _naive(win.start), _naive(win.end)
        if not _parse_date(hit.date) and hit.date == "":
            why.append(f"date 缺失，改按 URL 日期目录 {d:%Y-%m-%d} 判定")
        try:
            if ws < d <= we:
                s += 35
                why.append(f"发布时间在窗口内 +35（{d:%Y-%m-%d}）")
            elif d < ws:
                days = (ws - d).days
                pen = min(60, 18 + days // 7)
                s -= pen
                why.append(f"旧闻 {d:%Y-%m-%d}（{days}天前）−{pen}")
            else:
                s -= 12
                why.append(f"晚于窗口右界 {d:%Y-%m-%d} −12")
        except Exception as exc:
            why.append(f"时间比较异常（如实记录，不静默）：{type(exc).__name__}")
            s -= 10
    elif not hit.date:
        s -= 8
        why.append("搜索结果未给发布时间，无法确认落在窗口内 −8")
    else:
        s -= 4
        why.append("日期串无法归一化 −4")

    # 3) URL 形态（首页 / 栏目页不是独立原文，Bing 实测会拿官网首页充数）
    if UGC_HOST.search(hit.url):
        s -= 40
        why.append("UGC 专栏 / 聚合号，非权威独立原文 −40")
    if _is_index_url(hit.url):
        s -= 35
        why.append("域名根 / 栏目首页，非独立原文 −35")
    elif BAD_URL.search(hit.url):
        s -= 22
        why.append("非独立原文 URL（PDF/搜索页/话题页/聚合）−22")
    elif ARTICLE_URL.search(hit.url):
        s += 8
        why.append("文章页 URL（含日期 / 文章 ID） +8")
    elif hit.url.startswith("https://"):
        s += 3
        why.append("HTTPS 独立 URL +3")

    # 4) snippet 事实性（不是正文，只用于「是否值得去补正文」的判断）
    sn = PLACEHOLDER.sub("", hit.snippet or "")
    n_fact = len(FACT.findall(sn))
    if n_fact >= 3:
        s += 12
        why.append(f"候选摘要含 {n_fact} 处事实锚点 +12")
    elif n_fact >= 1:
        s += 6
        why.append(f"候选摘要含 {n_fact} 处事实锚点 +6")
    else:
        s -= 8
        why.append("候选摘要无数字 / 主体动作 −8")
    if len(sn) < 20:
        s -= 6
        why.append("候选摘要过短 −6")

    # 5) query 贴合度（2-gram Jaccard，纯代码）
    qb, tb = _bigrams(_strip_query_date(query)), _bigrams(hit.title)
    if qb and tb:
        j = len(qb & tb) / len(qb | tb)
        if j < 0.04:
            s -= 18
            why.append(f"标题与 query 几乎无关 Jaccard {j:.2f} −18")
        else:
            pts = round(j * 25, 1)
            s += pts
            why.append(f"标题与 query 2-gram Jaccard {j:.2f} +{pts}")

    # 6) 标题复读 / 过短
    if len(hit.title) < 8:
        s -= 10
        why.append("标题过短 −10")

    hit.score, hit.reasons = round(s, 2), why
    return hit


def _date_from_url(url: str) -> Optional[datetime]:
    """从 URL 路径里的日期目录反推发布期，堵住 Bing 不给 date 字段的旧闻。

    实测漏网样本：
      /zhengce/202608/content_7078874.htm  → 2026-08（早于窗口左界）
      /xjslt/c114445/202509/xxx.shtml      → 2025-09
      /2026-11/19/content_123.htm        → 2026-11-19
    只用于**窗口校验 / 旧闻拦截**，不写进成果的「发布时间」列（那仍需源站字段）。
    """
    try:
        path = urllib.parse.urlparse(url).path
    except Exception:
        return None
    m = re.search(r"/(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?:/|$)", path)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            return None
    m = re.search(r"/(20\d{2})(\d{2})(?:/|$)", path)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), 1)
        except Exception:
            return None
    return None


def _url_has_day(url: str) -> bool:
    try:
        p = urllib.parse.urlparse(url).path
    except Exception:
        return False
    return bool(re.search(r"/(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?:/|$)", p))


def _naive(dt: Optional[datetime]) -> Optional[datetime]:
    """把 Window 的 aware datetime 与搜索解析出的 naive datetime 统一到无时区。

    真实跑批里 Window.start/end 带 ZoneInfo('Asia/Shanghai')，而 date/URL 解析
    出的是 naive datetime；直接比较会抛 TypeError，又被 `except Exception: pass`
    静默吞掉 —— 结果是「旧闻判定」「窗口内加分」在真跑批里全部失效，
    2026-08 政策旧页能拿到 51 分混进选中列表。这个 bug 必须用可复跑用例锁住。
    """
    if dt is not None and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def _effective_date(hit: "SearchHit") -> Optional[datetime]:
    """窗口判定用「date 字段」与「URL 日期目录」综合判定。

    - URL 含**年月日**目录（/2026-11/19/…）→ 与 date 取较早者；
    - URL 只有**年月**（/zhengce/202608/…）→ 若与 date 同月，以更精确的 date 为准；
      不同月则说明 date 是旁注串，按较早的月份判（实测漏网：Bing 把
      「2026年11月19日」旁注当 date，URL 却是 2026-08 政策旧页）；
    - date 缺失 → 直接用 URL 日期。
    """
    a, b = _parse_date(hit.date), _date_from_url(hit.url)
    if a and b:
        if _url_has_day(hit.url):
            return min(a, b)
        if (a.year, a.month) == (b.year, b.month):
            return a
        return min(a, b)
    return a or b


def _parse_date(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = str(s).strip()
    m = DATE_CN.search(s) or DATE_NUM.search(s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            return None
    m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            pass
    return None


def _reject_reason(hit: SearchHit, win: Any, query: str = "") -> str:
    """硬闸：即便得分最高，也要拦住明确不合格的候选。"""
    if not hit.url.startswith(("http://", "https://")):
        return "URL 非法"
    if _is_index_url(hit.url):
        return "域名根 / 栏目首页（非独立原文）"
    if BAD_URL.search(hit.url):
        return "非独立原文 URL"
    if UGC_HOST.search(hit.url):
        return "UGC 专栏 / 聚合号（非权威独立原文）"
    # 无发布时间不直接硬拦：Bing 结果常漏 date，但真实新闻页 URL 带日期目录
    # （/2026-11/19/content_xxx.htm）。改用打分惩罚 −8 + min_score 门槛控制，
    # 避免把「有 URL 无 date」的真实新闻一并误杀。
    if _is_generic_title(hit.title):
        return "栏目 / 百科名，非新闻标题"
    if query:
        qb, tb = _bigrams(_strip_query_date(query)), _bigrams(hit.title)
        if qb and tb and len(qb & tb) / len(qb | tb) < 0.04:
            return "标题与检索词几乎无关"
    d = _naive(_effective_date(hit))
    if d and win is not None:
        ws, we = _naive(win.start), _naive(win.end)
        try:
            if d < ws:
                suffix = "（按 URL 日期目录判定）" if not _parse_date(hit.date) else ""
                return f"旧闻（{d:%Y-%m-%d} 早于窗口左界）{suffix}"
            if d > we:
                return f"晚于窗口右界（{d:%Y-%m-%d}）"
        except Exception as exc:
            return f"时间比较异常（不静默放行）：{type(exc).__name__}"
    if len(PLACEHOLDER.sub("", hit.snippet or "")) < 12:
        return "候选摘要无事实内容"
    return ""


# ---------------------------------------------------------------- 对外主入口
def search_best(query: str, *, win: Any = None, cfg: dict | None = None,
                site: str = "", num: int = 8, timeout: float = 8.0,
                min_score: float = 18.0) -> Optional[SearchHit]:
    """返回本 query 的最优独立原文候选；不合格返回 None 并留痕。

    provider 顺序（配置 `search_api.provider_order` 可改）：
      serper → tavily → bing → sogou
    有 key 的优先；无 key 自动跳到 Bing（本沙盒实测可达）。
    """
    cfg = cfg or {}
    conf = cfg.get("search_api", {}) or {}
    order = [x.strip() for x in str(conf.get("provider_order", "serper,tavily,bing,sogou")).split(",")
             if x.strip()]
    num = int(conf.get("results_per_query", num))
    timeout = float(conf.get("timeout", timeout))
    max_calls = int(conf.get("max_calls", 120))
    min_score = float(conf.get("min_score", min_score))
    default_site = str(conf.get("default_site", site) or "")
    site = site or default_site
    qkey = f"{query}|{site}"

    if qkey in _CACHE:
        hits = _CACHE[qkey]
        TRACE.append(TraceRow(query, "cache", "成功" if hits else "无结果",
                              len(hits), str(hits[0].url or "") if hits else "", 0.0,
                              "批次内缓存命中，不重复消耗免费额度"))
        return _pick(hits, query, win, min_score)

    all_hits: List[SearchHit] = []
    for prov in order:
        if _CALLS["total"] >= max_calls:
            TRACE.append(TraceRow(query, prov, "跳过配额", 0, "", 0.0,
                                  f"已达 max_calls={max_calls}"))
            break
        t0 = time.time()
        # 只对真正发起 HTTP 请求的 provider 计数：无 key 的 serper/tavily 只留
        # 「跳过」痕迹，不能算调用次数，否则 Excel 里会出现「16 次 Serper 调用」
        # 这种与实际不符的数字，也会白占 max_calls 配额。
        if prov == "serper":
            key = os.environ.get("SERPER_API_KEY", "")
            if not key:
                TRACE.append(TraceRow(query, prov, "跳过", 0, "", 0.0,
                                      "未配 SERPER_API_KEY，未发起请求", round(time.time() - t0, 3)))
                continue
            _CALLS["total"] += 1
            _CALLS[prov] += 1
            rows, status, err = _p_serper(query, key, timeout, num)
        elif prov == "tavily":
            key = os.environ.get("TAVILY_API_KEY", "")
            if not key:
                TRACE.append(TraceRow(query, prov, "跳过", 0, "", 0.0,
                                      "未配 TAVILY_API_KEY，未发起请求", round(time.time() - t0, 3)))
                continue
            _CALLS["total"] += 1
            _CALLS[prov] += 1
            rows, status, err = _p_tavily(query, key, timeout, num)
        elif prov == "keyless":
            # 无 key 主链路：Google News RSS / Bing RSS / GDELT DOC / SearXNG 全部免费公开。
            # 一个 query 拉回多个独立域名原文，按既有 score_hit / 硬闸挑最优；
            # 事件级成稿（多渠道来源列 + 两段摘要）由 keyless_compose.batch_compose 完成。
            # 2026-11-22：线上口径表证明 0a-0 未执行时，这一路把多源证据压成单 URL，
            # 43 次调用 0 成稿还白占配额。0a-0 已跑就跳过，不让单源结果稀释多渠道率。
            if ((cfg or {}).get("keyless", {}) or {}).get("batch_active"):
                TRACE.append(TraceRow(query, prov, "跳过", 0, "", 0.0,
                    "0a-0 已做事件级成稿，此处不再逐条选单 URL（避免重复检索与稀释多渠道）",
                    round(time.time() - t0, 3)))
                continue
            _CALLS["total"] += 1
            _CALLS[prov] = _CALLS.get(prov, 0) + 1
            try:
                from . import keyless_compose as kc
                evs, tr = kc.search_clue(query, cfg=cfg, win=win, resolve=True)
                rows = [{"title": e.title, "url": e.url,
                         "snippet": "", "source": e.source or e.domain,
                         "date": e.pub.isoformat() if e.pub else ""}
                        for e in evs[:max(num * 2, 12)]]
                status = "成功" if rows else "无结果"
                err = f"独立域名 {tr.domains} 个 | {tr.reason[:110]}"
            except Exception as e:
                rows, status, err = [], "失败", f"{type(e).__name__}: {str(e)[:100]}"
        elif prov == "bing":
            _CALLS["total"] += 1
            _CALLS[prov] += 1
            rows, status, err = _p_bing(query, timeout, num, site)
        elif prov == "sogou":
            _CALLS["total"] += 1
            _CALLS[prov] += 1
            rows, status, err = _p_sogou(query, timeout, num)
        else:
            TRACE.append(TraceRow(query, prov, "跳过", 0, "", 0.0, "未知 provider"))
            continue

        el = round(time.time() - t0, 3)
        if status != "成功":
            TRACE.append(TraceRow(query, prov, status, 0, "", 0.0, err, el))
            continue
        hits = [SearchHit(title=r.get("title", ""), url=r.get("url", ""),
                          snippet=r.get("snippet", ""), source=r.get("source", ""),
                          date=r.get("date", ""), provider=prov, candidates=len(rows))
                for r in rows]
        TRACE.append(TraceRow(query, prov, "成功", len(hits), "", 0.0,
                              f"拉回 {len(hits)} 个候选", el))
        all_hits.extend(hits)
        if len(all_hits) >= num:
            break   # 够了就停，不为凑候选把免费额度烧光

    scored = [score_hit(h, query, win) for h in all_hits]
    scored.sort(key=lambda x: -x.score)
    _CACHE[qkey] = scored
    return _pick(scored, query, win, min_score)


def _pick(scored: List[SearchHit], query: str, win: Any,
          min_score: float) -> Optional[SearchHit]:
    for h in scored:
        bad = _reject_reason(h, win, query)
        if bad:
            TRACE.append(TraceRow(query, h.provider, "候选不合格", len(scored),
                                  h.url, h.score, bad))
            continue
        if h.score < min_score:
            TRACE.append(TraceRow(query, h.provider, "低于分数门槛", len(scored),
                                  h.url, h.score,
                                  f"score {h.score} < {min_score}"))
            return None
        TRACE.append(TraceRow(query, h.provider, "成功", len(scored), h.url, h.score,
                              "；".join(h.reasons[:4])))
        return h
    TRACE.append(TraceRow(query, "-", "无合格候选", len(scored), "", 0.0,
                          "全部候选被硬闸拦下（旧闻 / 非独立原文 / 无事实）"))
    return None


def trace_rows() -> List[List[Any]]:
    """逐条容错：单条坏留痕降级为一行说明，不让整批采集结果无法出表。"""
    out: List[List[Any]] = []
    for t in TRACE:
        try:
            if isinstance(t, TraceRow):
                out.append(t.as_row())
            else:
                out.append([str(t)[:60], "-", "留痕对象类型异常", 0, "", 0.0,
                            "TRACE 中混入非 TraceRow 对象", 0.0])
        except Exception as exc:      # noqa: BLE001 - 诊断数据不允许拖垮主链路
            out.append([getattr(t, "query", ""), "-", "留痕序列化失败", 0, "", 0.0,
                        f"{type(exc).__name__}: {exc}"[:120], 0.0])
    return out


def provider_summary() -> List[List[Any]]:
    agg: Dict[str, List[float]] = {}
    for t in TRACE:
        try:
            a = agg.setdefault(str(getattr(t, "provider", "") or "-"), [0, 0, 0.0])
            a[0] += 1
            if getattr(t, "status", "") == "成功" and getattr(t, "chosen_url", ""):
                a[1] += 1
            a[2] = max(a[2], TraceRow._num(getattr(t, "chosen_score", 0)))
        except Exception:            # noqa: BLE001
            continue
    return [[p, int(v[0]), int(v[1]), round(v[2], 2)] for p, v in sorted(agg.items())]


# ---------------------------------------------------------------- 自检
def self_test() -> Dict[str, Any]:
    """离线自检：打分规则、硬闸、Bing 解析器、缓存，不联网也可跑。"""
    errs: List[str] = []
    reset()
    # 构造一个假 win
    class W:
        start = datetime(2026, 11, 18, 20, 0)
        end = datetime(2026, 11, 19, 20, 0)
    w = W()
    good = SearchHit(title="国家医保局发布新版医保目录 新增91种药品",
                     url="https://www.gov.cn/zhengce/202611/content_1.htm",
                     snippet="11月19日，新增91种药品，预计每年为患者减负400亿元。",
                     source="www.gov.cn", date="2026-11-19", provider="bing")
    old = SearchHit(title="2025年医保目录调整通知",
                    url="https://www.gov.cn/zhengce/202512/content_0.htm",
                    snippet="2025年12月26日发布，共新增91种药品。",
                    source="www.gov.cn", date="2025-12-26", provider="bing")
    pdf = SearchHit(title="医保目录全文", url="https://x.cn/a.pdf",
                    snippet="目录全文共91种药品 400亿元", date="2026-11-19", provider="bing")
    q = "医保目录 新增药品"
    index = SearchHit(title="国家医疗保障局", url="https://www.nhsa.gov.cn/",
                      snippet="医保目录新增91种药品，报销比例调整，可异地结算。",
                      source="www.nhsa.gov.cn", date="2026-11-19", provider="bing")
    g, o, p, ix = (score_hit(good, q, w), score_hit(old, q, w),
                   score_hit(pdf, q, w), score_hit(index, q, w))
    if not (g.score > o.score and g.score > p.score and g.score > ix.score):
        errs.append(f"打分排序错: good={g.score} old={o.score} pdf={p.score} index={ix.score}")
    if not _reject_reason(o, w, q):
        errs.append("旧闻未被硬闸拦下")
    if not _reject_reason(p, w, q):
        errs.append("PDF 未被硬闸拦下")
    if not _reject_reason(ix, w, q):
        errs.append("官网首页未被硬闸拦下")
    if _reject_reason(g, w, q):
        errs.append("优质候选被误拦：" + _reject_reason(g, w, q))
    # 实测漏网回归：Bing 把旁注日期当 date，但 URL 是 /202608/ 政策旧页
    conflict = SearchHit(title="国家医保局发布新版医保目录 新增91种药品",
                         url="https://www.gov.cn/zhengce/202608/content_7078874.htm",
                         snippet="11月19日，新增91种药品，预计每年为患者减负400亿元。",
                         source="www.gov.cn", date="2026-11-19", provider="bing")
    if not _reject_reason(conflict, w, q):
        errs.append("date 与 URL 日期目录冲突的旧闻未被拦下")
    # 时区回归：真实 Window 带 ZoneInfo('Asia/Shanghai')，naive 日期直接比较会抛
    # TypeError 并被 except 静默吞掉，导致旧闻闸门在真跑批里失效。
    try:
        from zoneinfo import ZoneInfo
        class WA:
            start = datetime(2026, 11, 18, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            end = datetime(2026, 11, 19, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        wa = WA()
        if not _reject_reason(conflict, wa, q):
            errs.append("aware 窗口下旧闻未被拦（时区比较 bug 回归）")
        if _reject_reason(g, wa, q):
            errs.append("aware 窗口下优质候选被误拦")
    except Exception as exc:
        errs.append(f"aware 窗口用例异常：{type(exc).__name__}: {exc}")
    # Bing 解析器
    fake = ('<li class="b_algo"><h2><a href="https://www.gov.cn/a/2026-11-19/1.htm">'
            '医保目录新增91种药品</a></h2><p>11月19日发布，新增91种药品。</p>'
            '<span>2026年11月19日 · 国家医保局</span></li>')
    rows = _parse_bing(fake, 5)
    if not rows or rows[0]["date"] != "2026-11-19" or "91种" not in rows[0]["snippet"]:
        errs.append(f"Bing 解析异常: {rows[:1]}")
    # 缓存
    _CACHE["x|y"] = [g]
    before = _CALLS["total"]
    r = search_best("x", win=w, cfg={"search_api": {"provider_order": "bing", "max_calls": 0}},
                    site="y")
    if _CACHE.get("x|y") is None:
        errs.append("缓存键被破坏")
    # 2026-11-20 Actions 崩溃回归：缓存空结果时 chosen_url 曾被写成 int 0，
    # 12 分钟采集跑完后 trace_rows() 切片抛 TypeError，Excel 未落盘。
    try:
        _CACHE["空缓存|z"] = []
        search_best("空缓存", win=w,
                    cfg={"search_api": {"provider_order": "bing", "max_calls": 0}},
                    site="z")
        cache_rows = [r for r in trace_rows() if str(r[1]) == "cache"]
        if not cache_rows or str(cache_rows[-1][4]) != "":
            errs.append("空缓存命中未留下 chosen_url 为空的 cache 留痕")
    except Exception as exc:
        errs.append(f"空缓存命中回归异常：{type(exc).__name__}: {exc}")
    # 任意脏字段 / 非 TraceRow 对象都不应让整批出表失败
    try:
        TRACE.append(TraceRow(query="脏字段", provider="x", status="成功",
                              chosen_url=0, chosen_score="abc", elapsed=None))  # type: ignore[arg-type]
        TRACE.append(TraceRow(query=None, provider=None, status=None,
                              chosen_url=None, chosen_score=None, reason=None))  # type: ignore[arg-type]
        TRACE.append("not-a-TraceRow")  # type: ignore[arg-type]
        rows2 = trace_rows()
        if len(rows2) != len(TRACE):
            errs.append(f"trace_rows 丢行：{len(rows2)} / {len(TRACE)}")
        provider_summary()   # 不得抛
        stats()              # 不得抛
    except Exception as exc:
        errs.append(f"脏留痕容错回归异常：{type(exc).__name__}: {exc}")
    return {"通过": not errs, "错误": errs,
            "分数": {"优质": g.score, "旧闻": o.score, "PDF": p.score, "首页": ix.score},
            "Bing解析": len(rows), "留痕条数": len(TRACE)}
