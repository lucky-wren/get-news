"""截图 44 修复后的三条硬红线（真跑产物 103 条主表暴露的）。

1) 活动/政务宣传稿仍被三类强剔除，不得借「降权保留」混进主表；
2) 正常民生稿不得被误伤（上一轮泛词误伤把主表从 18 砍到 9）；
3) 抖音等 9 个社交主干平台必须能挂进主表来源列；
4) 面向大众的每日要闻按重要性精选，不把窗口内 100+ 条全堆进主表。
"""
from __future__ import annotations

import json
from pathlib import Path

from newsbrief import clean, rules
from newsbrief.model import Item

ROOT = Path(__file__).resolve().parents[1]

PROMO = [
    "决赛结果即将亮相！2026全国智慧医保大赛颁奖仪式倒计时1天",
    "司法部、全国普法办发布第一批全国法治宣传教育创新案例",
    "2026年“E法同行”全国网络法治主题宣传收官总结暨上海站活动在沪启动",
]
NORMAL = [
    "国家医保局公布新版医保目录 多种药品报销比例调整",
    "多地推行落叶缓扫 留住初冬城市景观",
    "青海德令哈市发生5.6级地震 青藏铁路部分区段限速",
]


def _abstract(t: str) -> str:
    return "\u3000\u3000" + t + "，相关进展已发布说明。\n\u3000\u3000公众可查询具体安排。"


def _items(titles):
    return [Item(title=t, abstract=_abstract(t), sources="中新网",
                 published="2026-11-24 10:00", shape="独立原文") for t in titles]


def test_activity_promo_still_rejected():
    tr = rules.Trace()
    assert rules.admit(_items(PROMO), tr) == [], "活动宣传稿借降权保留混进主表"
    assert all("三类强剔除" in d["原因"] for d in tr.decisions), tr.decisions


def test_normal_news_not_killed_by_overbroad_words():
    tr = rules.Trace()
    kept = rules.admit(_items(NORMAL), tr)
    assert len(kept) == len(NORMAL), (
        f"正常稿被误剔：{[d['原因'] for d in tr.decisions]}")


def test_douyin_topic_bridges_into_source_column():
    """抖音快照词条 → 本批神舟独立稿：来源列必须出现「抖音」。"""
    assert "神舟二十四号发射准备就绪" in clean.BRIDGE
    it = Item(title="执行神舟二十四号载人飞行任务的航天员乘组确定 天宫将迎来首位外籍航天员",
              abstract=_abstract("乘组确定"), sources="中新网",
              published="2026-11-24 10:00", shape="独立原文")
    topic = {"title": "神舟二十四号发射准备就绪", "discovery": "抖音",
             "source": "抖音热榜"}
    matched, unmatched = clean.attach_discovery([it], [topic])
    assert matched and not unmatched, "抖音词条没挂接上"
    assert "抖音" in it.sources, it.sources


def test_main_table_is_curated_not_full_archive():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    cap = int(cfg["limits"]["max_main_items"])
    assert 1 <= cap <= 50, f"主表上限 {cap} 不像每日要闻，更像全量存档"
    assert cfg["network"]["concurrent_endpoints"] >= 8
    assert cfg["network"]["total_budget_seconds"] >= 240
    reg = (ROOT / "newsbrief" / "registry.py").read_text(encoding="utf-8")
    assert "ThreadPoolExecutor" in reg and "probe_status" in reg
    assert "catalog_expand" in reg
