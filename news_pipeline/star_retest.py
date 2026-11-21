"""资讯获取 · ★ 必采缺口定向重测（21 条）。

背景（2026-11-21 核查）：
  data/v3_star_pending.yaml 原先只有「状态 / 启用动作」文字，0 条真实 URL；
  reach.py 仅把 21 个渠道名读进元数据；workflow 里该文件出现 0 次；
  提交 step 只 `git add -A docs/`，即使重测了成果也随 runner 销毁。
  本模块把「Actions 每次构建自动重测」真正落地。

规则：
  - 逐条真发请求，仅 HTTP 200 且解析出 >=1 条【带时间字段】条目才算通过；
  - 首页 / 导航页 / 无时间字段一律判失败并逐条写明原因，不预设成功；
  - 通过的端点写回 data/endpoints_live.yaml（按 url 去重），并更新本清单状态；
  - site: RSS 是「发现端点」不是「成稿端点」，命中后仍须逐条回查独立原文。

用法：
  python -m news_pipeline.star_retest                 # 在线重测（CI 使用）
  python -m news_pipeline.star_retest --offline       # 纯结构自检，不发请求、不写回
  python -m news_pipeline.star_retest --timeout 8 --workers 12
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

BASE = Path(__file__).resolve().parents[1]
PENDING = BASE / "data" / "v3_star_pending.yaml"
ENDPOINTS = BASE / "data" / "endpoints_live.yaml"
RESULT = BASE / "data" / "star_retest_result.json"

TIME_RE = re.compile(
    r"(?:20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"
    r"|\b1[5-9]\d{8,10}\b)"
)


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


def _has_time(obj) -> int:
    """递归统计 JSON/JSONP 结构里像时间的字符串数量。"""
    n = 0
    if isinstance(obj, str):
        return 1 if TIME_RE.search(obj) else 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if re.search(r"date|time|pub|focus|create|update|发布|时间", str(k), re.I) and v:
                n += 1
            n += _has_time(v)
    elif isinstance(obj, list):
        for v in obj[:80]:
            n += _has_time(v)
    return n


def _probe_one(url: str, kind: str, timeout: float) -> dict:
    """真实请求 + 真实解析。返回逐候选留痕。"""
    row = {"url": url, "type": kind, "status": 0, "items": 0,
           "time_samples": 0, "ok": False, "reason": ""}
    try:
        import requests
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; NewsGetter/1.0)",
                                  "Accept": "*/*"})
        row["status"] = r.status_code
        if r.status_code != 200:
            row["reason"] = f"HTTP {r.status_code}"
            return row
        text = r.text or ""
        if len(text) < 120:
            row["reason"] = "响应过短，疑似空壳/拦截页"
            return row
    except Exception as e:
        row["reason"] = f"{type(e).__name__}: {str(e)[:120]}"
        return row

    if kind == "html":
        # 首页仅作连通性验证：无可枚举时间条目，按规格判失败
        row["reason"] = "首页/连通性候选，无可枚举带时间条目（按规格不登记为端点）"
        return row

    if kind == "rss":
        # feedparser 优先；缺库时用标准库 ElementTree 回退，保证任何环境都能判定。
        # 时间样本按「每个 item 自身带时间字段」计数，不能用全文 re.search 给每条都 +1。
        ents = None
        try:
            import feedparser
            ents = feedparser.parse(text).entries or []
            row["items"] = len(ents)
            row["time_samples"] = sum(
                1 for e in ents
                if getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
                or getattr(e, "published", "") or getattr(e, "updated", "")
            )
        except ImportError:
            ents = None
        except Exception as e:
            row["reason"] = f"RSS 解析失败 {type(e).__name__}: {str(e)[:80]}"
            return row
        if ents is None:
            try:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(text)
                items = root.findall(".//item") or [
                    e for e in root.iter() if e.tag.endswith("}entry") or e.tag == "entry"
                ]
                row["items"] = len(items)
                row["time_samples"] = sum(
                    1 for it in items
                    if any(
                        (c.tag.split("}")[-1] in ("pubDate", "date", "published", "updated"))
                        and (c.text or "").strip()
                        for c in it.iter()
                    )
                )
                row["reason"] = "feedparser 缺失，已用标准库 ElementTree 回退解析"
            except Exception as e:
                row["reason"] = f"RSS/Atom 解析失败 {type(e).__name__}: {str(e)[:80]}"
                return row
        if row["items"] == 0:
            row["reason"] = row["reason"] or "RSS/Atom 0 条目"
            return row
    else:  # jsonp / json
        s = text.strip()
        s = re.sub(r"^\s*[\w.$]+\s*\(\s*", "", s)
        s = re.sub(r"\s*\)\s*;?\s*$", "", s)
        try:
            data = json.loads(s)
        except Exception:
            row["reason"] = "JSON/JSONP 解析失败"
            return row
        if isinstance(data, dict):
            for key in ("data", "items", "list", "results", "rollData", "docs"):
                v = data.get(key)
                if isinstance(v, list) and v:
                    row["items"] = len(v)
                    break
            else:
                row["items"] = 1 if data else 0
        elif isinstance(data, list):
            row["items"] = len(data)
        row["time_samples"] = min(_has_time(data), max(row["items"], 1))

    row["ok"] = row["items"] >= 1 and row["time_samples"] >= 1
    if not row["ok"]:
        row["reason"] = (f"条目 {row['items']} / 带时间 {row['time_samples']}："
                         "未达到「>=1 条带时间字段」的登记门槛")
    return row


def _load() -> dict:
    if not PENDING.exists():
        raise SystemExit(f"FAIL: 缺少 {PENDING}")
    d = yaml.safe_load(PENDING.read_text(encoding="utf-8")) or {}
    ch = d.get("渠道") or []
    if len(ch) != 21:
        raise SystemExit(f"FAIL: ★ 缺口应为 21 条，实际 {len(ch)} 条")
    for i, x in enumerate(ch, 1):
        eps = x.get("候选端点") or []
        if not eps or not all(str(e.get("url", "")).startswith("http") for e in eps):
            raise SystemExit(f"FAIL: 第 {i} 条「{x.get('渠道')}」缺真实候选端点 URL")
        if not x.get("时间字段依据") and all("时间字段依据" not in e for e in eps):
            raise SystemExit(f"FAIL: 第 {i} 条缺时间字段依据")
    return d


def offline_check(d: dict) -> dict:
    ch = d["渠道"]
    eps = [e for x in ch for e in x["候选端点"]]
    kinds = {}
    for e in eps:
        kinds[e["type"]] = kinds.get(e["type"], 0) + 1
    checks = [
        ("★ 缺口条数 = 21", len(ch) == 21),
        ("21 条全部带真实候选 URL", all(any(str(e["url"]).startswith("http") for e in x["候选端点"]) for x in ch)),
        ("候选端点总数 >= 42", len(eps) >= 42),
        ("每条都有时间字段依据", all(any(e.get("时间字段依据") for e in x["候选端点"]) for x in ch)),
        ("每条都有实测状态字段", all("实测状态" in x for x in ch)),
        ("重测策略声明了写回目标", bool((d.get("重测策略") or {}).get("写回"))),
        ("重测策略声明了 CI 入口", "star_retest" in str((d.get("重测策略") or {}).get("入口", ""))),
        ("端点清单文件存在", ENDPOINTS.exists()),
    ]
    bad = [n for n, ok in checks if not ok]
    return {"mode": "offline", "checks": [{"检查": n, "结果": "PASS" if ok else "FAIL"} for n, ok in checks],
            "渠道数": len(ch), "候选端点数": len(eps), "类型分布": kinds,
            "结论": "PASS" if not bad else "FAIL", "失败项": bad}


def _write_back(d: dict, results: list) -> dict:
    """把通过的端点写回 endpoints_live.yaml，并更新清单状态。"""
    ep = yaml.safe_load(ENDPOINTS.read_text(encoding="utf-8")) or {}
    lst = ep.get("endpoints") or []
    # 关键隔离：site: RSS 是「发现端点」不是「采集端点」。
    # 若混进 endpoints 段，会被体检器/下游当成可直接抽新闻的时间流，
    # 把检索结果当新闻灌进候选池（正是用户投诉的噪声）。
    disc = ep.get("star_discovery") or []
    dseen = {str(e.get("url", "")) for e in disc}
    seen = {str(e.get("url", "")) for e in lst}
    added = 0
    for i, (x, res) in enumerate(zip(d["渠道"], results), 1):
        good = next((r for r in res["候选留痕"] if r["ok"]), None)
        if good:
            already = good["url"] in seen
            x["实测状态"] = "已实证发现端点" if not already else "归并到已实证端点"
            x["实证端点"] = good["url"]
            x["端点性质"] = "发现端点（须逐条回查独立原文，不直接进候选池）"
            x["实测条目数"] = good["items"]
            x["实测时间"] = _now()
            if not already and good["url"] not in dseen:
                disc.append({
                    "id": f"STAR-{i:02d}",
                    "channel": x["渠道"],
                    "type": good["type"],
                    "url": good["url"],
                    "kind": "discovery",
                    "section": f"★ 必采缺口定向重测 / {x['渠道']}",
                    "level": "★",
                    "requires": "none",
                    "source": "news_pipeline/star_retest.py 真实请求 + 真实解析",
                    "verified_items": good["items"],
                    "time_samples": good["time_samples"],
                    "retested_at": _now(),
                    "note": "发现端点：词条/条目须走 lookup_article 回查独立原文、补正文、"
                            "校验发布时间落在窗口内才成稿；回查不到进附录 15，不吃 snippet",
                })
                dseen.add(good["url"])
                added += 1
        else:
            x["实测状态"] = "未通过（逐候选原因见 star_retest_result.json）"
            x["实证端点"] = None
            x["实测条目数"] = 0
            x["实测时间"] = _now()

    ep["endpoints"] = lst            # 采集端点保持纯净，不因重测而增加
    ep["star_discovery"] = disc      # 发现端点单独落段
    ep["star_retest_at"] = _now()
    ENDPOINTS.write_text(yaml.safe_dump(ep, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8")
    d["last_retest"] = _now()
    PENDING.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8")

    summary = {
        "generated_at": _now(),
        "渠道数": len(results),
        "通过渠道数": sum(1 for x in d["渠道"] if x["实测状态"].startswith("已实证") or x["实测状态"] == "归并到已实证端点"),
        "新增发现端点数": added,
        "发现端点总数": len(disc),
        "采集端点总数": len(lst),
        "结论": ("21 条逐条真实重测完成；通过者写入 star_discovery 段（发现端点，不直接进候选池），"
                 "未通过者逐候选写明 HTTP 状态/解析结果，不预设成功；采集端点段保持原样"),
        "诚实边界": ("site: RSS 为发现端点非成稿端点；仍须逐条回查独立原文、补正文、校验发布时间落在窗口内；"
                     "本模块负责『拿到可拉取通路』，词条→正文的回查由 search_api/lookup_article 链路承担；"
                     "沙盒与 Actions runner 网络不同，结果以 CI 实测为准"),
        "逐条": results,
    }
    RESULT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="纯结构自检，不发请求、不写回")
    ap.add_argument("--timeout", type=float, default=6.0)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--limit", type=int, default=0, help="仅测前 N 条（调试）")
    a = ap.parse_args()

    d = _load()
    if a.offline:
        r = offline_check(d)
        for c in r["checks"]:
            print(f"  {c['结果']:4s} {c['检查']}")
        print(f"[自检] {r['结论']} | 渠道 {r['渠道数']} | 候选端点 {r['候选端点数']} | 类型 {r['类型分布']}")
        if r["结论"] != "PASS":
            print("FAIL:", r["失败项"]); return 1
        print("[自检] 未发请求、未写回，正式端点清单保持不变")
        return 0

    chans = d["渠道"][:a.limit] if a.limit else d["渠道"]
    jobs = [(i, x, e) for i, x in enumerate(chans, 1) for e in x["候选端点"]]
    print(f"[重测] {len(chans)} 个 ★ 渠道 / {len(jobs)} 个候选端点 / timeout={a.timeout}s / workers={a.workers}")

    def work(job):
        i, x, e = job
        return i, x, e, _probe_one(e["url"], e["type"], a.timeout)

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        done = list(ex.map(work, jobs))

    grouped: dict[int, list] = {}
    names: dict[int, str] = {}
    for i, x, e, r in done:
        grouped.setdefault(i, []).append(r)
        names[i] = x["渠道"]

    results = []
    for i in range(1, len(chans) + 1):
        rs = grouped.get(i, [])
        good = [r for r in rs if r["ok"]]
        results.append({
            "序号": i, "渠道": names.get(i, "?"),
            "判定": "通过" if good else "未通过",
            "通过端点": good[0]["url"] if good else None,
            "条目数": good[0]["items"] if good else 0,
            "候选留痕": rs,
        })
        tag = "✅" if good else "❌"
        print(f"  {tag} [{i:2d}/21] {names.get(i,'?')[:18]:20s} "
              f"通过 {len(good)}/{len(rs)}" + (f" | 条目 {good[0]['items']}" if good else ""))

    s = _write_back(d, results)
    print(f"\n[结论] 通过 {s['通过渠道数']}/{s['渠道数']} 个渠道 | "
          f"新增发现端点 {s['新增发现端点数']} 个（star_discovery 段）| "
          f"采集端点保持 {s['采集端点总数']} 个不变")
    print(f"[写回] {ENDPOINTS.name} / {PENDING.name} / {RESULT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
