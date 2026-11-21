# -*- coding: utf-8 -*-
"""get-news · 全局运行预算（防止 workflow 15 分钟硬杀导致整批白跑）。

当前 CI 真实设定：采集步 `timeout-minutes: 15`，env `NEWS_BUDGET_TOTAL=600`（10 分钟），
本模块再留 `export_reserve_seconds=25` 给筛选 / 评级 / 编排 / 五工作表出表。
三者是同一条链：600s 预算先短路采集层 → 必定出表 → 15min 只是兜底硬杀。

事故根因不是某一层慢，而是**每层各有一个独立 deadline，没有全局总预算**：
  keyless 420s + 正文补采 110s + 二次补采 130s + search body 180s
  + GDELT 20×5.2s + 10 平台×4 通路×7s + 社交逐条回查（max_lookup=0 不限）
  + RSS 采集约 16 分钟 —— 叠加远超硬杀线。到点 SIGTERM 把 Python 杀在
export_excel 之前，后面建站 / 提交 / 部署 6 个 step 全跳过，页面不更新。

本模块做三件事：
  1. apply(cfg)：把各层 deadline / 调用上限**裁剪到全局总预算内**，并留出
     export_reserve_seconds 给筛选、评级、编排、五工作表出表；
  2. gate(name, cfg)：预算耗尽时把该层开关关掉，让模块快速返回，
     **短路采集层但绝不终止整批**；
  3. report()：逐层配额 / 实耗 / 状态写进 Excel「工程留痕 N」与
     output/budget_report.json，可复跑核对。

原则：宁可少抓几层，也必须按时出表。不调低质量阈值、不改摘要口径、不改评级。
"""
from __future__ import annotations

import copy
import os
import time
from typing import Any, Dict, List

# 2026-11-22：与 config.yaml budget.layer_seconds 一致。总量 510s（原 540s），
# 单源 RSS 让位给生产多渠道事件的 keyless 主链路；总耗时上限反而下降 30s。
DEFAULT_LAYERS = {
    "rss": 95,
    "keyless": 185,
    "platform": 25,
    "free_sources": 15,
    "expansion": 35,
    "social": 35,
    "body": 70,
    "second_pass": 50,
}
LAYER_ORDER = list(DEFAULT_LAYERS)


class BudgetExpired(Exception):
    """采集层预算耗尽：由 Collector.run 捕获，保留已抓条目继续出表。"""


class Budget:
    def __init__(self, cfg: Dict[str, Any]):
        b = dict((cfg.get("budget") or {}))
        self.enabled = bool(b.get("enabled", True))
        self.total = float(os.environ.get("NEWS_BUDGET_TOTAL")
                           or b.get("total_collect_seconds", 600))
        self.reserve = float(b.get("export_reserve_seconds", 25))
        self.layers = dict(DEFAULT_LAYERS)
        self.layers.update({k: float(v) for k, v in (b.get("layer_seconds") or {}).items()})
        self.t0 = time.monotonic()
        self.marks: List[Dict[str, Any]] = []
        self.skipped: List[str] = []
        self.applied: Dict[str, Any] = {}

    # ---------------------------------------------------------------- 时间
    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    def left(self) -> float:
        return max(0.0, self.total - self.elapsed())

    def usable(self) -> float:
        """留给采集层的可用时间（扣掉出表保留）。"""
        return max(0.0, self.left() - self.reserve)

    def slice(self, name: str) -> float:
        """该层此刻最多能用多少秒 = min(层配额, 全局剩余可用)。"""
        if not self.enabled:
            return float(self.layers.get(name, 0))
        return max(0.0, min(float(self.layers.get(name, 0)), self.usable()))

    # ---------------------------------------------------------------- 接线
    def apply(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """深拷贝配置，把所有已知耗时参数裁剪到全局预算内。"""
        c = copy.deepcopy(cfg)
        if not self.enabled:
            return c
        sl = lambda n: max(1.0, self.slice(n))  # noqa: E731

        live = c.setdefault("source", {}).setdefault("live", {})
        live["request_timeout"] = min(float(live.get("request_timeout", 8)), 5.0)
        live["article_timeout"] = min(float(live.get("article_timeout", 6)), 5.0)
        live["max_articles"] = min(int(live.get("max_articles", 400)), 260)
        live["body_deadline_seconds"] = min(float(live.get("body_deadline_seconds", 110)), sl("body"))
        sp = live.setdefault("second_pass", {})
        sp["max_items"] = min(int(sp.get("max_items", 200)), 120)
        sp["deadline_seconds"] = min(float(sp.get("deadline_seconds", 130)), sl("second_pass"))

        sa = c.setdefault("search_api", {})
        sa["max_calls"] = min(int(sa.get("max_calls", 300)), 160)
        sa["timeout"] = min(float(sa.get("timeout", 9)), 5.0)
        sa["max_body_fetch"] = min(int(sa.get("max_body_fetch", 300)), 160)
        sa["body_timeout"] = min(float(sa.get("body_timeout", 9)), 6.0)
        sa["body_deadline_seconds"] = min(float(sa.get("body_deadline_seconds", 180)), sl("body"))

        kl = c.setdefault("keyless", {})
        kl["deadline_seconds"] = min(float(kl.get("deadline_seconds", 420)), sl("keyless"))
        kl["gdelt_max_total_calls"] = min(int(kl.get("gdelt_max_total_calls", 20)), 4)
        kl["gdelt_gap_seconds"] = min(float(kl.get("gdelt_gap_seconds", 5.2)), 5.1)
        kl["timeout"] = min(float(kl.get("timeout", 9)), 6.0)

        se = c.setdefault("source_expansion", {})
        se["gdelt_max_queries"] = min(int(se.get("gdelt_max_queries", 6)), 2)
        se["gdelt_gap_seconds"] = min(float(se.get("gdelt_gap_seconds", 5.2)), 5.1)
        se["timeout"] = min(float(se.get("timeout", 8)), 5.0)
        se["gnews_max_sites"] = min(int(se.get("gnews_max_sites", 10)), 4)
        se["per_query"] = min(int(se.get("per_query", 12)), 6)

        sm = c.setdefault("social_main", {})
        # 0 = 不限是上一版为「更多 A」放的，但逐条回查无上限会把整批拖死；
        # 改为按价值排序后的有限配额（见 social_main 的 _priority_words）。
        sm["max_lookup"] = min(int(sm.get("max_lookup", 40)) or 40, 40)

        pa = c.setdefault("platform_access", {})
        pa["timeout"] = min(float(pa.get("timeout", 7)), 4.0)

        fs = c.setdefault("free_sources", {})
        fs["free_probe_timeout"] = min(float(fs.get("free_probe_timeout", 4)), 3.0)
        fs["jina_timeout"] = min(float(fs.get("jina_timeout", 3)), 3.0)

        # 把采集层截止时刻传给 Collector（在 _get 里检查，抛出 BudgetExpired 即停）
        c.setdefault("budget", {})["_deadline_at"] = time.monotonic() + sl("rss")
        self.applied = {"_deadline_at": c["budget"]["_deadline_at"],
            "keyless_deadline": kl["deadline_seconds"], "gdelt_calls": kl["gdelt_max_total_calls"],
            "body_deadline": live["body_deadline_seconds"],
            "second_pass_deadline": sp["deadline_seconds"],
            "max_calls": sa["max_calls"], "max_lookup": sm["max_lookup"],
            "expansion_queries": se["gdelt_max_queries"],
        }
        return c

    def gate(self, name: str, cfg: Dict[str, Any]) -> bool:
        """预算不足时关掉该层开关，返回是否允许执行。同时记录上一层实耗。"""
        now = time.monotonic()
        if self.marks:
            _prev = self.marks[-1]
            _prev["实耗秒"] = round(now - _prev.get("_at", now), 1)
        allowed = bool(self.enabled) and self.slice(name) > 1.0
        self.marks.append({"层": name, "配额秒": self.layers.get(name, 0),
                           "剩余全局秒": round(self.left(), 1), "允许": allowed,
                           "_at": now})
        if not allowed:
            self.skipped.append(name)
            g = {
                "keyless": ("keyless", "enabled"), "platform": ("platform_access", "enabled"),
                "free_sources": ("search_api", "open_hot_sources"),
                "expansion": ("source_expansion", "enabled"),
                "social": ("social_main", "enabled"),
                "second_pass": ("source", "live", "second_pass_enabled"),
                "body": ("source", "live", "fetch_article"),
            }.get(name)
            if g:
                d = cfg
                for k in g[0:-1]:
                    d = d.setdefault(k, {})
                d[g[-1]] = False
        return allowed

    def deadline_at(self) -> float:
        return float((self.applied.get("_deadline_at") or 0))

    def report(self) -> Dict[str, Any]:
        now = time.monotonic()
        rows = []
        for m in self.marks:
            r = {k: v for k, v in m.items() if not k.startswith("_")}
            r.setdefault("实耗秒", round(now - m.get("_at", now), 1))
            rows.append(r)
        return {
            "启用": self.enabled, "总预算秒": self.total, "出表保留秒": self.reserve,
            "实际耗时秒": round(self.elapsed(), 1), "剩余秒": round(self.left(), 1),
            "被短路的层": self.skipped, "参数裁剪": self.applied,
            "逐层": rows,
            "结论": ("预算内完成" if not self.skipped else
                     "预算耗尽，已短路 " + "、".join(self.skipped) + "；采集层停止但本批仍出表"),
        }


def check(deadline_at: float) -> None:
    """给 Collector._get 用：到点抛 BudgetExpired，由 run() 捕获后继续出表。"""
    if deadline_at and time.monotonic() > deadline_at:
        raise BudgetExpired("采集层全局预算耗尽，保留已抓条目继续出表")
