"""渠道质量账本与低质渠道淘汰（用户 2026-11-20：「把低质量和过滤数据多的渠道直接去掉」）。

不删白名单：需求②要求 31 类 599 渠道逐条申报触达与启用条件。本模块做的是
「采集端跳采 + 逐条申报淘汰原因 + 可自动解禁」，而不是把渠道从清单里抹掉。

判定完全基于本批真实单元格，不靠黑名单硬编码：
  原始条数 / 窗口内 / 候选池 / 主表 / 摘要有效率 / 平均 Final / 剔除率
命中阈值 → 写入 data/channel_blocklist.yaml，下一轮采集跳过，
并在 Excel「工程留痕 I」逐条写明「淘汰原因 + 解禁条件」。

解禁条件是自动的：把该渠道的正文 URL 补进 data/endpoints_live.yaml 或
让它连续 N 批摘要有效率回到阈值以上，blocklist 会在下一批重算时自动移除。
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

BASE = Path(__file__).resolve().parents[1]
QUALITY_JSON = BASE / "data" / "channel_quality.json"
BLOCKLIST_YAML = BASE / "data" / "channel_blocklist.yaml"


def _key(it) -> str:
    return str(getattr(it, "channel_id", "") or getattr(it, "channel", "")
               or getattr(it, "source", "") or "未知渠道").strip()


def compute(raw: List[Any], candidate: List[Any], ordered: List[Any],
            cfg: dict) -> Dict[str, Any]:
    q = (cfg.get("channel_quality", {}) or {})
    min_raw = int(q.get("min_raw_to_judge", 12))
    min_abs = float(q.get("min_abstract_ratio", 0.15))
    min_main = float(q.get("min_main_ratio", 0.015))
    max_reject = float(q.get("max_reject_ratio", 0.95))
    top_n = int(q.get("top_block", 40))

    cand_seq = {getattr(x, "seq", None) for x in candidate}
    main_seq = {getattr(x, "seq", None) for x in ordered}
    agg: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"原始": 0, "窗口内": 0, "候选": 0, "主表": 0,
                 "有效摘要": 0, "无摘要": 0, "final_sum": 0.0,
                 "B以上": 0, "来源名": ""})
    for it in raw:
        k = _key(it)
        a = agg[k]
        a["原始"] += 1
        if getattr(it, "source", ""):
            a["来源名"] = str(it.source)
        seq = getattr(it, "seq", None)
        if seq in cand_seq:
            a["候选"] += 1
            st = str(getattr(it, "abstract_status", "") or "")
            if st in ("有效", "正文补采"):
                a["有效摘要"] += 1
            else:
                a["无摘要"] += 1
        if seq in main_seq:
            a["主表"] += 1
            a["final_sum"] += float(getattr(it, "Final_adj", 0) or 0)
            if str(getattr(it, "grade", "")) in ("S", "A", "B"):
                a["B以上"] += 1

    rows: List[Dict[str, Any]] = []
    blocked: List[Dict[str, str]] = []
    for k, a in agg.items():
        n = int(a["原始"])
        cand = int(a["候选"])
        main = int(a["主表"])
        abs_ok = a["有效摘要"] / max(1, cand)
        main_ratio = main / max(1, n)
        reject = 1 - cand / max(1, n)
        avg_final = a["final_sum"] / max(1, main)
        reasons = []
        if n >= min_raw and abs_ok < min_abs:
            reasons.append(f"摘要有效率 {abs_ok:.0%} < {min_abs:.0%}")
        if n >= min_raw and main_ratio < min_main and reject > max_reject:
            reasons.append(f"主表率 {main_ratio:.1%}、过滤率 {reject:.0%}")
        if n >= min_raw and cand >= 8 and main == 0:
            reasons.append("进候选池 ≥8 条但主表 0 条")
        r = {
            "渠道": k, "来源": a["来源名"] or k, "原始": n, "候选": cand,
            "主表": main, "摘要有效率": round(abs_ok, 4),
            "主表率": round(main_ratio, 4), "过滤率": round(reject, 4),
            "平均Final": round(avg_final, 2), "B以上": int(a["B以上"]),
            "淘汰": bool(reasons), "淘汰原因": "；".join(reasons),
        }
        rows.append(r)
        if reasons:
            blocked.append({"channel_id": k, "source": r["来源"],
                            "reason": r["淘汰原因"],
                            "unblock": "补该站独立原文端点 / 配 Jina key / 连续 2 批摘要有效率 ≥ %.0f%% 后自动解禁" % (min_abs * 100)})
    rows.sort(key=lambda x: (-int(x["淘汰"]), -x["原始"], x["摘要有效率"]))
    blocked = blocked[:top_n]

    out = {
        "口径": ("按本批真实单元格逐渠道复算；分母含窗口外与字段不合格条目。"
                 "淘汰只在采集端跳采，白名单 599 渠道仍逐条申报，满足需求②。"),
        "阈值": {"min_raw_to_judge": min_raw, "min_abstract_ratio": min_abs,
                 "min_main_ratio": min_main, "max_reject_ratio": max_reject,
                 "top_block": top_n},
        "汇总": {"渠道数": len(rows), "淘汰数": len(blocked),
                 "淘汰渠道原始条目": sum(next((x["原始"] for x in rows
                                          if x["渠道"] == b["channel_id"]), 0)
                                    for b in blocked)},
        "逐渠道": rows,
        "淘汰": blocked,
    }
    QUALITY_JSON.parent.mkdir(parents=True, exist_ok=True)
    QUALITY_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    if q.get("write_blocklist", True):
        import yaml
        BLOCKLIST_YAML.write_text(
            "# 由 channel_quality.py 按本批真实数据自动生成，勿手改\n"
            "# 采集端跳采；白名单仍逐条申报淘汰原因与解禁条件\n"
            + yaml.safe_dump({"blocked": blocked}, allow_unicode=True, sort_keys=False),
            encoding="utf-8")
    return out


def load_blocked(cfg: dict) -> Dict[str, Dict[str, str]]:
    if not (cfg.get("channel_quality", {}) or {}).get("enabled", True):
        return {}
    if not BLOCKLIST_YAML.exists():
        return {}
    try:
        import yaml
        d = yaml.safe_load(BLOCKLIST_YAML.read_text(encoding="utf-8")) or {}
        out = {}
        for x in (d.get("blocked") or []):
            out[str(x["channel_id"])] = x
            if x.get("source"):
                out["SRC::" + str(x["source"])] = x      # 同时按来源名命中
        return out
    except Exception:
        return {}


def apply_to_raw(raw: List[Any], cfg: dict) -> Tuple[List[Any], List[List[str]], Dict[str, str]]:
    """采集端跳采：返回 (保留条目, 逐条淘汰留痕, 命中渠道表)。"""
    blocked = load_blocked(cfg)
    if not blocked:
        return raw, [], {}
    kept, traces, hit = [], [], {}
    for it in raw:
        k = _key(it)
        src = "SRC::" + str(getattr(it, "source", "") or "")
        b = blocked.get(k) or blocked.get(src)
        if b:
            traces.append([k, str(getattr(it, "title", ""))[:80],
                           b.get("reason", ""), b.get("unblock", "")])
            hit[k] = b.get("reason", "")
            continue
        kept.append(it)
    return kept, traces, hit
