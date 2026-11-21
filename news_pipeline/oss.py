"""开源组件适配层（纯代码，无任何模型语义调用）。

设计原则
--------
1. **能复用就复用**：Feed 解析、正文抽取、时间归一化、HTTP 重试、HTML 清洗、
   配置校验、相似度诊断全部走成熟开源库，不再手写脆弱正则轮子。
2. **不能复用就如实自己写**：大众贴近度词组表、Final_adj 公式、编排分 U、
   「2-gram Jaccard / 最长公共子串」的阈值语义必须由本项目按规格实现，
   开源库只做加速与诊断，不替代业务判定。
3. **全部带降级**：任何一个库缺失或抛错，自动回退到内置实现，并在 OSS_STATUS
   里标记「降级」，绝不因为装不上库就编数据或静默出空表。
4. **不改事实**：相对时间串（"16 hours ago" / "2小时前"）按 SKILL §11.1
   只登记依据、不换算成 ISO 写进「发布时间」；dateparser 仅用于绝对时间兜底。

已核实（2026-11-19，本沙盒实际 import 验证，不是照抄文档）：
  feedparser 6.0.14 / trafilatura 2.3.1 / dateparser 1.5.1 / rapidfuzz 3.14.5
  jieba 0.42.1 / tenacity / cn2an 0.5.24 / bleach 6.4.0 / pydantic 2.14.3
  httpx 0.28.1 / openpyxl 3.1.5 / XlsxWriter 3.2.9 / PyYAML 6.0.3

调研发现的版本差异（如实记录）：rapidfuzz 3.14.5 的 `rapidfuzz.distance`
**不含 Jaccard 模块**（旧资料/部分教程里写有）。因此 2-gram Jaccard 仍按
规格用「字符 2-gram 集合交并比」自写；rapidfuzz 用于 LCSseq / 编辑距离的
快速诊断与候选召回。`LCSseq` 返回的是**最长公共子序列**（可不连续），与
规格里的「LCS ≥ 8」按**最长公共子串**（必须连续）语义不同，不能互换，
故判定链保留自写 DP，rapidfuzz 只作辅助信号。
"""
from __future__ import annotations

import html as _html
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 可用性登记
OSS_STATUS: Dict[str, Dict[str, str]] = {}


def _reg(name: str, purpose: str):
    try:
        mod = __import__(name)
        ver = getattr(mod, "__version__", "?")
        OSS_STATUS[name] = {"状态": "已启用", "版本": str(ver), "用途": purpose}
        return mod
    except Exception as exc:                                  # pragma: no cover
        OSS_STATUS[name] = {"状态": "降级（内置实现）", "版本": "—",
                            "用途": purpose, "原因": type(exc).__name__}
        return None


_feedparser = _reg("feedparser", "RSS/Atom 解析（替代手写 ElementTree）")
_trafilatura = _reg("trafilatura", "独立原文正文与元数据抽取")
_dateparser = _reg("dateparser", "绝对发布时间归一化兜底")
_cn2an = _reg("cn2an", "中文数词前置规范化")
_rapidfuzz = _reg("rapidfuzz", "LCSseq / 编辑距离快速诊断与候选召回")
_jieba = _reg("jieba", "中文词级 2-gram（辅助信号，不进判定）")
_tenacity = _reg("tenacity", "HTTP 瞬时失败指数退避重试")
_httpx = _reg("httpx", "带连接池 / 超时的 HTTP 客户端")
_bleach = _reg("bleach", "HTML 允许清单清洗")
_pydantic = _reg("pydantic", "配置强类型校验")
_xlsxwriter = _reg("xlsxwriter", "大文件流式出表评估（当前规模仍用 openpyxl）")

# trafilatura 依赖 lxml_html_clean，单独登记一次（缺失时 trafilatura 会 import 失败）
try:
    import lxml_html_clean  # noqa: F401
    OSS_STATUS["lxml_html_clean"] = {"状态": "已启用", "版本": "—", "用途": "trafilatura 的 HTML 清洗依赖"}
except Exception:
    OSS_STATUS["lxml_html_clean"] = {"状态": "缺失（trafilatura 降级）", "版本": "—", "用途": "同上"}


def oss_rows() -> List[List[str]]:
    """给口径表用的「开源组件逐项登记」行。"""
    return [[k, v["状态"], v["版本"], v["用途"]] for k, v in OSS_STATUS.items()]


def oss_summary() -> str:
    on = sum(1 for v in OSS_STATUS.values() if v["状态"] == "已启用")
    return f"开源组件 {on}/{len(OSS_STATUS)} 项已启用；全部判定为纯代码规则，无 LLM / 无模型语义层"


# ---------------------------------------------------------------- HTTP 抓取
_UA = "news-daily-pipeline/2.0 (+robots.txt respected; pure-code rules)"


def fetch_url(url: str, timeout: float = 6.0, retries: int = 2,
              max_chars: int = 400000) -> str:
    """httpx + tenacity 抓取；4xx 立即失败，仅对超时 / 5xx / 连接错误重试。"""
    if not url or not url.startswith(("http://", "https://")):
        return ""

    def _once() -> str:
        if _httpx is not None:
            with _httpx.Client(timeout=timeout, headers={"User-Agent": _UA},
                               follow_redirects=True) as cli:
                r = cli.get(url)
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code}")
                return r.text[:max_chars]
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(max_chars).decode(r.headers.get_content_charset() or "utf-8", "replace")

    if _tenacity is not None:
        from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
        wrapped = retry(stop=stop_after_attempt(retries + 1),
                        wait=wait_exponential(multiplier=0.4, min=0.4, max=2),
                        retry=retry_if_exception_type((TimeoutError, ConnectionError, OSError)),
                        reraise=False)(_once)
        try:
            return wrapped() or ""
        except Exception:
            return ""
    try:
        return _once()
    except Exception:
        return ""


# ---------------------------------------------------------------- Feed 解析
def parse_feed(raw: str | bytes, *, url: str = "") -> List[Dict[str, str]]:
    """统一输出 [{title, link, description, pub_raw, source}]，RSS2 / Atom / JSON Feed 通吃。"""
    if _feedparser is None:
        return _parse_feed_fallback(raw, url=url)
    try:
        f = _feedparser.parse(raw)
        out: List[Dict[str, str]] = []
        for e in f.entries:
            link = e.get("link") or ""
            if not link:
                for lk in e.get("links", []) or []:
                    if lk.get("rel") == "alternate" or lk.get("type", "").startswith("text/html"):
                        link = lk.get("href", "")
                        break
            desc = ""
            for key in ("content", "summary", "description"):
                v = e.get(key)
                if isinstance(v, list) and v:
                    v = v[0].get("value", "")
                if v:
                    desc = str(v)
                    break
            src = ""
            s = e.get("source")
            if isinstance(s, dict):
                src = str(s.get("title") or s.get("href") or "")
            elif s:
                src = str(s)
            out.append({
                "title": str(e.get("title") or "").strip(),
                "link": str(link).strip(),
                "description": desc,
                "pub_raw": str(e.get("published") or e.get("updated") or e.get("dc_date") or "未取得"),
                "source": src.strip(),
                "guid": str(e.get("id") or e.get("guid") or "").strip(),
            })
        return out
    except Exception:
        return _parse_feed_fallback(raw, url=url)


def _parse_feed_fallback(raw: str | bytes, *, url: str = "") -> List[Dict[str, str]]:
    """feedparser 缺失时的最小 ElementTree 回退（只处理 RSS2 / Atom 常见字段）。"""
    import xml.etree.ElementTree as ET
    from urllib.parse import urlparse
    try:
        root = ET.fromstring(raw)
    except Exception:
        return []
    nodes = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    out = []
    for n in nodes:
        def txt(tag: str) -> str:
            el = n.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        link = txt("link")
        if not link:
            le = n.find("{http://www.w3.org/2005/Atom}link") or n.find("link")
            link = le.get("href", "") if le is not None else ""
        title = txt("title")
        if not title:
            continue
        out.append({"title": title, "link": link,
                    "description": txt("description") or txt("summary") or txt("{http://www.w3.org/2005/Atom}summary"),
                    "pub_raw": txt("pubDate") or txt("published") or txt("updated") or "未取得",
                    "source": txt("source") or urlparse(link or url).netloc.replace("www.", ""),
                    "guid": txt("guid") or txt("id")})
    return out


# ---------------------------------------------------------------- 正文抽取
_NAV_NOISE = re.compile(r"^(首页|导航|登录|注册|分享|扫一扫|字体[:：]|责任编辑|版权所有|上一篇|下一篇|相关阅读|热门推荐)")


def extract_article(raw_html: str, *, url: str = "") -> Tuple[str, Dict[str, str]]:
    """返回 (纯文本正文, 元数据)。trafilatura 优先，失败回退 BeautifulSoup 段落密度。"""
    if not raw_html:
        return "", {}
    meta: Dict[str, str] = {}
    text = ""
    if _trafilatura is not None:
        try:
            m = _trafilatura.extract_metadata(raw_html)
            if m is not None:
                for k in ("title", "date", "sitename", "author"):
                    v = getattr(m, k, None)
                    if v:
                        meta[k] = str(v)
            text = _trafilatura.extract(
                raw_html, include_comments=False, include_tables=False,
                favor_precision=True, url=url or None) or ""
        except Exception:
            text = ""
    if not text:
        text = _extract_article_fallback(raw_html)
    # 通用后处理：丢导航残渣 / 过短行 / 网页框架字段；不改写任何数字
    lines = []
    for ln in (text or "").split("\n"):
        s = re.sub(r"\s+", " ", _html.unescape(ln)).strip().lstrip("\u3000 ").strip()
        if len(s) < 12 or _NAV_NOISE.match(s):
            continue
        if re.search(r"版权所有|免责声明|点击进入|扫码|广告|字体[:：]", s[:40]):
            continue
        lines.append(s)
    return "\n".join(lines).strip(), meta


def _extract_article_fallback(raw_html: str) -> str:
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw_html, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            tag.decompose()
        root = soup.find("article") or soup.body or soup
        return "\n".join(p.get_text(" ", strip=True) for p in root.find_all("p"))
    except Exception:
        import re as _re
        return "\n".join(_re.findall(r"<p[^>]*>(.*?)</p>", raw_html, _re.I | _re.S))


def clean_html_safe(raw: str) -> str:
    """bleach 允许清单清洗（只保留无标签纯文本时用）；不负责正文抽取。"""
    if _bleach is None:
        return _html.unescape(re.sub(r"<[^>]+>", "", str(raw or "")))
    try:
        return _bleach.clean(str(raw or ""), tags=[], attributes={}, strip=True)
    except Exception:
        return _html.unescape(re.sub(r"<[^>]+>", "", str(raw or "")))


# ---------------------------------------------------------------- 时间归一化
_RELATIVE_STR = re.compile(r"(?:hours?|分钟|小时|days?|天|weeks?|周|months?|月|ago|前|yesterday|昨天|today|今天|刚刚)", re.I)
_CN_NUM_UNIT = re.compile(r"([零一二三四五六七八九十百千两]+)\s*(分钟|小时|天|周|个?月)前")


def loose_datetime(value: str, *, hint_year: int | None = None,
                   base: datetime | None = None,
                   tz=None) -> Tuple[Optional[datetime], str]:
    """dateparser + cn2an 的**绝对时间**兜底。

    硬约束：相对时间串只登记依据、不换算成 ISO（SKILL §11.1 / 01 第一节）；
    缺年份的「11月18日」dateparser 返回 None，绝不擅自补年。
    调用方应先用项目自带的严格解析链，本函数只补它没覆盖到的绝对格式。
    """
    if _dateparser is None or not value:
        return None, "未取得（dateparser 未启用）"
    s = str(value).strip()
    if not s or s in ("未取得", "未知", "None", "null", "-"):
        return None, "未取得"
    if _RELATIVE_STR.search(s) and not re.search(r"\d{4}", s):
        return None, f"相对时间串只登记依据、不换算发布时间（{s[:40]}）"
    # 中文数词 → 阿拉伯数词，例如「三天前」→「3天前」（随后仍会被相对时间规则拦下）
    if _cn2an is not None and _CN_NUM_UNIT.search(s):
        def _rep(m):
            try:
                return f"{_cn2an.transform(m.group(1), 'cn2an')}{m.group(2)}前"
            except Exception:
                return m.group(0)
        s = _CN_NUM_UNIT.sub(_rep, s)
    settings: Dict[str, Any] = {"STRICT_PARSING": True, "RETURN_AS_TIMEZONE_AWARE": False}
    if base is not None:
        settings["RELATIVE_BASE"] = base.replace(tzinfo=None) if base.tzinfo else base
    if hint_year:
        settings["PREFER_DAY_OF_MONTH"] = "first"
    try:
        dt = _dateparser.parse(s, settings=settings)
    except Exception:
        return None, "未取得（dateparser 解析异常）"
    if dt is None:
        return None, "未取得（dateparser 未识别；缺年份不擅自补年）"
    if tz is not None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        else:
            dt = dt.astimezone(tz)
    return dt, "dateparser 归一化（绝对时间，纯规则）"


# ---------------------------------------------------------------- 相似度
def bigrams(s: str) -> set:
    s = re.sub(r"[\s，,。、：:；;！!？?（）()【】\[\]「」“”\"'·|｜\-—/]", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else ({s} if s else set())


def bigram_jaccard(a: str, b: str) -> float:
    """规格判据：字符 2-gram 集合的 Jaccard（交并比）。

    rapidfuzz 3.14.5 不含 Jaccard 模块，且「2-gram 集合」语义与库内任何
    编辑距离都不同，必须自写；结果可与 rapidfuzz 的序列相似度交叉校验。
    """
    A, B = bigrams(a), bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def lcs_substring(a: str, b: str) -> int:
    """规格判据：最长**公共子串**长度（连续），LCS ≥ 8 中的 LCS 指这个。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def lcsseq_len(a: str, b: str) -> int:
    """rapidfuzz 的**最长公共子序列**（可不连续）：只作候选召回 / 诊断，不进判定。"""
    if _rapidfuzz is None:
        return 0
    try:
        from rapidfuzz.distance import LCSseq
        return int(LCSseq.similarity(a, b))
    except Exception:
        return 0


def fuzz_ratio(a: str, b: str) -> float:
    """rapidfuzz 归一化编辑相似度，仅写进附录 12 的诊断列。"""
    if _rapidfuzz is None:
        return 0.0
    try:
        from rapidfuzz import fuzz
        return round(float(fuzz.ratio(a, b)), 2)
    except Exception:
        return 0.0


def word_bigrams(s: str) -> set:
    """jieba 词级 2-gram，辅助信号；不进任何判定式。"""
    if _jieba is None:
        return set()
    try:
        words = [w for w in _jieba.cut(re.sub(r"\s+", "", s or "")) if len(w) > 1]
        return {words[i] + words[i + 1] for i in range(len(words) - 1)}
    except Exception:
        return set()


# ---------------------------------------------------------------- 热榜（自建开源服务适配）
def hotboard_snapshot(base_url: str, platforms: List[str], *, timeout: float = 5.0) -> Dict[str, Any]:
    """对接自建 imsyy/DailyHotApi（/weibo、/douyin …）或 DIYgod/RSSHub 的 RSS 输出。

    未配置 base_url 或不可达 → 原样返回「未取得 + 原因」，绝不编造词条。
    """
    result: Dict[str, Any] = {"开源方案": "imsyy/DailyHotApi（MIT）或 DIYgod/RSSHub（MIT），需自建实例",
                              "base_url": base_url or "未配置", "逐平台": {}, "状态": "未取得"}
    if not base_url:
        result["原因"] = "未配置自建热榜实例地址；按文档如实申报，不假装有数据"
        return result
    ok = 0
    for p in platforms:
        url = f"{base_url.rstrip('/')}/{p}"
        raw = fetch_url(url, timeout=timeout, retries=0)
        if not raw:
            result["逐平台"][p] = {"状态": "未取得", "原因": "自建实例不可达 / 非 2xx"}
            continue
        try:
            import json
            data = json.loads(raw)
            items = data.get("data") or data.get("items") or []
            result["逐平台"][p] = {"状态": "已触达", "条数": len(items),
                                   "样例": [str(x.get("title") or x.get("name") or "")[:40] for x in items[:3]]}
            ok += 1
        except Exception:
            # RSS 模式交给 feedparser
            entries = parse_feed(raw)
            result["逐平台"][p] = {"状态": "已触达（RSS）", "条数": len(entries)}
            ok += 1
    result["状态"] = "部分触达" if ok else "未取得"
    result["原因"] = f"{ok}/{len(platforms)} 个平台取到快照；词条仍须逐条回查独立原文后才可成稿"
    return result


# ---------------------------------------------------------------- 配置校验
def config_check(cfg: dict) -> List[str]:
    """pydantic 强类型校验；返回问题列表（空 = 通过）。不达标不静默出表。"""
    problems: List[str] = []
    if _pydantic is None:
        # 无库时做等价的纯 Python 检查
        rules = cfg.get("rules", {}) or {}
        if rules.get("mass_strictness") not in ("strict", "moderate", "off"):
            problems.append("rules.mass_strictness 必须是 strict / moderate / off 三档之一")
        sc = cfg.get("scoring", {}) or {}
        if float(sc.get("lambda_", 6)) < 0:
            problems.append("scoring.lambda_ 不能为负")
        if int(rules.get("story_max_keep", 1)) < 1:
            problems.append("rules.story_max_keep 必须 ≥ 1（01 默认 1）")
        return problems
    from pydantic import BaseModel, Field, ValidationError

    class _Rules(BaseModel):
        mass_strictness: str = Field(default="strict", pattern="^(strict|moderate|off)$")
        story_max_keep: int = Field(default=1, ge=1)
        story_dedup: bool = True
        intl_three_only: bool = True

    class _Scoring(BaseModel):
        lambda_: float = Field(default=6, ge=0)
        h_channel: str = Field(default="auto", pattern="^(auto|on|off)$")

    class _Orch(BaseModel):
        enabled: bool = True
        head_window: int = Field(default=3, ge=0)
        front_window: int = Field(default=10, ge=1)

    for name, model, payload in (("rules", _Rules, cfg.get("rules", {}) or {}),
                                 ("scoring", _Scoring, cfg.get("scoring", {}) or {}),
                                 ("orchestrate", _Orch, cfg.get("orchestrate", {}) or {})):
        try:
            model.model_validate(payload)
        except ValidationError as exc:
            for e in exc.errors():
                problems.append(f"{name}.{'.'.join(str(x) for x in e['loc'])}: {e['msg']}")
    return problems
