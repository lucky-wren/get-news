#!/usr/bin/env python3
"""改 config.yaml 的 schedule.run_at，自动把 cron 同步进 GitHub Actions 工作流。

GitHub Actions 的 `on.schedule.cron` 必须是静态字面量，不能在运行时读配置生成，
所以「灵活配置几点跑」用这个文件做单点同步：只改一处，其余全部联动。

    python scripts/set_schedule.py 20:00          # 每天北京时间 20:00 跑（现行默认）
    python scripts/set_schedule.py 06:30 --window 24    # 06:30 跑，窗口 24h（正整数）
    python scripts/set_schedule.py --show          # 只看当前配置，不改文件
    python scripts/set_schedule.py --disable       # 暂停定时（仍可手动触发）

同步内容：config.yaml 的 cron_utc / run_at / window.start_offset_hours，
以及 .github/workflows/daily-news.yml 的 cron 行。UTC cron = 北京时间 - 8 小时。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CFG = BASE / "config.yaml"
WF = BASE / ".github" / "workflows" / "daily-news.yml"


def load():
    import yaml
    return yaml.safe_load(CFG.read_text(encoding="utf-8")) or {}


def to_utc_cron(hhmm: str) -> str:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", hhmm.strip())
    if not m:
        raise ValueError(f"时间格式应为 HH:MM，收到 {hhmm!r}")
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h < 24 and 0 <= mi < 60):
        raise ValueError(f"时间超出范围：{hhmm}")
    utc_h = (h - 8) % 24
    return f"{mi} {utc_h} * * *"


def show(cfg: dict) -> None:
    s = cfg.get("schedule", {})
    w = cfg.get("window", {})
    print("当前配置（config.yaml 单点）")
    print(f"  定时：    每天 {s.get('run_at')} {s.get('timezone')}  "
          f"({'启用' if s.get('enabled', True) else '暂停'})")
    print(f"  UTC cron：{s.get('cron_utc')}")
    print(f"  窗口：    mode={w.get('mode')}  "
          f"({w.get('start_offset_hours')}h → {w.get('end_offset_hours')}h)  "
          f"{w.get('interval')}")
    print(f"  输出：    {cfg.get('output', {}).get('name')} → "
          f"覆盖 {cfg.get('output', {}).get('latest_name')}")


def main() -> int:
    p = argparse.ArgumentParser(description="设置每天几点跑 + 资讯窗口")
    p.add_argument("run_at", nargs="?", help="北京时间 HH:MM，如 08:00 / 06:30")
    p.add_argument("--window", type=int, metavar="HOURS",
                   help="窗口跨度（正整数，右界为运行时刻，左界 = -HOURS 小时）")
    p.add_argument("--interval", choices=["lo-ro", "lo-rc", "lc-ro", "lc-rc"])
    p.add_argument("--disable", action="store_true", help="暂停定时（保留手动触发）")
    p.add_argument("--enable", action="store_true", help="恢复定时")
    p.add_argument("--show", action="store_true", help="只显示当前配置")
    args = p.parse_args()

    cfg = load()
    if args.show and not any((args.run_at, args.window, args.interval,
                              args.disable, args.enable)):
        show(cfg)
        return 0

    s = cfg.setdefault("schedule", {})
    w = cfg.setdefault("window", {})
    changed = []

    if args.run_at:
        cron = to_utc_cron(args.run_at)
        s["run_at"] = args.run_at
        s["cron_utc"] = cron
        changed.append(f"run_at={args.run_at} → UTC cron '{cron}'")
    if args.window is not None:
        if args.window <= 0:
            raise SystemExit("--window 必须是正整数小时数")
        w["mode"] = "relative"
        w["start_offset_hours"] = -args.window
        w["end_offset_hours"] = 0
        changed.append(f"窗口=运行时刻前 {args.window} 小时")
    if args.interval:
        w["interval"] = args.interval
        changed.append(f"端点开闭={args.interval}")
    if args.disable:
        s["enabled"] = False
        changed.append("定时暂停")
    if args.enable:
        s["enabled"] = True
        changed.append("定时启用")

    cron = s.get("cron_utc") or to_utc_cron(s.get("run_at", "20:00"))
    text = CFG.read_text(encoding="utf-8")
    text = re.sub(r'(^\s*run_at:\s*").*?(")', lambda m: m.group(1) + s.get("run_at", "20:00") + m.group(2),
                  text, count=1, flags=re.M)
    text = re.sub(r'(^\s*cron_utc:\s*").*?(")', lambda m: m.group(1) + cron + m.group(2),
                  text, count=1, flags=re.M)
    if args.window is not None:
        text = re.sub(r'(^\s*start_offset_hours:\s*).*$',
                      lambda m: m.group(1) + str(-args.window), text, count=1, flags=re.M)
    if args.interval:
        text = re.sub(r'(^\s*interval:\s*)[\w-]+', lambda m: m.group(1) + args.interval,
                      text, count=1, flags=re.M)
    if args.disable or args.enable:
        text = re.sub(r'(^\s*enabled:\s*)\w+',
                      lambda m: m.group(1) + ("true" if args.enable else "false"),
                      text, count=1, flags=re.M)
    CFG.write_text(text, encoding="utf-8")

    wf = WF.read_text(encoding="utf-8")
    new_wf, n = re.subn(r"(cron:\s*')[^']*(')", lambda m: m.group(1) + cron + m.group(2),
                        wf, count=1)
    if n != 1:
        print(f"[警告] 工作流里未找到 cron 行，请手动把 cron 改为 '{cron}'", file=sys.stderr)
    else:
        WF.write_text(new_wf, encoding="utf-8")
        changed.append(f"工作流 cron → '{cron}'")

    print("已同步：")
    for c in changed:
        print("  -", c)
    show(load())
    print("\n下一步：git add config.yaml .github/workflows/daily-news.yml && git commit && git push")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
