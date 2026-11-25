"""RSS / Atom 采集器：feedparser 归一 + ETag/Last-Modified 条件请求。

feedparser 官方纪律：bozo 且无 entries 时判解析失败（很多站点返 200 但正文是 HTML 错误页），
由 registry 的 try/except 回吐旧缓存，单源阵亡不扩散。
"""
from __future__ import annotations

import re

import feedparser
import httpx

from newsbrief.registry import source


@source("rss")
def fetch(client: httpx.Client, ep: dict, timeout: float) -> list[dict]:
    c = ep.get("_cache") or {}
    headers = {}
    if c.get("etag"):
        headers["If-None-Match"] = c["etag"]
    if c.get("modified"):
        headers["If-Modified-Since"] = c["modified"]
    r = client.get(ep["url"], timeout=timeout, headers=headers)
    if r.status_code == 304:
        return [dict(x) for x in c.get("rows", [])]
    r.raise_for_status()
    d = feedparser.parse(r.text)
    if d.get("bozo") and not d.entries:
        raise ValueError(f"feed 解析失败：{d.get('bozo_exception')}")
    rows = []
    for e in d.entries[:60]:
        body = ""
        if e.get("content"):
            body = e["content"][0].get("value", "")
        body = body or e.get("summary", "") or ""
        rows.append({
            "title": e.get("title", ""), "url": e.get("link", ""),
            "body": body, "published": e.get("published", ""),
            "source": ep["name"], "group_id": ep.get("group_id"),
            "group": ep.get("group_name") or ep.get("board") or "民生服务",
            "shape": "独立原文", "evidence": "A",
        })
    if rows:
        rows[0]["_meta"] = {"etag": r.headers.get("etag", ""),
                            "modified": r.headers.get("last-modified", "")}
    return rows


@source("jsonapi")
def fetch_jsonapi(client: httpx.Client, ep: dict, timeout: float) -> list[dict]:
    """§31 免 key 结构化源：GDELT DOC artlist / Serper / Tavily / NewsAPI。

    GDELT 的 seendate 是「见到文章时间」而非原文首发时刻，按清单 §31
    逐条标注 evidence=B，由下游时间硬闸判定入窗，不冒充首发时间。
    """
    import json as _json
    from datetime import datetime, timezone

    r = client.get(ep["url"], timeout=timeout)
    r.raise_for_status()
    try:
        d = r.json()
    except ValueError as ex:
        raise ValueError(f"jsonapi 非 JSON 响应：{ex}")
    arts = (d.get("articles") or d.get("results") or d.get("organic")
            or d.get("news") or [])
    if not isinstance(arts, list):
        raise ValueError("jsonapi 未返回文章数组")
    rows = []
    for a in arts[: int(ep.get("max_articles", 60))]:
        if not isinstance(a, dict):
            continue
        title = str(a.get("title") or "").strip()
        url = str(a.get("url") or a.get("link") or "").strip()
        if not title or not url:
            continue
        raw_t = str(a.get("seendate") or a.get("publishedAt")
                    or a.get("date") or a.get("published") or "").strip()
        pub = ""
        if re.fullmatch(r"\d{8}T\d{6}Z", raw_t):       # GDELT 20261124T120000Z
            try:
                pub = datetime.strptime(raw_t, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pub = ""
        else:
            pub = raw_t
        rows.append({
            "title": title, "url": url,
            "body": str(a.get("content") or a.get("description")
                        or a.get("snippet") or "")[:1500],
            "published": pub, "source": ep["name"],
            "group_id": ep.get("group_id"),
            "group": ep.get("group_name") or ep.get("board") or "综合资讯",
            "shape": "独立原文", "evidence": "B",
            "time_note": ("GDELT seendate 为见到文章时间，非原文首发时刻" if raw_t else ""),
        })
    if rows:
        rows[0]["_meta"] = {"etag": r.headers.get("etag", ""),
                            "modified": r.headers.get("last-modified", "")}
    return rows
