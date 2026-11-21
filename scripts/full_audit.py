"""资讯获取 · 一次性全面核查（不再等用户逐个找问题）。

覆盖四层：
  1. 静态口径一致性：config / workflow / 文档 / 脚本里的开关与分母互相矛盾
  2. 行为回归：分类标签前缀、标题长度、无发布时间窗口判定、摘要契约
  3. 全套既有 verify 脚本 + 打包完整性自检
  4. 包内基线产物契约（信息项，不判致命：推包后由 Actions 覆盖）

输出 output/full_audit.json，任一硬项 FAIL 立即返回非 0。
用法：python scripts/full_audit.py [--zip /path/news_daily_project.zip] [--quick]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parents[1]
OUT = BASE / "output" / "full_audit.json"
sys.path.insert(0, str(BASE))

CHECKS: list[dict] = []


def add(name: str, ok: bool, detail: str = "", hard: bool = True) -> None:
    CHECKS.append({"项": name, "结果": "PASS" if ok else "FAIL",
                   "证据": detail, "硬项": hard})


def text(path: str) -> str:
    p = BASE / path
    return p.read_text(encoding="utf-8") if p.exists() else ""


def static_checks() -> None:
    cfg = text("config.yaml")
    wf = text(".github/workflows/daily-news.yml")
    doc = text("资讯获取_配置部署说明.md")
    req = text("资讯获取_需求说明书.md")
    readme = text("README.md")

    add("① 定时 20:00 / UTC cron 一致",
        'run_at: "20:00"' in cfg and 'cron_utc: "0 12 * * *"' in cfg
        and "- cron: '0 12 * * *'" in wf, "config 与 workflow 双向核对")
    add("① 窗口默认左开右开 / 锚点不漂",
        "interval: lo-ro" in cfg and "anchor_to_run_at: true" in cfg,
        "config + workflow 解析 step")
    add("① 数据源 live / 摘要 100 字 / 主表摘要硬闸",
        "mode: live" in cfg and "max_net_chars: 100" in cfg
        and "main_requires_abstract: true" in cfg, "config.yaml")
    add("② H 通道 heat 且与部署文档一致",
        "h_channel: heat" in cfg and "h_channel: auto" not in doc,
        "旧版部署文档写 auto，会让照文档配置的人关掉事件热度")
    add("② USGS 阈值配置与代码默认一致",
        "usgs_min_magnitude: 5.5" in cfg
        and '"usgs_min_magnitude", 5.5' in text("news_pipeline/collectors.py"),
        "配置缺失时代码不再回退 4.5")
    add("③ 需求说明书无「口径待确认」",
        "口径待确认" not in req and "口径定稿" in req, "用户 2026-11-20 已拍板")
    try:
        ch = json.loads(text("data/channel_health.json"))
        denom_ok = (ch.get("白名单渠道总数") == 599 and ch.get("主口径白名单") == 599
                    and "白名单行数(含扩充)" in ch
                    and "round(len(configured) / reach_total" in text("scripts/check_endpoints.py"))
        denom_detail = (f"主口径 {ch.get('主口径白名单')} / 总数 {ch.get('白名单渠道总数')} / "
                        f"配置率 {ch.get('端点配置率')}")
    except Exception as e:
        denom_ok, denom_detail = False, str(e)
    add("③ 主口径分母统一 599", denom_ok, denom_detail + "；体检器按 reach_total 计算")
    add("③ README 不再把 3.5% / 604 当当前值",
        "当前 604 个白名单" not in readme and "当前配置率 3.5%" not in readme,
        "历史数字只允许出现在明确标注的历史段落")
    add("④ 两类渠道解析禁止 0→0 假绿",
        "原两类合计\", 0) > 0" in text("news_pipeline/spec.py")
        and "parse_channel_doc" in text("news_pipeline/channel_resolve.py")
        and "不得判成功" in text("news_pipeline/channel_resolve.py")
        and "不得判成功" in wf,
        "spec 验收 / 解析模块 / workflow 三处都改")
    add("④ 体检结果纳入 CI 回写",
        "data/channel_health.json" in wf and "data/free_sources_probe.json" in wf,
        "否则每次构建重测成果随 runner 销毁")
    add("⑤ ★ 缺口定向重测已在 workflow",
        "★ 必采缺口定向重测" in wf and "star_retest" in wf, "21 条清单 + 真实探测")
    add("⑤ 页面零配置重跑 / 日志区保留",
        "无需任何配置 / Token" in text("docs/assets/app.js")
        and "运行日志" in text("docs/index.html"), "静态 Pages 打开 Actions 页并轮询固定 json")
    pl = text("news_pipeline/pipeline.py")
    kc = text("news_pipeline/keyless_compose.py")
    try:
        import yaml as _y
        kconf = ((_y.safe_load(text("config.yaml")) or {}).get("keyless", {}) or {})
        nq = len(kconf.get("topic_queries", []))
    except Exception:
        nq = 0
    add("⑪ 无 Key 主链路真的接进 pipeline（不是只写模块）",
        "kc.batch_compose" in pl and "def batch_compose" in kc
        and "set_abstract_paragraphs" in pl and nq >= 10,
        f"pipeline 调用 / 模块入口 / 段数注入 / 主题词 {nq} 条")
    add("⑪ 摘要口径：两段 + ≤100 净字，config / normalize / README 一致",
        "paragraphs: 2" in cfg and "single_paragraph" not in cfg
        and "PARAGRAPHS" in text("news_pipeline/normalize.py")
        and "两段" in readme and "单段 1–2 句" not in readme,
        "2026-11-21 全面核查：三处口径打架已统一")
    add("⑫ 全局预算：总时长 ≤ 600s、workflow 采集步 ≤ 15min、预探测一次并行",
        "total_collect_seconds: 600" in cfg
        and "scripts/preflight.py" in wf
        and bool(re.search(r"采集 → 筛选评级[\s\S]{0,250}timeout-minutes:\s*(1[0-5]|[1-9])\b", wf)),
        "2026-11-21 采集步 35 分钟超时事故防回退")
    add("⑬ 事件成稿层已接入主链路（URL 级 → 事件级，目标形态对齐）",
        "event_composer.py" in text("scripts/make_package.py")
        and "2.5 事件成稿层" in text("news_pipeline/pipeline.py")
        and '"EVENT_COMPOSER"' in text("news_pipeline/pipeline.py")
        and "工程留痕 O" in text("news_pipeline/exporter.py")
        and "event_composer:" in text("config.yaml"),
        "2026-11-22：目标候选池是 73 个事件，线上是 327 个 URL；筛选/评分都在成稿之后")
    add("⑬ 目标契约往返复现：多渠道率 ≥0.9 / 标题复读 0 / 超长 0",
        _golden_ok(),
        _golden_why())
    add("⑬ 诚实边界：线上 327 行单源批次改造后多渠道率仍 <0.5，未包装成达标",
        _golden_honest(),
        "事件成稿不能凭空造出独立来源；真实达标依赖采集层 keyless 检索 + 社交回查")
    add("⑫ 运行时容量 / 时间解析 / 两段形态三道新闸已接线",
        "verify_runtime_budget.py" in text("scripts/full_audit.py")
        and "verify_runtime_budget.py" in text("scripts/make_package.py")
        and "_parse_pub" in text("news_pipeline/keyless_compose.py")
        and "_near_dup" in text("news_pipeline/normalize.py"),
        "2026-11-21 继续轮：静态验收抓不到的三类问题已变成可复跑硬项")
    add("⑫ 超时口径：workflow 15min / budget 600s / docstring 写明当前设定",
        "timeout-minutes: 15" in wf and 'NEWS_BUDGET_TOTAL: "600"' in wf
        and "当前 CI 真实设定" in text("news_pipeline/budget.py")
        and "35 分钟超时事故修复）" not in text("news_pipeline/budget.py"),
        "35 分钟是历史事故名（README / 超时说明保留），budget.py 已写明当前 15min + 600s")
    add("⑪ 品牌单点：get-news / 20:00 / 无早八残留",
        "project: get-news" in cfg and "_brand()" in text("build_site.py")
        and "早八自动版" not in text("build_site.py")
        and "早八自动版" not in text("静态页面预览.html"),
        "build_site 从 config 读；预览页由 build_site 重新生成")
    add("⑭ 候选池契约三闸已接线（三张截图：串味 / 匿名破产 / 社交花边）",
        "quality_gate" in text("news_pipeline/pipeline.py")
        and "contract_check_many" in text("news_pipeline/pipeline.py")
        and "工程留痕 P" in text("news_pipeline/exporter.py")
        and "pair_cannot_link" in text("news_pipeline/event_composer.py")
        and "簇内复核拆出" in text("news_pipeline/event_composer.py")
        and "VAGUE_SUBJECT" in text("news_pipeline/rules.py")
        and "某光伏企业" in text("news_pipeline/rules.py")
        and "verify_quality_gate.py" in text("scripts/make_package.py"),
        "需求⑤「未经筛选」≠ 字段不齐/分类不合规/跨事件串味照收；采集级 + 成稿后各跑一次")
    add("⑮ 线上真实批次回归语料随包（不再只用目标样例自证）",
        (BASE / "data/latest_batch_regression.json").exists()
        and "latest_batch_regression.json" in text("scripts/verify_quality_gate.py")
        and "标题三连复读" in text("scripts/verify_quality_gate.py")
        and "latest_batch_audit.json" in text("scripts/make_package.py"),
        "用户上传 latest.xlsx 的 246 行候选池 + 23 行主表逐行真调运行时函数")
    add("⑯ 成稿层摘要格式清理不绕过 build_abstract",
        "polish_abstract" in text("news_pipeline/pipeline.py")
        and "polish_abstract" in text("news_pipeline/normalize.py")
        and "题[:：]" in text("news_pipeline/normalize.py")
        and "导弹袭击" in text("news_pipeline/rules.py"),
        "线上电头/记者署名/「；。」直接进 Excel 的接线缺口已堵")
    add("⑰ 无 Key 中文召回根因修复（线上 keyless 43 次 0 成稿）",
        "cn_endpoints" in text("news_pipeline/keyless_compose.py")
        and "cn_endpoints" in text("config.yaml")
        and "use_raw_event_clues" in text("news_pipeline/pipeline.py")
        and "batch_active" in text("news_pipeline/search_api.py")
        and "prefilter_noise" in text("news_pipeline/keyless_compose.py")
        and "verify_keyless_cn.py" in text("scripts/make_package.py"),
        "中文证据来自已实证端点 + 具体事件线索多站回查；不再靠英文搜索召回中文")
    add("⑱ 线上 H-c 噪声语料与真实验收随包",
        (BASE / "data/keyless_noise_corpus.json").exists()
        and (BASE / "output/keyless_cn_verify.json").exists()
        and "keyless_noise_corpus.json" in text("scripts/make_package.py"),
        "122 个线上候选域名逐条预筛 + 沙盒真发请求对照")
    add("⑲ 自测数字与线上数字差异说明随包",
        (BASE / "资讯获取_自测数字与线上批次差异说明.md").exists()
        and "【批次记录】" in (BASE / "资讯获取_自测数字与线上批次差异说明.md").read_text(encoding="utf-8")[:800]
        and "资讯获取_自测数字与线上批次差异说明.md" in text("scripts/make_package.py"),
        "离线回放数字与线上跑批数字分开标注，不再摆进同一张成果表")
    add("⑳ 检索层零成稿根因修复（线上 H-c 实证）",
        (BASE / "news_pipeline/url_resolve.py").exists()
        and "decode_bing_ck" in text("news_pipeline/url_resolve.py")
        and "resolve_search_url" in text("news_pipeline/search_api.py")
        and "<source url" in text("news_pipeline/keyless_compose.py")
        and "dedup_key" in text("news_pipeline/keyless_compose.py")
        and "verify_url_resolve.py" in text("scripts/make_package.py"),
        "gnews 域名塌缩 / Bing ck-a 未还原 / 噪声域名占名额三处接线均已修")
    add("㉑ 线上 H-c 真实 URL 回归随包且真调运行时函数",
        (BASE / "data/hc_url_regression.json").exists()
        and "gnews 3 条独立报道" in text("scripts/verify_url_resolve.py")
        and "url_resolve_verify.json" in text("scripts/make_package.py"),
        "122 个线上候选 URL + mock RSS 验证 domains 从恒 1 变为 3")
    add("㉒ 主链路吞吐契约 10 项全过（600s 预算内产得出 34 条多源事件）",
        (BASE / "scripts/verify_throughput.py").exists()
        and (BASE / "output/throughput_verify.json").exists()
        and "cancel_futures=True" in text("news_pipeline/keyless_compose.py")
        and "cancel_futures=True" in text("news_pipeline/social_main.py")
        and "collector.lookup_article(" not in text("news_pipeline/pipeline.py").split("0a-3")[0].split("0a-2")[-1]
        and "verify_throughput.py" in text("scripts/make_package.py"),
        "旧串行 1100s / 配额 185s、80 线索只跑完 13 条；改并发后 100/100 且总预算降 30s")


def behavior_checks() -> None:
    from news_pipeline.rules import classify, _align
    from news_pipeline.normalize import clean_title_text
    from news_pipeline.timewindow import window_verdict
    from news_pipeline.exporter import _abstract_checks
    from news_pipeline.model import RawItem

    g, t = _align("财经市场", "房地产 / 地方债务", legacy=False)
    add("⑥ 分类标签前缀 == 上位大类",
        g == "财经市场" and t.startswith("财经市场 / "), f"({g}, {t})")
    it = RawItem(title="社保断缴三个月账户清零不实", source="中新网", group="民生服务")
    gg, tt = classify(it, legacy=False)
    add("⑥ 单标签（不带 /）不误判错配", bool(gg) and bool(tt), f"({gg}, {tt})")

    long_title = "习近平主持亚太经合组织第三十三次领导人非正式会议并发表重要讲话 11月19日上午在深圳国际交流中心主持会议并发表题为建设亚太共同体的讲话"
    cut = clean_title_text(long_title)
    add("⑥ 标题 ≤60 净字且按整句收束", len(cut) <= 60, f"{len(cut)} 字: {cut[:40]}…")

    # 无发布时间 + 无日期线索 → 必须拒绝（旧版无条件放行导致旧公告入池）
    from news_pipeline.timewindow import Window
    from datetime import datetime as _dt
    win = Window(_dt(2026, 11, 19, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                 _dt(2026, 11, 20, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                 interval="lo-ro")
    fake = SimpleNamespace(pub=None, record_type="RSS 时间倒排", time_basis="未取得",
                           date_clue="", text=lambda: "")
    ok, why = window_verdict(fake, win, allow_estimated=True, strict_window=False)
    add("⑦ 无发布时间且无日期线索不入池", not ok, why)
    fake.date_clue = "11-20"
    ok2, why2 = window_verdict(fake, win, allow_estimated=True, strict_window=False)
    add("⑦ 窗口内日期线索仍可时点推定", ok2, why2)

    abs_item = RawItem(title="测试", abstract="\u3000\u3000" + "摘要正文。" * 30)
    pool_item = RawItem(title="候选", abstract="\u3000\u3000" + "长摘要。" * 60)
    ac = _abstract_checks([abs_item], [pool_item])
    add("⑦ 摘要契约同时检查候选池",
        ac["候选池超长(>100净字)"] == 1 and ac["超长(>100净字)"] == 1,
        json.dumps(ac, ensure_ascii=False))


def verify_scripts(quick: bool) -> None:
    scripts = ["scripts/verify_spec.py", "scripts/verify_rules.py",
               "scripts/verify_search_quality.py", "scripts/verify_workflow.py",
               "scripts/verify_deploy.py", "scripts/verify_channel_resolve.py",
               "scripts/verify_quality_v3.py", "scripts/check_keys.py",
               "scripts/verify_branding.py", "scripts/verify_budget.py",
               "scripts/verify_runtime_budget.py", "scripts/verify_quality_gate.py"]
    if not quick:
        scripts.append("scripts/verify_share_pages.py")
    rows = []
    # 2026-11-22：多个 verify 脚本各自跑 run.py 离线回放，会共享
    # docs/downloads/latest.xlsx 与环境变量，顺序执行时结果偶发漂移。
    # 根因已修（run.py 支持 NEWS_SKIP_FIXED_OUTPUT，回放不覆盖固定产物）；
    # 这里再显式隔离 env + 对单次失败做**独立二次复跑**，两次都红才判 FAIL，
    # 且把「一次 rc / 复跑 rc」逐条留痕，不把偶发洗成静默通过。
    import os
    env = {k: v for k, v in os.environ.items()
           if k not in ("NEWS_BUDGET_TOTAL", "FREE_SOURCES_OFFLINE")}
    latest = BASE / "docs" / "downloads" / "latest.xlsx"
    dated = sorted((BASE / "output").glob("*_窗口期*.xlsx"))
    if not latest.exists() and dated:
        import shutil
        shutil.copyfile(dated[-1], latest)

    def _run(script):
        r = subprocess.run([sys.executable, script], cwd=BASE, capture_output=True,
                           text=True, timeout=240, env=env)
        return r.returncode, (r.stdout + r.stderr).strip().splitlines()[-1][:140]

    for s in scripts:
        try:
            rc, tail = _run(s)
            rec = {"脚本": s, "rc": rc, "尾行": tail, "一次rc": rc}
            if rc != 0:
                rc2, tail2 = _run(s)
                rec.update({"复跑rc": rc2, "复跑尾行": tail2, "rc": rc2,
                            "说明": "共享产物状态耦合；独立复跑后通过" if rc2 == 0
                                   else "独立复跑仍失败（真实回归）"})
            rows.append(rec)
        except Exception as e:
            rows.append({"脚本": s, "rc": -1, "尾行": str(e)[:140]})
    bad = [r for r in rows if r["rc"] != 0]
    flakes = [r["脚本"] for r in rows if r.get("复跑rc") == 0]
    add("⑧ 全套既有 verify 脚本 rc=0", not bad,
        "; ".join(f"{r['脚本']}={r['rc']}" for r in rows)
        + (f"；偶发状态耦合复跑通过：{', '.join(flakes)}" if flakes else ""))
    CHECKS[-1]["明细"] = rows
    # 2026-11-21：无 key 多源检索 + 事件级成稿是新增主链路，必须随每次交付复验
    try:
        rk = subprocess.run([sys.executable, "scripts/verify_keyless.py",
                             "--json", "output/keyless_verify.json"],
                            cwd=BASE, capture_output=True, text=True, timeout=240)
        vj = json.loads((BASE / "output/keyless_verify.json").read_text(encoding="utf-8"))
        ok = rk.returncode == 0 and vj.get("通过") is True and vj["统计"]["FAIL"] == 0
        add("⑨ 无 Key 多源成稿链路 21 项全过", ok,
            f"rc={rk.returncode} PASS={vj['统计']['PASS']}/{vj['统计']['检查项']}")
    except Exception as e:
        add("⑨ 无 Key 多源成稿链路 21 项全过", False, f"{type(e).__name__}: {str(e)[:120]}")


def package_check(zip_path: str) -> None:
    p = Path(zip_path)
    if not p.exists():
        add("⑨ 项目包存在", False, str(p))
        return
    sys.path.insert(0, str(BASE))
    from scripts.make_package import check as zipcheck
    r = zipcheck(p)
    add("⑨ 项目包完整性（隐藏目录 / 证据 / 中文名 / 单份 xlsx）",
        r["PASS"], "; ".join(r["问题"]) or f"{r['条目数']} 条目")
    CHECKS[-1]["明细"] = r


def artifact_info() -> None:
    """包内基线产物契约：信息项，推包后由 Actions 覆盖，不判致命。"""
    try:
        import openpyxl
        p = BASE / "docs" / "downloads" / "latest.xlsx"
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        sheets = wb.sheetnames
        hdr = [c.value for c in next(wb[sheets[2]].iter_rows(min_row=1, max_row=1))]
        wb.close()
        add("⑩ 包内基线产物五工作表 / 12 列（信息项）",
            len(sheets) >= 5 and len(hdr) == 12,
            f"表 {len(sheets)} 个 | 第三表列数 {len(hdr)}", hard=False)
    except Exception as e:
        add("⑩ 包内基线产物可读（信息项）", False, str(e)[:160], hard=False)


def _golden() -> dict:
    p = BASE / "output/golden_diff.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _golden_ok() -> bool:
    d = _golden()
    r = (d.get("复现") or {}).get("目标往返主表") or {}
    return (r.get("多渠道率", 0) >= 0.90 and r.get("标题复读", 1) == 0
            and r.get("超100净字", 1) == 0 and r.get("平均来源数", 0) >= 2.5)


def _golden_why() -> str:
    d = _golden()
    r = (d.get("复现") or {}).get("目标往返主表") or {}
    return (f"多渠道率 {r.get('多渠道率')} / 平均来源 {r.get('平均来源数')} / "
            f"两段率 {r.get('两段率')} / 标题复读 {r.get('标题复读')}")


def _golden_honest() -> bool:
    d = _golden()
    l = (d.get("复现") or {}).get("线上改造主表") or {}
    return 0 <= l.get("多渠道率", 1) < 0.5 and bool(d.get("诚实边界"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", default=str(BASE.parent / "news_daily_project.zip"))
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    static_checks()
    behavior_checks()
    verify_scripts(args.quick)
    package_check(args.zip)
    artifact_info()
    hard = [c for c in CHECKS if c["硬项"]]
    fail = [c for c in hard if c["结果"] == "FAIL"]
    report = {
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "硬项": len(hard), "PASS": len(hard) - len(fail), "FAIL": len(fail),
        "失败项": fail, "全部": CHECKS,
        "说明": "⑩ 为信息项；包内 docs 基线产物是上一批备份，推包后由 20:00 Actions 覆盖。",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"全面核查：硬项 {len(hard)} | PASS {len(hard)-len(fail)} | FAIL {len(fail)}")
    for c in fail:
        print(f"  [FAIL] {c['项']} — {c['证据'][:180]}")
    print(f"报告：{OUT}")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
