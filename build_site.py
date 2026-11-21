#!/usr/bin/env python3
"""把流水线产出的五工作表 Excel 转成 GitHub Pages 静态站数据。

用法：
  python build_site.py                            # 自动取 output 下最新 *_窗口期.xlsx
  python build_site.py --xlsx output/2026_11_17_窗口期.xlsx --summary output/2026_11_17_窗口期.json
  python build_site.py --standalone 静态页面预览.html   # 额外产出单文件自包含预览

两种模式（由 config.output.overwrite 决定，也可 --overwrite 强制）：
  覆盖式（默认，推荐）：每次覆盖同一批固定文件，页面地址与下载链接永不变
    assets/news-data.js      只含最新一期 + window.NEWS_CONFIG
    data/latest.json         页面固定数据源
    downloads/latest.xlsx    页面固定下载入口
  归档式：追加多期，data/editions/<id>.json + downloads/<日期>.xlsx

index.html / assets/styles.css / assets/app.js 为静态源码，本脚本不覆盖。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import yaml
from openpyxl import load_workbook
from openpyxl.reader.excel import ExcelReader

# 部分台账的 custom.xml 带未声明命名空间，读取时跳过自定义属性（不影响单元格数据）
ExcelReader.read_custom = lambda self: None  # type: ignore[method-assign]

BASE = Path(__file__).resolve().parent


def _brand() -> tuple[str, str, str]:
    """单点：项目名 / 产品名 / 运行时刻全部从 config.yaml 读，不再硬编码。"""
    try:
        c = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8")) or {}
        p, s = (c.get("product", {}) or {}), (c.get("schedule", {}) or {})
        return (str(p.get("project") or "get-news"),
                str(p.get("name") or "资讯获取"),
                str(s.get("run_at") or "20:00"))
    except Exception:
        return "get-news", "资讯获取", "20:00"


PROJECT, PRODUCT, RUN_AT = _brand()
SITE_TITLE = f"{PROJECT} · {PRODUCT} · 每天 {RUN_AT}"
SITE_SUBTITLE = f"每天 {RUN_AT}（Asia/Shanghai）自动生成 · 窗口可配置"


def sync_index_brand(docs: Path) -> None:
    """覆盖式建站时把产品名同步进 index.html 的 title/h1（单点生效）。"""
    import re as _re
    p = docs / "index.html"
    if not p.exists():
        return
    s = p.read_text(encoding="utf-8")
    s2 = _re.sub(r"(<title>).*?(</title>)", lambda m: m.group(1) + PRODUCT + m.group(2),
                 s, count=1, flags=_re.S)
    s2 = _re.sub(r"(<h1[^>]*>).*?(</h1>)", lambda m: m.group(1) + PRODUCT + m.group(2),
                 s2, count=1, flags=_re.S)
    if s2 != s:
        p.write_text(s2, encoding="utf-8")


def kv_from_kouching(ws) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for row in ws.iter_rows(values_only=True):
        if len(row) < 2 or row[0] is None or row[1] is None:
            continue
        k, v = str(row[0]).strip(), row[1]
        if k in ("项目", "内容") or "附录" in k or len(k) > 40:
            continue
        if isinstance(v, str) and len(v) > 600:
            continue
        out.setdefault(k, v)
    return out


def read_rows(path: Path, sheet: str) -> List[List[Any]]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        rows = [list(r) for r in wb[sheet].iter_rows(values_only=True)]
    finally:
        wb.close()
    return rows


def read_time_trace(path: Path) -> List[Dict[str, Any]]:
    """解析口径表「逐条发布时间 / 时间依据 / 时效标记 / 日期线索留痕」。

    该表按采集序 1..N 与「窗口内全部数据」逐行对齐，是前端窗口切片的唯一时间依据。
    """
    rows = read_rows(path, "口径与稿外信息")
    idx = next((i for i, r in enumerate(rows)
                if r and r[0] is not None and "逐条发布时间" in str(r[0])), None)
    if idx is None:
        return []

    def s(v):
        return "" if v is None or str(v) == "None" else str(v)

    out: List[Dict[str, Any]] = []
    for r in rows[idx + 2:]:
        if not r or r[0] is None or not re.fullmatch(r"\d+", str(r[0])):
            break
        out.append({
            "seq": int(r[0]),
            "title": s(r[1] if len(r) > 1 else None),
            "pub": s(r[2] if len(r) > 2 else None) or "未取得",
            "time_basis": s(r[3] if len(r) > 3 else None),
            "time_mark": s(r[4] if len(r) > 4 else None),
            "date_clue": s(r[5] if len(r) > 5 else None),
            "t_basis": s(r[6] if len(r) > 6 else None),
        })
    return out


def parse_window(label: str, hours: float | None, interval: str = "lo-ro"):
    start = end = ""
    m = re.search(r"[(\[]([^,]+),\s*([^)\]]+)[)\]]", str(label))
    if m:
        start, end = m.group(1).strip(), m.group(2).strip()
    names = {"lo-ro": "左开右开", "lo-rc": "左开右闭",
             "lc-ro": "左闭右开", "lc-rc": "左闭右闭"}
    return {"label": str(label), "start": start, "end": end,
            "hours": float(hours) if hours is not None else "",
            "interval": interval, "interval_name": names.get(interval, "左开右开"),
            "left_open": interval.startswith("lo"),
            "right_open": interval.endswith("ro")}


def parse_summary(path: Path | None) -> Dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def build_edition(xlsx: Path, summary: Dict[str, Any], mode: str | None) -> Dict[str, Any]:
    main_rows = read_rows(xlsx, "处理后的数据")[1:]
    pool_rows = read_rows(xlsx, "窗口内全部数据")[1:]
    top_rows = read_rows(xlsx, "排名最靠前的10条")[1:]
    wb = load_workbook(xlsx, read_only=True, data_only=True)
    try:
        kv = kv_from_kouching(wb["口径与稿外信息"])
    finally:
        wb.close()

    def cell(r, i):
        return r[i] if i < len(r) else None

    def paragraphs_of(r):
        return [p.strip().lstrip("\u3000").strip()
                for p in str(cell(r, 8) or "").split("\n") if p.strip()]

    def sources_of(r):
        return [s.strip() for s in str(cell(r, 9) or "").split(" · ") if s.strip()]

    def abstract_state(title, paragraphs, sources):
        """服务端判定摘要状态：HTML 实体残留 / 标题复读一律算「未取得」。"""
        raw = "".join(paragraphs or [])
        if not raw or re.search(r"&nbsp;|&#160;|<[a-z]", raw):
            return "未取得"
        flat = lambda s: re.sub(r"\s+", "", s)
        core = flat(raw).replace(flat(str(title)), "")
        for s in sources:
            core = core.replace(flat(s), "")
        core = re.sub(r"[。！？；，、,.!?;:：·|｜\-—/\d]", "", core)
        return "有效" if len(core) >= 12 else "未取得"

    time_trace = read_time_trace(xlsx)
    default_time = {"pub": "未取得", "time_basis": "", "time_mark": "", "date_clue": "", "t_basis": ""}

    def time_of(seq, title):
        if seq and 0 < seq <= len(time_trace):
            t = time_trace[seq - 1]
            if not t["title"] or t["title"] == title:
                return t
        for t in time_trace:
            if t["title"] == title:
                return t
        return default_time

    # 候选池（窗口内全部数据）：与逐条时间留痕按采集序对齐，供前端任意窗口切片
    candidate: List[Dict[str, Any]] = []
    for i, r in enumerate(pool_rows, 1):
        if not r or cell(r, 7) is None:
            continue
        seq = int(cell(r, 0) or i)
        title, source = str(cell(r, 7)), str(cell(r, 9) or "")
        t = time_of(seq, title)
        candidate.append({
            "seq": seq,
            "title": title,
            "paragraphs": paragraphs_of(r),
            "sources": sources_of(r),
            "abstract_status": abstract_state(title, paragraphs_of(r), sources_of(r)),
            "group": str(cell(r, 5) or "—"),
            "tag": str(cell(r, 6) or "—"),
            "record_type": str(cell(r, 11) or "—"),
            "pub": t["pub"],
            "time_basis": t["time_basis"],
            "time_mark": t["time_mark"],
            "date_clue": t["date_clue"],
            "t_basis": t["t_basis"],
        })

    known_pubs = sorted(c["pub"] for c in candidate
                        if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", c["pub"]))
    coverage = {"min": known_pubs[0] if known_pubs else "",
                "max": known_pubs[-1] if known_pubs else ""}

    items: List[Dict[str, Any]] = []
    for r in main_rows:
        if not r or cell(r, 7) is None:
            continue
        paragraphs = paragraphs_of(r)
        source = str(cell(r, 9) or "")
        title = str(cell(r, 7))
        t = time_of(None, title)
        items.append({
            "order": int(cell(r, 0) or 0),
            "final_adj": cell(r, 1),
            "grade": str(cell(r, 2) or "D"),
            "final": cell(r, 3),
            "score_order": cell(r, 4),
            "group": str(cell(r, 5) or "—"),
            "tag": str(cell(r, 6) or "—"),
            "title": title,
            "paragraphs": paragraphs,
            "sources": sources_of(r),
            "abstract_status": abstract_state(title, paragraphs, sources_of(r)),
            "eid": str(cell(r, 10) or "—"),
            "record_type": str(cell(r, 11) or "—"),
            "top10": int(cell(r, 0) or 0) <= 10,
            "pub": t["pub"],
            "time_basis": t["time_basis"],
            "time_mark": t["time_mark"],
            "date_clue": t["date_clue"],
            "t_basis": t["t_basis"],
        })

    grades = {g: sum(1 for it in items if it["grade"] == g) for g in "SABCD"}
    group_counts: Dict[str, int] = {}
    tag_counts: Dict[str, int] = {}
    for it in items:
        group_counts[it["group"]] = group_counts.get(it["group"], 0) + 1
        tag_counts[it["tag"]] = tag_counts.get(it["tag"], 0) + 1
    top10_cats = len({str(cell(r, 5)) for r in top_rows if cell(r, 5) is not None})

    ch = summary.get("渠道触达", {})
    intl = summary.get("国际资讯", {})
    story = summary.get("议题簇", {})
    stats = {
        "raw": summary.get("原始采集条数", ""),
        "candidate": summary.get("候选池条数", len(pool_rows)),
        "main": summary.get("主表条数", len(items)),
        "top10": len(top_rows),
        "top10_cats": summary.get("前10覆盖大类数", top10_cats),
        "grades": summary.get("等级分布", grades),
        "group_counts": dict(sorted(group_counts.items(), key=lambda x: -x[1])),
        "tag_counts": dict(sorted(tag_counts.items(), key=lambda x: -x[1])),
        "h_channel": summary.get("H通道", str(kv.get("H 通道状态", "—"))),
        "channels": {
            "total": ch.get("白名单渠道总数"),
            "reached": ch.get("已触达渠道数"),
            "enumerable": ch.get("能按窗口枚举全量的渠道数"),
            "snapshot": ch.get("仅快照 / 相关性 top-N 的渠道数"),
            "star": ch.get("★必采"),
            "supplement": ch.get("☆补充"),
            "reference": ch.get("✗热度参考需回查"),
        },
        "excluded": summary.get("窗口外排除", ""),
        "estimated": summary.get("时点推定入池", ""),
        "intl": intl,
        "story": story,
        "mass_strictness": str(kv.get("大众贴近度档位", "strict")),
    }

    m = summary.get("采集窗口") or str(kv.get("采集窗口", ""))
    hours = summary.get("窗口跨度_小时") or kv.get("窗口跨度（小时）")
    interval = summary.get("interval") or str(kv.get("端点开闭", ""))
    if interval not in ("lo-rc", "lo-ro", "lc-rc", "lc-ro"):
        name2id = {"左开右闭": "lo-rc", "左开右开": "lo-ro",
                   "左闭右闭": "lc-rc", "左闭右开": "lc-ro"}
        if any(k in interval for k in name2id):
            interval = next(v for k, v in name2id.items() if k in interval)
        else:
            head = str(m).split("  ")[0].strip()
            interval = ("lo" if head.startswith("(") else "lc") + "-" + (
                "ro" if head.endswith(")") else "rc")
    win = parse_window(m, float(hours) if isinstance(hours, (int, float)) else None, interval)

    stem = xlsx.stem
    eid = stem
    date_m = re.match(r"(\d{4})_(\d{2})_(\d{2})", stem)
    date = f"{date_m.group(1)}-{date_m.group(2)}-{date_m.group(3)}" if date_m else stem[:10]
    used_mode = mode or str(kv.get("采集模式", "demo"))

    notes = [
        "采集窗口：" + win["label"] + "，" + win["interval_name"] +
        "；窗口期由 --start/--end/--window/--date/--interval 设置",
        "大众贴近度：" + stats["mass_strictness"] + " 档；先算三类强剔除，再看正面清单",
        str(kv.get("国际门禁", "国际动态仅保留三类重大突发")),
        str(kv.get("展示序声明", "编排层只改展示序，不改任何评分数值")),
        str(kv.get("等级声明", "等级按原始 Final 机械映射")),
        str(kv.get("候选池定义", "候选池为未经筛选的采集条目")),
        str(kv.get("尺度与诚实声明", "Final 是合成判断分，不是实测值")),
        str(kv.get("编排开关", "")),
        str(kv.get("渠道触达口径", "未触达渠道逐条申报原因")),
    ]
    notes = [n for n in notes if n and n != "None"]

    disclaimers = {
        "target": "本期由代码化全渠道补跑链路生成（8 大类归组 → 词条发现 → 独立原文回查 → 正文补采 → "
                  "多渠道合并 → 两段摘要 → 评分编排），数据来自内置标准语料按当前窗口重放，"
                  "属历史批次、非实时抓取；实时端点可达或配好检索 key 后把 source.mode 切为 live 即接实时源。",
        "demo": "本期为离线演示台账，用于验证窗口过滤与评级链路，不声称实时抓取结果；正式运行请把采集模式切为 live 并配置检索 key。",
        "inbox": "本期为外部注入清单，条目事实以注入来源为准。",
        "live": "本期由真实联网请求抓取：央视网 JSONP 秒级时间倒排、中新网/人民网 RSS、站内时间倒排页与"
                "独立原文正文补采；逐源触达与失败原因见 Excel 附录 21。受限网络下不可达端点逐源申报、"
                "不编造条目；无平台后台数据时 H 通道按规范关闭。",
        "hybrid": "本期为联网采集 + 离线台账兜底，含非实时演示数据，已在窗口徽章标注。",
    }
    disclaimer = disclaimers.get(used_mode, "本期数据来源见口径表「采集模式」。") + " " + str(kv.get("边界声明", ""))

    return {
        "id": eid,
        "date": date,
        "name": xlsx.name,
        "mode": used_mode,
        "generated_at": str(kv.get("生成时间", "")),
        "window": win,
        "stats": stats,
        "items": items,
        "candidate": candidate,
        "coverage": coverage,
        "excel": "downloads/" + xlsx.name,
        "quality": summary.get("采集质量", {}),
        "live_sources": summary.get("实时源触达", {}),
        "notes": notes,
        "disclaimer": disclaimer.strip(),
    }


def js_dumps(obj: Any) -> str:
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def repo_info(cfg: dict) -> Dict[str, str]:
    """仓库坐标：config.site.github_repo 优先，否则从 git remote origin 自动探测。

    写进 NEWS_CONFIG / latest.json，让页面「重跑并刷新」零配置：
    静态 Pages 不需要用户填 owner / repo / Token，直接打开 Actions 工作流页即可。
    """
    site = cfg.get("site", {}) or {}
    slug = str(site.get("github_repo") or "").strip().strip("/")
    if not slug:
        try:
            import subprocess
            out = subprocess.run(["git", "remote", "get-url", "origin"],
                                 capture_output=True, text=True, timeout=3).stdout.strip()
            m = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?$", out)
            if m:
                slug = m.group(1) + "/" + m.group(2)
        except Exception:
            pass
    if not slug or "/" not in slug:
        return {"github_repo": "", "actions_url": "", "workflow_url": "", "branch": "main"}
    owner, repo = slug.split("/", 1)
    base = "https://github.com/" + owner + "/" + repo
    return {
        "github_repo": slug,
        "branch": str(site.get("branch") or "main"),
        "actions_url": base + "/actions",
        "workflow_url": base + "/actions/workflows/daily-news.yml",
    }


def write_site(docs: Path, editions: List[Dict[str, Any]], keep: int, xlsx: Path,
               default_id: str) -> None:
    (docs / "assets").mkdir(parents=True, exist_ok=True)
    (docs / "data" / "editions").mkdir(parents=True, exist_ok=True)
    (docs / "downloads").mkdir(parents=True, exist_ok=True)
    (docs / ".nojekyll").write_text("", encoding="utf-8")

    for ed in editions:
        (docs / "data" / "editions" / f"{ed['id']}.json").write_text(
            json.dumps(ed, ensure_ascii=False, indent=1), encoding="utf-8")

    bundle = {
        "site": {
            "title": SITE_TITLE,
            "subtitle": SITE_SUBTITLE,
            "tz": "Asia/Shanghai",
            "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
            "default_edition": default_id,
        },
        "editions": editions[:keep],
    }
    (docs / "assets" / "news-data.js").write_text(
        "/* 由 build_site.py 生成，勿手改 */\nwindow.NEWS_DATA = " + js_dumps(bundle) + ";\n",
        encoding="utf-8")
    shutil.copyfile(xlsx, docs / "downloads" / xlsx.name)


def write_fixed_site(docs: Path, ed: Dict[str, Any], xlsx: Path, cfg: dict) -> Dict[str, Any]:
    """覆盖式：每次运行覆盖同一批固定文件，页面地址与下载链接永不变。

    - docs/data/latest.json        页面固定数据源
    - docs/downloads/latest.xlsx  页面固定下载入口（同时保留按日期命名副本）
    - docs/assets/news-data.js    只含最新一期 + NEWS_CONFIG（页面默认读它）
    """
    o = cfg.get("output", {}) or {}
    latest_xlsx = o.get("latest_name", "latest.xlsx")
    latest_json = o.get("site_latest_json", "latest.json")
    (docs / "assets").mkdir(parents=True, exist_ok=True)
    (docs / "data").mkdir(parents=True, exist_ok=True)
    (docs / "downloads").mkdir(parents=True, exist_ok=True)
    (docs / ".nojekyll").write_text("", encoding="utf-8")

    fixed = dict(ed)
    fixed["excel"] = "downloads/" + latest_xlsx
    ri = repo_info(cfg)
    fixed["repo"] = ri                       # 页面从固定 json 读仓库坐标，零配置重跑
    (docs / "data" / latest_json).write_text(
        json.dumps(fixed, ensure_ascii=False, indent=1), encoding="utf-8")
    _dst = docs / "downloads" / latest_xlsx
    if xlsx.resolve() != _dst.resolve():
        shutil.copyfile(xlsx, _dst)
    if o.get("keep_history", False):
        shutil.copyfile(xlsx, docs / "downloads" / xlsx.name)
    else:
        # 覆盖式：清掉归档模式的残留，保证仓库里只有固定地址那一份
        ed_dir = docs / "data" / "editions"
        if ed_dir.exists():
            shutil.rmtree(ed_dir)
        for p in (docs / "downloads").glob("*.xlsx"):
            if p.name != latest_xlsx:
                p.unlink()

    sch = cfg.get("schedule", {}) or {}
    w = cfg.get("window", {}) or {}
    config = {
        "run_at": sch.get("run_at", "20:00"),
        "timezone": sch.get("timezone", "Asia/Shanghai"),
        "cron_utc": sch.get("cron_utc"),
        "schedule_enabled": bool(sch.get("enabled", True)),
        "window_mode": w.get("mode", "relative"),
        "start_offset_hours": w.get("start_offset_hours", -12),
        "end_offset_hours": w.get("end_offset_hours", 0),
        "interval": w.get("interval", "lo-ro"),
        "source_mode": cfg.get("source", {}).get("mode", "demo"),
        "latest_xlsx": latest_xlsx,
        "latest_json": latest_json,
        "quality": (cfg.get("quality", {}) or {}).get("warn_only", True),
        "github_repo": ri["github_repo"],
        "actions_url": ri["actions_url"],
        "workflow_url": ri["workflow_url"],
        "branch": ri["branch"],
        "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
    }
    bundle = {
        "site": {
            "title": SITE_TITLE, "subtitle": SITE_SUBTITLE, "tz": "Asia/Shanghai",
            "updated_at": config["updated_at"], "default_edition": ed["id"],
            "fixed": True,
        },
        "editions": [fixed],
    }
    (docs / "assets" / "news-data.js").write_text(
        "/* 由 build_site.py 覆盖生成，勿手改 */\n"
        "window.NEWS_DATA = " + js_dumps(bundle) + ";\n"
        "window.NEWS_CONFIG = " + js_dumps(config) + ";\n",
        encoding="utf-8")
    return config


def write_standalone(docs: Path, editions: List[Dict[str, Any]], keep: int, out: Path,
                     default_id: str, cfg: dict | None = None,
                     overwrite_fixed: bool = False) -> None:
    html = (docs / "index.html").read_text(encoding="utf-8")
    css = (docs / "assets" / "styles.css").read_text(encoding="utf-8")
    app = (docs / "assets" / "app.js").read_text(encoding="utf-8")
    standalone = [dict(ed) for ed in editions[:keep]]
    import os
    prefix = os.path.relpath(docs.resolve(), out.parent.resolve()).replace(os.sep, "/") + "/"
    fixed_name = ((cfg or {}).get("output", {}) or {}).get("latest_name")
    for ed in standalone:
        if ed.get("excel"):
            fn = fixed_name if (overwrite_fixed and fixed_name) else ed["excel"].split("/")[-1]
            ed["excel"] = prefix + "downloads/" + fn
    bundle = {
        "site": {
            "title": SITE_TITLE, "subtitle": SITE_SUBTITLE, "tz": "Asia/Shanghai",
            "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
            "default_edition": default_id,
        },
        "editions": standalone,
    }
    cfg = cfg or {}
    sch, w, o = (cfg.get("schedule", {}) or {}, cfg.get("window", {}) or {},
                 cfg.get("output", {}) or {})
    config = {
        "run_at": sch.get("run_at", "20:00"), "timezone": sch.get("timezone", "Asia/Shanghai"),
        "cron_utc": sch.get("cron_utc"), "schedule_enabled": bool(sch.get("enabled", True)),
        "window_mode": w.get("mode", "relative"),
        "start_offset_hours": w.get("start_offset_hours", -12),
        "end_offset_hours": w.get("end_offset_hours", 0),
        "interval": w.get("interval", "lo-ro"),
        "source_mode": cfg.get("source", {}).get("mode", "demo"),
        "latest_xlsx": o.get("latest_name", "latest.xlsx"),
        "latest_json": o.get("site_latest_json", "latest.json"),
        "github_repo": repo_info(cfg)["github_repo"],
        "actions_url": repo_info(cfg)["actions_url"],
        "workflow_url": repo_info(cfg)["workflow_url"],
        "branch": repo_info(cfg)["branch"],
        "updated_at": bundle["site"]["updated_at"],
    }
    data_js = ("window.NEWS_DATA = " + js_dumps(bundle) + ";\n"
               "window.NEWS_CONFIG = " + js_dumps(config) + ";")
    html = html.replace('<link rel="stylesheet" href="assets/styles.css">', "<style>\n" + css + "\n</style>")
    html = html.replace('<script src="assets/news-data.js"></script>', "<script>\n" + data_js + "\n</script>")
    html = html.replace('<script src="assets/app.js"></script>', "<script>\n" + app + "\n</script>")
    out.write_text(html, encoding="utf-8")


def load_archives(docs: Path) -> List[Dict[str, Any]]:
    d = docs / "data" / "editions"
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Excel → GitHub Pages 静态站数据")
    p.add_argument("--xlsx", help="流水线产物 xlsx；默认自动取 output 下最新 *_窗口期.xlsx")
    p.add_argument("--summary", help="run.py --json-summary 产出的同名 json；默认自动匹配")
    p.add_argument("--docs", default="docs", help="静态站目录，默认 docs")
    p.add_argument("--mode", choices=["target", "demo", "inbox", "live", "hybrid"],
                   help="覆盖采集模式标记")
    p.add_argument("--keep", type=int, default=60, help="归档模式保留最近 N 期，默认 60")
    p.add_argument("--standalone", help="额外产出单文件自包含预览 HTML 路径")
    p.add_argument("--overwrite", action="store_true",
                   help="覆盖式：只写固定 latest.json / latest.xlsx / news-data.js，页面地址不变")
    p.add_argument("--config", default="config.yaml", help="读取单点配置用于 NEWS_CONFIG")
    args = p.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = BASE / cfg_path
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {} if cfg_path.exists() else {}
    overwrite = args.overwrite or bool((cfg.get("output", {}) or {}).get("overwrite", False))

    if args.xlsx:
        xlsx = Path(args.xlsx)
    else:
        candidates = sorted((BASE / "output").glob("*_窗口期.xlsx"),
                            key=lambda x: x.stat().st_mtime, reverse=True)
        if not candidates:
            raise SystemExit("未找到 output/*_窗口期.xlsx，请先运行 python run.py")
        xlsx = candidates[0]
    if not xlsx.is_absolute():
        cand = Path.cwd() / xlsx
        xlsx = cand.resolve() if cand.exists() else (BASE / xlsx).resolve()
    if not xlsx.exists():
        raise SystemExit(f"xlsx 不存在：{xlsx}")

    summary_path = Path(args.summary) if args.summary else xlsx.with_suffix(".json")
    if not summary_path.is_absolute():
        cand = Path.cwd() / summary_path
        summary_path = cand if cand.exists() else BASE / summary_path
    summary = parse_summary(summary_path)
    mode = args.mode or summary.get("_mode")
    if mode:
        cfg.setdefault("source", {})["mode"] = mode  # NEWS_CONFIG 反映本期真实模式

    docs = Path(args.docs)
    if not docs.is_absolute():
        docs = BASE / docs

    ed = build_edition(xlsx, summary, mode)
    if overwrite:
        sync_index_brand(docs)
        config = write_fixed_site(docs, ed, xlsx, cfg)
        editions = [ed]
        print(f"[站点] {docs}（覆盖式 · 固定地址）")
        print(f"[固定] docs/data/{config['latest_json']}  docs/downloads/{config['latest_xlsx']}")
        print(f"[配置] 每天 {config['run_at']} {config['timezone']} | cron {config['cron_utc']} | "
              f"窗口 {config['start_offset_hours']}h→{config['end_offset_hours']}h {config['interval']}")
    else:
        archives = {x["id"]: x for x in load_archives(docs)}
        archives[ed["id"]] = ed
        editions = sorted(archives.values(), key=lambda x: (x["date"], x["id"]), reverse=True)
        write_site(docs, editions, args.keep, xlsx, ed["id"])
        for p in sorted((BASE / "output").glob("*.xlsx")):
            shutil.copyfile(p, docs / "downloads" / p.name)
        config = None
        print(f"[站点] {docs}（归档式）")
        print(f"[归档] {len(editions)} 期，bundle 载入 {min(len(editions), args.keep)} 期")
        print(f"[下载] docs/downloads/{xlsx.name}")
    print(f"[本期] {ed['id']} 窗口 {ed['window']['label']} 候选 {ed['stats']['candidate']} / 主表 {ed['stats']['main']}")

    if args.standalone:
        out = Path(args.standalone)
        if not out.is_absolute():
            out = BASE / out
        out.parent.mkdir(parents=True, exist_ok=True)
        write_standalone(docs, editions, args.keep, out, ed["id"], cfg, overwrite)
        print(f"[预览] {out}（单文件，可直接双击打开）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
