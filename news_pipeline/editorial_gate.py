# -*- coding: utf-8 -*-
"""编辑部内容硬闸（2026-11-22 线上 latest.xlsx 逐条复盘后新增）。

背景（用户上传的 2026-11-21 20:28 批次，job=success 但质量崩）：
  质量门禁实测 A级独立原文占比 0.1071（门槛 0.6）、词条回查成功率 0.0（门槛 0.8）、
  非新闻噪声率 0.3253（门槛 0.1），规格验收 55/59、主表 56/56 摘要格式 FAIL；
  但因为 `quality.warn_only=true` 且 `run.py` 的 `--spec-verify` 只打印不阻断，
  CI 照样建站→提交→部署→success。用户要的是「必须达到数据质量效果」，不是「标注一下」。

本模块纯规则、无模型、无网络，对**候选池与主表**同时生效，逐条留痕：
  闸 E1 非新闻形态（SEO/教程/广告/黄页/剧透/视频/栏目页）
  闸 E2 摘要整句（半句 / 坏标点 / 串味 / 净字 / 1–2 段）
  闸 E3 假多渠道合并（来源列堆不相干域名、标题无事件锚点）
  闸 E4 社交词条娱乐化（游戏/影视/追星不进池）
  闸 E5 来源列规范化（`渠道 · 渠道`，裸域名转渠道名，F10/公告标栏目页）

设计约束：正例（德令哈地震 / LPR / 使馆提醒 / 沙特导弹 / 冬季文旅 / 电池回收）
必须全部放行；任何一条误杀都算回归失败，由 scripts/verify_online_quality.py 断言。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

# ---------------------------------------------------------------- 词表
# 非新闻标题形态：教程 / 问答 / 办事指南 / 广告 / 黄页 / 剧透 / 视频页 / 品牌尾巴
NON_NEWS_TITLE = re.compile(
    r"怎么写|如何写|申请书|文书结构|证据清单|仲裁时效|办事指南|"
    r"攻略|全解析|一文读懂|值得买吗|选购|测评|实测体验|使用教程|操作步骤|"
    r"剧透|好结局|路线实录|热门视频|精翻|剧情解析|第几季|开箱|"
    r"合作工业制造平台|合作平台|招商|加盟|供应商|厂家直销|报价单|"
    r"3D打印|CNC加工|钣金|注塑|一站式|解决方案|"
    r"\s[—-]\s*[A-Za-z][A-Za-z0-9.]{2,}\s*$|_\w+热门|_游戏|"
    r"【(?:精翻|攻略|测评|盘点)】|^\s*\|")
# 明确的新闻事实锚点（有其一即认为可能是真新闻，避免误杀）
NEWS_ANCHOR = re.compile(
    r"\d|地震|震源|级|死|伤|遇难|失联|通报|发布|公布|表示|称|提醒|预警|"
    r"拦截|袭击|发射|事故|起火|停产|召回|退市|破产|清算|中标|签约|"
    r"利率|报价|LPR|降价|调整|开通|上线|投用|开工|竣工|查处|判处|"
    r"会议|论坛|开幕|闭幕|赛季|联赛|选举|访问|会谈|使馆|领保")
# 娱乐 / 游戏 / 追星（社交词条硬排除，需求 §5：词条必须回查带事实独立原文）
ENTERTAINMENT = re.compile(
    r"GTA|剧透|游戏热门|热门视频|精翻|剧情|综艺|偶像|追星|明星|CP|"
    r"第几季|番剧|动漫|手游|电竞|皮肤|抽卡|Cosplay|影视飓风|年度佳片")
# 【2026-11-22 全量线上重判修正】原先 NON_ORIGINAL_SOURCE 把 F10 / 东方财富公告
# 也当成「非独立原文」整条删除，实测误杀线上主表 5 条真新闻（油轮遇袭 / AI液冷 /
# 腊肉季 / 储能盈利 / 全球AI产业）。栏目页只应「标注、不参与 A 级计数」，不是删稿理由。
# 真正该删的只有垃圾站 / 百科问答 / 企业黄页 / 视频页：
JUNK_SITE = re.compile(
    r"baike\.baidu|zhidao\.baidu|zhuanlan\.zhihu|zhihu\.com/question|"
    r"meshlaw|91gc\.com|oemao\.com|yungongchang|faxiangongchang|gongchang\.com|"
    r"ali213|crazygames|texttwist|fanbolt|vapepinoy|cabinsusagatlinburg|"
    r"kmap-state|kancare|bilibili\.com/video|douyin\.com/video|"
    r"小红书笔记|贴吧|论坛|博客|个人公众号")
# 栏目页：只标注，不删条目
NON_ORIGINAL_SOURCE = re.compile(
    r"F10|个股资料|行情中心|公告\b|东方财富公告|同花顺")
# 宣传成就 / 拼盘稿（需求 §3「国家机构的稳中向好类成就宣传统统不要」）
PROPAGANDA_TITLE = re.compile(
    r"新思想引领新征程|唱空.{0,6}落空|透过数据|世界首个|蝉联榜首|刷新纪录|"
    r"捷报频传|惠及|位居全国前列|稳中向好|成就宣传|集中发布|打出.组合拳|"
    r"超大市场为全球|何以焕新|高质量发展蓄势")
# 栏目页 / 公告聚合（不参与"独立原文"计数，但不删条目）
COLUMN_SOURCE = re.compile(r"F10|公告\b|公告$|资料页|行情中心|频道首页|栏目页|专题页|索引页")
# 坏标点 / 半句残片（线上第 1 条「(北纬37.14度。（。」、第 18 条「据沙特《」）
BAD_PUNCT = re.compile(r"（。|。）。|；。|。。|、《|\(\s*。|《\s*$|\(\s*$|：\s*$|，\s*$")
SENT_END = ("。", "！", "？", "；")
# 裸域名 → 渠道名（规格验收点名：eqsn.gov.cn / www1.xinhuanet.com / news.cn / Sohu 混用）
DOMAIN_TO_CHANNEL = {
    "eqsn.gov.cn": "中国地震台网", "www1.xinhuanet.com": "新华网",
    "news.cn": "新华网", "www.xinhuanet.com": "新华网", "sohu": "搜狐网",
    "people.com.cn": "人民网", "chinanews.com.cn": "中新网",
    "cctv.com": "央视网", "gmw.cn": "光明网", "ce.cn": "中国经济网",
    "finance.sina.com.cn": "新浪财经", "eastmoney.com": "东方财富",
    "thepaper.cn": "澎湃新闻", "guancha.cn": "观察者网",
    "m.gmw.cn": "光明网", "news.qq.com": "腾讯新闻",
}


def _flat(s: str) -> str:
    return re.sub(r"[\s\u3000]+", "", str(s or ""))


def _net(s: str) -> str:
    return re.sub(r"[\s\u3000]", "", str(s or ""))


def _sents(s: str) -> List[str]:
    return [x.strip() for x in re.split(r"(?<=[。！？；])\s*", str(s or "")) if x.strip()]


def normalize_source(src: str) -> Tuple[str, List[str]]:
    """来源列统一 `渠道 · 渠道`；裸域名转渠道名，栏目页加标注。返回 (新来源, 留痕)。"""
    parts = [p.strip() for p in re.split(r"\s*[·/|｜、]\s*", str(src or "")) if p.strip()]
    out: List[str] = []
    notes: List[str] = []
    for p in parts:
        key = p.lower().replace("https://", "").replace("http://", "").split("/")[0]
        if COLUMN_SOURCE.search(p):
            out.append(p.replace("/", " · "))
            notes.append(f"栏目页不参与独立原文计数：{p[:30]}")
            continue
        mapped = DOMAIN_TO_CHANNEL.get(key) or DOMAIN_TO_CHANNEL.get(key[4:] if key.startswith("www.") else key)
        if mapped:
            out.append(mapped)
            notes.append(f"裸域名映射：{p[:24]} → {mapped}")
        else:
            out.append(p)
    seen: set = set()
    uniq = []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return " · ".join(uniq)[:180], notes


# ---------------------------------------------------------------- 闸 E1/E4
def non_news_verdict(it) -> Tuple[bool, str, str]:
    """返回 (是否保留, 闸, 依据)。"""
    title = str(getattr(it, "title", "") or "")
    src = str(getattr(it, "source", "") or "")
    rtype = str(getattr(it, "record_type", "") or "")
    social = ("社交" in rtype or "热榜" in rtype or "词条" in rtype
              or str(getattr(it, "channel_id", "")).startswith("OSS"))
    t = _flat(title)

    bad = NON_NEWS_TITLE.search(title)
    if bad and not NEWS_ANCHOR.search(t):
        return False, "E1 非新闻形态", f"标题命中「{bad.group(0)}」且无可核对事实锚点，属 SEO/广告/教程页"
    # 广告黄页：即使带数字（3D 打印型号）也不算新闻
    if re.search(r"合作工业制造平台|一站式|厂家直销|招商加盟|解决方案", title):
        return False, "E1 非新闻形态", "企业黄页 / 招商广告，不是资讯事件"
    # 栏目页 / F10 只标注不删；只有垃圾站（黄页 / 百科 / 专栏 / 视频页）才整条剔除
    junk = JUNK_SITE.search(src)
    if junk and not NEWS_ANCHOR.search(t):
        return False, "E1 垃圾站点", f"来源含非新闻站点（{junk.group(0)}）且标题无事实锚点"
    if ENTERTAINMENT.search(title):
        return False, "E4 社交娱乐", f"游戏/影视/剧透内容（{ENTERTAINMENT.search(title).group(0)}），需求 §5 词条须回查带事实独立原文"
    if social and ENTERTAINMENT.search(_flat(title) + _flat(str(getattr(it, "abstract", "") or ""))):
        return False, "E4 社交娱乐", "社交词条成稿含娱乐/游戏内容，不得进池"
    return True, "", "新闻形态与来源合规"


# ---------------------------------------------------------------- 闸 E2
def abstract_verdict(abstract: str) -> Tuple[bool, str, str]:
    """摘要：1–2 段、整句收束、无坏标点、净字 ≤100、无串味残片。"""
    a = str(abstract or "").strip()
    if not a or "摘要未取得" in a:
        return False, "E2 摘要缺失", "占位摘要/未取得，不得计入有效摘要（需求 §5）"
    paras = [p for p in re.split(r"[\r\n]+", a) if p.strip()]
    if len(paras) > 2:
        return False, "E2 段落超限", f"{len(paras)} 段，需求 §7 要求 1–2 段"
    if BAD_PUNCT.search(a):
        m = BAD_PUNCT.search(a)
        return False, "E2 坏标点/半句", f"命中「{m.group(0)}」，摘要被切断或拼接残破（线上地震/沙特条）"
    for p in paras:
        if not p.rstrip().endswith(SENT_END):
            return False, "E2 未整句收束", f"段落结尾「{p.rstrip()[-8:]}」不是整句标点（不得切半句）"
    n = len(_net(a))
    if n > 100:
        return False, "E2 净字超限", f"净字 {n} > 100（需求 §7 硬阈值）"
    # 标题复述 + 单一残句：至少要有 2 个完整句或 1 个含事实的长句
    sents = _sents(a)
    if len(sents) < 1 or (len(sents) == 1 and n < 18 and not re.search(r"\d", a)):
        return False, "E2 无事实内容", f"仅 {len(sents)} 句 / {n} 净字且无数字，无法说清楚事件"
    return True, "", f"摘要合规（{len(paras)} 段 / {n} 净字 / {len(sents)} 整句）"


def polish_abstract_strict(text: str, title: str = "") -> Tuple[str, str]:
    """把线上旧口径（2–4 段 / 240 字）修成需求 §7（1–2 段 / ≤100 净字 / 整句）。

    2026-11-22 全量线上 56+344 条重判发现：旧版遇到 3–4 段就判「E2 段落超限」
    直接删条目，误杀 LPR / 国产大模型 / Token 算力 / 高铁维修车间等真新闻。
    正确行为是**修复而不是删除**：取前 2 段、丢标题复读段、清坏标点与未闭合括号、
    按整句边界收束、总净字 ≤100，全程数字一字不改。
    返回 (修好的摘要, 依据)。确实修不了（「据沙特《」）就返回原串并标 BAD 交主表硬闸。
    """
    a = str(text or "")
    tflat = _flat(title)
    out: List[str] = []
    for para in re.split(r"[\r\n]+", a):
        p = para.strip()
        if not p:
            continue
        # 清孤立坏标点组合（保留句子内容，数字一字不动）
        p = re.sub(r"[（(]\s*。|。\s*[）)]\s*。|；\s*。", "。", p)
        p = re.sub(r"[。；]{2,}", "。", p)
        # 未闭合括号残片（「(北纬37.14度。」）整段丢，不留半截括号
        p = re.sub(r"[（(][^）)]{0,40}?[。；]", "。", p)
        # 截到最后一个合法整句标点；尾部残片（「据沙特《」）丢掉
        last = max(p.rfind(c) for c in SENT_END)
        if last < 0:
            continue
        p = p[:last + 1].strip()
        if len(_net(p)) < 8:
            continue
        # 标题复读段不算正文（线上「当Token走进大众消费。」首段就是标题）
        if tflat and _flat(p) and (_flat(p) in tflat or tflat in _flat(p)) and len(out) == 0:
            continue
        out.append(p)
    # 1–2 段：只取前两段，绝不切半句；总净字 ≤100 时按整句从后往前丢
    out = out[:2]
    while out and len(_net("\n".join(out))) > 100:
        sents = _sents(out[-1])
        if len(sents) > 1:
            out[-1] = "".join(sents[:-1])
        else:
            out.pop()
    res = "\n".join(out)
    if not res:
        return a, "无法整句收束，保留原串并标 BAD（交主表硬闸）"
    return res, ("坏标点清理 + 尾部半句截断 + 多段收束至 1–2 段 / ≤100 净字"
                 "（数字未改写）")


# ---------------------------------------------------------------- 闸 E3
def fake_merge_verdict(it) -> Tuple[bool, str, str]:
    """多渠道合并必须是同一事件的多篇独立报道，不是同一 query 拉回的杂项堆砌。"""
    src = str(getattr(it, "source", "") or "")
    rtype = str(getattr(it, "record_type", "") or "")
    title = str(getattr(it, "title", "") or "")
    domains = [x.strip() for x in re.split(r"\s*[·/|｜]\s*", src) if x.strip()]
    if "多渠道合并" not in rtype and len(domains) < 3:
        return True, "", "非多渠道成稿，不适用"
    # 2026-11-22 全量重判修正：旧版用窄 NEWS_ANCHOR 词表判「无锚点即假合并」，
    # 把「顺丰被暂停全军采购」「特朗普誓言阻止美企海外建数据中心」「日媒核潜艇」
    # 等真新闻也拦了。同一事件判定归 event_composer 的 cannot-link，这里只拦两类：
    #   (a) 来源列混进垃圾站 / 百科 / 专栏页（黄页广告伪装成多渠道）
    #   (b) 标题是宣传成就 / 拼盘稿（需求 §3 明确统统不要）
    junk = [d for d in domains if JUNK_SITE.search(d)]
    if junk and not re.search(r"地震|级|死|伤|袭击|预警|通报|发布|LPR|利率", title):
        return False, "E3 假多渠道合并", (
            f"来源含非新闻站点 {junk[:3]} 且非硬突发，合并证据不成立")
    prop = PROPAGANDA_TITLE.search(title)
    if prop and len(domains) >= 3:
        return False, "E3 宣传拼盘稿", (
            f"标题命中宣传/成就稿「{prop.group(0)}」且堆 {len(domains)} 个来源，"
            "属泛词检索杂项聚簇，非同一事件多家报道（需求 §3）")
    return True, "", f"多渠道来源可信（{len(domains)} 来源；同事件判定由成稿层 cannot-link 负责）"


# ---------------------------------------------------------------- 统一入口
def editorial_check(it, stage: str = "main") -> Tuple[bool, str, str]:
    """stage=candidate（候选池）/ main（主表）。

    需求⑤：候选池「摘要未取得」要**留在池里逐条留痕**，主表硬闸才拦；
    旧版不分阶段，把「摘要未取得 / 多段 / 半句」直接从候选池踢掉，违反口径。
    形态级垃圾（SEO / 黄页 / 娱乐 / 宣传拼盘 / 垃圾站来源）两层都剔。
    """
    ok, gate, why = non_news_verdict(it)
    if not ok:
        return False, gate, why
    ok, gate, why = fake_merge_verdict(it)
    if not ok:
        return False, gate, why
    if stage == "main":
        ok, gate, why = abstract_verdict(str(getattr(it, "abstract", "") or ""))
        if not ok:
            return False, gate, why
    return True, "", ("编辑部硬闸通过（候选池层：形态 / 来源可信；摘要留池留痕）"
                      if stage == "candidate" else
                      "编辑部硬闸通过（主表层：形态 / 摘要整句 / 来源可信 / 非娱乐）")


def editorial_check_many(items: List[Any], stage: str = "candidate") -> Tuple[List[Any], List[dict]]:
    kept, rejected = [], []
    for it in items:
        ok, gate, why = editorial_check(it, stage=stage)
        if ok:
            # 来源列就地规范化，规格验收的「渠道 · 渠道」不再 FAIL
            new_src, notes = normalize_source(str(getattr(it, "source", "") or ""))
            it.source = new_src
            raw = getattr(it, "raw", None)
            if isinstance(raw, dict):
                if notes:
                    raw.setdefault("来源规范化", []).extend(notes)
                if stage == "candidate":
                    # 摘要/格式问题不剔条，只留痕交主表硬闸（需求⑤）
                    aok, agate, awhy = abstract_verdict(str(getattr(it, "abstract", "") or ""))
                    if not aok:
                        raw["候选池摘要留痕"] = f"{agate}：{awhy}"
            kept.append(it)
        else:
            rejected.append({"展示序": getattr(it, "seq", 0),
                             "标题": getattr(it, "title", ""),
                             "来源": getattr(it, "source", ""),
                             "命中闸": gate, "依据": why, "阶段": stage})
    return kept, rejected
