"""采集适配器层：渠道文档 §0.1 三层能力的第 2 层「每渠道采集器」。

已实现：外部注入 JSON / 已有 xlsx 台账、RSS（Google News RSS + RSSHub 路由）、
结构化 API（USGS / GDACS / GDELT DOC / ReliefWeb）、站内列表页（CSS 选择器）、
榜单四时点快照（词条 → 回查独立原文）、Serper/Tavily 搜索回查。

诚实降级：任何渠道无 key / 超时 / 被拦截 / 选择器失配，都逐渠道申报「未取得」，
不编造条目、不把检索工具名写进来源列（渠道文档 §36.4 / §36.18）。
"""
from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
import yaml

from .model import RawItem
from .article import clean_html, fetch_article_text, is_title_echo
from .timewindow import parse_datetime_text, extract_date_clues

UA_DEFAULT = "news-daily-pipeline/1.0 (+robots.txt respected)"


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lstrip("www.")
    except Exception:
        return ""


def _norm_source(text: str) -> str:
    """来源列：渠道 · 渠道；平台内部层级用不带空格的 ·，不带链接 / 编号 / 竖线。"""
    s = (text or "").strip()
    s = re.sub(r"https?://\S+", "", s)
    s = s.replace("|", "·").replace("｜", "·")
    s = re.sub(r"\s*[（(][^）)]*[）)]\s*$", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" ·-")


def _clean_title(text: str) -> str:
    s = (text or "").strip()
    s = re.sub(r"\s+[-_|｜]\s+[^•]{1,30}$", "", s)   # 去 SEO 尾巴（站点名）
    s = re.sub(r"^【[^】]{1,20}】|^（[^）]{1,20}）|^\([^)]{1,20}\)", "", s)
    return s.strip()


def _split_polluted_title(title: str) -> tuple:
    """源站把整段正文塞进 title（实测主表 3 条 >60 字、最长 125 字）。

    返回 (标题, 余文)：标题按首个整句边界收束，余文交给调用方当 abstract 候选。
    只做字段归位，不新增任何事实；余文仍要过 build_abstract 的标题复读判定。
    """
    s = (title or "").strip()
    if len(s) <= 60:
        return s, ""
    m = re.search(r"[。！？!?；;]", s[:80])
    cut = m.end() if m and m.end() >= 20 else 60
    return s[:cut].strip().rstrip("，,、 "), s[cut:].strip()


class Collector:
    def __init__(self, cfg: dict, registry, window, base_dir: Path):
        self.cfg = cfg
        self.live = (cfg.get("source", {}).get("live", {}) or {})
        self.registry = registry
        self.win = window
        self.base = Path(base_dir)
        self.timeout = float(self.live.get("request_timeout", 8))
        self.ua = self.live.get("user_agent", UA_DEFAULT)
        self.max_per = int(self.live.get("max_per_channel", 60))
        self.items: List[RawItem] = []
        self.clues: List[dict] = []          # 附录 15：线索待原文
        self.failures: List[dict] = []       # 逐渠道缺口
        self.article_stats: dict = {}        # 正文补采统计
        self.last_request = 0.0
        self._seq = 0

    # ---------- 统一台账写入 ----------
    def _add(self, *, ch: dict, title: str, abstract: str = "", source: str = "",
             url: str = "", pub: Optional[datetime] = None, pub_raw: str = "未取得",
             basis: str = "", mark: str = "", evidence: str = "B",
             shape: str = "独立原文", has_event: bool = True,
             record_type: str = "独立原文", group: str = "", tag: str = "",
             board_platforms: Optional[List[str]] = None, board_slots: int = 0,
             media_slots: int = 0, raw: Optional[dict] = None) -> RawItem:
        self._seq += 1
        _t, _rest = _split_polluted_title(_clean_title(title))
        if _rest and not abstract:
            abstract = _rest                      # 字段归位，不新增事实
        title = _t
        it = RawItem(
            title=title, abstract=abstract or "", source=_norm_source(source),
            url=url, pub_raw=pub_raw, pub=pub, time_basis=basis, time_mark=mark,
            date_clue=extract_date_clues(f"{title} {abstract}"),
            group=group, tag=tag, evidence=evidence,
            dedup_key=f"{url}|{_clean_title(title)}",
            seq=self._seq, channel=ch.get("channel", ""), channel_id=ch.get("channel_id", ""),
            channel_section=ch.get("section", ""), channel_level=ch.get("level", "☆"),
            shape=shape, has_event=has_event, record_type=record_type,
            board_platforms=board_platforms or [], board_slots=board_slots,
            media_slots=media_slots, raw=raw or {},
        )
        self.items.append(it)
        return it

    def _throttle(self, seconds: float = 0.0) -> None:
        # 全局预算：到点抛 BudgetExpired，由 run() 捕获，保留已抓条目继续出表
        from .budget import BudgetExpired, check as _budget_check
        _budget_check(float((self.cfg.get("budget") or {}).get("_deadline_at") or 0))
        if seconds <= 0:
            return
        gap = time.time() - self.last_request
        if gap < seconds:
            time.sleep(seconds - gap)
        self.last_request = time.time()

    def _get(self, url: str, *, params: dict | None = None,
             headers: dict | None = None, kind: str = "text") -> Any:
        self._throttle(float(self.live.get("min_interval_seconds", 0)) if "gdelt" in url else 0)
        h = {"User-Agent": self.ua}
        if headers:
            h.update(headers)
        r = requests.get(url, params=params, headers=h, timeout=self.timeout)
        r.raise_for_status()
        return r.json() if kind == "json" else r.text

    @staticmethod
    def _fail_class(reason: str) -> str:
        """把「未取得」拆成可行动的原因，避免 597 个渠道只显示一句笼统失败。"""
        r = str(reason)
        if "待配置端点" in r or "无端点" in r:
            return "未配置端点（可修复：补官方 RSS / 时间倒排 / API）"
        if "Timeout" in r or "timeout" in r or "timed out" in r:
            return "网络超时（可重试 + 退避）"
        if "403" in r or "Forbidden" in r or "412" in r:
            return "访问被拒 403/412（反爬，需 UA / 代理 / 合规评估）"
        if "404" in r or "Not Found" in r:
            return "端点失效 404（需更新 URL）"
        if "401" in r or "429" in r or "key" in r.lower() or "API" in r and "未取得" in r:
            return "需密钥 / 触发限流（配 key 或降频）"
        if "pubDate" in r or "解析" in r or "Parse" in r:
            return "解析失败 / 无时间字段（需正文回查 pubDate）"
        if "NameResolution" in r or "Connection" in r or "DNS" in r:
            return "DNS / 连接失败（网络出口策略）"
        return "其他（见 reason 原文）"

    def _fail(self, ch: dict, adapter: str, reason: str) -> None:
        cat = self._fail_class(reason)
        self.failures.append({"channel": ch.get("channel", ""), "adapter": adapter,
                              "reason": reason, "category": cat})
        self.registry.mark(ch.get("channel_id", ""), hit=False, shape="", count=0,
                           full_enumeration=ch.get("full_enumeration", "否"),
                           adapter=adapter, gap=f"[{cat}] {reason}")

    def _skip(self, ch: dict, adapter: str, reason: str, sample: str = "") -> None:
        """质量 v3：列表页条目被「非新闻硬闸」拦下，逐条留痕不静默丢弃。"""
        self.skipped = getattr(self, "skipped", [])
        self.skipped.append({"channel": ch.get("channel", ""), "adapter": adapter,
                             "reason": reason, "sample": sample[:80]})

    def _ok(self, ch: dict, adapter: str, shape: str, count: int,
            full: Optional[str] = None) -> None:
        self.registry.mark(ch.get("channel_id", ""), hit=count > 0, shape=shape if count else "",
                           count=count, full_enumeration=full or ch.get("full_enumeration", "否"),
                           adapter=adapter,
                           gap="" if count else "已触达但窗口内 0 条（如实申报，不补造）")

    # ---------- 1. 外部注入 / 离线演示台账 ----------
    def collect_inbox(self, path: str | Path, *, demo: bool = False) -> List[RawItem]:
        p = Path(path)
        if not p.is_absolute():
            p = self.base / p
        if not p.exists():
            raise FileNotFoundError(f"注入清单不存在：{p}")
        ch = {"channel": "外部注入台账" if not demo else "离线演示台账",
              "channel_id": "INBOX-001", "level": "☆", "section": "§31 数据基础设施 / 外部注入"}
        if p.suffix.lower() in (".xlsx", ".xlsm"):
            records = self._read_xlsx_inbox(p)
        else:
            payload = json.loads(p.read_text(encoding="utf-8"))
            records = payload.get("items", payload if isinstance(payload, list) else [])
        before = len(self.items)
        for r in records:
            pub = None
            if r.get("pub"):
                pub, basis = parse_datetime_text(str(r["pub"]), hint_year=self.win.end.year)
            else:
                basis = ""
            self._add(
                ch=ch, title=r.get("title", ""), abstract=r.get("abstract", ""),
                source=r.get("source", ""), url=r.get("url", ""),
                pub=pub, pub_raw=str(r.get("pub") or "未取得"),
                basis=r.get("time_basis") or basis or "外部注入 pub",
                mark=r.get("time_mark", ""), evidence=r.get("evidence", "B"),
                shape=r.get("shape", "独立原文"), has_event=bool(r.get("has_event", True)),
                record_type=r.get("record_type", "独立原文"),
                group=r.get("group", ""), tag=r.get("tag", ""),
                board_platforms=r.get("board_platforms") or [],
                board_slots=int(r.get("board_slots", 0) or 0),
                media_slots=int(r.get("media_slots", 0) or 0), raw=r,
            )
        added = len(self.items) - before
        self._ok(ch, "inbox", "独立原文 / 台账", added, "是")
        return self.items[before:]

    @staticmethod
    def _read_xlsx_inbox(p: Path) -> List[dict]:
        import openpyxl
        from openpyxl.reader.excel import ExcelReader
        # 会话内生成的台账常带未声明命名空间的 custom.xml，跳过自定义属性即可
        ExcelReader.read_custom = lambda self: None  # type: ignore[method-assign]
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        sheet = "窗口内全部数据" if "窗口内全部数据" in wb.sheetnames else wb.sheetnames[0]
        ws = wb[sheet]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        head = [str(c or "") for c in rows[0]]
        out = []
        for row in rows[1:]:
            d = dict(zip(head, row))
            if not d.get("标题"):
                continue
            out.append({
                "title": d.get("标题", ""), "abstract": d.get("内容摘要", ""),
                "source": d.get("来源", ""), "group": d.get("上位大类", ""),
                "tag": d.get("分类标签", ""), "record_type": d.get("记录类型", "台账导入"),
                "pub": d.get("发布时间"), "time_basis": "xlsx 台账导入（原台账无逐条发布时间）",
                "evidence": "B", "has_event": True, "shape": "独立原文",
            })
        return out

    # ---------- 2. RSS / 时间倒排 ----------
    def collect_rss(self) -> List[RawItem]:
        """Feed 解析走 feedparser（RSS 0.9x/1.0/2.0 + Atom + 编码/命名空间/bozo），
        缺失时由 oss.parse_feed 自动回退 ElementTree；不再手写 XML 节点遍历。"""
        from . import oss
        for ch in self.registry.with_endpoint():
            url = ch.get("rss_url")
            if not url or ch.get("hotboard"):
                continue
            try:
                xml = self._get(url)
                entries = oss.parse_feed(xml, url=url)
                n = 0
                for e in entries[: self.max_per]:
                    title = e.get("title", "")
                    link = e.get("link", "")
                    desc = clean_html(e.get("description", ""))[:600]
                    src = e.get("source", "") or urlparse(link).netloc.replace("www.", "")
                    pub_raw = e.get("pub_raw") or "未取得"
                    pub, basis = parse_datetime_text(pub_raw, hint_year=self.win.end.year)
                    if not title:
                        continue
                    self._add(ch=ch, title=title, abstract=desc, source=src,
                              url=link, pub=pub, pub_raw=pub_raw, basis=basis,
                              evidence="A" if link else "B", shape="独立原文",
                              record_type="RSS 时间倒排", media_slots=1 if link else 0,
                              raw={"rss": url, "guid": e.get("guid", "")})
                    n += 1
                self._ok(ch, "rss", "独立原文", n)
            except Exception as exc:  # 网络 / 解析 / 反爬
                self._fail(ch, "rss", f"RSS 未取得：{type(exc).__name__}: {str(exc)[:120]}")
        return self.items

    # ---------- 3. 结构化 API ----------
    def collect_apis(self) -> List[RawItem]:
        for ch in self.registry.with_endpoint():
            if not ch.get("api"):
                continue
            kind = ch.get("api_kind", "")
            try:
                data = self._get(ch["api"], kind="json")
                n = 0
                if kind == "usgs":
                    # 用户①定稿：国际只留重大突发。USGS 全量 feed 会把 0.7 级小震
                    # 灌进候选池并靠「级地震」关键词混过国际三类门禁，实测 0.7 级
                    # 排到主表第 4。低于阈值的事件不入库（逐条计数留痕，不编造）。
                    # 默认与 config.yaml 保持一致，配置缺失时不再回退 4.5 灌小震。
                    mag_min = float((self.cfg.get("channels", {}) or {}).get(
                        "usgs_min_magnitude", 5.5))
                    for f in data.get("features", [])[: self.max_per]:
                        p = f.get("properties", {})
                        mag, place = p.get("mag"), p.get("place", "")
                        if isinstance(mag, (int, float)) and mag < mag_min:
                            self.usgs_below_threshold = getattr(self, "usgs_below_threshold", 0) + 1
                            continue
                        ts = p.get("time")
                        pub = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone(self.win.end.tzinfo) if ts else None
                        title = f"{place or '境外'}发生{mag:.1f}级地震" if mag else f"{place}地震速报"
                        abstract = (f"USGS 测定：{place or '震中未标明'}发生{mag}级地震，"
                                    f"震源深度 {f.get('geometry', {}).get('coordinates', [None, None, None])[2]} 公里。"
                                    f"事件页可核对发震时刻、震级修订与所在海域。") if mag else "USGS 地震速报，见事件页。"
                        self._add(ch=ch, title=title, abstract=abstract, source="USGS",
                                  url=p.get("url", ch["api"]), pub=pub,
                                  pub_raw=pub.isoformat() if pub else "未取得",
                                  basis="USGS GeoJSON properties.time", evidence="A",
                                  shape="结构化", record_type="USGS 结构化事件",
                                  group="国际动态", tag="国际动态 / 灾害与公共卫生",
                                  media_slots=1, raw=p)
                        n += 1
                elif kind == "gdacs":
                    feats = data.get("features", data if isinstance(data, list) else [])
                    for f in feats[: self.max_per]:
                        p = f.get("properties", f) or {}
                        title = str(p.get("name") or p.get("title") or "GDACS 灾害警报")
                        pub, basis = parse_datetime_text(str(p.get("fromdate") or ""), hint_year=self.win.end.year)
                        self._add(ch=ch, title=title, abstract=str(p.get("description") or title),
                                  source="GDACS", url=p.get("url") or ch["api"], pub=pub,
                                  pub_raw=str(p.get("fromdate") or "未取得"), basis=basis or "GDACS fromdate",
                                  evidence="B", shape="结构化", record_type="GDACS 结构化事件",
                                  group="国际动态", tag="国际动态 / 灾害与公共卫生")
                        n += 1
                elif kind in ("gdelt", "reliefweb"):
                    arts = data.get("articles", []) if kind == "gdelt" else data.get("data", [])
                    for a in arts[: self.max_per]:
                        if kind == "gdelt":
                            title, url = a.get("title", ""), a.get("url", "")
                            # GDELT seendate 是「见到文章时间」不是发布时间，不得冒充（§31）
                            pub, basis, pub_raw = None, "GDELT seendate 为见到文章时间，非发布时间；近一天收录仅保证抓取窗口", "未取得"
                            src = _norm_source(a.get("domain", ""))
                        else:
                            f = a.get("fields", {})
                            title, url = f.get("title", [""])[0] if isinstance(f.get("title"), list) else f.get("title", ""), f.get("url", "")
                            pub, basis = parse_datetime_text(str(f.get("date", {}).get("created", "")), hint_year=self.win.end.year)
                            pub_raw = str(f.get("date", {}).get("created", "未取得"))
                            src = "ReliefWeb"
                        if not title:
                            continue
                        self._add(ch=ch, title=title, abstract="", source=src, url=url,
                                  pub=pub, pub_raw=pub_raw, basis=basis, evidence="B",
                                  shape="结构化", record_type=f"{kind.upper()} 检索线索",
                                  group="国际动态" if kind == "gdelt" else "重大突发与司法")
                        n += 1
                self._ok(ch, f"api:{kind}", "结构化", n)
            except Exception as exc:
                self._fail(ch, f"api:{kind}", f"API 未取得：{type(exc).__name__}: {str(exc)[:120]}")
        return self.items

    # ---------- 4. 站内列表页 / 公告列表 ----------
    def collect_html_lists(self) -> List[RawItem]:
        from bs4 import BeautifulSoup
        for ch in self.registry.with_endpoint():
            pages = ch.get("list_pages") or []
            if not pages or ch.get("hotboard"):
                continue
            n, errs = 0, []
            for page in pages:
                try:
                    html = self._get(page["url"])
                    soup = BeautifulSoup(html, "html.parser")
                    sel = page.get("selectors", {})
                    for node in soup.select(sel.get("item", "a"))[: self.max_per]:
                        t = node.select_one(sel.get("title", "a")) if sel.get("title") else node
                        a = node.select_one(sel.get("link", "a")) if sel.get("link") else node
                        title = t.get_text(" ", strip=True) if t else ""
                        link = a.get("href", "") if a and a.name == "a" else ""
                        if link and link.startswith("/"):
                            u = urlparse(page["url"])
                            link = f"{u.scheme}://{u.netloc}{link}"
                        tnode = node.select_one(sel["time"]) if sel.get("time") else None
                        pub_raw = tnode.get_text(" ", strip=True) if tnode else "未取得"
                        pub, basis = parse_datetime_text(pub_raw, hint_year=self.win.end.year)
                        if not title or len(title) < 6:
                            continue
                        # 2026-11-21 质量 v3：默认 soup.select("a") 会把首页/栏目/
                        # 导航/机构职能/JS 模板/模型卡/论坛帖/旧公告全当新闻抓进来
                        # （用户上传 latest.xlsx 候选池 646-732 行实测 30+ 条）。
                        # 入库前按「新闻条目形态」硬闸，逐条留痕，不靠后端筛选兜底。
                        from .quality_v3 import is_news_item
                        _ok_news, _why = is_news_item(title, "", link, pub, strict_url=True)
                        if not _ok_news:
                            self._skip(ch, "html_list", _why, title[:50])
                            continue
                        self._add(ch=ch, title=title, abstract="", source=ch["channel"],
                                  url=link, pub=pub, pub_raw=pub_raw, basis=basis,
                                  evidence="B", shape="独立原文" if link else "时间流",
                                  record_type="站内时间倒排", media_slots=1 if link else 0)
                        n += 1
                except Exception as exc:
                    errs.append(f"{type(exc).__name__}: {str(exc)[:80]}")
            if n:
                self._ok(ch, "html_list", "独立原文 / 时间流", n)
            else:
                self._fail(ch, "html_list", "；".join(errs) or "列表页已触达但选择器未命中，窗口内 0 条")
        return self.items

    # ---------- 5. 榜单四时点快照：词条 → 回查 → 成稿 ----------
    def collect_hotboards(self) -> List[RawItem]:
        for ch in self.registry.with_endpoint():
            if not ch.get("hotboard"):
                continue
            words: List[Tuple[str, str]] = []
            try:
                if ch.get("rss_url"):
                    root = ET.fromstring(self._get(ch["rss_url"]))
                    for node in root.findall(".//item")[: self.max_per]:
                        t = (node.findtext("title") or "").strip()
                        l = (node.findtext("link") or "").strip()
                        if t:
                            words.append((t, l))
                elif ch.get("list_pages"):
                    from bs4 import BeautifulSoup
                    for page in ch["list_pages"]:
                        soup = BeautifulSoup(self._get(page["url"]), "html.parser")
                        sel = page.get("selectors", {})
                        for node in soup.select(sel.get("item", "a"))[: self.max_per]:
                            t = node.select_one(sel.get("title", "a")) if sel.get("title") else node
                            a = node.select_one(sel.get("link", "a")) if sel.get("link") else node
                            title = t.get_text(" ", strip=True) if t else ""
                            link = a.get("href", "") if a and a.name == "a" else page["url"]
                            if title:
                                words.append((title, link))
            except Exception as exc:
                self._fail(ch, "hotboard", f"榜单快照未取得：{type(exc).__name__}: {str(exc)[:100]}")
                continue

            made = 0
            # 单榜单回查上限：热榜常有 50+ 词条，全量逐条打搜索 API 会把免费额度
            # 和跑批耗时吃光。超出部分直接进附录 15「线索待原文」，不静默丢弃。
            lookup_max = int((self.cfg.get("search_api", {}) or {})
                             .get("hotboard_lookup_max", 12))
            for word, snap_url in words[:lookup_max]:
                hit = self.lookup_article(word) if self.live.get("search", True) else None
                if hit and hit.get("title") and hit.get("url"):
                    pub, basis = parse_datetime_text(hit.get("date") or "", hint_year=self.win.end.year)
                    it = self._add(
                        ch=ch, title=hit["title"], abstract="",   # 不吃 snippet
                        source=hit.get("source", ch["channel"]), url=hit["url"],
                        pub=pub, pub_raw=hit.get("date") or "未取得", basis=basis,
                        evidence="A", shape="词条回查成稿", has_event=True,
                        record_type="社交平台词条回查成稿",
                        board_platforms=[ch["channel"]], board_slots=1, media_slots=1,
                        raw={"榜单词条": word, "搜索Provider": hit.get("provider", ""),
                             "质量分": hit.get("score", 0),
                             "候选摘要(仅留痕,不作正文)": hit.get("snippet", "")[:220]})
                    it.abstract_body = ""     # 由 _fetch_search_bodies 从独立原文补
                    made += 1
                else:
                    # 只有话题名 + 快照 → has_event=false，进附录 15，不进主表
                    self.clues.append({"词条": word, "榜单": ch["channel"],
                                       "快照页": snap_url or ch.get("rss_url", ""),
                                       "原因": "未回查到带事实的独立原文（无 key / 无结果）"})
                    self._add(ch=ch, title=word, abstract="", source=ch["channel"],
                              url=snap_url or "", pub=None, pub_raw="未取得",
                              basis="榜单快照，仅保证抓取窗口", mark="当日速览推定",
                              evidence="C", shape="词条", has_event=False,
                              record_type="词条（待回查）", board_platforms=[ch["channel"]],
                              board_slots=1)
            for word, snap_url in words[lookup_max:]:
                self.clues.append({"词条": word, "榜单": ch["channel"],
                                   "快照页": snap_url or ch.get("rss_url", ""),
                                   "原因": f"超出单榜单回查上限 {lookup_max}，本批未调用搜索 API，留痕待下批"})
            self._ok(ch, "hotboard", "词条 / 回查成稿", made,
                     f"快照制（回查上限 {lookup_max}，其余 {max(0, len(words)-lookup_max)} 条进附录 15）")
        return self.items

    # ---------- 6. 搜索回查（统一搜索层：多 provider + 质量打分 + 配额留痕） ----------
    def lookup_article(self, query: str, *, site: str = "") -> Optional[dict]:
        """回查到「最优」独立原文候选，不是第一条。

        走 news_pipeline/search_api.py：Serper / Tavily（有 key）→ Bing 站内（无 key，
        本沙盒实测可达）→ 搜狗；多候选按权威域名 / 窗口内发布时间 / 事实锚点 / 贴合度
        打分，旧闻与 PDF/话题页由硬闸拦下。搜索摘要只作「是否值得补正文」的判断，
        真正的内容摘要仍由 _fetch_articles 从独立原文 URL 补正文后生成。
        """
        from . import search_api
        try:
            hit = search_api.search_best(query, win=self.win, cfg=self.cfg, site=site)
        except Exception as exc:
            self.failures.append({"channel": "搜索回查", "adapter": "search_api",
                                  "reason": f"{type(exc).__name__}: {str(exc)[:100]}"})
            return None
        if not hit:
            return None
        d = hit.as_legacy()
        d["source"] = _norm_source(d.get("source") or _domain_of(d.get("url", "")))
        return d

    def collect_search(self) -> List[RawItem]:
        """按需求①的 11 类大众贴近主题做定向检索；检索工具名不进来源列。

        与旧版三点不同：
        - 主题词直接来自用户口径（衣食住行 / 工作 / 医保社保 / 工厂制药医药 /
          AI / 新能源 / 裁员 / 地方债 / 房地产 / 倒闭资不抵债 / 社会议题）；
        - 回查走统一搜索层的**多候选质量打分**，不是取第一条；
        - **不吃 snippet**：`abstract` 留空，snippet 只进 raw 留痕，
          正文由 `_fetch_search_bodies()` 从独立原文 URL 补采后生成摘要。
        """
        from . import search_api
        conf = self.cfg.get("search_api", {}) or {}
        groups: List[Tuple[str, List[str]]] = [
            ("衣食住行", ["成品油调价 每升", "寒潮预警 高速封闭", "CPI 食品价格"]),
            ("工作与社保", ["拖欠工资 劳动仲裁", "医保目录 新增药品", "社保缴费 基数调整"]),
            ("工厂与医药", ["工厂停产 订单交付", "药品召回 通告", "原料药 涨价"]),
            ("AI 与新能源", ["大模型 发布 备案", "新能源车 补贴 政策", "光伏 产能 调整"]),
            ("裁员与地产", ["裁员 补偿 员工", "保交楼 交付", "房贷 利率 调整"]),
            ("地方债与倒闭", ["地方债 发行 置换", "上市公司 破产重整", "资不抵债 清算"]),
            ("社会议题", ["食品安全 抽检 不合格", "重大事故 伤亡 通报", "诈骗 预警 提醒"]),
            ("国际三类", ["地震 伤亡", "洪水 灾害 救援", "空难 事故"]),
        ]
        # 每组最多取 1 个主题词，避免把 Serper 免费额度 / Bing 请求打满
        per_group = int(conf.get("keywords_per_group", 2))
        pseudo = {"channel": "定向检索回查", "channel_id": "SEARCH-001",
                  "level": "☆", "section": "§31 搜索与抓取基础设施"}
        n = 0
        for gname, kws in groups:
            for kw in kws[:per_group]:
                q = f"{kw} {self.win.end.strftime('%Y-%m-%d')}"
                hit_d = self.lookup_article(q)
                if not hit_d:
                    continue
                pub, basis = parse_datetime_text(hit_d.get("date") or "",
                                                 hint_year=self.win.end.year)
                it = self._add(
                    ch=pseudo, title=hit_d["title"], abstract="",
                    source=hit_d.get("source", ""), url=hit_d["url"], pub=pub,
                    pub_raw=hit_d.get("date") or "未取得",
                    basis=basis or "搜索 API date 字段", evidence="A",
                    shape="独立原文", record_type="联网检索独立原文",
                    media_slots=1,
                    raw={"检索主题": gname, "检索词": kw,
                         "搜索Provider": hit_d.get("provider", ""),
                         "候选数": hit_d.get("candidates", 0),
                         "质量分": hit_d.get("score", 0),
                         "候选摘要(仅留痕,不作正文)": hit_d.get("snippet", "")[:260]})
                it.abstract_body = ""      # 由 _fetch_search_bodies 补
                n += 1
        self._ok(pseudo, "search", "独立原文（待补正文）", n, "否")
        return self.items

    # ---------- 6b. 搜索成稿条目的独立原文正文补采（不吃 snippet） ----------
    def _fetch_search_bodies(self, items: Optional[List[RawItem]] = None) -> dict:
        """对「搜索回查成稿」条目，从独立原文 URL 补正文。

        规格硬约束：搜索摘要只是发现线索，绝不当内容摘要。补不到就留空，
        由 normalize.build_abstract 标「摘要未取得」，不拿 snippet 冒充。
        `items` 可指定外部条目列表（开源热榜源成稿用），不影响主台账顺序。
        """
        from concurrent.futures import ThreadPoolExecutor
        from . import free_sources
        conf = self.cfg.get("search_api", {}) or {}
        limit = int(conf.get("max_body_fetch", 40))
        timeout = float(conf.get("body_timeout", 6.0))
        jina_max = int(conf.get("jina_max", 8))
        pool = items if items is not None else self.items
        targets = [it for it in pool
                   if it.url and not it.abstract_body
                   and (it.record_type in ("联网检索独立原文", "社交平台词条回查成稿",
                                           "开源热榜词条回查成稿")
                        or "回查成稿" in (it.record_type or ""))][:limit]
        stat = {"目标": len(targets), "成功": 0, "失败": 0,
                "Jina回退尝试": 0, "Jina回退成功": 0}

        def one(it: RawItem) -> Tuple[str, str]:
            body = fetch_article_text(it.url, timeout=timeout)
            how = "trafilatura"
            if not body and jina_max > 0:
                body = free_sources.jina_reader_text(it.url, timeout=4.0)
                how = "jina"
            return body, how

        if not targets:
            self.search_body_stats = stat
            return stat
        import time as _time
        from concurrent.futures import wait, FIRST_COMPLETED
        deadline = float(conf.get("body_deadline_seconds", 45))
        stat["批量时限(秒)"] = deadline
        ex = ThreadPoolExecutor(max_workers=6)
        try:
            futs = {ex.submit(one, it): it for it in targets}
            pending, t0, timed_out = set(futs), _time.time(), 0
            while pending and (_time.time() - t0) < deadline:
                fin, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                for fu in fin:
                    it = futs[fu]
                    try:
                        body, how = fu.result()
                    except Exception:
                        body, how = "", "trafilatura"
                    if body:
                        it.abstract_body = body
                        it.evidence = "A"
                        stat["成功"] += 1
                        if how == "jina":
                            stat["Jina回退成功"] += 1
                    else:
                        stat["失败"] += 1
                    if how == "jina":
                        stat["Jina回退尝试"] += 1
            for fu in pending:
                fu.cancel()
                timed_out += 1
                stat["失败"] += 1
            stat["批量超时取消"] = timed_out
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        self.search_body_stats = stat
        return stat

    # ---------- 独立原文正文补采 ----------
    def _fetch_articles(self) -> dict:
        """RSS description 常是标题复读；从 URL 补正文，失败如实标记不编造。

        并发 + 整批硬上限：旧版是 160 篇顺序请求、单篇 6s 超时，最坏 16 分钟，
        是本轮跑批卡住的根因之一；改为 8 并发 + deadline，超时取消并如实留空。
        """
        import time as _time
        from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
        from . import free_sources
        limit = int(self.live.get("max_articles", 60))
        free_conf = self.cfg.get("free_sources", {}) or {}
        jina_on = bool(free_conf.get("jina_reader_fallback", True))
        jina_max = int(free_conf.get("jina_fallback_max", 12))
        jina_timeout = float(free_conf.get("jina_timeout", 3.0))
        deadline = float(self.live.get("body_deadline_seconds", 50))
        stat = {"尝试": 0, "成功": 0, "失败": 0, "跳过_无URL": 0, "跳过_摘要已有效": 0,
                "Jina回退尝试": 0, "Jina回退成功": 0, "批量时限(秒)": deadline,
                "批量超时取消": 0}
        # 关键修复（2026-11-20）：旧版按 self.items 原始采集顺序取前 limit 篇，
        # 名额大量被「窗口外条目」吃掉（人民网 RSS 停在 2025-06 的旧页、
        # 央视网晚于右界的最新流都在前面）。结果窗口内条目补不到正文，
        # 被 validate_fields 以「摘要与正文均缺失」剔除（本批 807 条）。
        # 现在先按「是否落在窗口内」排序，配额 100% 优先服务于窗口内条目，
        # 窗口外条目排在后面、有余量才补，并逐条留痕。
        def _in_window(it: RawItem) -> bool:
            d = it.pub
            if d is None:
                try:
                    d, _ = parse_datetime_text(str(it.pub_raw or ""),
                                               hint_year=self.win.end.year)
                except Exception:
                    d = None
            if d is None:
                return False
            if d.tzinfo is not None:
                d = d.replace(tzinfo=None)
            ws = self.win.start.replace(tzinfo=None) if self.win.start.tzinfo else self.win.start
            we = self.win.end.replace(tzinfo=None) if self.win.end.tzinfo else self.win.end
            try:
                return ws <= d <= we
            except Exception:
                return False

        todo: List[RawItem] = []
        for it in self.items:
            if not it.url:
                stat["跳过_无URL"] += 1
                continue
            if it.abstract_body:
                # 搜索成稿条目已由 _fetch_search_bodies 并发补过，不重复请求
                stat["跳过_已有正文"] = stat.get("跳过_已有正文", 0) + 1
                continue
            if not is_title_echo(it.title, it.abstract, it.source):
                stat["跳过_摘要已有效"] += 1
                continue
            todo.append(it)
        in_win = sum(1 for it in todo if _in_window(it))
        # 先全量收集 → 窗口内优先排序 → 再按配额截断（旧版在排序前截断，
        # 前 160 个恰好都是旧页时窗口内条目一个都补不到）
        todo.sort(key=lambda it: (0 if _in_window(it) else 1, it.seq))
        stat["待补候选总数"] = len(todo)
        stat["其中窗口内"] = in_win
        stat["配额外未补（如实留痕）"] = max(0, len(todo) - limit)
        todo = todo[:limit]
        stat["窗口内优先"] = sum(1 for it in todo if _in_window(it))
        stat["窗口外排队（有余量才补）"] = len(todo) - stat["窗口内优先"]
        stat["尝试"] = len(todo)

        def one(it: RawItem):
            body = fetch_article_text(it.url, timeout=float(self.live.get("article_timeout", 4)))
            how = "trafilatura"
            if not body and jina_on:
                body = free_sources.jina_reader_text(it.url, timeout=jina_timeout)
                how = "jina"
            return body, how

        if todo:
            ex = ThreadPoolExecutor(max_workers=8)
            try:
                futs = {ex.submit(one, it): it for it in todo}
                pending, t0, jina_used = set(futs), _time.time(), 0
                while pending and (_time.time() - t0) < deadline:
                    fin, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                    for fu in fin:
                        it = futs[fu]
                        try:
                            body, how = fu.result()
                        except Exception:
                            body, how = "", "trafilatura"
                        if how == "jina":
                            stat["Jina回退尝试"] += 1
                            jina_used += 1
                        if body:
                            it.abstract_body = body
                            it.evidence = "A"
                            stat["成功"] += 1
                            if how == "jina":
                                stat["Jina回退成功"] += 1
                        else:
                            stat["失败"] += 1
                for fu in pending:
                    fu.cancel()
                    stat["批量超时取消"] += 1
                    stat["失败"] += 1
                if jina_used < jina_max:
                    stat["Jina额度未用满"] = jina_max - jina_used
            finally:
                ex.shutdown(wait=False, cancel_futures=True)
        self.article_stats = stat
        return stat

    # ---------- 总入口 ----------
    def run(self, mode: str, inbox_path: str | None = None) -> List[RawItem]:
        if mode == "target":
            from .target_source import TargetSource
            ts = TargetSource(self.cfg, self.registry, self.win, self.base)
            probe = ts.probe_live()
            items = ts.build(probe)
            self.items.extend(items)
            self.target_meta = ts.meta
            self.registry.mark(
                "TARGET-001", hit=bool(items), shape="独立原文 / 词条回查成稿",
                count=len(items), full_enumeration="是", adapter="target",
                gap="" if items else "内置标准语料为空")
            return self.items
        if mode in ("demo", "inbox", "hybrid") and inbox_path:
            self.collect_inbox(inbox_path, demo=(mode != "inbox"))
        if mode in ("live", "hybrid"):
            from .budget import BudgetExpired
            from . import runlog
            try:
                self._run_live()
            except BudgetExpired as exc:
                runlog.log("WARN", "采集层运行预算耗尽", 原因=str(exc),
                           已抓条数=len(self.items))
                self.skipped.append({"渠道": "全局", "原因": str(exc),
                                     "处置": "保留已抓条目，继续筛选评级与五工作表出表"})
        return self.items

    def _run_live(self) -> None:
        """live 采集主体；独立成函数便于预算异常时整体短路而不丢已抓数据。"""
        if True:
            from .live_cn import fetch_cn_sources
            specs, meta = fetch_cn_sources(self.cfg, self.win)
            self.cn_meta = meta
            for s in specs:
                feed = {"channel": s["source"], "channel_id": s["raw"]["feed_id"],
                        "section": "真实联网源 / " + s["raw"]["feed_id"], "level": "★"}
                it = self._add(ch=feed, title=s["title"], abstract=s.get("abstract", ""),
                               source=s["source"], url=s.get("url", ""), pub=s.get("pub"),
                               pub_raw=s.get("pub_raw", "未取得"), basis=s.get("basis", ""),
                               mark=s.get("mark", ""), evidence=s.get("evidence", "B"),
                               shape=s.get("shape", "独立原文"),
                               has_event=s.get("has_event", True),
                               record_type=s.get("record_type", "独立原文"),
                               group=s.get("group", ""), tag=s.get("tag", ""),
                               raw=s.get("raw", {}))
                it.abstract_body = s.get("abstract", "") if s.get("raw", {}).get("body_fetched") else ""
            for r in meta.get("逐源触达", []):
                ch = {"channel": r["name"], "channel_id": r["id"],
                      "section": "真实联网源", "level": "★", "full_enumeration": "是"}
                if r["status"] == "未取得":
                    self._fail(ch, r["type"], str(r.get("detail", {})))
                else:
                    self._ok(ch, r["type"], "结构化时间倒排 / RSS / API", r["条数"], "是")
            self.article_stats = meta.get("正文补采", {})

            # 国际源组不可达时跳过旧采集器（避免 21 个端点逐个超时），如实申报；
            # GitHub Actions 等 unrestricted 网络下正常启用全部端点。
            intl_ok = bool(meta.get("国际源组探测", {}).get("ok"))
            if intl_ok and self.live.get("rss", True):
                self.collect_rss()
            if intl_ok and self.live.get("apis", True):
                self.collect_apis()
            if intl_ok and self.live.get("html_lists", True):
                self.collect_html_lists()
            if self.live.get("hotboards", False):
                self.collect_hotboards()
            # 搜索回查默认开启：有 key 走 Serper/Tavily，无 key 自动降级 Bing 站内
            # （本沙盒实测 200 可解析）；不再因为没配 key 就整条链路废掉。
            if self.live.get("search", True):
                self.collect_search()
            # 正文补采不再受「国际源组是否可达」限制——国内源 / 搜索成稿条目
            # 同样需要从独立原文 URL 补正文，旧版 intl_ok 门槛会把它们全部漏掉。
            if self.live.get("fetch_article", True):
                self._fetch_search_bodies()   # 搜索成稿优先并发补，不吃 snippet
                self._fetch_articles()        # 其余条目顺序补，跳过已有正文的
