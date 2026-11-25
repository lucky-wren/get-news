"""源注册表：NewsNow `getters.ts` + DailyHotApi `registry.ts` 的 Python 版。

- 目录扫描自动注册：加源 = 在 sources/ 加一个文件，核心代码零改动。
- @source 装饰器统一签名；单源异常不扩散，回吐上次成功缓存。
- 短超时 6s（DailyHotApi REQUEST_TIMEOUT），ETag/Last-Modified 走 feedparser。
- 源函数一律接收注入的 client（httpx.MockTransport 可零网络测试）。
"""
from __future__ import annotations

import importlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import httpx

ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = ROOT / "data" / "cache.json"
TIMEOUT = 6.0

REGISTRY: dict[str, Callable[[httpx.Client, dict, float], list[dict]]] = {}


def source(kind: str):
    """注册一个采集器；kind 与 sources.json 里端点的 type 对应。"""
    def deco(fn):
        if kind in REGISTRY:
            raise ValueError(f"重复注册采集器：{kind}")
        REGISTRY[kind] = fn
        return fn
    return deco


def discover() -> dict[str, Callable]:
    """扫 sources/*.py 并 import，等价于 import.meta.glob('./sources/*.ts')。"""
    pkg = Path(__file__).parent / "sources"
    for f in sorted(pkg.glob("*.py")):
        if f.name.startswith("__"):
            continue
        importlib.import_module(f"newsbrief.sources.{f.stem}")
    return REGISTRY


def load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _slim(rows: list[dict]) -> list[dict]:
    """落盘只保留可回吐的最小字段：真实首页正文可达数十 KB，缓存不能无界膨胀。"""
    out = []
    for r in rows[:30]:
        d = dict(r)
        if isinstance(d.get("body"), str):
            d["body"] = d["body"][:300]
        out.append(d)
    return out


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def _stale_in_window(row: dict, window: tuple) -> bool:
    """缓存回吐行必须自己带可解析且落在窗口内的时间，否则不回吐。"""
    from newsbrief.clean import parse_pub
    p = parse_pub(str(row.get("published") or ""))
    return p is not None and window[0] < p <= window[1]


def collect(cfg: dict, channels: dict, *, client: httpx.Client,
            timeout: float = TIMEOUT,
            window: tuple | None = None) -> tuple[list[dict], list[dict], dict]:
    """逐类采集 → (原始条目, 31 类状态申报, 失败清单)。

    错误当返回值：失败只影响该端点，写进 reports 缺口与 failed 字典，
    最终由 sheet.py 写进「口径与稿外信息」，绝不静默。
    """
    reg = discover()
    cache = load_cache()
    # 《资讯渠道清单》完整台账：691 渠道 / 503 URL 逐条登记，逐类申报
    # 「清单登记 / 实测可达 / 已接入活跃端点」，不把 403 入口伪装成命中。
    try:
        cat = json.loads((ROOT / "data" / "channel_catalog.json").read_text(encoding="utf-8"))
        # 按 entry.mapped_group 归到工程 31 类；清单章节号与工程类号并不一致。
        by_sec: dict[int, dict] = {}
        for e in cat.get("entries", []):
            ok = sum(1 for u in e.get("u", []) if isinstance(u, list) and len(u) > 1 and u[1] == 200)
            for gid in e.get("g", []):
                d = by_sec.setdefault(int(gid), {"ch": 0, "ok": 0})
                d["ch"] += 1
                d["ok"] += ok
        catalog_note = (f"《资讯渠道清单》登记 {cat.get('channels', 0)} 渠道 / "
                        f"{cat.get('unique_urls', 0)} 入口**全量挂为活跃端点**；"
                        f"本环境策略层拦截 {cat.get('blocked_urls', 0)} 仅作参考，"
                        "不据此裁剪端点（沙盒出口 No policy rule matched 非站点拒绝，"
                        "正式环境可达即采）；清单 URL 运行时全量展开，一个都不删"
                        + ("" if cfg["network"].get("catalog_expand", True)
                           else "（本轮为单测隔离，未展开清单端点）"))
    except (OSError, ValueError):
        by_sec, catalog_note = {}, "清单台账未随包，逐类申报以 sources.json 为准"

# 隔离开关默认 True：生产与 CI 全量展开清单，单测可显式关。
    expand_enabled = bool(cfg["network"].get("catalog_expand", True))
# 清单 URL 运行时全量展开，不按本环境探测裁剪端点。
    expand: dict[int, list[dict]] = {}
    static_urls = {e.get("url") for g in channels["groups"]
                   for e in g.get("endpoints", []) if e.get("url")}
    try:
        _cat = json.loads((ROOT / "data" / "channel_catalog.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _cat = {}
    for _e in (_cat.get("entries", []) if expand_enabled else []):
        for _pair in _e.get("u", []):
            if not (isinstance(_pair, list) and len(_pair) >= 2):
                continue
            _u, _status = _pair[0], _pair[1]
            if _u in static_urls:
                continue
            _low = _u.lower()
            # §31 免 key 基础设施：GDELT DOC 是 JSON artlist，RSSHub /
            # Google News RSS 是 feed。旧判据一律当 html 抽 <a>，必然 0 条。
            _type = ("jsonp" if ("jsonp" in _low or "cmsdatainterface" in _low)
                     else "geojson" if "geojson" in _low
                     else "jsonapi" if any(k in _low for k in
                         ("gdeltproject.org/api/v2/doc", "api.tavily",
                          "google.serper.dev", "newsapi.org", "eventregistry"))
                     else "rss" if any(k in _low for k in
                         ("rss", "/feed", "atom", ".xml", "rsshub.app",
                          "news.google.com/rss"))
                     else "html")
            _gid = int((_e.get("g") or [23])[0])
            expand.setdefault(_gid, []).append({
                "name": f"{_e['n']}（清单§{_e['s']}）", "url": _u, "type": _type,
                "level": _e.get("l") or "☆", "catalog_sec": _e.get("s"),
                "catalog_board": _e.get("b", ""), "probe_status": int(_status),
                "enabled": True, "max_articles": 4,
                "note": "清单 URL 运行时展开；probe_status 仅本环境参考，不决定是否启用"})
    try:
        _soc = json.loads((ROOT / "data" / "social_endpoints.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _soc = {}
    for _e in (_soc.get("endpoints", []) if expand_enabled else []):
        _gid = int(_e.get("group_id") or 27)
        expand.setdefault(_gid, []).append(
            {k: v for k, v in _e.items() if k != "group_id"})
    _n_expand = sum(len(v) for v in expand.values())

# 端点全真跑，单源失败隔离并回吐缓存。
    reports, raw, failed = [], [], {}
    budget = float(cfg["network"].get("total_budget_seconds", 300))
    workers = max(1, int(cfg["network"].get("concurrent_endpoints", 16)))
    t_start = time.monotonic()

# 端点一个不删：16 线程并发、可达优先、单源隔离、逐类申报。
    tasks: list[tuple[int, str, dict]] = []
    group_spans: list[tuple[dict, list[dict], int, int]] = []
    for g in channels["groups"]:
        eps = [e for e in g["endpoints"] if e.get("url")]
        eps += expand.get(int(g["id"]), [])       # 清单全量端点，一个都不删
        eps = sorted(eps, key=lambda e: (0 if int(e.get("probe_status") or 0) == 200 else 1))
        begin = len(tasks)
        for ep in eps:
            tasks.append((int(g["id"]), g["name"], ep))
        group_spans.append((g, eps, begin, len(tasks)))

    def _fetch(task: tuple[int, str, dict]) -> dict:
        gid, gname, ep = task
        out = {"gid": gid, "rows": [], "got": 0, "errs": [], "skip": False,
               "failed": None, "cache": None}
        if time.monotonic() - t_start > budget:
            out["skip"] = True
            out["errs"].append(f"{ep['name']}：全局预算 {budget:.0f}s 用尽，本轮跳过"
                               "（端点保留在配置里，非站点不可达）")
            return out
        key = ep["url"]
        fn = reg.get(ep.get("type", "rss"))
        if fn is None:
            out["errs"].append(f"{ep['name']}：未注册 type={ep.get('type')}")
            return out
        try:
            args = dict(ep, group_id=gid, group_name=gname,
                        max_articles=cfg["network"].get("html_articles_per_endpoint", 3),
                        endpoint_seconds=cfg["network"].get("endpoint_seconds", 14),
                        _cache=cache.get(key, {}), _window=window)
            rows = fn(client, args, min(timeout, cfg["network"]["timeout_seconds"]))
            meta = (rows[0].pop("_meta", {}) if rows and isinstance(rows[0], dict) else {})
            out["rows"] = rows
            out["got"] = len(rows)
            if rows and ep.get("type") != "social_hot":
                # 热榜词条是本批快照，不跨批回吐（§12.1 每轮独立采集）
                out["cache"] = (key, {"etag": meta.get("etag", ""),
                                      "modified": meta.get("modified", ""),
                                      "rows": _slim(rows), "ok": True})
            if not rows and ep.get("type") == "html":
                out["errs"].append(f"{ep['name']}：栏目页未解析出独立原文"
                                   "（整页首页/导航不作成稿，不编造条目）")
        except Exception as ex:                       # 单源失败不扩散
            stale = cache.get(key, {}).get("rows", [])
            kept_stale = []
            for row in stale:                        # 回吐旧缓存，逐行打标
                d = dict(row)
                d["stale"] = True
                # 只采集窗口内数据：缓存行若无当批可核对时间证据，
                # 不进 raw（否则几百条上一批旧稿会灌满超窗留痕）。
                if window and not _stale_in_window(d, window):
                    continue
                kept_stale.append(d)
            out["rows"] = kept_stale
            out["got"] = len(kept_stale)
            msg = re.sub(r"\s+", " ", str(ex))[:80]
            resp = getattr(ex, "response", None)
            blocked = resp is not None and b"policy_default_denied" in bytes(resp.content[:400])
            tag = ("出口策略拒绝（非站点故障，正式环境/代理可达即采）: "
                   + type(ex).__name__ if blocked else f"{type(ex).__name__}: {msg}")
            out["failed"] = (ep["name"], tag)
            out["errs"].append(f"{ep['name']}：{tag}"
                               + ("，已回吐旧缓存" if stale else "，无旧缓存本类缺数据"))
        return out

    # pool.map 按输入顺序返回：候选池展示序与逐类申报顺序保持确定，不受并发影响。
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_fetch, tasks))

    for g, eps, begin, end in group_spans:
        got, errs = 0, []
        group_skips = 0
        for out in results[begin:end]:
            raw.extend(out["rows"])
            got += out["got"]
            errs.extend(out["errs"])
            group_skips += int(out["skip"])
            if out["failed"]:
                failed[out["failed"][0]] = out["failed"][1]
            if out["cache"]:
                cache[out["cache"][0]] = out["cache"][1]
        sec = int(g["id"])
        cs = by_sec.get(sec, {})
        reports.append({
            "类目": f"§{sec} {g['name']}",
            "覆盖渠道": " · ".join(e["name"] for e in g["endpoints"]) or "（无公开免鉴权端点）",
            "本轮命中": f"{len(eps)} 端点 / {got} 条",
            "_got": got,
            "清单登记渠道": cs.get("ch", 0),
            "清单可达入口": cs.get("ok", 0),
            "已接入活跃端点": len(eps),
            "成稿条数": 0,
            # 01 §1：会话/仓库链路均为相关性 top-N，能按窗口时间倒排枚举全量的渠道为 0。
            "能否按窗口枚举全量": "否",
            "缺口": ("；".join(errs) + "；" if errs else "")
                    + (g.get("note") or "")
                    + "；本轮为相关性可见子集，无站内时间倒排与四时点批量拉取，"
                      "按窗口枚举全量的渠道数为 0（采集缺口如实申报，非链路故障）；"
                      + f"清单口径：{catalog_note}；"
                      + f"本轮并发 {workers} 线程、可达端点优先（端点未删除）；"
                      "单源失败已隔离，不影响其他类",
        })
        if group_skips:
            reports[-1]["缺口"] += f"；本组预算跳过 {group_skips} 个端点（端点未删除）"
    save_cache(cache)
    return raw, reports, failed
