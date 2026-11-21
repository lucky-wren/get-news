"""资讯获取 · 固化打包脚本。

2026-11-21 全面核查发现：此前每轮用内联命令手工打包，规则不落盘，结果
`.github/` 隐藏目录漏过一次、`output/*.json` 的 18 个验证证据文件全漏过一次
（而 spec.py 的 ⑧⑨ 验收项与 CI 的 --spec-verify 都依赖它们）。本脚本把
「该进包 / 不该进包」写成代码，并在打包后自检，杜绝再靠用户发现漏文件。

用法：
  python scripts/make_package.py                     # 生成项目包
  python scripts/make_package.py --out /path/x.zip   # 指定输出
  python scripts/make_package.py --check-only        # 只跑自检不写文件
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parents[1]
DEFAULT_OUT = BASE.parent / "news_daily_project.zip"

# 目录级排除：日志/缓存不进包；logs/.gitkeep 单独保留（旧 workflow 的兜底）
SKIP_DIRS = {".git", "__pycache__", "node_modules", "_sim", "_verify_final", ".pytest_cache"}
SKIP_EXT = {".pyc", ".pyo", ".log", ".tmp", ".out", ".zip", ".bak"}
SKIP_NAMES = {"deploy_selftest_state.json"}

# 解包后必须存在的文件（缺一个就判打包失败）
REQUIRED = [
    ".github/workflows/daily-news.yml",
    "config.yaml", "requirements.txt", "run.py", "build_site.py", "serve.py",
    "deploy.bat", "deploy.py", "deploy.ps1", "README.md",
    "docs/index.html", "docs/assets/app.js", "docs/assets/news-data.js",
    "docs/data/latest.json", "docs/downloads/latest.xlsx", "docs/.nojekyll",
    "news_pipeline/pipeline.py", "news_pipeline/spec.py",
    "news_pipeline/quality_v3.py", "news_pipeline/channel_resolve.py",
    "news_pipeline/star_retest.py", "news_pipeline/rules.py",
    "news_pipeline/keyless_compose.py",
    "scripts/verify_spec.py", "scripts/verify_rules.py",
    "scripts/verify_workflow.py", "scripts/verify_deploy.py",
    "scripts/verify_quality_v3.py", "scripts/verify_channel_resolve.py",
    "scripts/verify_search_quality.py", "scripts/verify_share_pages.py",
    "scripts/check_endpoints.py", "scripts/discover_endpoints.py",
    "scripts/make_package.py", "scripts/full_audit.py",
    "scripts/verify_keyless.py", "scripts/verify_branding.py",
    "scripts/verify_runtime_budget.py", "scripts/verify_golden.py",
    "news_pipeline/event_composer.py",
    "news_pipeline/quality_gate.py", "scripts/verify_quality_gate.py",
    "output/quality_gate_verify.json", "output/latest_batch_audit.json",
    "scripts/verify_throughput.py", "output/throughput_verify.json",
    "资讯获取_主链路吞吐根因与并发改造.md",
    "output/keyless_cn_verify.json", "scripts/verify_keyless_cn.py",
    "data/keyless_noise_corpus.json",
    "data/latest_batch_regression.json", "data/hc_url_regression.json",
    "news_pipeline/url_resolve.py", "scripts/verify_url_resolve.py",
    "output/url_resolve_verify.json",
    "资讯获取_检索层零成稿根因修复.md",
    "资讯获取_候选池契约三闸与社交话题重定义.md",
    "资讯获取_自测数字与线上批次差异说明.md",
    "资讯获取_无Key中文召回根因与修复.md",
    "scripts/verify_budget.py", "scripts/preflight.py",
    "news_pipeline/budget.py",
    "output/branding_verify.json", "output/budget_verify.json",
    "output/runtime_budget_verify.json", "output/golden_diff.json",
    "output/verification/golden_replay.xlsx", "output/verification/latest_recomposed.xlsx",
    "output/budget_report.json",
    "资讯获取_全面排查与调整报告_v2.md", "资讯获取_超时事故修复与耗时优化说明.md",
    "资讯获取_目标样例形态对齐与事件成稿层.md",
    "data/资讯渠道大全_31类扩展版.md", "data/endpoints_live.yaml",
    "data/v3_star_pending.yaml", "data/channel_reach.json",
    "data/channel_resolved.json", "data/channel_health.json",
    "output/spec_verify.json", "output/quality_v3_verify.json",
    "output/keyless_verify.json", "output/full_audit.json",
    "output/channel_resolve_verify.json", "output/workflow_verify.json",
    "output/deploy_verify.json", "output/rules_regression.json",
    "output/search_quality.json", "output/share_pages_check.json",
]
# 中文文件名（验证 UTF-8 flag，避免 CP437 乱码）；纯 ASCII 名不在此列
REQUIRED_CN = [
    "data/资讯渠道大全_31类扩展版.md",
    "资讯获取_配置部署说明.md", "资讯获取_需求说明书.md",
    "资讯获取_无Key多源成稿方案.md",
    "资讯获取_自测数字与线上批次差异说明.md",
    "资讯获取_检索层零成稿根因修复.md",
    "资讯获取_无Key中文召回根因与修复.md",
    "静态页面预览.html",
]


def collect() -> list[Path]:
    out: list[Path] = []
    # output 里按「年月日+窗口期」命名的 xlsx 只留最新一份（需求⑤：单个 xlsx）
    dated = sorted(
        (p for p in (BASE / "output").glob("*_窗口期*.xlsx")),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    keep_dated = {dated[0].resolve()} if dated else set()
    for dp, dn, fn in os.walk(BASE):
        dn[:] = sorted(d for d in dn if d not in SKIP_DIRS)
        rel_dir = Path(dp).relative_to(BASE)
        for f in sorted(fn):
            rel = (rel_dir / f).as_posix()
            p = Path(dp) / f
            if p.suffix.lower() in SKIP_EXT or f in SKIP_NAMES:
                continue
            if rel.startswith("logs/") and f != ".gitkeep":
                continue
            if rel.startswith("output/") and f.endswith("_窗口期.xlsx") and p.resolve() not in keep_dated:
                continue
            out.append(p)
    return sorted(out, key=lambda p: p.relative_to(BASE).as_posix())


def write_zip(path: Path, files: list[Path]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in files:
            rel = p.relative_to(BASE).as_posix()
            info = zipfile.ZipInfo(rel, date_time=datetime.now(ZoneInfo("Asia/Shanghai")).timetuple()[:6])
            info.flag_bits |= 0x800  # UTF-8 文件名
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o644 & 0xFFFF) << 16
            z.writestr(info, p.read_bytes())


def check(path: Path) -> dict:
    problems: list[str] = []
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        for r in REQUIRED:
            if r not in names:
                problems.append("缺文件: " + r)
        for r in REQUIRED_CN:
            if r in names:
                info = z.getinfo(r)
                # zipfile 对纯 ASCII 名会自动清掉 UTF-8 flag，这是合法的；
                # 只有含非 ASCII 字符的文件名才必须带 0x800，否则 Windows 解包乱码。
                if any(ord(ch) > 127 for ch in r) and not (info.flag_bits & 0x800):
                    problems.append("中文名未设 UTF-8 flag: " + r)
        bad = [n for n in names if n.endswith((".pyc", ".log")) or "__pycache__" in n
               or (n.startswith("logs/") and not n.endswith(".gitkeep"))]
        if bad:
            problems.append("不该入包: " + ", ".join(bad[:8]))
        dated = [n for n in names if n.startswith("output/") and n.endswith("_窗口期.xlsx")]
        if len(dated) != 1:
            problems.append("output 必须恰好 1 份日期 xlsx（需求⑤ 单个 xlsx）：" + ", ".join(dated))
        else:
            a = hashlib.md5(z.read(dated[0])).hexdigest()
            bkey = "docs/downloads/latest.xlsx"
            if bkey not in names:
                problems.append("缺固定下载文件 " + bkey)
            elif hashlib.md5(z.read(bkey)).hexdigest() != a:
                problems.append("output 日期 xlsx 与 docs/downloads/latest.xlsx 字节不一致")
        if ".github/workflows/daily-news.yml" not in names:
            problems.append("隐藏目录 .github/ 漏打包")
    return {"条目数": len(names), "问题": problems, "PASS": not problems}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--check-only", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)
    if not args.check_only:
        files = collect()
        write_zip(out, files)
        print(f"[打包] {out} | {len(files)} 文件 | {out.stat().st_size/1024/1024:.2f} MB")
    else:
        print(f"[自检] {out}")
    r = check(out)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    if not r["PASS"]:
        print("[打包自检] FAIL", file=sys.stderr)
        return 1
    print("[打包自检] PASS：隐藏目录 / 验证证据 / 中文名 / 单份 xlsx 全部齐全")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
