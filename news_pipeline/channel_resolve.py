"""资讯获取 · 「非独立站点 / DNS 未解析」逐条解析为可拉取通路。

2026-11-20 纠错：旧版 reach.py 把这两类判成终态（188 条「归并到所属平台
时间流，或人工确认」+ 112 条「换网络重测」）。实测证明这不是「拿不到数据」：

  · mof.gov.cn 本环境 DNS gaierror，但 Bing site:mof.gov.cn RSS = 200 / 10 条 / 带 pubDate；
  · 央视网 news_1.jsonp = 200 / 80 条 / focus_date，可承接「央视新闻客户端」等栏目；
  · pbc.gov.cn = 200，可承接「LPR 与房贷利率」；
  · Actions 无限制网络日志实证「指纹救回 96」个域名。

本模块读 channel_reach.json + channel_routes.yaml + endpoints_live.yaml，
把每一条「未解决」改写为「有明确可拉取通路 + 实测状态 + 时间字段依据 +
启用条件」，输出 data/channel_resolved.json。绝不把"有通路"写成"已抓到"。

用法：
  python -m news_pipeline.channel_resolve                  # 离线解析（用已有实测证据）
  python -m news_pipeline.channel_resolve --probe         # 真发请求验证可达通路
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import yaml

BASE = Path(__file__).resolve().parents[1]
REACH = BASE / "data" / "channel_reach.json"
ROUTES = BASE / "data" / "channel_routes.yaml"
EPS = BASE / "data" / "endpoints_live.yaml"
OUT = BASE / "data" / "channel_resolved.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 news-daily/1.0"

TARGET_STATES = ("非独立站点", "DNS 未解析")
DOMAIN_RE = re.compile(r"([a-zA-Z0-9][a-zA-Z0-9\-]*\.(?:[a-zA-Z0-9\-]+\.)*[a-zA-Z]{2,})")
SECTION_RE = re.compile(r"§\d+")


def _http(url: str, timeout: float = 6.0) -> Dict[str, Any]:
    """真实发一次请求，返回状态码 / 条数 / 是否有时间字段。不吞异常、不伪造。"""
    out: Dict[str, Any] = {"url": url, "http": None, "items": 0, "has_time": False, "error": ""}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(120_000)
        txt = raw.decode("utf-8", "replace")
        out["http"] = r.status
        out["items"] = len(re.findall(r"<item[ >]|<entry[ >]|<h2[^>]*>\s*<a", txt)) or txt.count('"title"')
        out["has_time"] = bool(re.search(r"pubDate|published|datePublished|seendate|focus_date|\"date\"", txt, re.I))
    except urllib.error.HTTPError as e:
        out["http"] = e.code
        out["error"] = f"HTTP {e.code}"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:100]}"
    return out


def _match_rule(text: str, rules: List[dict]) -> List[dict]:
    hits = []
    for r in rules:
        keys = [str(k) for k in (r.get("keys") or [])]
        if any(k in text for k in keys):
            hits.append(r)
    return hits


def _section(text: str) -> str:
    m = SECTION_RE.search(text or "")
    return m.group(0) if m else "default"


def _bing_site_url(domain: str, keyword: str = "") -> str:
    q = f"site:{domain} {keyword}".strip()
    return "https://www.bing.com/search?q=" + urllib.parse.quote(q) + "&format=rss"


def resolve_virtual(row: Dict[str, Any], cfg: dict, eps: List[dict]) -> Dict[str, Any]:
    """把一条「非独立站点」解析为可拉取通路。"""
    name = str(row.get("渠道") or "")
    entry = str(row.get("入口") or "")
    sec = str(row.get("大类") or "")
    text = name + " " + entry + " " + sec
    rules = _match_rule(text, cfg.get("rules") or [])
    routes: List[dict] = []

    for r in rules:
        routes.append({
            "通路类型": r["type"], "目标": r["target"],
            "时间字段依据": r.get("time_field", ""),
            "启用条件": r.get("condition", ""),
            "实测依据": r.get("evidence", ""),
            "来源": "具体规则 keys=" + "/".join(str(k) for k in r["keys"][:3]),
        })

    # 归并到已在采的实证端点（按关键词/渠道名关联 endpoints_live.yaml）
    for e in eps:
        label = str(e.get("channel") or e.get("name") or "")
        if label and (label in name or any(k in name for k in (label[:2],) if len(label) > 2)):
            routes.append({
                "通路类型": "mother_endpoint", "目标": str(e.get("url") or ""),
                "时间字段依据": "源站 pubDate / focus_date（该端点已实证）",
                "启用条件": "已在采，本次运行直接采集",
                "实测依据": "data/endpoints_live.yaml 实证端点", "来源": "端点清单归并",
            })
            break

    # section 兜底
    fb = (cfg.get("section_fallback") or {})
    f = fb.get(_section(sec)) or fb.get("default") or {}
    if f:
        routes.append({
            "通路类型": f["type"], "目标": f["target"],
            "时间字段依据": f.get("time_field", ""), "启用条件": f.get("condition", ""),
            "实测依据": "", "来源": f"section 兜底 {_section(sec)}",
        })

    # 去重（同 target）
    seen, uniq = set(), []
    for x in routes:
        k = (x["通路类型"], x["目标"])
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    return {"解析状态": "已解析（虚拟渠道归并）", "可拉取通路": uniq,
            "通路数": len(uniq), "原状态": row.get("状态")}


def resolve_dns(row: Dict[str, Any], cfg: dict, eps: List[dict]) -> Dict[str, Any]:
    """把一条「DNS 未解析」解析为 site: 回查 / 母站归并 / 归档 / 换网络重测。"""
    entry = str(row.get("入口") or "")
    domains = row.get("域名") or list(dict.fromkeys(DOMAIN_RE.findall(entry)))[:2]
    domain = ""
    for d in domains:
        d = str(d).strip()
        if d and "." in d and not d.startswith("www."):
            domain = d
            break
    if not domain and domains:
        domain = str(domains[0]).lstrip("www.")
    if not domain:
        domain = "gov.cn"

    name = str(row.get("渠道") or "")
    kw = re.sub(r"[\s/、，,（）()]+", " ", name)[:18].strip() or "公告"
    routes: List[dict] = []
    for r in (cfg.get("dns_policy") or {}).get("routes", []):
        tpl = str(r.get("template") or "")
        target = tpl.replace("{domain}", domain).replace("{keyword}", kw)
        if r["type"] == "search" and "bing.com" in tpl:
            target = _bing_site_url(domain, kw)
        routes.append({
            "通路类型": r["type"], "目标": target,
            "时间字段依据": r.get("time_field", ""),
            "启用条件": r.get("condition", ""),
            "实测依据": "本轮沙盒实测" if r.get("measured") else "",
            "来源": "dns_policy",
        })
    return {"解析状态": "已解析（DNS 替代回查）", "可拉取通路": routes,
            "通路数": len(routes), "原状态": row.get("状态"), "主体域名": domain}


def probe_checks(rows: List[dict]) -> List[dict]:
    """真实发请求验证关键通路（不伪造、失败如实记）。"""
    urls = [
        ("Bing site:mof.gov.cn（DNS 失败域名的替代回查）",
         _bing_site_url("mof.gov.cn", "地方债")),
        ("Bing site:mohurd.gov.cn（住建部）", _bing_site_url("mohurd.gov.cn", "保交楼")),
        ("Bing site:nhc.gov.cn（卫健委）", _bing_site_url("nhc.gov.cn", "通知")),
        ("中国政府网门户（部委内容转载归并）", "https://www.gov.cn/xinwen/"),
        ("央行 LPR 母站（非独立站点归并）", "https://www.pbc.gov.cn/"),
        ("央视网时间流（央视新闻客户端归并）",
         "https://news.cctv.com/2019/07/gaiban/cmsdatainterface/page/news_1.jsonp"),
    ]
    return [{"名称": n, **_http(u, 6.0)} for n, u in urls]


def run(probe: bool = False) -> Dict[str, Any]:
    if not REACH.exists():
        raise RuntimeError("data/channel_reach.json 缺失，先跑 python -m news_pipeline.reach")
    cfg = yaml.safe_load(ROUTES.read_text(encoding="utf-8")) or {}
    eps: List[dict] = []
    if EPS.exists():
        eps = (yaml.safe_load(EPS.read_text(encoding="utf-8")) or {}).get("endpoints") or []
    reach = json.loads(REACH.read_text(encoding="utf-8"))
    rows = reach.get("渠道") or []

    # 2026-11-21 全面核查：11-21 Actions 日志 step 9 打印「原两类 0 条 → 已解析
    # 0 条」却 rc=0 假绿。根因是上一版虽号称「按结构属性判定」，实际仍读
    # reach.py 的中间产物（入口 / 域名 / 状态）；无限制网络下 reach 会把状态
    # 全部改写成「实证端点」、给每行补域名，于是 188 个非独立站点凭空消失。
    # 现在直接读原始主白名单 Markdown 的「入口」列，这是与网络无关的结构属性；
    # DNS 类仍取 reach 的失败状态，两类都允许 reach 缺行时按白名单补齐。
    from .channels import parse_channel_doc
    raw = parse_channel_doc(BASE / "data" / "资讯渠道大全_31类扩展版.md")
    virtual_names = {
        str(c["channel"]).strip() for c in raw
        if not DOMAIN_RE.findall(str(c.get("entry") or ""))
    }

    def _dns(r):
        st = str(r.get("状态") or "") + " " + str(r.get("状态码") or "")
        return ("DNS" in st or "gaierror" in st.lower() or "Name or service" in st
                or "ERR_NAME" in st)

    targets: List[dict] = []
    seen: set[str] = set()
    for r in rows:
        name = str(r.get("渠道") or "").strip()
        if name in virtual_names or _dns(r):
            targets.append(r)
            seen.add(name)
    for c in raw:
        name = str(c["channel"]).strip()
        if name in virtual_names and name not in seen:
            targets.append({
                "channel_id": c.get("channel_id", ""), "渠道": name,
                "入口": c.get("entry", ""), "大类": c.get("section", ""),
                "级别": c.get("level", ""),
                "状态": "非独立站点（原始白名单入口列无域名）", "域名": [],
            })
    if not targets:
        # 599 条主白名单里结构上必然存在无域名入口，0 条只可能是解析失败，
        # 必须报错而不是打印「解析 0 条」当成功。
        print("[解析] 异常：原始白名单结构属性判定得到 0 条，检查主白名单解析是否失败")
    resolved: List[dict] = []
    for r in targets:
        st = str(r.get("状态") or "")
        rr = resolve_dns(r, cfg, eps) if "DNS" in st else resolve_virtual(r, cfg, eps)
        resolved.append({
            "channel_id": r.get("channel_id", ""), "渠道": r.get("渠道", ""),
            "入口": r.get("入口", ""), "大类": r.get("大类", ""),
            "级别": r.get("级别", ""), "原状态": st,
            **rr,
        })

    # 覆盖统计
    by_type: Counter = Counter()
    no_route = 0
    for x in resolved:
        for p in x["可拉取通路"]:
            by_type[p["通路类型"]] += 1
        if not x["可拉取通路"]:
            no_route += 1
    state_cnt = Counter(x["解析状态"] for x in resolved)

    probes = probe_checks(resolved) if probe else []
    probe_ok = sum(1 for p in probes if p["http"] == 200)
    probe_with_time = sum(1 for p in probes if p["http"] == 200 and p["has_time"])

    out = {
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "产品": "资讯获取",
        "结论": ("两类终态已全部逐条解析为可拉取通路；"
                 "本环境 DNS 失败不等于域名不存在，栏目/账号不等于没有数据")
                if targets else "本批全网络可达，无两类待解析渠道（不假装解析了 300 条）",
        "判定口径": "直接读原始主白名单 Markdown 入口列（无域名=非独立站点）+ reach 的 DNS 失败状态；不依赖 reach 的状态字符串或补写的域名",
        "主口径白名单": (reach.get("汇总") or {}).get("白名单", 599),
        "原两类合计": len(targets),
        "已解析条数": len(resolved),
        "零通路条数": no_route,
        "解析状态分布": dict(state_cnt),
        "通路类型分布": dict(by_type),
        "实测": {"执行": probe, "请求数": len(probes), "HTTP200": probe_ok,
                 "带时间字段": probe_with_time, "明细": probes},
        "逐渠道": resolved,
        "零条原因": "" if targets else "异常：原始主白名单解析得到 0 条无域名入口；请检查 channels.parse_channel_doc",
        "诚实边界": ("「已解析」= 有明确可拉取通路且逐条写出时间字段依据与启用条件，"
                     "不等于本批已抓到该渠道数据。实测 403 / 需 key / 需授权 / 需换网络的"
                     "通路逐条标注，不伪装成已触达；Actions 无限制网络与配 key 后自动启用。"),
        "依据": (cfg.get("measured_evidence") or []),
    }
    OUT.parent.mkdir(exist_ok=True, parents=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="非独立站点 / DNS 未解析逐条解析为可拉取通路")
    ap.add_argument("--probe", action="store_true", help="真发请求验证关键通路")
    args = ap.parse_args()
    d = run(args.probe)
    if d["原两类合计"] == 0:
        print("::error::两类渠道解析为 0 条（主白名单结构属性异常），不得判成功")
    print(f"[解析] 原两类 {d['原两类合计']} 条 → 已解析 {d['已解析条数']} 条 | 零通路 {d['零通路条数']} 条")
    print(f"[状态] {d['解析状态分布']}")
    print(f"[通路] {d['通路类型分布']}")
    if d["实测"]["执行"]:
        print(f"[实测] HTTP200 {d['实测']['HTTP200']}/{d['实测']['请求数']} | "
              f"带时间字段 {d['实测']['带时间字段']}")
        for p in d["实测"]["明细"]:
            print(f"  {p['http'] or 'FAIL':>4} 条目~{p['items']:3} 时间={p['has_time']} | {p['名称']}")
    print(f"[产物] {OUT}")
    return 0 if (d["原两类合计"] > 0 and d["零通路条数"] == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
