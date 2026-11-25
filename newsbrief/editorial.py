"""面向大众「每日要闻」编辑层：对应硬闸 + 正向选题 + 9 平台真实回查 + 严格降序。"""
from __future__ import annotations

import re
from collections import OrderedDict

from .clean import canon, normalize_source
from .model import Item

# 9 个主干平台：精确名，不被上位词吞掉
MAIN_PLATFORMS = ("微博", "百度", "抖音", "今日头条", "知乎",
                  "小红书", "快手", "贴吧", "微信搜一搜")

# 平台名只在有词条回查证据（共享实词 >=2）时挂；不设上限、不桥接、不凑数。
MIN_SHARED_BIGRAMS = 2

# 政务发布主体：机构宣传稿不进每日要闻；§27 的「XX 发布」是线索台账。
_SOURCE_BLOCK = ("人民政府", "公安局", "公安分局", "派出所", "政务发布",
                 "管委会", "街道办", "区委", "市委", "文明办")

# 聚合页/早报/榜单镜像名：可以出现在来源里当「发现线索」，但不计平台票， 更不参与 impact 评分（此前就是它们把一行堆到 10 段刷成 S）。
AGGREGATE = tuple("夸克/搜狗热点|腾讯新闻早报|网易|搜狐|新浪早报|今日热榜聚合|澎湃热榜|微信24h热文榜|品玩|查榜网|早报|热榜聚合|热点聚合".split("|"))
_AGG_RE = re.compile(r"清单\s*§|清单§|\(清单|（清单")

# 大众贴近主题族：正向准入；标题命中某族时摘要须同族或共享专名。
FAMILIES = {
    "天气出行": tuple(
        "冷空气 降温 升温 雨雪 暴雨 大风 台风 高温 寒潮 气温 ℃ 气象 预警 高速 航班 地铁 公交 铁路 车票 限行 出行 堵 车 回暖 晴朗 "
        "降雨 最低气温 最高气温 大部 "
    .strip().split()),
    "医保社保": tuple(
        "医保 社保 养老金 公积金 药品 集采 医院 挂号 就诊 看病 疫苗 流感 传染病 报销 门诊 住院 呼吸道 发烧 咳嗽 就医 儿科 "
        "患儿 感冒 冬季 养老 "
    .strip().split()),
    "物价消费": tuple(
        "物价 涨价 降价 菜价 油价 补贴 退税 个税 工资 退款 快递 外卖 水电燃气 供暖 暖气 房贷 房租 "
        "退票 旅居 医美 纹眉 美容 术中加价 逼单 套路 维权 加价 "
    .strip().split()),
    "食品安全": tuple("食品 抽检 不合格 召回 中毒 过期 添加剂 餐厅 外卖 厨房 菌落".split()),
    "诈骗与公共安全": tuple("诈骗 反诈 被骗 套路 警方 立案 判决 火灾 爆炸 事故 伤亡 遇难 死亡 失联 坠亡 救援 塌方 洪水 地震 应急响应 疏散 被查 通报".split()),
    "教育就业": tuple("高考 中考 开学 学费 幼儿园 托育 招聘 就业 失业 助学金 奖助学金 考研 校招 寒暑假 课时 学生 校园".split()),
    "住房与城市": tuple("房价 租房 落户 户籍 老旧小区 电梯 物业 装修 保障房 棚改 断供".split()),
    "文娱赛事": tuple("世界杯 国足 联赛 夺冠 比赛 票房 电影 演唱会 赛事 球员 决赛 开赛".split()),
    "数码与消费电子": tuple("手机 折叠 旗舰 华为 Mate 苹果 iPhone 小米 英伟达 黄仁勋 降价 召回 断网 停机".split()),
    "航天与全国热点": tuple("神舟 飞船 发射 乘组 航天员 空间站 探测器 卫星".split()),
    "生活服务": tuple("青年驿站 快递 外卖 供暖 暖气 水电燃气 车票 退票 限行 机票 菜市场 早市 早餐 体检 挂号 通勤 班车 停水 停电 停运 退费 退款 办证".split()),
    "国际重大": tuple("空袭 遇袭 加沙 联合国 撤侨 中国公民 伤亡 地震 坠机 沉船".split()),
}

_STRONG_EXCLUDE = tuple("习近平 总书记 人民情怀 学习贯彻 全会精神 亚太经合组织 部际联席会议 大国外交 焦点访谈 礼赞 绘就时代华章 征文 优秀文章 评选揭晓 研讨班".split())
_TITLE_EXCLUDE = tuple("纪委 监委 征求意见 品种名单 工作会议在 公告 通知 全链条全周期全流程 服务保障 提供治理观察 基层治理 提供全链条".split())
# 机构文体：无具体民生事实（金额/伤亡/气温/价格/时刻）不进主表。 「面向大众每日要闻」负向清单：行业简报、政务会议、产业观察、人物特写、 规划公示、评论解读——命中即拒。用户上传批次 50 条里 22 条属这类， 这是「有效数据少」的主因，不是渠道没采到。
_DAILY_REJECT = tuple("党组 党性 中心组 理论学习 党日 座谈 调度会 实战演练 工作组 培训会 工作会 推进会 论坛 峰会 大会开幕 开幕 致辞 出席 技能大赛 大赛 颁奖仪式 倒计时 回顾 现场观众 花絮 60秒 以点串链 生态警务 机制建设 市场稳健运行 首席风险官 公示 评审结果 通车 走访 纪实 热评 评论指 观察 独家对话 释放什么信号 全链条 高质量 下沉听民情 交旅融合 水运新通道 文旅新探 走红的 藏在城市烟火 烟火里 老健会 银发体育 一坛水酒 专场活动 会议在京 工作组会议 保障市场 TOD 重塑城市形态 数字底座 项目建设 成果入选 优秀案例 净收入 财报 同比增长 演练 培训 减贫实践 获评 最佳案例 含视频 保障措施 配额 专家共识 安全培训 消防安全 所开展 一季度 三季度 利润暴跌 服务保障 治理观察 全链条全周期全流程 提供全链条".split())
_SPLICE_MARK = tuple("都视频 客户端 全过程回顾 独家对话 美股收盘 GPT- 网红打卡 龚健雅 何小鹏 智源 龚健雅 实证驱动 亿元布局 破发".split())
def _hit(words, s): return next((w for w in words if w in s), None)
_TITLE_DIRTY = re.compile(r"^[-—（(]|\.\.\.|<truncated>|^20\d\d-\d\d")
_TITLE_DATE = re.compile(r"(20\d\d)年(\d{1,2})月(\d{1,2})日")
_FACT_RE = re.compile(r"\d+\s*(?:元|亿元|万元|人|死|亡|℃|度|%|级|条|起|比|:\d|时|分)")

# 品牌/产品专名须在摘要复现，否则判为标题讲 A、摘要讲 B 的拼接。
_LATIN_BRAND = re.compile(r"[A-Za-z]{2,}[\s\-]?[A-Za-z0-9]*")
_BRAND_WORDS = tuple("华为 苹果 小米 英伟达 黄仁勋 荣耀 OPPO vivo 特斯拉 比亚迪".split())

_STOP_BIGRAM = set("影响 进行 发布 表示 有关 目前 今天 记者 报道 相关 工作 问题 已经 没有 一个 这些 一些 以及 同时 其中 通过 要求 开展 实施 部门 单位 地区 部分 当地 近日 日前 今年 以来".split())

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
    """拒标题脏串 / 拼接串 / 主题漂移 / 专名换主体。"""
    t, a = title or "", abstract or ""
    if _TITLE_DIRTY.search(t):
        return False, "标题含前导符号/日期戳/截断残留（栏目页标题拼接）"
    if len(t) >= 16 and t[:8] and t.count(t[:8]) >= 2:
        return False, "标题自我重复（同一串出现两次）"
    marks = {w for w in _SPLICE_MARK if w in a}
    if len(marks) >= 2:
        return False, f"摘要是站点壳/早报拼接串（{'/'.join(sorted(marks)[:3])}）"
    tf, af = _families(t), _families(a)
    other = af - tf
    tb = _bigrams(t)
    shared = tb & _bigrams(a)
    clauses = [c.strip() for c in re.split(r"[。！？\n]+", a) if len(c.strip()) >= 10]
    foreign = [c for c in clauses if not (tb & _bigrams(c))]
    if len(foreign) >= 2:
        return False, f"摘要混入 {len(foreign)} 个异主题分句（多稿拼接）"
    if not shared:
        return False, "标题与摘要无任何共享实词（讲的不是一件事）"
    if foreign and any(u in c for c in foreign
                       for u in ("赶紧", "别急着", "快看", "小心", "注意！")):
        return False, "摘要含异主题祈使句（换了另一篇稿的导语）"
    ok, why = _named_ok(t, a)
    if not ok:
        return False, why
    if tf and not (tf & af) and len(other) >= 1:
        return False, f"摘要主题 {','.join(sorted(other))} 与标题主题 {','.join(sorted(tf))} 不符"
    return True, ""


def public_interest(title: str, abstract: str, sources: str = "") -> tuple[bool, str, str]:
    """大众正向准入：行业简报/政务会议/人物特写不进主表。"""
    t = title or ""
    blob = t + (abstract or "")
    hard = ([w for w in _STRONG_EXCLUDE if w in blob]
            + [w for w in _TITLE_EXCLUDE if w in t])
    fams = _families(blob)
    if hard:
        return False, "", f"非面向大众：{'/'.join(hard[:3])}"
    m = _hit(_DAILY_REJECT, t) or _hit(_DAILY_REJECT, str(abstract or "")[:80])
    if m:
        return False, "", f"行业简报/政务会议/人物特写：{m}"
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


def pool_filter(items: list[Item], window: tuple | None = None) -> tuple[list[Item], list[dict]]:
    """候选池只留形态合规、非政务宣传的待选要闻；被剔的逐条留痕。"""
    from datetime import datetime
    out, edge = [], []
    for it in items:
        ok, why = title_abstract_correspondence(it.title, it.abstract)
        reason = f"形态不合格：{why}" if not ok else ""
        if ok:
            blob = it.title + (it.abstract or "")
            m = (_hit(_DAILY_REJECT, it.title) or
                 _hit(("习近平", "总书记", "人民情怀", "学习贯彻", "全会精神",
                       "党组", "党性", "中心组", "理论学习", "党日", "座谈",
                       "研讨", "APEC", "亚太经合组织"), blob))
            if m:
                reason = f"政务宣传/机构动态：{m}"
        if not reason and window:
            m = _TITLE_DATE.search(it.title)
            if m:
                d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if not (window[0].date() <= d.date() <= window[1].date()):
                    reason = f"标题日期 {d.date()} 不在窗口内"
        if reason:
            edge.append({"标题": it.title[:60], "依据": reason, "来源": it.sources})
            continue
        it.sources = normalize_source(it.sources)
        out.append(it)
    return out, edge


def is_aggregate(name: str) -> bool:
    n = (name or "").strip()
    return (not n) or bool(_AGG_RE.search(n)) or any(a in n for a in AGGREGATE)


def real_platform_hits(sources: str) -> list[str]:
    """只认 9 个主干平台精确命中。"""
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
    """来源列 = 真实媒体 + 有证据的平台。"""
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
    return ""


def augment_platform_hits(items: list[Item], topics: list[dict]) -> dict[str, list[str]]:
    """平台词条真实回查才计票；不设上限、不桥接、不凑数。"""
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
            if load >= MIN_SHARED_BIGRAMS:
                continue
            inter = len(bg & _bigrams(it.title))
            if inter >= MIN_SHARED_BIGRAMS:
                cands.append((inter, -float(it.final_adj or 0), -idx, it))
        best = max(cands, key=lambda x: x[:3])[3] if cands else None
        if best is None:
            for it in items:
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


def _dedup_events(items: list[Item]) -> list[Item]:
    """同一事件只留代表稿。"""
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
                 *, cap: int = 50, window: tuple | None = None
                 ) -> tuple[list[Item], list[dict], dict]:
    """主表唯一出表口：正向选题、证据制来源、严格降序。"""
    rejects: list[dict] = []
    kept: list[Item] = []
    for it in items:
        if window:
            md = _TITLE_DATE.search(it.title)
            if md:
                from datetime import datetime
                d = datetime(int(md.group(1)), int(md.group(2)), int(md.group(3)))
                if not (window[0].date() <= d.date() <= window[1].date()):
                    rejects.append(_rej(it, f"标题日期 {d.date()} 不在窗口内"))
                    continue
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
    kept.sort(key=lambda x: (-x.final_adj, -x.final, x.title))
    kept = kept[:cap]
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
        "声明": "只收大众贴近且标题—摘要对应的独立原文；平台名须有词条证据",
    }
    return kept, rejects, stats
