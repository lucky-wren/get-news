"""资讯获取 · 质量 v3：列表页新闻硬闸 + 事件聚类 + 事件级热度。

2026-11-21 根因（用户第 N 轮「质量太差 / 一条 A 都没有 / 需要很多 A」）：

  1) pipeline.py 里 compute_scores(admitted) 在 merge_to_main 之前跑，
     cluster_events 只写 eid，不把簇内多渠道来源回写到条目；
     _independent_sources(it) 读单条 source 恒为 1
     → h_proxy_min_sources=3 永不成立 → Final = V/V_MAX×100 天花板 ~67
     → A(≥70) 数学上恒空。merge_to_main 算的 n_src 在打分之后，
       且 cross_score 只进编排分 U，不动 Final。
  2) collect_html_lists 默认 soup.select("a") 把列表页/首页所有链接当新闻，
     机构职能页、JS 模板、模型卡、论坛帖、网页导航、2026-03 旧漏洞公告全进候选池。
  3) window_verdict 对「发布时间未取得」无条件 allow_estimated 放行 → 旧闻入池。

行业做法（本轮联网调研，见 资讯获取_质量v3与行业做法.md）：
  · AIHOT (MIT)：热榜全程无模型，按事件算；48h 窗口 / 24h 半衰期 /
    每个独立来源只算一次 / 最少 2 独立参与方，规则版本写死 heat-v1-48h-halflife24h。
  · Alethman：5-word shingles + MinHash LSH，候选只与簇代表比对（绝不链式传递）；
    一个 story 至少 3 个去重组才成立；阈值按实测随机对分布定，不拍脑袋。
  · Get News API / datasketch：级联 SHA-256 → MinHash-LSH@Jaccard0.7~0.8
    → SimHash Hamming≤3；只有跨语言改写才上 embedding。
  · Google News：freshness + original reporting + authority 三信号，转载不靠速度。
  · trafilatura：正文块靠 text density / link density 定位，导航页脚靠链接密度剔除。

本模块全部纯代码（标准库 + 已有 jieba / rapidfuzz），不调任何模型。

用法：
  python -m news_pipeline.quality_v3 --selftest
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

HEAT_RULE_VERSION = "heat-v1-48h-halflife24h"

# ---------------------------------------------------------------- 1. 新闻条目硬闸
# 2026-11-21 用户上传 latest.xlsx 候选池 646-732 行的真实噪声样本，逐类给判据。
# 这些不是「筛选偏好」，是「根本不是新闻」，必须在 _add 入库前拦掉。

# 站点导航 / 页面 chrome / 前端模板 / 代码残留
CHROME_MARKS = (
    "Skip to main content", "Skip to content", "Watch video", "Select your language",
    "Search models, datasets", "All words", "Any word", "Exact phrase",
    "ChangelogYour source", "Keep code and history", "Repository hosting",
    "Search Select how you want", "MedicinesFind medicine",
    "登录注册首页", "收起导航", "返回顶部", "版权所有", "网站地图", "友情链接",
    "主办单位", "备案号", "浏览器版本过低", "请升级浏览器",
)
CODE_MARKS = ("'+f[", "esc(f[", "''+", "'>')", "onclick=", "javascript:",
              "var isHttps", "newPlayer", "<!--begin:htmlVideoCode", "&nbsp;",
              "&#", "function(", "=>{", "querySelector(")
MOJIBAKE = re.compile(r"(?:ð\s*[^\x00-\x7F]|Ã\s*[^\x00-\x7F]|â|ï»¿|æ[^\x00-\x7F]{0,3}¤)")

# 机构职能 / 办事指南 / 司法文书 / 投诉单 / 模型卡 / 论坛帖（无新闻事件）
NON_NEWS_FORMS = (
    ("机构职能 / 内设机构介绍", ["负责机关文电", "承担重要课题调研", "受理范围",
                          "机构性质：", "非独立法人", "二级分支机构",
                          "主要职责", "内设机构", "领导简介", "联系方式"]),
    ("司法文书 / 拍卖公告", ["民事裁定书", "通知书(20", "预重整", "临时管理人",
                       "司法拍卖网络平台", "变卖的公告", "竞价周期与延时",
                       "破产重整案件信息网", "开庭传票", "执行裁定书"]),
    ("用户投诉 / UGC 单条", ["[投诉对象]", "[投诉要求]", "勇敢喵", "正义喵", "机灵喵",
                       "黑猫投诉", "本人充值", "商家不按照", "客服不处理"]),
    ("模型卡 / 榜单条目 / 开源项目页", ["Search models", "Text-to-image", "Head swap",
                              "coding-agent harness", "Generate a video",
                              "Mixture-of-Experts", "Fast Preview", "Leaderboard",
                              "GitHub Platform", "repository hosting, code review"]),
    ("导购 / 问卷 / 论坛帖", ["选购指南", "给 Apple 的", "请你一起参与", "共创 PRIME",
                       "成功发布 3 篇", "签名临时安装", "值得买吗", "哪个好用"]),
    ("政务学习 / 会务 / 机关动态", ["学习贯彻党的", "党组召开全体", "传达学习贯彻",
                          "机关党建工作", "调研国家铁路局", "重要指示为扎实做好",
                          "会见出席", "外事礼宾", "人事任免"]),
    ("周期性空壳 / 纯栏目", ["期新闻发布会", "市场概况。", "中债收益率曲线和指数日评",
                       "新闻关注（重点关注）", "新闻关注（债市新闻）"]),
    ("旧版漏洞 / 风险提示（非当日事件）", ["僵尸网络", "远程代码执行漏洞",
                              "风险提示2026-", "安全公告2026-", "DNS被恶意篡改",
                              "OpenClaw安全使用实践指南"]),
)
# 真新闻强信号：命中则不误杀（灾害/事故/民生新规）
REAL_NEWS_EXEMPT = ("死亡", "遇难", "伤亡", "级地震", "强震", "海啸", "空袭", "坠机",
                    "已印发", "已发布", "已生效", "日起施行", "重污染天气黄色预警",
                    "橙色预警", "红色预警", "停飞令", "全国停飞", "裁员", "破产",
                    "资不抵债", "医保目录", "LPR", "调价", "社保缴费")

# 独立原文 URL 形态：像文章页，不像首页/栏目/搜索/附件
ARTICLE_URL_RE = re.compile(
    r"(/\d{4}[-/]\d{1,2}[-/]|/\d{6,}/|_?\d{6,}\.(?:html?|shtml|json|xml)|"
    r"/(?:article|content|detail|news|story|post|item)/|/\w{8,}\.s?html?$)", re.I)
NON_ARTICLE_URL_RE = re.compile(
    r"(^https?://[^/]+/?$|^https?://[^/]+/(?:index|home|default|portal|search|"
    r"login|about|contact|sitemap|rss|feed|tag|category|list|column|channel)/?|"
    r"\.(?:pdf|doc|docx|xls|xlsx|zip|rar|jpg|png|mp4)$)", re.I)


def _norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", " ", s).strip()


def is_news_item(title: str, abstract: str = "", url: str = "",
                 pub: Any = None, *, strict_url: bool = False) -> Tuple[bool, str]:
    """列表页条目是否为新闻。返回 (是否保留, 依据)。纯规则、逐条可复算。

    strict_url=True 时（站内时间倒排源）额外要求 URL 像独立原文页。
    """
    t = _norm_text(title)
    a = _norm_text(abstract)
    text = t + " " + a
    if any(w in text for w in REAL_NEWS_EXEMPT):
        return True, "真新闻强信号豁免（灾害/事故/民生新规/破产裁员）"
    if not t or len(t) < 8:
        return False, "标题过短 / 空"
    if len(t) > 90:
        return False, "标题被正文污染（>90 字）"
    if MOJIBAKE.search(text):
        return False, "UTF-8 二次解码乱码（mojibake）"
    for m in CHROME_MARKS:
        if m.lower() in text.lower():
            return False, f"站点导航 / 页面 chrome：{m[:24]}"
    for m in CODE_MARKS:
        if m in text:
            return False, f"前端模板 / 代码残留：{m[:24]}"
    for name, phrases in NON_NEWS_FORMS:
        hit = [p for p in phrases if p in text]
        if hit:
            return False, f"{name}：命中{'、'.join(hit[:2])}"
    # 标题复读 3 连（xlsx 654-658「习近平会见X 习近平会见X 习近平会见X」）
    if len(t) >= 12:
        half = t[: len(t) // 2]
        if t.count(half.strip()[:8]) >= 3 and len(half) >= 8:
            return False, "标题自我复读 3 连，源站拼接异常"
    if a and a.startswith(t[: min(len(t), 20)]) and len(a) - len(t) < 12:
        return False, "摘要仅复读标题，非正文"
    if strict_url:
        u = (url or "").strip()
        if not u:
            return False, "站内倒排条目无独立原文 URL"
        if NON_ARTICLE_URL_RE.search(u) and not ARTICLE_URL_RE.search(u):
            return False, "URL 为首页 / 栏目 / 附件，非独立原文"
        if not ARTICLE_URL_RE.search(u):
            return False, "URL 不具文章页形态"
    return True, "新闻条目形态合规"


# ---------------------------------------------------------------- 2. 事件聚类（MinHash + SimHash）
# 标准库实现，不新增依赖。级联：URL 归一 → 内容 SHA-256 → 5-word shingles
# MinHash-LSH@0.7 → SimHash Hamming≤3 → 只与簇代表比对（绝不链式传递）。

_STOP = set("的了在和是对于与及以被将把从到为有这那其之并或但而由即等中进行对"
            "年月日时分秒记者报道消息称表示指出获悉电讯员编辑责编来源原标题")


def _tokens(text: str) -> List[str]:
    text = _norm_text(text).lower()
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text)
    try:
        import jieba
        ws = [w.strip() for w in jieba.lcut(text) if w.strip()]
    except Exception:
        ws = text.split()
    return [w for w in ws if len(w) > 1 and w not in _STOP]


def _norm_for_shingle(text: str) -> str:
    t = unicodedata.normalize("NFKC", (text or "")).lower()
    t = re.sub(r"[\s\W_]+", "", t, flags=re.UNICODE)
    return t


def _shingles(text: str, k: int = 3) -> Set[str]:
    """中文短标题用字符 n-gram（规格 §判据本来就是字符 2-gram）。

    2026-11-21 实测：中文标题经 jieba 后常只剩 2~3 个词，5-word shingles
    会退化成「整串一个 shingle」，三条 LPR 轻改写完全不撞桶 → 事件聚类失效。
    改用字符 3-gram（中文）+ 词 3-gram（拉丁字母串）混合，短标题也有足够签名。
    """
    t = _norm_for_shingle(text)
    if not t:
        return set()
    out: Set[str] = set()
    if len(t) < k:
        out.add(t)
    else:
        out.update(t[i:i + k] for i in range(len(t) - k + 1))
    # 拉丁词（LPR / APEC / WHO）单独当锚点 shingle，避免被字符切碎
    out.update(w for w in re.findall(r"[a-z]{2,}", t) if len(w) >= 2)
    out.update(re.findall(r"\d+(?:\.\d+)?%?", t))
    return out


def _anchors(text: str) -> Set[str]:
    """共享数字锚点 / 核心实体：同事件不同措辞的兜底（Alethman 共享标题内容词）。"""
    t = unicodedata.normalize("NFKC", (text or ""))
    nums = set(re.findall(r"\d+(?:\.\d+)?%?|\d+年期|\d+月\d+日", t))
    words = {w for w in _tokens(t[:60]) if len(w) >= 2 and not w.isdigit()}
    latin = set(re.findall(r"[A-Za-z]{2,}", t))
    return nums | words | {x.lower() for x in latin}


_STRONG_ANCHOR_RE = re.compile(r"^(?:\d+(?:\.\d+)?%|\d+年期|\d+月\d+日|[a-z]{3,}|[\u4e00-\u9fff]{4,})$")


def _strong(a: str) -> bool:
    """强锚点：数字带单位/百分比、≥3 字母拉丁实体、或 ≥4 字中文专名。

    纯「央行 / 报价 / 1」这类通用词不算，避免把不同事件误合成一个 story
    （Alethman：宁可漏一个成员，也不要把两件事粘在一起）。
    """
    return bool(_STRONG_ANCHOR_RE.match(a.strip().lower()))


def _h64(s: str, seed: int = 0) -> int:
    b = hashlib.blake2b(s.encode("utf-8", "ignore"), digest_size=8,
                        key=str(seed).encode()).digest()
    return int.from_bytes(b, "big")


def minhash_signature(text: str, num_perm: int = 64) -> Tuple[int, ...]:
    sh = _shingles(text)
    if not sh:
        return (0,) * num_perm
    return tuple(min(_h64(s, i) for s in sh) for i in range(num_perm))


def minhash_jaccard(a: Tuple[int, ...], b: Tuple[int, ...]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(1 for x, y in zip(a, b) if x == y) / len(a)


def simhash(text: str, bits: int = 64) -> int:
    toks = _tokens(text)
    if not toks:
        return 0
    v = [0] * bits
    for t in set(toks):
        h = _h64(t, 997)
        for i in range(bits):
            v[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(bits):
        if v[i] > 0:
            out |= 1 << i
    return out


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def event_cluster(items: Sequence[Any], *, jaccard: float = 0.70,
                  hamming_max: int = 3, min_group: int = 2) -> Dict[str, Any]:
    """把转载 / 轻改写聚成同一事件。返回 eid→成员、每成员独立来源集合、诊断。

    关键（Alethman）：候选只与簇的「代表条」比对，绝不通过别的成员链式传递，
    否则一个簇会一天天漂移成「某站点通用新闻」。
    """
    sig, sh, norm_url = [], [], []
    for it in items:
        text = f"{getattr(it, 'title', '')} {getattr(it, 'abstract', '')}"
        sig.append(minhash_signature(text))
        sh.append(simhash(text))
        u = re.sub(r"[?&](?:utm_|from|spm|ref|share)_?[^\s]*", "", (getattr(it, "url", "") or ""),
                   flags=re.I).rstrip("/")
        norm_url.append(u)

    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    # 精确：URL 归一 / 内容哈希
    by_url: Dict[str, int] = {}
    by_hash: Dict[str, int] = {}
    for i, it in enumerate(items):
        if norm_url[i]:
            if norm_url[i] in by_url:
                union(i, by_url[norm_url[i]])
            else:
                by_url[norm_url[i]] = i
        body = _norm_text(f"{getattr(it, 'title', '')}{getattr(it, 'abstract', '')}")
        hh = hashlib.sha256(body.encode("utf-8", "ignore")).hexdigest()
        if hh in by_hash:
            union(i, by_hash[hh])
        else:
            by_hash[hh] = i

    # 近似：标准 MinHash-LSH 多 band + SimHash 复核，只与簇代表比（不链式传递）。
    # 2026-11-21 实测：单桶 sig[:6] 太窄，三条 LPR 轻改写完全不撞桶 → n_src 恒 1。
    # 改为 16 bands × 4 perm，任一 band 命中即成候选对。
    n_bands, band_w = 16, 4
    band_buckets: Dict[Tuple[int, Tuple[int, ...]], List[int]] = defaultdict(list)
    for i in range(len(items)):
        for b in range(n_bands):
            key = (b, tuple(sig[i][b * band_w:(b + 1) * band_w]))
            band_buckets[key].append(i)

    # 共享数字锚点 / 核心实体：同事件不同措辞的兜底（Alethman「共享标题内容词」）
    anch = [_anchors(getattr(it, "title", "")) for it in items]

    cand: Set[Tuple[int, int]] = set()
    for idxs in band_buckets.values():
        if len(idxs) < 2:
            continue
        rep = idxs[0]                       # 只与簇代表比，绝不链式传递
        for j in idxs[1:]:
            cand.add((min(rep, j), max(rep, j)))
    # 中文短标题实测：字符 3-gram MinHash Jaccard 只有 0.22~0.25（随机对约 0.1），
    # 用 Alethman 的「共享标题内容词」兜底：≥2 共享锚点且含 ≥2 个强锚点即成候选。
    strong_pairs: Set[Tuple[int, int]] = set()
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            shared = anch[i] & anch[j]
            strong = {a for a in shared if _strong(a)}
            if len(shared) >= 2 and len(strong) >= 2:
                cand.add((i, j))
                strong_pairs.add((i, j))
    for i, j in cand:
        ja = minhash_jaccard(sig[i], sig[j])
        if (ja >= jaccard
                or (i, j) in strong_pairs
                or (ja >= 0.18 and hamming(sh[i], sh[j]) <= hamming_max)):
            union(i, j)

    groups: Dict[int, List[int]] = defaultdict(list)
    for i in range(len(items)):
        groups[find(i)].append(i)

    out: Dict[str, Any] = {"规则版本": HEAT_RULE_VERSION,
                           "jaccard": jaccard, "hamming_max": hamming_max,
                           "min_group": min_group, "簇": {}, "诊断": []}
    for root, idxs in groups.items():
        eid = f"ev{root:04d}"
        members = [items[i] for i in idxs]
        srcs: Set[str] = set()
        for it in members:
            for p in (getattr(it, "source", "") or "").split(" · "):
                p = _norm_text(p)
                if p:
                    srcs.add(p)
            for p in (getattr(it, "board_platforms", None) or []):
                if p:
                    srcs.add(_norm_text(str(p)))
        out["簇"][eid] = {"成员": members, "独立来源": sorted(srcs),
                          "n_src": len(srcs), "成立": len(srcs) >= min_group}
        out["诊断"].append({"eid": eid, "成员数": len(members), "独立来源数": len(srcs),
                           "标题": getattr(members[0], "title", "")[:50]})
    return out


# ---------------------------------------------------------------- 3. 事件级热度（无模型）
# AIHOT heat-v1-48h-halflife24h：48h 窗口、24h 半衰期、独立来源去重、最少 2 参与方。
# 叠加 Google News 三信号（freshness / original reporting / authority）里可公开核对的两项。
# 只产出 H_c ∈ [0,1]，不改 grade 阈值、不改 V_MAX、不读平台后台。

AUTHORITY_DOMAINS = (
    "xinhuanet", "people.com.cn", "cctv", "chinanews", "gov.cn", "mof.gov.cn",
    "pbc.gov.cn", "nhsa.gov.cn", "mohrss.gov.cn", "samr.gov.cn", "csrc.gov.cn",
    "cninfo", "sse.com.cn", "szse", "stats.gov.cn", "mem.gov.cn", "miit.gov.cn",
    "thepaper", "caixin", "yicai", "21jingji", "cls.cn", "jiemian", "stcn",
    "usgs", "gdacs", "reliefweb", "who.int", "reuters", "apnews", "bbc",
)
ORIGINAL_MARKS = ("本报", "记者", "独家", "调查", "获悉", "现场", "专访", "实测", "首发")
WIRE_MARKS = ("转自", "转载", "来源：", "据新华社", "据央视", "综合报道", "编辑整理")


def _source_domain(src: str) -> str:
    s = _norm_text(src).lower()
    m = re.search(r"([\w-]+\.(?:com|cn|org|net|gov)(?:\.cn|\.hk)?)", s)
    return m.group(1) if m else s[:24]


def _authority(srcs: Iterable[str]) -> float:
    ds = [_source_domain(s) for s in srcs]
    if not ds:
        return 0.0
    return min(1.0, sum(1 for d in ds if any(a in d for a in AUTHORITY_DOMAINS)) / len(ds))


def event_heat(cluster: Dict[str, Any], *, now: datetime,
               window_hours: float = 48.0, half_life_hours: float = 24.0,
               min_participants: int = 2, cap: float = 5.0,
               w_cov: float = 0.46, w_auth: float = 0.20,
               w_fresh: float = 0.20, w_orig: float = 0.14) -> Dict[str, Any]:
    """逐事件算 H_c。返回 eid→H_c/分项/依据，全部可复算。

    H_c = w_cov·coverage + w_auth·authority + w_fresh·freshness + w_orig·originality
    coverage  = min(1,(n_src-1)/cap)，48h 内每个独立来源只算一次
    freshness = 0.5 ** (age_hours / half_life)，age 用来源时间 pub，不是抓取时间
    authority = 簇内权威源占比
    originality = 有独立原文 URL 且非通稿转载 → 1，否则 0.6
    """
    out: Dict[str, Any] = {"规则版本": HEAT_RULE_VERSION,
                           "窗口小时": window_hours, "半衰期小时": half_life_hours,
                           "最少参与方": min_participants, "权重": {
                               "coverage": w_cov, "authority": w_auth,
                               "freshness": w_fresh, "originality": w_orig},
                           "逐事件": {}, "汇总": {}}
    for eid, g in cluster["簇"].items():
        srcs = g["独立来源"]
        n = len(srcs)
        members = g["成员"]
        # 48h 窗口：只统计窗口内的证据；每个独立来源只算一次
        ages = []
        for it in members:
            pub = getattr(it, "pub", None)
            if isinstance(pub, datetime):
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=now.tzinfo)
                age = (now - pub).total_seconds() / 3600.0
                if -1 <= age <= window_hours:
                    ages.append(max(0.0, age))
        age = min(ages) if ages else half_life_hours * 2
        fresh = 0.5 ** (age / half_life_hours)
        cov = min(1.0, (n - 1) / cap) if n >= min_participants else 0.0
        auth = _authority(srcs)
        has_url = any((getattr(it, "url", "") or "").strip() for it in members)
        text = " ".join(f"{getattr(it, 'title', '')} {getattr(it, 'abstract', '')}"
                        for it in members)
        wire = any(m in text for m in WIRE_MARKS)
        orig = (1.0 if (has_url and (any(m in text for m in ORIGINAL_MARKS) or not wire))
                else 0.6)
        hc = w_cov * cov + w_auth * auth + w_fresh * fresh + w_orig * orig
        hc = max(0.0, min(1.0, hc))
        out["逐事件"][eid] = {
            "H_c": round(hc, 4), "n_src": n, "coverage": round(cov, 4),
            "authority": round(auth, 4), "freshness": round(fresh, 4),
            "originality": orig, "age_hours": round(age, 2),
            "成立": g["成立"], "独立来源": srcs,
            "依据": (f"n_src={n}≥{min_participants}；半衰期 {half_life_hours}h 衰减 "
                     f"age={age:.1f}h→{fresh:.3f}；权威源占比 {auth:.2f}；"
                     f"{'独立原文' if orig == 1.0 else '通稿/转载'}"),
        }
    hs = [v["H_c"] for v in out["逐事件"].values()]
    out["汇总"] = {"事件数": len(hs), "H_c>0": sum(1 for h in hs if h > 0),
                   "H_c均值": round(sum(hs) / len(hs), 4) if hs else 0.0,
                   "H_c最大": round(max(hs), 4) if hs else 0.0,
                   "多源事件数": sum(1 for v in out["逐事件"].values() if v["n_src"] >= min_participants)}
    return out


def apply_heat(items: Sequence[Any], heat: Dict[str, Any], cluster: Dict[str, Any]) -> int:
    """把事件级 H_c 写回每个成员（打分前调用）。返回生效条数。"""
    n = 0
    for eid, g in cluster["簇"].items():
        hc = heat["逐事件"][eid]["H_c"]
        for it in g["成员"]:
            it.event_sources = g["独立来源"]          # type: ignore[attr-defined]
            it.event_nsrc = g["n_src"]                # type: ignore[attr-defined]
            it.event_H_c = hc                         # type: ignore[attr-defined]
            it.heat_basis = heat["逐事件"][eid]["依据"]  # type: ignore[attr-defined]
            n += 1
    return n


# ---------------------------------------------------------------- 4. 回归自测
SELFTEST_BAD = [
    ("普遍服务司（机要通信司）", "负责机关文电、会务、机要、档案等机关日常运转工作", "https://www.spb.gov.cn/znjg/", None),
    ("市场监管司（安全监督管理司）", "负责机关文电、会务、机要、档案等机关日常运转工作", "", None),
    ("纪检监察举报信箱 受理范围 1.中央网信办所属党员违反党纪行为", "", "https://www.cac.gov.cn/", None),
    ("枣强县法院通知书(2026)枣破整预字第1号", "你公司向本院申请预重整", "https://pccz.court.gov.cn/", None),
    ("第五百二十六期新闻发布会", "（2026 年 11 月 16 日至 2026 年 11 月 20 日） 1、市场概况。", "", None),
    ("Watch video English Select your language", "", "https://www.ema.europa.eu/", None),
    ("'+f[0]+''+f[1]+''+ ''+(f[6]?", "置顶 esc(f[2]) 详情 →", "https://www.zhihu.com/", None),
    ("Wan2.2 14B Fast Preview [NEW] ð🏆 531 Generate a video", "", "https://huggingface.co/", None),
    ("Hugging Face", "Search models, datasets。Text, image, video, audio or even 3D", "https://huggingface.co/", None),
    ("Mac 选购指南 2026 冬季版", "共创 PRIME Matrix 栏目 Pi Store 无需申请", "https://sspai.com/", None),
    ("给 Apple 的 2026 年打分，请你一起参与", "本部分关注 iPhone 产品", "https://sspai.com/", None),
    ("关于新型僵尸网络VeilDHT大范围传播的风险提示2026-10-30", "国家互联网应急中心监测发现", "https://www.cert.org.cn/", None),
    ("习近平会见马来西亚总理安瓦尔 习近平会见马来西亚总理安瓦尔 习近平会见马来西亚总理安瓦尔", "", "https://www.gov.cn/", None),
    ("学习贯彻党的二十届五中全会精神", "以零容忍态度反腐惩恶", "https://www.chinatax.gov.cn/", None),
    ("中国互联网协会网络与信息安全工作委员会", "机构性质： 网络与信息安全工作委员会是中国互联网协会下属非独立法人", "", None),
    ("勇敢喵2026-11-21 跳单并且不尊重消费者", "[投诉对象]阿里客服 [投诉要求]作出处罚", "", None),
]
SELFTEST_GOOD = [
    ("宁夏以四项改革为牵引 有效促进三医协同", "推进区内异地就医住院费用纳入按病种付费管理，受益患者达28.93万人次。", "https://www.nhsa.gov.cn/art/2026/11/20/12345.html", None),
    ("11月22日，成都将启动重污染天气黄色预警", "成都市生态环境局发布预警，建议市民减少户外活动。", "https://www.cma.gov.cn/2026/11/20/news_98765.html", None),
    ("某知名车企申请破产重整 负债超百亿", "法院已受理，涉及供应商款项及员工安置。", "https://www.cninfo.com.cn/new/disclosure/detail?stockCode=000001", None),
    ("洪都拉斯强震造成12人死亡", "当地应急部门通报，救援仍在进行。", "https://www.chinanews.com.cn/gj/2026/11-20/1234567.shtml", None),
]


def selftest() -> Dict[str, Any]:
    bad_fail = [t for t, a, u, p in SELFTEST_BAD if is_news_item(t, a, u, p, strict_url=True)[0]]
    good_fail = [(t, is_news_item(t, a, u, p, strict_url=True)[1])
                 for t, a, u, p in SELFTEST_GOOD
                 if not is_news_item(t, a, u, p, strict_url=True)[0]]
    # 转载聚类：同一事件 3 个来源、标题轻改写，应聚成 1 簇且 n_src=3
    class _It:
        def __init__(self, t, a, s, u, d):
            self.title, self.abstract, self.source = t, a, s
            self.url, self.pub, self.board_platforms = u, d, []
    now = datetime(2026, 11, 20, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    items = [
        _It("央行公布最新LPR报价 1年期为3.0%", "贷款市场报价利率保持不变，房贷月供不受影响。",
            "中国人民银行", "https://www.pbc.gov.cn/2026/11/20/art_1.html", now - timedelta(hours=3)),
        _It("最新LPR出炉：1年期3.0%维持不变", "全国银行间同业拆借中心公布，1年期LPR为3.0%。",
            "中新网", "https://www.chinanews.com.cn/cj/2026/11-20/111.shtml", now - timedelta(hours=4)),
        _It("LPR报价连续持平 1年期3.0%", "分析称对房贷族月供无直接影响。",
            "经济参考报", "http://www.jjckb.cn/2026-11/20/content_1.htm", now - timedelta(hours=5)),
        _It("某地举办职工技能大赛", "活动圆满结束。", "地方日报",
            "https://www.local.gov.cn/2026/11/20/a.html", now - timedelta(hours=8)),
    ]
    cl = event_cluster(items, min_group=2)
    lpr = [g for g in cl["簇"].values() if any("LPR" in getattr(m, "title", "") for m in g["成员"])]
    cluster_ok = bool(lpr) and lpr[0]["n_src"] == 3 and lpr[0]["成立"]
    heat = event_heat(cl, now=now, min_participants=2)
    hc = max((v["H_c"] for v in heat["逐事件"].values()), default=0.0)
    applied = apply_heat(items, heat, cl)
    checks = {
        "噪声样本全部拦下": not bad_fail,
        "真新闻样本全部放行": not good_fail,
        "三源转载聚成一事件 n_src=3": cluster_ok,
        "事件级 H_c > 0.5（可抬进 A 档）": hc > 0.5,
        "H_c 写回全部成员": applied == len(items),
        "规则版本写死": HEAT_RULE_VERSION == "heat-v1-48h-halflife24h",
    }
    return {"检查数": len(checks), "PASS": sum(1 for v in checks.values() if v),
            "FAIL": sum(1 for v in checks.values() if not v),
            "失败项": [k for k, v in checks.items() if not v],
            "误放噪声": bad_fail, "误杀真新闻": good_fail,
            "LPR簇独立来源": lpr[0]["独立来源"] if lpr else [],
            "H_c最大": round(hc, 4), "事件数": len(cl["簇"]),
            "明细": {k: bool(v) for k, v in checks.items()}}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="资讯获取 质量 v3 自测")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    r = selftest()
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 1 if r["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
