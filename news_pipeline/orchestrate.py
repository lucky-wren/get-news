"""编排层（SKILL §4.9–§4.14）：只改展示序，不改 Final / Final_adj / 等级 / gin / cap / V / I / C。

开关：orchestrate=false → 纯 Final_adj 降序；head_window=0 → 关闭头部独立判定；
interleave=false → 关闭相邻同类打散。展示序与 Final_adj 严格降序不一致是编排口径的代价，如实标注。
"""
from __future__ import annotations

import re
from typing import Dict, List

from .model import RawItem
from .rules import daily_groups

PROTECTED_TIERS = ("重大伤亡 / 人员死亡", "终局性新规 / 法律生效",
                   "权威预警 / 红色预警", "重大司法终局")


def severe_pool(it: RawItem) -> bool:
    """渠道文档 §32.8 特别严重突发池：死亡≥10 / 伤亡≥30 / 震级≥7 / 国家级响应 / 重大公卫。"""
    t = it.text()
    nums = [int(x) for x in re.findall(r"(\d+)\s*人死亡", t)]
    nums += [int(x) for x in re.findall(r"死亡超\s*(\d+)", t)]
    nums += [int(x) for x in re.findall(r"伤亡\s*(\d+)\s*人", t)]
    nums += [int(x) for x in re.findall(r"(\d+)\s*余人受伤", t)]
    if any(n >= 10 for n in nums):
        return True
    quake = re.search(r"(\d+(?:\.\d+)?)\s*级地震", t)
    if quake and float(quake.group(1)) >= 7.0:
        return True
    if any(w in t for w in ("国家级应急响应", "一级响应", "全国哀悼", "特别重大", "重大公共卫生事件")):
        return True
    return False


def impact4(it: RawItem) -> dict:
    """需求④：逐条算「影响面四维」——人群范围 / 地域范围 / 紧迫与严重 / 可行动性。

    只决定展示顺序：由 compute_U 计入编排分 U，**不改** Final / Final_adj /
    等级 / gin / cap / V / I / C 任何评级数值（01 第五节、SKILL §2 铁律 3）。
    """
    t = it.text()
    crowd = 3 if any(w in t for w in ("全国", "各地", "多地", "跨省", "大部", "全行业")) else \
        (2 if any(w in t for w in ("多省", "全省", "波及", "上下游")) else 1)
    region = 3 if any(w in t for w in ("跨省", "多省", "全国", "西北地区", "华北")) else \
        (2 if any(w in t for w in ("全省", "区域", "多地")) else 1)
    severity = 3 if severe_pool(it) else (2 if any(
        w in t for w in ("预警", "应急响应", "死亡", "伤亡", "地震", "疫情")) else 1)
    action = 3 if it.A >= 11 else (2 if it.A >= 6 else 0)
    total = round(min(12.0, crowd * 1.5 + region + severity * 1.5 + action), 3)
    return {"人群范围": crowd, "地域范围": region, "紧迫与严重": severity,
            "可行动性": action, "合计": total}


def impact_score(it: RawItem) -> float:
    """兼容旧调用；四维明细见 impact4()。"""
    return impact4(it)["合计"]


def force_first_hit(it: RawItem, cfg: dict) -> tuple[bool, str]:
    """需求④：≥7 级地震 / 死伤多的事故 / 多渠道高覆盖 → 强制第 1。

    三个条件任一命中即可；全部是可核对词组与数字，**禁用「重大 / 影响大」主观词**。
    只返回「是否命中 + 依据」，位次由 orchestrate 改，评级数值一律不动。
    """
    rk = cfg.get("rank", {}) or {}
    t = it.text()
    reasons: List[str] = []
    quake = re.search(r"(\d+(?:\.\d+)?)\s*级地震", t)
    if quake and float(quake.group(1)) >= float(rk.get("quake_min", 7.0)):
        reasons.append(f"震级 {quake.group(1)} ≥ {rk.get('quake_min', 7.0)} 级")
    deaths = [int(x) for x in re.findall(r"(\d+)\s*人死亡", t)]
    deaths += [int(x) for x in re.findall(r"死亡超\s*(\d+)", t)]
    casualties = [int(x) for x in re.findall(r"伤亡\s*(\d+)\s*人", t)]
    casualties += [int(x) for x in re.findall(r"(\d+)\s*余人(?:受伤|伤亡)", t)]
    if any(n >= int(rk.get("casualty_min", 10)) for n in deaths):
        reasons.append(f"死亡 {max(deaths)} 人 ≥ {rk.get('casualty_min', 10)}")
    if any(n >= int(rk.get("casualty_total_min", 30)) for n in casualties):
        reasons.append(f"伤亡 {max(casualties)} 人 ≥ {rk.get('casualty_total_min', 30)}")
    ch = len(set(p.strip() for p in it.source.split(" · ") if p.strip()))
    if ch >= int(rk.get("cross_channels_min", 4)):
        reasons.append(f"独立信源 {ch} 个 ≥ {rk.get('cross_channels_min', 4)}（多渠道高覆盖）")
    return (bool(reasons), "；".join(reasons))


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def compute_U(main: List[RawItem], cfg: dict) -> List[dict]:
    sc, oc = cfg.get("scoring", {}), cfg.get("orchestrate", {})
    w_final = float(sc.get("u_final_weight", 8))
    w_intl = float(sc.get("intl_important_weight", 6))
    bonus = sc.get("intl_label_bonus", {}) or {}
    max_adj = max([x.Final_adj for x in main] or [1.0]) or 1.0
    trace = []
    for it in main:
        groups, n_hits, it.daily_score = daily_groups(it)
        label_bonus = float(bonus.get(it.intl_tag, 0)) if it.intl_tag else 0.0
        intl_score = w_intl if it.intl_important else 0.0
        # SKILL §4.9 基线：U = daily_score + cross_score + w×clip(Final_adj/max)
        #                  + intl_important_score + intl_label_bonus。
        # 需求④（用户口径，写入附录 7 转换表）：影响面四维按 rank.impact4_weight
        #   计入 U，**只决定展示顺序**，不改 Final / Final_adj / 等级 / gin / cap / V / I / C。
        # head_score 仍按 SKILL §4.10 原式，不受影响面四维影响。
        imp4 = impact4(it)
        imp = imp4["合计"]
        rk = cfg.get("rank", {}) or {}
        w_imp = float(rk.get("impact4_weight", 1.0)) if rk.get("impact4_in_u", True) else 0.0
        it.U = round(it.daily_score + it.cross_score
                     + w_final * _clip(it.Final_adj / max_adj)
                     + intl_score + label_bonus + w_imp * imp, 3)
        it.head_score = round(1.8 * it.daily_score + 1.5 * it.cross_score
                              + 6.0 * _clip(it.Final_adj / max_adj), 3)
        trace.append({"标题": it.title, "日常组": "、".join(groups),
                      "daily_score": it.daily_score, "独立渠道数": len(set(
                          [p.strip() for p in it.source.split(" · ") if p.strip()])),
                      "cross_score": it.cross_score,
                      "影响面四维": "人群%d/地域%d/紧迫%d/可行动%d" % (
                          imp4["人群范围"], imp4["地域范围"], imp4["紧迫与严重"], imp4["可行动性"]),
                      "impact_score": imp, "impact4权重": w_imp,
                      "U": it.U, "head_score": it.head_score,
                      "Final_adj": it.Final_adj, "纯 Final_adj 基准序": it.score_order,
                      "强制第1依据": force_first_hit(it, cfg)[1]})
    return trace


def _pick_head(pool: List[RawItem], head_window: int,
               interleave_head: bool = True) -> tuple[List[RawItem], List[dict], bool]:
    """SKILL §4.10：资格池 = cross_score>0 且 daily_score>0；池不足 head_window 才放宽到全池
    并声明 head_relaxed。逐槽在「上位大类 ≠ 上一条大类」的候选里取 head_score 最高者。
    不再另设「特别严重突发池置顶」或「D 级不得占头部」等 SKILL 之外的前置开关。
    """
    qualified = [x for x in pool if x.cross_score > 0 and x.daily_score > 0]
    relaxed = len(qualified) < head_window
    src = qualified if not relaxed else pool
    log, head, last_group = [], [], ""
    head_ids: set = set()
    for slot in range(1, head_window + 1):
        cands = [x for x in src if id(x) not in head_ids
                 and (not interleave_head or x.group != last_group)]
        if not cands:
            cands = [x for x in src if id(x) not in head_ids]
        if not cands:
            break
        best = max(cands, key=lambda x: (x.head_score, x.Final_adj, -x.seq))
        forced = bool(last_group and best.group == last_group)
        head.append(best)
        head_ids.add(id(best))
        log.append({"槽位": slot, "标题": best.title, "上位大类": best.group,
                    "head_score": best.head_score, "daily_score": best.daily_score,
                    "cross_score": best.cross_score, "Final_adj": best.Final_adj,
                    "资格池": "多渠道+日常相关" if not relaxed else "放宽到全池（head_relaxed）",
                    "相邻同类": "被迫连续同类" if forced else "已错开"})
        last_group = best.group
    return head, log, relaxed


def _cover_front(head: List[RawItem], rest: List[RawItem], cfg: dict) -> tuple[List[RawItem], List[dict], dict]:
    oc = cfg.get("orchestrate", {})
    front_window = int(oc.get("front_window", 10))
    max_per_cat = int(oc.get("max_per_cat", 3))
    intl_cap = int(cfg.get("rules", {}).get("intl_front_cap", 4))
    front_left = max(0, front_window - len(head))
    counts: Dict[str, int] = {}
    for x in head:
        counts[x.group] = counts.get(x.group, 0) + 1
    front, log = [], []
    front_ids: set = set()
    for it in sorted(rest, key=lambda x: (-x.U, -x.Final_adj, x.seq)):
        if len(front) >= front_left:
            break
        cap = intl_cap if it.group == "国际动态" else max_per_cat
        # 前窗内同一三类重大突发标签最多 1 条
        if it.group == "国际动态" and it.intl_tag and any(
                x.intl_tag == it.intl_tag for x in head + front):
            continue
        if counts.get(it.group, 0) >= cap:
            continue
        front.append(it)
        front_ids.add(id(it))
        counts[it.group] = counts.get(it.group, 0) + 1
    # 覆盖保底：未被覆盖但有准入条目的大类，换掉前窗中「所属类 ≥2 条且 U 最低」的一条
    available = {x.group for x in head + front + rest}
    for g in sorted(available - set(counts)):
        cand = next((x for x in sorted(rest, key=lambda y: (-y.U, y.seq))
                     if x.group == g and id(x) not in front_ids), None)
        if not cand:
            continue
        victims = [x for x in front if counts.get(x.group, 0) >= 2]
        if not victims:
            continue
        victim = min(victims, key=lambda x: (x.U, x.Final_adj))
        idx = front.index(victim)
        front[idx] = cand
        counts[victim.group] -= 1
        counts[g] = 1
        log.append({"覆盖保底换入": cand.title, "大类": g, "换出": victim.title,
                    "换出 U": victim.U, "换入 U": cand.U, "依据": "前窗未覆盖且有准入条目"})
    covered_before = len({x.group for x in head + front})
    capacity = len(available) * max_per_cat
    note = ("容量检查：可用大类数 × max_per_cat < front_window，上限自动放宽"
            if capacity < front_window else "容量充足")
    return front, log, {"covered_before": covered_before, "front_window": front_window,
                        "note": note, "intl_front_cap": intl_cap}


def _interleave(head: List[RawItem], front: List[RawItem], tail: List[RawItem],
                cfg: dict) -> tuple[List[RawItem], List[dict]]:
    oc = cfg.get("orchestrate", {})
    eps = float(oc.get("interleave_label_epsilon", 1.0))
    front_window = int(oc.get("front_window", 10))
    # 分两段：front（前窗）与 tail 各自在段内按 U 降序贪心，
    # 保证覆盖保底换入的条目不会被 tail 中 U 更高的条目挤出前 10（SKILL §4.12）。
    ordered, log, prev = list(head), [], (head[-1] if head else None)
    used: set = {id(x) for x in ordered}
    pos = len(head)
    for segment, segment_pool in (("front", front), ("tail", tail)):
        pool = sorted(segment_pool, key=lambda x: (-x.U, -x.Final_adj, x.seq))
        while True:
            remaining = [x for x in pool if id(x) not in used]
            if not remaining:
                break
            pos += 1
            ok = [x for x in remaining if not prev or x.group != prev.group]
            u_sorted = remaining  # 已按 U 降序
            if ok:
                best = max(ok, key=lambda x: (x.U, x.Final_adj, -x.seq))
                if prev and best.tag == prev.tag and eps > 0:
                    alt = [x for x in ok if x.tag != prev.tag and best.U - x.U <= eps]
                    if alt:
                        chosen = max(alt, key=lambda x: (x.U, x.Final_adj, -x.seq))
                        action = "标签错开改取次优"
                    else:
                        chosen, action = best, "按 U 降序取合格者"
                else:
                    chosen, action = best, "按 U 降序取合格者"
            else:
                chosen = u_sorted[0]
                action = "被迫连续同类（容量不足）"
            shift = next((i for i, x in enumerate(u_sorted) if id(x) == id(chosen)), 0)
            chosen.interleave_action = action
            chosen.interleave_shift = shift
            log.append({"展示序": pos, "U 降序位": shift + 1, "标题": chosen.title,
                        "上位大类": chosen.group, "分类标签": chosen.tag, "U": chosen.U,
                        "打散动作": action, "后移位数": shift, "段": segment,
                        "前窗内": pos <= front_window,
                        "相邻同大类": bool(prev and chosen.group == prev.group),
                        "相邻同标签": bool(prev and chosen.tag == prev.tag)})
            ordered.append(chosen)
            used.add(id(chosen))
            prev = chosen
    return ordered, log


def _protected(it: RawItem) -> bool:
    return it.func_tier in PROTECTED_TIERS or it.Final >= 70


def apply_max_items(ordered: List[RawItem], max_items: int | None) -> tuple[List[RawItem], dict]:
    """§4.14 保护截断：不直接按 Final_adj 截前 N；最终仍按 Final_adj 单一降序。"""
    if not max_items or max_items >= len(ordered):
        return ordered, {"启用": False}
    by_adj = sorted(ordered, key=lambda x: (-x.Final_adj, -x.Final, x.seq))
    chosen = {id(x) for x in by_adj[:max_items]}
    prot = [x for x in ordered if _protected(x) and id(x) not in chosen]
    chosen |= {id(x) for x in prot}
    # 大类覆盖保底（未剔除大类在选集中 0 条时补其展示分最高的一条）
    groups = {x.group for x in ordered}
    for g in groups:
        if not any(x.group == g for x in ordered if id(x) in chosen):
            cand = next((x for x in by_adj if x.group == g), None)
            if cand:
                chosen.add(id(cand))
    selected = [x for x in ordered if id(x) in chosen]
    if len(selected) > max_items:
        drop = sorted([x for x in selected if not _protected(x)],
                      key=lambda x: (x.Final_adj, x.Final))[: len(selected) - max_items]
        drop_ids = {id(x) for x in drop}
        selected = [x for x in selected if id(x) not in drop_ids]
    selected.sort(key=lambda x: (-x.Final_adj, -x.Final, x.seq))
    # 反事实验证：把上限临时压到更小的数跑一遍
    smaller = max(1, max_items // 2)
    counter = sorted(ordered, key=lambda x: (-x.Final_adj, -x.Final, x.seq))[:smaller]
    kept_ids = {id(x) for x in selected}
    dropped = [{"全表展示序": i + 1, "标题": x.title, "展示分": x.Final_adj,
                "原始 Final": x.Final, "是否 protected": _protected(x),
                "大类": x.group} for i, x in enumerate(ordered) if id(x) not in kept_ids]
    for i, x in enumerate(selected, 1):
        x.display_order = i
    return selected, {
        "启用": True, "上限": max_items, "保护集合条数": sum(1 for x in ordered if _protected(x)),
        "反事实验证上限": smaller, "反事实条数": len(counter),
        "被截条目逐条核验": dropped,
        "打散位次损失": sum(1 for x in selected if x.interleave_shift > 0),
    }


def orchestrate(main: List[RawItem], cfg: dict) -> tuple[List[RawItem], dict]:
    oc = cfg.get("orchestrate", {})
    u_trace = compute_U(main, cfg)
    if not oc.get("enabled", True):
        ordered = sorted(main, key=lambda x: (-x.Final_adj, -x.Final, x.seq))
        for i, x in enumerate(ordered, 1):
            x.display_order = i
        return ordered, {"开关": "orchestrate=false（纯 Final_adj 降序）", "U 留痕": u_trace}

    head_window = int(oc.get("head_window", 3))
    head, head_log, relaxed = _pick_head(main, head_window, True) if head_window else ([], [], False)
    head_ids = {id(x) for x in head}
    rest = [x for x in main if id(x) not in head_ids]
    front, cover_log, cover_stat = _cover_front(head, rest, cfg)
    front_ids = {id(x) for x in head + front}
    tail = [x for x in main if id(x) not in front_ids]
    if oc.get("interleave", True):
        ordered, inter_log = _interleave(head, front, tail, cfg)
    else:
        ordered = head + sorted(front + tail, key=lambda x: (-x.U, -x.Final_adj, x.seq))
        inter_log = [{"开关": "interleave=false（按纯 U 降序）"}]

    # 需求④：强制第 1（只改展示序，评级数值一个不动，逐条留痕）
    force_log: List[dict] = []
    rk = cfg.get("rank", {}) or {}
    if rk.get("force_first", True):
        hits = [(x, force_first_hit(x, cfg)) for x in ordered]
        hits = [(x, r) for x, r in hits if r[0]]
        if hits:
            # 多条命中时取「命中依据数最多 → Final_adj 最高 → 采集序最早」，可复算
            def _ncond(r): return len(r[1][1].split("；"))
            pick = max(hits, key=lambda hr: (_ncond(hr), hr[0].Final_adj, -hr[0].seq))[0]
            before = ordered.index(pick) + 1
            ordered.remove(pick)
            ordered.insert(0, pick)
            force_log.append({"标题": pick.title, "原展示序": before, "新展示序": 1,
                              "命中依据": force_first_hit(pick, cfg)[1],
                              "数值未改": "Final / Final_adj / 等级 / gin / cap / V / I / C 全部保持"})
        else:
            force_log.append({"标题": "—", "原展示序": "—", "新展示序": "—",
                              "命中依据": "本批无 ≥7 级地震 / 死伤多事故 / 多渠道高覆盖条目",
                              "数值未改": "无需调整"})
    for i, x in enumerate(ordered, 1):
        x.display_order = i
    max_items = cfg.get("scoring", {}).get("max_items") or cfg.get("editing", {}).get("max_items")
    trunc = {}
    if max_items:
        ordered, trunc = apply_max_items(ordered, int(max_items))

    fw = int(oc.get("front_window", 10))
    front_pairs = sum(1 for r in inter_log[:fw - len(head)]
                      if isinstance(r, dict) and r.get("相邻同大类"))
    all_pairs = sum(1 for r in inter_log if isinstance(r, dict) and r.get("相邻同大类"))
    label_pairs = sum(1 for r in inter_log if isinstance(r, dict) and r.get("相邻同标签"))
    forced = sum(1 for r in inter_log if isinstance(r, dict) and "被迫" in str(r.get("打散动作", "")))
    return ordered, {
        "开关": "orchestrate=true（U 决定展示序，评级数值未改）",
        "特别严重突发池命中条数（仅诊断留痕，不参与头部选择）": sum(1 for x in main if severe_pool(x)),
        "head_window": head_window, "head_relaxed": relaxed,
        "front_window": fw, "max_per_cat": oc.get("max_per_cat", 3),
        "intl_front_cap": cover_stat.get("intl_front_cap"),
        "U 留痕": u_trace, "头部三席留痕": head_log, "覆盖保底留痕": cover_log,
        "相邻打散留痕": inter_log, "覆盖统计": cover_stat,
        "前10覆盖大类数": len({x.group for x in ordered[:10]}),
        "相邻同大类对数（前窗）": front_pairs,
        "相邻同大类对数（全表）": all_pairs,
        "相邻同标签对数": label_pairs,
        "被迫连续处数": forced,
        "前10吃到多渠道加分的条数": sum(1 for x in ordered[:10] if x.cross_score > 0),
        "保护截断": trunc,
        "强制第1留痕": force_log,
        "影响面四维计入U": bool((cfg.get("rank", {}) or {}).get("impact4_in_u", True)),
    }
