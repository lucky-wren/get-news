#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冒烟：确认「工程留痕 L · 两类渠道解析」真的写进五工作表 Excel。

用最小 R + 安全日志 stub 调 export_excel，不联网、不覆盖正式产物。
"""
from __future__ import annotations

import sys
import yaml
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from news_pipeline.timewindow import Window            # noqa: E402
from news_pipeline.exporter import export_excel        # noqa: E402
from news_pipeline.channels import ChannelRegistry     # noqa: E402
from news_pipeline.model import RawItem, COLS_12       # noqa: E402


class _Any(dict):
    """日志 stub：任意层级索引 / .get / str 都不抛异常，返回空值。"""

    def __missing__(self, k):
        return _Any()

    def __getitem__(self, k):
        return _Any()

    def get(self, k, d=None):
        return _Any() if d is None else d

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __str__(self):
        return ""

    def __bool__(self):
        return False

    # exporter 会拿 log["score"]["lambda"] 做 Final_adj 逐条复算，stub 当 0 处理
    def __float__(self):
        return 0.0

    def __mul__(self, o):
        return 0.0

    def __rmul__(self, o):
        return 0.0

    def __add__(self, o):
        return 0.0 if isinstance(o, (int, float)) else ""

    def __radd__(self, o):
        return 0.0 if isinstance(o, (int, float)) else ""

    def __sub__(self, o):
        return 0.0 if isinstance(o, (int, float)) else ""

    def __rsub__(self, o):
        return 0.0 if isinstance(o, (int, float)) else ""


def main() -> int:
    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8")) or {}
    reg = ChannelRegistry(str(BASE / "data" / "资讯渠道大全_31类扩展版.md"),
                          str(BASE / "data" / "channels_extra.yaml"))
    win = Window(start=datetime(2026, 11, 19, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                 end=datetime(2026, 11, 20, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                 interval="lo-ro", tz="Asia/Shanghai")
    it = RawItem(title="测试条目 LPR 保持不变",
                 abstract="央行公布最新贷款市场报价利率，1年期为3.0%。",
                 abstract_status="有效", source="中新网", group="财经市场",
                 tag="房地产 / 地方债务", eid="e1001", record_type="独立原文",
                 evidence="A", seq=1, grade="B", Final=60.0, Final_adj=60.0,
                 score_order=1, display_order=1)
    log = _Any()
    log["mode"] = "live"
    R = {"window": win, "cfg": cfg, "registry": reg,
         "candidate": [it], "main": [it], "ordered": [it], "logs": log}
    p = BASE / "output" / "_smoke_L.xlsx"
    export_excel(p, R)

    from openpyxl import load_workbook
    wb = load_workbook(p, read_only=True, data_only=True)
    txt = "\n".join(" | ".join(str(x or "") for x in row)
                    for row in wb["口径与稿外信息"].iter_rows(values_only=True))
    sheets_ok = wb.sheetnames == ["处理后的数据", "排名最靠前的10条",
                                 "窗口内全部数据", "口径与稿外信息", "评级留痕"]
    checks = {
        "五工作表齐全": sheets_ok,
        "12 列表头一致": all(str(x or "").strip() in COLS_12
                        for x in next(wb["处理后的数据"].iter_rows(values_only=True))[:12]),
        "留痕 L 标题": "工程留痕 L · 两类渠道逐条解析" in txt,
        "L-a 实测明细": "工程留痕 L-a" in txt,
        "L-b 逐渠道通路": "工程留痕 L-b" in txt,
        "Bing site 实测证据": "Bing site:mof.gov.cn" in txt,
        "零通路条数": "零通路条数" in txt,
        "诚实边界": "不等于本批已抓到" in txt,
    }
    wb.close()
    p.unlink(missing_ok=True)
    bad = [k for k, v in checks.items() if not v]
    print("Excel 留痕 L 冒烟：", "PASS" if not bad else "FAIL",
          f"({len(checks) - len(bad)}/{len(checks)})")
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} | {k}")
    if bad:
        print("失败项:", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
