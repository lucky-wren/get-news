"""统一数据模型：采集条目 RawItem、评级字段与出表列契约。

字段契约来源：资讯渠道大全 §0.3（11 项）+ 每日要闻筛选评级 SKILL §1.3（12 列）。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

# SKILL §5：8 个上位大类（分类只作标签与对称打散维度，不进六维价值分）
GROUPS_8 = [
    "民生服务", "财经市场", "科技与产业", "文娱与热榜",
    "重要赛事", "重大突发与司法", "国际动态", "社会治理与养老",
]

# 样例台账里出现过的扩展大类 -> SKILL 8 类（legacy_groups=true 时保留原值）
GROUP_ALIAS = {
    "社会治理": "社会治理与养老",
    "工厂与制造业": "科技与产业",
    "社会治理与养老": "社会治理与养老",
}

COLS_12 = [
    "展示序", "Final_adj", "等级", "原始 Final", "分数序", "上位大类",
    "分类标签", "标题", "内容摘要", "来源", "事件主键", "记录类型",
]

COLS_TRACE_15 = [
    "展示序", "标题", "上位大类", "分类标签", "Final_adj", "等级", "原始 Final",
    "分数序", "gin", "cap", "V", "I", "C", "cross_score", "记录类型",
]

# 评级列：候选池一律置破折号
DASH_COLS = {"Final_adj", "等级", "原始 Final", "分数序", "事件主键"}

INTL_TAGS = ["战争与武装冲突", "灾害与公共卫生", "突发事故与重大安全事件"]


@dataclass
class RawItem:
    """一条采集条目。前 11 项为渠道文档 §0.3 的采集字段。"""

    # --- 采集字段 ---
    title: str = ""
    abstract: str = ""                 # 内容摘要（原文全文 / 精简后）
    abstract_body: str = ""            # 从独立原文 URL 补采的正文（清洗后）
    abstract_status: str = ""          # 有效 / 正文补采 / 标题补全 / 未取得（标题复读不冒充摘要）
    source: str = ""                   # 渠道 · 渠道
    url: str = ""                      # 独立原文 URL；词条行填榜单快照页
    pub_raw: str = "未取得"            # 发布时间字面值
    pub: Optional[datetime] = None     # 解析后的 aware datetime
    time_basis: str = ""               # 时间依据
    time_mark: str = ""                # 时效标记
    date_clue: str = ""                # 日期线索（只留痕，不冒充发布时间）
    group: str = ""                    # 上位大类
    tag: str = ""                      # 分类标签
    evidence: str = "B"                # A 独立原文 / B 聚合页拆出需回查 / C 仅话题名
    dedup_key: str = ""                # 采集层 URL+标题

    # --- 采集台账元数据 ---
    seq: int = 0                       # 采集序
    channel: str = ""                  # 渠道名
    channel_id: str = ""               # 渠道编号
    channel_section: str = ""          # 渠道所属节（§1–§31）
    channel_level: str = "☆"           # ★ / ☆ / ✗
    shape: str = "独立原文"            # 独立原文 / 聚合页 / 词条 / 公告 / 时间流 / 结构化
    has_event: bool = True             # 词条是否回查到带事实的独立原文
    record_type: str = "独立原文"       # 出表第 12 列
    board_platforms: List[str] = field(default_factory=list)   # 登上榜单的平台（归一后）
    board_slots: int = 0                                        # 热榜词条个数
    media_slots: int = 0                                        # 有具体文章 URL 的新闻源条目数
    platform_metrics: Dict[str, Any] = field(default_factory=dict)  # 后台播放/阅读/讨论数
    raw: Dict[str, Any] = field(default_factory=dict)           # 保留原始注入内容

    # --- 计算字段（准入 / 评分 / 编排层写入，不回写采集字段） ---
    admitted: bool = True
    reject_reason: str = ""
    reject_stage: str = ""
    intl_important: bool = False
    intl_tag: str = ""
    func_tier: str = ""                # 命中的最高信息功能档位
    func_delta_i: int = 0
    func_delta_c: int = 0
    T: float = 0.0
    I: float = 0.0
    A: float = 0.0
    C: float = 0.0
    M: float = 0.0
    Vv: float = 0.0                    # 可核实度（0–5）
    V: float = 0.0                     # 价值分（0–82.32）
    P: Optional[float] = None
    H: Optional[float] = None
    H_c: Optional[float] = None
    Final: float = 0.0
    grade: str = "D"
    gin: int = 0
    cap: int = 0
    decrement: float = 0.0
    Final_adj: float = 0.0
    score_order: int = 0               # 纯 Final_adj 基准序
    cross_score: float = 0.0
    daily_score: float = 0.0
    U: float = 0.0
    head_score: float = 0.0
    display_order: int = 0
    eid: str = ""
    eid_raw: str = ""
    sid: str = ""
    abstract_source: str = "原文摘要"
    quality_penalty: float = 0.0
    interleave_action: str = ""
    interleave_shift: int = 0

    def text(self) -> str:
        return f"{self.title} {self.abstract}"

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        d["pub"] = self.pub.isoformat() if self.pub else None
        return d


@dataclass
class ChannelStatus:
    """渠道文档 §35 逐渠道状态申报表的一行。"""
    channel: str = ""
    channel_id: str = ""
    section: str = ""
    level: str = "☆"
    fetch_method: str = ""
    hit: str = "否"
    shape: str = ""
    window_count: int = 0
    full_enumeration: str = "否"
    gap: str = ""
    adapter: str = ""

    def row(self) -> List[Any]:
        return [self.channel, self.level, self.fetch_method, self.hit, self.shape,
                self.window_count, self.full_enumeration, self.gap, self.adapter]
