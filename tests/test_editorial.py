"""截图 44/45 与 latest.xlsx 回归：夹具逐字取自用户上传的线上批次，
证明标题—摘要错配与政务/行业稿确实被编辑层拦住，不是自证。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from newsbrief import editorial
from newsbrief.model import Item

ROOT = Path(__file__).resolve().parents[1]


def item(title: str, a1: str, a2: str, sources: str = "中新网", **kw) -> Item:
    abstract = "\u3000\u3000" + a1 + "\n\u3000\u3000" + a2
    return Item(title=title, abstract=abstract, sources=sources, **kw)


BAD_3 = item(
    "陈吉宁被查，上海市委表态",
    "阿拉山口铁路口岸运力升级广交会观察：智能产品成获单“利器”AI长图：礼赞七十七载。",
    "绘就时代华章蓝色粮仓的科技“长征”——海洋探宝。",
    "夸克/搜狗热点 · 今日头条 · 抖音 · 百度 · 微博 · 腾讯新闻早报 / 网易 / 搜狐 / 新浪早报（清单§28）",
)
BAD_13 = item(
    "千万级用户背书，华为Mate X8诠释大折叠旗舰新高度！",
    "实测无界方舟Edge-B：做最强端侧智能，真正成为生产力黄小艺月日千亿模型装进电脑，端侧不再做平替。",
    "Yoky月日AI创作的下一场竞争，是把作品做完AI办公如何真正懂得什么叫工作？",
    "夸克/搜狗热点 · 今日头条 · 百度 · 品玩（清单§13）",
)

NOT_DAILY = [
    item("2026-11-19习近平主持亚太经合组织第三十三次领导人非正式会议并发表重要讲话",
         "这是会议前，习近平同与会的各经济体领导人、代表集体合影。",
         "国家主席习近平在深圳国际交流中心主持亚太经合组织第三十三次领导人非正式会议。",
         "生态环境部"),
    item("学习贯彻党的二十届五中全会精神专栏",
         "出席这次全会的有，中央委员162人，候补中央委员117人。",
         "中央纪律检查委员会委员和有关方面负责同志列席会议。", "中国政府网"),
    item("“防风险 强监管 开新局——金融强国与债券市场高质量发展”主题征文活动优秀文章揭晓",
         "面对复杂多变的内外部形势，我国坚定不移走中国特色金融发展之路。",
         "经双方编辑部初筛及专家评审，本次征文活动最终评选出优秀文章20篇。",
         "中债登 / 上清所（清单§5）"),
    item("上海原油期货合约成交情况一览（20261124北京时间11月24日15点）",
         "近期从欧美到东南亚，针对中国钢铁的贸易限制满天飞。",
         "外部环境越来越差，靠出口消化国内过剩产能难以为继。", "隆众 / 金联创"),
    item("河北省气象局关于2025年度雷电防护装置检测质量考核不合格单位整改复查考核结果的通报",
         "2026年9月，河北省气象局组织对检测质量考核不合格单位的整改情况进行了复查考核。",
         "经复查，1家单位仍不合格，3家单位承诺不再在河北省行政区域内开展业务。",
         "各省气象局与 12379 分站"),
    item("2026年11月24日审査イベントNew2026年度 医疗機器规制と审査の最適化のための協働计画",
         "更新した动画は、準備が整い次第公开します。",
         "募集人数は50名程度とし、参加者は原則として1社につき1名とします。", "FDA"),
]

GOOD = [
    item("冷空气影响持续 四川云南西藏等地有雨雪天气",
         "24日早晨，东北地区及山东、江苏、安徽等地部分地区出现6～10℃降温，局地降幅超过13℃。",
         "江南、华南等地部分地区出现中到大雨，局地暴雨。", "微信搜一搜 · 中新网"),
    item("网络交友被骗压岁钱 警惕针对青少年的小额多次式诈骗",
         "法院综合考虑杨某的犯罪事实、性质、情节及对社会的危害程度，依法以诈骗罪判处杨某有期徒刑六个月。",
         "一是单笔金额小，隐蔽性强，家长和平台都不容易察觉到。", "央视网"),
    item("各地积极拓宽住房公积金使用范围",
         "今年以来，各地积极调整优化住房公积金政策，将住房装修、物业费等纳入提取范围。",
         "新修订的《住房公积金管理条例》施行两个月以来，各地出台优化调整政策约80条。",
         "微信搜一搜 · 今日头条 · 中国经济网 · 央视网"),
    item("神舟二十四号载人飞行任务新闻发布会 瞄准25日08时18分发射",
         "今天（11月24日）上午，神舟二十四号载人飞行任务新闻发布会在酒泉卫星发射中心召开。",
         "发言人介绍，瞄准北京时间11月25日08时18分发射神舟二十四号载人飞船。",
         "夸克/搜狗热点 · 微信搜一搜 · 贴吧 · 抖音 · 央视新闻"),
    item("奖助学金陆续到账 学生需提防这五种诈骗套路",
         "这个阶段也是资助类诈骗的高发窗口。",
         "近期，多地教育、公安部门及高校发布反诈预警。", "中新网"),
]


def test_screenshot_bad_rows_fail_correspondence():
    for bad, label in ((BAD_3, "序3 陈吉宁"), (BAD_13, "序13 华为Mate X8")):
        ok, why = editorial.title_abstract_correspondence(bad.title, bad.abstract)
        assert not ok, f"{label} 不该判为对应：{why}"
        assert why, "必须给出可核对的拒绝原因"


def test_good_rows_pass_correspondence_and_public_interest():
    for g in GOOD:
        ok, why = editorial.title_abstract_correspondence(g.title, g.abstract)
        assert ok, f"误伤大众要闻「{g.title}」：{why}"
        pub, fams, why2 = editorial.public_interest(g.title, g.abstract, g.sources)
        assert pub, f"误伤大众贴近主题「{g.title}」：{why2}"
        assert fams


def test_select_daily_rejects_every_bad_row_and_keeps_good():
    pool = [BAD_3, BAD_13] + NOT_DAILY + GOOD
    main, rejects, stats = editorial.select_daily(pool)
    titles = {x.title for x in main}
    for bad in (BAD_3, BAD_13):
        assert bad.title not in titles, "拼接稿绝不能进主表"
    for nd in NOT_DAILY:
        assert nd.title not in titles, f"非大众要闻不该进主表：{nd.title}"
    for g in GOOD:
        assert g.title in titles, f"大众要闻被误剔：{g.title}"
    assert stats["标题摘要不对应剔除"] >= 2   # 两条截图拼接稿 + 2 条行业稿
    assert stats["非面向大众剔除"] >= 4       # 其余政务/机构/征文由正向准入拦下
    assert len(rejects) == stats["标题摘要不对应剔除"] + stats["非面向大众剔除"]
    assert len(rejects) == len(pool) - len(main)
    fins = [x.final_adj for x in main]
    assert fins == sorted(fins, reverse=True), f"未按重要性排序：{fins}"
    assert [x.seq for x in main] == list(range(1, len(main) + 1))


def test_aggregate_names_never_inflate_score():
    """截图 45 那串 10 个聚合名曾把稿刷到 S 87.9；新口径聚合名不计票。"""
    assert editorial.real_platform_hits(BAD_3.sources) and \
        set(editorial.real_platform_hits(BAD_3.sources)) <= set(editorial.MAIN_PLATFORMS)
    assert "腾讯新闻早报" not in editorial.real_platform_hits(BAD_3.sources)
    for a in ("夸克/搜狗热点", "腾讯新闻早报", "网易", "清单§28", "品玩（清单§13）"):
        assert editorial.is_aggregate(a)
    # 只堆聚合名、无真实平台命中 → 不进主表（对应闸先拒），且不产生虚高 impact
    s = "夸克/搜狗热点 · 腾讯新闻早报 / 网易 / 搜狐 / 新浪早报（清单§28） · 品玩"
    assert editorial.real_platform_hits(s) == []


def test_nine_platforms_real_hits_not_hardcoded():
    """热词不得把 9 平台堆到一条；挂不下的逐平台留缺口。"""
    it = item("黄仁勋重新出任英伟达CEO 股价大涨", "英伟达公司23日宣布恢复黄仁勋总裁职务。",
              "董事会通过复职决定，当日股价上涨。", "中新网")
    topics = [{"title": "黄仁勋重新出任英伟达CEO", "discovery": p}
              for p in ("微博热搜", "今日头条", "快手", "百度热搜", "知乎热榜",
                        "小红书", "微信搜一搜·搜索发现", "百度贴吧", "抖音热榜")]
    log = editorial.augment_platform_hits([it], topics)
    assert sum(1 for v in log.values() if v) <= editorial.MAX_PLATFORMS_PER_ITEM
    assert len(editorial.real_platform_hits(it.sources)) <= editorial.MAX_PLATFORMS_PER_ITEM
    assert not any(editorial.is_aggregate(x) for x in it.sources.split(" · "))


def test_editorial_is_shipped_and_wired():
    """新增模块必须进交付清单、必须真的被主表出表口调用。"""
    man = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert "newsbrief/editorial.py" in man["required"]
    assert "tests/test_editorial.py" in man["required"]
    pipe = (ROOT / "newsbrief" / "pipeline.py").read_text(encoding="utf-8")
    assert "editorial.select_daily(" in pipe
    ver = (ROOT / "newsbrief" / "pipeline.py").read_text(encoding="utf-8")
    assert "⑲ 主表零条标题—摘要不对应" in ver
