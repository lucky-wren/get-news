"""资讯获取 · GitHub Actions workflow 静态契约校验（纯离线，可反复跑）。

锁死 2026-11-20 那次「首步失败 → 后续 9 步 0s 全跳过 → 摘要自己也崩」的三类回退：
  1. 任何写 logs/ 的 run 块，必须在写入之前 `mkdir -p logs`（checkout 后 logs/ 不存在）；
  2. 增强 / 诊断类 step 必须 continue-on-error，绝不阻断当天出表；
     CI 运行摘要必须 if: always() 且自己建目录、全程容错；
  3. 主链路（采集 / 建站 / 提交 / 部署）不许被 continue-on-error 掩盖失败。

用法：python scripts/verify_workflow.py [--json output/workflow_verify.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
WF = BASE / ".github" / "workflows" / "daily-news.yml"

ENHANCE = ("端点自动发现", "全渠道可达性矩阵", "两类渠道逐条解析",
           "★ 必采缺口定向重测", "免费源池实测", "渠道端点体检")
MAIN = ("采集 → 筛选评级", "构建固定站点", "覆盖提交回仓库", "部署到 GitHub Pages")
OLD_WORDS = ("更新要闻速览", "供次日累积", "左开右闭", "→ 当日 0800", "历史归档")


def main() -> int:
    import yaml

    if not WF.exists():
        print("FAIL: .github/workflows/daily-news.yml 缺失"); return 1
    raw = WF.read_text(encoding="utf-8")
    try:
        d = yaml.safe_load(raw)
    except Exception as e:
        print("FAIL: workflow YAML 无法解析:", e); return 1
    steps = ((d.get("jobs") or {}).get("build-deploy") or {}).get("steps") or []
    if not steps:
        print("FAIL: 未解析到 build-deploy.steps"); return 1

    rows, bad = [], []

    def add(name: str, ok: bool, detail: str = "") -> None:
        rows.append({"检查": name, "结果": "PASS" if ok else "FAIL", "依据": detail})
        if not ok:
            bad.append(name)

    by_name = {str(s.get("name", "")): s for s in steps}

    # 1) 每个写 logs/ 的 run 块，mkdir 必须出现在首次写入之前
    writers = [s for s in steps if isinstance(s.get("run"), str)
               and re.search(r"(?:>\s*|>>\s*|tee(?:\s+-a)?\s+)logs/", s["run"])]
    for s in writers:
        body = s["run"]
        mdir = re.search(r"mkdir\s+-p\s+logs", body)
        mwrite = re.search(r"(?:>\s*|>>\s*|tee(?:\s+-a)?\s+)logs/", body)
        ok = bool(mdir and mwrite and mdir.start() < mwrite.start())
        add(f"logs 写入前置 mkdir · {s.get('name', '?')}", ok,
            f"mkdir@{mdir.start() if mdir else '-'} / 首次写入@{mwrite.start() if mwrite else '-'}")
    add("写 logs 的 step 数 ≥ 6", len(writers) >= 6, f"实际 {len(writers)} 个")

    # 2) 最前面就有准备日志目录 step（第二层保险）
    add("checkout 后首步准备日志目录",
        bool(steps) and "准备日志目录" in str(steps[1].get("name", ""))
        and "mkdir -p logs" in str(steps[1].get("run", "")),
        f"第 2 步 = {steps[1].get('name', '?') if len(steps) > 1 else '?'}")

    # 3) 增强类：失败不阻断
    for key in ENHANCE:
        s = next((v for n, v in by_name.items() if key in n), None)
        add(f"增强 step 容错 · {key}",
            bool(s and s.get("continue-on-error") is True),
            "" if s else "step 缺失")

    # 4) 主链路：不许掩盖失败
    for key in MAIN:
        s = next((v for n, v in by_name.items() if key in n), None)
        add(f"主链路硬失败 · {key}",
            bool(s) and s.get("continue-on-error") is not True,
            "" if s else "step 缺失")

    # 5) CI 摘要：always + 自建目录 + 容错
    s = next((v for n, v in by_name.items() if "CI 运行摘要" in n), None)
    body = str(s.get("run", "")) if s else ""
    add("CI 摘要 if: always()", bool(s and s.get("if") == "always()"))
    add("CI 摘要自建 logs 目录", "mkdir -p logs" in body)
    add("CI 摘要写文件容错", "|| true" in body)
    add("CI 摘要记录 job.status", "job.status" in body)

    # 6) 日志上传 always + 缺文件只告警
    s = next((v for n, v in by_name.items() if "上传运行日志" in n), None)
    add("运行日志 always 上传",
        bool(s and s.get("if") == "always()"
             and (s.get("with") or {}).get("if-no-files-found") == "warn"))

    # 7) 旧文案 / 旧口径清理
    for w in OLD_WORDS:
        add(f"无旧文案「{w}」", w not in raw)
    add("并行预探测 step 存在（6 个串行网络探测合成一次）",
        "并行预探测" in raw and "scripts/preflight.py" in raw, "总闸 150s")
    add("采集步 timeout ≤ 15 分钟且带全局预算 env",
        "NEWS_BUDGET_TOTAL" in raw and bool(
            re.search(r"采集 → 筛选评级[\s\S]{0,250}timeout-minutes:\s*(1[0-5]|[1-9])\b", raw)),
        "35min 硬杀白跑 → 脚本 600s 自己返回")
    add("采集步不再重复跑 --probe（由预探测产出 hot_probe.json）",
        "modelscope_notebook.py --probe --timeout 6" not in raw
        and "logs/hot_probe.json" in raw, "不二次发 10 平台请求")

    # 8) 关键口径仍在
    add("窗口 lo-ro 左开右开", "lo-ro" in raw or "interval" in raw)
    add("commit 产品名=覆盖资讯获取数据", "覆盖资讯获取数据" in raw)
    add("采集带 --spec-verify", "--spec-verify" in raw)

    # 8b) 2026-11-21：★ 必采缺口重测必须真正在 workflow 里，且成果必须回写仓库。
    #     原实现：v3_star_pending.yaml 只有文字、workflow 出现 0 次、git add 只含 docs/，
    #     导致「Actions 每次构建自动重测」从未生效，runner 一销毁成果全丢。
    star = next((v for n, v in by_name.items() if "★ 必采缺口定向重测" in n), None)
    star_body = str(star.get("run", "")) if star else ""
    add("★ 重测 step 存在", bool(star))
    add("★ 重测调用 star_retest 模块", "news_pipeline.star_retest" in star_body)
    add("★ 重测写入 logs 前置 mkdir",
        "mkdir -p logs" in star_body and star_body.find("mkdir -p logs") < star_body.find("tee logs/"))
    add("★ 重测逐条打印结果", "star_retest_result.json" in star_body)
    commit = next((v for n, v in by_name.items() if "覆盖提交回仓库" in n), None)
    cbody = str(commit.get("run", "")) if commit else ""
    add("提交回写 v3_star_pending.yaml", "data/v3_star_pending.yaml" in cbody)
    add("提交回写 endpoints_live.yaml", "data/endpoints_live.yaml" in cbody)
    add("提交回写 star_retest_result.json", "data/star_retest_result.json" in cbody)
    add("★ 清单 21 条且全带候选端点",
        (lambda p: bool(p.exists()) and
         len((yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("渠道", [])) == 21 and
         all(x.get("候选端点") for x in
             (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("渠道", [])))
        (BASE / "data" / "v3_star_pending.yaml"))
    add("star_retest 模块存在", (BASE / "news_pipeline" / "star_retest.py").exists())

    # 9) 双保险：即使远端仍是旧 workflow，logs/ 也必须天然存在；
    #    以及一键部署脚本必须随包提供，用来根治「.github 没推上去」。
    add("logs/.gitkeep 随仓库提供（旧 workflow 也不缺目录）",
        (BASE / "logs" / ".gitkeep").exists())
    add("随包提供 deploy.bat / deploy.py（主实现）",
        (BASE / "deploy.bat").exists() and (BASE / "deploy.py").exists())
    add("随包提供 deploy.ps1（无 Python 时回退）", (BASE / "deploy.ps1").exists())

    out = {
        "产品": "资讯获取", "workflow": str(WF.relative_to(BASE)),
        "step 数": len(steps), "写 logs 的 step 数": len(writers),
        "检查数": len(rows), "PASS": len(rows) - len(bad), "FAIL": len(bad),
        "失败项": bad, "检查": rows,
    }
    print(json.dumps({"检查数": len(rows), "PASS": out["PASS"],
                      "FAIL": out["FAIL"], "失败项": bad}, ensure_ascii=False))
    if bad:
        for r in rows:
            if r["结果"] == "FAIL":
                print("  [FAIL]", r["检查"], "|", r["依据"])
    dest = Path(sys.argv[sys.argv.index("--json") + 1]) if "--json" in sys.argv \
        else BASE / "output" / "workflow_verify.json"
    dest.parent.mkdir(exist_ok=True, parents=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
