# -*- coding: utf-8 -*-
"""get-news · 全局运行预算（防止 workflow 15 分钟硬杀导致整批白跑）。

当前 CI 真实设定：采集步 `timeout-minutes: 15`，env `NEWS_BUDGET_TOTAL=600`（10 分钟），
本模块再留 `export_reserve_seconds=25` 给筛选 / 评级 / 编排 / 五工作表出表。
三者是同一条链：600s 预算先短路采集层 → 必定出表 → 15min 只是兜底硬杀。

2026-11-22 事故根因（用户截图：采集 step 15min timed out）：
  **旧版 gate() 只判断「此刻是否允许开始」，从不限制「这一层能跑多久」**。
  slice(name) = min(层配额, 全局剩余)，每层配额都远小于当时的全局剩余，
  于是 gate() 永远返回 True；配置里 510s 是「配额之和」，不是「实际耗时之和」。
  source_expansion / platform_access / free_sources / social 的 digest 抓取
  都是纯串行循环、没有整层 deadline，最坏按 timeout 累加到十几分钟；
  只有 Collector.run 的 rss 层靠 _deadline_at 受控。SIGTERM 时 Excel 未落盘，
  后面建站/提交/部署 6 步全跳过。

本版三道真闸（不再只是纸面配额）：
  1. **全局硬墙**：daemon watchdog 在 total-reserve 秒置 expired；
     gate()/slice() 立即返回 False/0，后续层全部短路，不再"允许开始"。
  2. **每层真限时 bounded()**：把整层调用放进 daemon 线程，join(timeout=limit)，
     到点立即返回 fallback、主线程继续筛选评级与出表；同时把
     `_layer_deadline_at` 注入 cfg，让模块内部循环自行 break（不靠甩线程）。
  3. **真实耗时口径**：report() 用 monotonic 实耗；出表之后由 pipeline
     再覆盖写一次 budget_report.json，避免"采集结束快照"漏算出表段却自报达标。

原则：宁可少抓几层，也必须按时出表。不调低质量阈值、不改摘要口径、不改评级。
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

# 与 config.yaml budget.layer_seconds 一致。总量 510s（原 540s），
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
        self.cfg_ref: Dict[str, Any] = {}
        self.expired = False
        self.layer_deadlines: Dict[str, float] = {}
        self._stop = threading.Event()
        # 全局硬墙：到 total-reserve 秒强制 expired。daemon 不阻止进程退出。
        self._watchdog = threading.Thread(target=self._watch, daemon=True,
                                          name="budget-watchdog")
        self._watchdog.start()

    def _watch(self) -> None:
        limit = max(0.1, self.total - self.reserve)
        # 用 Event.wait 便于测试时提前结束，不占 CPU
        self._stop.wait(limit)
        if not self._stop.is_set():
            self.expired = True

    def close(self) -> None:
        self._stop.set()

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
        if self.expired:
            return 0.0
        if not self.enabled:
            return float(self.layers.get(name, 0))
        return max(0.0, min(float(self.layers.get(name, 0)), self.usable()))

    # ---------------------------------------------------------------- 接线
    def apply(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """深拷贝配置，把所有已知耗时参数裁剪到全局预算内。"""
        c = copy.deepcopy(cfg)
        self.cfg_ref = c
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

        # 2026-11-22：扩展层必须自带整层 deadline（旧版纯串行循环按 timeout 累加到数分钟）
        se["layer_deadline_seconds"] = sl("expansion")
        pa = c.setdefault("platform_access", {})
        pa["layer_deadline_seconds"] = sl("platform")

        sm = c.setdefault("social_main", {})
        sm["max_lookup"] = min(int(sm.get("max_lookup", 40)) or 40, 40)
        sm["lookup_deadline_seconds"] = min(float(sm.get("lookup_deadline_seconds", 45)),
                                            sl("social"))
        sm["digest_deadline_seconds"] = sl("social")

        pa2 = c.setdefault("platform_access", {})
        pa2["timeout"] = min(float(pa2.get("timeout", 7)), 4.0)

        fs = c.setdefault("free_sources", {})
        fs["free_probe_timeout"] = min(float(fs.get("free_probe_timeout", 4)), 3.0)
        fs["jina_timeout"] = min(float(fs.get("jina_timeout", 3)), 3.0)
        fs["layer_deadline_seconds"] = sl("free_sources")

        # 把采集层截止时刻传给 Collector（在 _get 里检查，抛出 BudgetExpired 即停）
        c.setdefault("budget", {})["_deadline_at"] = time.monotonic() + sl("rss")
        # 全局硬墙绝对时刻：后台限量补采若发现余量不足就立即停，
        # 否则 rss 层短路后后台线程仍占 CPU，会把主线程拖过 15min 线（冒烟实测）。
        c["budget"]["_wall_at"] = self.t0 + max(0.1, self.total - self.reserve)
        self.applied = {"_deadline_at": c["budget"]["_deadline_at"],
            "keyless_deadline": kl["deadline_seconds"], "gdelt_calls": kl["gdelt_max_total_calls"],
            "body_deadline": live["body_deadline_seconds"],
            "second_pass_deadline": sp["deadline_seconds"],
            "max_calls": sa["max_calls"], "max_lookup": sm["max_lookup"],
            "expansion_queries": se["gdelt_max_queries"],
            "expansion_deadline": se["layer_deadline_seconds"],
            "platform_deadline": pa["layer_deadline_seconds"]}
        return c

    def gate(self, name: str, cfg: Dict[str, Any]) -> bool:
        """预算不足时关掉该层开关，返回是否允许执行。同时记录上一层实耗。"""
        now = time.monotonic()
        if self.marks:
            _prev = self.marks[-1]
            if "实耗秒" not in _prev:
                _prev["实耗秒"] = round(now - _prev.get("_at", now), 1)
        # 全局硬墙：到点后所有后续层一律短路（旧版这里永远 True）
        allowed = bool(self.enabled) and not self.expired and self.slice(name) > 1.0
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

    # ---------------------------------------------------------------- 每层真限时
    def bounded(self, name: str, fn, *args, fallback=None, grace=0.0, **kwargs):
        """整层调用真限时：daemon 线程 + join(timeout=层配额)。

        超时立即返回 fallback，主线程继续筛选/评级/出表；不再等后台跑完。
        后台线程通过 cfg["budget"]["_layer_deadline_at"] 自行 break，
        且 daemon 不阻止解释器退出（不会像 ThreadPoolExecutor 那样 join 拖死）。
        """
        limit = self.slice(name)
        if not self.enabled:
            limit = float(self.layers.get(name, 0))
        if self.expired or limit <= 1.0:
            self.skipped.append(name)
            self.marks.append({"层": name, "配额秒": self.layers.get(name, 0),
                               "实耗秒": 0.0, "允许": False, "短路": "全局硬墙/预算耗尽"})
            return fallback
        deadline_at = time.monotonic() + limit
        self.layer_deadlines[name] = deadline_at
        # 注入模块内部循环的检查点，让后台线程也能尽快停
        for a in args:
            if isinstance(a, dict):
                a.setdefault("budget", {})["_layer_deadline_at"] = deadline_at
                break
        if self.cfg_ref:
            self.cfg_ref.setdefault("budget", {})["_layer_deadline_at"] = deadline_at

        box: Dict[str, Any] = {}
        def _run():
            try:
                box["v"] = fn(*args, **kwargs)
            except BaseException as e:      # noqa: BLE001
                box["e"] = e
        th = threading.Thread(target=_run, daemon=True, name=f"layer-{name}")
        t_start = time.monotonic()
        th.start()
        # grace：给后台线程一小段宽限，让它走完内部的 BudgetExpired 路径、
        # 把已抓条目落下来再被截断（rss 层专用，避免「出表了但是空表」）。
        th.join(timeout=max(0.2, limit))
        if th.is_alive() and grace > 0:
            th.join(timeout=grace)
        spent = round(time.monotonic() - t_start, 1)
        # 写回 gate() 留下的同一条记录，避免逐层出现重复行
        rec = next((m for m in reversed(self.marks)
                    if m.get("层") == name and "实耗秒" not in m), None)
        if rec is None:
            rec = {"层": name, "配额秒": self.layers.get(name, 0), "允许": True,
                   "_at": t_start}
            self.marks.append(rec)
        rec["实耗秒"] = spent
        rec["短路"] = "" if not th.is_alive() else "层超时（主线程继续出表）"
        if th.is_alive():
            self.skipped.append(name)
            # 后台仍在跑，但模块内部会读 _layer_deadline_at 自行退出；
            # daemon 线程不阻止进程退出。主流程必须继续，绝不能 join。
            return fallback
        if "e" in box:
            rec["异常"] = f"{type(box['e']).__name__}: {str(box['e'])[:100]}"
            return fallback
        return box.get("v", fallback)

    def deadline_at(self) -> float:
        return float((self.applied.get("_deadline_at") or 0))

    def report(self) -> Dict[str, Any]:
        # 不再用「下一层 gate 的时间差」当实耗：bounded 已逐层写实耗秒。
        now = time.monotonic()
        rows = []
        for m in self.marks:
            r = {k: v for k, v in m.items() if not k.startswith("_")}
            if "实耗秒" not in r:
                # gate() 判「不允许」的层根本没执行，实耗必须是 0。
                # 旧版用 now-_at 补算，把整段后续时间算到它头上（冒烟实测 2.9s / 配额 1s）。
                r["实耗秒"] = (0.0 if not m.get("允许", True)
                              else round(now - m.get("_at", now), 1))
            rows.append(r)
        real = round(self.elapsed(), 1)
        return {
            "启用": self.enabled, "总预算秒": self.total, "出表保留秒": self.reserve,
            "实际耗时秒": real, "剩余秒": round(self.left(), 1),
            "全局硬墙": self.expired,
            "被短路的层": self.skipped, "参数裁剪": self.applied,
            "逐层": rows,
            # 2026-11-22：线上日志出现过「353.7s / 600.0s | 剩余 246.3s |
            # 预算耗尽」——层配额之和用尽被误报成全局预算耗尽，直接导致正文补采
            # 被压到 6 篇。两者必须分开写，否则下游按「快没时间」做错误降级。
            "结论": ("预算内完成" if not self.skipped and not self.expired else
                     (("全局硬墙到点" if self.expired else
                       f"层配额用尽（全局硬墙仍有余量 {round(self.left(), 1)}s）")
                      + "，已短路 " + "、".join(self.skipped)
                      + "；采集层停止但本批仍出表")),
        }

    def write_report(self, base_dir) -> None:
        """增量落盘：每层由 pipeline 调用，超时被硬杀时也留得下诊断。"""
        try:
            p = os.path.join(str(base_dir), "output", "budget_report.json")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(self.report(), f, ensure_ascii=False, indent=1)
        except Exception:
            pass


def check(deadline_at: float) -> None:
    """给 Collector._get 用：到点抛 BudgetExpired，由 run() 捕获后继续出表。"""
    if deadline_at and time.monotonic() > deadline_at:
        raise BudgetExpired("采集层全局预算耗尽，保留已抓条目继续出表")


def layer_expired(cfg: Dict[str, Any], key: str = "layer_deadline_seconds") -> bool:
    """给模块内部循环用：读 config 的整层 deadline 或 budget 注入的绝对时刻。"""
    dl = float((cfg.get("budget") or {}).get("_layer_deadline_at") or 0)
    if dl and time.monotonic() > dl:
        return True
    for section in ("source_expansion", "platform_access", "social_main", "free_sources"):
        v = float((cfg.get(section) or {}).get(key, 0) or 0)
        if v and v > 0 and time.monotonic() > dl:
            return True
    return False


def deadline_at_of(cfg: Dict[str, Any]) -> float:
    return float((cfg.get("budget") or {}).get("_layer_deadline_at") or 0)
