"""候选池契约三闸（2026-11-22 用户三张截图指名修复）。

需求⑤原文是「窗口内全部数据 = 标题/摘要/来源**齐备、分类合规**、未经筛选的候选池」。
「未经筛选」= 不做贴近度严选 / 评级 / 编排，**不等于**字段不齐、分类不合规、
不同事件串味也照收。此前把契约读成「什么都能进池」，产出三类可见错误：

  截图1  A股收盘标题配「临平劳动争议」摘要，来源列混 5 个不相干主体 → 事件过合并
  截图2  「某光伏企业破产清算 + 微信公众号」→ 匿名/不可核对主体混入
  截图3  「政要偶遇/打趣 + 占位摘要」→ 社交话题被当成花边收集箱

行业依据：相似度只做候选召回，合并须由身份决策层授权并留 cannot-link 约束与可拆回证据
[citation:4][citation:6][citation:16]；主体可核对靠稳定标识符（股票代码 / LEI / 已验证域名），
纯名称匹配错误率约 40% [citation:9][citation:13]；热榜的公共价值 ≠ 热度，须有可核验证据与
对普通人权利/安全/钱/出行的影响 [citation:8][citation:12][citation:15]。

本模块纯规则、无模型、无网络。三闸对**候选池**生效（主表是它的子集，自然同步）。
"""
from __future__ import annotations

import re
from typing import List, Tuple

from . import rules as _rules

# ---------------------------------------------------------------- 通用工具
def _flat(s: str) -> str:
    return re.sub(r"[\s\u3000]+", "", str(s or ""))


def _net(s: str) -> str:
    return re.sub(r"[\s\u3000]", "", str(s or ""))


def _text(it) -> str:
    """标题 + 摘要 + 来源；判定用，不写回条目。"""
    return " ".join(str(x or "") for x in
                    (getattr(it, "title", ""), getattr(it, "abstract", ""),
                     getattr(it, "abstract_body", ""), getattr(it, "source", "")))


def _nondate_numbers(s: str) -> set:
    """非日期数字：带小数 / ≥3 位 / 后跟实质单位。11、19、20、2026 一律不算锚点。"""
    out = set()
    for m in re.finditer(r"\d+(?:\.\d+)?", str(s or "")):
        n = m.group(0)
        tail = str(s or "")[m.end():m.end() + 6]
        if "." in n or len(n) >= 3 or re.match(r"(?:%|个百分点|元|亿元|万元|亿|万人|人|死|伤|起|列|家|店|枚|级|公里|吨|台|辆|户|宗|次|名|条|款|只|岁|周岁|平方米|亩)", tail):
            if n not in ("2026", "2025", "2024") or "." in n:
                out.add(n)
    return out


# 同一事件的合法分项 / 背景数字：标题是汇总值、句子讲分项或背景，数字不重合属正常。
# 例：地方债「合计230.228亿元」↔「一般债89.03亿元、专项债141.20亿元」；
#     A股「沪指3577.62点」↔「创业板成交3101.21亿元」；
#     电池「换电池8万元」↔「近2000万辆跨过质保线」。
# 数字不重合只能作辅助证据，**不得单独否决**；硬否决只看事件类别互斥与主体专名零交集。
SEGMENT_HINT = re.compile(
    r"其中|分别|同期|累计|截至|分项|一般债|专项债|收报|成交|另报|并列|"
    r"记\d|记为|报道|将跨|背景|质保|万辆|共计|合计|约|起|达|"
    r"亿元|万元|点|个百分点|%|只|家|列|宗|起|人|死|伤")
CN_NUM_QUANTITY = re.compile(
    r"[一二三四五六七八九十百千万两数几]{1,5}(?:名|人|死|伤|起|宗|家|店|枚|级|"
    r"列|只|户|次|条|款|岁|万元|亿元|公里|吨|台|辆|平方米|亩|余)")


# ---------------------------------------------------------------- 闸0：字段形态合规（需求⑤「齐备」）
# 线上 2026-11-22 批次实测：候选池前 10 行有 5 行是同一短标题重复三次
# （「习近平会见马来西亚总理安瓦尔」×3），来源列拼着「教育部 · 国家统计局 ·
# 中国工程院」这类与该事件毫无关系的机构名。两者都不是「未经筛选该留的内容」，
# 是采集/成稿层产出的非法字段形态，必须在契约层拦下并逐条留痕。
TITLE_ISO_STUB = re.compile(r"\d{4}-\d{1,2}-\d{0,2}\b")
# 非媒体机构：部委 / 统计 / 工程院 / 科学院 / 协会，出现在来源列即需与正文主体核对
NONMEDIA_ORG = re.compile(
    r"教育部|国家统计局|中国工程院|中国科学院|中国社会科学院|工业和信息化部|"
    r"财政部|商务部|国家发展改革委|发展改革委|人力资源社会保障部|人社部|"
    r"住房城乡建设部|住建部|交通运输部|国家卫生健康委|卫健委|农业农村部|"
    r"生态环境部|应急管理部|公安部|司法部|自然资源部|水利部|文化和旅游部|"
    r"退役军人事务部|审计署|海关总署|市场监管总局|税务总局|体育总局|"
    r"统计局|工程院|科学院|协会|研究院|管理中心|事业单位|领导小组办公室")


def title_shape_verdict(title: str) -> Tuple[bool, str]:
    """闸0a：标题必须是单一、完整、不复读的合法标题。返回 (是否合规, 依据)。"""
    t = _flat(title)
    if len(t) < 4:
        return False, "标题过短，不构成合法资讯标题"
    if len(t) > 60:
        return False, f"标题净字 {len(t)}，疑似正文被塞进标题字段（需求⑤字段齐备）"
    if TITLE_ISO_STUB.search(t):
        return False, "标题夹带未闭合的 ISO 日期残串，属拼接残留"
    # 任意 ≥8 字片段在标题里出现两次以上 → 标题复读/拼接（线上三连复读行）
    for i in range(0, max(0, len(t) - 7)):
        if t[i:i + 8] in t[i + 8:]:
            return False, f"标题含重复片段「{t[i:i + 8]}」，属同一短标题被拼接多次"
    return True, "标题形态合规（单一、完整、不复读）"


def source_identity_verdict(it) -> Tuple[bool, str]:
    """闸0b：来源列里的非媒体机构必须与标题/摘要主体相关，否则是乱拼来源。

    正常多源（央视网新闻 · 新华网 · 光明网）不含非媒体机构，不触发；
    「教育部 · 国家统计局 · 中国工程院」配会见稿 → 3 个机构、正文零对应 → 拦。
    「财政部公布…」正文含「财政部」→ 放行。
    """
    src = str(getattr(it, "source", "") or "")
    orgs = sorted(set(NONMEDIA_ORG.findall(src)))
    if len(orgs) < 2:
        return True, "来源列非媒体机构少于 2 个，不适用"
    body = _flat(str(getattr(it, "title", "") or "")) + _flat(
        str(getattr(it, "abstract", "") or ""))
    matched = [o for o in orgs if o in body or (len(o) >= 4 and o[:4] in body)]
    if matched:
        return True, f"来源机构与正文主体对应（{'、'.join(matched[:3])}）"
    return False, (f"来源列含 {len(orgs)} 个非媒体机构（{'、'.join(orgs[:4])}），"
                   f"标题与摘要均无对应主体，属成稿层乱拼来源")


# ---------------------------------------------------------------- 闸1：标题 ↔ 摘要事件身份一致性
# 硬事件类别：两边都明确命中且类别不同 → cannot-link（防截图1那种跨事件合并）
EVENT_TYPE_LEXICON = {
    "证券行情": ["沪指", "创业板指", "深成指", "收盘", "收报", "成交额", "指数产品",
                 "股价", "跌破", "面值", "回购", "增持", "目标价", "ETF上报", "A股11月",
                 "股票数据库", "交易所行情", "证券行情"],
    "劳动争议": ["劳动争议", "仲裁院", "劳动仲裁", "开庭", "补偿金", "抚养费", "工伤认定",
                 "拖欠工资", "欠薪", "劳动合同"],
    "破产清算": ["破产清算", "破产重整", "债权申报", "债权人会议", "指定管理人",
                 "资不抵债", "清盘呈请", "法院裁定受理"],
    "交通路况": ["收费站", "高速路况", "改扩建", "封闭", "绕行", "通行", "路况通报"],
    "气象预警": ["气象台", "大风蓝色预警", "降温", "降雪", "暴雨预警", "重污染预警", "气温创立秋后新低"],
    "地震灾害": ["级地震", "震源深度", "海啸预警", "最大震度", "疏散令"],
    "刑事案件": ["刑拘", "判处", "一审", "获刑", "立案调查", "涉嫌诈骗", "被传唤"],
    "消费维权": ["仅退款", "缺斤少两", "消费维权", "立案调查", "赔付达成一致", "市监局通报"],
}

# 强标志：命中 ≥2 个不同词才算「明确命中」，避免单个通用词误判
_STRONG_MIN = 2


def _event_types(s: str) -> set:
    flat = _flat(s)
    out = set()
    for k, words in EVENT_TYPE_LEXICON.items():
        n = sum(1 for w in words if w in flat)
        if n >= _STRONG_MIN or (n == 1 and any(w in flat for w in words if len(w) >= 6)):
            out.add(k)
    return out


# 机构 / 主体专名（稳定标识的近似，纯规则版）
_ORG_SUFFIX = ("公司|集团|银行|法院|仲裁院|管理局|监督局|人民政府|市政府|市监局|卫健委|"
               "人社局|医保局|财政部|商务部|工信部|证监会|交易所|港交所|新华社|央视网|"
               "中新网|澎湃新闻|财联社|经济参考报|界面新闻|新京报|每日新闻|共同社|NHK|"
               "电视台|报社|大学|学院|医院|中心|协会|街道|管委会|总领馆|使馆|气象台|"
               "交通运输局|数据局|能源局|药监局|工程院|支队|大队|陆港|机场|地铁")
ORG_RE = re.compile(r"[\u4e00-\u9fff]{2,10}(?:" + _ORG_SUFFIX + r")")


def _orgs(s: str) -> set:
    return set(ORG_RE.findall(str(s or "")))


def _abstract_sentence_ok(rep_title: str, sentence: str) -> Tuple[bool, str]:
    """第二段（或任一跨源句）能否进入该事件摘要：事件身份必须一致。

    硬否决只有两条：事件类别互斥、主体专名零交集。
    数字不重合**不能单独否决**——同一事件的汇总/分项、结论/背景、跨源并列
    数字本来就不重合，此前把它当硬闸误杀了地方债、A股收盘、电池脱保潮等
    目标样例里的合规行（2026-11-22 复验发现）。
    """
    t_types, s_types = _event_types(rep_title), _event_types(sentence)
    if t_types and s_types and not (t_types & s_types):
        tn, sn = _nondate_numbers(rep_title), _nondate_numbers(sentence)
        extra = "；数字锚点亦完全不重合" if tn and sn and not (tn & sn) else ""
        return False, f"事件类别冲突 标题{sorted(t_types)} / 句{sorted(s_types)}{extra}"
    to, so = _orgs(rep_title), _orgs(sentence)
    # 同一机构的全称/简称（「缙云法院」vs「缙云县人民法院」）不算零交集：
    # 两边各有 ≥2 个明确机构名、完全不重合、且不是分项/背景句时才否决。
    if len(to) >= 2 and len(so) >= 2 and not (to & so):
        tn, sn = _nondate_numbers(rep_title), _nondate_numbers(sentence)
        if not (tn & sn) and not SEGMENT_HINT.search(sentence):
            return False, (f"主体专名零交集且无共享数字锚点 标题{sorted(to)[:2]} / "
                           f"句{sorted(so)[:2]}")
    return True, "事件身份一致（分项/背景/跨源并列数字不重合属正常，不作否决）"


def headline_abstract_mismatch(title: str, abstract: str) -> Tuple[bool, str]:
    """闸1：标题与摘要是否讲同一件事。返回 (是否错配, 依据)。"""
    a = _flat(abstract)
    t = _flat(title)
    if not a or not t:
        return False, "—"
    # 「摘要未取得」是显式留痕（需求⑤允许留池并由主表硬闸拦截），不是错配
    if "摘要未取得" in a or len(_net(a)) < 12:
        return False, "摘要未取得/过短，交由主表硬闸与质量门禁，不判错配"
    # 逐句查：拼接摘要里只要有一整句属于别的事件，就是错配（截图1）
    sents = [p.strip() for p in re.split(r"(?<=[。！？；])\s*", str(abstract or "")) if p.strip()]
    bad = []
    for s in sents[:6]:
        if len(_net(s)) < 12:
            continue
        ok, why = _abstract_sentence_ok(title, s)
        if not ok:
            bad.append(why)
    if bad:
        return True, "；".join(bad[:2])
    # 全格级数字核对只作留痕、不否决：标题常是汇总值，摘要讲分项/背景，
    # 两者数字不重合是目标样例的常态（地方债、A股、医保钱包均如此）。
    return False, "标题与摘要事件身份一致（数字分项/背景不判错配）"


def pair_cannot_link(a, b) -> Tuple[bool, str]:
    """聚类前硬否决：事件类别冲突 / 主体零交集 / 只有日期数字作锚点。"""
    ta, tb = _event_types(a.title), _event_types(b.title)
    if ta and tb and not (ta & tb):
        return True, f"cannot-link：事件类别互斥 {sorted(ta)} vs {sorted(tb)}"
    oa, ob = _orgs(a.title), _orgs(b.title)
    if oa and ob and not (oa & ob):
        na, nb = _nondate_numbers(a.title), _nondate_numbers(b.title)
        if not (na & nb):
            return True, (f"cannot-link：主体专名零交集且无共享非日期数字 "
                          f"{sorted(oa)[:2]} vs {sorted(ob)[:2]}")
    return False, ""


def date_only_anchor(shared: set) -> bool:
    """共享数字里是否只剩日期数字（11/19/20/2026 等）——是则不得据此合并。"""
    def _year_or_date(x: str) -> bool:
        head = x.split(".")[0]
        if not head.isdigit():
            return False
        v = int(head)
        # 4 位年份（1900–2100）与 1–31 的月/日数字都属日期
        return (1900 <= v <= 2100 and len(head) == 4) or (v <= 31 and "." not in x and len(head) <= 2)

    real = {x for x in shared if not _year_or_date(x)}
    return not real


# ---------------------------------------------------------------- 闸2：破产倒闭主体可核对
ANON_SUBJECT = re.compile(
    r"某(?:一)?[\u4e00-\u9fff]{0,10}?(?:企业|公司|厂|店|房企|光伏|科技|集团|商行|"
    r"银行|平台|门店|厂商|车企|药企|主体)|(?:匿名|未具名|不具名|无名)[\u4e00-\u9fff]{0,6}"
    r"(?:企业|公司|主体)")
STOCK_CODE = re.compile(
    r"(?:\(\s*\d{5}\.HK\s*\)|（\s*\d{5}\.HK\s*）|\d{6}\.(?:SZ|SH|BJ)|"
    r"\(\s*\d{6}\s*\)|（\s*\d{6}\s*）|股票代码[:：]?\s*\d{6})", re.I)
# 来源列里的权威财经/交易所信源。成稿后带股票代码的首句可能被分给其它成员，
# 此时 `Item.text()` 只看 title+abstract 会看不到上市信号，必须用来源列补判定。
AUTHORITATIVE_FINANCE = [
    "东方财富", "港交所", "深交所", "上交所", "交易所公告", "巨潮资讯", "证监会",
    "财联社", "21财经", "证券时报", "同花顺", "新浪财经", "乐居财经", "央视财经",
    "新华社", "中国经营报", "界面新闻", "澎湃新闻", "新京报", "每日经济新闻",
    "南华早报", "大公", "证券之星", "中国证券报", "上海证券报", "第一财经",
    "华尔街见闻", "36氪", "经济参考报",
]
# 明确公司主体（非「某企业」匿名说法）
NAMED_CORPORATE = re.compile(
    r"[\u4e00-\u9fff]{2,12}(?:股份|集团|控股|地产|置业|科技|汽车|能源|药业|"
    r"医药|保险|银行|实业|资本|发展|国际|制造|物流|零售|寄卖)")
COURT_NOISE = re.compile(r"人民法院|法院公告|裁定受理|案号|破\d+号|债权人会议|债权申报|指定管理人")
SCALE_WITH_SUBJECT = re.compile(
    r"(?:营收|营业收入|注册资本|总资产|负债|债权|涉款|涉案金额|员工|职工|门店|"
    r"产能|市值|年销售额)[^。；]{0,12}?\d+(?:\.\d+)?\s*(?:亿元|万元|亿|万|人|家|店|名|%|平方米)")
WEAK_SOURCE = re.compile(r"微信公众号|自媒体|个人号|车链法观|论坛|贴吧|博客")


def bankruptcy_contract(it) -> Tuple[bool, str, str]:
    """闸2：破产倒闭只留上市 / 知名 / 规模可核对主体。返回 (通过, 类型, 依据)。"""
    text = _text(it)
    hit = [w for w in _rules.BANKRUPTCY_WORDS if w in text]
    if not hit:
        return True, "—", "非破产倒闭类，不适用"

    # 股票代码是唯一无争议的上市信号；法院「公告」不是上市公告
    if STOCK_CODE.search(text):
        return True, "上市主体", f"命中股票代码 {STOCK_CODE.search(text).group(0)}，主体可核对"

    anon = ANON_SUBJECT.search(text)
    if anon:
        return False, "匿名主体不可核对", (
            f"命中破产词「{'、'.join(hit[:2])}」但主体为「{anon.group(0)}」；"
            f"需求④只留上市/知名/规模可核对主体，匿名主体逐条留痕不进池")

    # 上市/知名主体的来源列补判：成稿后带股票代码的首句可能被拆给别的成员，
    # 但「东方财富网 · 21财经 · 港交所公告」这类多源权威财经信源本身即可核对主体。
    # 匿名主体与单一弱来源已在上面/下面否决，此处不会把「某光伏 + 公众号」放回来。
    src_txt = str(getattr(it, "source", "") or "")
    auth = sorted({w for w in AUTHORITATIVE_FINANCE if w in src_txt})
    named = NAMED_CORPORATE.search(str(getattr(it, "title", "") or ""))
    if len(auth) >= 2 and named:
        return True, "上市/知名主体（多源可核对）", (
            f"来源列含 {len(auth)} 个权威财经/交易所信源（{'、'.join(auth[:4])}），"
            f"标题含明确公司主体「{named.group(0)}」；按需求④视为主体可核对")

    # 法院公告 + 单一弱来源 + 无可核对主体 → 剔除（截图2）
    if COURT_NOISE.search(text) and WEAK_SOURCE.search(getattr(it, "source", "")) \
            and not _orgs(getattr(it, "title", "")):
        return False, "来源与主体均不可核对", (
            "仅法院程序公告 + 微信公众号单一弱来源，无明确可核对企业主体；按需求④剔除")

    scale = SCALE_WITH_SUBJECT.findall(text)
    if scale:
        return True, "规模可核对", f"带主体关联的规模数字：{'、'.join(scale[:2])}"

    # 其余交给原规则（已去掉裸「公告」上市信号）
    ok, kind, why = _rules.bankruptcy_subject_verdict(it)
    if ok:
        return True, kind, why
    return False, "主体不可核对", why


# ---------------------------------------------------------------- 闸3：社交话题 = 民生社会议题
LIVELIHOOD_TOPICS = [
    "医保", "社保", "养老金", "养老", "退休", "缴费年限", "工资", "欠薪", "裁员", "劳动仲裁",
    "就业", "招聘", "工伤", "房价", "房贷", "租金", "公积金", "物业", "供暖", "水电",
    "物价", "菜价", "猪肉", "鸡蛋", "食品", "食品安全", "外卖", "快递", "预制菜", "消费维权",
    "退款", "仅退款", "缺斤少两", "预付卡", "充值", "医保钱包", "集采", "药品", "药价",
    "挂号", "医院", "学费", "幼儿园", "学位", "课后服务", "校车", "公交", "地铁", "高速",
    "路况", "收费站", "停运", "航班", "火车", "12306", "天气", "预警", "降温", "降雪",
    "暴雨", "重污染", "停水", "停电", "燃气", "反诈", "诈骗", "辟谣", "召回", "质量",
    "童工", "未成年人", "儿童", "老人", "残疾人", "劳动者", "消费者", "车主", "参保人",
    "购房者", "患者", "学生", "居民", "职工", "被裁员工", "退休人员", "最低缴费",
    "寄卖", "二手", "退货", "货品", "门店", "押金", "预付", "理赔", "保单",
    # 工厂制药医药 / 医疗成本（用户①点名行业，且属民生成本议题）
    "医药", "制药", "药企", "CRO", "实验猴", "原料药", "疫苗", "临床试验", "耗材",
    "报价", "存栏", "集采", "诊疗", "床位", "药厂", "临床试验",
    # 车主使用成本（衣食住行里的「行」）：电池脱保 / 残值 / 维修 / 充电
    "新能源车", "电池", "质保", "残值", "换电池", "维修", "保养", "充电",
    "车价", "报废置换", "充电桩", "续航",
]
FACT_ANCHORS = [
    "通报", "立案", "判决", "一审", "判处", "刑拘", "处罚", "罚款", "辟谣", "回应",
    "发布", "预警", "停运", "封闭", "下调", "上调", "退款", "赔付", "召回", "伤亡",
    "死亡", "受伤", "裁员", "欠薪", "停发", "到期", "受理", "获批", "实施", "上线",
    "开行", "涨价", "降价", "跌破", "超", "立案调查", "被传唤", "申请限制减刑",
    "举报", "查获", "抓获", "拘留", "赔偿", "调解", "开庭", "宣判", "报案", "涉款",
    "关门", "欺诈", "跟进", "接获", "未获退回", "受损", "案发", "失联", "追责",
    "补缴", "代缴", "共济", "生效", "撤销", "下架", "停售", "限购", "查封",
    # 国际三类（战争/灾害/突发事故）与领事保护，目标样例里的高关注社会事件
    "遇害", "遇难", "身亡", "袭击", "空袭", "使馆", "领保", "安全提醒", "撤离",
    "疏散", "停运", "余震", "震感", "伤亡", "报案", "涉款", "涨价", "上调",
]
GOSSIP_EXCLUDE = [
    "偶遇", "打趣", "玩具", "围巾", "粉丝", "应援", "合影", "试吃", "街头", "盛赞",
    "刷屏", "走红", "萌", "可爱", "政要现身", "多国政要", "花边", "宣传稿", "图案取自",
    "车厘子", "网红", "打卡", "综艺", "春晚", "演唱会", "电视剧", "明星", "开播",
    "启播", "微纪录", "大国外交", "盛赞", "点赞", "热议围观", "三轮元首",
]
# 占位摘要：不是「摘要齐备」，是契约说明文字，绝不冒充内容
PLACEHOLDER_ABSTRACT = [
    "按未经筛选契约", "未作贴近度判定", "榜单快照与聚合日报", "该词条来自",
    "仅话题名", "按未经筛选", "未作贴近", "聚合日报", "热度值", "热度分别为",
    "榜第", "列第", "在榜", "快照与聚合", "未经筛选契约保留",
]


def is_social_item(it) -> bool:
    g = str(getattr(it, "group", "") or "")
    t = str(getattr(it, "tag", "") or "")
    rt = str(getattr(it, "record_type", "") or "")
    src = str(getattr(it, "source", "") or "")
    return ("文娱" in g or "社交" in t or "社交" in rt or "热榜" in t or
            bool(re.search(r"热搜|热榜|抖音·热点|微博·|百度·|知乎·|快手·|贴吧", src)))


def placeholder_verdict(abstract: str) -> Tuple[bool, str]:
    a = _flat(abstract)
    hit = [p for p in PLACEHOLDER_ABSTRACT if p in a]
    if hit:
        return True, f"占位/契约说明文字（{'、'.join(hit[:2])}），不算需求⑤「摘要齐备」"
    return False, ""


def social_topic_contract(it) -> Tuple[bool, str]:
    """闸3：社交话题必须是民生社会议题或高关注社会事件，且要有详细事实内容。"""
    if not is_social_item(it):
        return True, "非社交/热榜类，不适用"
    text = _text(it)
    abs_txt = str(getattr(it, "abstract", "") or "")
    ph, why = placeholder_verdict(abs_txt)
    if ph:
        return False, why
    gossip = [w for w in GOSSIP_EXCLUDE if w in text]
    if gossip:
        return False, (f"政要花边/宣传/综艺类，非民生社会议题（{'、'.join(gossip[:3])}）；"
                       f"社交话题按「与衣食住行密切相关的社会议题 / 高关注社会事件」定义")
    live = [w for w in LIVELIHOOD_TOPICS if w in text]
    facts = [w for w in FACT_ANCHORS if w in text]
    if not live and not facts:
        return False, "既无民生议题词，也无可核对事实锚点（仅热度/榜单占位）"
    if len(_net(abs_txt)) < 25:
        return False, f"摘要净字 {len(_net(abs_txt))}，无详细事实内容"
    # 高关注社会事件：可核对数字（阿拉伯数字）、中文数量词（「一名中国公民遇害」）、
    # 或 ≥2 个事实锚点，三者任一即可。此前只认阿拉伯数字，误杀了领保提醒类条目。
    has_num = bool(_nondate_numbers(text))
    has_cn_qty = bool(CN_NUM_QUANTITY.search(text))
    if not live and facts and not (has_num or has_cn_qty or len(facts) >= 2):
        return False, "仅单个泛化事实词、无人数/金额/规模等可核验证据，不构成高关注社会事件"
    return True, (f"民生议题 {len(live)} 项 / 事实锚点 {len(facts)} 项 / "
                  f"摘要 {len(_net(abs_txt))} 净字，符合社会议题定义")


# ---------------------------------------------------------------- 统一入口
def contract_check(it) -> Tuple[bool, str, str]:
    """候选池契约校验（非严选）。返回 (是否入池, 命中的闸, 依据)。

    顺序：标题形态 → 占位摘要 → 标题摘要错配 → 来源身份 → 破产主体 → 社交话题。
    前两闸是需求⑤「字段齐备 / 形态合规」，不是贴近度严选；被拦条目逐条写
    「工程留痕 P」，不进候选池；主表是其子集，同步生效。
    """
    title = str(getattr(it, "title", "") or "")
    abs_txt = str(getattr(it, "abstract", "") or "")

    tok, twhy = title_shape_verdict(title)
    if not tok:
        return False, "标题形态不合规", twhy

    ph, why = placeholder_verdict(abs_txt)
    if ph:
        return False, "占位摘要", why

    mismatch, mwhy = headline_abstract_mismatch(title, abs_txt)
    if mismatch:
        return False, "标题摘要错配", mwhy

    sok_id, swhy_id = source_identity_verdict(it)
    if not sok_id:
        return False, "来源身份不一致", swhy_id

    bok, _bkind, bwhy = bankruptcy_contract(it)
    if not bok:
        return False, "破产主体不可核对", bwhy

    sok, swhy = social_topic_contract(it)
    if not sok:
        return False, "社交话题不合规", swhy

    return True, "—", "契约校验通过（齐备 / 形态合规 / 身份一致 / 主体可核对 / 议题合定义）"


def contract_check_many(items: List) -> Tuple[List, List[dict]]:
    kept, rejected = [], []
    for it in items:
        ok, gate, why = contract_check(it)
        if ok:
            kept.append(it)
        else:
            rejected.append({
                "展示序": getattr(it, "seq", 0), "标题": getattr(it, "title", ""),
                "来源": getattr(it, "source", ""), "命中闸": gate, "依据": why,
            })
    return kept, rejected
