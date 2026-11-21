"""评级模型（SKILL §4.2–§4.7）：六维价值分 V（T/I/A/C/M/V）+ H 通道 → 信息功能档位
→ Final（含 §4.6 质量惩罚）→ 等级 → gin/cap → Final_adj。

诚实声明（写进口径表）：Final 是合成判断分而非实测值；H 是传播足迹代理变量而非真实浏览量；
W_H=0.262 为审慎先验、非因果估计；无平台后台数据时 H 通道关闭，Final 仅反映价值分 V。
"""
from __future__ import annotations

import math
import re
import statistics
from collections import defaultdict
from typing import Dict, List

from .model import RawItem
from .rules import func_tier, SUBSTANTIVE_NUM

V_MAX = 82.32
W_H = 0.262
W_V = 0.738

AFFECTED = ["中国", "国内", "全国", "车主", "旅客", "消费者", "患者", "学生", "市民",
            "投资者", "市场", "居民", "劳动者", "职工", "老年人", "儿童", "家长", "司机",
            "员工", "公众", "群众", "网民", "网友", "乘客", "用户", "企业", "公司", "机构",
            "毕业生", "购房者", "骑手", "农民", "家庭", "参保", "缴存", "用电", "供暖",
            "车主", "商家", "供应商", "债权人", "股民", "观众", "读者", "乘客", "一线",
            "各地", "多地", "沿线", "流域", "大部", "中小学", "幼儿园", "农户", "学校"]
EMOTION = ["震惊", "泪目", "暖心", "刷屏", "炸了", "怒", "心疼"]


def compute_T(items: List[RawItem], win) -> Dict[str, str]:
    """T = 25 × 当日清单内「发布时间距采集窗口中点的小时差」百分位（最近→25，最旧→0）。

    SKILL §4.2 / §4.5 明确基准为「采集窗口中点」（不是窗口右界 win.end）。取有符号
    小时差 mid − pub：值越小表示越新，按「最近→25，最旧→0」给分。由于 end−pub 与
    mid−pub 只差常数「半窗口长度」，百分位次序完全相同，改基准不改变任何 T 值，
    仅把口径表述对齐文档。pub 缺失取批内已知时刻年龄中位数，不按当前时刻算 age=0。
    """
    mid = win.start + (win.end - win.start) / 2
    ages: Dict[int, float] = {}
    for it in items:
        if it.pub:
            ages[it.seq] = (mid - it.pub).total_seconds() / 3600
    median = statistics.median(ages.values()) if ages else 0.0
    eff = {it.seq: ages.get(it.seq, median) for it in items}
    vals = list(eff.values())
    trace = {}
    for it in items:
        # 年龄小（贴近窗口中点）→ 百分位高 → 时效分高
        less = sum(1 for x in vals if x < eff[it.seq])
        pct = 1.0 - (less / (len(vals) - 1)) if len(vals) > 1 else 0.5
        it.T = round(25 * pct, 2)
        trace[it.seq] = ("实证" if it.pub else f"时点推定（取批内已知时刻年龄中位数 {median:.2f}h）")
    return trace


ACTION_WITH_DEADLINE = ["预警", "应急响应", "管制", "封闭", "停运", "停课", "撤离", "疏散",
                        "调价", "每升降", "纳入目录", "日起施行", "日起生效", "申报",
                        "债权申报", "异议", "截止", "前提交", "前申报", "召回"]
HINT_WORDS = ["提示", "警惕", "防范", "注意", "建议", "提醒", "请", "非必要不", "核实",
              "可通过", "可办", "可查", "拨打", "避开", "减速", "谨慎驾驶", "查询", "缴费",
              "关注", "留意", "办理", "申请", "提交", "领取"]


def _A(it: RawItem) -> float:
    """公共可行动性：明确对象+步骤+条件+时限 11–15；提示类 6–10；无行动 0–5。"""
    t = it.text()
    timed = bool(re.search(r"\d{1,2}月\d{1,2}日|\d{1,2}日\d{1,2}时|截止|自20\d{2}年|\d{1,2}时起", t))
    n_deadline = sum(1 for w in ACTION_WITH_DEADLINE if w in t)
    n_hint = sum(1 for w in HINT_WORDS if w in t)
    if n_deadline >= 1 and (timed or n_deadline >= 2):
        return 13.0 if n_deadline >= 2 else 11.0
    if n_hint >= 2:
        return 10.0
    if n_hint == 1:
        return 7.0
    return 3.0


def _net_abstract(it: RawItem) -> str:
    """摘要净字数口径（用户补充 ≤100 字）：去掉缩进 / 空白 / 换行。"""
    return re.sub(r"[\u3000\s]", "", it.abstract or "")


def _M(it: RawItem) -> float:
    """内容适配 0–5：时间地点可定位 / 标题正文一致 / 结构清晰 / 适配信息流 / 无情绪化。

    摘要精简到 ≤100 字后，「结构清晰」不再以 >80 字 / 含 LF 判定（否则全批系统性少 1 分），
    改看「有效摘要 + 净字数 ≥30 + 整句收束」。口径表「摘要精简的连带校准」逐条声明。
    """
    t = it.text()
    net = _net_abstract(it)
    v = 0.0
    if re.search(r"\d{1,2}月\d{1,2}日|当地时间|\d{4}年|\d{1,2}日\d{1,2}时", t):
        v += 1                                   # 可定位时间地点
    if it.title and it.abstract and it.title[:6] in it.abstract:
        v += 1                                   # 标题正文一致
    if (getattr(it, "abstract_status", "") in ("有效", "正文补采")
            and len(net) >= 30
            and it.abstract.rstrip().endswith(("。", "！", "？", "；"))):
        v += 1                                   # 结构清晰（完整整句，不按长度 / 段数）
    if SUBSTANTIVE_NUM.search(t):
        v += 1                                   # 适配信息流（有锚点数字）
    if not any(w in t for w in EMOTION):
        v += 1                                   # 情绪 / 名人只进这里
    return min(5.0, v)


def _verifiable(it: RawItem) -> float:
    return {"A": 5.0, "B": 4.0, "C": 2.0}.get(it.evidence, 0.0)


def _penalty(it: RawItem) -> tuple[float, List[str]]:
    """01 第四节 / 02 §4.6 的五项质量惩罚，全部为可核对词组 / 可复算规则。

    旧版只实现了 3 项（−5 / −4 / −3）；本次补齐「旧闻冒充新事 −8」与
    「标题与正文失配 −5」，两项都用保守条件，避免误伤正常新闻。
    """
    from . import oss
    p, why = 0.0, []
    t = it.text()
    net = _net_abstract(it)
    # 摘要已精简到 ≤100 字，判「无新事实」改看净字数 <20 且无任何可核对事实信号，
    # 不能再用「<30 字」这种为 240 字摘要设的阈值，否则正常精摘要被系统性罚 −4。
    substance = re.search(r"\d|％|%|万|亿|元|人|起|次|发布|宣布|发生|造成|导致|"
                          r"下调|上调|停产|裁员|破产|获批|预警|通报|回应|同比|环比", net)
    if it.evidence == "C" or not it.has_event:
        p += 5
        why.append("来源仅自媒体且无独立佐证 −5")
    if re.search(r"死亡|伤亡|事故|着火|坠机|政策|预警", t) and not it.pub:
        p += 3
        why.append("涉伤亡 / 事故 / 政策却无更新时间 −3")
    if len(net) < 20 and not substance and not any(w in t for w in AFFECTED):
        p += 4
        why.append("只有情绪或立场、无新事实 −4")
    # 旧闻冒充新事 / 旧影像无标注：只在没有窗口内发布时间证据、且出现往期年份线索
    # 或旧影像标记时触发；标题里的月日仍只作日期线索，不据此反推发布时间。
    from datetime import datetime as _dt
    old_year = next((y for y in re.findall(r"(20\d{2})\s*年", it.date_clue or "")
                     if int(y) < _dt.now().year), None)
    old_marks = [w for w in ("旧影像", "资料图", "往年", "回顾", "去年", "往期") if w in t]
    if it.pub is None and (old_year or old_marks):
        p += 8
        why.append(f"旧闻冒充新事 / 旧影像无标注 −8（线索：{old_year or '、'.join(old_marks)}）")
    # 标题与正文主体失配。摘要精简到 ≤100 字后 bigram 交集天然变小，
    # 故收紧为「交集为 0 + 标题任意 2 字片段都不在摘要 + 标题前 6 字未重现」才罚，
    # 避免把正常的精简导语误判成失配（口径表「摘要精简的连带校准」已声明）。
    tb = oss.bigrams((it.title or "")[:14])
    ab = oss.bigrams(net[:100])
    frags = [it.title[i:i + 2] for i in range(0, min(12, max(0, len(it.title or "") - 1)))]
    any_frag = any(f and f in net for f in frags)
    if (len(net) >= 30 and tb and len(tb & ab) == 0 and not any_frag
            and (it.title or "")[:6] not in net):
        p += 5
        why.append("标题与正文主体失配 −5")
    return p, why


def grade_of(final: float) -> str:
    if final >= 85:
        return "S"
    if final >= 70:
        return "A"
    if final >= 55:
        return "B"
    if final >= 40:
        return "C"
    return "D"


def compute_scores(items: List[RawItem], win, cfg: dict) -> dict:
    scoring = cfg.get("scoring", {})
    lam = float(scoring.get("lambda_", 6))
    t_trace = compute_T(items, win)
    verify_capped = 0

    # H 通道诚实降级判定
    with_backend = sum(1 for it in items if it.platform_metrics)
    h_mode = scoring.get("h_channel", "auto")
    if h_mode == "off":
        h_enabled = False
    elif h_mode == "on":
        h_enabled = True
    else:
        h_enabled = (len(items) > 0 and with_backend / len(items) > 0.30)

    # ---------------------------------------------------------------- H 代理通路
    # 用户 2026-11-20：「数量多了但一个 A 都没有」。
    # 根因不是阈值高，是 H 通道关闭时「多渠道覆盖」这一路可核对的公开信号
    # 完全不计入 Final（media_slots / board_platforms 只在 h_enabled 分支使用），
    # 而 V_MAX=82.32 是理论满分，实际条目拿不到 → Final 天花板 ~67，A(≥70) 恒空。
    # 解法：h_channel=proxy 时，用「同一事件主键下不同独立来源数」构造 H_c 代理。
    # 这不是编造浏览量，是公开可核对的传播足迹；spec 明确 H 是「传播足迹代理变量」。
    # 只对该事件确实有 ≥3 个独立来源的条目生效，稀疏生效、不整体上移等级。
    from collections import defaultdict as _dd
    from . import quality_v3 as qv3
    _by_eid = _dd(set)
    for _it in items:
        _by_eid[str(getattr(_it, "eid", "") or getattr(_it, "seq", "x"))].add(
            str(getattr(_it, "source", "") or ""))
    _proxy_min = int(scoring.get("h_proxy_min_sources", 3))
    _proxy_cap = float(scoring.get("h_proxy_cap", 5.0))
    use_proxy = (h_mode == "proxy" and not h_enabled)
    use_heat = (h_mode == "heat" and not h_enabled)
    proxy_used = 0

    for it in items:
        base = 5.0 if any(w in it.text() for w in AFFECTED) else 3.0
        tier, d_i, cap, d_c, c_name = func_tier(it)
        it.func_tier, it.func_delta_i, it.func_delta_c, it.cap = tier, d_i, d_c, cap
        it.I = min(20.0, base + d_i)
        it.A = _A(it)
        it.C = min(10.0, 1 + d_c)
        it.M = _M(it)
        it.Vv = _verifiable(it)
        raw6 = (0.24 * it.T + 0.18 * it.I + 0.14 * it.A + 0.10 * it.C
                + 0.06 * it.M + 0.10 * it.Vv) / 0.82
        it.V = round(raw6 * 5, 4)

        platforms = len(set(it.board_platforms))
        board_slots = max(it.board_slots, platforms)
        media_slots = it.media_slots
        _nsrc = len(_by_eid.get(str(getattr(it, "eid", "") or getattr(it, "seq", "x")), ()))
        if h_enabled:
            P = min(20, 5 * platforms + 1 * max(0, board_slots - platforms)
                    + 3 * min(media_slots, 2) + (1 if media_slots > 0 else 0))
            it.P, it.H = P, P * 5
            it.H_c = math.sqrt(it.H / 100)
            it.Final = 100 * (W_H * it.H_c + W_V * it.V / V_MAX)
        elif use_heat and getattr(it, "event_H_c", None) is not None:
            # 质量 v3：事件级热度（48h 窗口 / 24h 半衰期 / 独立来源去重 /
            # authority + originality）。由 pipeline 在打分前写回 event_H_c。
            # 不改 grade 阈值、不改 V_MAX、不改 W_H、不读平台后台。
            it.H_c = float(it.event_H_c)
            it.P = it.H = None
            it.Final = min(100.0, it.V / V_MAX * 100 + 100 * W_H * it.H_c)
            it.proxy_h_sources = int(getattr(it, "event_nsrc", 0))
            proxy_used += 1
        elif use_proxy and _nsrc >= _proxy_min:
            # 公开可核对：同一事件下不同独立来源数；不读平台后台、不猜浏览量。
            # 注意：H 关闭分支用的是「价值分基准 V/V_MAX×100」（价值分占满 100% 权重），
            # 而 H 开启分支是 W_H×H_c + W_V×V/V_MAX。若代理直接套后者会乘 0.738，
            # 反而比不开代理更低。这里以同一价值分基准为底，叠加 100×W_H×H_c 的传播增量。
            it.H_c = min(1.0, (_nsrc - 1) / _proxy_cap)
            it.P = it.H = None
            it.Final = min(100.0, it.V / V_MAX * 100 + 100 * W_H * it.H_c)
            it.proxy_h_sources = _nsrc
            proxy_used += 1
        else:
            it.P = it.H = it.H_c = None
            it.Final = it.V / V_MAX * 100

        pen, why = _penalty(it) if scoring.get("quality_penalty", True) else (0.0, [])
        it.quality_penalty = pen
        it.Final = round(max(0.0, it.Final - pen), 2)
        # 三态核实（01 第四节 / 02 §4.6）：封顶作用在 Final 上，等级仍按 Final
        # 机械映射，因此「等级按原始 Final」与「部分核实封顶 C」两条同时成立、逐条可复算。
        #   evidence=A 已核实 → 正常评分；B 部分核实 → 封顶 C（<55）；C 未核实 → 不得进 A（<70）。
        verify_cap = bool(scoring.get("verify_cap", True))
        ceiling = {"A": None, "B": 54.9, "C": 69.9}.get(it.evidence)
        capped = 0
        if verify_cap and ceiling is not None and it.Final > ceiling:
            before = it.Final
            it.Final = ceiling
            capped = 1
            why.append(f"核实态封顶（{it.evidence}）{before:.2f}→{ceiling}")
        verify_capped += capped
        it.grade = grade_of(it.Final)
        it.reject_reason = (it.reject_reason + "；" + "、".join(why)).strip("；") if why else it.reject_reason

    # gin：上位大类内按 Final 降序的排名（按事件主键去重计数）
    by_group: Dict[str, List[RawItem]] = defaultdict(list)
    for it in items:
        by_group[it.group].append(it)
    seen_eid = set()
    for g, group_items in by_group.items():
        ordered = sorted(group_items, key=lambda x: (-x.Final, x.seq))
        rank = 0
        for it in ordered:
            key = it.eid or f"seq{it.seq}"
            if key in seen_eid:
                it.gin = rank          # 同事件共享一个 gin
            else:
                rank += 1
                it.gin = rank
                seen_eid.add(key)
        for it in ordered:
            it.decrement = round(lam * min(max(0, it.gin - 1), it.cap), 2)
            it.Final_adj = round(it.Final - it.decrement, 2)

    ordered_all = sorted(items, key=lambda x: (-x.Final_adj, -x.Final, x.seq))
    for i, it in enumerate(ordered_all, 1):
        it.score_order = i
    return {
        "lambda": lam,
        "h_enabled": h_enabled,
        "h_mode": h_mode,
        "H代理通路": {
            "启用": use_proxy or use_heat,
            "模式": h_mode,
            "规则版本": qv3.HEAT_RULE_VERSION if use_heat else "proxy-min3",
            "生效条数": proxy_used,
            "最小独立来源数": _proxy_min,
            "口径": ("H 通道因缺平台后台数据关闭时，用同一事件主键下不同独立来源数构造 H_c 代理"
                     "（min(1,(n-1)/cap)）。Final = 价值分基准 V/V_MAX×100 + 100×W_H×H_c"
                     "（W_H=0.262 不变；H 关闭时价值分已占满权重，故用叠加而非乘性合成，"
                     "否则代理反而降分）。不读平台后台、不猜浏览量、不改 grade 阈值、"
                     "不调 V_MAX；仅 n≥阈值 的条目生效，逐条可复算。"),
        },
        "with_backend": with_backend,
        "total": len(items),
        "t_trace": t_trace,
        "grade_dist": {g: sum(1 for it in items if it.grade == g) for g in "SABCD"},
        "质量惩罚项数": 5,
        "核实态封顶条数": verify_capped,
        "核实态三态": {"A": "已核实正常评分", "B": "部分核实封顶 C（<55）", "C": "未核实不得进 A（<70）"},
        "h_closed_reason": "" if h_enabled else "本批无平台后台数据（>70% 条目缺播放/阅读/讨论数），H 通道关闭，Final 仅反映价值分 V",
    }
