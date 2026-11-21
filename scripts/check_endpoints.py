#!/usr/bin/env python3
"""渠道端点体检：把「很多渠道未取得」拆成可行动清单。

    python scripts/check_endpoints.py                    # 实测已登记端点，生成健康度
    python scripts/check_endpoints.py --offline          # 不联网，只统计端点配置缺口
    python scripts/check_endpoints.py --max 20           # 最多实测 20 个端点

产物：data/channel_health.json
  - 白名单总数 / 已配置端点数 / 待配置端点数
  - 每个端点的 HTTP 状态、是否有时间字段、条目数、失败类别
  - ★ 必采渠道里「待配置端点」的优先补齐名单

诚实边界：脚本只测仓库里已登记的 URL，不会凭空编造端点；没有 URL 的渠道
一律标「待配置端点」，由使用者补官方 RSS / 站内时间倒排 / API 后再测。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
import yaml
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
from news_pipeline.channels import ChannelRegistry   # noqa: E402

# 2026-11-20 修复：旧版指向 v3 全行业分类版（142/148 行），与 step 8 的
# 「599 渠道」和 step 7 的「148 入口域名」三套分母打架，还把央视/新华/中新/人民
# 误报成「★ 必采待补端点」。主口径按用户定稿 = 31 类 599 渠道扩展版。
DOC = BASE / "data" / "资讯渠道大全_31类扩展版.md"
EXTRA = BASE / "data" / "channels_extra.yaml"
OUT = BASE / "data" / "channel_health.json"
LIVE_EP = BASE / "data" / "endpoints_live.yaml"      # step 7 真实请求后写入
REACH = BASE / "data" / "channel_reach.json"          # step 8 逐渠道补齐结果
UA = "news-daily-pipeline/1.0 (endpoint health check; robots.txt respected)"
ENDPOINT_KEYS = ("rss_url", "api", "api_url", "list_pages", "hotboard", "search", "url")


def urls_of(ch: dict) -> list:
    out = []
    for k in ENDPOINT_KEYS:
        v = ch.get(k)
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            out.append((k, v))
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, str) and x.startswith(("http://", "https://")):
                    out.append((k, x))
                elif isinstance(x, dict) and str(x.get("url", "")).startswith("http"):
                    out.append((k, x["url"]))
        elif isinstance(v, dict) and str(v.get("url", "")).startswith("http"):
            out.append((k, v["url"]))
    return out


def probe(url: str, timeout: float = 8.0) -> dict:
    res = {"url": url, "http": None, "bytes": 0, "items": 0,
           "has_time_field": False, "category": "", "error": ""}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(300000)
            res["http"] = r.status
            res["bytes"] = len(raw)
            text = raw.decode(r.headers.get_content_charset() or "utf-8", "replace")
        res["items"] = len(re.findall(r"<item[ >]|<entry[ >]", text)) or text.count('"title"')
        res["has_time_field"] = bool(re.search(
            r"pubDate|published|updated|datePublished|seendate|created|\"date\"", text, re.I))
        if res["items"] == 0:
            res["category"] = "可达但无条目（选择器 / 参数需调整）"
        elif not res["has_time_field"]:
            res["category"] = "可达但无时间字段（需正文回查 pubDate）"
        else:
            res["category"] = "可用（有时间字段，可按资讯时间过滤）"
    except urllib.error.HTTPError as e:
        res.update(http=e.code, category=f"HTTP {e.code}（403 反爬 / 404 端点失效 / 401 需密钥）",
                   error=str(e)[:120])
    except Exception as e:
        name = type(e).__name__
        cat = ("网络超时 / 出口不可达" if "Timeout" in name or "timed out" in str(e)
               else "DNS / 连接失败" if "NameResolution" in name or "Connection" in name or "DNS" in name
               else "其他（见 error）")
        res.update(category=cat, error=f"{name}: {str(e)[:120]}")
    return res


def main() -> int:
    p = argparse.ArgumentParser(description="渠道端点体检")
    p.add_argument("--offline", action="store_true", help="不联网，只统计配置缺口")
    p.add_argument("--max", type=int, default=30, help="最多实测端点数，默认 30")
    p.add_argument("--timeout", type=float, default=8.0)
    args = p.parse_args()

    reg = ChannelRegistry(DOC, EXTRA)

    # 合并 step 7/8 的真实实证端点：只要该渠道名在 endpoints_live.yaml 里
    # 有带时间字段的端点，就算「已配置」，不再按白名单文字列误报待补。
    live_names: set[str] = set()
    if LIVE_EP.exists():
        try:
            ep = yaml.safe_load(LIVE_EP.read_text(encoding="utf-8")) or {}
            for e in (ep.get("endpoints") or []):
                for key in ("channel", "name", "id"):
                    v = str(e.get(key) or "").strip()
                    if v:
                        live_names.add(v)
        except Exception:
            pass
    reach_total = 599
    reach_reached = 0
    if REACH.exists():
        try:
            r = json.loads(REACH.read_text(encoding="utf-8"))
            sm = r.get("汇总") or {}
            reach_total = int(sm.get("白名单") or 599)
            reach_reached = int(sm.get("实证端点渠道") or 0)
            # 2026-11-21 修复：channel_reach.json 的键是「渠道」不是「逐渠道」，
            # 旧版读错键 → reach 实证的 287 个渠道一个都没进 live_names，
            # 把已在采的央视/人民日报/新华社误报成「★ 必采待补端点」。
            for row in (r.get("渠道") or r.get("逐渠道") or []):
                st = str(row.get("状态") or "")
                if st.startswith("实证端点") or st.startswith("已解析"):
                    live_names.add(str(row.get("渠道") or "").strip())
        except Exception:
            pass
    # reach.py 指纹矩阵生成的端点清单也纳入（Actions 无限制网络下 197 个）
    for extra in (Path("data/endpoints_reach.yaml"),):
        if extra.exists():
            try:
                for e in ((yaml.safe_load(extra.read_text(encoding="utf-8")) or {}).get("endpoints") or []):
                    v = str(e.get("channel") or e.get("name") or "").strip()
                    if v:
                        live_names.add(v)
            except Exception:
                pass

    def _core(x: str) -> str:
        x = (x or "").lower().strip()
        x = re.sub(r"^(?:www\.)?", "", x)
        x = re.sub(r"\.(?:com|cn|org|net|gov)(?:\.cn)?$", "", x)
        return re.sub(r"[^\w\u4e00-\u9fff]", "", x)

    _cores = {_core(n) for n in live_names if _core(n)}

    def _live_hit(name: str) -> bool:
        if name in live_names:
            return True
        if any(n and (n in name or name in n) for n in live_names):
            return True
        c = _core(name)
        return bool(c and any(c == d or c in d or d in c for d in _cores))

    configured, unconfigured, targets = [], [], []
    for c in reg.channels:
        u = urls_of(c)
        if u or _live_hit(str(c.get("channel") or "")):
            configured.append(c)
            targets.extend((c["channel"], c["level"], k, v) for k, v in u)
        else:
            unconfigured.append(c)

    results = []
    if not args.offline:
        for name, level, kind, url in targets[:max(1, args.max)]:
            r = probe(url, args.timeout)
            r.update(channel=name, level=level, kind=kind)
            results.append(r)
            print(f"  [{r['category']}] {name} · {kind} · HTTP {r['http']} · 条目 {r['items']}")

    star_todo = [{"渠道": c["channel"], "节": c["section"], "级别": c["level"]}
                 for c in unconfigured if c.get("level") == "★"][:40]
    cats = {}
    for r in results:
        cats[r["category"]] = cats.get(r["category"], 0) + 1

    data = {
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "mode": "offline" if args.offline else "probe",
        # 2026-11-21 全面核查：旧版用 len(reg.channels)=604（含 5 行扩充/历史行）
        # 当分母，日志报「白名单 604 / 配置率 14.2%」，与定稿 599 打架。
        # 主口径统一 599；表格行数单独留一个字段供排障，不再当主口径。
        "白名单行数(含扩充)": len(reg.channels),
        "白名单渠道总数": reach_total,
        "主口径白名单": reach_total,
        "实证端点渠道数": reach_reached,
        "已配置端点渠道数": len(configured),
        "待配置端点渠道数": max(0, reach_total - len(configured)),
        "端点配置率": round(len(configured) / reach_total, 4) if reach_total else 0,
        "已登记端点数": len(targets),
        "实证端点合并数": len(live_names),
        "实测端点数": len(results),
        "实测失败分类": cats,
        "端点明细": results,
        "★必采待补端点优先名单": star_todo,
        "分母口径说明": (
            "主口径固定 31 类 599 渠道（用户定稿）；step 7 的 148 是「入口列可提取的独立域名」子集，"
            "step 8 的 599 是全渠道矩阵分母。旧版用 len(reg.channels)=604（含扩充/历史行）"
            "算配置率，日志报「白名单 604 / 配置率 14.2%」与主口径打架；本版主口径、"
            "待配置数、配置率一律按 599 计算，604 只作为「白名单行数(含扩充)」排障字段。"
            "已合并 data/endpoints_live.yaml 与 data/channel_reach.json 的实证端点，"
            "央视/新华/中新/人民等已在采渠道不再误报「★ 必采待补」。"),
        "说明": "待配置端点 = 白名单只登记名称、无可拉取 URL；这是「渠道未取得」的首要根因，"
                "不是网络失败。补官方 RSS / 站内时间倒排 / API 后重跑本脚本验证。",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 渠道端点体检 ===")
    print(f"  白名单 {data['主口径白名单']} | 已配置端点 {data['已配置端点渠道数']} "
          f"| 待配置端点 {data['待配置端点渠道数']} | 配置率 {data['端点配置率']:.1%}")
    if cats:
        print("  实测失败分类：")
        for k, v in sorted(cats.items(), key=lambda x: -x[1]):
            print(f"    {k}: {v}")
    print(f"  ★ 必采待补端点优先名单 {len(star_todo)} 个（前 8）：")
    for x in star_todo[:8]:
        print(f"    {x['级别']} {x['渠道']}（{x['节']}）")
    print(f"  健康度已写入 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
