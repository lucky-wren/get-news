"""截图 44 回归：候选池 13 / 主表 0 / 页面却报「达标（内容形态与编排全部通过）」。

两个根因，各自锁死：
  1) verify 的 17 项全是「空集恒真」判据，⑱ 只查 xlsx 骨架字节数，
     0 条主表时 18/18 全 PASS → 覆盖固定地址与页面；
  2) rules.admit 把「未命中我的贴近度词表」当成硬剔除，13 条窗口内
     中性稿被剔掉 12 条，当批只采到十几条时主表直接塌成 0。

本文件用真实 pipeline.run + MockTransport，不用诊断回放。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from newsbrief import pipeline, rules, sheet, site
from newsbrief.model import Item

TZ = timezone(timedelta(hours=8))
WIN = (datetime(2026, 11, 23, 20, 0, tzinfo=TZ),
       datetime(2026, 11, 24, 20, 0, tzinfo=TZ))

# 窗口内、契约合规、但刻意不命中 MASS_TOPIC / NATIONAL_HOT 的中性稿
NEUTRAL = [
    "国家图书馆开放新一批古籍数字阅览资源", "多地美术馆公布冬季展览安排",
    "京杭大运河沿线开展文化遗存联合巡查", "一批考古遗址保护工程完成年度验收",
    "出版社发布本年度古籍整理出版目录", "博物馆推出夜间延时开放服务",
    "地方志编纂工作推进会在南京举行", "非遗代表性项目名录完成阶段性复核",
    "城市公共阅读空间新增十二处服务点", "古籍修复技艺培训班结业",
    "沿湖湿地候鸟观测记录完成年度汇总", "图书馆界开展馆藏文献互换协作",
    "历史文化街区保护利用案例汇编印发",
]
PROPAGANDA = ["总书记", "人民情怀", "焦点访谈", "学习贯彻", "宣讲团",
              "权威访谈", "系列重要讲话", "政绩观"]


def _abstract(t: str) -> str:
    return ("\u3000\u3000" + t + "相关工作已按计划推进。\n"
            "\u3000\u3000承办单位发布了本次进展说明。")


def _neutral_items() -> list[Item]:
    return [Item(title=t, abstract=_abstract(t), sources="央视网",
                 published="2026-11-24 10:00", shape="独立原文")
            for t in NEUTRAL]


def test_admit_keeps_neutral_items_instead_of_wiping_them():
    """反证点：旧判据这 13 条只通过 1 条（12 条被『未命中正面清单』打回）。"""
    items = _neutral_items()
    tr = rules.Trace()
    kept = rules.admit(items, tr)
    assert len(kept) >= 10, (
        f"准入把 {len(items) - len(kept)} 条中性稿打回，主表会塌成 0；"
        f"原因分布 {[d['原因'] for d in tr.decisions[:4]]}")
    assert tr.counters.get("空表兜底", {}).get("触发") is False
    assert tr.counters.get("降权保留（未命中贴近度词表）", 0) >= 10
    # 三类强剔除仍然有效：政务宣传稿不得借「降权保留」混进来
    bad = Item(title="学习贯彻党的二十届五中全会精神 焦点访谈专题报道",
               abstract=_abstract("专题报道"), sources="央视网",
               published="2026-11-24 10:00")
    tr2 = rules.Trace()
    assert rules.admit([bad], tr2) == [], "三类强剔除被误放宽"


def _run_13_neutral(tmp_path: Path, monkeypatch):
    """只喂 13 条中性稿：屏蔽注入语料，隔离准入/验收行为。"""
    monkeypatch.setattr(pipeline, "INBOX", tmp_path / "no_inbox.json")
    lst = []
    for i, t in enumerate(NEUTRAL):
        d = datetime(2026, 11, 24, 9 + i % 10, 0, tzinfo=TZ)
        lst.append({"title": t, "brief": _abstract(t).replace("\n", ""),
                    "focus_date": str(int(d.timestamp())),
                    "url": f"https://news.cctv.com/2026/11/24/n{i}.shtml"})
    payload = json.dumps({"data": {"list": lst}}, ensure_ascii=False)
    served = {"cctv": False}
    lock = threading.Lock()      # 并发采集下保证「13 条」只被喂一次

    def handler(req):
        u = str(req.url)
        with lock:
            first = "cmsdatainterface" in u and not served["cctv"]
            if first:
                served["cctv"] = True
        if first:
            return httpx.Response(200, text="news(" + payload + ");",
                                  headers={"content-type": "application/javascript"})
        if "cmsdatainterface" in u:
            empty = json.dumps({"data": {"list": []}})
            return httpx.Response(200, text="news(" + empty + ");",
                                  headers={"content-type": "application/javascript"})
        return httpx.Response(200, text="", headers={"content-type": "text/plain"})

    cfg, ch = pipeline.load_config()
    with httpx.Client(transport=httpx.MockTransport(handler),
                      follow_redirects=True) as c:
        return pipeline.run(mode="live", cfg=cfg, channels=ch,
                            date="2026-11-24", window=WIN, client=c)


def test_production_live_13_neutral_still_delivers(tmp_path, monkeypatch):
    """修复后：只有 13 条中性稿也要出表，不能是 0 条。"""
    b = _run_13_neutral(tmp_path, monkeypatch)
    assert len(b.candidate) >= 13, f"候选池异常 {len(b.candidate)}"
    assert len(b.main) >= 1, "主表仍为 0 条"
    for it in b.main:
        assert not any(w in it.title for w in PROPAGANDA), \
            f"主表混入政务宣传：{it.title}"
    out = tmp_path / "ok.xlsx"
    sheet.build_workbook(b, out, b.koushi, [it.trace_row() for it in b.main])
    checks = pipeline.verify(b, out)
    hard = [c for c in checks if c["结果"] == "FAIL" and c.get("gate", True)]
    assert not hard, f"交付批次硬闸 FAIL：{[c['项'] for c in hard]}"


def test_zero_main_is_hard_fail_not_fake_pass(tmp_path, monkeypatch):
    """截图 44 精确复现 + 修复：把主表强制置 0，⑱ 必须 FAIL(gate=True)。"""
    b = _run_13_neutral(tmp_path, monkeypatch)
    b.candidate = b.candidate[:13]
    b.main = []
    b.failed = {}
    out = tmp_path / "zero.xlsx"
    sheet.build_workbook(b, out, b.koushi, [])
    checks = pipeline.verify(b, out)
    c18 = next(c for c in checks if c["项"].startswith("⑱"))
    assert c18["结果"] == "FAIL", "0 条主表竟然判 PASS（截图 44 假达标复现）"
    assert c18.get("gate", True) is True, "0 条必须是形态硬闸，不是产能标注"
    hard = [c for c in checks if c["结果"] == "FAIL" and c.get("gate", True)]
    assert hard, "0 条没有触发撤回，固定地址与页面会被空表覆盖"
    # 反证：旧 ⑱ 只看文件大小/表数，在这个空表上恒真——证明新判据确实抓住了
    from openpyxl import load_workbook
    wb = load_workbook(out, data_only=True)
    old_expr = out.stat().st_size > 0 and len(wb.sheetnames) == 5
    wb.close()
    assert old_expr is True, "旧判据在空表上本应为 True（正是漏网原因）"


def test_site_quality_never_reports_ok_on_empty_main(tmp_path, monkeypatch):
    """第二层防御：即使绕过验收直接生成页面，0 条也不能渲染成绿色达标。"""
    b = _run_13_neutral(tmp_path, monkeypatch)
    b.candidate = b.candidate[:13]
    b.main = []
    b.failed = {}
    out = tmp_path / "zero.xlsx"
    sheet.build_workbook(b, out, b.koushi, [])
    checks = pipeline.verify(b, out)
    docs = tmp_path / "docs"
    site.build_site(b, checks, docs)
    raw = (docs / "news-data.js").read_text(encoding="utf-8")
    q = json.loads(raw.split("=", 1)[1].rstrip().rstrip(";"))["quality"]
    assert q["level"] != "达标", f"0 条页面仍报达标：{q}"
    assert "主表 0 条" in q["text"]
    assert any("主表 0 条" in u for u in q["unmet"])


def test_main_cli_rejects_zero_batch(monkeypatch):
    """静态锁死：0 条时 __main__ 必须 return 1 且不覆盖固定地址/页面。"""
    src = (pipeline.ROOT / "newsbrief" / "__main__.py").read_text(encoding="utf-8")
    assert "if hard:" in src and "return 1" in src
    assert "本批主表 0 条" in src and "保留上一批" in src
    assert 'if batch.mode == "live" and not diagnose:' in src
