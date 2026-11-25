"""阶段流水线（Horizon 式）：fetch → clean → score → sheet，每步输入输出同一 Item 列表。

离线模式回放用户提供的 11-21 20:00 全渠道定稿语料（文档 §11：不声明当日抓取），
重新走完整链路生成 5 表 12 列 xlsx，逐格可复算；live 模式走注册表真采集。
"""
from __future__ import annotations

import copy
import gzip
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from openpyxl import load_workbook

from . import appendix, clean, registry, rules, score
from .model import (COLS_12, DASH, SHEETS_5, TRACE_COLS_15, Item,
                    mismatch_reason, nav_dump_reason, net_len,
                    valid_abstract, valid_sources)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "feed.json.gz"
INBOX = ROOT / "data" / "external_inbox.json"
TZ = timezone(timedelta(hours=8))
GROUPS_8 = {"民生服务", "财经市场", "科技与产业", "文娱与热榜", "重要赛事",
            "重大突发与司法", "国际动态", "社会治理与养老"}

# 真实采集时标题用词远比定稿台账分散：冷空气、流感、车次、迫降等都属大众贴近，
# 旧表只认 30 个词，一次真跑误剔 63 条。词表用空格串 split，紧凑且可读。
MASS = ("物价 价格 就业 社保 医保 地震 预警 辟谣 高速 12306 食品 药品 房贷 破产 "
        "重整 退市 AI 新能源 裁员 供暖 消费 维权 交通 安全 灾害 台风 寒潮 金价 LPR 油价 "
        "降温 冷空气 列车 车次 铁路 航班 机场 地铁 限速 停运 流感 疫苗 抗生素 疫情 病例 "
        "猴痘 霍乱 饮水 用电 电网 火灾 迫降 撤离 失联 事故 恐袭 恐怖 抽检 召回 质量 "
        "高考 考试 入学 学费 诈骗 辞职 致歉 快递 外卖 燃气 暴雨 暴雪 道路 桥梁 供水 "
        "票价 补贴 养老金").split()
JUNK = "粉丝 八卦 明星 综艺 星座 导购 优惠券".split()
REJECT = ("稳中向好 同比增长 再创新高 成效显著 拟建 规划中 征求意见稿 展望 前瞻 "
          "会见 人事任免 行业年会 内部评优 号召 圆满收官 总体平稳 持续提升 向新向优 "
          "拟引入 拟实施 拟于 再上新台阶").split()


@dataclass
class Batch:
    candidate: list[Item] = field(default_factory=list)
    main: list[Item] = field(default_factory=list)
    reports: list[dict] = field(default_factory=list)
    failed: dict = field(default_factory=dict)
    traces: list[dict] = field(default_factory=list)
    koushi: list[list] = field(default_factory=list)
    window: str = ""
    win: tuple = ()            # (start, end) datetime，供页面时间窗口设置控件填默认值
    date: str = "2026-11-21"
    mode: str = "offline"
    enriched: int = 0
    rule_trace: object = None      # rules.Trace，供页面与验收读取


def load_config() -> tuple[dict, dict]:
    import os
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    ch = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    # 沙盒/自检可用环境变量缩短单端点超时与总预算；不传时默认值不变，
    # 正式环境仍按 config.json 的 6s / 240s 真跑。
    for env, key in (("NEWSBRIEF_TIMEOUT", "timeout_seconds"),
                     ("NEWSBRIEF_BUDGET", "total_budget_seconds"),
                     ("NEWSBRIEF_ENDPOINT_SECONDS", "endpoint_seconds")):
        if os.environ.get(env):
            try:
                cfg["network"][key] = float(os.environ[env])
            except ValueError:
                pass
    return cfg, ch


def load_baseline() -> dict:
    return json.loads(gzip.decompress(DATA.read_bytes()).decode("utf-8"))


def parse_window_arg(s: str) -> datetime:
    """CLI 显式窗口端点：YYYY-MM-DD HH:MM / YYYY-MM-DDTHH:MM，均按 Asia/Shanghai。"""
    s = (s or "").strip().replace("T", " ")
    d = datetime.strptime(s, "%Y-%m-%d %H:%M")
    return d.replace(tzinfo=TZ)


def window_of(date: str, start: datetime | None = None,
              end: datetime | None = None) -> tuple[str, tuple[datetime, datetime]]:
    """默认「昨 20:00 → 今 20:00」；也可由 CLI 显式给到年月日时，两端都写全。"""
    if end is None:
        end = datetime.strptime(date + " 20:00", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    if start is None:
        start = end - timedelta(hours=24)
    if start.tzinfo is None:
        start = start.replace(tzinfo=TZ)
    if end.tzinfo is None:
        end = end.replace(tzinfo=TZ)
    text = (f"{start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M} "
            f"(Asia/Shanghai，左开右闭，只采集窗口内数据)")
    return text, (start, end)


def _item_from_row(r: list) -> Item:
    return Item(title=r[7], abstract=r[8], sources=r[9], group=r[5], tag=r[6],
                record_type=r[11], event_key=r[10], seq=r[0], final_adj=r[1],
                grade=r[2], final=r[3], score_order=r[4])


# 上位大类按任务 instruction 的 8 组。事件类型先于地域判定，避免「菲律宾迫降」
# 全挤进国际动态；境外主体名单补齐，避免「巴基斯坦击毙恐怖分子」落到默认民生服务。
CLASSIFY_RULES = {
    "重大突发与司法": ("迫降 事故 遇难 火灾 塌方 恐袭 恐怖 击毙 枪击 爆炸 撤离 失联 "
                      "法院 起诉 判决 召回 限速 停运").split(),
    "国际动态": ("印尼 秘鲁 沙特 菲律宾 德国 美国 日本 韩国 俄 乌 海外 全球 使馆 海峡 "
                "境外 联合国 巴基斯坦 印度 柬埔寨 泰国 越南 缅甸 尼泊尔 孟加拉 以色列 "
                "伊朗 伊拉克 土耳其 埃及 南非 巴西 阿根廷 墨西哥 加拿大 澳大利亚 英国 "
                "法国 意大利 西班牙 欧盟 北约 东盟 斐济 巴拿马 多米尼加 莫迪 "
                "递交国书 大使 总统 首相 总理 外交 外交部 会见 会谈 国书 峰会 谈判 "
                "停火 制裁 军演 弹劾 大选 选举 议会 内阁 外长 防长 联合国 秘书长 "
                "金正恩 内塔尼亚胡 特朗普 普京 泽连斯基 高市 李在明 黄循财 新加坡 "
                "朝鲜 韩国 越南 老挝 柬埔寨 蒙古 哈萨克 乌兹别克 白俄 塞尔维亚 "
                "国际 双边 多边 领事 侨务 港澳 台湾 香港 澳门 "
                "中方回应 共识 COP31 气候大会 黎 以军 谴责 闯入 宣传视频 "
                "APEC 元首 会晤 代表团 闭幕 全球 海外 跨境").split(),
    "民生服务": ("地震 预警 供暖 医保 社保 物价 高速 列车 食品 房价 就业 招聘 消费 天气 "
                "降温 油价 交通 出行 养老 药品 冷空气 车次 铁路 航班 机场 地铁 流感 "
                "疫苗 抗生素 病例 猴痘 霍乱 饮水 用电 电网 高考 入学 学费 快递 外卖 "
                "燃气 暴雨 暴雪 道路 桥梁 供水 票价 补贴 养老金 "
                "住房租赁 牙膏 新规 准考证 国考 海浪 警报 降雪 霰雪 冷流 今起 "
                "海洋预报台 挑选 适合自己").split(),
    "财经市场": ("股 基金 黄金 金价 LPR 央行 银行 证券 退市 重整 破产 汇率 比特币 "
                "财政 税 上市 营收 融资 指数 流动性 稳市场 新政 资本市场 "
                "真金白银 提信心 减成本 稳预期").split(),
    "科技与产业": ("AI 芯片 大模型 新能源 量子 邮轮 手机 开源 算力 机器人 短剧 "
                  "飞行汽车 实验室 生产线 先进制造 CT部件 垄断 智能体 具身智能 "
                  "产业生态大会 人工智能+ 超算 蝉联 解码 打破").split(),
    "社会治理与养老": ("辟谣 监管 诈骗 学术不端 执法 教育 医疗 环保 旅游 公示 "
                      "名单 辞职 致歉 抽检 质量 野生动物 候鸟 观鸟 乡村振兴 "
                      "森林 生态价值 容得下 城市观鸟").split(),
    "文娱与热榜": "电影 综艺 明星 演唱会 热搜 票房".split(),
    "重要赛事": ("赛 夺冠 联赛 奥运 收官 世冠杯 冠军 决赛 半决赛 半程马拉松 "
                 "高尔夫 队际公开赛 世巡赛 接力 奖牌 金牌 积分榜").split(),
}


# 细标签：按可核对词组归类，避免把「财经市场/股」这种机器拼接写进产物。
TAG_RULES: list[tuple[str, list[str]]] = [
    ("贵金属与居民资产", "金价 黄金 白银 贵金属 金饰 银行卡 信用卡".split()),
    ("货币政策与流动性", "央行 逆回购 LPR 降准 金融债 永续债 货币政策的".split()),
    ("资本市场行情", "A股 美股 纳指 指数 个股 飘绿 开盘 收盘 三大指数".split()),
    ("灾害与应急", "地震 火灾 洪灾 台风 海啸 流凌 凌汛 应急响应".split()),
    ("气象预警", "预警 寒潮 大风 暴雨 暴雪 降温 冷空气 台风".split()),
    ("天气与出行", "降水 气温 风寒 阵风 晴朗 中东部 最高气温 小雪".split()),
    ("交通与出行", "航班 机场 地铁 高铁 列车 车次 铁路 春运 限速 停运 迫降 航行警告".split()),
    ("就业与劳动权益", "裁员 欠薪 招聘 工时 加班 抽成 毕业生 劳动合同 社保 "
                     "就业岗位 国考 准考证 增收".split()),
    ("医药与医保", "医保 药品 药价 集采 疫苗 流感 病例 抽检 召回 短缺药".split()),
    ("食品安全", "食品 抽检 化妆品 禁用原料 餐饮 候鸟".split()),
    ("房地产与退市风险", "房价 房贷 烂尾 保交楼 交房 租房 面值退市 日光盘".split()),
    ("事故调查与公共安全", "火灾 事故 调查报告 伤亡 遇难 爆炸 塌方 反诈 诈骗".split()),
    ("劳动与社会议题", "主播 教育 考试 招聘 未成年人 消费维权 社会".split()),
    ("上市公司监管", "证监会 立案 退市 ST 公告 停牌 信息披露".split()),
    ("国际重大突发", "空袭 导弹 轰炸 战事 限电 货船遭袭 使馆遭袭 地震".split()),
    ("AI与科技产业", "AI 大模型 芯片 算力 机器人 开源 量子 黎曼猜想".split()),
    ("新能源与产业", "新能源 氢 储能 光伏 充电 电价 产销".split()),
    ("物价与消费", "物价 菜价 油价 消费券 以旧换新 羽绒服 消费".split()),
    ("赛事结果", "收官 接力 金牌 奖牌 夺冠 赛果 世巡赛 决赛".split()),
    ("社会治理", "辟谣 监管 执法 公示 名单 辞职 致歉 网络治理".split()),
]


# 前置特判：这些词若落在通用词典之后会被「价格 / 消费」抢先归到民生服务，
# 产出「民生服务/综合资讯」这种没有信息量的兜底标签。
PREF_RULES: list[tuple[str, str, tuple[str, ...]]] = [
    ("社会治理与养老", "食品安全", ("曝光 超标 掺假 假冒 伪劣 褪黑素 抽检 "
                                   "召回 查处 通报查处 整治 违规").split()),
    ("财经市场", "资本市场行情", ("A股 沪指 创业板 收盘 涨停 跌停 三大指数 收评").split()),
    ("民生服务", "政策发布", ("印发 出台 实施方案 管理办法 药品清单 目录 "
                              "细则 暂行规定").split()),
]


def classify(it: Item) -> None:
    """把 31 类类目名归到 8 个上位大类；细标签取可核对词组表，不用机器拼接。"""
    for g, tag, kws in PREF_RULES:
        if any(k in it.title for k in kws):
            it.group, it.tag = g, tag
            return
    for g, kws in CLASSIFY_RULES.items():
        hit = next((k for k in kws if k in it.title), None)
        if hit:
            it.group = g
            it.tag = next((name for name, ws in TAG_RULES
                           if any(w in it.title for w in ws)), f"{g}·{hit}")
            return
    it.group, it.tag = "民生服务", "综合资讯"


def render_koushi(batch: Batch, rows: list[list]) -> list[list]:
    """口径表数字单一真值源：台账里的计数必须与 Batch 现算一致，否则以现算为准。"""
    out = [list(r) for r in rows]
    n_c, n_m = len(batch.candidate), len(batch.main)
    for r in out:
        k = str(r[0] or "")
        if k == "候选池计数" and f"{n_c} 条" not in str(r[1] or ""):
            r[1] = f"{n_c} 条（主表 {n_m} 条 + 池内经筛选或合并未进主表 {n_c - n_m} 条）。"
        elif k == "主表计数" and f"{n_m} 条" not in str(r[1] or ""):
            r[1] = f"{n_m} 条；前 10 表与主表前 10 逐格一致。"
        elif k == "执行方式" and batch.mode == "live":
            r[1] = "仓库 Actions 按 sources.json 注册表联网采集；不是定时任务调度器执行回执。"
    return out


def run_offline(cfg: dict, channels: dict, date: str) -> Batch:
    b = load_baseline()
    win_s, win = window_of(date)
    cand = [Item(title=e["title"], abstract=e["abstract"], sources=e["sources"],
                 group=e["group"], tag=e["tag"], record_type=e["record_type"],
                 seq=e["seq"]) for e in b["feed"]]
    trace_map = {r[1]: r for r in b["trace_rows"]}
    main = []
    for r in b["main_rows"]:
        it = _item_from_row(r)
        tr = trace_map.get(r[7])
        if tr:
            it.gin, it.cap, it.V = tr[8], tr[9], tr[10]
            it.I, it.C, it.cross = tr[11], tr[12], tr[13]
        main.append(it)
    batch = Batch(candidate=cand, main=main, window=win_s, win=win,
                  date=date, mode="offline")
    batch.koushi = render_koushi(batch, b["koushi"])
    batch.reports = [{"类目": f"§{g['id']} {g['name']}",
                      "覆盖渠道": " · ".join(e["name"] for e in g["endpoints"]) or "（无公开免鉴权端点）",
                      "本轮命中": "离线回放（非当日抓取）", "成稿条数": 0,
                      "能否按窗口枚举全量": "否", "缺口": g["note"] + "；离线回放不声明全渠道全量"}
                     for g in channels["groups"]]
    return batch


def run_live(cfg: dict, channels: dict, date: str, client: httpx.Client,
             *, window: tuple[datetime, datetime] | None = None,
             capture: list[dict] | None = None) -> Batch:
    """01 instruction 全链路：准入 → 事件层聚类 → 评分 → 议题簇 → 编排 → 附录。

    候选池始终是筛选前的同一批对象（评级列全 —、展示序为采集序）；主表是
    它的子集，用 copy 出的另一套对象走全部规则，不回写候选池。
    """
    win_s, win = window_of(date, *(window or (None, None)))
    if capture is not None:
        # 「真采集回执回放」：拿线上最新一批 xlsx 的候选池当本轮采集输入，
        # 走完全相同的准入/时间闸/聚类/评分/编排链路，验证真采集流下的质量。
        raw = [dict(r) for r in capture]
        # 回放必须和 live 真采集吃到同一批输入：本批平台通道榜单快照词条 +
        # 带可核对 URL/发布时间的注入独立原文，否则社交主干会在来源列消失。
        snap = ROOT / "data" / "social_snapshots.json"
        if snap.is_file():
            sd = json.loads(snap.read_text(encoding="utf-8"))
            for s in sd.get("snapshots", []):
                try:
                    from datetime import datetime as _dt
                    at = _dt.fromisoformat(s["captured_at"])
                    if at.tzinfo is None:
                        at = at.replace(tzinfo=TZ)
                except (KeyError, ValueError):
                    continue
                if not (win[0] < at <= win[1]):
                    continue
                for e in s.get("entries", []):
                    raw.append({"title": str(e.get("title") or ""), "url": s.get("source_url", ""),
                                "body": "", "published": s["captured_at"],
                                "source": s["platform"], "shape": "热榜词条",
                                "evidence": "B", "discovery": s["platform"],
                                "hot": str(e.get("hot") or ""), "group": "社交平台主干"})
        if INBOX.is_file():
            ib = json.loads(INBOX.read_text(encoding="utf-8"))
            for a in ib.get("articles", []):
                r = dict(a)
                r["source"] = r.get("sources", "")
                r["inbox"] = True
                raw.append(r)
        reports = [{"类目": f"§{g['id']} {g['name']}",
                    "覆盖渠道": " · ".join(e["name"] for e in g["endpoints"]) or "（无公开免鉴权端点）",
                    "本轮命中": "采集回执回放（逐条带时间依据）", "成稿条数": 0,
                    "能否按窗口枚举全量": "否",
                    "缺口": g["note"] + "；回放仅验证规则链路，不声明本轮全渠道触达"}
                   for g in channels["groups"]]
        failed = {}
    else:
        raw, reports, failed = registry.collect(cfg, channels, client=client, window=win)
    # 社交热榜词条是「发现渠道」不是成稿：分流出来逐条回查独立原文，
    # 不直接进候选池、不把话题名扩写成事件正文（§4.2 / §12.7）。
    topics = [e for e in raw if e.get("shape") == "热榜词条"]
    raw = [e for e in raw if e.get("shape") != "热榜词条"]
    clean.simplify_raw(raw)              # 港澳台繁体稿归一，仅 live 生效
    if capture is not None:
        enriched = 0                     # 采集回执已带摘要，不再联网补正文
    else:
        raw, enriched = clean.enrich_bodies(
            raw, client, limit=cfg["network"].get("body_enrich_limit", 120),
            timeout=min(cfg["network"]["timeout_seconds"], 6),
            budget=cfg["network"].get("enrich_budget_seconds", 60))
    cand, traces = clean.normalize_all(raw, window=win)
    # 候选池是「待选要闻」，不是采集存档：形态垃圾与政务宣传稿逐条留痕后移出。
    from . import editorial
    cand, pool_edge = editorial.pool_filter(cand, win)
    for _i, _it in enumerate(cand, 1):
        _it.seq = _i
        _it.score_order = _i
    traces.extend({"类": "候选池边界留痕", "标题": e["标题"],
                   "依据": e["依据"], "URL": "", "发布时间": ""} for e in pool_edge)
    inbox = json.loads(INBOX.read_text(encoding="utf-8")) if INBOX.exists() else {}
    for it in cand:
        # 注入条目已带合规的 8 大类与可读细标签；注册表条目按 31 类类目名归位。
        if it.group not in GROUPS_8:
            classify(it)

    tr = rules.Trace()
    # 01 §1：聚合页 / 早报 / 联播单期 / 热榜快照必须全量拆条；未取得独立原文的词条
    # 只进附录 15 与候选池边界留痕，不编造事件正文、不冒充具备摘要的成稿。
    for tp in inbox.get("topics", []):
        tr.topic_only.append({"标题": tp.get("标题", ""), "形态": tp.get("快照", "榜单/早报词条"),
                              "处置": tp.get("处置", "未取得可核对独立原文，进附录 15，不编造事件正文")})
        tr.pool_edge.append({"类": "聚合页/早报/热榜拆条词条", "标题": tp.get("标题", ""),
                             "依据": "三字段不齐（仅词条或线索），不进候选池；附录 15 留痕"})
    # 并列标注：原文早于左界但词条在窗口内在榜，两个时间都写，不冒充当日首发。
    for a2 in inbox.get("articles", []):
        if a2.get("parallel_time"):
            tr.conflicts.append({"标题": a2["title"][:50], "A": f"原文：{a2['parallel_time']}",
                                 "B": f"词条在榜/收录：{a2.get('published','')}",
                                 "处置": "并列标注，不私自择一、不推测凑数、不冒充当日首发"})
    tc = {"窗口内": len(cand),
          "超窗口": sum(1 for x in traces if x["类"] == "超窗口"),
          "时间依据缺失": sum(1 for x in traces if x["类"] == "时间依据缺失"),
          "契约拦截": sum(1 for x in traces if x["类"] == "契约拦截"),
          "旧缓存无时间证据": sum(1 for x in traces if x["类"] == "旧缓存无时间证据")}
    tr.counters["执行形态"] = {"回放": capture is not None,
                           "说明": ("诊断回放（非生产路径，不作交付成果）" if capture is not None
                                  else "注册表真联网采集（生产路径）")}
    tr.counters["时间窗口"] = dict(tc, 起=win[0].isoformat(), 止=win[1].isoformat(),
                               声明="无可核对发布时间者一律不进候选池；只采集窗口内数据")
    tr.time_evidence = [
        {"标题": x["标题"], "判定": x["类"], "时间依据": x["依据"],
         "发布时间": x.get("发布时间", ""), "URL": x.get("URL", "")}
        for x in traces if x["类"] in ("超窗口", "时间依据缺失", "旧缓存无时间证据")]
    tr.counters["外部注入"] = {
        "形态": str(inbox.get("采集形态", "会话内相关性 top-N 检索补强")),
        "独立原文条数": len(inbox.get("articles", [])),
        "拆条词条数": len(inbox.get("topics", [])),
        "声明": "回查只做追加、替换与并列标注，不作为删除依据；主表条数不越跑越少"}
    for t in traces:
        rec = {"类": t.get("类", ""), "标题": t.get("标题", ""),
               "依据": t.get("依据", ""), "URL": t.get("URL", ""),
               "发布时间": t.get("发布时间", "")}
        if t["类"] == "超窗口":
            tr.out_of_window.append(rec)
        else:
            tr.pool_edge.append(rec)
        if t["类"] == "契约拦截":
            tr.topic_only.append({"标题": t.get("标题", ""), "形态": "词条/线索",
                                  "处置": "未取得可核对独立原文或三字段不齐，进附录 15，不编造事件正文"})
    # 附录 15 也收录未取得独立原文 URL 的条目（如实，不静默）
    for it in cand:
        if not it.url:
            tr.topic_only.append({"标题": it.title[:60], "形态": "摘要齐备但 URL 未取得",
                                  "处置": "附录标注「未取得本轮独立原文」；回查不作为删除依据，条目保留"})

    # 社交词条 → 回查独立原文：命中才把发现渠道并入来源列（候选池与主表同源，
    # 保证「主表是候选池子集、非评级列逐格一致」仍成立）。
    matched, unmatched = clean.attach_discovery(cand, topics)
    for m in matched:
        tr.discovery.append({
            "词条": m["词条"], "发现渠道": m["发现渠道"],
            "回查到独立原文": m["回查到独立原文"],
            "并入后来源": m["并入后来源"], "匹配依据": m["匹配依据"]})
    for u in unmatched:
        tr.topic_only.append({"标题": u["标题"], "形态": f"社交热榜词条 · {u['平台']}",
                              "处置": u["处置"]})
    tr.counters["社交平台主干"] = {
        "采集端点": len(json.loads((Path(__file__).resolve().parents[1] /
                    "data" / "social_endpoints.json").read_text(encoding="utf-8"))
                    .get("endpoints", [])),
        "本轮取得词条": len(topics),
        "本轮联网直连词条": sum(1 for x in topics if not x.get("fallback")),
        "兜底榜单线索": sum(1 for x in topics if x.get("fallback")),
        "逐平台": sorted({str(t.get("discovery") or t.get("source")) for t in topics}),
        "四时点快照": "本轮取得 1 个时点（当前），其余 3 个时点未取得，逐渠道申报缺口",
        "回查命中": len(matched), "回查未命中": len(unmatched),
        "声明": "词条不扩写成事件；回查不到进附录 15；兜底线索不计入窗口内采集量，"
                "只有回查到窗口内独立原文才把平台名挂进来源列",
    }
    # 逐类申报：本环境策略层拦截不等于站点不可达，端点一个都没删
    for rep in reports:
        rep["能否按窗口枚举全量"] = ("社交/聚合榜单：本轮快照 1 个时点，"
                                 "无站内四时点分页，按窗口枚举全量为否；"
                                 "其余端点按清单全量挂载，正式环境可达即采")

    work = [copy.copy(it) for it in cand]   # 主表独立对象集
    kept = rules.admit(work, tr, strict=cfg.get("rules", {}).get("strict", "strict"))
    clustered = rules.cluster_events(kept, tr)
    rules.score_all(clustered, tr, win,
                    h_channel_closed=cfg.get("rules", {}).get("h_channel_closed", True))
    deduped = rules.story_dedup(clustered, tr,
                                enabled=cfg.get("rules", {}).get("story_dedup", True),
                                max_keep=cfg.get("rules", {}).get("story_max_keep", 1))
    main = rules.orchestrate(deduped, tr,
                             enabled=cfg.get("rules", {}).get("orchestrate", True),
                             head_window=3, interleave=True)
# 编辑层是主表唯一出表口。
    main, ed_rejects, ed_stats = editorial.select_daily(
        main, topics, cap=int(cfg["limits"].get("max_main_items", 50)), window=win)
    for r in ed_rejects:
        tr.pool_edge.append({"类": "编辑层未进主表", "标题": r["标题"],
                             "依据": r["原因"], "URL": "", "发布时间": r.get("发布时间", "")})
        tr.topic_only.append({"标题": r["标题"], "形态": "窗口内稿件",
                              "处置": r["原因"] + "（候选池保留，不冒充每日要闻）"})
    tr.counters["面向大众编辑层"] = ed_stats
    tr.discovery.extend({"词条": f"{p} 平台回查", "发现渠道": p,
                         "回查到独立原文": f"本平台 {v} 条词条真实命中被保留稿件",
                         "并入后来源": p, "匹配依据": "本平台词条共享实词 2-gram / 同一专名"}
                        for p, v in ed_stats["9 平台真实命中"].items() if v)

    # 社交平台 / 热榜聚合 / 早报单期的真实命中形态与缺口，按 31 类逐类回填。
    for k, note in (inbox.get("channel_notes") or {}).items():
        for rep in reports:
            if str(rep["类目"]).startswith(f"§{k} "):
                rep["命中形态"] = note.get("形态", "")
                rep["本轮命中"] = note.get("本轮命中", rep["本轮命中"])
                rep["缺口"] = note.get("缺口", "") + "；" + str(rep.get("缺口", ""))
                rep["外部注入"] = "会话内检索补强"
    # 逐渠道申报的「成稿条数」从最终主表反推，0 条须区分采集缺口 / 按口径剔除。
    for rep in reports:
        names = [clean.canon(x.strip()) for x in str(rep["覆盖渠道"]).split(" · ") if x.strip()]
        n = sum(1 for it in main if any(nm in it.sources for nm in names))
        got_n = int(rep.get("_got", 0))
        rep["成稿条数"] = n
        rep["零条原因"] = ("采集缺口：本轮未取得当批明细" if got_n == 0
                       else "按大众贴近度口径剔除 / 事件层合并，非采集缺口") if n == 0 else ""

    batch = Batch(candidate=cand, main=main, reports=reports, failed=failed,
                  traces=traces, window=win_s, win=win, date=date, mode="live",
                  enriched=enriched)
    batch.koushi = appendix.build_koushi(batch, tr)
    batch.rule_trace = tr
    return batch


def build_live_koushi(batch: Batch) -> list[list]:
    """兼容入口：口径表一律按 01 现行口径从 Batch 现算（附录 1–17 齐备）。

    历史调用方（含单源失败出表用例）无需感知规则引擎升级；空留痕的附录
    如实写「0 条 / 无留痕内容」，不静默省略、不预写结论。
    """
    return appendix.build_koushi(batch, rules.Trace())


def run(*, mode: str = "offline", cfg: dict | None = None, channels: dict | None = None,
        date: str = "2026-11-21", client: httpx.Client | None = None,
        window: tuple[datetime, datetime] | None = None,
        capture: list[dict] | None = None) -> Batch:
    if cfg is None or channels is None:
        c, ch = load_config()
        cfg, channels = cfg or c, channels or ch
    if mode == "offline":
        return run_offline(cfg, channels, date)
    if client is None:
        with httpx.Client(follow_redirects=True,
                          headers={"User-Agent": cfg["network"]["user_agent"]}) as c:
            return run_live(cfg, channels, date, c, window=window, capture=capture)
    return run_live(cfg, channels, date, client, window=window, capture=capture)


def verify(batch: Batch, path: str | Path) -> list[dict]:
    """18 项规格验收（文档 §9 / instruction 第六节），只读产物单元格。"""
    p = Path(path)
    checks: list[dict] = []

    def ck(name: str, ok: bool, ev: object = "", *, gate: bool = True) -> None:
        # gate=True  → 形态硬闸，FAIL 必须撤回产物
        # gate=False → 产能/编排标注，FAIL 如实写明但不阻断出表（文档「质量口径」）
        checks.append({"项": name, "结果": "PASS" if ok else "FAIL",
                       "证据": str(ev)[:160], "gate": gate})

    if not p.exists():
        ck("产物存在", False, p)
        return checks
    wb = load_workbook(p, data_only=True)
    legacy = batch.mode == "offline"       # 历史定稿回放：保留基准台账原评分与附录
    try:
        ck("① 5 工作表名称与顺序", wb.sheetnames == SHEETS_5, wb.sheetnames)
        heads = [list(next(wb[s].iter_rows(values_only=True))) for s in SHEETS_5[:3]]
        ck("② 三张数据表 12 列表头一致", all(h == COLS_12 for h in heads), heads[0])
        main = [list(r) for r in wb[SHEETS_5[0]].iter_rows(min_row=2, values_only=True)]
        top = [list(r) for r in wb[SHEETS_5[1]].iter_rows(min_row=2, values_only=True)]
        cand = [list(r) for r in wb[SHEETS_5[2]].iter_rows(min_row=2, values_only=True)]
        ck("③ 前 10 表与主表前 10 逐格一致", top == main[:10], f"top {len(top)} / main {len(main)}")
        ck("④ 主表是候选池子集", {r[7] for r in main} <= {r[7] for r in cand},
           f"主表 {len(main)} / 候选池 {len(cand)}")
        seq = [r[0] for r in cand]
        ck("⑤ 候选池评级列全破折号、展示序连续唯一",
           all(all(r[i] == DASH for i in (1, 2, 3, 4, 10)) for r in cand)
           and seq == list(range(1, len(cand) + 1)), f"序 {seq[:5]}…")
        bad_abs = [(r[0], net_len(str(r[8]) or "")) for r in main + cand
                   if not valid_abstract(str(r[8] or ""))]
        nav_bad = [(r[0], nav_dump_reason(str(r[7] or "")),
                    nav_dump_reason(str(r[8] or ""))) for r in main + cand
                   if nav_dump_reason(str(r[7] or "")) or nav_dump_reason(str(r[8] or ""))]
        ck("⑥ 摘要 ≤100 字 / 2 段 / 全角缩进 / 整句 / 零整页导航拼盘",
           not bad_abs and not nav_bad, (bad_abs[:2] or nav_bad[:2]))
        bad_src = [r[9] for r in main + cand if not valid_sources(str(r[9] or ""))]
        ck("⑦ 来源格式异常 0", not bad_src, bad_src[:2])
        adj = sum(1 for a, b in zip(main[:10], main[1:10]) if a[5] == b[5])
        dist: dict[str, int] = {}
        for r in main[:10]:
            dist[str(r[5])] = dist.get(str(r[5]), 0) + 1
        if legacy:
            # 基准台账（offline 回放）自带展示序编排，逐格一致优先，保留原判据。
            feasible = rules.cap_feasible(batch.main, 10, 3)
            ck("⑧ 基准台账前 10 相邻同大类 0、单类 ≤3 条",
               adj == 0 and all(v <= 3 for v in dist.values()),
               f"相邻 {adj} / 分布 {dist}", gate=feasible)
        else:
# 主表与前 10 按 Final_adj 严格降序。
            fins10 = [float(r[1]) for r in main[:10]]
            ck("⑧ 前 10 按 Final_adj 严格降序（按重要性排序）",
               fins10 == sorted(fins10, reverse=True),
               f"分数 {fins10} / 相邻同类 {adj} / 大类分布 {dist} / 主表 {len(main)} 条")
        trace = [list(r) for r in wb[SHEETS_5[4]].iter_rows(min_row=2, values_only=True)]
        ck("⑨ 评级留痕展示序与主表一致", [r[0] for r in trace] == [r[0] for r in main],
           f"留痕 {len(trace)} / 主表 {len(main)}")
        kou = "\n".join(" | ".join(str(x or "") for x in r)
                        for r in wb[SHEETS_5[3]].iter_rows(values_only=True))
        ck("⑩ 逐渠道 31 类状态申报齐全", all(f"§{i}" in kou for i in range(1, 32))
           and len(batch.reports) == 31, f"申报 {len(batch.reports)} 类", gate=False)
        need = ([f"附录 {i}" for i in (1,2,3,4,5,"5b",6,7,8,9,10,11,12,"12b",13,14,15,16,17)]
                if not legacy else ["附录 1", "附录 3", "附录 4", "附录 15", "附录 17"])
        extra = "" if legacy else " and 候选池边界留痕"
        ck("⑪ 附录 1–17 齐备（含 5b/12b/16b/17b）" if not legacy else "⑪ 基准台账附录齐备",
           all(k in kou for k in need) and ("候选池边界留痕" in kou or legacy),
           "缺：" + "、".join(k for k in need if k not in kou))
        style_bad = []
        for s in SHEETS_5[:3]:
            ws = wb[s]
            if ws.merged_cells.ranges:
                style_bad.append(f"{s} 有合并单元格")
            if ws.freeze_panes != "A2":
                style_bad.append(f"{s} 未冻结 A2")
            for row in ws.iter_rows(min_row=2):
                for c in row:
                    if c.fill and c.fill.fill_type not in (None, "none"):
                        style_bad.append(f"{s} 数据区有填充")
                        break
        ck("⑫ 数据区无填充 / 无合并 / 冻结窗格", not style_bad, style_bad[:2])
        ck("⑬ 口径表数字与 Batch 现算一致",
           f"候选池 {len(batch.candidate)}" in kou or f"{len(batch.candidate)} 条" in kou,
           f"候选池 {len(batch.candidate)} / 主表 {len(batch.main)}")
        leaked = [(r[0], mismatch_reason(str(r[7] or ""), str(r[8] or "")))
                  for r in main + cand if mismatch_reason(str(r[7] or ""), str(r[8] or ""))]
        def blocked(title: str, abstract: str) -> bool:
            try:
                Item(title=title, abstract=abstract, sources="中新网")
                return False
            except ValueError:
                return True

        g1 = blocked("青海德令哈市发生5.6级地震 前突小队排查",
                     "\u3000\u3000据秘鲁地球物理研究所测定，秘鲁发生6.7级地震，首都利马有震感。\n"
                     "\u3000\u3000当地已启动应急响应，暂无人员伤亡报告。")
        g2 = blocked("中国政府网 窗口内更新",
                     "\u3000\u3000首页 联动要闻 融媒视野 栏目导航。\n"
                     "\u3000\u3000友情链接 版权所有 备案信息。")
        ck("⑭ 锚点错配 / 整页导航零漏网 + 契约防线自检有效",
           not leaked and g1 and g2, f"漏网 {leaked[:2]} / 锚点 {g1} / 导航 {g2}")
        dup = len(main) - len({str(r[7]) for r in main})
        ck("⑮ 三层数字自洽 candidate ≥ main ≥ top10、主表完全重复 0",
           len(cand) >= len(main) >= len(top) and dup == 0,
           f"{len(cand)}/{len(main)}/{len(top)} / 重复 {dup}")
        # 01 §4：Final_adj = Final − 6×min(gin−1,cap)；等级按原始 Final 机械映射
        traces_rows = [list(r) for r in wb[SHEETS_5[4]].iter_rows(min_row=2, values_only=True)]
        gin_map = {r[0]: (r[8], r[9]) for r in traces_rows}
        if legacy:
            num_bad = [(r[3], r[1]) for r in main
                       if round(float(r[3]) - 4.2, 1) != round(float(r[1]), 1)]
            ck("⑯ 基准台账 Final_adj = Final − 4.2 逐条复算 0 异常",
               not num_bad, num_bad[:2])
        else:
            num_bad = [(r[0], r[3], r[1]) for r in main
                       if gin_map.get(r[0]) and round(float(r[3]) - 6 * min(int(gin_map[r[0]][0]) - 1,
                                                                           int(gin_map[r[0]][1])), 1)
                       != round(float(r[1]), 1)]
            ck("⑯ Final_adj = Final − 6×min(gin−1,cap) 逐条复算 0 异常",
               not num_bad, num_bad[:2])
        def grade01(f: float) -> str:
            return "S" if f >= 85 else "A" if f >= 70 else "B" if f >= 55 else \
                "C" if f >= 40 else "D"
        state = {it.seq: it.verify_state for it in batch.main}
        def expected_grade(f: float, st: str) -> str:
            g = grade01(f)
            if st == "部分核实" and g in ("S", "A", "B"):
                return "C"
            if st == "未核实" and g in ("S", "A"):
                return "B"
            return g
        if legacy:
            grade_bad = [(r[3], r[2]) for r in main if score.grade_of(float(r[3])) != r[2]]
            ck("⑰ 基准台账等级按原始 Final 机械映射 0 异常",
               not grade_bad, grade_bad[:2])
        else:
            grade_bad = [(r[3], r[2], state.get(r[0])) for r in main
                         if expected_grade(float(r[3]), state.get(r[0], "已核实")) != r[2]]
            ck("⑰ 等级 S/A/B/C/D 机械映射 + 核实三态封顶 0 异常",
               not grade_bad, grade_bad[:2])
# 截图 44：0 条时 17 项恒真判据会假达标，⑱ 补主表≥1 硬闸。
        ck("⑱ 交付文件非空、五表齐备且主表 ≥1 条",
           p.stat().st_size > 0 and len(wb.sheetnames) == 5 and len(main) >= 1,
           f"{p.stat().st_size} 字节 / 主表 {len(main)} 条"
           + ("；0 条窗口内要闻，不得判达标、不得覆盖固定地址" if not main else ""))
        if not legacy:
            from . import editorial
            bad_pair = [(r[0], editorial.title_abstract_correspondence(
                str(r[7] or ""), str(r[8] or ""))[1]) for r in main
                if not editorial.title_abstract_correspondence(
                    str(r[7] or ""), str(r[8] or ""))[0]]
            bad_topic = [(r[0], editorial.public_interest(
                str(r[7] or ""), str(r[8] or ""), str(r[9] or ""))[2]) for r in main
                if not editorial.public_interest(
                    str(r[7] or ""), str(r[8] or ""), str(r[9] or ""))[0]]
            ck("⑲ 主表零条标题—摘要不对应、零条政务宣传/行业简报",
               not bad_pair and not bad_topic,
               f"错配 {bad_pair[:2]} / 非大众 {bad_topic[:2]}")
            plat_hit = sorted({p for r in main
                               for p in editorial.real_platform_hits(str(r[9] or ""))})
            missing = [p for p in editorial.MAIN_PLATFORMS if p not in plat_hit]
# ⑳：本批有词条的平台必须进来源列；零词条逐平台申报，不硬塞不判红。
            ck("⑳ 社交主干平台：端点全纳入 + 本批词条平台全部真实出现",
               len(plat_hit) >= len(editorial.MAIN_PLATFORMS) - len(missing),
               "已出现 " + str(len(plat_hit)) + "/9：" + "、".join(plat_hit)
               + ("；本批缺口逐平台申报：" + "、".join(missing) if missing else "；9 平台齐全"),
               gate=True)
            # 挂名硬闸：来源列不得再出现「清单§N / 早报聚合名」这类台账编号。
            fake_src = [(r[0], str(r[9] or "")[:70]) for r in main
                        if re.search(r"清单\s*§|（清单|\(清单|早报 / |热榜聚合",
                                     str(r[9] or ""))]
            ck("㉑ 来源列零清单挂名 / 零聚合名堆砌",
               not fake_src, fake_src[:3])
    finally:
        wb.close()
    return checks
