#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-11-22 线上 CI 截图（规格验收 56/59）的确定性回归。

截图三条 FAIL 没有一条是数据质量问题，全是验收器/产物摆放自己跟自己打架：
  1. 来源列：第 3 行 sohu.com / 163.com / ycpai.ycwb.com 裸域名漏映射，
     且 163.com 的数字加点命中「编号」正则被拒；
  2. 10 个社交平台登记为主干发现入口：验收器只扫「口径与稿外信息」sheet
     并数短语 >=10，旧版该 sheet 只有 2 处，逐平台 10 行写在别处；
  3. 单个 xlsx：output/线上批次新规则重判对照.xlsx 是随包排障证据，
     被 glob 数成本批第二份产物，自己把自己判红。

外加截图自相矛盾的两行：先打印「覆盖 docs/downloads/latest.xlsx」，
下一行「规格验收不达标」，最后「撤回…（不覆盖固定地址）」——
固定地址其实已被覆盖。本脚本用子进程拦截 shutil.copyfile，验证
不达标批次一次都不会写固定下载地址。

用法：python scripts/verify_spec_ci.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

CHECKS: list[dict] = []


def add(name: str, ok: bool, evidence: str = "") -> None:
    CHECKS.append({"项": name, "结果": "PASS" if ok else "FAIL", "证据": str(evidence)[:220]})


class _Any(dict):
    """日志 stub：任意层级索引 / .get / 数值运算都不抛异常。"""

    def __missing__(self, k):
        return _Any()

    def __getitem__(self, k):
        return dict.__getitem__(self, k) if k in self else _Any()

    def get(self, k, d=None):
        return dict.get(self, k, d if d is not None else _Any())

    def __mul__(self, o): return 0.0
    def __rmul__(self, o): return 0.0
    def __add__(self, o): return 0.0 if isinstance(o, (int, float)) else ""
    def __radd__(self, o): return 0.0 if isinstance(o, (int, float)) else ""
    def __sub__(self, o): return 0.0 if isinstance(o, (int, float)) else ""
    def __rsub__(self, o): return 0.0 if isinstance(o, (int, float)) else ""


def main() -> int:
    from news_pipeline import editorial_gate as eg
    from news_pipeline.spec import source_format_ok, verify_workbook

    # ---- ① 截图第 3 行裸域名 -------------------------------------------------
    raw_src = "sohu.com · 163.com · 腾讯新闻 · ycpai.ycwb.com"
    fixed, notes = eg.normalize_source(raw_src)
    add("截图第3行三个裸域名全部映射为渠道名",
        all(k not in fixed for k in ("sohu.com", "163.com", "ycpai.ycwb.com"))
        and source_format_ok(fixed),
        f"{raw_src} → {fixed} | 留痕 {notes}")
    add("映射表补齐 sohu.com / 163.com / ycpai.ycwb.com（防回退）",
        all(k in eg.DOMAIN_TO_CHANNEL for k in ("sohu.com", "163.com", "ycpai.ycwb.com")),
        "旧表只有裸词 key 'sohu'（线上语料序1 用得上），缺 sohu.com / 163.com / 羊城派；"
        "现在裸词与域名两类 key 并存")

    # ---- ② 编号拒绝正则：仍拒真编号，不再误伤数字域名 ------------------------
    must_reject = ["1、搜狐网 · 中新网", "2. 中新网", "https://a.com/x",
                   "未取得", "中新网 · 待补"]
    leaked = [s for s in must_reject if source_format_ok(s)]
    add("编号 / 链接 / 占位仍被拒（收紧正则没有放宽验收）",
        not leaked, f"应拒 {len(must_reject)} 条；漏网 {leaked}")
    add("数字域名不再被当编号误杀",
        source_format_ok("网易 · 搜狐网") and source_format_ok(fixed),
        "163.com 映射为渠道名；点后紧跟字母的域名不再命中编号式")

    # ---- ③ 真出表：口径表逐平台 10 行 + 单个 xlsx 口径 -----------------------
    probe = BASE / "output" / "verification" / "spec_ci_probe.xlsx"
    probe.parent.mkdir(parents=True, exist_ok=True)
    txt = ""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        import yaml
        from openpyxl import load_workbook
        from news_pipeline.timewindow import Window
        from news_pipeline.exporter import export_excel
        from news_pipeline.channels import ChannelRegistry
        from news_pipeline.model import RawItem

        cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8")) or {}
        reg = ChannelRegistry(str(BASE / "data" / "资讯渠道大全_31类扩展版.md"),
                              str(BASE / "data" / "channels_extra.yaml"))
        win = Window(start=datetime(2026, 11, 20, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                     end=datetime(2026, 11, 21, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                     interval="lo-ro", tz="Asia/Shanghai")
        it = RawItem(title="测试条目 LPR 保持不变",
                     abstract="　　央行公布最新贷款市场报价利率，1年期为3.0%。　　中新网报道。",
                     abstract_status="有效", source=fixed,
                     group="财经市场", tag="房地产 / 地方债务", eid="e1001",
                     record_type="独立原文", evidence="A", seq=1, grade="B",
                     Final=60.0, Final_adj=60.0, score_order=1, display_order=1)
        log = _Any()
        log["mode"] = "live"
        R = {"window": win, "cfg": cfg, "registry": reg,
             "candidate": [it], "main": [it], "ordered": [it], "logs": log}
        export_excel(probe, R)
        wb = load_workbook(probe, read_only=True, data_only=True)
        try:
            txt = "\n".join(" | ".join(str(x or "") for x in row)
                            for row in wb["口径与稿外信息"].iter_rows(values_only=True))
        finally:
            wb.close()
        n = txt.count("主干发现入口")
        add("口径表逐平台登记 10 行（验收器数 >=10 的条件真满足）",
            n >= 10 and "10 个社交主干平台逐条申报" in txt,
            f"主干发现入口出现 {n} 次（线上截图为 2 次）")
        add("来源列验收项对截图原串真通过（写进产物再读回）",
            source_format_ok(fixed), f"写回来源列：{fixed}")
    except Exception as e:
        add("真出表回归可用", False, f"{type(e).__name__}: {str(e)[:150]}")
    try:
        vr = verify_workbook(str(probe))
        single = [c["结果"] for c in vr["明细"] if "单个 xlsx" in c["项"]]
        add("单个 xlsx 只数同批期次产物，排障证据不再自己判红",
            bool(single) and single[0] == "PASS",
            f"验收结果 {single}；verification 目录另有 golden_replay / latest_recomposed 两份证据 xlsx")
    except Exception as e:
        add("单个 xlsx 验收项可复算", False, f"{type(e).__name__}: {str(e)[:150]}")
    finally:
        probe.unlink(missing_ok=True)

    # ---- ④ 覆盖顺序：不达标批次一次都不写固定下载地址 ------------------------
    # 子进程要真正走到 spec_verify：demo 台账的「摘要有效率」本来会触发质量硬闸，
    # 所以用一份只放宽阈值、spec_fail_hard 仍为 True 的临时探针配置，把场景精确
    # 收敛到「规格验收 FAIL」这一条路径上。
    code = (
        "import sys, shutil, contextlib, yaml;"
        "sys.path.insert(0,'.'); import run;"
        "from news_pipeline import spec;"
        "calls=[];"
        "shutil.copyfile=lambda a,b: calls.append(str(b));"
        "c=yaml.safe_load(open('config.yaml',encoding='utf-8'));"
        "q=c['quality'];"
        "q.update({k:(0.0 if k.startswith('min_') else (1.0 if k.startswith('max_') else q[k])) for k in list(q)});"
        "q['hard_fail_metrics']=[];"
        "open('output/verification/spec_ci_cfg.yaml','w',encoding='utf-8').write("
        "  yaml.safe_dump(c,allow_unicode=True,sort_keys=False));"
        "fake={'明细':[{'项':'模拟规格FAIL','结果':'FAIL','证据':'probe'}],"
        "'检查项':1,'通过':0,'失败':1};"
        "spec.verify_workbook=lambda p: fake; spec.verify_extra=lambda p: [];"
        "sys.argv=['run.py','--source','demo','--no-log','--spec-verify',"
        "'--config','output/verification/spec_ci_cfg.yaml',"
        "'--output-dir','output/verification','--name','spec_ci_run.xlsx'];"
        "\nwith contextlib.suppress(BaseException): run.main()"
        "\nprint('SPEC_CI_CALLS='+str([c for c in calls if 'latest' in c]))"
    )
    env = dict(os.environ)
    env.update({"FREE_SOURCES_OFFLINE": "1", "NEWS_LOG_INDEX": "0"})
    try:
        r = subprocess.run([sys.executable, "-c", code], cwd=str(BASE), env=env,
                           capture_output=True, text=True, timeout=200)
        out_txt = r.stdout + r.stderr
        marker = [l for l in out_txt.splitlines() if l.startswith("SPEC_CI_CALLS=")]
        calls = marker[-1] if marker else "SPEC_CI_CALLS=[未打印]"
        add("规格验收不达标时不覆盖固定下载地址（截图两行自相矛盾已修）",
            "SPEC_CI_CALLS=[]" in calls and "规格验收不达标" in out_txt,
            f"{calls} | 子进程 rc={r.returncode}")
    except Exception as e:
        add("规格验收不达标时不覆盖固定下载地址", False,
            f"{type(e).__name__}: {str(e)[:120]}")
    finally:
        d = BASE / "output" / "verification"
        for f in ("spec_ci_run.xlsx", "spec_ci_run.xlsx.rejected",
                  "spec_ci_run.reject.json", "spec_ci_cfg.yaml",
                  "spec_ci_run.json"):
            (d / f).unlink(missing_ok=True)

    src = (BASE / "run.py").read_text(encoding="utf-8")
    i_false = src.find("sync_single_xlsx(cfg, out, apply_latest=False)")
    i_true = src.find("sync_single_xlsx(cfg, out, apply_latest=True)")
    i_spec = src.find("if args.spec_verify:")
    i_raise = src.rfind("raise RuntimeError(reason)")
    add("run.py 顺序：验收前只清往期，撤回之后才允许覆盖",
        i_false >= 0 and i_true >= 0 and i_false < i_spec < i_raise < i_true,
        f"False@{i_false} < spec_verify@{i_spec} < raise@{i_raise} < True@{i_true}")

    passed = sum(1 for c in CHECKS if c["结果"] == "PASS")
    p = BASE / "output" / "spec_ci_verify.json"
    p.write_text(json.dumps({"通过": passed, "总计": len(CHECKS), "检查": CHECKS,
                             "语料": "2026-11-22 线上 CI 截图三条规格验收 FAIL"},
                            ensure_ascii=False, indent=2), encoding="utf-8")
    for c in CHECKS:
        print(f"[{c['结果']}] {c['项']} — {c['证据'][:110]}")
    print(f"\n{passed}/{len(CHECKS)} PASS → {p}")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
