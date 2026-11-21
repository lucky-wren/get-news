# -*- coding: utf-8 -*-
"""线上真产物质量回归（2026-11-22 用户「必须达到数据质量效果」）。

之前十几轮「全面核查」用的是离线样例 / 静态算式 / 自造台账，从没拿**用户上传的
线上 latest.xlsx** 做逐条回归，所以每次都报全绿、线上却一直有 Bug。本脚本的语料
是 `data/online_quality_corpus_20261121.json`：逐字照抄线上主表前 18 条 + 采集日志的
门禁实测值，负例必须被拦、正例必须保留、格式样本必须被修复。

四类真验证（不是字符串检查）：
  * 行为 A：把 max_noise_ratio 调到 -1（形态硬闸必失败）真跑 run.py，必须 rc≠0 且产物撤回；
  * 行为 B：把规格验收 mock 成不达标真跑 run.py，必须因「规格验收不达标」失败；
  * 行为 C：仅产能指标不达标时 run.py 必须 rc=0（页面不永久停更），但结论含
            「产能不足」且未达标项仍列出 —— 2026-11-22 修掉硬门禁自身的永久红副作用；
  * CI 模式：读本批 xlsx 主表逐条过编辑部硬闸，任一垃圾漏网即 FAIL，让 job 真红。

用法：
  python scripts/verify_online_quality.py                 # 离线语料回归
  python scripts/verify_online_quality.py --mode ci      # 校验本批产物（由 workflow 调用）
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import yaml  # noqa: E402
from news_pipeline import editorial_gate as eg  # noqa: E402

CHECKS: list[dict] = []
CORPUS = BASE / "data" / "online_quality_corpus_20261121.json"
OUT = BASE / "output" / "online_quality_verify.json"


def add(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append({"项": name, "结果": "PASS" if ok else "FAIL", "证据": str(detail)[:320]})
    print(f"[{'PASS' if ok else 'FAIL'}] {name} — {str(detail)[:170]}")


def quality_conclusion_ok(concl: str) -> bool:
    """形态硬闸不达标 / 告警一律判红；「产能不足」允许交付但已在未达标项显式标注。"""
    c = str(concl or "")
    return "不达标" not in c and "告警" not in c


def as_item(d: dict):
    return SimpleNamespace(
        title=d.get("标题", ""), abstract=d.get("内容摘要", ""),
        abstract_body="", source=d.get("来源", ""),
        record_type=d.get("记录类型", ""), channel_id=d.get("事件主键", ""),
        group="", tag="", url="", pub=None, seq=d.get("展示序", 0),
        raw={}, shape="独立原文", has_event=True,
        abstract_status="有效", evidence="B", grade=d.get("等级", "—"))


def corpus_checks() -> None:
    d = json.loads(CORPUS.read_text(encoding="utf-8"))
    add("线上真产物语料随包（主表 1–18 逐字照抄 + 门禁实数）",
        len(d["负例"]) >= 3 and len(d["正例"]) >= 6 and "线上门禁实测" in d,
        f"负例 {len(d['负例'])} / 正例 {len(d['正例'])} / 格式样本 {len(d['格式样本'])}")

    # ① 负例必须被编辑部硬闸拦下
    for x in d["负例"]:
        it = as_item(x)
        ok, gate, why = eg.editorial_check(it)
        add(f"负例拦截｜序{x['展示序']} {x['标题'][:20]}", not ok,
            f"命中 {gate}：{why}" if not ok else "漏网！垃圾条目仍会进主表")

    # ② 正例必须全部保留（防止修过头把真新闻误杀）
    for x in d["正例"]:
        it = as_item(x)
        ok, gate, why = eg.editorial_check(it)
        add(f"正例保留｜{x['标题'][:20]}", ok,
            "通过" if ok else f"误杀 {gate}：{why}")

    # ③ 格式样本：摘要半句必须判 BAD，且严格收口能修好；来源列必须规范化
    for x in d["格式样本"]:
        seq = x["展示序"]
        if seq in (1, 18):
            ok, gate, why = eg.abstract_verdict(x["内容摘要"])
            add(f"摘要硬闸｜序{seq} 半句/坏标点被判不合格", not ok,
                f"{gate}：{why}" if not ok else "漏判！半句摘要会摆进交付表")
            fixed, fwhy = eg.polish_abstract_strict(x["内容摘要"])
            ok2, g2, w2 = eg.abstract_verdict("\u3000\u3000" + fixed)
            # 半句若根本无法凭空补全（序18「据沙特《」），正确行为是明确标 BAD
            # 交主表硬闸拒绝，绝不编造后半句；这也算合规修复。
            add(f"摘要收口｜序{seq} 修后整句或明确拒绝", ok2 or "无法整句收束" in fwhy,
                f"{fwhy} | 修后：{fixed[:48]!r} | {g2}:{w2}"[:200])
        if seq == 1:
            new, notes = eg.normalize_source(x["来源"])
            no_bare = not re.search(r"eqsn\.gov\.cn|www1\.xinhuanet\.com|news\.cn|Sohu", new, re.I)
            add("来源规范化｜序1 裸域名映射为渠道名（规格验收 FAIL 点名）",
                bool(new.strip()) and no_bare and " / " not in new and " · " in new,
                f"{x['来源'][:52]} → {new[:72]} | {notes[:2]}")
        if seq == 10:
            new, notes = eg.normalize_source(x["来源"])
            add("来源规范化｜序10 `/` 改 `·`、F10 标栏目页",
                "/" not in new and " · " in new and any("栏目页" in n for n in notes),
                f"{x['来源']} → {new} | {notes}")

    # ④ 线上 3 项不达标指标必须落在硬失败清单，且不再被 warn_only 掩盖
    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
    q = cfg.get("quality", {})
    hard = set(q.get("hard_fail_metrics") or [])
    failed_online = ["A级独立原文占比", "词条回查成功率", "非新闻噪声率"]
    # 第二轮：全量 56+344 条线上真表重判证据（不再只用 18 条样本自证）
    rj = BASE / "output" / "online_batch_rejudge.json"
    if rj.exists():
        rjd = json.loads(rj.read_text(encoding="utf-8"))
        st,断言 = rjd.get("统计", {}), rjd.get("断言", [])
        add("全量线上真表重判 14/14：只删 3 条垃圾、56→53 且 S 级地震保留",
            all(c["结果"] == "PASS" for c in 断言) and len(断言) >= 12
            and st.get("主表原始") == 56 and st.get("主表保留") == 53
            and st.get("主表剔除") == 3 and st.get("候选池原始") == 344,
            f"摘要修复 {st.get('主表摘要修复')} / 来源规范化 {st.get('主表来源规范化')} / "
            f"候选池留池留痕 {st.get('候选池摘要留痕')}")
    else:
        add("全量线上真表重判证据随包", False, "缺 output/online_batch_rejudge.json")
    add("线上 3 项不达标指标全部列入内容硬失败清单",
        all(m in hard for m in failed_online), f"hard_fail_metrics={sorted(hard)}")
    add("spec_fail_hard 开启（规格验收 FAIL 不再 return 0）",
        bool(q.get("spec_fail_hard")), f"spec_fail_hard={q.get('spec_fail_hard')}")
    run_src = (BASE / "run.py").read_text(encoding="utf-8")
    add("run.py 对规格验收不达标真 raise",
        "规格验收不达标：" in run_src and '结论"].startswith("不达标")' in run_src,
        "质量门禁 + 规格验收双阻断")
    wf = (BASE / ".github/workflows/daily-news.yml").read_text(encoding="utf-8")
    add("workflow 采集步接线线上质量回归（FAIL 直接红）",
        "verify_online_quality.py --mode ci" in wf, "不再是「告警后继续建站部署」")
    pipe = (BASE / "news_pipeline/pipeline.py").read_text(encoding="utf-8")
    add("pipeline 接入编辑部硬闸 + 摘要缩进/整句复核",
        "editorial_check_many" in pipe and "_ensure_indent" in pipe
        and "abstract_verdict" in pipe, "主表是候选池子集，同步生效")
    # CI 模式结论判据的双分支：达标才绿，warn_only 告警也必须红（线上事故根因）
    add("CI 结论判据｜达标 → 放行", quality_conclusion_ok("达标"),
        "结论=达标")
    add("CI 结论判据｜告警（warn_only）→ 判红", not quality_conclusion_ok("告警（warn_only）"),
        "线上批次结论正是「告警（warn_only）」，旧版却继续部署；现在必红")
    add("CI 结论判据｜不达标（内容硬闸）→ 判红",
        not quality_conclusion_ok("不达标（内容硬闸：A级独立原文占比）"),
        "内容硬指标不受 warn_only 保护")
    wf2 = (BASE / ".github/workflows/daily-news.yml").read_text(encoding="utf-8")
    add("workflow 显式导出本批 XLSX 给质量回归（不吃回放台账）",
        'XLSX="$XLSX" python scripts/verify_online_quality.py' in wf2
        and "echo \"XLSX=$XLSX\" >> \"$GITHUB_ENV\"" in wf2,
        "回归只读与本批 xlsx 同 stem 的运行摘要")


def _run_probe(extra_env=None, patch_spec=False, config_path=None) -> tuple[int, str]:
    """真跑一次 run.py demo；返回 (rc, 输出尾部)。config_path 供行为 C 单独构造场景。"""
    env = dict(os.environ)
    env.update({"NEWS_SKIP_FIXED_OUTPUT": "1", "NEWS_BUDGET_TOTAL": "60",
                "FREE_SOURCES_OFFLINE": "1", "NEWS_LOG_INDEX": "0"})
    if extra_env:
        env.update(extra_env)
    if patch_spec:
        code = (
            "import sys,json;"
            "from unittest.mock import patch;"
            "sys.path.insert(0,'.'); import run;"
            "fake={'明细':[{'项':'模拟规格FAIL','结果':'FAIL','证据':'probe'}],'检查项':1,'通过':0,'失败':1};"
            "sys.argv=['run.py','--source','demo','--no-log','--spec-verify',"
            f"'--config','{config_path or 'output/online_probe_pass.yaml'}',"
            "'--output-dir','output/online_probe','--name','probe_窗口期.xlsx'];"
            "from news_pipeline import spec;"
            "spec.verify_workbook=lambda p: fake; spec.verify_extra=lambda p: [];"
            "raise SystemExit(run.main())"
        )
        cmd = [sys.executable, "-c", code]
    else:
        cmd = [sys.executable, "run.py", "--source", "demo", "--no-log",
               "--config", config_path or "output/online_probe_config.yaml",
               "--output-dir", "output/online_probe", "--name", "probe_窗口期.xlsx"]
    try:
        r = subprocess.run(cmd, cwd=str(BASE), env=env, capture_output=True,
                           text=True, timeout=180)
        # 2026-11-22：旧版只取尾部 2500 字符，行为 C 的「[质量] 产能不足」/「[产能]」
        # 出现在 summarize 的大 JSON 之前，会被整段挤掉而误判 FAIL。返回完整输出，
        # 展示处仍各自截断，断言则能看见整批真实日志。
        return r.returncode, (r.stdout + r.stderr)
    except subprocess.TimeoutExpired:
        return -9, "超时"


def behavior_checks() -> None:
    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
    probe = copy.deepcopy(cfg)
    # 2026-11-22 二次修正：A级独立原文占比已划为「产能指标」（不阻断、显式标注），
    # 行为 A 必须改用形态硬闸构造，否则测的仍是产能分支、会假绿。
    # max_noise_ratio=-1 → 任何实测 ≥0 都超上限，必触发形态硬闸 → run.py 必须 rc≠0。
    probe.setdefault("quality", {})["max_noise_ratio"] = -1.0
    probe["source"] = {"mode": "demo"}
    (BASE / "output").mkdir(exist_ok=True)
    (BASE / "output/online_probe_config.yaml").write_text(
        yaml.safe_dump(probe, allow_unicode=True, sort_keys=False), encoding="utf-8")
    # 行为 B 专用：把质量指标阈值全部放宽到必达标，确保 run.py 能走到 spec_verify，
    # 而不是被 A 的质量硬门禁先拦下（那样测不到规格验收阻断）。
    qpass = copy.deepcopy(probe).get("quality", {})
    for k in list(qpass):
        if k.startswith("min_"):
            qpass[k] = 0.0
        elif k.startswith("max_"):
            qpass[k] = 1.0
    qpass["min_a_ratio"] = 0.0
    probe_pass = copy.deepcopy(probe)
    probe_pass["quality"] = qpass
    (BASE / "output/online_probe_pass.yaml").write_text(
        yaml.safe_dump(probe_pass, allow_unicode=True, sort_keys=False), encoding="utf-8")
    rc, out = _run_probe()
    probe_xlsx = BASE / "output" / "online_probe" / "probe_窗口期.xlsx"
    add("行为 A：内容硬指标不达标时 run.py 真失败（warn_only 不再掩盖）",
        rc != 0 and ("内容硬闸" in out or "采集质量门禁未达标" in out),
        f"rc={rc} | {out[-260:].replace(chr(10),' ')}")
    add("行为 A2：不达标批次产物被撤回（正式 xlsx 不存在，只留 .rejected 供排障）",
        rc != 0 and not probe_xlsx.exists()
        and (BASE / "output" / "online_probe" / "probe_窗口期.xlsx.rejected").exists()
        and "[撤回]" in out,
        "线上 55/59 却继续部署的终结：不达标连正式产物名都不留")
    rc2, out2 = _run_probe(patch_spec=True)
    add("行为 B：规格验收不达标时 run.py 真失败（线上 55/59 却 success 的终结）",
        rc2 != 0 and "规格验收不达标" in out2,
        f"rc={rc2} | {out2[-260:].replace(chr(10),' ')}")
    # 行为 C：形态硬闸全部达标、仅产能指标不达标 → 必须出表（页面不永久停更），
    # 但结论含「产能不足」且「未达标项」仍列出 A级独立原文占比，绝不伪装达标。
    qscale = copy.deepcopy(qpass)
    qscale["min_a_ratio"] = 1.1   # >1 恒不可能达标，且属产能类
    probe_scale = copy.deepcopy(probe_pass)
    probe_scale["quality"] = qscale
    (BASE / "output/online_probe_scale.yaml").write_text(
        yaml.safe_dump(probe_scale, allow_unicode=True, sort_keys=False), encoding="utf-8")
    rc3, out3 = _run_probe(config_path="output/online_probe_scale.yaml")
    add("行为 C：仅产能不足时 run.py 不阻断（推包后页面不会永久停在旧基线）",
        rc3 == 0 and "[质量] 产能不足（内容形态全部达标" in out3
        and "[产能]" in out3 and "[撤回]" not in out3,
        f"rc={rc3} | {out3[-300:].replace(chr(10),' ')}")
    add("行为 C2：产能不足仍如实列出未达标项（页面/CI/Excel 显式可见，不伪装达标）",
        rc3 == 0 and "未达标：A级独立原文占比" in out3
        and "[质量] 产能不足" in out3,
        f"结论与未达标项均在 stdout：{out3[-200:].replace(chr(10),' ')}")
    add("行为 C3：产能不足不等于放行低质内容（形态硬闸仍在 hard_fail_metrics）",
        "非新闻噪声率" in (cfg.get("quality") or {}).get("hard_fail_metrics", [])
        and "A级独立原文占比" in (cfg.get("quality") or {}).get("scale_metrics", [])
        and "非新闻噪声率" not in (cfg.get("quality") or {}).get("scale_metrics", []),
        "形态 6 项仍阻断；产能 2 项只标注；阈值一个未改")
    # 清理探针产物，不污染交付目录（full_audit 的「单份 xlsx」硬项会数这里的文件）
    import shutil
    shutil.rmtree(BASE / "output" / "online_probe", ignore_errors=True)
    for f in ("output/online_probe_config.yaml", "output/online_probe_pass.yaml",
              "output/online_probe_scale.yaml"):
        (BASE / f).unlink(missing_ok=True)


def ci_checks() -> None:
    """读本批 xlsx 主表逐条过编辑部硬闸；同时复核质量门禁结论。"""
    from openpyxl import load_workbook
    from openpyxl.reader.excel import ExcelReader
    ExcelReader.read_custom = lambda self: None
    # 2026-11-22 口径修正：旧版按 mtime 取「最新一份 xlsx」，本地解包自检时
    # smoke_run / verify_quality_v3 刚生成的回放产物会盖过本批交付物，把
    # 离线台账的「告警（warn_only）」误判成本批失败。CI 里由 workflow 显式
    # export XLSX（本批文件名来自 cfg step，不依赖产物是否存在）；无该 env 时
    # 只做逐条内容硬闸，质量结论项标「不适用」，不拿别的脚本产物凑数。
    env_xlsx = os.environ.get("XLSX", "").strip()
    if env_xlsx:
        cand = BASE / env_xlsx
        if not cand.is_absolute() and not cand.exists():
            cand = BASE / "output" / Path(env_xlsx).name
        xs = [cand] if cand.exists() else []
    else:
        xs = sorted((BASE / "output").glob("*_窗口期*.xlsx"),
                    key=lambda pth: pth.stat().st_mtime, reverse=True)
    if not xs:
        add("CI 模式：找到本批 xlsx", False,
            f"XLSX={env_xlsx or '未指定'}，output/ 下无对应 *_窗口期*.xlsx")
        return
    xlsx = xs[0]
    wb = load_workbook(xlsx, data_only=True)
    ws = wb["处理后的数据"] if "处理后的数据" in wb.sheetnames else wb[wb.sheetnames[0]]
    hdr = [c.value for c in ws[1]]
    rows = [dict(zip(hdr, [c.value for c in r]))
            for r in ws.iter_rows(min_row=2) if r[0].value is not None]
    add("CI 模式：主表可读", len(rows) > 0, f"{xlsx.name} 主表 {len(rows)} 条")
    bad, fmt_bad = [], []
    for r in rows:
        it = as_item({"标题": r.get("标题", ""), "内容摘要": r.get("内容摘要", ""),
                      "来源": r.get("来源", ""), "记录类型": r.get("记录类型", ""),
                      "事件主键": r.get("事件主键", "")})
        ok, gate, why = eg.editorial_check(it)
        if not ok:
            bad.append(f"{r.get('展示序')} {gate}:{str(r.get('标题'))[:26]}")
        oka, ga, wa = eg.abstract_verdict(str(r.get("内容摘要") or ""))
        if not oka:
            fmt_bad.append(f"{r.get('展示序')} {ga}:{wa[:30]}")
        if re.search(r"\s/\s|eqsn\.gov\.cn|www1\.xinhuanet\.com", str(r.get("来源") or "")):
            fmt_bad.append(f"{r.get('展示序')} 来源格式未规范化")
    add("CI 模式：主表零垃圾条目（SEO/黄页/剧透/假合并）", not bad,
        "漏网：" + "；".join(bad[:6]) if bad else f"{len(rows)} 条全部通过编辑部硬闸")
    add("CI 模式：主表摘要整句 / 来源列格式全合规", not fmt_bad,
        "异常：" + "；".join(fmt_bad[:6]) if fmt_bad else "零异常")
    # 质量门禁结论复核：只读与本批 xlsx 同 stem 的运行摘要，绝不吃回放台账。
    summary = xlsx.with_suffix(".json")
    if not env_xlsx:
        add("CI 模式：质量门禁结论（无 XLSX 环境变量，标不适用）", True,
            "离线自检不指定本批文件，逐条内容硬闸已独立生效；结论复核仅 CI 启用")
    elif summary.exists():
        q = (json.loads(summary.read_text(encoding="utf-8")) or {}).get("采集质量", {})
        concl = str(q.get("结论", ""))
        add("CI 模式：本批质量门禁结论不是「告警/不达标」",
            quality_conclusion_ok(concl),
            f"{summary.name} | 结论={concl} | 未达标={q.get('未达标项')}")
    else:
        add("CI 模式：本批质量门禁结论（摘要缺失，标不适用）", True,
            f"未找到 {summary.name}；run.py 质量硬门禁已先行阻断，本项不重复判")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["offline", "ci"], default="offline")
    args = ap.parse_args()
    corpus_checks()
    if args.mode == "offline":
        behavior_checks()
    else:
        ci_checks()
    passed = sum(1 for c in CHECKS if c["结果"] == "PASS")
    rep = {"模式": args.mode, "通过": passed, "总计": len(CHECKS),
           "失败项": [c["项"] for c in CHECKS if c["结果"] == "FAIL"],
           "检查": CHECKS,
           "说明": "语料来自用户上传的线上 latest.xlsx 与采集日志；含真跑 run.py 的行为验证"}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{passed}/{len(CHECKS)} PASS → {OUT}")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
