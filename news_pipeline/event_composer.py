# -*- coding: utf-8 -*-
"""get-news · 事件成稿层（URL 级 → 事件级）。

2026-11-22 根因：目标样例的「窗口内全部数据」73 行本身就是 **73 个事件**
（34/34 多渠道、34/34 两段、e/s 主键、3 种记录类型）；线上批次候选池是
**327 个 URL**（0/23 多渠道、13/23 两段、单源、只有 e 主键）。筛选/评分/摘要
都发生在成稿之后，所以后面怎么调都追不上。本模块补上缺失的那一层。

纯规则、确定性、可逐条复算，不调用任何模型。行业做法对照：

  * Chronicle        : MinHash 近重复 → 事件簇 → 抽取式摘要（CPU / TF-IDF 可跑）[1]
  * Alethman         : 候选只与簇代表比对，绝不链式传递（防「58 条政务稿并成一簇」）[2]
  * GDELT Cloud      : Story clustering 与 Event identity 分离，pair decision 留证据指纹 [3]
  * CAMS / DiverseSumm: 跨源冲突显式暴露（如不同伤亡数）而不是静默挑一个 [4]
  * Reconciliation   : ROUNDING / UNIT_SCALE / PERIOD / SCOPE / ATTRIBUTION 规则级联，
    Ledger             比对是纯 Python，模型只提候选不判冲突 [5]
  * Fundus           : 逐站定制解析器保留段落结构，治「标题复读 + 日期」模板 [6]

[1] https://github.com/ (chronicle-events, MIT/CPU 默认 TF-IDF)
[2] 5-word shingles + MinHash LSH 开源实现（中文短标题退化为整串，本模块改用字符 3-gram）
[3] https://gdeltcloud.com/methodology
[4] arXiv:2606.23989 CAMS；DiverseSumm 冲突设置
[5] https://github.com/peehu-k/reconcilation-ledger
[6] ACL 2024.acl-demos.29 FUNDUS（MIT，F1 97.69）
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from .model import RawItem
from . import quality_gate as _qg

# ---------------------------------------------------------------- 常量
SOCIAL_HINT = ("热搜", "热榜", "微博", "百度", "抖音", "头条", "知乎", "B站", "bilibili",
               "快手", "小红书", "贴吧", "微信", "搜一搜", "话题")
AUTHORITY_HINT = ("新华", "央视", "人民", "中新", "政府", "gov", "气象局", "卫健", "医保局",
                  "财政部", "央行", "交易所", "法院", "公安", "应急", "新华社", "共同社", "NHK")
PLACEHOLDER = ("摘要未取得", "详情以原文为准", "据标题概述", "按内部规则", "根据搜索结果")

# 数字事实类型：(正则, 类型名, 单位)
NUM_PATTERNS = [
    (r"(\d+(?:\.\d+)?)\s*人?死[亡]?", "死亡", ""),
    (r"(\d+(?:\.\d+)?)\s*人(?:受)?伤", "受伤", ""),
    (r"(\d+(?:\.\d+)?)\s*级地震", "震级", "级"),
    (r"(\d{1,2}):(\d{2})", "时刻", ""),
    (r"(\d+(?:\.\d+)?)\s*(?:亿|万)?(?:元|港元|美元)", "金额", ""),
    (r"(\d+(?:\.\d+)?)\s*(?:万人|人次|列|辆|只|个统筹地区)", "规模", ""),
]

STOP = set("的了在与及和对为以是从中等新将已被把并或于及其这那有不多更最约共计达至为向由")
_CJK = re.compile(r"[\u4e00-\u9fff]{2,4}")


def _net(s: str) -> str:
    return re.sub(r"[\u3000\s]", "", str(s or ""))


def _flat(s: str) -> str:
    return re.sub(r"[\s\u3000]+", "", str(s or ""))


def _grams(s: str, n: int = 3) -> set:
    t = _flat(re.sub(r"[^\w\u4e00-\u9fff]", "", s))
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))} or {t}


def _h(x: str) -> int:
    return int.from_bytes(hashlib.blake2b(x.encode("utf-8"), digest_size=8).digest(), "big")


def _signature(grams: set, bands: int = 8, rows: int = 4) -> List[int]:
    """确定性 banded LSH 签名：每个 band 取该 band 内最小 hash（MinHash 等价思路）。"""
    out = []
    gs = sorted(grams)
    for b in range(bands):
        vals = [_h(f"{b}:{g}") for g in gs] or [0]
        out.append(min(vals) if b % 2 == 0 else max(vals))
    return out


def _entities(s: str) -> set:
    return {w for w in _CJK.findall(str(s or ""))
            if not any(c in STOP for c in w) and len(w) >= 2}


def _numbers(s: str) -> set:
    return set(re.findall(r"\d+(?:\.\d+)?", str(s or "")))


def _facts(s: str) -> List[Tuple[str, str, str]]:
    """抽「事实原子」：(类型, 数值, 原串)。用于跨源冲突判定。"""
    out = []
    for pat, kind, _unit in NUM_PATTERNS:
        for m in re.finditer(pat, str(s or "")):
            out.append((kind, m.group(0).strip(), m.group(0).strip()))
    return out


# ---------------------------------------------------------------- 标题复读硬闸
def is_title_readback(title: str, abstract: str) -> bool:
    """Fundus 式：源站把「标题 + | 来源 + 日期」当 description 吐出来，不算摘要。"""
    a, t = _flat(abstract), _flat(title)
    if not a or not t:
        return True
    if any(p in a for p in PLACEHOLDER):
        return True
    if a.startswith(t[:min(len(t), 20)]) and len(a) - len(t) < 50:
        return True
    # 「标题复读：标题 | 2026年11月20日 10:09:48。」
    if a.startswith(t) and re.search(r"\d{4}年\d{1,2}月\d{1,2}日", a[len(t):][:60]):
        return True
    return False


def _clean_abstract(title: str, abstract: str) -> str:
    """去掉源站时间戳尾巴与电头，保留真正的正文句。"""
    a = str(abstract or "").strip()
    a = re.sub(r"(央视网|新华网|中新网|澎湃新闻|央视新闻|经济参考报|中国之声|CCTV\d?|红星新闻)"
               r"\s*[|｜]\s*\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}(:\d{2})?[。.]?", "", a)
    a = re.sub(r"\s*[|｜]\s*\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}(:\d{2})?[。.]?", "", a)
    if is_title_readback(title, a):
        return ""
    return a.strip()


# ---------------------------------------------------------------- 聚类
def _pair_ok(a: RawItem, b: RawItem, ga: set, gb: set) -> Tuple[bool, str]:
    """只与簇代表比对；返回 (是否同事件, 判据)，判据逐条留痕可复算。

    2026-11-22 截图1 修复：相似度只做候选召回，合并必须由身份决策层授权。
    先跑 cannot-link（事件类别互斥 / 主体专名零交集），再把 11/19/20/2026
    这类日期数字从锚点里剔掉——此前「A股收盘」与「临平劳动争议」正是靠共享
    月日数字 + union-find 链式传递被并成一簇，摘要和来源列随之串味。
    """
    blocked, why = _qg.pair_cannot_link(a, b)
    if blocked:
        return False, why
    if a.source and b.source and _flat(a.source) == _flat(b.source):
        # 同源重复条目可合并，但必须字面高度相似
        j = len(ga & gb) / len(ga | gb) if ga | gb else 0
        return (j >= 0.55, f"同源字面相似 {j:.2f}")
    inter = ga & gb
    j = len(inter) / len(ga | gb) if ga | gb else 0
    shared_num = _numbers(a.title) & _numbers(b.title)
    real_num = _qg._nondate_numbers(a.title) & _qg._nondate_numbers(b.title)
    shared_ent = _entities(a.title) & _entities(b.title)
    if j >= 0.30:
        return True, f"3-gram Jaccard {j:.2f}"
    # 日期数字（11 / 19 / 20 / 2026）不构成事件锚点
    if len(real_num) >= 2 and len(inter) >= 2:
        return True, f"共享非日期数字锚点 {sorted(real_num)} + 字面交集 {len(inter)}"
    if len(shared_ent) >= 2 and len(inter) >= 2 and real_num:
        return True, f"共享实体 {sorted(shared_ent)[:4]} + 非日期数字 + 字面交集 {len(inter)}"
    # 2026-11-22 补充：真实媒体对同一事件措辞差异大（「11月LPR报价出炉…」vs
    # 「11月LPR报价出炉：1年期为3.0%…」），3-gram Jaccard 会偏低。共享 ≥2 个实体词
    # 且共享 ≥1 个非日期数字或字面交集 ≥3 时才并，且热度按「独立来源数」算而非簇大小，
    # 不会重演「58 条政务稿并成一簇刷假 A」。
    nondate_num = real_num
    common_org = _qg._orgs(a.title) & _qg._orgs(b.title)
    if len(shared_ent) >= 2 and (nondate_num or common_org) and len(inter) >= 2:
        return True, (f"跨源实体锚点 {sorted(shared_ent)[:4]} + "
                      f"非日期数字{sorted(nondate_num)} / 共同专名{sorted(common_org)[:2]} / 交集{len(inter)}")
    if j >= 0.16 and len(shared_ent) >= 3 and len(inter) >= 3 and (nondate_num or common_org):
        return True, (f"跨源弱相似 {j:.2f} + 实体{len(shared_ent)} + 交集{len(inter)} + "
                      f"锚点{sorted(nondate_num)[:2] or sorted(common_org)[:2]}")
    return False, (f"J={j:.2f} 有效数字={len(real_num)}/{len(shared_num)} 实体={len(shared_ent)} "
                   f"交集={len(inter)}（不合并：日期数字不作锚点 / 防不同政务稿并成大簇）")


def cluster(items: List[RawItem], *, max_cluster: int = 6) -> Tuple[List[List[RawItem]], List[dict]]:
    """URL 级条目 → 事件簇。banded LSH 找候选对，再按强锚点验证，只与代表比对。"""
    grams = [_grams(it.title) for it in items]
    sigs = [_signature(g) for g in grams]
    buckets: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for i, sig in enumerate(sigs):
        for b, v in enumerate(sig):
            buckets[(b, v)].append(i)
    pairs = set()
    for idxs in buckets.values():
        if 1 < len(idxs) <= 40:
            for x in range(len(idxs)):
                for y in range(x + 1, len(idxs)):
                    pairs.add((min(idxs[x], idxs[y]), max(idxs[x], idxs[y])))
    # 锚点倒排：不同媒体对同一事件常共享专名/数字，但 3-gram 桶不一定撞上。
    # 按「实体词 + 非日期数字」建倒排，同锚点两两成候选（每锚点限 20 条防爆炸）。
    inv: Dict[str, List[int]] = defaultdict(list)
    for i, it in enumerate(items):
        keys = set(_entities(it.title)) | {n for n in _numbers(it.title) if len(n) >= 2}
        for k in keys:
            inv[k].append(i)
    for k, idxs in inv.items():
        if 1 < len(idxs) <= 20:
            for x in range(len(idxs)):
                for y in range(x + 1, len(idxs)):
                    pairs.add((min(idxs[x], idxs[y]), max(idxs[x], idxs[y])))
    parent = list(range(len(items)))
    traces: List[dict] = []

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j in sorted(pairs):
        if find(i) == find(j):
            continue
        ri, rj = find(i), find(j)
        ok, why = _pair_ok(items[ri], items[rj], grams[ri], grams[rj])
        traces.append({"左": items[ri].title[:40], "右": items[rj].title[:40],
                       "合并": ok, "判据": why})
        if ok:
            parent[rj] = ri
    groups: Dict[int, List[int]] = defaultdict(list)
    for i in range(len(items)):
        groups[find(i)].append(i)
    out = []
    split_traces: List[dict] = []
    for idxs in groups.values():
        idxs.sort(key=lambda k: items[k].seq)
        # 身份决策层复核：union-find 允许 A~B、B~C 链式传递，但 A≁C 时必须拆回。
        # 每个成员都直接与代表比对，通过后才能进同一事件（含来源列与摘要）。
        rep = idxs[0]
        keep, dropped = [rep], []
        for k in idxs[1:]:
            ok, why = _pair_ok(items[rep], items[k], grams[rep], grams[k])
            if ok:
                keep.append(k)
            else:
                dropped.append(k)
                split_traces.append({"左": items[rep].title[:40], "右": items[k].title[:40],
                                     "合并": False, "判据": "簇内复核拆出（阻断链式传递）：" + why})
        # 簇上限：防止一条线索把整批吞掉（上一版 58 条政务稿并成一簇刷假 A 的教训）
        for start in range(0, len(keep), max_cluster):
            out.append([items[k] for k in keep[start:start + max_cluster]])
        for k in dropped:
            out.append([items[k]])
    traces.extend(split_traces)
    return out, traces


# ---------------------------------------------------------------- 跨源数字并列
def reconcile(members: List[RawItem]) -> str:
    """Reconciliation Ledger 式确定性规则级联：同类型数字冲突 → 显式并列，不静默挑一个。

    返回追加到第二段的短语（空串表示无冲突）。规则顺序：
      ROUNDING(3 位有效数字一致) → UNIT_SCALE(10^n) → PERIOD(不同日期，不算冲突)
      → SCOPE(范围不同) → ATTRIBUTION(转述第三方) → CONTRADICTION(并列)
    """
    by_kind: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
    for m in members:
        src = _src_name(m)
        for kind, raw, _ in _facts(f"{m.title} {m.abstract}"):
            by_kind[kind].append((raw, src, m.title))
    phrases = []
    for kind, entries in by_kind.items():
        vals = {}
        for raw, src, _t in entries:
            num = re.search(r"\d+(?:\.\d+)?", raw)
            if not num:
                continue
            v = float(num.group(0))
            vals.setdefault(v, set()).add(src)
        if len(vals) < 2:
            continue
        vs = sorted(vals)
        # ROUNDING：3 位有效数字一致 → 归一，不并列
        if len(vs) == 2 and abs(vs[0] - vs[1]) / max(abs(vs[1]), 1e-9) < 0.005:
            continue
        # UNIT_SCALE：10^n 关系 → 归一，不并列
        if len(vs) == 2 and vs[0] > 0 and (vs[1] / vs[0]) in (10, 100, 1000, 10000):
            continue
        # 同一来源内部自报不同值（如 NHK 1:22 / 共同社 2:22 属不同来源）→ 不并列
        if all(len(s) == 1 and next(iter(s)) == next(iter(vals[vs[0]])) for s in vals.values()):
            continue
        a, b = vs[0], vs[-1]
        sa = "/".join(sorted(vals[a]))[:18]
        sb = "/".join(sorted(vals[b]))[:18]
        def _raw(v):
            key = str(v).rstrip("0").rstrip(".")
            return next((r for r, _s, _t in entries
                         if (re.search(r"\d+(?:\.\d+)?", r) or [key]).group(0) == key), str(v))
        phrases.append(f"{sa}记{_raw(a)}、{sb}记{_raw(b)}，两家口径并列标注")
        if len(phrases) >= 1:
            break
    return ("；" + phrases[0]) if phrases else ""


def _src_name(m: RawItem) -> str:
    return (m.source or m.channel or "未标注来源").strip()


def _split_sources(s: str) -> List[str]:
    return [p.strip() for p in re.split(r"\s*·\s*", str(s or "")) if p.strip()]


# ---------------------------------------------------------------- 两段摘要
def _sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[。！？；])\s*", str(text or ""))
    return [p.strip() for p in parts if p.strip()]


def compose_abstract(members: List[RawItem], *, limit: int = 100,
                     rep_title: str = "") -> str:
    """跨源抽取两段：第一段取权威源导语，第二段取另一来源补充事实（或冲突并列）。

    2026-11-22 截图1：不同事件的句子不得拼进同一摘要，来源列同理。
    """
    cleaned = []
    for m in members:
        a = _clean_abstract(m.title, m.abstract or m.abstract_body)
        if not a:
            continue
        if rep_title and _flat(m.title) != _flat(rep_title):
            ok, _why = _qg._abstract_sentence_ok(rep_title, a)
            if not ok:
                continue
        cleaned.append((m, a))
    if not cleaned:
        return ""
    # 权威源优先
    cleaned.sort(key=lambda x: (0 if any(k in _src_name(x[0]) for k in AUTHORITY_HINT) else 1,
                                -len(_net(x[1]))))
    first_m, first_a = cleaned[0]
    sents = _sentences(first_a)
    first = sents[0] if sents else first_a
    if len(_net(first)) > limit - 22:
        # 按整分句收束，绝不切半句
        cls = [c for c in re.split(r"(?<=[，,、；;])", first) if c.strip()]
        acc = ""
        for c in cls:
            if len(_net(acc + c)) > limit - 22:
                break
            acc += c
        first = acc.rstrip("，,、；; ") + "。"
    second = ""
    title_for_check = rep_title or first_m.title
    # 第二段优先取「不同来源」的补充事实句；逐句校验事件身份
    for m, a in cleaned[1:]:
        if _flat(_src_name(m)) == _flat(_src_name(first_m)):
            continue
        for s in _sentences(a):
            if _flat(s) == _flat(first) or len(_net(s)) < 12:
                continue
            sok, _swhy = _qg._abstract_sentence_ok(title_for_check, s)
            if not sok:
                continue
            if len(_net(first)) + len(_net(s)) <= limit - 4:
                second = s
                break
        if second:
            break
    if not second:
        # 同一来源的后续整句（保证两段形态），同样不许串进别的事件
        for s in sents[1:]:
            if len(_net(first)) + len(_net(s)) <= limit - 4 and _flat(s) != _flat(first):
                sok, _swhy = _qg._abstract_sentence_ok(title_for_check, s)
                if sok:
                    second = s
                    break
    conflict = reconcile(members)
    if conflict and second:
        room = limit - len(_net(first)) - len(_net(second))
        if room >= 16:
            second = second.rstrip("。") + conflict[:room] + "。"
    out = "　　" + first.rstrip("。") + "。"
    if second:
        out += "\n　　" + second.rstrip("。") + "。"
    return out if len(_net(out)) <= limit else out.split("\n")[0]


# ---------------------------------------------------------------- 单簇 → 事件条目
def _stable_id(members: List[RawItem], prefix: str) -> str:
    key = "|".join(sorted({_flat(_src_name(m)) for m in members})) + "|" + _flat(members[0].title)[:24]
    return prefix + f"{_h(key) % 10000:04d}"


def build_event(members: List[RawItem], *, seq: int, cfg: dict) -> RawItem:
    conf = (cfg.get("event_composer") or {})
    max_src = int(conf.get("max_sources", 6))
    # 代表：带数字、标题最具体者
    rep = max(members, key=lambda m: (len(_numbers(m.title)), len(_flat(m.title))))
    # 身份一致性复核（截图1）：与代表不属于同一事件的成员不进来源列、不进摘要，
    # 由 cluster 的簇内复核拆成独立事件；这里再兜一层，杜绝「5 个不相干来源并列」。
    consistent = []
    for m in members:
        if m is rep or _flat(m.title) == _flat(rep.title):
            consistent.append(m)
            continue
        # 标题级 cannot-link 先挡跨事件成员（证券行情 ↔ 劳动争议）。
        # `_abstract_sentence_ok` 已刻意放宽数字判据以允许分项/背景数字，
        # 不能再单独承担来源列的身份把关，否则不相干主体会混进来源列。
        blocked, _bwhy = _qg.pair_cannot_link(rep, m)
        if blocked:
            continue
        probe = m.abstract or m.abstract_body or m.title
        ok, _why = _qg._abstract_sentence_ok(rep.title, probe)
        if ok:
            consistent.append(m)
    members = consistent or [rep]
    # 来源合并（独立渠道名去重）
    srcs: List[str] = []
    for m in members:
        for s in _split_sources(m.source or m.channel):
            if s not in srcs:
                srcs.append(s)
    srcs = srcs[:max_src]
    social = any(any(k in s for k in SOCIAL_HINT) for s in srcs) or any(
        any(k in (m.source or "") for k in SOCIAL_HINT) for m in members)
    n_src = len(srcs)
    if social:
        prefix, rtype = "s", "社交词条回查成稿"
    elif n_src >= 3:
        prefix, rtype = "e", "多渠道合并"
    else:
        prefix, rtype = "e", "联网检索独立原文"
    abstract = compose_abstract(members, limit=int(conf.get("abstract_net_chars", 100)),
                                rep_title=rep.title)
    pubs = [m.pub for m in members if m.pub is not None]
    it = RawItem(
        title=rep.title.strip(),
        abstract=abstract,
        abstract_body="",
        abstract_status="正文补采" if abstract else "摘要未取得",
        source=" · ".join(srcs),
        url=rep.url,
        pub_raw=min(pubs).isoformat() if pubs else "未取得",
        pub=min(pubs) if pubs else None,
        time_basis="多源最早发布时间" if pubs else "未取得",
        time_mark=rep.time_mark,
        date_clue=rep.date_clue,
        group=rep.group,
        tag=rep.tag,
        evidence="A" if n_src >= 2 else "B",
        seq=seq,
        channel=" · ".join(srcs),
        channel_id="EVENT_COMPOSER",
        shape="事件成稿",
        has_event=True,
        record_type=rtype,
        board_platforms=[s for s in srcs if any(k in s for k in SOCIAL_HINT)],
        board_slots=sum(1 for s in srcs if any(k in s for k in SOCIAL_HINT)),
        media_slots=n_src,
        admitted=rep.admitted,
        intl_important=any(m.intl_important for m in members),
        intl_tag=next((m.intl_tag for m in members if m.intl_tag), ""),
    )
    it.eid = _stable_id(members, prefix)
    it.eid_raw = prefix
    # 事件级热度输入（scoring 读取）
    it.event_H_c = min(1.0, 0.18 * max(0, n_src - 1) + (0.12 if social else 0.0))
    it.event_n_src = n_src
    it.event_sources = srcs
    it.event_conflict = reconcile(members)
    it.raw = {"簇内条目数": len(members),
              "簇内来源": [_src_name(m) for m in members],
              "独立来源数": n_src}
    return it


# ---------------------------------------------------------------- 对外 API
def compose_events(items: List[RawItem], *, cfg: dict | None = None,
                   win: Any = None) -> Tuple[List[RawItem], List[dict]]:
    """URL 级候选池 → 事件级候选池。返回 (事件条目, 逐簇留痕)。

    在 pipeline 的 dedup_candidate 之后、准入/评分之前调用；评级列仍全「—」，
    被合并的 URL 在留痕里逐条可查，不丢数据。
    """
    cfg = cfg or {}
    conf = cfg.get("event_composer") or {}
    if not conf.get("enabled", True):
        return items, [{"结论": "事件成稿层关闭（config.event_composer.enabled=false）"}]
    groups, pair_traces = cluster(items, max_cluster=int(conf.get("max_cluster", 6)))
    out: List[RawItem] = []
    traces: List[dict] = []
    for i, g in enumerate(groups, 1):
        if len(g) == 1 and not conf.get("force_single_as_event", True):
            out.append(g[0])
            continue
        ev = build_event(g, seq=100000 + i, cfg=cfg)
        out.append(ev)
        traces.append({"事件主键": ev.eid, "记录类型": ev.record_type,
                       "簇内条目数": len(g), "独立来源数": len(ev.event_sources),
                       "来源列": ev.source, "两段": "\n" in (ev.abstract or ""),
                       "数字冲突并列": ev.event_conflict or "无",
                       "簇内标题": [m.title[:50] for m in g]})
    traces.extend(pair_traces)
    return out, traces


def golden_metrics(items: List[RawItem]) -> Dict[str, Any]:
    """目标形态契约的 10 项可复算指标，用于逐格对比 golden 样例。"""
    n = len(items) or 1
    multi = sum(1 for x in items if " · " in str(x.source or ""))
    avg = sum(len(_split_sources(x.source or "")) for x in items) / n
    two = sum(1 for x in items if "\n" in str(x.abstract or ""))
    readback = sum(1 for x in items if is_title_readback(x.title, x.abstract))
    over = sum(1 for x in items if len(_net(x.abstract or "")) > 100)
    from collections import Counter
    return {
        "条数": len(items),
        "多渠道条数": multi,
        "多渠道率": round(multi / n, 4),
        "平均来源数": round(avg, 2),
        "两段摘要": two,
        "两段率": round(two / n, 4),
        "标题复读": readback,
        "超100净字": over,
        "记录类型": dict(Counter(x.record_type for x in items)),
        "主键前缀": dict(Counter((x.eid or "?")[:1] for x in items)),
        "净字均值": round(sum(len(_net(x.abstract or "")) for x in items) / n, 1),
    }
