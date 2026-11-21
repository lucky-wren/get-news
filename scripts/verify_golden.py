# -*- coding: utf-8 -*-
"""get-news · 目标样例契约 + 事件成稿层往返复现（交付前必跑，且真的产出 xlsx）。

不再只跑「自己写的验收器报全绿」。本脚本做三件可肉眼核对的事：

  1. 逐格读用户给的目标样例 xlsx，把「34/34 多渠道、34/34 两段、e21/s13、
     3 种记录类型、A9、候选池 73 含 46 社交线索」抽成机器可读契约；
  2. 用目标样例的候选池 73 行**拆回独立报道** → 喂给新造的 event_composer →
     重新聚成事件、合并来源、抽两段、判类型与 e/s 主键 → 输出
     `output/golden_replay.xlsx`。这是往返一致性验证，不是抄结果；
  3. 用线上 latest.xlsx 的 327 行真实候选池跑同一个成稿器 →
     `output/latest_recomposed.xlsx`，如实回答「这批数据改造后能到什么水平」。

产物是可以直接打开对比的五工作表 xlsx（12 列、候选池评级列全「—」），
不是一份「PASS/FAIL」文本。指标差距写进 `output/golden_diff.json`。

用法：python scripts/verify_golden.py [--json output/golden_diff.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import openpyxl
import yaml
from openpyxl.styles import Alignment, Font, PatternFill

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from news_pipeline.model import RawItem, COLS_12          # noqa: E402
from news_pipeline import event_composer as ec            # noqa: E402
from news_pipeline import rules, scoring                  # noqa: E402
from news_pipeline.timewindow import Window               # noqa: E402

GOLDEN = Path("/data/inputs/daily_news_20261119_20h_社交平台补录版.xlsx")
CURRENT = Path("/data/inputs/latest.xlsx")
if not GOLDEN.exists():
    GOLDEN = BASE / "spec" / "daily_news_20261119_20h_社交平台补录版.xlsx"
if not CURRENT.exists():
    CURRENT = BASE / "docs" / "downloads" / "latest.xlsx"

HEAD_FILL = PatternFill("solid", fgColor="DDEBF7")
HEAD_FONT = Font(bold=True)


# ---------------------------------------------------------------- 兼容读取
def load_wb(path: Path):
    """目标样例 docProps/custom.xml 的 vt 命名空间未声明，openpyxl 直读会炸。"""
    try:
        return openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception:
        tmp = Path(tempfile.mktemp(suffix=".xlsx"))
        with zipfile.ZipFile(str(path)) as zin, zipfile.ZipFile(str(tmp), "w", zipfile.ZIP_DEFLATED) as zout:
            for it in zin.infolist():
                if it.filename == "docProps/custom.xml":
                    continue
                zout.writestr(it, zin.read(it.filename))
        wb = openpyxl.load_workbook(str(tmp), read_only=True, data_only=True)
        tmp.unlink(missing_ok=True)
        return wb


def sheet_rows(wb, idx: int):
    ws = wb[wb.sheetnames[idx]]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    return [r for r in rows if r and any(x is not None and str(x).strip() for x in r)]


def rows_to_items(rows):
    """12 列 → RawItem。评级列（2/3/4/5/11）在候选池是「—」，原样保留为元数据。"""
    out = []
    for i, r in enumerate(rows, 1):
        r = list(r) + [None] * (12 - len(r))
        it = RawItem(
            title=str(r[7] or "").strip(), abstract=str(r[8] or "").strip(),
            source=str(r[9] or "").strip(), group=str(r[5] or "").strip(),
            tag=str(r[6] or "").strip(), record_type=str(r[11] or "").strip(),
            seq=i, channel=str(r[9] or "").strip(),
            shape="词条" if any(k in str(r[9] or "") for k in ec.SOCIAL_HINT) else "独立原文",
            has_event=bool(ec._clean_abstract(str(r[7] or ""), str(r[8] or ""))),
        )
        it.eid = str(r[10] or "").strip()
        it.eid_raw = it.eid[:1] if it.eid else ""
        # 摘要里的「2026年11月20日 15:40:58」只是线索，不反推发布时间
        m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})", it.abstract)
        if m:
            try:
                it.pub = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                  int(m.group(4)), int(m.group(5)),
                                  tzinfo=ZoneInfo("Asia/Shanghai"))
                it.pub_raw = it.pub.isoformat()
                it.time_basis = "摘要内源站时间戳（仅作复现验证输入）"
            except Exception:
                pass
        out.append(it)
    return out


def contract_metrics(rows):
    """从主表 12 列直接算契约指标（不依赖任何项目代码）。"""
    n = len(rows) or 1
    multi = sum(1 for r in rows if " · " in str(r[9] or ""))
    avg = sum(len([x for x in re.split(r"\s*·\s*", str(r[9] or "")) if x.strip()]) for r in rows) / n
    two = sum(1 for r in rows if "\n" in str(r[8] or ""))
    grades = Counter(str(r[2]).strip() for r in rows)
    rtypes = Counter(str(r[11]).strip() for r in rows)
    eids = Counter(str(r[10] or "?").strip()[:1] for r in rows)
    readback = 0
    for r in rows:
        t, a = ec._flat(str(r[7] or "")), ec._flat(str(r[8] or ""))
        if t and a.startswith(t[:min(len(t), 20)]) and len(a) - len(t) < 50:
            readback += 1
    return {
        "条数": len(rows), "多渠道条数": multi, "多渠道率": round(multi / n, 4),
        "平均来源数": round(avg, 2), "两段摘要": two, "两段率": round(two / n, 4),
        "标题复读": readback, "超100净字": sum(1 for r in rows if len(ec._net(str(r[8] or ''))) > 100),
        "等级": dict(grades), "记录类型": dict(rtypes), "主键前缀": dict(eids),
        "净字均值": round(sum(len(ec._net(str(r[8] or ''))) for r in rows) / n, 1),
    }


def pool_metrics(rows):
    n = len(rows) or 1
    srcs = Counter(str(r[9] or "").strip() for r in rows)
    social = sum(1 for r in rows if any(k in str(r[9] or "") for k in ec.SOCIAL_HINT))
    return {"行数": len(rows), "评级列全—": sum(1 for r in rows if str(r[2]).strip() == "—"),
            "来源种类": len(srcs), "社交快照线索": social}


# ---------------------------------------------------------------- 严选 + 评分
def select_and_score(items, cfg, win):
    """用项目真实准入判据严选；评分调 scoring.compute_scores（不抄历史分数）。"""
    admitted, traces = [], []
    for it in items:
        if it.abstract_status not in ("有效", "正文补采"):
            traces.append([it.title, "主表摘要未取得"]); continue
        nok, nrule, nwhy = rules.noise_verdict(it)
        if not nok:
            traces.append([it.title, f"F5 {nrule}: {nwhy[:60]}"]); continue
        mok, _keep, _rej, _why = rules.mass_verdict(it, strictness="strict")
        if not mok:
            traces.append([it.title, "大众贴近度 strict 未通过"]); continue
        iok, _iwhy, _imp, _itag = rules.intl_verdict(it)
        if not iok and it.group == "国际动态":
            traces.append([it.title, "国际非三类（战争/灾害/突发事故）"]); continue
        admitted.append(it)
    try:
        scoring.compute_scores(admitted, win, cfg)
    except Exception as e:
        traces.append(["评分", f"compute_scores 异常 {type(e).__name__}: {str(e)[:80]}"])
    admitted.sort(key=lambda x: -(getattr(x, "Final_adj", 0) or 0))
    for i, it in enumerate(admitted, 1):
        it.display_order = i
        it.score_order = i
    return admitted, traces


def write_xlsx(path, main, pool, traces, meta_lines):
    """五工作表 / 12 列，与规格一致；候选池评级列全「—」、展示序为采集序。"""
    wb = openpyxl.Workbook()
    names = ["处理后的数据", "排名最靠前的10条", "窗口内全部数据", "口径与稿外信息", "评级留痕"]
    ws = wb.active; ws.title = names[0]
    sheets = [ws] + [wb.create_sheet(n) for n in names[1:]]

    def head(w, cols):
        w.append(cols)
        for c in range(1, len(cols) + 1):
            w.cell(1, c).font = HEAD_FONT; w.cell(1, c).fill = HEAD_FILL
            w.cell(1, c).alignment = Alignment(vertical="center", wrap_text=True)

    def row12(it, cand=False, order=0):
        if cand:
            return [order, "—", "—", "—", "—", it.group, it.tag, it.title,
                    it.abstract, it.source, "—", it.record_type]
        return [order, round(getattr(it, "Final_adj", 0) or 0, 2),
                getattr(it, "grade", "D"), round(getattr(it, "Final", 0) or 0, 2),
                getattr(it, "score_order", 0), it.group, it.tag, it.title,
                it.abstract, it.source, it.eid, it.record_type]

    head(sheets[0], COLS_12)
    for i, it in enumerate(main, 1):
        sheets[0].append(row12(it, order=i))
    head(sheets[1], COLS_12)
    for i, it in enumerate(main[:10], 1):
        sheets[1].append(row12(it, order=i))
    head(sheets[2], COLS_12)
    for i, it in enumerate(pool, 1):
        sheets[2].append(row12(it, cand=True, order=i))
    head(sheets[3], ["项", "值", "说明"])
    for k, v, note in meta_lines:
        sheets[3].append([k, str(v), note])
    head(sheets[4], ["事件主键", "记录类型", "簇内条目数", "独立来源数", "来源列", "两段", "数字冲突并列"])
    for t in traces:
        if isinstance(t, dict):
            sheets[4].append([t.get("事件主键", ""), t.get("记录类型", ""),
                              t.get("簇内条目数", ""), t.get("独立来源数", ""),
                              t.get("来源列", ""), "是" if t.get("两段") else "否",
                              t.get("数字冲突并列", "无")])
        elif isinstance(t, list) and len(t) >= 2:
            sheets[4].append(["—", "严选剔除", "", "", t[0][:60], "", t[1][:80]])
    for w in sheets:
        w.column_dimensions["H"].width = 52
        w.column_dimensions["I"].width = 62
        w.column_dimensions["J"].width = 40
        for row in w.iter_rows(min_row=2):
            for c in row:
                c.alignment = Alignment(vertical="top", wrap_text=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))
    return path


# ---------------------------------------------------------------- 主流程
def main() -> int:
    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8")) or {}
    win = Window(datetime(2026, 11, 19, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                 datetime(2026, 11, 20, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                 interval="lo-ro")

    gwb = load_wb(GOLDEN)
    golden_main = sheet_rows(gwb, 0)
    golden_pool = sheet_rows(gwb, 2)
    g_contract = contract_metrics(golden_main)
    g_pool = pool_metrics(golden_pool)
    gwb.close()

    cwb = load_wb(CURRENT)
    cur_main = sheet_rows(cwb, 0)
    cur_pool = sheet_rows(cwb, 2)
    c_contract = contract_metrics(cur_main)
    c_pool = pool_metrics(cur_pool)
    cwb.close()

    # ---- A. 目标候选池往返：拆回独立报道 → 成稿器重聚
    g_items = rows_to_items(golden_pool)
    split = []
    for it in g_items:
        srcs = ec._split_sources(it.source) or [it.source or "未标注"]
        paras = [p.strip() for p in str(it.abstract or "").split("\n") if p.strip()]
        for j, s in enumerate(srcs):
            m = RawItem(title=it.title,
                        abstract=(paras[j] if j < len(paras) else (paras[-1] if paras else "")),
                        source=s, group=it.group, tag=it.tag, seq=len(split) + 1,
                        record_type=it.record_type, pub=it.pub,
                        abstract_status="正文补采" if paras else "未取得",
                        shape="词条" if any(k in s for k in ec.SOCIAL_HINT) else "独立原文",
                        has_event=bool(paras))
            m.eid = it.eid
            split.append(m)
    replay_pool, replay_traces = ec.compose_events(split, cfg=cfg, win=win)
    # 与主链路一致：成稿后必须再跑一次候选池契约三闸（截图1/2/3 的脏行都是成稿层产物）
    from news_pipeline import quality_gate as qg
    replay_pool, replay_rejected = qg.contract_check_many(replay_pool)
    replay_main, sel_traces = select_and_score(replay_pool, cfg, win)
    r_contract = ec.golden_metrics(replay_main)
    r_pool = {"行数": len(replay_pool), "评级列全—": len(replay_pool),
              "来源种类": len({s for x in replay_pool for s in ec._split_sources(x.source)}),
              "社交快照线索": sum(1 for x in replay_pool if x.board_platforms)}

    # ---- B. 线上 327 行真实候选池改造
    l_items = rows_to_items(cur_pool)
    live_pool, live_traces = ec.compose_events(l_items, cfg=cfg, win=win)
    live_pool, live_rejected = qg.contract_check_many(live_pool)
    live_main, live_sel = select_and_score(live_pool, cfg, win)
    l_contract = ec.golden_metrics(live_main)
    l_pool = {"行数": len(live_pool), "评级列全—": len(live_pool),
              "来源种类": len({s for x in live_pool for s in ec._split_sources(x.source)}),
              "社交快照线索": sum(1 for x in live_pool if x.board_platforms)}

    # ---- 真的产出两份可打开的 xlsx
    note = [
        ("批次性质", "规格复现验证批次（非实时抓取）",
         "输入=用户提供的候选池；只验证 event_composer 能否重建目标形态，不冒充当日新闻"),
        ("输入", GOLDEN.name, "目标样例 73 行候选池，按「 · 」拆回独立报道后重新成稿"),
        ("成稿器", "news_pipeline/event_composer.py", "纯规则：3-gram LSH 聚类 / 来源合并 / 两段抽取 / 数字并列 / e-s 主键"),
        ("行业参照", "Chronicle / Fundus / CAMS / Reconciliation Ledger / GDELT Cloud", "见模块 docstring"),
        ("评分", "scoring.compute_scores 真实复算", "不抄目标样例的历史分数"),
        ("契约三闸", f"成稿后拦截 {len(replay_rejected)} 条",
         "占位摘要 / 标题摘要错配 / 破产主体不可核对 / 社交话题不合规，逐条依据见工程留痕 P 口径"),
    ]
    p1 = write_xlsx(BASE / "output/verification/golden_replay.xlsx", replay_main, replay_pool,
                    replay_traces + sel_traces, note)
    note2 = [("批次性质", "线上批次形态改造验证（非实时抓取）",
              "输入=用户上传 latest.xlsx 的 327 行真实候选池，走同一成稿器"),
             ("输入", CURRENT.name, "线上 URL 级候选池"),
             ("说明", "单源 RSS 聚不出 34/34 多渠道",
              "要真达到目标，采集时必须由检索/社交回查提供 ≥2 个独立域名原文，见 pipeline 0a-0"),
             ("评分", "scoring.compute_scores 真实复算", ""),
             ("契约三闸", f"成稿后拦截 {len(live_rejected)} 条",
              "与 pipeline 主链路同一判据，逐条可复算")]
    p2 = write_xlsx(BASE / "output/verification/latest_recomposed.xlsx", live_main, live_pool,
                    live_traces + live_sel, note2)

    keys = ["条数", "多渠道条数", "多渠道率", "平均来源数", "两段摘要", "两段率",
            "标题复读", "超100净字", "净字均值"]
    diff = []
    for k in keys:
        diff.append({"指标": k, "当前线上": c_contract.get(k), "目标样例": g_contract.get(k),
                     "目标往返复现": r_contract.get(k), "线上改造后": l_contract.get(k),
                     "复现达标": (r_contract.get(k, 0) >= g_contract.get(k, 0) * 0.9)
                     if isinstance(g_contract.get(k), (int, float)) and k not in ("标题复读", "超100净字") else
                     (r_contract.get(k, 0) <= g_contract.get(k, 0)) if k in ("标题复读", "超100净字") else None})
    report = {
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "目标样例": GOLDEN.name, "当前线上": CURRENT.name,
        "契约": {"目标主表": g_contract, "目标候选池": g_pool,
               "当前主表": c_contract, "当前候选池": c_pool},
        "复现": {"目标往返主表": r_contract, "目标往返候选池": r_pool,
               "线上改造主表": l_contract, "线上改造候选池": l_pool},
        "逐项对比": diff,
        "目标等级": g_contract["等级"], "当前等级": c_contract["等级"],
        "复现等级": dict(Counter(getattr(x, "grade", "D") for x in replay_main)),
        "线上改造等级": dict(Counter(getattr(x, "grade", "D") for x in live_main)),
        "产物": {"golden_replay": str(p1), "latest_recomposed": str(p2)},
        "契约三闸": {
            "目标往返拦截": len(replay_rejected),
            "目标往返按闸": dict(Counter(x["命中闸"] for x in replay_rejected)),
            "目标往返样本": replay_rejected[:12],
            "线上改造拦截": len(live_rejected),
            "线上改造按闸": dict(Counter(x["命中闸"] for x in live_rejected)),
        },
        "诚实边界": [
            "往返复现证明成稿器逻辑能重建目标形态，不是实时抓取结果",
            "线上 327 行全是单源 URL 级条目，聚一聚无法凭空造出多源证据；真实达标依赖采集层 0a-0 的检索回查",
            "目标样例的 docProps/custom.xml 命名空间损坏，读取时已跳过该文件，不影响 sheet 数据",
        ],
    }
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=str(BASE / "output/golden_diff.json"))
    args = ap.parse_args()
    out = Path(args.json); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print("目标契约 :", json.dumps(g_contract, ensure_ascii=False)[:300])
    print("当前线上 :", json.dumps(c_contract, ensure_ascii=False)[:300])
    print("往返复现 :", json.dumps(r_contract, ensure_ascii=False)[:300])
    print("线上改造 :", json.dumps(l_contract, ensure_ascii=False)[:300])
    print(f"\n产物：{p1}（{len(replay_main)} 主表 / {len(replay_pool)} 候选）")
    print(f"      {p2}（{len(live_main)} 主表 / {len(live_pool)} 候选）")
    print(f"差距报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
