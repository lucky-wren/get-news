"""同事件聚类 / 多渠道重复度 / 议题簇去重（SKILL §4.8、§4.13、§4.13.1、附录 12/12b/17）。

事件层与议题层严格分离：事件层合并计入 cross_score（可数序次信号，不冒充 H），
议题层只收敛同一议题的不同侧面，不计入 cross_score。
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Tuple

from .model import RawItem
from .rules import story_key

EVENT_PHRASES = ["停飞令", "大雾橙色预警", "级地震", "KPL", "总冠军", "FMVP", "重映", "定档",
                 "开画", "票房榜", "火神火箭", "破产重整", "医保目录", "寒潮蓝色预警",
                 "车辆召回", "重整计划", "发射失败", "发射异常", "A股"]

# 2026-11-20 Actions 批次实测：LPR 连 18 个月不变被 5 家来源各发一条，
# 标题分别是「中国LPR连续18个月保持不变 / 2026年11月LPR报价保持稳定… /
# 事关房贷！央行公布最新LPR / 最新LPR公布！ / LPR报价连续第18个月持稳」。
# 字符 2-gram Jaccard 全低于 0.30、LCS<5、NUM_ANCHOR 不认「18个月」、
# EVENT_PHRASES 没有 LPR → 分成 5 个独立事件主键 → H 代理 _nsrc 恒 1
# → Final 天花板 ~69.86、A(≥70) 恒空。
# 主题词簇是「同事件」的语义兜底：命中同一主题 key 且共享数字锚点或同一天，
# 判为同簇。不编造来源，只让公开可核对的多渠道传播足迹真正进入 H 代理。
# 只收「同一具体事件」的词，不收「新能源 / AI算力」这种大类词——
# 2026-11-20 回放实测：用大类词会把财政部/商务部/彩票等 58 条不同政务稿
# 并成一簇，H 代理狂加分刷出 212 个 S/A，正是用户①明确不要的「稳中向好」。
TOPIC_KEYS: Dict[str, List[str]] = {
    "LPR连续不变": ["LPR连续", "LPR报价", "最新LPR", "公布最新LPR", "LPR保持"],
    "医保目录": ["医保目录", "国谈", "一类新药进医保", "医保钱包"],
    "公积金提取": ["公积金提取", "优化公积金"],
    "成品油调价": ["成品油调价", "每升下调", "加满一箱"],
    "社保断缴": ["社保断缴", "社保缴满15年"],
    "地方债限额": ["地方债结存限额", "专项债四季度"],
    "保交楼": ["保交楼", "不惜贷抽贷断贷"],
    "破产重整主体": ["破产重整", "受理破产清算", "职工债权"],
    "召回公告": ["召回公告", "召回近18万辆"],
    "重污染预警": ["重污染天气黄色预警", "启动重污染"],
    "寒潮预警": ["寒潮蓝色预警", "寒潮橙色预警"],
    "级地震": ["级地震", "震源深度"],
}
NUM_ANCHOR = re.compile(r"\d+(?:\.\d+)?\s*(?:级|亿元|万吨|万人|元/升|元每升|%|人死亡|人受伤|颗|股|列|公里|例|支|个统筹|家|吨|亿|万|TOPS|万人|居民|个月|届|年|日)")
DAY_ANCHOR = re.compile(r"(?:20\d{2}年)?\d{1,2}月\d{1,2}日|\d{1,2}日")
DATE_ONLY = re.compile(r"^\d{1,2}月\d{1,2}日$|^\d{4}-\d{2}-\d{2}$")
STOP_CHARS = re.compile(r"[\s，,。、：:；;！!？?（）()【】\[\]「」“”\"'·|｜\-—/]")


def normalize_channel(name: str) -> str:
    s = (name or "").strip()
    for p in ("抖音", "微博", "微信", "百度", "知乎", "今日头条", "B站", "快手", "小红书"):
        if s.startswith(p):
            return p
    return s


def _norm_title(t: str) -> str:
    s = STOP_CHARS.sub("", t or "")
    # 栏目前缀只在【】/() 成对时才剥离
    s = re.sub(r"^【[^】]+】|^\([^)]+\)|^（[^）]+）", "", s)
    return s


def _bigrams(s: str) -> set:
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else {s}


def _jaccard(a: str, b: str) -> float:
    """规格判据：字符 2-gram 集合 Jaccard，由 oss 层实现。

    rapidfuzz 3.14.5 实测不含 Jaccard 模块，且「2-gram 集合」语义与库内
    任何编辑距离都不同，不能硬套；rapidfuzz 只用于 LCSseq / fuzz 诊断。
    """
    from . import oss
    return oss.bigram_jaccard(_norm_title(a), _norm_title(b))


def _lcs(a: str, b: str) -> int:
    """规格判据：最长**公共子串**（必须连续）。rapidfuzz.LCSseq 是公共子序列，
    语义不同，不能替换；oss 层保留自写 DP，并额外提供 LCSseq 作候选召回诊断。"""
    from . import oss
    return oss.lcs_substring(a, b)


def _anchors(text: str) -> set:
    out = set()
    for m in NUM_ANCHOR.finditer(text or ""):
        tok = m.group(0).strip()
        if not DATE_ONLY.match(tok) and (("." in tok) or re.search(r"[^\d\s]", tok)):
            out.add(tok)
    return out


class DSU:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def cluster_events(items: List[RawItem]) -> Tuple[List[dict], List[dict]]:
    """给每条赋统一 eid；返回 (逐对判定理由, 簇信息)。"""
    n = len(items)
    dsu = DSU(n)
    norms = [_norm_title(it.title) for it in items]
    anchors = [_anchors(it.text()) for it in items]
    topic_hits = []
    for it in items:
        t = (it.title or "") + " " + (it.abstract or "")
        topic_hits.append({k for k, ws in TOPIC_KEYS.items() if any(w in t for w in ws)})
    day_hits = [set(DAY_ANCHOR.findall((it.title or "") + " " + (it.abstract or ""))) for it in items]
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            ja, lc = _jaccard(norms[i], norms[j]), _lcs(norms[i], norms[j])
            shared_num = bool(anchors[i] & anchors[j])
            shared_phrase = [p for p in EVENT_PHRASES if p in norms[i] and p in norms[j]]
            shared_topic = sorted(topic_hits[i] & topic_hits[j])
            shared_day = bool(day_hits[i] & day_hits[j])
            reason = ""
            if ja >= 0.55 or lc >= 8:
                reason = f"2-gram Jaccard={ja:.2f}≥0.55 / LCS={lc}≥8"
            elif (ja >= 0.30 or lc >= 5) and shared_num:
                reason = f"Jaccard={ja:.2f}/LCS={lc} 且共享数字锚点 {sorted(anchors[i] & anchors[j])[:2]}"
            elif shared_phrase and (shared_num or ja >= 0.20):
                reason = f"共享事件词组 {shared_phrase[:1]} 且共享数字/主体"
            elif shared_topic and (shared_num or ja >= 0.18
                                  or min(len(norms[i]), len(norms[j])) <= 12):
                # 主题簇兜底：LPR / 医保目录 / 油价 这类「多家同题不同标题」。
                # 不能只用「同一天」就合并——2026-11-20 回放实测那样会把
                # 财政部/商务部/彩票等 58 条不同政务稿并成一簇，刷出假 A/S。
                # 短讯标题（≤12 字，如「最新LPR公布！」）是同一事件的常见简写，
                # 允许纯主题归并；长标题仍要共享数字锚点或 Jaccard≥0.18。
                reason = (f"共享主题簇 {shared_topic[:1]}（共享数字={shared_num} / "
                          f"Jaccard={ja:.2f} / 短讯标题="
                          f"{min(len(norms[i]), len(norms[j])) <= 12}）")
            if reason:
                dsu.union(i, j)
                from . import oss
                pairs.append({"标题 A": items[i].title, "标题 B": items[j].title,
                              "理由": reason,
                              "开源诊断（不进判定）": (
                                  f"rapidfuzz LCSseq={oss.lcsseq_len(norms[i], norms[j])} / "
                                  f"fuzz={oss.fuzz_ratio(norms[i], norms[j])}；"
                                  f"jieba 词级 2-gram 交集="
                                  f"{len(oss.word_bigrams(norms[i]) & oss.word_bigrams(norms[j]))}")})
    groups: Dict[int, List[int]] = defaultdict(list)
    for i in range(n):
        groups[dsu.find(i)].append(i)
    # 事件主键分两组编号，对齐目标态台账：
    #   e1xxx = 独立原文 / 检索 / 多渠道合并；e2xxx = 社交平台词条回查成稿
    clusters = []
    n1 = n2 = 0
    for _, idxs in sorted(groups.items(), key=lambda x: min(x[1])):
        social = any("社交平台" in (items[i].record_type or "")
                     or "回查成稿" in (items[i].record_type or "") for i in idxs)
        if social:
            n2 += 1
            eid = f"e2{n2:03d}"
        else:
            n1 += 1
            eid = f"e1{n1:03d}"
        for i in idxs:
            items[i].eid_raw = items[i].eid or f"raw{items[i].seq}"
            items[i].eid = eid
        clusters.append({"eid": eid, "idx": idxs, "size": len(idxs)})
    return pairs, clusters


def _independent_sources(it: RawItem) -> set:
    parts = [p.strip() for p in (it.source or "").split(" · ") if p.strip()]
    return {normalize_channel(p) for p in parts} | set(it.board_platforms)


def merge_to_main(items: List[RawItem]) -> Tuple[List[RawItem], List[dict], dict]:
    """主表按事件主键去重；被合并渠道记录逐条进附录 12b；cross_score 只进编排分 U。"""
    by_eid: Dict[str, List[RawItem]] = defaultdict(list)
    for it in items:
        by_eid[it.eid or f"seq{it.seq}"].append(it)
    main, merged_log = [], []
    for eid, group in by_eid.items():
        group.sort(key=lambda x: (x.evidence != "A", -len(_independent_sources(x)), -x.Final, x.seq))
        rep = group[0]
        all_sources, seen = [], set()
        for it in group:
            for s in _independent_sources(it):
                if s and s not in seen:
                    seen.add(s)
                    all_sources.append(s)
        # 代表条「来源」沿用候选池台账原值，保证「主表是候选池子集且非评级列逐格一致」；
        # 簇内全部渠道逐条列在附录 12b，不静默消失。
        rep.media_slots = max((x.media_slots for x in group), default=rep.media_slots)
        rep.board_slots = max((x.board_slots for x in group), default=rep.board_slots)
        rep.board_platforms = sorted({p for x in group for p in x.board_platforms}) or rep.board_platforms
        n_src = len(seen)
        rep.cross_score = min(12.0, (n_src - 1) * 3) if n_src > 1 else 0.0
        rep.record_type = rep.record_type or "主条"
        main.append(rep)
        for it in group[1:]:
            merged_log.append({"事件主键": eid, "保留稿": rep.title, "被合并稿": it.title,
                               "来源": it.source, "渠道": it.channel})
    stats = {
        "聚类合并条数": len(items) - len(main),
        "同事件簇数": len(main),
        "多渠道事件数": sum(1 for x in main if x.cross_score > 0),
        "单事件最多渠道数": max([len(_independent_sources(x)) for x in main] or [0]),
        "cross_score 最大值": max([x.cross_score for x in main] or [0]),
        "前 10 吃到多渠道加分的条数": 0,   # 编排后回填
    }
    return main, merged_log, stats


def story_dedup(main: List[RawItem], enabled: bool = True,
                max_keep: int = 1) -> Tuple[List[RawItem], List[dict], dict]:
    """同一议题下的不同侧面：每簇保留 max_keep 条代表，其余完整内容进附录 17。"""
    if not enabled:
        return main, [], {"议题簇数": 0, "议题簇合并条数": 0, "开关": "story_dedup=false"}
    clusters: Dict[str, List[RawItem]] = defaultdict(list)
    for it in main:
        key = story_key(it)
        if key:
            clusters[key].append(it)
            it.sid = key
    keep_ids, log = set(), []
    for name, group in clusters.items():
        group.sort(key=lambda x: (-x.Final_adj, -len(hits_subject(x, name)), x.seq))
        for it in group[:max_keep]:
            keep_ids.add(id(it))
        for it in group[max_keep:]:
            log.append({"议题名": name, "代表条": group[0].title, "被合并条": it.title,
                        "来源": it.source, "摘要首段": it.abstract.split("\n")[0][:120],
                        "命中主体与动作词组": story_key(it)})
    reduced = [it for it in main if not it.sid or id(it) in keep_ids]
    return reduced, log, {
        "议题簇数": len(clusters),
        "议题簇合并条数": len(main) - len(reduced),
        "story_max_keep": max_keep,
        "开关": "story_dedup=true（议题层不计入 cross_score）",
    }


def hits_subject(it: RawItem, story: str) -> list:
    return [w for w in (it.title or "") if w in story]
