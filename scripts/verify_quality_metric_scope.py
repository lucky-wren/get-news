# -*- coding: utf-8 -*-
"""采集质量门禁「口径方向」回归：真调 compute_quality，不用静态字符串自证。

2026-11-22 线上事故续：上一轮加了编辑部硬闸 E1–E5 与内容硬门禁，
但两处指标口径与修复方向**相反**，会让「垃圾清干净」反而必然触发硬失败：

  ① 非新闻噪声率 = F5剔除 / (F5剔除 + 主表)。硬闸拦下的 SEO/黄页/剧透
     不进 noise_rows 却让 main 变小 → 比值变大（线上 0.3253 就是这么来的）。
  ② 词条回查成功率 = 成稿词条 / (成稿词条 + 未回查线索)。按需求⑤
     「回查不到进附录 15」本就是正确行为，全算失败分母 → 数学上趋近 0。

本脚本用线上真产物语料构造主表，逐项断言新口径：
  - 成品噪声率按最终主表逐条复检 editorial_check，干净表 = 0、垃圾表 > 0；
  - 同样的 F5 剔除数下，移除垃圾后噪声率必须下降（旧口径会上升）；
  - 词条回查按「真实回查尝试」计，附录 15 留痕线索不再稀释；有线索零回查仍判 0。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from news_pipeline.quality import compute_quality  # noqa: E402

CORPUS = BASE / "data" / "online_quality_corpus_20261121.json"
CHECKS: list[dict] = []


def add(name: str, ok: bool, evidence: str = "") -> None:
    CHECKS.append({"项": name, "结果": "PASS" if ok else "FAIL", "证据": evidence})
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} — {evidence[:160]}")


def as_item(d: dict, *, shape="独立原文", evidence="B"):
    return SimpleNamespace(
        title=d.get("标题", ""), abstract=d.get("内容摘要", ""),
        abstract_body="", source=d.get("来源", ""),
        record_type=d.get("记录类型", "联网检索独立原文"),
        channel_id=d.get("事件主键", ""), group=d.get("上位大类", "民生服务"),
        tag=d.get("分类标签", "综合"), url="", pub=None,
        seq=d.get("展示序", 0), raw={}, shape=shape, has_event=True,
        abstract_status="有效", evidence=evidence, grade=d.get("等级", "—"))


class _Registry:
    def summary(self):
        return {"白名单渠道总数": 599, "已触达渠道数": 96}

    def table(self):
        return ["示例渠道 · 待配置端点"] * 3


def build_R(main, *, f5_rows=0, clues_n=0, sa_stats=None, admitted=None):
    return {
        "ordered": main, "candidate": list(main),
        "admitted": admitted if admitted is not None else list(main),
        "registry": _Registry(),
        "logs": {
            "noise_rows": [["x"]] * f5_rows,
            "clues": [{"词": "x"}] * clues_n,
            "search_api": {"stats": sa_stats or {}, "trace": [], "providers": []},
            "score": {}, "live_cn": {}, "story": {},
            "mass": [], "f_rows": [], "intl_rows": [], "bankruptcy_rows": [],
            "social_main": {}, "platform_access": {}, "digest_rows": [],
            "intl_counts": {}, "excluded": [], "estimated": [],
            "field_reject": [], "contract_reject": [], "dedup_dropped": [],
            "pairs": [], "merged": [], "story_rows": [], "failures": [],
            "article": {}, "second_pass": {}, "search_bodies": {},
            "source_expansion": {}, "heat": {}, "skipped": [],
            "channel_block_traces": [], "channel_block_hit": 0,
            "target": {}, "failure_categories": {}, "budget": {},
            "event_composer": [],
        },
    }


CFG = {
    "source": {"mode": "inbox"},   # 采集侧四项标不适用，内容侧仍逐条实测
    "quality": {
        "max_noise_ratio": 0.10, "min_lookup_ratio": 0.80,
        "warn_only": True,
        "hard_fail_metrics": [
            "A级独立原文占比", "词条回查成功率", "非新闻噪声率", "摘要有效率",
            "摘要长度合规率", "分类一致率", "段落重复率", "议题重复率"],
    },
    "rules": {"mass_strictness": "strict"},
    "orchestrate": {"enabled": True},
}


def m(q, name):
    return (q["指标"][name]["实测"], q["指标"][name]["达标"])


def main() -> int:
    d = json.loads(CORPUS.read_text(encoding="utf-8"))
    clean = [as_item(x) for x in d["正例"]]
    dirty = [as_item(x) for x in d["负例"]]

    # ① 干净主表：成品噪声率必须为 0（不是把采集侧剔除数硬塞回分母）
    q_clean = compute_quality(build_R(clean, f5_rows=27, clues_n=50,
                                      sa_stats={"成稿": 0, "失败": 0}), CFG)
    n_clean = m(q_clean, "非新闻噪声率")[0]
    add("干净主表成品噪声率 = 0（逐条复检编辑部硬闸，非硬编码）",
        n_clean == 0.0,
        f"F5 仍剔除 27 条，成品噪声率={n_clean}；旧口径会是 27/(27+8)=0.771")
    add("复检函数真被调用（成品噪声样本为空、诊断字段齐全）",
        q_clean.get("成品噪声检出") == 0
        and "采集侧F5剔除占比（诊断）" in q_clean
        and q_clean["采集侧F5剔除占比（诊断）"] > 0.7,
        f"成品噪声检出={q_clean.get('成品噪声检出')} | "
        f"采集侧F5占比(仅诊断)={q_clean.get('采集侧F5剔除占比（诊断）')}")

    # ② 3 条线上垃圾混进主表：必须检出并触发内容硬失败
    q_dirty = compute_quality(build_R(clean + dirty, f5_rows=27, clues_n=50,
                                      sa_stats={"成稿": 0, "失败": 0}), CFG)
    n_dirty = m(q_dirty, "非新闻噪声率")[0]
    add("线上 3 条负例进主表时噪声率 > 0 且判内容硬失败",
        n_dirty > 0 and "非新闻噪声率" in q_dirty["内容硬失败项"]
        and not m(q_dirty, "非新闻噪声率")[1],
        f"噪声率={n_dirty} | 内容硬失败项={q_dirty['内容硬失败项']} | "
        f"样本={q_dirty.get('成品噪声样本')[:2]}")
    add("硬闸失效时结论为「不达标」而不是告警放行",
        q_dirty["结论"].startswith("不达标") and not q_dirty["warn_only"],
        f"结论={q_dirty['结论']} | warn_only={q_dirty['warn_only']}")

    # ③ 方向性：F5 剔除数不变，把垃圾从成品移走，噪声率必须下降
    add("移除垃圾后噪声率下降（旧口径会上升，方向修正的核心）",
        n_clean < n_dirty,
        f"含垃圾 {n_dirty} → 清干净 {n_clean}；旧口径 27/(27+11)=0.711 → "
        f"27/(27+8)=0.771 是反的")

    # ④ 词条回查：有线索、零回查尝试 → 0 且判失败（如实暴露「没做回查」）
    q_zero = compute_quality(build_R(clean, clues_n=50,
                                     sa_stats={"成稿": 0, "失败": 0}), CFG)
    l_zero = m(q_zero, "词条回查成功率")[0]
    add("有线索却零回查 → 0.0 且判失败（根因是没做回查，不是线索太多）",
        l_zero == 0.0 and "词条回查成功率" in q_zero["未达标项"],
        f"实测={l_zero} | 诊断 回查尝试数={q_zero.get('回查尝试数')}")

    # ⑤ 真实尝试 8 次、成稿 6 条：6/(6+2)=0.75，不再被 50 条附录 15 线索稀释
    words = [as_item(x, shape="词条") for x in d["正例"][:6]]
    q_hit = compute_quality(
        build_R(clean, clues_n=50, sa_stats={"成稿": 6, "失败": 2},
                admitted=words), CFG)
    l_hit = m(q_hit, "词条回查成功率")[0]
    add("按真实回查尝试计：6 成稿 / (6+2 失败) = 0.75，不被附录15线索稀释",
        abs(l_hit - 0.75) < 1e-6 and l_hit > 6 / (6 + 50),
        f"实测={l_hit} | 旧口径只会是 6/(6+50)={6/56:.4f}")

    # ⑥ 尝试了但全失败（线上 keyless 43 次 0 成稿）→ 仍 0，不放水
    q_fail = compute_quality(
        build_R(clean, clues_n=50, sa_stats={"成稿": 0, "失败": 43}), CFG)
    l_fail = m(q_fail, "词条回查成功率")[0]
    add("43 次尝试 0 成稿仍判 0（新口径没有放水）",
        l_fail == 0.0 and "词条回查成功率" in q_fail["未达标项"],
        f"实测={l_fail} | 回查失败数={q_fail.get('回查失败数')}")

    # ⑦ 本批无词条业务 → 1.0 不适用，不因「没做」被罚
    q_na = compute_quality(build_R(clean, clues_n=0, sa_stats={}), CFG)
    l_na = m(q_na, "词条回查成功率")[0]
    add("无线索且零尝试 → 1.0（不适用，不是虚高也不是虚低）",
        l_na == 1.0, f"实测={l_na}")

    # ⑧ 全程无 NameError / KeyError（noise 变量改名后的回归）
    add("compute_quality 全程无异常且返回诊断字段完备",
        all(k in q_clean for k in ["成品噪声检出", "回查尝试数",
                                   "采集侧F5剔除占比（诊断）", "口径说明"]),
        f"返回字段 {len(q_clean)} 个 | 口径说明含方向修正="
        f"{'方向相反' in q_clean['口径说明']}")

    # ⑨ 数据层联动：evidence 升 A 只能靠真补到正文，短路分支不再固定压成 6 篇
    import re as _re
    from pathlib import Path as _Path
    lc_src = (_Path(BASE) / "news_pipeline" / "live_cn.py").read_text(encoding="utf-8")
    bd_src = (_Path(BASE) / "news_pipeline" / "budget.py").read_text(encoding="utf-8")
    add("短路后正文补采按全局硬墙余量足量（不再固定 6 篇 / 4s）",
        "min(int(_lv.get(\"max_articles\", 60)), 6)" not in lc_src
        and "_left * 0.55" in lc_src
        and '_lv["body_workers"] = 8' in lc_src
        and "待补条目" in lc_src,
        "线上 353.7s/600s 剩余 246.3s 却只补 6 篇 → evidence 全 B 封顶 ≤54.9 → A 恒空")

    # ⑩ 行为：enrich_bodies 在 60 条缺摘要条目下真能补到足量正文（mock 网络）
    from unittest.mock import patch
    from news_pipeline import live_cn as _lc

    class _Win:
        def contains(self, pub):
            return True

    specs = [{"url": f"https://news.example.com/a/{i}.html", "pub": None,
              "abstract": "", "raw": {"feed_id": f"src{i % 6}"},
              "source": f"src{i % 6}", "basis": "测试"}
             for i in range(60)]
    cfg_live = {"source": {"live": {"fetch_article": True, "max_articles": 60,
                                    "article_timeout": 6,
                                    "body_deadline_seconds": 20,
                                    "body_workers": 8}}}
    with patch.object(_lc, "fetch_body",
                      side_effect=lambda u, timeout: "据中国地震台网正式测定，"
                      "11月21日18时45分青海海西州德令哈市发生5.6级地震，震源深度10千米，"
                      "相关地区暂无人员伤亡报告，应急部门已启动核查。"):
        bres = _lc.enrich_bodies(specs, _Win(), cfg_live)
    add("缺摘要 60 条真补到足量正文（evidence 才可能升 A）",
        bres.get("正文补采成功", 0) >= 50 and bres.get("候选", 0) >= 50,
        f"候选 {bres.get('候选')} / 成功 {bres.get('正文补采成功')} / "
        f"失败 {bres.get('补采失败（如实留空）')}")

    # ⑪ 行为：层配额用尽不再误报成「全局预算耗尽」
    from news_pipeline import budget as _bd
    b = _bd.Budget({"budget": {"enabled": True, "total_collect_seconds": 600,
                               "export_reserve_seconds": 25,
                               "layer_seconds": {}}})
    b.skipped = ["rss"]
    b.expired = False
    concl = b.report()["结论"]
    add("层配额用尽与全局硬墙到点分开写（不再误导下游降级）",
        "层配额用尽" in concl and "全局预算耗尽" not in concl,
        f"结论={concl}")

    p = BASE / "output" / "quality_metric_scope_verify.json"
    p.write_text(json.dumps({"检查数": len(CHECKS),
                             "PASS": sum(1 for c in CHECKS if c["结果"] == "PASS"),
                             "FAIL": sum(1 for c in CHECKS if c["结果"] == "FAIL"),
                             "失败项": [c["项"] for c in CHECKS if c["结果"] == "FAIL"],
                             "检查": CHECKS}, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    ok = all(c["结果"] == "PASS" for c in CHECKS)
    print(f"\n{sum(1 for c in CHECKS if c['结果'] == 'PASS')}/{len(CHECKS)} PASS → {p}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
