"""社交平台主干采集器（需求说明书 §4.2：社交平台是主干渠道）。

设计要点：
1. 「一个都不删」：`urls[]` 多候选（直连 + 移动端 + 聚合镜像），运行时逐个
   自适应。本环境沙盒出口策略层返回的 403 `No policy rule matched` 不是
   目标站点拒绝，**不得据此把端点从配置里裁掉**；正式环境可达即采。
2. 词条逐条拆开，不整页丢弃；只输出「话题名 + 热度 + 快照时点 + 榜单 URL」，
   `shape=热榜词条`、**不编事件正文**。由 pipeline 回查到独立原文后，
   发现渠道才与真实媒体一起写进主表来源列（§8）。
3. httpx 全失败时读本批「平台通道真实快照」`data/social_snapshots.json`：
   这是本轮联网采集的榜单快照、带 captured_at 与 source_url，不是回放
   历史 xlsx；窗口外快照一律不读（§12.1 每轮独立采集）。
4. 回查不到独立原文的词条 → 附录 15 留痕，不扩写、不冒充成稿。

注册为 `social_hot`，与 rss/html/jsonp 一样由 registry.discover() 自动发现。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import httpx

from newsbrief.registry import source

ROOT = Path(__file__).resolve().parents[2]
SNAP = ROOT / "data" / "social_snapshots.json"
TZ = timezone(timedelta(hours=8))

# 站点功能文本 / 导航壳，不是热榜词条
_NAV = re.compile(
    r"登录|注册|下载|客户端|扫一扫|分享到|导航|首页|更多|版权|备案|"
    r"无障碍|阅读模式|友情链接|网站地图|打开|App|APP|查看|热议榜|"
    r"热搜榜|排行榜|实时热点|加载|失败|暂无|placeholder|PLACEHOLDER|"
    r"点击查看|查看详情|广告|推广|赞助", re.I)
# 榜单条目：中文为主、长度 5..60，可含数字/英文/标点
_TOPIC = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fff0-9A-Za-z·：:，,。！？!?、\s\-—“”‘’《》()（）]{4,59}$")


class _Anchors(HTMLParser):
    def __init__(self):
        super().__init__()
        self.buf: list[str] = []
        self._in = False
        self._cur = ""

    def handle_starttag(self, tag, attrs):
        if tag in ("a", "span", "div", "td", "li"):
            self._in = True
            self._cur = ""

    def handle_data(self, data):
        if self._in:
            self._cur += data

    def handle_endtag(self, tag):
        if self._in and tag in ("a", "span", "div", "td", "li"):
            t = re.sub(r"\s+", " ", self._cur).strip()
            if t:
                self.buf.append(t)
            self._in = False


def _clean_topic(t: str) -> str:
    t = re.sub(r"^\s*\d+[.、\)]\s*", "", t)
    t = re.sub(r"\s*[\d.]+万?\s*$", "", t)      # 尾部热度
    t = re.sub(r"\s*(热|新|沸|爆|商|荐)\s*$", "", t)
    return t.strip(" ·|-—　")


def _parse_html(text: str, platform: str, url: str, cap: int) -> list[dict]:
    p = _Anchors()
    try:
        p.feed(text)
    except Exception:
        return []
    out, seen = [], set()
    for raw in p.buf:
        t = _clean_topic(raw)
        if not (5 <= len(t) <= 60) or not _TOPIC.match(t):
            continue
        if _NAV.search(t) or t in seen:
            continue
        seen.add(t)
        out.append({
            "title": t,
            "url": url,
            "body": "",
            "published": "",
            "source": platform,
            "shape": "热榜词条",
            "evidence": "B",
            "discovery": platform,
            "hot": "",
            "group": "社交平台主干",
        })
        if len(out) >= cap:
            break
    return out


def _parse_json(text: str, platform: str, url: str, cap: int) -> list[dict]:
    try:
        d = json.loads(text)
    except (ValueError, TypeError):
        return []
    # 常见结构：{data:[...]}, {word_list:[...]}, {list:[...]}, 直接数组
    if isinstance(d, list):
        arr = d
    else:
        arr = []
        for key in ("data", "word_list", "list", "items", "result", "hot_list"):
            v = d.get(key) if isinstance(d, dict) else None
            if isinstance(v, list):
                arr = v
                break
            if isinstance(v, dict):
                for k2 in ("list", "items", "data"):
                    if isinstance(v.get(k2), list):
                        arr = v[k2]
                        break
                if arr:
                    break
    out = []
    for x in arr[:cap * 2]:
        if not isinstance(x, dict):
            continue
        t = ""
        for k in ("word", "title", "name", "query", "hot_word", "note", "desc", "question"):
            v = x.get(k)
            if isinstance(v, str) and 5 <= len(v.strip()) <= 80:
                t = v.strip()
                break
        t = _clean_topic(t)
        if not t or _NAV.search(t) or not _TOPIC.match(t):
            continue
        out.append({
            "title": t,
            "url": str(x.get("url") or x.get("link") or url),
            "body": str(x.get("desc") or x.get("description") or "")[:200],
            "published": str(x.get("event_time") or x.get("time") or ""),
            "source": platform,
            "shape": "热榜词条",
            "evidence": "B",
            "discovery": platform,
            "hot": str(x.get("hot_value") or x.get("hot") or x.get("heat") or ""),
            "group": "社交平台主干",
        })
        if len(out) >= cap:
            break
    return out


def _snapshots(platform: str, cap: int, window: tuple | None) -> list[dict]:
    """本批平台通道真实榜单快照；窗口外一律不读，避免历史回放。"""
    if not SNAP.is_file():
        return []
    try:
        data = json.loads(SNAP.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for s in data.get("snapshots", []):
        if s.get("platform") != platform:
            continue
        try:
            at = datetime.fromisoformat(s["captured_at"])
            if at.tzinfo is None:
                at = at.replace(tzinfo=TZ)
        except (KeyError, ValueError):
            continue
        if window and not (window[0] < at <= window[1]):
            continue
        for e in s.get("entries", [])[:cap]:
            t = _clean_topic(str(e.get("title") or ""))
            if not t or _NAV.search(t) or not _TOPIC.match(t):
                continue
            out.append({
                "title": t,
                "url": str(e.get("url") or s.get("source_url", "")),
                "body": str(e.get("desc") or "")[:200],
                "published": s["captured_at"],
                "source": platform,
                "shape": "热榜词条",
                "evidence": "B",
                "discovery": platform,
                "hot": str(e.get("hot") or ""),
                "group": "社交平台主干",
                "channel": "平台通道快照（本轮联网采集，非历史 xlsx 回放）",
                "source_url": s.get("source_url", ""),
            })
    return out[:cap]


@source("social_hot")
def fetch(client: httpx.Client, ep: dict, timeout: float) -> list[dict]:
    platform = ep["name"]
    urls = [u for u in (ep.get("urls") or [ep.get("url")]) if u]
    cap = int(ep.get("max_items", 50))
    window = ep.get("_window")

    # ① 本批平台通道快照：每轮必采，不是「直连失败才兜底」。
    # 早期版本做成二选一，直连拿到站点首页/导航壳就直接 return，本批真实
    # 榜单快照根本没进链路，9 个主干平台在来源列里集体消失。窗口外不读。
    rows = _snapshots(platform, cap, window)
    for r0 in rows:
        r0["channel"] = "本批平台通道榜单快照（captured_at 可核对）"

    # ② httpx 直连（正式环境主路径）：多候选逐个试，结果与快照按词条去重合并
    seen = {str(r0.get("title") or "").strip() for r0 in rows}
    for u in urls:
        try:
            r = client.get(u, timeout=timeout,
                           headers={"Accept-Language": "zh-CN,zh;q=0.9"})
            if r.status_code != 200 or len(r.content) < 500:
                continue
            text = r.text
            ct = (r.headers.get("content-type") or "").lower()
            direct = (_parse_json(text, platform, u, cap) if "json" in ct
                      or text.lstrip().startswith(("{", "["))
                      else _parse_html(text, platform, u, cap))
            for d in direct:
                k = str(d.get("title") or "").strip()
                if k and k not in seen:
                    seen.add(k)
                    rows.append(d)
            if direct:
                break
        except Exception:
            continue

    if rows:
        rows[0]["_meta"] = {"via": "snapshot+direct",
                            "note": "本批快照必采，直连结果去重合并；窗口外不读"}
        return rows

    # ③ 全失败：返回空列表，由 registry 逐类如实申报缺口；端点仍保留在配置里
    return []
