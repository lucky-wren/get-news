"""规则引擎 · 按《01 定时任务 instruction 现行全文》（最高仲裁优先级）实现。

仲裁：01 > 00 手册 > 05/06 > 02 > 03 > 04 > 07/08。旧实现误用了 06 的简化
公式（Final=30+6I+2C+V+impact、Final_adj=Final−4.2、等级 A/B/C/D/E）；
01 要求 Final=V/82.32×100、等级 S/A/B/C/D、gin/cap/λ=6，且含 T 六维、
H 通道关闭、核实三态、影响面四维、表 R 八组、置顶池、头部三席、事件层
与议题层严格分离。本模块一次补齐，每层产出留痕供附录 1–17 现算。

三条铁律：先剔后评；编排只改展示序、不改任何评级数值；剔除留痕不删行。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import clean
from .model import Item

TZ = timezone(timedelta(hours=8))
LAMBDA = 6.0

# ── 可核对词组表：全部为词组，禁用裸词（01 §2 / 00 §5.2）─────────────
ACHIEVEMENT = ("稳中向好 同比增长 再创新高 成效显著 持续向好 突破万亿 已达2.7万个 "
               "总体平稳 持续提升 向新向优 圆满收官 再上新台阶 提质扩容 加速提质 "
               "较快增长 成效明显 突破千亿 覆盖全部 全国首个 "
               "活力持续释放 稳定向好 稳步提升 市场活力 持续释放 态势向好 "
               "再创新高 创新高 数量再创新高 质量稳定向好 总体平稳 "
               "成效显著 亮点纷呈 交出答卷 新答卷 增速 同比接近翻倍 "
               "立国之本 强国之基 经济命脉 重要指示").split()
FAR_FUTURE = ("拟建 规划中 征求意见稿 框架协议 意向签约 试点意向 多年后目标 "
              "趋势预测 展望 前瞻 拟引入 拟实施 拟于 计划于 即将启动 未来五年 "
              "2030年 2035年").split()
UNRELATED = ("会见出席 会务筹备 人事任免 外事礼宾 内部评优 行业年会 纯政策解读 "
             "无落地动作 挂牌成立 揭牌仪式 座谈交流 签署备忘录 "
             "论道 律师业 探索法律服务 行业论坛 研讨会 行业峰会 圆桌沙龙 "
             "留后门 别给 "
             "学习贯彻 宣讲 宣讲团 纪律教育 二十届五中全会 焦点访谈 权威访谈 "
             "新思想引领新征程 一见 习言道 和合共生 国际观察 记者手记 经纬线 "
             "系列重要讲话 热烈反响 政绩观 组织优势 单行本 致贺电 贺电 会见 "
             "出席 揭牌 授牌 挂牌 授牌仪式 内部评优 行业年会 座谈交流 研讨会 "
             "市委副书记 到卫生健康系统 调研 会议指出 访谈 专访 综述 "
             "纪录片 开播 系列片 揭秘 探访 侧记 纪实 "
             # 只保留「栏目名 / 系列名 / 特定标题特征」，不收普通名词。
             # 上一版塞进「安全」「会晤」「APEC」「水域面积」「就近实惠」等泛词，
             # 把正常新闻当无关机构动态剔掉，真采集主表从 18 掉到 9。
             "总书记 人民情怀 高技能人才 大国工匠 来深圳 五十年 "
             "大国良性互动 汇聚信心 热烈反响 政绩观 组织优势 单行本 "
             "致贺电 贺电 以正确政绩观 充分发挥党的组织优势 "
             "投资于人 完整社区 电子商务助力 从前10个月 用电量 "
             "全家福 深圳印记 青铜如故 一日店长 "
             "系列重要讲话 学习贯彻党的 纪律教育 "
             "亚太经合组织关于亚太地区 新思想 以人为本加快建设 "
             # 截图 44 后真跑里的活动宣传稿：大赛颁奖 / 宣传收官 / 案例汇编。
             # 全是标题级具体特征，不含「安全」「会晤」这类泛词。
             "大赛颁奖 颁奖仪式 倒计时1天 宣传收官 收官总结 主题宣传 "
             # 「案例汇编」是泛词，会误伤「历史文化街区保护利用案例汇编印发」
             # 这类中性文化稿；3 条活动宣传稿由上面更精确的特征词全部覆盖。
             "创新案例 法治宣传教育 智慧医保大赛 启动仪式").split()
PROTECT = ("油价调整 气象预警 医保目录 社保办理 供暖退费 疫苗 限行 放假安排 "
           "退费 12306 辟谣 抽检 召回 管制 消费警示 寒潮预警 大风预警 暴雨预警 "
           "停运 限速 风险提示 药品目录 报销比例 公积金 养老金").split()
MASS_TOPIC = ("物价 价格上调 价格下调 菜价 电价 水价 票价 消费券 以旧换新 "
              "裁员 欠薪 招聘 工时 加班 劳动合同 劳动争议 最低工资 失业 工伤 "
              "医保目录 药品目录 报销比例 集采 养老金 公积金 异地结算 社保办理 "
              "工厂 停产 复工 产能 订单 制造业 供应链 半导体 关厂 大面积关店 "
              "药价 新药获批 短缺药 抽检不合格 CAR-T 药监局 召回 "
              "AI 大模型 芯片 算力 产品发布 产业落地 就业影响 生成式人工智能 "
              "新能源 电动汽车 电价调整 储能 充电设施 光伏 氢能 "
              "化债 专项债 结存限额 地方政府负债 "
              "房价 房贷 烂尾 保交楼 交房 租房 面值退市 公积金贷款 "
              "倒闭 资不抵债 破产重整 退市 清算 债权 "
              "教育 医疗 养老 交通出行 环境 公共安全 消费维权 食品安全 反诈 "
              "天气预警 降温 寒潮 台风 暴雨 暴雪 供暖 春运 高速 航班 地铁 "
              "地震 火灾 事故 迫降 撤离 失联 遇难 流感 疫苗 病例 猴痘 霍乱 "
              "诈骗 辞职 致歉 高考 入学 学费 快递 外卖 燃气 供水 桥梁 "
              "补贴 信用卡 金价 油价 LPR 央行 银行存款 贵金属 冷空气 气温下降 降水 "
              "风寒 阵风 最高气温 天气晴朗 准考证 国考 就业岗位 增收 赛事收官 金牌 "
              "电动汽车 降费 隔夜逆回购 核能供暖 流感季 毒株 机器人 "
              "短道速滑 世巡赛 奖牌 夺冠 赛果 融资保证金 保证金比例 融资融券 "
              "交易所通知 增量资金 假酒 中毒 住院 公共卫生 未成年人 性侵害 "
              "指导性案例 司法终局 谣言 行政拘留 网络谣言 盒饭 餐食 高铁 "
              "候鸟 野生动物 流感疫苗 接种 "
              "航天 载人飞船 空间站 乘组 发射 神舟 天宫 外籍航天员 "
              "英伟达 黄仁勋 复职 回归 CEO 股价 美股 芯片 显卡 算力 "
              "全网热搜 热搜第一 现象级热点 复职 涨超 暴涨 "
              "落叶缓扫 冰雪运动 新雪季 学校早餐 儿童用药 婴幼儿辅助食品 "
              "冰雪 新雪季 滑雪 雪场 开板 冰雪消费券 冰雪旅游 "
              "恶意差评 外卖平台 差评 刷分 控评 消费维权 "
              "跨海大桥 架梁 高铁 轨道交通 计次票 满额优惠 起乘价 "
              "AI创造公开赛 像素射击 奖金池 单元赛道 "
              "ETC 退费 赔偿金 赔款 医保谈判 药品降价 公积金提取 物业费 "
              "房价 房租 月租酒店 外卖骑手 未成年人 校园 诈骗 网络谣言 "
              "高铁架梁 轨道交通票价 新票价 高速管制 航班取消 供暖 降温 "
              "地震 限速 火灾 中毒 迫降 撤离 失联 遇难 谣言 辟谣").split()
NATIONAL_HOT = ("重大天气预警 全国性公共安全事件 现象级文娱 全国性赛事结果 "
                "涉及大范围人群的价格 国家级应急响应 大范围灾害 重大公共卫生事件 "
                "世巡赛收官 接力一金一铜 夺冠 金牌 奖牌 赛果 决赛 世界杯 锦标赛 "
                "载人航天 航天员乘组 全网热搜 热搜榜首 现象级热点 "
                "全运会 破纪录 冠军 联赛收官 中国队获").split()
INTL_GEO = ("印尼 秘鲁 沙特 菲律宾 德国 美国 日本 韩国 俄罗斯 乌克兰 巴基斯坦 "
            "印度 柬埔寨 泰国 越南 缅甸 尼泊尔 以色列 伊朗 伊拉克 土耳其 埃及 "
            "南非 巴西 阿根廷 墨西哥 加拿大 澳大利亚 英国 法国 意大利 西班牙 "
            "欧盟 北约 联合国 波兰 芬兰 加沙 海湾 霍尔木兹 海外 境外 使馆").split()
INTL_WAR = ("空袭 弹道导弹 无人机袭击 停火协议破裂 撤军 胡塞武装 阵亡 "
            "军事侵略 轰炸 开火 武装冲突 战事 击伤 击中领土").split()
INTL_DISASTER = ("级地震 强震 海啸 洪灾 飓风 火山喷发 埃博拉 霍乱 疫情 "
                 "重大公共卫生事件 大范围灾害 假酒中毒 中毒事件 公共卫生事件").split()
INTL_ACCIDENT = ("全国停飞 停飞令 机场关闭 空难 坠机 剧烈爆炸 核泄漏 恐袭 "
                 "大规模停电 货船遭袭 使馆遭袭 紧急降落").split()
INTL_NEUTRAL = ("自贸区 议定书 国际标准 汇率 全球机构 监管调查 外交摩擦 会务成果 "
                "选举 换政 国际形势 重大突破 合作备忘录 论坛 博览会").split()
F1 = "纯规划 条例草案 征求意见 中长期规划 纲要".split()
F2_F3 = ("单市区县 局部节点 政务窗口 内部通报 地方例会").split()
SOFT_H = "软文 特写 成就抒情 侧记 纪实 巡礼 札记".split()

# 信息功能档位 I（单条取最高一档，不叠加）
I_TIERS: list[tuple[int, str, list[str]]] = [
    (9, "重大伤亡", "死亡10人 死亡15人 死亡20人 死亡5人 致多人死亡 伤亡30人 重大伤亡 "
                    "遇难5人 失联30人 5死1伤 多人死亡".split()),
    (7, "权威预警", "中央气象台 气象台发布 寒潮蓝色预警 大风蓝色预警 暴雨预警 暴雪预警 "
                    "国家预警信息发布中心 权威预警 发布预警 气象台继续发布".split()),
    (5, "终局性新规", "正式施行 公告施行 国务院决定 新规施行 管理办法发布 印发方案 "
                     "发布通知 实施方案 印发通知 部署实施".split()),
    (5, "国家级响应", "国家级应急响应 启动应急响应 启动Ⅲ级应急响应 国务院部署 "
                     "两部门 三部委 联合发布 国家医保局".split()),
    (5, "重大司法终局", "一审宣判 判决 指导性案例 立案告知书 司法终局 刑事拘留 "
                       "调查报告 通报查处 被查 挂牌督办".split()),
    (4, "重大数据", "统计公报 三季度数据 70城房价 CPI PPI 产销数据 预计毕业生 "
                   "逆回购操作 降费金额 超24亿元 亿元规模".split()),
    (4, "科技工程突破", "首条超百公里 首飞 成功发射 重大科研成果 技术突破 启运 "
                       "核能供暖 AI控温 机器人 全球第一 蝉联全球第一".split()),
    (1, "消费警示", "风险提示 抽检不合格 召回 消费警示 请勿 谨防 回应 辟谣 "
                   "注意了 提醒 谣言 集体提示风险".split()),
]
AFFECTED = "市民 消费者 旅客 患者 司机 学生 居民 农户 职工 家长 用户 群众 司机朋友".split()
NATIONWIDE = "全国 多地 中东部 全国性 国务院 央行 部委 各省 全国范围".split()
REGIONAL = "跨省 区域 多省 华北 华东 中东部 沿海 流域".split()
LOCAL = "某市 单市 区县 本市 当地 该县 该市".split()
ACTION = "提醒 请注意 应避免 可办理 停运 限速 召回 防范 请勿 需提前 停止使用 关注".split()
STATIC = "回顾 盘点 纪念 综述 回顾性".split()

# 表 R 八组（编排层 daily_score，只进 U / head_score）
TABLE_R: list[tuple[str, list[str]]] = [
    ("天气与出行", "天气 预警 降温 寒潮 台风 暴雨 暴雪 高速 航班 地铁 春运 交通 出行 限速".split()),
    ("物价与消费", "物价 菜价 油价 金价 消费券 以旧换新 价格 消费 金价 银价 羽绒服".split()),
    ("房价与居住", "房价 房贷 烂尾 保交楼 交房 租房 公积金 房地产 面值退市".split()),
    ("工作与收入与社保", "裁员 欠薪 就业 招聘 工时 社保 医保 养老金 劳动争议 抽成 毕业生".split()),
    ("医药与医保", "药品 药价 集采 新药 疫苗 流感 医保 报销 抽检 召回 病例".split()),
    ("产业与就业", "工厂 停产 产能 供应链 半导体 AI 新能源 储能 光伏 破产 化债 专项债".split()),
    ("社会议题", "教育 医疗 养老 环境 公共安全 消费维权 食品安全 反诈 辟谣 诈骗 火灾".split()),
    ("公共服务", "12306 快递 外卖 供水 供电 燃气 桥梁 放假安排 退费 政务服务".split()),
]
STORY_GROUPS = {
    # 同一议题的多篇侧面稿（发布会/乘组/人物特写/配套解读）合并为 1 条，
    # 被合并条完整进附录 17；侧面稿不重复占主表席位、不刷屏。
    "神舟二十四号载人飞行任务": (
        "神舟二十四 神舟二十四号 空间站 乘组 汤洪波 武毅 齐尚 天宫 揽月着陆器 载人登月".split(),
        "载人飞行 发射 乘组 新闻发布会 在轨 太空实验 登月 出征 指令长 飞行任务".split()),
    "一老一小服务体系建设": (
        "一老一小 半日托 计时托 老幼共托 托育 养老机构 护理型床位".split(),
        "实施方案 服务体系 覆盖率 入托率 改造升级 一刻钟托育".split()),
    "新雪季与冰雪消费": (
        "新雪季 冰雪 滑雪 雪场 冰雪消费 冰雪旅游 冰雪装备".split(),
        "开板 冰雪游 消费券 雪道 亲子 航线 装备 雪季".split()),
    "上海轨道交通票价调整": (
        "上海轨道交通 上海地铁 计次票 满额优惠".split(),
        "新票价 票价 配套优惠 12月1日 实施".split()),
    "青少年网络交友诈骗": (
        "网络交友 压岁钱 小额多次 网络游戏交友".split(),
        "诈骗 转账 判刑 有期徒刑 退缴".split()),
    "医保目录高价药谈判": (
        "129万元 CAR-T 瑞基奥仑赛 倍诺达 医保药品目录 抗癌药".split(),
        "医保目录 谈判 支付标准 28.86万 注射液".split()),
    "融资保证金比例调整": (
        "融资保证金 沪深北交易所 两融 融资融券".split(),
        "80% 11月30日 比例调整 逆周期调节 保证金比例".split()),
    "美伊冲突与海湾局势": ("伊朗 美伊 海湾 霍尔木兹 胡塞 以色列 加沙".split(),
                          "空袭 轰炸 停火 袭击 限电 战事 封锁 导弹".split()),
    "俄乌局势": ("俄乌 俄罗斯 乌克兰 波兰 北约".split(),
                "导弹 停火 袭击 军演 前线 击中 冲突".split()),
}
SEV_RE = re.compile(r"(\d+(?:\.\d+)?)\s*级地震|死亡\s*(\d+)\s*人|(\d+)\s*人死亡"
                    r"|伤亡\s*(\d+)\s*人|(\d+)\s*人伤亡")
UNIT_NUM = re.compile(r"\d+(?:\.\d+)?\s*(?:万人|亿元|公里|元/克|元/股|%|级|人|吨|万张|万平方公里)")


def _hits(text: str, words: list[str]) -> list[str]:
    return [w for w in words if w in text]


canon = clean.canon          # 归一规则单一真值源（clean.py）


def channels_of(it: Item) -> list[str]:
    """独立信源渠道数；同一主媒体下的多频道归一为 1 家，不虚增 cross_score。"""
    out, seen = [], set()
    for c in (it.sources or "").split(" · "):
        n = canon(c)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def cap_feasible(items: list[Item], n: int = 10, cap: int = 3) -> bool:
    """纯分组判定：能否挑 n 条满足「相邻不同大类 + 单类 ≤cap」。

    多重集可交错排列的充要条件是最大类 ≤ ⌈n/2⌉；再对每类按 cap 截断求和。
    与 U / 置顶池 / 细标签无关，用于验收 ⑧ 区分「算法未做到」与「当天产能无解」。
    """
    from collections import Counter
    if len(items) < n:
        return False
    cnt = Counter(it.group for it in items)
    trimmed = {g: min(v, cap) for g, v in cnt.items()}
    if sum(trimmed.values()) < n:
        return False
    return max(trimmed.values()) <= (n + 1) // 2


@dataclass
class Trace:
    """规则引擎全量留痕；附录 1–17 与口径正文只从此处现算，不写死数字。"""
    decisions: list[dict] = field(default_factory=list)     # 附录 1
    pool_edge: list[dict] = field(default_factory=list)     # 附录 2 候选池边界
    out_of_window: list[dict] = field(default_factory=list)  # 附录 4
    abstract_len: list[dict] = field(default_factory=list)  # 附录 5
    I_hits: list[dict] = field(default_factory=list)        # 附录 8
    cluster_pairs: list[dict] = field(default_factory=list)  # 附录 12
    merged_channels: list[dict] = field(default_factory=list)  # 附录 12b
    discovery: list[dict] = field(default_factory=list)       # 附录 12c 社交词条回查挂接
    time_evidence: list[dict] = field(default_factory=list)   # 附录 18 逐条时间依据留痕
    intl_decisions: list[dict] = field(default_factory=list)  # 附录 13
    interleave: list[dict] = field(default_factory=list)     # 附录 14
    topic_only: list[dict] = field(default_factory=list)     # 附录 15
    head: list[dict] = field(default_factory=list)           # 附录 16
    story: list[dict] = field(default_factory=list)          # 附录 17
    conflicts: list[dict] = field(default_factory=list)
    impact4: list[dict] = field(default_factory=list)        # 附录 11 四维
    orchestrate: list[dict] = field(default_factory=list)    # 附录 11 编排
    top_pool: dict = field(default_factory=dict)
    counters: dict = field(default_factory=dict)


# ────────────────────────── 1. 准入（只决定收不收）──────────────────────────
def admit(items: list[Item], tr: Trace, *, strict: str = "strict") -> list[Item]:
    """判定顺序：大类级 → 大众贴近度（含主体规模闸）→ 国际三类 → F1–F4。
    剔除逐条留痕、不删行；被剔条目仍在候选池里（主表是其子集）。
    """
    kept = []
    n_reject = {"成就宣传": 0, "远期规划": 0, "无关机构动态": 0}
    n_subject_gate = {"通过": 0, "剔除": 0}
    n_intl = {"保留": 0, "剔除": 0, "重要": 0,
              "战争与武装冲突": 0, "灾害与公共卫生": 0, "突发事故与重大安全事件": 0}
    n_mass_hit = 0
    soft_only: list[Item] = []   # 仅因「未命中贴近度词表」被降权保留的条目
    for it in items:
        t = it.title + it.abstract
        # 独立原文准入 + 结构化小震门禁（01 §1/§2）：USGS 的 "M 4.7 - 221 km NE of ..."
        # 是机器串不是专门报道该事件的独立文章；4.5 级无伤亡也不构成重大突发。
        if not re.search(r"[\u4e00-\u9fff]", it.title) or it.shape == "结构化":
            mag = re.search(r"(?:^M\s*|\b)(\d+(?:\.\d+)?)\s*级?地震?", it.title)
            big = (mag and float(mag.group(1)) >= 7.0) or \
                bool(re.search(r"死亡|伤亡|中国公民|国家级应急响应|重大灾害", t))
            if not big:
                tr.decisions.append({
                    "标题": it.title[:60], "命中词组": "—", "reject 分支": "独立原文准入",
                    "keep 分支": "—", "主体规模闸": "不适用", "国际三类": "非三类重大突发",
                    "判定": "剔除", "原因": "结构化小震 / 非中文独立原文，无重大突发事实字段",
                    "来源": it.sources, "URL": it.url, "发布时间": it.published})
                continue
        protect = _hits(t, PROTECT)
        a_hits = _hits(t, ACHIEVEMENT)
        f_hits = _hits(t, FAR_FUTURE)
        u_hits = _hits(t, UNRELATED)
        mass = _hits(t, MASS_TOPIC)
        hot = _hits(t, NATIONAL_HOT)
        reject_branch = ("成就宣传" if a_hits else "远期规划" if f_hits
                         else "无关机构动态" if u_hits else "")
        keep_branch = ("MASS_TOPIC_HIT" if mass else "NATIONAL_HOT" if hot else "")
        reason, ok = "", True

        # 防误伤保护清单优先：即时可行动民生信息一律保留（01 §2 判据澄清）
        if reject_branch and protect:
            reject_branch, keep_branch = "", "防误伤保护清单"
        elif reject_branch and strict == "off":
            reject_branch = ""
        elif reject_branch:
            ok, reason = False, f"三类强剔除：{reject_branch}（命中 {','.join((a_hits or f_hits or u_hits)[:3])}）"
            n_reject[reject_branch] += 1
        elif not keep_branch:
            # 截图 44：把「我的词表没覆盖」当成「不贴近大众」硬剔除，是 13 条
            # 窗口内稿件塌成主表 0 条的直接原因。词表再大也覆盖不了所有民生稿，
            # 尤其窗口右界未到、当批只采到十几条时，一刀切就等于当天不交付。
            # 改为：正面清单只做加权/留痕（_daily 用 TABLE_R 独立现算，未命中
            # 天然 daily_score=0、编排排后面），不再作为准入否决项。
            # 三类强剔除 / F1–F4 / 国际三类门禁 / 主体规模闸全部保留，
            # 「学习贯彻」「焦点访谈」这类政务宣传仍进不来。
            keep_branch = "未命中正面清单（降权不剔除）"
            reason = "窗口内可核对、非宣传稿；未命中贴近度词表，降权保留"
            soft_only.append(it)
        if mass or hot:
            n_mass_hit += 1

        # 主体规模闸：只作用于企业经营恶化类，规模只认原文可核对字段
        subject = ""
        if ok and any(w in t for w in "倒闭 资不抵债 破产重整 退市 大面积关店 清算".split()):
            ev = _hits(t, "上市公司 股票代码 交易所公告 资产负债率 营收 员工人数 门店数 职工债权 债权人数 三季末".split())
            if ev:
                subject = "通过（" + "、".join(ev[:2]) + "）"
                n_subject_gate["通过"] += 1
            else:
                ok, reason = False, "主体规模准入未通过：原文无上市/知名/大型可核对字段"
                subject = "剔除（无规模字段）"
                n_subject_gate["剔除"] += 1

        # 国际三类门禁：境外主体信号前置，只留三类重大突发
        intl_tag, intl_reason = "", ""
        geo = _hits(it.title, INTL_GEO)      # 主体信号只认标题，防摘要夹带国名
        is_intl = bool(geo) or it.group == "国际动态"
        if ok and is_intl:
            war, dis, acc = (_hits(t, INTL_WAR), _hits(t, INTL_DISASTER),
                             _hits(t, INTL_ACCIDENT))
            neutral = _hits(t, INTL_NEUTRAL)
            cross_border = _hits(t, "跨境航班 进口药断供 涉中国公民 跨境价格 供应突变".split())
            if war:
                intl_tag = "战争与武装冲突"
            elif dis:
                intl_tag = "灾害与公共卫生"
            elif acc:
                intl_tag = "突发事故与重大安全事件"
            elif cross_border:
                intl_tag = "民生例外（直接波及境内）"
            else:
                intl_tag = "非三类重大突发"
                ok, reason = False, f"国际动态仅三类重大突发：{','.join((neutral or geo)[:3])}"
                intl_reason = "中性国际资讯"
                n_intl["剔除"] += 1
            if ok and intl_tag in n_intl:
                n_intl[intl_tag] += 1
                n_intl["重要"] += 1
                n_intl["保留"] += 1
            intl_reason = intl_reason or ("、".join((war or dis or acc)[:2]) or "三类门禁通过")
            tr.intl_decisions.append({
                "标题": it.title[:60], "境外信号": "、".join(geo[:3]),
                "三类标签": intl_tag, "判定": "保留" if ok else "剔除",
                "依据": intl_reason, "来源": it.sources})
        elif is_intl is False:
            intl_tag = "非国际条目"

        # F1–F4：F4 三条件缺一不可
        if ok:
            f1 = _hits(t, F1)
            f23 = _hits(t, F2_F3)
            soft = _hits(t, SOFT_H)
            has_unit = bool(UNIT_NUM.search(t))
            if f1 and not protect:
                ok, reason = False, "F1 纯规划 / 条例 / 征求意见（与三类强剔除不重复计数）"
            elif soft and not _hits(t, [w for _, _, ws in I_TIERS for w in ws]) and not has_unit:
                ok, reason = False, "F4 软文 / 特写 / 成就抒情（三条件同时成立）"
            elif f23 and not protect and not mass:
                ok, reason = False, "F2/F3 单市区县非紧急 / 纯地方政务局部节点"

        it.intl_tag = intl_tag
        it.admit_hits = "、".join((mass or hot)[:4])
        it.reject_branch = reject_branch
        it.keep_branch = keep_branch
        tr.decisions.append({
            "标题": it.title[:60], "命中词组": it.admit_hits or "—",
            "reject 分支": reject_branch or "—", "keep 分支": keep_branch or "—",
            "主体规模闸": subject or "不适用", "国际三类": intl_tag,
            "判定": "通过" if ok else "剔除", "原因": reason or "正面清单命中",
            "来源": it.sources, "URL": it.url, "发布时间": it.published})
        if ok:
            kept.append(it)
    if not kept and items:
        # 极端兜底：若将来有人把某条软闸重新收紧、导致窗口内稿件被全数打回，
        # 仍从「仅因未命中贴近度词表」的条目里按新鲜度补足最少 1 条，
        # 绝不放宣传稿进来（那些条目根本不进 soft_only）。
        fallback = sorted(soft_only, key=lambda x: str(x.published), reverse=True)[:8]
        for it in fallback:
            it.keep_branch = "窗口内可见子集兜底"
            kept.append(it)
        tr.counters["空表兜底"] = {
            "触发": True, "补入": len(fallback),
            "说明": "准入全数打回时仅补窗口内非宣传稿；三类强剔除条目一律不补"}
    else:
        tr.counters["空表兜底"] = {"触发": False, "补入": 0,
                                "说明": "准入正常通过，未触发兜底"}
    tr.counters.update({"贴近度档位": strict, "正面清单命中": n_mass_hit,
                        "三类强剔除": n_reject, "主体规模准入": n_subject_gate,
                        "国际动态": n_intl, "准入通过": len(kept),
                        "准入剔除": len(items) - len(kept),
                        "降权保留（未命中贴近度词表）": len(soft_only)})
    return kept


# ───────────────── 2. 事件层聚类（同事件多渠道合并）────────────────
def _bigrams(s: str) -> set[str]:
    s = re.sub(r"[^\u4e00-\u9fff0-9.]", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _similar(a: Item, b: Item) -> tuple[bool, str]:
    ta, tb = a.title, b.title
    ga, gb = _bigrams(ta), _bigrams(tb)
    j = len(ga & gb) / max(1, len(ga | gb))
    na = set(re.findall(r"\d+(?:\.\d+)?\s*(?:级|人|亿元|公里|元/克|%|万人)", ta))
    nb = set(re.findall(r"\d+(?:\.\d+)?\s*(?:级|人|亿元|公里|元/克|%|万人)", tb))
    shared_anchor = bool(na & nb)
    shared_words = len(ga & gb)
    if j >= 0.62:
        return True, f"2-gram Jaccard {j:.2f} ≥0.62"
    if shared_anchor and shared_words >= 2:
        return True, f"共享数字锚点 {','.join(sorted(na & nb))[:20]} + 中文实词 {shared_words} 个"
    return False, f"Jaccard {j:.2f} / 锚点 {bool(na & nb)}"


def cluster_events(items: list[Item], tr: Trace) -> list[Item]:
    """准入后保守聚类，统一事件主键；同事件多渠道来源合并、cross_score 计入编排。
    候选池不参与本层（文档 01 §3）。被合并渠道一条不丢，全进附录 12b。
    """
    roots: list[Item] = []
    for it in items:
        it.eid_raw = it.event_key or re.sub(r"\s+", "", it.title)[:24]
        hit = None
        for r in roots:
            same, why = _similar(it, r)
            tr.cluster_pairs.append({"A": r.title[:40], "B": it.title[:40],
                                     "同事件": same, "理由": why})
            if same:
                hit = r
                break
        if hit is None:
            it.event_key = f"e{len(roots) + 1:04d}"
            roots.append(it)
            continue
        # 合并真实出现过的渠道名，不编造、不去重计数标记
        raw_a, raw_b = hit.sources, it.sources
        merged, seen = [], set()
        for c in channels_of(hit) + channels_of(it):
            if c not in seen:
                seen.add(c)
                merged.append(c)
        if merged:
            hit.sources = " · ".join(merged)
        tr.merged_channels.append({"事件主键": hit.event_key, "代表条": hit.title[:50],
                                   "被合并条": it.title[:50], "合并后渠道": hit.sources,
                                   "合并前渠道 A/B": f"[{raw_a}] + [{raw_b}]",
                                   "原始 eid": it.eid_raw})
    for r in roots:
        r.n_channels = max(1, len(channels_of(r)))
        r.record_type = "多渠道合并" if r.n_channels > 1 else "独立原文"
    tr.counters["事件层聚类"] = {"聚前": len(items), "聚后": len(roots),
                                "多渠道事件": sum(1 for r in roots if r.n_channels > 1)}
    return roots


# ───────────────── 3. 议题簇去重（与事件层严格分离）────────────────
def story_dedup(items: list[Item], tr: Trace, *, enabled: bool = True,
                max_keep: int = 1) -> list[Item]:
    """主体词只认标题、动作词可标题或摘要；≥1 主体且主体+动作合计 ≥2 不同词才归簇。
    每簇保留 Final_adj 最高代表条，其余完整内容进附录 17；议题层不参与 cross_score。
    """
    if not enabled:
        tr.counters["议题簇去重"] = {"开关": "story_dedup=false", "簇数": 0, "合并条数": 0}
        return items
    buckets: dict[str, list[Item]] = {}
    for it in items:
        for name, (subjects, actions) in STORY_GROUPS.items():
            s = _hits(it.title, subjects)
            a = _hits(it.title + it.abstract, actions)
            if s and len(set(s + a)) >= 2:
                buckets.setdefault(name, []).append(it)
                break
    keep, merged_n = [], 0
    for name, group in buckets.items():
        if len(group) <= max_keep:
            keep.extend(group)
            continue
        # 同议题保留信息量最大的事件主体稿，不让人物特写/侧记当代表条
        REP = ("乘组确定 新闻发布会 发射 公布 通报 发布 调整 实施 回应 名单 "
               "典型案例 开赛 发布会将于 服务体系 覆盖率").split()
        group.sort(key=lambda x: (-(x.final_adj + (18 if _hits(x.title, REP) else 0)),
                                  -len(_hits(x.title, STORY_GROUPS[name][0]))))
        rep = group[:max_keep]
        for r in rep:
            keep.append(r)
        for other in group[max_keep:]:
            merged_n += 1
            tr.story.append({"议题": name, "代表条": rep[0].title[:50],
                             "被合并条": other.title[:50], "来源": other.sources,
                             "摘要首段": other.abstract.split("\n")[0][:60],
                             "命中主体": "、".join(_hits(other.title, STORY_GROUPS[name][0])[:3]),
                             "命中动作": "、".join(_hits(other.title + other.abstract,
                                                     STORY_GROUPS[name][1])[:3])})
    for it in items:
        if it not in keep and not any(x["被合并条"] == it.title[:50] for x in tr.story):
            keep.append(it)
    tr.counters["议题簇去重"] = {"开关": f"story_dedup=true / story_max_keep={max_keep}",
                                "簇数": len(buckets), "合并条数": merged_n,
                                "默认词组组": list(STORY_GROUPS)}
    return keep


# ───────────────── 4. 打分（数值不可为编排结果改动）────────────────
def score_all(items: list[Item], tr: Trace, window: tuple[datetime, datetime],
              *, h_channel_closed: bool = True) -> list[Item]:
    """I/C 档位 → T 百分位 → V 六维 → H 关闭 → Final → 等级 → gin/cap/Final_adj。
    等级按原始 Final 机械映射 S/A/B/C/D；核实三态封顶；质量惩罚逐条留痕。
    """
    mid = window[0] + (window[1] - window[0]) / 2
    ages: list[float] = []
    for it in items:
        # RFC822（RSS pubDate）、ISO、%Y-%m-%d 统一走 clean.parse_pub；
        # 旧版只用 fromisoformat，中新网 "Mon, 23 Nov 2026 ..." 全解析失败，
        # 导致 T 全部回退中位数、评分只有 2 个唯一值。
        pub = clean.parse_pub(str(it.published or ""))
        it.T = abs((mid - pub).total_seconds()) / 3600 if pub else -1.0
        if pub:
            ages.append(it.T)
    med_age = sorted(ages)[len(ages) // 2] if ages else 12.0
    lo, hi = (min(ages), max(ages)) if ages else (0.0, 24.0)
    for it in items:
        age = it.T if it.T >= 0 else med_age
        it.T = round(25.0 * (1.0 - (age - lo) / max(1e-6, hi - lo)), 2)

    for it in items:
        t = it.title + it.abstract
        # I：单条取最高一档、不叠加
        it.I_func, it.I_hit = 0, ""
        for val, label, words in I_TIERS:
            h = _hits(t, words)
            if h:
                it.I_func, it.I_hit = val, f"{label}（{','.join(h[:2])}）"
                break
        it.I_base = 5 if _hits(t, AFFECTED) else 3
        it.I = min(20, it.I_base + it.I_func)
        # C：全国性 / 区域性 / 本地性
        it.C_func = 3 if _hits(t, NATIONWIDE) else 2 if _hits(t, REGIONAL) \
            else 1 if _hits(t, LOCAL) else 0
        it.C = min(10, 1 + it.C_func)
        # A / M / V核实 / 质量惩罚
        it.A = {"A": 15.0, "B": 8.0, "C": 3.0}.get(it.evidence, 8.0)
        it.M = min(12.0, (it.n_channels - 1) * 3)
        stale = it.T < 0 or "2025" in str(it.published) or "2024" in str(it.published)
        it.verify_state = "未核实" if it.evidence == "C" else \
            "部分核实" if it.evidence == "B" else "已核实"
        it.Vv = {"已核实": 15.0, "部分核实": 8.0, "未核实": 3.0}[it.verify_state]
        pen = 0
        if stale:
            pen -= 8
        if re.search(r"[\d.]+\s*(?:级|人|元)", it.title) and \
                not any(x in it.abstract for x in re.findall(r"[\d.]+\s*(?:级|人|元)", it.title)):
            pen -= 5
        if it.n_channels == 1 and not _hits(it.sources, "中新网 新华网 人民网 央视 央广 财联社 澎湃 国家 中国".split()):
            pen -= 5
        if _hits(t, STATIC):
            pen -= 4
        if not it.published:
            pen -= 3
        it.pen = pen
        it.V = round((0.24 * it.T + 0.18 * it.I + 0.14 * it.A + 0.10 * it.C
                      + 0.06 * it.M + 0.10 * it.Vv) / 0.82 * 5 + pen, 2)
        it.final = round(max(0.0, min(100.0, it.V / 82.32 * 100)), 1)
        g = "S" if it.final >= 85 else "A" if it.final >= 70 else \
            "B" if it.final >= 55 else "C" if it.final >= 40 else "D"
        if it.verify_state == "部分核实" and g in ("S", "A", "B"):
            g = "C"
        if it.verify_state == "未核实" and g in ("S", "A"):
            g = "B"
        it.grade = g
        it.cap = 4 if it.I_func >= 7 else 3 if it.I_func >= 4 else 2
        tr.I_hits.append({"标题": it.title[:50], "I 命中档位": it.I_hit or "便民提示(0)",
                          "I_base": it.I_base, "I_func": it.I_func, "I": it.I,
                          "C_func": it.C_func, "C": it.C, "T": it.T,
                          "A": it.A, "M": it.M, "V核实": it.Vv, "惩罚": pen,
                          "V": it.V, "Final": it.final, "等级": it.grade})
    # gin：大类内按 Final 降序、按事件主键去重计数
    for g in {it.group for it in items}:
        group = sorted([x for x in items if x.group == g], key=lambda x: -x.final)
        seen, k = set(), 0
        for it in group:
            if it.event_key in seen:
                it.gin = k
            else:
                k += 1
                seen.add(it.event_key)
                it.gin = k
    for it in items:
        it.final_adj = round(it.final - LAMBDA * min(it.gin - 1, it.cap), 1)
    order = sorted(items, key=lambda x: (-x.final_adj, -x.final))
    for i, it in enumerate(order, 1):
        it.score_order = i
    tr.counters["H 通道"] = "关闭：>70% 条目无平台后台阅读/播放/讨论数，P/H 置 NULL；Final 仅反映价值分 V" \
        if h_channel_closed else "开启"
    tr.counters["评分公式"] = ("V=(0.24T+0.18I+0.14A+0.10C+0.06M+0.10V核实)/0.82×5+惩罚；"
                              "Final=V/82.32×100；等级 S≥85/A≥70/B≥55/C≥40/D<40；"
                              "gin 大类内 Final 降序按事件主键去重；cap=4/3/2；"
                              "Final_adj=Final−6×min(gin−1,cap)")
    return items


# ───────────────── 5. 编排（只改展示序，不改任何评级数值）────────────────
def _impact(it: Item, tr: Trace) -> None:
    t = it.title + it.abstract
    p1 = 3 if _hits(t, NATIONWIDE) else 2 if _hits(t, REGIONAL) else 1 if _hits(t, LOCAL) else 0
    p2 = 2 if _hits(t, NATIONWIDE + REGIONAL) else 1 if _hits(t, LOCAL) else 0
    sev = bool(SEV_RE.search(t)) or bool(_hits(t, "国家级应急响应 红色预警 橙色预警 大范围中断".split()))
    p3 = 3 if sev or _hits(t, "正在发生 即时行动 需立即".split()) else \
        2 if _hits(t, "已发生 影响持续 持续影响 恢复运行".split()) else 1
    p4 = 2 if _hits(t, ACTION) else 1
    it.p1, it.p2, it.p3, it.p4 = p1, p2, p3, p4
    it.impact = round(min(12, p1 * 1.5 + p2 + p3 * 1.5 + p4), 1)
    it.impact_why = (f"人群{p1}({'全国' if p1==3 else '区域' if p1==2 else '本地' if p1==1 else '不明确'}) "
                     f"地域{p2} 紧迫{p3}{'(伤亡/响应)' if sev else ''} 可行动{p4}")
    tr.impact4.append({"标题": it.title[:50], "①人群范围": p1, "②地域范围": p2,
                       "③紧迫与严重": p3, "④可行动性": p4,
                       "impact_score": it.impact, "依据": it.impact_why,
                       "原文可核对字段": "、".join(_hits(t, ACTION + NATIONWIDE + ["级地震", "死亡", "预警"])[:4]) or "标题事实"})


def _daily(it: Item) -> None:
    t = it.title + it.abstract
    groups, words = [], []
    for name, ws in TABLE_R:
        h = _hits(t, ws)
        if h:
            groups.append(name)
            words.extend(h)
    it.daily_hit = "、".join(groups) or "—"
    it.daily_score = min(12, 4 * len(groups) + min(4, len(set(words))))


def _arrange(items: list[Item], n: int, cap: int, first: Item | None = None) -> list[int] | None:
    """DFS：前 n 位相邻大类不同、单类 ≤cap、同细标签 ≤1；首位可由置顶池指定。"""
    groups = [it.group for it in items]
    tags = [it.tag for it in items]
    used, path, budget = [False] * len(items), [], [300000]
    counts: dict[str, int] = {}
    tag_seen: set[str] = set()

    def dfs(last: str | None) -> bool:
        if len(path) == n:
            return True
        if budget[0] <= 0:
            return False
        for i, g in enumerate(items):
            if used[i] or g.group == last:
                continue
            lim = 4 if g.group == "国际动态" else cap
            if counts.get(g.group, 0) >= lim:
                continue
            if len(path) + 1 < n and tags[i] in tag_seen:
                continue
            budget[0] -= 1
            used[i] = True
            counts[g.group] = counts.get(g.group, 0) + 1
            tag_seen.add(tags[i])
            path.append(i)
            if dfs(g.group):
                return True
            path.pop()
            tag_seen.discard(tags[i])
            counts[g.group] -= 1
            used[i] = False
        return False

    if first is not None:
        i = items.index(first)
        used[i] = True
        counts[first.group] = 1
        tag_seen.add(first.tag)
        path.append(i)
        if dfs(first.group):
            return list(path)
        return None
    return list(path) if dfs(None) else None


def orchestrate(items: list[Item], tr: Trace, *, enabled: bool = True,
                head_window: int = 3, interleave: bool = True) -> list[Item]:
    """覆盖影响面 → U → 置顶池 → 头部三席 → 前窗打散；只改 seq，不动评级数值。"""
    if not enabled:
        out = sorted(items, key=lambda x: -x.final_adj)
        for i, it in enumerate(out, 1):
            it.seq = i
        tr.counters["编排"] = "orchestrate=false，回纯 Final_adj 降序"
        return out
    max_adj = max([it.final_adj for it in items] or [1.0]) or 1.0
    for it in items:
        _impact(it, tr)
        _daily(it)
        it.cross = min(12, (it.n_channels - 1) * 3)
        ratio = max(0.0, min(1.0, it.final_adj / max_adj))
        intl_add = 6 if it.intl_tag in ("战争与武装冲突", "灾害与公共卫生",
                                        "突发事故与重大安全事件") else 0
        it.U = round(it.daily_score + it.cross + it.impact + 8 * ratio + intl_add, 2)
        it.head_score = round(1.8 * it.daily_score + 1.5 * it.cross
                              + 1.2 * it.impact + 6 * ratio, 2)

    # 置顶池（事件级）：特别严重突发 ∪ 多渠道高覆盖（≥5 家独立信源）
    severe, multi = [], []
    for it in items:
        t = it.title + it.abstract
        m = SEV_RE.search(t)
        big = False
        if m:
            mag = m.group(1)
            dead = m.group(2) or m.group(3)
            hurt = m.group(4) or m.group(5)
            big = (mag and float(mag) >= 7.0) or (dead and int(dead) >= 10) or \
                  (hurt and int(hurt) >= 30)
        if not big:
            big = bool(_hits(t, "国家级应急响应 跨省红/橙预警 大范围灾害 重大公共卫生事件".split()))
        if big:
            severe.append(it)
            it.top_pool = "特别严重突发池"
        elif it.n_channels >= 5:
            multi.append(it)
            it.top_pool = "多渠道高覆盖池"
        else:
            it.top_pool = "未入池"
    severe.sort(key=lambda x: (-x.impact, -x.head_score, -x.n_channels))
    multi.sort(key=lambda x: (-x.n_channels, -x.head_score))
    first = (severe or multi or [None])[0]
    tr.top_pool = {"特别严重突发池": len(severe), "多渠道高覆盖池": len(multi),
                   "首位来源": (first.top_pool if first else "两池皆空，回退头部三席逻辑"),
                   "首位标题": first.title[:60] if first else "（无）"}

    # 按 U 降序；置顶条锁定第 1 位，其余保持 U 降序。
    by_u = sorted(items, key=lambda x: (-x.U, -x.head_score))
    if first is not None:
        by_u = [first] + [x for x in by_u if x is not first]

    # 头部三席：第 1 位来自置顶池；第 2/3 位资格池 cross>0 且 daily_score>0，
    # 逐槽在「上位大类 ≠ 上一条」候选里取 head_score 最高；池不足放宽并留痕。
    head: list[Item] = []
    if first is not None:
        head.append(first)
        tr.head.append({"槽位": 1, "标题": first.title[:50], "上位大类": first.group,
                        "head_score": first.head_score,
                        "资格池": "置顶池（特别严重突发 / 多渠道高覆盖）",
                        "置顶池": first.top_pool})
    elif by_u:
        # 两池皆空：第 1 位取 U 最高，口径正文与附录 16 显式声明回退。
        tr.head.append({"槽位": 1, "标题": "（两池皆空，回退头部三席）", "上位大类": "—",
                        "head_score": "—", "资格池": "—", "置顶池": "两池皆空"})
        head.append(by_u[0])
        tr.head.append({"槽位": 1, "标题": by_u[0].title[:50], "上位大类": by_u[0].group,
                        "head_score": by_u[0].head_score, "资格池": "U 降序回退",
                        "置顶池": by_u[0].top_pool})
    pool = [x for x in by_u if x not in head and x.cross > 0 and x.daily_score > 0]
    relaxed = len(pool) < max(0, head_window - len(head))
    if relaxed:
        pool = [x for x in by_u if x not in head]
    for slot in range(len(head), head_window):
        if not head:
            break
        cand = [x for x in pool if x not in head and x.group != head[-1].group]
        if not cand:
            break                      # 不塞同类：交给前窗 DFS 交错，保证相邻不同
        pick = max(cand, key=lambda x: x.head_score)
        head.append(pick)
        tr.head.append({"槽位": slot + 1, "标题": pick.title[:50],
                        "上位大类": pick.group, "head_score": pick.head_score,
                        "资格池": "放宽（池不足 3 条）" if relaxed else "cross>0 且 daily_score>0",
                        "置顶池": pick.top_pool})

    # 前窗：以 head 为固定前缀，在 U 降序序列上回溯填满 10 位。
    # 相邻同大类是绝对硬约束；单类 ≤cap 从 3 逐级放宽，容量不足如实声明、不编造凑数。
    n = min(10, len(by_u))
    front, used_cap, tag_relaxed = None, None, False
    # 先守「单类 ≤3 + 细标签 ≤1」；再守「单类 ≤3」放宽细标签（如实声明）；
    # 最后才逐级放宽单类上限。容量不足不编造凑数。
    for cap in range(3, 11):
        for strict_tag in (True, False):
            front = _fill_front(head, by_u, n, cap, strict_tag=strict_tag)
            if front is not None:
                used_cap, tag_relaxed = cap, not strict_tag
                break
        if front is not None:
            break
    if front is None:
        used_cap = "无解（全池同一大类）"
        front = (head + [x for x in by_u if x not in head])[:n]
    rest_all = [x for x in by_u if x not in front]
    out = front + rest_all

    for i, it in enumerate(out, 1):
        it.seq = i
        tr.orchestrate.append({
            "U 降序位": next((k + 1 for k, x in enumerate(by_u) if x is it), 0),
            "展示序": i, "标题": it.title[:44], "日常组": it.daily_hit,
            "独立渠道数": it.n_channels, "cross_score": it.cross,
            "impact_score": it.impact, "四维": it.impact_why,
            "置顶池": it.top_pool, "U": it.U, "Final_adj": it.final_adj,
            "动作": "头部" if i <= len(head) else "前窗打散" if i <= 10 else "尾部顺延"})
    for i in range(min(10, len(out)) - 1):
        a, b = out[i], out[i + 1]
        tr.interleave.append({"位置": f"{i+1}→{i+2}", "上位大类A": a.group,
                              "细标签A": a.tag, "上位大类B": b.group, "细标签B": b.tag,
                              "相邻同类": "是" if a.group == b.group else "否",
                              "处理": "错开" if a.group != b.group else "容量不足被迫连续，逐条留痕"})
    adj_pairs = sum(1 for x in tr.interleave if x["相邻同类"] == "是")
    tr.counters["编排"] = (f"orchestrate=true / head_window={head_window} / interleave=true；"
                           f"front_cap 已扣减头部 {len(head)} 席；单类上限实际取 {used_cap}"
                           + ("（容量不足已放宽并如实声明）" if isinstance(used_cap, int) and used_cap > 3 else "")
                           + ("；细标签上限容量不足已放宽并逐条留痕" if tag_relaxed else "")
                           + f"；相邻同大类 {adj_pairs} 对")
    return out


def _fill_front(head: list[Item], ordered: list[Item], n: int, cap: int,
                strict_tag: bool = True) -> list[Item] | None:
    """从 U 降序序列里挑满前 n 位：head 为不可变前缀，相邻大类不同、单类 ≤cap。
    细标签 ≤1 只在 cap≤4 时强制；容量不足放宽时不制造假解。
    """
    if len(head) > n:
        return list(head[:n])
    path, used = list(head), {id(x) for x in head}
    counts: dict[str, int] = {}
    for x in head:
        counts[x.group] = counts.get(x.group, 0) + 1
    tags = {x.tag for x in head}
    budget = [300000]

    def dfs(last: str | None) -> bool:
        if len(path) == n:
            return True
        if budget[0] <= 0:
            return False
        for x in ordered:
            if id(x) in used or x.group == last:
                continue
            lim = 4 if x.group == "国际动态" else cap
            if counts.get(x.group, 0) >= lim:
                continue
            if strict_tag and x.tag in tags:
                continue
            budget[0] -= 1
            path.append(x)
            used.add(id(x))
            counts[x.group] = counts.get(x.group, 0) + 1
            tags.add(x.tag)
            if dfs(x.group):
                return True
            path.pop()
            used.discard(id(x))
            counts[x.group] -= 1
            tags.discard(x.tag)
        return False

    return list(path) if dfs(head[-1].group if head else None) else None
