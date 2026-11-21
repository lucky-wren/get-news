"""HTML 清洗 + 独立原文正文补采。

RSS 的 description 经常是「标题&nbsp; 来源 标题。」这种模板复读，直接当摘要会
产出 100% 无效摘要。本模块做两件事：
  1. clean_html()：实体 / 标签 / 零宽字符 / 空白归一；
  2. fetch_article_text()：从独立原文 URL 取正文（<article> 优先，否则取 <p> 密度），
     失败返回空串，由调用方明确标记「摘要未取得」，绝不复读标题冒充。
"""
from __future__ import annotations

import html
import re
import urllib.request

TAG = re.compile(r"<(script|style|nav|footer|header|aside|form)[^>]*>.*?</\1>", re.I | re.S)
ANY_TAG = re.compile(r"<[^>]+>")
ZERO = re.compile(r"[\u200b-\u200f\u2028\u2029\ufeff]")
WS = re.compile(r"[ \t\u00a0\u3000]+")
P = re.compile(r"<p[^>]*>(.*?)</p>", re.I | re.S)
ARTICLE = re.compile(r"<article[^>]*>(.*?)</article>", re.I | re.S)
# 部分站点把 <script> 里的 JS 或视频播放器模板当纯文本吐进 description / 正文：
#   · 28 条「var isHttps = location.href.substr(...)」
#   · 6 条央视网「[!--begin:htmlVideoCode--]<guid>,0,1,16:9,newPlayer[!」
# 强特征命中的整行直接丢，这些写法在中文新闻正文里几乎不会出现，不会误伤正常句子。
CODE_LINE = re.compile(
    r"\bvar\s+[A-Za-z_$][\w$]*\s*=|\bfunction\s*\(|=>|location\.href|"
    r"document\.|window\.|console\.|getElementById|JSON\.parse|\.substr\s*\(|"
    r"\bif\s*\([^)]*\)\s*\{|\}\s*else\s*\{|\breturn\s+[^，。；]{0,40};|"
    r"\[!--(?:begin|end):|htmlVideoCode|newPlayer\[!|\[!--.*?--\]")
UA = "news-daily-pipeline/1.0 (+robots.txt respected; article text for abstract)"


def clean_html(raw: str) -> str:
    """保留换行、去掉实体与标签，输出可判重与可展示的纯文本。"""
    s = html.unescape(str(raw or ""))
    # 源站偶发吞掉 & 只剩 #246;，且可能跨行（H#246;\n　　egh）；补回字符避免摘要出现乱码
    s = re.sub(r"#(\d{2,5});\s*\n?\s*", lambda m: chr(int(m.group(1))), s)
    s = re.sub(r"#x([0-9a-fA-F]{2,4});\s*\n?\s*", lambda m: chr(int(m.group(1), 16)), s)
    s = ZERO.sub("", s)
    s = TAG.sub("", s)
    s = s.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    s = ANY_TAG.sub("", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for ln in s.split("\n"):
        t = WS.sub(" ", ln).strip().lstrip("\u3000 ").strip()
        if t and CODE_LINE.search(t):
            continue
        lines.append(t)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def compact(s: str) -> str:
    return re.sub(r"\s+", "", clean_html(s))


def is_title_echo(title: str, abstract: str, source: str = "") -> bool:
    """清洗后去掉标题与来源，剩余有效字太少 → 判定为模板复读 / 占位摘要。"""
    t, a = compact(title), compact(abstract)
    if not t or not a:
        return True
    core = a.replace(t, "")
    for s in re.split(r"[·|,，/、]", str(source or "")):
        if compact(s):
            core = core.replace(compact(s), "")
    core = re.sub(r"[。！？；，、,.!?;:：·|｜\-—/()（）\[\]【】\d]", "", core)
    return len(core) < 12 or (a.count(t) >= 2 and len(a) - len(t) * a.count(t) < 30)


def fetch_article_text(url: str, timeout: float = 6.0, max_chars: int = 6000) -> str:
    """取独立原文正文；任何失败都返回空串，不抛异常、不编造内容。

    主链路走开源组件：httpx + tenacity 抓取 → trafilatura 正文/元数据抽取。
    开源库不可用或抽取为空时回退到内置正则实现（oss.py 已登记降级状态）。
    两段都是纯代码，不调用任何模型语义层。
    """
    if not url or not url.startswith(("http://", "https://")):
        return ""
    from . import oss
    raw = oss.fetch_url(url, timeout=timeout, retries=1)
    if raw:
        text, _meta = oss.extract_article(raw, url=url)
        if text:
            return text[:max_chars]
    # 降级：内置 urllib + <article>/<p> 密度抽取
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                   "Accept": "text/html,application/xhtml+xml"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(400000).decode(r.headers.get_content_charset() or "utf-8", "replace")
        blocks = ARTICLE.findall(raw) or [raw]
        paras = []
        for b in blocks:
            for m in P.finditer(b):
                t = clean_html(m.group(1))
                if len(t) >= 18 and not re.search(r"版权所有|免责声明|点击进入|扫码|广告", t[:40]):
                    paras.append(t)
        if not paras:
            paras = [p for p in (clean_html(x) for x in P.findall(raw)) if len(p) >= 18]
        text = "\n".join(paras).strip()
        return text[:max_chars]
    except Exception:
        return ""
