"""栏目页采集器（适配《资讯渠道清单》里的政府/部委/媒体站）。

旧版逐篇串行抓文章页 + 严拒「公告/通知」，在政府站点上几乎恒空。
这版按 NewsNow / DailyHotApi 的单源短超时思路重写：

1. 用 HTMLParser 跟踪 `<li>/<div>/<tr>` 容器，从列表页直接拿到
   「锚文本 + 同容器日期 + 同容器描述」，很多站点不必回查就有时间证据；
2. 描述不足才**并发**回查文章页（trafilatura 抽正文），6 线程、单篇 5s、
   整个端点 20s 硬时限，单篇失败隔离；
3. 只拒站点功能链接（下载/登录/备案/友情链接），不再误杀
   「关于…的通知 / …公告」这类清单明确要收的政策原文；
4. 时间取列表容器日期优先，URL 日期兜底，远期未来/陈旧稿一律不进池。

一条都拿不到就返回空列表，由 registry 如实申报缺口，绝不编造。
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from newsbrief.model import nav_dump_reason
from newsbrief.registry import source

try:
    import trafilatura
except ImportError:
    trafilatura = None

TZ = timezone(timedelta(hours=8))

# 站点功能/外壳文本：只有这些才拒；政策「通知 / 公告 / 公示」不拒（清单 §2/§3 要收）。
_FUNC = re.compile(
    r"^(?:下载|PDF|阅读软件|无障碍|长者模式|登录|注册|返回|上一页|下一页|更多|"
    r"English|ENGLISH|网站地图|版权|备案|友情链接|客户端|扫码|分享|索引|目录|"
    r"帮助中心|意见反馈|联系我们|移动版|手机版|专题|图集|直播|视频|"
    r"首页|设为主页|加入收藏|打印|关闭|错误页|404|搜索|提交|清空|确定|取消)|"
    r"ICP备|公网安备|版权所有|主办单位|承办单位|京公网安备",
    re.I)
_BODY_REJECT = re.compile(
    r"无障碍阅读模式|友情链接|网站地图|备案号|主办单位|承办单位|"
    r"上一篇|下一篇|相关阅读|点击量|打印本页|关闭窗口|You need to enable JavaScript")
_DATE = re.compile(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?")
_URL_DATE = re.compile(r"/(20\d{2})[-/]?(\d{2})[-/]?(\d{2})")


def as_dict(doc) -> dict:
    if isinstance(doc, dict):
        return doc
    if doc is not None and hasattr(doc, "as_dict"):
        return doc.as_dict()
    return {}


class _ListParser(HTMLParser):
    """收集 (href, 锚文本, 所在容器文本)；容器文本用于抽日期与描述。"""

    def __init__(self):
        super().__init__()
        self.out: list[tuple[str, str, str]] = []
        self._stack: list[list[str]] = []
        self._href: str | None = None
        self._buf: list[str] = []

    def _cur(self) -> str:
        return " ".join("".join(s) for s in self._stack[-3:]) if self._stack else ""

    def handle_starttag(self, tag, attrs):
        if tag in ("li", "div", "tr", "article", "section", "p"):
            self._stack.append([])
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._buf = []
            if self._stack:
                self._stack[-1].append(" ")

    def handle_data(self, data):
        t = data.strip()
        if not t:
            return
        if self._href is not None:
            self._buf.append(t)
        if self._stack:
            self._stack[-1].append(t + " ")

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            title = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if title:
                self.out.append((self._href, title, self._cur()))
            self._href = None
        if tag in ("li", "div", "tr", "article", "section", "p") and self._stack:
            done = "".join(self._stack.pop())
            if self._stack:
                self._stack[-1].append(done)


def _published(container: str, url: str, text: str) -> str:
    m = _DATE.search(container) or _DATE.search(text or "")
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh, mm = int(m.group(4) or 0), int(m.group(5) or 0)
        return f"{y:04d}-{mo:02d}-{d:02d}" + (f" {hh:02d}:{mm:02d}" if m.group(4) else "")
    m = _URL_DATE.search(url)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r'<meta[^>]+(?:article:published_time|pubdate)[^>]+content="([^"]+)"',
                  text or "", re.I)
    return m.group(1) if m else ""


def _parse_dt(s: str) -> datetime | None:
    """把列表/文章里的时间串统一成 Asia/Shanghai 的 datetime。"""
    if not s:
        return None
    s = str(s).strip().replace("T", " ")
    m = re.match(r"(20\d{2})-(\d{1,2})-(\d{1,2})(?:[ /]+(\d{1,2}):(\d{2}))?", s)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        int(m.group(4) or 0), int(m.group(5) or 0), tzinfo=TZ)
    except ValueError:
        return None


def _date_ok(s: str, window: tuple | None = None) -> bool:
    """时间硬闸：只采集当批窗口内数据（左开右闭），不再按 now±3 天漂移。

    旧实现用 datetime.now() 相对窗口：CI 在不同日期跑同一份 fixture，
    11-22 的稿在 11-25 就被判超窗 → 栏目页整批返回空，既让零网络自检红，
    也让几十个 html 端点在真采集里恒空。无窗口时只拒「明显远期未来」，
    其余交给 clean.normalize_all 的统一窗口硬闸逐条留痕。
    """
    d = _parse_dt(s)
    if d is None:
        return True                      # 无可核对时间：交给下游统一硬闸剔除
    if window is not None:
        return window[0] < d <= window[1]
    now = datetime.now(TZ)
    # 无窗口（局部单测/兜底）：只拒远期未来与明显陈旧的存档稿，30 天宽松带，
    # 不再用 ±3 天随跑测日期漂移。生产路径一律带 _window，由上面严格判定。
    return (now - timedelta(days=30)) <= d <= (now + timedelta(hours=12))


def _desc(container: str, title: str) -> str:
    """容器里除标题与日期外的文本，作为列表页自带摘要。"""
    t = re.sub(r"\s+", " ", container or "")
    t = t.replace(title, " ", 1)
    t = _DATE.sub(" ", t)
    t = re.sub(r"[\d\s·|｜>《》「」()（）\[\]【】]+", " ", t).strip()
    return t


_P = re.compile(r"<p\b[^>]*>(.*?)</p>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")


def _plain(html: str) -> str:
    """trafilatura 缺失时的兜底：按 <p> 抽段落，去标签，不引入第三方依赖。"""
    parts = []
    for m in _P.finditer(html or ""):
        t = re.sub(r"\s+", " ", _TAG.sub(" ", m.group(1))).strip()
        if len(t) >= 20 and not _BODY_REJECT.search(t[:200]):
            parts.append(t)
    return "\n".join(parts)[:1500]


def _fetch_article(client: httpx.Client, url: str, timeout: float) -> tuple[str, str]:
    """返回 (正文, 原始 html)；失败返回 ('','')，由调用方隔离。

    trafilatura 只是增强：未装时改用内置 <p> 抽取，保证清单里几十个
    html 端点不会因为一个可选依赖整批失效（CI/本机环境差异大）。
    """
    try:
        a = client.get(url, timeout=timeout)
        a.raise_for_status()
        if trafilatura is not None:
            # 可选依赖只做增强：任何异常/空结果都降级到内置 <p> 抽取，
            # 绝不让一个第三方库把整批 html 端点吞成空列表（CI 装了
            # .[dev,extract]，本机没装，此前正是这个差异让自检恒红）。
            try:
                d = as_dict(trafilatura.bare_extraction(a.text))
                text = (d.get("text") or "").strip()
                if len(text) >= 40 and not _BODY_REJECT.search(text[:600]):
                    if d.get("date"):
                        return text[:1500], str(d["date"])
                    return text[:1500], a.text
            except Exception:
                pass
        return _plain(a.text), a.text
    except Exception:
        return "", ""


@source("html")
def fetch(client: httpx.Client, ep: dict, timeout: float) -> list[dict]:
    deadline = time.monotonic() + float(ep.get("endpoint_seconds", 20))
    window = ep.get("_window")       # registry.collect 注入的当批左开右闭窗口
    r = client.get(ep["url"], timeout=timeout)
    r.raise_for_status()
    lp = _ListParser()
    lp.feed(r.text)
    home = urlparse(ep["url"])
    seen: set[str] = set()
    cand: list[tuple[str, str, str, str]] = []
    for href, title, container in lp.out:
        if not (10 <= len(title) <= 90) or nav_dump_reason(title) or _FUNC.search(title):
            continue
        if href.startswith(("#", "javascript:", "mailto:")):
            continue
        if re.search(r"\.(?:jpg|png|gif|css|js|zip|mp4)(?:\?|$)", href, re.I):
            continue
        u = urljoin(ep["url"], href)
        p = urlparse(u)
        if p.scheme not in ("http", "https") or not p.netloc or u in seen:
            continue
        if (p.path.rstrip("/") or "/") == (home.path.rstrip("/") or "/"):
            continue
        seen.add(u)
        pub = _published(container, u, "")
        if not _date_ok(pub, window):
            continue
        cand.append((u, title, _desc(container, title), pub))
    # 有日期/有描述的排前面，优先抓高价值链接
    cand.sort(key=lambda x: (0 if x[3] else 1, 0 if len(x[2]) >= 40 else 1))
    cand = cand[: int(ep.get("max_articles", 8))]

    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {}
        for u, title, desc, pub in cand:
            if time.monotonic() >= deadline:
                break
            futs[pool.submit(_fetch_article, client, u, min(timeout, 5))] = (u, title, desc, pub)
        for f in as_completed(futs):
            u, title, desc, pub = futs[f]
            try:
                body, raw = f.result()
            except Exception:
                continue
            # 列表页自带描述优先（政府站常是完整导语），否则用回查正文
            text = desc if len(desc) >= 40 and re.search(r"[。！？]", desc) else body
            if len(text) < 40 or nav_dump_reason(text[:600]) or _BODY_REJECT.search(text[:600]):
                continue
            if not re.search(r"[。！？]", text[:800]):
                continue
            p = pub or _published("", u, raw or "") or _published("", u, text)
            if not _date_ok(p, window):
                continue
            out.append({
                "title": title, "url": u, "body": text[:1500],
                "published": p, "source": ep["name"],
                "group_id": ep.get("group_id"),
                "group": ep.get("group_name") or ep.get("board") or "民生服务",
                "shape": "独立原文", "evidence": "A",
            })
    return out
