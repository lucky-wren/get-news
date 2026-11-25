"""外部注入采集器：会话内相关性检索补强（01 §1 允许回查补强与并列标注）。

data/external_inbox.json 随工程交付，逐条带可核对 URL、发布时间与真实多渠道来源；
只做追加，不删改注册表真采集结果。早报/热榜拆出的词条单独留在 topics，
由 pipeline 写进附录 15 与候选池边界留痕，不冒充具备摘要的成稿。
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from newsbrief.registry import source

ROOT = Path(__file__).resolve().parents[2]


@source("inbox")
def fetch(client: httpx.Client, ep: dict, timeout: float) -> list[dict]:
    p = ROOT / ep["url"]
    if not p.exists():
        return []
    d = json.loads(p.read_text(encoding="utf-8"))
    rows = []
    for a in d.get("articles", []):
        r = dict(a)
        r["inbox"] = True
        r["source"] = r.get("sources", "")
        rows.append(r)
    return rows
