"""RSS / Atom 采集器：feedparser 归一 + ETag/Last-Modified 条件请求。

feedparser 官方纪律：bozo 且无 entries 时判解析失败（很多站点返 200 但正文是 HTML 错误页），
由 registry 的 try/except 回吐旧缓存，单源阵亡不扩散。
"""
from __future__ import annotations

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
