"""央视网 cmsdatainterface JSONP 采集器（免 key、海外可达、当天秒级时间）。

探测结论（2026-11-23 真发请求）：
  https://news.cctv.com/2019/07/gaiban/cmsdatainterface/page/{channel}_{n}.jsonp
  返回 `news({...})` / `world({...})`，data.list 每页 80 条，字段含
  title / brief（真实正文导语 59–191 字）/ focus_date（秒级）/ url / keywords。
  旧版 sources.json 一个 CCTV 端点都没挂，而人民网/新华网 RSS 停在 2025-06 / 2022，
  31 类里 12 个「央媒」端点 82% 的 raw 全是超窗死 feed —— 这是「渠道太少」的头号根因。

单端点失败隔离、最多 3 页、带 ETag 无则不带；绝不把 jsonp 壳当正文。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import httpx

from newsbrief.registry import source

_JSONP = re.compile(r"^\s*\w+\((.*)\)\s*;?\s*$", re.S)


def _parse(text: str) -> list[dict]:
    m = _JSONP.match(text or "")
    if not m:
        raise ValueError("非 JSONP 响应（可能是 HTML 错误页）")
    data = json.loads(m.group(1))
    lst = (data.get("data") or {}).get("list") or []
    if not isinstance(lst, list):
        raise ValueError("data.list 不是数组")
    return lst


@source("jsonp")
def fetch(client: httpx.Client, ep: dict, timeout: float) -> list[dict]:
    pages = int(ep.get("pages", 2))
    win = ep.get("_window")
    base = ep["url"]
    out, seen = [], set()
    for page in range(1, pages + 1):
        u = re.sub(r"_(\d+)\.jsonp", f"_{page}.jsonp", base) if page > 1 else base
        r = client.get(u, timeout=timeout)
        r.raise_for_status()
        for x in _parse(r.text):
            title = str(x.get("title") or "").strip()
            url = str(x.get("url") or "").strip()
            if not title or not url or url in seen:
                continue
            seen.add(url)
            # brief 本身就是完整导语；keywords 是元数据不是事实正文，
            # 拼进去只会制造无句读尾巴，反而让摘要契约整条拒收。
            body = str(x.get("brief") or "").strip()
            pub = str(x.get("focus_date") or "").strip()
            # focus_date 是 Unix 秒/毫秒字符串；不转成 ISO 的话下游 parse_pub
            # 认不出，窗口闸会把「无发布时间」一律放行，9 月旧稿就混进主表。
            if pub.isdigit():
                v = int(pub)
                if v > 10 ** 11:
                    v //= 1000
                pub = datetime.fromtimestamp(v, timezone(timedelta(hours=8))).isoformat()
            # 栏目页是「全量列表」不是「窗口内新增」：采集层就按左开右闭过滤，
            # 避免把 9 月 / 11-18 / 11-21 旧稿整批灌回下游制造几百条超窗留痕。
            if win:
                try:
                    d = datetime.fromisoformat(pub)
                    if not (win[0] < d <= win[1]):
                        continue
                except (ValueError, TypeError):
                    continue
            out.append({
                "title": title, "url": url, "body": body,
                "published": pub,
                # 同一家媒体的不同频道/端点只算 1 个独立信源（01 §6）；
                # 端点名保留在逐类申报里，不写进来源列造成假多渠道。
                "source": ep.get("source_name") or "央视网",
                "group_id": ep.get("group_id"),
                "group": ep.get("group_name") or ep.get("board") or "民生服务",
                "shape": "独立原文", "evidence": "A",
            })
    # 窗口内确实没稿时返回空（不是异常）：否则会触发回吐旧缓存，反而把
    # 上一批的稿混进「只采集窗口内数据」的当批。
    return out[: int(ep.get("max_items", 160))]
