#!/usr/bin/env python3
"""get-news · 资讯获取 流水线 CLI。

窗口期设置（Asia/Shanghai，端点开闭可用 --interval 切换；用户拍板默认左开右开）：
  python run.py --date 2026-11-17                              # 默认 (11-16 20:00, 11-17 20:00) 左开右开
  python run.py --start "2026-11-16 20:00" --end "2026-11-17 08:00"
  python run.py --window "2026-11-16 20:00~2026-11-17 08:00" --interval lo-ro   # 左开右开（默认）
  python run.py --date 2026-11-17 --hours 12                   # 右界 20:00 往前 12 小时
  --interval: lo-ro 左开右开(默认) / lo-rc 左开右闭 / lc-ro 左闭右开 / lc-rc 左闭右闭
产物命名：窗口右界年月日 + 窗口期，例 2026_11_17_窗口期.xlsx
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import traceback
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from news_pipeline import runlog                         # noqa: E402
from news_pipeline.quality import compute_quality        # noqa: E402
from news_pipeline.pipeline import run_pipeline          # noqa: E402
from news_pipeline.timewindow import build_window        # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(description="抓取整合新闻资讯 → 按 SKILL 评级 → 五工作表 Excel")
    p.add_argument("--config", default=str(BASE / "config.yaml"))
    w = p.add_argument_group("窗口期")
    w.add_argument("--date", help="右界日期，如 2026-11-17；默认窗口 (前一日20:00, 当日20:00) 左开右开")
    w.add_argument("--start", help="窗口左界，如 '2026-11-16 20:00'（端点是否计入由 --interval 决定）")
    w.add_argument("--end", help="窗口右界，如 '2026-11-17 08:00'（端点是否计入由 --interval 决定）")
    w.add_argument("--window", help="'2026-11-16 20:00~2026-11-17 08:00'")
    w.add_argument("--hours", type=float, help="窗口跨度小时数（右界往前推）")
    w.add_argument("--anchor", help="relative 窗口的锚点时刻，留空=当前时刻；如 '2026-11-17 08:00'")
    w.add_argument("--tz", default=None, help="时区，默认 Asia/Shanghai")
    w.add_argument("--interval", choices=["lo-ro", "lo-rc", "lc-ro", "lc-rc"], default=None,
                   help="端点开闭：lo-ro 左开右开(默认) / lo-rc 左开右闭 / lc-ro 左闭右开 / lc-rc 左闭右闭")
    w.add_argument("--strict-window", action="store_true", help="发布时间未取得的条目一律不入池")
    s = p.add_argument_group("数据源")
    s.add_argument("--source", choices=["live", "target", "demo", "inbox", "hybrid"],
                   help="采集模式；默认 live = 真实联网多源抓取（受限网络下按可达性自适应）")
    s.add_argument("--inbox", help="外部注入 JSON / xlsx 台账路径")
    s.add_argument("--rss", action="store_true", help="live 模式开启 RSS 采集")
    s.add_argument("--apis", action="store_true", help="live 模式开启结构化 API")
    s.add_argument("--html", action="store_true", help="live 模式开启站内列表页")
    s.add_argument("--hotboards", action="store_true", help="live 模式开启榜单快照（词条需回查）")
    s.add_argument("--search", action="store_true", help="live 模式开启 Serper/Tavily 回查（需 key）")
    r = p.add_argument_group("评级口径")
    r.add_argument("--lambda", dest="lam", type=float, help="同类边际递减 λ，默认 6；0 退回纯 Final 降序")
    r.add_argument("--strictness", choices=["strict", "moderate", "off"],
                   help="大众贴近度档位（SKILL §1.5 三档）：默认 strict；moderate = 未命中三类强剔除即通过；off = 关闭本层")
    r.add_argument("--max-items", type=int, help="只保留 N 条，走保护截断")
    r.add_argument("--legacy-groups", action="store_true", help="保留样例台账的扩展大类原值")
    r.add_argument("--no-orchestrate", action="store_true", help="关闭编排层，按纯 Final_adj 降序")
    o = p.add_argument_group("输出")
    o.add_argument("--output-dir", help="输出目录")
    o.add_argument("--name", help="文件名模板，默认 {date}_窗口期.xlsx")
    o.add_argument("--suffix", default="", help="文件名附加后缀（用于短窗对照产物）")
    o.add_argument("--verify-short-window", action="store_true",
                   help="额外用 (start, start+12h] 跑一遍，验证窗口期确实生效")
    o.add_argument("--json-summary", action="store_true", help="同时输出运行摘要 JSON")
    o.add_argument("--deliver", action="store_true",
                   help="额外产出 01 第六节规定的 daily_news_YYYYMMDD_20h.xlsx 交付副本（仓库链路仍保留固定 latest.xlsx）")
    o.add_argument("--spec-verify", action="store_true",
                   help="出表后用 news_pipeline/spec.py 做纯代码规格验收，结果写 output/spec_verify.json")
    lg = p.add_argument_group("运行日志")
    lg.add_argument("--log-dir", default="logs", help="日志目录，默认 logs/（可用 NEWS_LOG_DIR 覆盖）")
    lg.add_argument("--no-log", action="store_true", help="不写运行日志（仍可正常运行）")
    lg.add_argument("--keep-logs", type=int, default=200, help="启动时保留最近 N 次运行的日志，默认 200")
    return p.parse_args()


def make_cfg(args) -> dict:
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    cfg.setdefault("window", {})
    if args.tz:
        cfg["window"]["timezone"] = args.tz
    if args.strict_window:
        cfg["window"]["strict_window"] = True
    cfg.setdefault("source", {})
    if args.source:
        cfg["source"]["mode"] = args.source
    if args.inbox:
        cfg["source"]["inbox_path"] = args.inbox
        if not args.source:
            cfg["source"]["mode"] = "inbox"
    if not args.source and not args.inbox:
        cfg["source"]["mode"] = "live"
    live = cfg["source"].setdefault("live", {})
    for flag, key in (("rss", "rss"), ("apis", "apis"), ("html", "html_lists"),
                      ("hotboards", "hotboards"), ("search", "search")):
        if getattr(args, flag):
            live[key] = True
    if args.lam is not None:
        cfg.setdefault("scoring", {})["lambda_"] = args.lam
    if args.strictness:
        cfg.setdefault("rules", {})["mass_strictness"] = args.strictness
    if args.max_items:
        cfg.setdefault("scoring", {})["max_items"] = args.max_items
    if args.legacy_groups:
        cfg.setdefault("rules", {})["legacy_groups"] = True
    if args.no_orchestrate:
        cfg.setdefault("orchestrate", {})["enabled"] = False
    cfg.setdefault("output", {})
    if args.output_dir:
        cfg["output"]["dir"] = args.output_dir
    if args.name:
        cfg["output"]["name"] = args.name
    if args.suffix:
        cfg["output"]["suffix"] = args.suffix
    return cfg


def sync_single_xlsx(cfg: dict, out: Path, apply_latest: bool = True) -> Path:
    """需求⑤：单个 xlsx。output 目录只留本批一份；docs/downloads/latest.xlsx 是它的字节副本。

    页面固定地址与下载链接永不变；不累积往期归档、不产生第二套口径。

    2026-11-22 线上 CI 截图暴露顺序漏洞：旧版「清理往期 + 覆盖 latest」在规格验收
    **之前**做完，日志先打印 `[单文件交付] → 覆盖 docs/downloads/latest.xlsx`，
    下一行才 `[规格验收] 不达标`，最后 `[撤回] …（不交付 / 不覆盖固定地址）`——
    固定地址其实已经被覆盖了，撤回声明不成立（只是 job 变红让后续 commit 没跑，
    仓库里的 latest 才侥幸没变）。现拆两步：apply_latest=False 只清往期供验收，
    **验收通过后**才 apply_latest=True 真正覆盖固定地址。
    """
    import os
    import shutil
    # 2026-11-22：只清理「按年月日+窗口期」命名的日期产物。
    # output/verification/ 下的规格复现证据 xlsx 属验收材料，不能被真实跑批误删；
    # 旧版 `glob("*.xlsx")` 会把它们一起删掉，导致打包完整性 FAIL。
    for f in out.parent.glob("*_窗口期*.xlsx"):
        if f.resolve() != out.resolve():
            f.unlink()
    name = cfg.get("output", {}).get("latest_name", "latest.xlsx")
    latest = BASE / "docs" / "downloads" / name
    if not apply_latest:
        return latest
    if os.environ.get("NEWS_SKIP_FIXED_OUTPUT") == "1":
        # 离线回放专用：不覆盖固定下载文件，避免多个 verify 脚本并发/顺序互相踩。
        return latest
    latest.parent.mkdir(parents=True, exist_ok=True)
    # copy2 会 copystat（utime），在 Actions/沙盒里目标文件属主不同会抛
    # PermissionError: [Errno 1] Operation not permitted，导致「出表成功但整批失败」。
    # 这里只需要内容一致，不需要复制时间戳。
    shutil.copyfile(out, latest)
    return latest

def output_path(cfg: dict, win, suffix: str = "") -> Path:
    ocfg = cfg.get("output", {})
    name = ocfg.get("name", "{date}_窗口期.xlsx").format(date=win.end.strftime("%Y_%m_%d"))
    suffix = suffix or ocfg.get("suffix", "")
    if suffix:
        stem, ext = Path(name).stem, Path(name).suffix
        name = f"{stem}{suffix}{ext}"
    d = Path(ocfg.get("dir", "output"))
    if not d.is_absolute():
        d = BASE / d
    return d / name


def make_window(cfg: dict, args, *, override_start=None, override_end=None):
    w = cfg.get("window", {})
    return build_window(
        start=(override_start or args.start or w.get("start")),
        end=(override_end or args.end or w.get("end")),
        date=(args.date or w.get("date")),
        window=(None if override_start else args.window),
        hours=(args.hours if args.hours is not None else w.get("hours")),
        tz=w.get("timezone", "Asia/Shanghai"),
        interval=(args.interval or w.get("interval", "lo-ro")),
    )


def relative_window(cfg: dict, args):
    """config.window.mode=relative 时，以运行时刻（或 --anchor）为锚算起止。

    命令行显式给了 --start/--end/--date/--window 则完全不介入，保证可复现。
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    w = cfg.get("window", {})
    if w.get("mode") != "relative":
        return None, None
    if args.start or args.end or args.date or args.window:
        return None, None
    tz = ZoneInfo(w.get("timezone", "Asia/Shanghai"))
    if args.anchor:
        anchor = datetime.strptime(args.anchor, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    else:
        anchor = datetime.now(tz).replace(second=0, microsecond=0)
    left = anchor + timedelta(hours=float(w.get("start_offset_hours", -12)))
    right = anchor + timedelta(hours=float(w.get("end_offset_hours", 0)))
    return left.strftime("%Y-%m-%d %H:%M"), right.strftime("%Y-%m-%d %H:%M")


def summarize(R: dict) -> dict:
    return {
        "产物": str(R["path"]),
        "产物名": R["path"].name,
        "采集窗口": R["window"].label,
        "端点开闭": R["window"].interval_name,
        "interval": R["window"].interval,
        "窗口跨度_小时": R["window"].span_hours,
        "原始采集条数": R["raw_count"],
        "原始采集条数口径": "采集层去重前全量台账 len(raw)；含结构化源 / 站内时间倒排 / "
                      "搜索成稿 / 热榜词条 / 拼盘拆条，≥ 口径表「实时源原始条目」len(specs)",
        "候选池条数": len(R["candidate"]),
        "主表条数": len(R["ordered"]),
        "前10覆盖大类数": R["logs"]["orch"].get("前10覆盖大类数"),
        # 旧值取自准入全量台账（含聚类/议题簇合并掉的条目），曾出现合计 302 > 主表 208
        # 的自相矛盾；与 exporter「统计值」统一按主表 ordered 逐条重数。
        "等级分布": {g: sum(1 for x in R["ordered"] if str(x.grade) == g)
                 for g in "SABCD"},
        "等级分布口径": "主表 ordered 逐条重数（台账口径见评级留痕）",
        "H通道": "开启" if R["logs"]["score"]["h_enabled"] else "关闭（仅价值分 V）",
        "渠道触达": R["registry"].summary(),
        "实时源触达": R["logs"].get("live_cn", {}),
        "窗口外排除": len(R["logs"]["excluded"]),
        "时点推定入池": len(R["logs"]["estimated"]),
        "国际资讯": R["logs"]["intl"],
        "议题簇": R["logs"]["story"],
        "采集质量": R.get("quality", {}),
        "配置": {
            "定时": (R["cfg"].get("schedule", {}).get("run_at")),
            "UTC cron": (R["cfg"].get("schedule", {}).get("cron_utc")),
            "窗口模式": (R["cfg"].get("window", {}).get("mode")),
            "覆盖产物": (R["cfg"].get("output", {}).get("latest_name")),
        },
    }


def _reject_artifact(out: Path, reason: str) -> None:
    """不达标时撤回本批产物：正式 xlsx 不留、只留 .rejected + 原因 json 供排障。

    2026-11-22 真联网 live 探测发现：run.py 先出表再判质量门禁，rc=1 虽然拦住了
    workflow 后续的建站/提交/部署，但 runner 工作目录还躺着一份不达标的 xlsx。
    这不是「交付」（固定页面仍是上一批），但语义不干净；撤回后「不达标不交付」
    从「不部署」变成「连正式产物名都不存在」。
    """
    try:
        if not out.exists():
            return
        rej = out.with_name(out.name + ".rejected")
        out.replace(rej)
        out.with_suffix(".reject.json").write_text(
            json.dumps({"批次": out.name, "撤回原因": reason,
                        "处置": "本批不达标，产物已撤回；未覆盖固定地址、未提交、未部署",
                        "排障": "逐项指标见同目录运行摘要 JSON 与 logs/ci-run.log"},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[撤回] 本批不达标，产物已改为 {rej.name}（不交付 / 不覆盖固定地址）")
    except Exception as e:                       # 撤回失败不掩盖质量失败
        print(f"[撤回] 未能重命名产物（不影响失败判定）：{e}")


def main() -> int:
    args = parse_args()
    log_dir = Path(os.environ.get("NEWS_LOG_DIR") or args.log_dir)
    if not log_dir.is_absolute():
        log_dir = BASE / log_dir
    rl = None
    if not args.no_log:
        runlog.prune(log_dir, args.keep_logs)
        rl = runlog.RunLog(log_dir, title="pipeline",
                           index=os.environ.get("NEWS_LOG_INDEX", "1") != "0",
                           echo=False)
        runlog.set_current(rl)
        rl.kv("命令行参数", {k: v for k, v in vars(args).items() if v not in (None, False, "")})
        rl.event("INFO", "运行环境", python=sys.version.split()[0], cwd=str(BASE),
                 时区=os.environ.get("TZ", "Asia/Shanghai"), 日志目录=str(log_dir))
        print(f"[日志] {rl.path}")

    try:
        cfg = make_cfg(args)
        rs, re_ = relative_window(cfg, args)
        win = make_window(cfg, args, override_start=rs, override_end=re_)
        out = output_path(cfg, win)
        if rl:
            rl.kv("解析后的窗口与配置", {
                "采集窗口": win.label, "端点开闭": win.interval_name,
                "跨度小时": win.span_hours, "时区": win.tz,
                "数据源": cfg.get("source", {}).get("mode"),
                "贴近度档位": cfg.get("rules", {}).get("mass_strictness", "strict"),
                "λ": cfg.get("scoring", {}).get("lambda_", 6),
                "输出文件": str(out),
            })
        print(f"[窗口] {win.label}  跨度 {win.span_hours:.1f}h")
        print(f"[模式] {cfg.get('source', {}).get('mode')}  [产物] {out}")
        R = run_pipeline(cfg, win, BASE, out)
        R["cfg"] = cfg
        R["quality"] = R.get("quality") or compute_quality(R, cfg)
        s = summarize(R)
        q = R["quality"]
        if rl:
            rl.kv("采集质量门禁", {"结论": q["结论"], "未达标项": q["未达标项"] or "无",
                                  **{k: f"{v['实测']} ({'下限' if v.get('方向') == 'min' else '上限'} " +
                                        str(v.get("下限", v.get("上限"))) + ")"
                                     for k, v in q["指标"].items()}})
            if q["未达标项"]:
                rl.event("WARN", "采集质量未达标（不伪造成品，仅如实标注）",
                         未达标="、".join(q["未达标项"]), 处置=q["结论"])
        print("[质量] " + q["结论"] +
              ("；未达标：" + "、".join(q["未达标项"]) if q["未达标项"] else ""))
        # 2026-11-22：旧版 raise 嵌在 if rl: 里，--no-log 就绕过整个硬门禁。
        # 质量是否达标与「是否写运行日志」无关；内容硬指标不达标一律失败，
        # 否则又会出现「[质量] 告警/不达标」却继续建站部署的假成功。
        if q["结论"].startswith("不达标"):
            reason = "采集质量门禁未达标：" + "、".join(q["未达标项"])
            _reject_artifact(Path(out), reason)
            raise RuntimeError(reason)
        # 2026-11-22 二次修正：形态硬闸全部通过、仅多渠道覆盖不足时，出表但显式标注。
        # q["未达标项"] 仍含这两项 → 页面质量行 ⚠ 红条、CI 摘要、Excel 工程留痕 A 都看得到；
        # 不 raise 是为了不让页面永久停在上一批（上一轮硬门禁引入的副作用）。
        if q.get("产能未达标项"):
            print("[产能] 内容形态全部达标，但 " + "、".join(q["产能未达标项"]) +
                  " 未达阈值；已如实标注，不用低质条目凑数。" +
                  "提升：配 SERPER_API_KEY / 补 ★ 端点 / 提高预算余量内正文补采配额。")
        if rl:
            s["run_id"] = rl.run_id
            s["日志文件"] = rl.path.name
            s["阶段耗时"] = rl.stages
        print(json.dumps(s, ensure_ascii=False, indent=2))

        if args.verify_short_window:
            from datetime import timedelta
            short_cfg = copy.deepcopy(cfg)
            short_end = win.start + timedelta(hours=12)
            if short_end >= win.end:
                print("[短窗验证] 原窗口不足 12 小时，跳过")
            else:
                short_win = build_window(start=win.start.strftime("%Y-%m-%d %H:%M"),
                                         end=short_end.strftime("%Y-%m-%d %H:%M"),
                                         tz=win.tz, interval=win.interval)
                short_out = output_path(short_cfg, short_win, suffix="_短窗12h")
                R2 = run_pipeline(short_cfg, short_win, BASE, short_out)
                print(f"[短窗验证] {short_win.label} → 候选池 {len(R2['candidate'])} 条 / 主表 {len(R2['ordered'])} 条"
                      f"（原窗口 {len(R['candidate'])} / {len(R['ordered'])}），产物 {short_out}")
                if rl:
                    rl.checkpoint("短窗对照验证", 候选池=len(R2["candidate"]),
                                  主表=len(R2["ordered"]))

        if args.json_summary:
            p = out.with_suffix(".json")
            p.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")

        # 需求⑤：验收前只清理往期期次产物，让规格验收看到「单个 xlsx」。
        # 固定下载地址此时**不动**；不达标撤回时「未覆盖固定地址」才是一句真话。
        latest = sync_single_xlsx(cfg, out, apply_latest=False)
        print(f"[单文件交付] 已清理往期期次产物，待验收通过后覆盖 {latest.relative_to(BASE)}")

        if args.spec_verify:
            from news_pipeline.spec import verify_workbook, verify_extra
            # 单文件交付会清理 output/*.json；口径回归由纯代码现跑一遍再验收
            import subprocess as _sp
            import sys as _sys
            _sp.run([_sys.executable, str(BASE / "scripts" / "verify_rules.py")],
                    cwd=str(BASE), capture_output=True)
            vres = verify_workbook(str(out))
            vres["明细"] = list(vres["明细"]) + verify_extra(str(out))
            vres["检查项"] = len(vres["明细"])
            vres["通过"] = sum(1 for c in vres["明细"] if c["结果"] == "PASS")
            vres["失败"] = vres["检查项"] - vres["通过"]
            vres["结论"] = "达标" if vres["失败"] == 0 else "不达标"
            vp = out.parent / "spec_verify.json"
            vp.write_text(json.dumps(vres, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"[规格验收] {vres['结论']}  {vres['通过']}/{vres['检查项']} 项 PASS → {vp}")
            for c in vres["明细"]:
                if c["结果"] == "FAIL":
                    print(f"  FAIL | {c['项']} | {c['证据']}")
            if rl:
                rl.checkpoint("纯代码规格验收", 结论=vres["结论"],
                              通过=vres["通过"], 检查项=vres["检查项"])
            # 2026-11-22 线上事故：规格验收打印「不达标 55/59」+ 4 行 FAIL 之后，
            # run.py 仍 return 0 → CI 继续建站/提交/部署 → job success。
            # 用户要求「必须达到效果」，验收 FAIL 必须真阻断（可用 spec_fail_hard=false 关闭）。
            if bool((cfg.get("quality") or {}).get("spec_fail_hard", True)) and vres["结论"] != "达标":
                reason = ("规格验收不达标：" +
                          "；".join(c["项"] for c in vres["明细"] if c["结果"] == "FAIL"))
                _reject_artifact(Path(out), reason)
                raise RuntimeError(reason)

        # 走到这里才说明质量门禁 + 规格验收都已通过：现在才覆盖固定下载地址。
        # 不达标批次不会执行到这一步，页面与 latest.xlsx 保持上一批不变。
        latest = sync_single_xlsx(cfg, out, apply_latest=True)
        if os.environ.get("NEWS_SKIP_FIXED_OUTPUT") == "1":
            print(f"[单文件交付] {out.name} 验收通过；离线回放模式不覆盖 {latest.relative_to(BASE)}")
        else:
            print(f"[单文件交付] {out.name} → 覆盖 {latest.relative_to(BASE)}（字节一致，不累积期次）")
        if args.deliver:
            # 需求⑤：不再产出第二份副本；单 xlsx 已由 sync_single_xlsx 覆盖到固定地址
            print("[交付] 单文件已覆盖固定地址；按 01 第六节交付名 "
                  f"daily_news_{win.end.strftime('%Y%m%d')}_20h.xlsx 可用 --name 指定")

        if rl:
            rl.finish("ok", file=out.name, window=win.label, interval=win.interval,
                      interval_name=win.interval_name,
                      source=cfg.get("source", {}).get("mode"),
                      candidate=len(R["candidate"]), main=len(R["ordered"]),
                      excluded=len(R["logs"]["excluded"]),
                      estimated=len(R["logs"]["estimated"]))
        return 0
    except Exception as exc:
        if rl:
            rl.error(exc, label="流水线异常终止")
            rl.finish("error")
        print(traceback.format_exc(), file=sys.stderr)
        print(f"[失败] {type(exc).__name__}: {exc}" + (f"  日志：{rl.path}" if rl else ""),
              file=sys.stderr)
        return 1
    finally:
        runlog.set_current(None)


if __name__ == "__main__":
    raise SystemExit(main())
