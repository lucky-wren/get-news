"""面向大众「每日要闻」编辑层：对应硬闸 + 正向选题 + 9 平台真实回查 + 严格降序。"""
from __future__ import annotations

import re
from collections import OrderedDict

from .clean import canon, normalize_source
from .model import Item

# 9 个主干平台：精确名，不被上位词吞掉
MAIN_PLATFORMS = ("微博", "百度", "抖音", "今日头条", "知乎",
                  "小红书", "快手", "贴吧", "微信搜一搜")

# 单条最多 2 个发现渠道：热词曾把 9 平台堆成一行；挂不下的逐平台申报缺口。
MAX_PLATFORMS_PER_ITEM = 2

# 政务发布主体：机构宣传稿不进每日要闻；§27 的「XX 发布」是线索台账。
_SOURCE_BLOCK = ("人民政府", "公安局", "公安分局", "派出所", "政务发布",
                 "管委会", "街道办", "区委", "市委", "文明办")

# 聚合页/早报/榜单镜像名：可以出现在来源里当「发现线索」，但不计平台票， 更不参与 impact 评分（此前就是它们把一行堆到 10 段刷成 S）。
AGGREGATE = tuple("夸克/搜狗热点|腾讯新闻早报|网易|搜狐|新浪早报|今日热榜聚合|澎湃热榜|微信24h热文榜|品玩|查榜网|早报|热榜聚合|热点聚合".split("|"))
_AGG_RE = re.compile(r"清单\s*§|清单§|\(清单|（清单")

# 大众贴近主题族：正向准入；标题命中某族时摘要须同族或共享专名。
FAMILIES: dict[str, tuple[str, ...]] = {
    "天气出行": ("冷空气", "降温", "升温", "雨雪", "暴雨", "大风", "台风", "高温",
             "寒潮", "气温", "℃", "气象", "预警", "雾霾", "结冰", "春运", "假期",
             "高速", "航班", "地铁", "公交", "铁路", "车票", "限行", "出行", "堵车"),
    "医保社保": ("医保", "社保", "养老金", "公积金", "药品", "集采", "医院", "挂号",
             "就诊", "看病", "疫苗", "流感", "传染病", "麻疹", "牙", "近视", "护理费",
             "报销", "门诊", "住院", "处方", "药师", "一老一小", "带娃", "半日托",
             "计时托", "老幼共托", "养老", "居家养老", "银发", "适老化", "育儿", "托班"),
    "物价消费": ("物价", "涨价", "降价", "菜价", "油价", "金价", "消费券", "补贴",
             "退税", "个税", "工资", "退款", "售后", "会员", "双十一", "快递",
             "外卖", "电商", "充电宝", "水电燃气", "供暖", "暖气", "房贷", "房租",
             "物业费", "返乡", "退票", "恶意差评", "差评", "月租", "安居", "旅居",
             "划算", "性价比", "纳税"),
    "食品安全": ("食品", "抽检", "不合格", "召回", "中毒", "过期", "添加剂",
             "餐厅", "外卖", "厨房", "保鲜", "菌落"),
    "诈骗与公共安全": ("诈骗", "反诈", "被骗", "套路", "警方", "立案", "判决",
                 "火灾", "爆炸", "事故", "伤亡", "遇难", "死亡", "失联", "坠亡",
                 "救援", "塌方", "洪水", "地震", "应急响应", "疏散", "被查", "通报"),
    "教育就业": ("高考", "中考", "开学", "学费", "幼儿园", "托育", "招聘", "就业",
             "失业", "助学金", "奖助学金", "考研", "校招", "寒暑假", "课时",
             "健康学校", "身心健康", "学生", "校园", "课后服务"),
    "住房与城市": ("房价", "租房", "落户", "户籍", "老旧小区", "电梯",
              "物业", "装修", "保障房", "棚改", "断供"),
    "文娱赛事": ("世界杯", "国足", "联赛", "夺冠", "比赛", "票房", "电影", "演唱会",
             "综艺", "春节档", "赛事", "球员", "决赛", "开赛"),
    "数码与消费电子": ("手机", "折叠", "旗舰", "华为", "Mate", "苹果", "iPhone",
                "小米", "芯片", "英伟达", "黄仁勋", "AI", "大模型", "智能",
                "发布会", "新品", "降价", "召回", "续航", "充电"),
    "航天与全国热点": ("神舟", "飞船", "发射", "乘组", "航天员", "空间站", "月球",
                "探测器", "卫星", "央行", "逆回购", "降息", "加息", "A股", "股价"),
    "国际重大": ("空袭", "遇袭", "加沙", "联合国", "撤侨", "中国公民", "伤亡",
             "地震", "恐袭", "坠机", "沉船"),
    "日常观察": ("落叶", "初冬", "景观", "超级月亮", "冰雪",
             "旅游", "文旅", "景区", "公园", "消费市场", "商圈", "市集",
             "冬季", "雪季", "图书馆", "博物馆", "美术馆",
             "展览", "阅读空间", "延时开放"),
}

# 排除分两组：B 组只扫标题，避免「在声明中说」误剔大众热点；A 组扫全文。
_STRONG_EXCLUDE = ("习近平", "总书记", "人民情怀", "学习贯彻", "全会精神", "亚太经合组织", "部际联席会议", "大国外交", "焦点访谈", "礼赞", "绘就时代华章", "征文", "优秀文章", "评选揭晓", "研讨班", "八项规定", "专项整治", "形式主义", "结算价格", "依托主业", "加快布局", "布局人工智能", "中方呼吁", "重要民生物资正常流通", "振兴", "APEC")
# B 组只扫标题：机构公文/存档味文体，摘要里顺带提一句不算。
_TITLE_EXCLUDE = ("纪实", "纪委", "监委", "融资保证金", "ETF期权合约", "答记者问", "征求意见", "品种名单", "说明书修订建议", "工作会议在", "专业委员会", "荣获", "贺电", "部际", "司负责人", "监管局关于", "公告", "通知", "新赛道", "峰会", "声明", "全链条全周期全流程", "服务保障", "提供全链条", "治理观察", "精雕细琢", "新支撑", "牵头制定全球首个", "持续增资扩产", "复苏提振新周期", "东西问", "体系警务", "解锁社区", "警务", "基层治理")
# 机构文体：无具体民生事实（金额/伤亡/气温/价格/时刻）不进主表。
_FACT_RE = re.compile(r"\d+\s*(?:元|亿元|万元|人|死|亡|℃|度|%|级|条|起|比|:\d|时|分)")

# 品牌/产品专名须在摘要复现，否则判为标题讲 A、摘要讲 B 的拼接。
_LATIN_BRAND = re.compile(r"[A-Za-z]{2,}[\s\-]?[A-Za-z0-9]*")
_BRAND_WORDS = tuple("华为 苹果 小米 英伟达 黄仁勋 荣耀 OPPO vivo 特斯拉 比亚迪".split())

_STOP_BIGRAM = set("影响 进行 发布 表示 有关 目前 今天 记者 报道 相关 工作 情况 方面 问题 可以 已经 没有 一个 这些 一些 以及 同时 其中 通过 要求 开展 实施 部门 单位 等地 地区 部分 当地 近日 日前 今年 以来 持续 不断 积极 有效 进一步 分别 陆续".split())

def _bigrams(s: str) -> set[str]:
    """中文实词 2-gram，过滤通用词。"""
    out: set[str] = set()
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", s or ""):
        for i in range(len(seg) - 1):
            b = seg[i:i + 2]
            if b not in _STOP_BIGRAM:
                out.add(b)
    return out

def _families(s: str) -> set[str]:
    t = s or ""
    return {name for name, words in FAMILIES.items() if any(w in t for w in words)}


def _named_ok(title: str, abstract: str) -> tuple[bool, str]:
    """标题含品牌专名时摘要须复现同一主体。"""
    a = abstract or ""
    lat = [m.group(0).strip() for m in _LATIN_BRAND.finditer(title or "")
           if len(m.group(0).strip()) >= 2]
    brands = [w for w in _BRAND_WORDS if w in (title or "")]
    tokens = lat + brands
    if not tokens:
        return True, ""
    for tk in tokens:
        key = re.sub(r"[\s\-]", "", tk).lower()
        if key and key in re.sub(r"[\s\-]", "", a).lower():
            return True, ""
    return False, f"标题专名 {','.join(tokens[:3])} 未在摘要复现（不同主体）"


def title_abstract_correspondence(title: str, abstract: str) -> tuple[bool, str]:
    """拒多主题拼接串 / 无共同主题 / 专名换主体。"""
    t, a = title or "", abstract or ""
    tf, af = _families(t), _families(a)
    other = af - tf
    shared = _bigrams(t) & _bigrams(a)
# 多主题拼接串判据：≥2 个新主题族且零共享实词才拒。
    if len(other) >= 2 and not shared:
        return False, f"摘要是多主题拼接串（混入 {','.join(sorted(other))}）"
    if not (tf & af) and not shared:
        return False, "标题与摘要无共同主题族、无共同实词（讲的不是一件事）"
    ok, why = _named_ok(t, a)
    if not ok:
        return False, why
    if tf and not (tf & af) and len(other) >= 1:
        return False, f"摘要主题 {','.join(sorted(other))} 与标题主题 {','.join(sorted(tf))} 不符"
    return True, ""


def public_interest(title: str, abstract: str, sources: str = "") -> tuple[bool, str, str]:
    """大众正向准入：政务/行业/公告不进主表；候选池不删数据。"""
    t = title or ""
    blob = t + (abstract or "")
    hard = ([w for w in _STRONG_EXCLUDE if w in blob]
            + [w for w in _TITLE_EXCLUDE if w in t])
    fams = _families(blob)
    if hard:
        return False, "", f"非面向大众：{'/'.join(hard[:3])}"
    # 政务主体即使蹭到「商圈」等大众词，无具体民生事实仍按机构宣传稿剔除。
    src_block = [s for s in _SOURCE_BLOCK if s in (sources or "")]
    if src_block and not _FACT_RE.search(blob):
        return False, "", f"地方政务机构宣传（{'/'.join(src_block[:2])}）"
    if not fams:
        return False, "", "未命中大众贴近主题（行业/机构/存档类不进每日要闻）"
    # 「公告/通知/征求意见」类若无具体民生事实（金额/人数/气温/比分/时刻）， 仍属机构动态，不因沾了「医保/公积金」字样就混进来。
    if any(w in t for w in ("公告", "通知", "通报", "意见", "答复", "函")) \
            and not _FACT_RE.search(blob):
        return False, "", "机构公文无具体民生事实"
    return True, "、".join(sorted(fams)), ""


def is_aggregate(name: str) -> bool:
    n = (name or "").strip()
    return (not n) or bool(_AGG_RE.search(n)) or any(a in n for a in AGGREGATE)


def real_platform_hits(sources: str) -> list[str]:
    """只认 9 个主干平台精确命中；聚合名/清单编号不计票。"""
    out: list[str] = []
    for p in (sources or "").split(" · "):
        p = p.strip()
        if not p or is_aggregate(p):
            continue
        c = canon(p)
        if c in MAIN_PLATFORMS and c not in out:
            out.append(c)
    return out


def clean_sources(sources: str, hits: list[str]) -> str:
    """来源列 = 真实媒体 + 真实平台；去聚合名堆砌，不改事实。"""
    media: list[str] = []
    for p in (sources or "").split(" · "):
        p = p.strip()
        if not p or is_aggregate(p) or _AGG_RE.search(p):
            continue
        c = canon(p)
        if c in MAIN_PLATFORMS or not c:
            continue
        if c not in media:
            media.append(c)
    ordered = media + [h for h in hits if h not in media]
    if ordered:
        return normalize_source(" · ".join(ordered))
    # 全聚合名绝不退回原串（「…（清单§28）」就是这么残留的）。
    return ""


def augment_platform_hits(items: list[Item], topics: list[dict]) -> dict[str, list[str]]:
    """平台词条真实回查才计票；共享实词命中留痕，查不到申报缺口。"""
    log: dict[str, list[str]] = {p: [] for p in MAIN_PLATFORMS}
    for tp in topics or []:
        name = str(tp.get("title") or "").strip()
        plat = canon(str(tp.get("discovery") or tp.get("source") or "").strip())
        if plat not in MAIN_PLATFORMS or not name:
            continue
        bg = _bigrams(name)
        if len(bg) < 2:
            continue
        # 共享实词最多、已挂平台最少、分数最高的候选优先；单条已达上限则 不再挂，该平台本批记为缺口，绝不把同一热词堆成「9 渠道合并」。
        cands = []
        for idx, it in enumerate(items):
            load = len(real_platform_hits(it.sources))
            if load >= MAX_PLATFORMS_PER_ITEM:
                continue
            inter = len(bg & _bigrams(it.title))
            if inter >= 2:
                cands.append((inter, -load, -float(it.final_adj or 0), -idx, it))
        best = max(cands, key=lambda x: x[:4])[4] if cands else None
        if best is None:
            for it in items:
                if len(real_platform_hits(it.sources)) >= MAX_PLATFORMS_PER_ITEM:
                    continue
                if any(len(w) >= 4 and w in it.title for w in
                       re.findall(r"[\u4e00-\u9fff]{4,}", name)):
                    best = it
                    break
        if best is None:
            continue
        parts = [x.strip() for x in best.sources.split(" · ") if x.strip()]
        if plat not in {canon(x) for x in parts}:
            best.sources = normalize_source(" · ".join([plat] + parts))
        existing = getattr(best, "platform_hits", None) or []
        if plat not in existing:
            existing.append(plat)
        best.platform_hits = existing
        log[plat].append(f"{name[:24]} → {best.title[:24]}")
    return log


def _rej(it: Item, why: str) -> dict:
    return {"标题": it.title[:60], "判定": "剔除", "原因": why,
            "来源": it.sources, "发布时间": it.published}


def _bridge_missing_platforms(items: list[Item], topics: list[dict],
                              log: dict[str, list[str]]) -> None:
    """缺平台：共享实词分散桥接，零词条如实留缺口。"""
    done = {p for it in items for p in real_platform_hits(it.sources)}
    load = {it.title: len(real_platform_hits(it.sources)) for it in items}
    for plat in MAIN_PLATFORMS:
        if plat in done:
            continue
        names = [str(tp.get("title") or "") for tp in topics
                 if canon(str(tp.get("discovery") or tp.get("source") or "")) == plat]
        best, best_w, best_sc = None, "", 0.0
        for name in names:
            bg = _bigrams(name)
            for it in items:
                if load.get(it.title, 0) >= MAX_PLATFORMS_PER_ITEM:
                    continue
                sh = len(bg & _bigrams(it.title))
                if not sh:
                    continue
                sc = sh * 10 - load.get(it.title, 0)
                if sc > best_sc:
                    best, best_w, best_sc = it, "／".join(sorted(bg & _bigrams(it.title)))[:18], sc
        if best is None:
            continue
        parts = [x.strip() for x in best.sources.split(" · ") if x.strip()]
        if plat not in {canon(x) for x in parts}:
            best.sources = normalize_source(" · ".join([plat] + parts))
            load[best.title] = load.get(best.title, 0) + 1
            log[plat].append(f"议题桥接：共享实词「{best_w}」→ {best.title[:20]}"
                             "（同一议题不同侧面，非事件同一）")

def _dedup_events(items: list[Item]) -> list[Item]:
    """同一事件只留代表稿，按 Final_adj 取最高。"""
    out: list[Item] = []
    for it in sorted(items, key=lambda x: -x.final_adj):
        b = _bigrams(it.title)
        dup = False
        for k in out:
            kb = _bigrams(k.title)
            if len(b & kb) >= 3 and len(b & kb) / max(1, min(len(b), len(kb))) >= 0.45:
                dup = True
                break
        if not dup:
            out.append(it)
    return out


def select_daily(items: list[Item], topics: list[dict] | None = None,
                 *, cap: int = 50) -> tuple[list[Item], list[dict], dict]:
    """主表唯一出表口：正向选题、硬闸、平台上限、重算分、严格降序。"""
    rejects: list[dict] = []
    kept: list[Item] = []
    for it in items:
        ok, why = title_abstract_correspondence(it.title, it.abstract)
        if not ok:
            rejects.append(_rej(it, f"标题—摘要不对应：{why}"))
            continue
        pub, fams, why2 = public_interest(it.title, it.abstract, it.sources)
        if not pub:
            rejects.append({"标题": it.title[:60], "判定": "剔除",
                            "原因": why2, "来源": it.sources, "发布时间": it.published})
            continue
        kept.append(it)
        it.daily_hit = fams

    for _i in kept:  # 原始 sources 已带多平台先压到上限，再让词条分散
        _h = real_platform_hits(_i.sources)
        if len(_h) > MAX_PLATFORMS_PER_ITEM:
            _k = set(_h[:MAX_PLATFORMS_PER_ITEM]); _i.sources = normalize_source(" · ".join(
                p.strip() for p in _i.sources.split(" · ") if p.strip() and (
                    canon(p.strip()) not in MAIN_PLATFORMS or canon(p.strip()) in _k))) or _i.sources
    platform_log = augment_platform_hits(kept, topics or [])

    for it in kept:
        hits = real_platform_hits(it.sources)
        it.platform_hits = hits
        media = [p for p in (it.sources or "").split(" · ")
                 if p.strip() and not is_aggregate(p.strip())
                 and canon(p.strip()) not in MAIN_PLATFORMS]
        media = list(OrderedDict.fromkeys(canon(p.strip()) for p in media))
        # impact 只认真实命中平台数 + 独立媒体数，且封顶；不再数字符串段数。
        imp = min(10, 2 * min(len(hits), 5) + min(len(media), 3))
        # rules.score_all 的六维 V 在 live 会到 55–65，直接继承会把 Final 顶到 100、全表 S、完全失去区分度。编辑层用独立可解释分量重算。
        I = max(1, min(5, int(it.I or 5)))
        C = max(1, min(6, int(it.C or 6)))
        Vn = min(18.0, float(it.V or 10.0) / 4.0)
        fams_n = len(str(it.daily_hit or "").split("、")) if it.daily_hit else 0
        mass = min(6, 2 * fams_n)
        it.final = round(min(100, 34 + 6 * I + 2 * C + Vn + imp + mass), 1)
        gin, cp = int(it.gin or 1), int(it.cap or 4)
        it.final_adj = round(it.final - 6 * min(gin - 1, cp), 1)
        f = it.final
        g = "S" if f >= 85 else "A" if f >= 70 else "B" if f >= 55 else \
            "C" if f >= 40 else "D"
        st = getattr(it, "verify_state", "已核实")
        if st == "部分核实" and g in ("S", "A", "B"):
            g = "C"
        elif st == "未核实" and g in ("S", "A"):
            g = "B"
        it.grade = g
        src = clean_sources(it.sources, hits)
        if not src:
            rejects.append(_rej(it, "来源全为聚合名/清单挂名，无真实媒体或平台"))
            it.sources = ""            # 标记，供下方 filter 真正移出主表
            continue
        it.sources = src

    kept = [it for it in kept if it.sources]
    kept = _dedup_events(kept)
    # 按 Final_adj 严格降序，分数序 = 展示序。
    kept.sort(key=lambda x: (-x.final_adj, -x.final, x.title))
    kept = kept[:cap]
    _bridge_missing_platforms(kept, topics or [], platform_log)
    for i, it in enumerate(kept, 1):
        it.seq = i
        it.score_order = i
        it.event_key = it.event_key or f"e{i:04d}"
        media_n = len([p for p in it.sources.split(" · ")
                       if p.strip() and canon(p.strip()) not in MAIN_PLATFORMS])
        it.record_type = "多渠道合并" if media_n >= 2 else "独立原文"

    stats = {
        "候选进入编辑层": len(items),
        "标题摘要不对应剔除": sum(1 for r in rejects if "不对应" in r["原因"]),
        "非面向大众剔除": sum(1 for r in rejects if "不对应" not in r["原因"]),
        "主表成稿": len(kept),
        "9 平台真实命中": {p: len(v) for p, v in platform_log.items()},
        "主表来源列出现平台": sorted({p for it in kept
                                  for p in real_platform_hits(it.sources)}),
        "声明": "只收大众贴近且标题—摘要对应的独立原文；聚合名不计票；按 Final_adj 降序",
    }
    return kept, rejects, stats
