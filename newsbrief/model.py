"""12 列数据契约（文档 §5 / §7 / §10）。

照 NewsNow `NewsItem` 的「必填极窄」：只有 title / abstract / sources 必填，
格式在构造期校验；其余字段由清洗、评分、编排阶段补齐。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

COLS_12 = ["展示序", "Final_adj", "等级", "原始 Final", "分数序", "上位大类",
           "分类标签", "标题", "内容摘要", "来源", "事件主键", "记录类型"]
SHEETS_5 = ["处理后的数据", "排名最靠前的10条", "窗口内全部数据",
            "口径与稿外信息", "评级留痕"]
TRACE_COLS_15 = ["展示序", "标题", "上位大类", "分类标签", "Final_adj", "等级",
                 "原始 Final", "分数序", "gin", "cap", "V", "I", "C",
                 "cross_score", "记录类型"]
DASH = "—"

BAD_SRC = re.compile(
    r"https?://|www\.|\||｜|\.(?:com|cn|org|net|jp|gov)|\d+、|未取得|待补|N/A|等\d+家")
ENDINGS = ("。", "！", "？", "；")
PARALLEL = ("另测", "另报", "同区域", "并列", "分别", "中方", "USGS", "GDACS", "同时")

# 整页导航 / 菜单拼盘判据：生成端（Item）与验收端（verify）共用。
# 真实站点首页经 trafilatura 抽出的是「无障碍阅读模式 · 首页 · 栏目 · 备案」，
# 格式检查（2 段 / 缩进 / 整句）查不出「这根本不是一条新闻」。
_NAV_WORDS = ("无障碍", "阅读模式", "首页", "友情链接", "版权所有", "备案",
              "主办单位", "承办单位", "栏目", "网站地图", "登录", "注册",
              "客户端", "扫码", "分享到", "导航", "更多>>", "长者模式")
_FAKE_TITLE = re.compile(r"(?:窗口内更新|站内更新|首页更新)\s*$")
# 站点壳 / JS 未渲染 / 反爬拦截页：GDACS 首页被抓回来就是
# `register/login YouneedtoenableJavaScripttorunthisapp.`，旧版当成一条新闻进候选池。
_SHELL = re.compile(
    r"register\s*/?\s*login|enable\s*javascript|javascript\s*to\s*run|"
    r"you\s*need\s*to\s*enable|access\s*denied|403\s*forbidden|"
    r"service\s*unavailable|please\s*enable\s*cookies|cloudflare|"
    r"captcha|人机验证|请开启\s*javascript|浏览器不支持|"
    r"autoplay\s*:|delaytime\s*:|更多视频新闻|新闻直播间|点击即玩|"
    r"更多视频\s*\+|朝闻天下解码|扫码下载|打开app",
    re.I)


def nav_dump_reason(text: str) -> str:
    """返回「这不是独立原文」的原因；空串表示看起来是正文。"""
    t = str(text or "")
    if not t:
        return ""
    if _SHELL.search(t):
        return "站点壳/JS 未渲染/反爬拦截页（不是新闻正文）"
    if _FAKE_TITLE.search(t):
        return "伪造标题（把整页首页冒充成稿）"
    hits = [w for w in _NAV_WORDS if w in t]
    if len(hits) >= 3:
        return f"整页导航/菜单拼盘（栏目词簇 {len(hits)} 个）"
    seps = len(re.findall(r"[\s·×|｜]", t))
    punct = len(re.findall(r"[，。！？；,.]", t))
    if seps >= 12 and punct <= 1 and hits:
        return "并列栏目串（无句读的菜单）"
    return ""


# 标题里粘了来源/相对时间/纯日期后缀（「证券时报9小时前」「土豆味小哲· 11月20日」
# 「…2026年11月11日」），是抓取时把页面元数据并进了标题，不是稿件的真实标题。
_TITLE_DIRT = re.compile(
    r"(?:\d+\s*(?:小时|分钟)前|20\d{2}年\d{1,2}月\d{1,2}日|"
    r"\d{1,2}月\d{1,2}日)\s*$|·\s*\d{1,2}月\d{1,2}日\s*$")


def title_dirt_reason(t: str) -> str:
    return "标题粘了来源/相对时间/日期后缀（非稿件真实标题）" if _TITLE_DIRT.search(t or "") else ""


def net_len(s: str) -> int:
    return len(re.sub(r"[\s\u3000]", "", s or ""))


def valid_abstract(a: str) -> bool:
    """2 段 / LF 单换行 / 每段 2 个全角空格缩进 / 净字数 ≤100 / 整句收束。"""
    ps = (a or "").split("\n")
    return (len(ps) == 2
            and all(p.startswith("\u3000\u3000") for p in ps)
            and all(p.rstrip().endswith(ENDINGS) for p in ps)
            and net_len(a) <= 100)


def valid_sources(s: str) -> bool:
    """`渠道 · 渠道`，不带编号/竖线/链接/等N家；单渠道不带分隔符。"""
    if not (s or "").strip() or BAD_SRC.search(s):
        return False
    parts = s.split(" · ")
    return all(p.strip() and " · " not in p for p in parts)


def mismatch_reason(title: str, abstract: str) -> str:
    """标题/摘要强锚点错配。并列标注（§17）不算错配，不误伤。"""
    t = (title or "") + (abstract or "")
    if any(w in t for w in PARALLEL):
        return ""
    mag_t = re.findall(r"(\d+(?:\.\d+)?)\s*级地震", title or "")
    mag_a = re.findall(r"(\d+(?:\.\d+)?)\s*级地震", abstract or "")
    if mag_t and mag_a and mag_t[0] != mag_a[0]:
        return "震级不一致"
    # 地名不再用泛正则：中文里「退市 / 旅游市场 / 上市」都会被误当成地名。
    # 锚点只认带单位的硬数字，且并列标注（§17）一律不算错配。
    dead_t = re.findall(r"死亡\s*(\d+)\s*人", title or "")
    dead_a = re.findall(r"死亡\s*(\d+)\s*人", abstract or "")
    if dead_t and dead_a and dead_t[0] != dead_a[0]:
        return "死亡人数不一致"
    return ""


@dataclass
class Item:
    title: str
    abstract: str
    sources: str
    group: str = "民生服务"
    tag: str = "综合"
    record_type: str = "多渠道合并"
    url: str = ""
    published: str = ""
    time_basis: str = ""        # 可核对的时间依据（原文首发 / 回执采集时刻 / 并列标注）
    evidence: str = "A"
    shape: str = "独立原文"
    event_key: str = ""
    # 主表评分（候选池保持 0 / DASH）
    seq: int = 0
    final: float = 0.0
    final_adj: float = 0.0
    grade: str = ""
    score_order: int = 0
    gin: int = 1
    cap: int = 4
    V: float = 10.0
    I: int = 5
    C: int = 6
    cross: int = 0
    # ── 01 定时任务 instruction 现行全文（最高仲裁优先级）所需分量 ──
    T: float = 0.0            # 发布时间距窗口中点的样本自适应百分位 0..25
    A: float = 0.0            # 证据等级分
    M: float = 0.0            # 多渠道分
    Vv: float = 0.0           # 核实三态分
    pen: int = 0              # 质量惩罚合计（负值）
    verify_state: str = "已核实"
    I_base: int = 5
    I_func: int = 0
    I_hit: str = ""
    C_func: int = 0
    # 编排层（只影响展示序，不改任何评级数值）
    impact: float = 0.0
    p1: int = 0
    p2: int = 0
    p3: int = 0
    p4: int = 0
    impact_why: str = ""
    daily_score: float = 0.0
    daily_hit: str = ""
    U: float = 0.0
    head_score: float = 0.0
    top_pool: str = ""        # 特别严重突发 / 多渠道高覆盖 / 未入池
    intl_tag: str = ""        # 国际三类门禁标签
    eid_raw: str = ""         # 事件层聚类前的原始主键
    admit_hits: str = ""      # 准入逐条命中词组
    reject_branch: str = ""
    keep_branch: str = ""
    n_channels: int = 1

    def __post_init__(self) -> None:
        if not (self.title or "").strip():
            raise ValueError("标题为空")
        if not valid_abstract(self.abstract):
            raise ValueError(f"摘要不合规：2 段/全角缩进/≤100 字/整句收束 → {self.title[:24]}")
        if not valid_sources(self.sources):
            raise ValueError(f"来源格式不合规：{self.sources[:40]}")
        m = mismatch_reason(self.title, self.abstract)
        if m:
            raise ValueError(f"契约拒绝：{m} → {self.title[:24]}")
        n = (nav_dump_reason(self.title) or nav_dump_reason(self.abstract)
             or title_dirt_reason(self.title))
        if n:
            raise ValueError(f"契约拒绝：{n} → {self.title[:24]}")

    def row(self, *, main: bool) -> list:
        """候选池评级列全破折号；主表写入评分。"""
        if not main:
            return [self.seq, DASH, DASH, DASH, DASH, self.group, self.tag,
                    self.title, self.abstract, self.sources, DASH, self.record_type]
        return [self.seq, self.final_adj, self.grade, self.final, self.score_order,
                self.group, self.tag, self.title, self.abstract, self.sources,
                self.event_key or f"e{self.seq:04d}", self.record_type]

    def trace_row(self) -> list:
        return [self.seq, self.title, self.group, self.tag, self.final_adj,
                self.grade, self.final, self.score_order, self.gin, self.cap,
                self.V, self.I, self.C, self.cross, self.record_type]
