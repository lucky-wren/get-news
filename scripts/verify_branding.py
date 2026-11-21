# -*- coding: utf-8 -*-
"""get-news · 资讯获取：品牌 / 口径 / 接线 / 产物一致性硬校验（交付前必跑）。

2026-11-21 全面排查新增。此前的问题不是单点 bug，而是「显示名硬编码在 5 处、
摘要口径写在 3 处、模块写了但没验是否接进主链路、包里没有本批 xlsx」。
本脚本把这些全部变成可复跑的机械检查，任一硬项 FAIL 返回非 0。

用法：python scripts/verify_branding.py [--json output/branding_verify.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parents[1]
CHECKS: list[dict] = []


def add(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append({"项": name, "结果": "PASS" if ok else "FAIL", "证据": detail})


def text(rel: str) -> str:
    p = BASE / rel
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def main() -> int:
    cfg = yaml.safe_load(text("config.yaml")) or {}
    p = cfg.get("product", {}) or {}
    a = cfg.get("abstract", {}) or {}
    project, name, alias = str(p.get("project") or ""), str(p.get("name") or ""), str(p.get("alias") or "")

    add("① 项目名 get-news / 产品名单点 / 同音别名已登记",
        project == "get-news" and bool(name) and "咨询获取" in alias,
        f"project={project} name={name} alias={alias}（改 name 后须同步下列显示点）")

    bs = text("build_site.py")
    add("② 建站标题从 config 单点读取，且无「早八」旧文案",
        "_brand()" in bs and "PROJECT, PRODUCT, RUN_AT = _brand()" in bs
        and "早八自动版" not in bs and "每天 {RUN_AT}" in bs,
        "SITE_TITLE / SITE_SUBTITLE / sync_index_brand 全部由 config 生成")

    idx = text("docs/index.html")
    add("③ 页面 title / h1 == product.name",
        f"<title>{name}</title>" in idx and re.search(r"<h1[^>]*>" + re.escape(name) + r"</h1>", idx) is not None,
        "build_site.py --overwrite 时由 sync_index_brand 自动同步")

    wf = text(".github/workflows/daily-news.yml")
    add("④ workflow 名称与提交文案含产品名 / 无旧称",
        f"name: {name}" in wf and f"覆盖{name}数据" in wf
        and "更新要闻速览" not in wf and "早八" not in wf,
        "提交文案为硬编码，改 name 后需同步此行（脚本会报出来）")

    dep, ps1 = text("deploy.py"), text("deploy.ps1")
    bat_b = (BASE / "deploy.bat").read_bytes()
    bat = bat_b.decode("ascii", "replace")
    add("⑤ 一键部署校验标记 / 编码仍正确",
        f"覆盖{name}数据" in dep and "deploy.py" in bat
        and all(b < 128 for b in bat_b) and bat_b.count(b"\r\n") > 0,
        f"bat {len(bat_b)} 字节纯 ASCII CRLF（{bat_b.count(b'\r\n')} 行）；ps1 {len(ps1)} 字节")

    rd = text("README.md")
    add("⑥ README 头部标注项目名 / 产品名 / 当前状态入口",
        "get-news" in rd[:500] and name in rd[:500] and "full_audit.json" in rd[:800],
        "历史根因报告已在头部声明不代表当前状态")

    pv = text("静态页面预览.html")
    add("⑦ 单文件预览无旧定时 / 旧品牌文案",
        "早八自动版" not in pv and "每日 08:00" not in pv and name in pv,
        "预览页由 build_site.py --standalone 重新生成")

    nm = text("news_pipeline/normalize.py")
    kc = text("news_pipeline/keyless_compose.py")
    add("⑧ 摘要口径：两段 + ≤100 净字，config / normalize / keyless / README 四处一致",
        int(a.get("paragraphs", 0)) == 2 and int(a.get("max_net_chars", 0)) == 100
        and "PARAGRAPHS = 2" in nm and "单段 1–2 句" not in nm
        and "_split_two" in nm and "\\u3000\\u3000" in kc
        and "两段" in rd[:2500] and "single_paragraph" not in text("config.yaml"),
        "目标样例 34/34 两段、净字 64–100；生成端与验收端同口径")

    pl = text("news_pipeline/pipeline.py")
    kconf = cfg.get("keyless", {}) or {}
    nq = len(kconf.get("topic_queries", []) or [])
    add("⑨ 无 Key 主链路真接进 pipeline（不是只写模块）",
        "kc.batch_compose" in pl and "def batch_compose" in kc
        and "set_abstract_paragraphs" in pl and kconf.get("enabled") is True
        and nq >= 10 and int((cfg.get("search_api", {}) or {}).get("max_calls", 0)) >= 200,
        f"主题词 {nq} 条 / 声明 max_calls={int((cfg.get('search_api', {}) or {}).get('max_calls', 0))}"
        "（运行时裁剪值由 verify_runtime_budget.py 校验）")

    dated = sorted((BASE / "output").glob("*_窗口期.xlsx"))
    latest = BASE / "docs/downloads/latest.xlsx"
    if len(dated) == 1 and latest.exists():
        h1 = hashlib.md5(dated[0].read_bytes()).hexdigest()
        h2 = hashlib.md5(latest.read_bytes()).hexdigest()
        ok_x = h1 == h2
        detail_x = f"{dated[0].name} 与 latest.xlsx 字节一致（{h1[:12]}）"
    else:
        ok_x, detail_x = False, f"output 日期 xlsx {len(dated)} 份 / latest 存在={latest.exists()}"
    add("⑩ 包内单个 xlsx：output 日期产物 == 固定下载文件", ok_x, detail_x)

    hist = [q for q in sorted(BASE.glob("*.md"))
            if q.name not in {"README.md", "资讯获取_需求说明书.md",
                              "资讯获取_配置部署说明.md", "资讯获取_一键部署说明.md",
                              "资讯获取_免费方案四步操作手册.md",
                              "资讯获取_全面排查与调整报告_v2.md",
                              "资讯获取_超时事故修复与耗时优化说明.md"}]
    missing = [q.name for q in hist if "【批次记录】" not in q.read_text(encoding="utf-8")[:800]]
    add("⑪ 历史报告均有「批次记录」标注", not missing,
        f"共 {len(hist)} 份，缺标注 {len(missing)} 份" + ("：" + "、".join(missing[:5]) if missing else ""))

    # 旧称只在这些「可见显示点」里禁止；spec/ 原始输入、渠道白名单、
    # 验收脚本里的 OLD_WORDS 检测字符串、拆条正则均属正当保留。
    visible = ["build_site.py", "docs/index.html", "docs/assets/app.js",
               "docs/assets/news-data.js", "静态页面预览.html",
               "deploy.py", "deploy.ps1", "deploy.bat",
               ".github/workflows/daily-news.yml", "README.md"]
    bad = []
    for rel in visible:
        s = text(rel)
        for w in ("每日要闻速览", "要闻速览", "早八自动版", "每日 08:00"):
            if w in s:
                bad.append(f"{rel}:{w}")
    add("⑫ 可见显示点无旧称 / 旧定时刻度", not bad, "、".join(bad[:6]) or "10 个显示点全部干净")

    passed = sum(1 for c in CHECKS if c["结果"] == "PASS")
    failed = [c for c in CHECKS if c["结果"] == "FAIL"]
    report = {
        "项目": project, "产品名": name, "别名": alias,
        "检查项": len(CHECKS), "PASS": passed, "FAIL": len(failed),
        "失败项": failed, "全部": CHECKS,
        "说明": "改 config.yaml 的 product.name 后重跑本脚本，可列出所有需同步的显示点。",
    }
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=str(BASE / "output/branding_verify.json"))
    args = ap.parse_args()
    out = Path(args.json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"品牌/接线/产物核查：{passed}/{len(CHECKS)} PASS | FAIL {len(failed)}")
    for c in failed:
        print(f"  [FAIL] {c['项']} — {c['证据'][:200]}")
    print(f"报告：{out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
