"""截图 44 的产能根因回归：503 端点串行 × 6s 超时把整批预算耗在前几十个。

验证 concurrency 改动真的解决了「有效数据太少」，而不是只改了文案：
  - 24 个慢端点在并发下明显快于串行理论耗时，且一个都不被预算跳过；
  - 实测可达（probe_status=200）的端点排在前面，预算耗尽也先采完能采的；
  - 端点一个都不删、单源失败仍隔离、并发下候选池顺序确定。
"""
from __future__ import annotations

import time

import httpx
import pytest

from newsbrief import registry

RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>unit</title>
<item><title>新闻{i}</title><link>https://unit.test/a{i}</link>
<pubDate>Tue, 24 Nov 2026 10:00:00 +0800</pubDate>
<description>第一句事实陈述完整。第二句事实补充完整。</description>
</item></channel></rss>"""


def _cfg(workers: int = 8, budget: float = 30.0) -> dict:
    return {"network": {
        "timeout_seconds": 6, "html_articles_per_endpoint": 2,
        "concurrent_endpoints": workers, "total_budget_seconds": budget,
        "catalog_expand": False,       # 局部测试隔离 503 清单端点，生产默认 True
    }}


def _channels(n: int, *, probe: bool = False, bad_index: int | None = None) -> dict:
    eps = []
    for i in range(n):
        e = {"name": f"rss{i}", "url": f"https://unit.test/f{i}.xml", "type": "rss"}
        if probe:
            # 故意把 403 放在数组前面：排序后 200 必须排到前面去
            e["probe_status"] = 403 if i < n // 2 else 200
        eps.append(e)
    return {"groups": [{"id": 90, "name": "并发测试", "note": "", "endpoints": eps}]}


def _client(n: int, delay: float, seen: list, bad_index: int | None = None,
            timing: list | None = None):
    def handler(req):
        t0 = time.monotonic()
        u = str(req.url)
        i = int(u.rsplit("/f", 1)[1].split(".", 1)[0])
        seen.append((i, t0))
        time.sleep(delay)
        if timing is not None:
            timing.append((t0, time.monotonic()))
        if bad_index is not None and i == bad_index:
            raise httpx.ConnectError("simulated endpoint down", request=req)
        return httpx.Response(200, text=RSS.format(i=i),
                              headers={"content-type": "application/rss+xml"})
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("workers", [8])
def test_concurrent_fetch_is_faster_and_keeps_every_endpoint(monkeypatch, tmp_path,
                                                            workers):
    monkeypatch.setattr(registry, "CACHE_PATH", tmp_path / "cache.json")
    n, delay = 24, 0.15
    seen: list = []
    timing: list = []
# 冷启动首次 import 采集器约 5s，预热后耗时测试不抖。
    registry.discover()
    with _client(n, delay, seen, timing=timing) as c:
        t0 = time.monotonic()
        raw, reports, failed = registry.collect(_cfg(workers), _channels(n), client=c)
        elapsed = time.monotonic() - t0
    serial = n * delay
    assert len(raw) == n, f"并发丢了端点：{len(raw)}/{n}"
    assert not failed
    assert len(seen) == n, "有端点没真发请求"
    assert "预算跳过" not in reports[0]["缺口"], reports[0]["缺口"][-200:]
    # 直接证据：请求区间必须有时间重叠。耗时阈值在共享 CPU 上会抖动，
    # 「N 个请求同时在进行」才是并发真正生效的判据。
    max_overlap = max(sum(1 for s, e in timing if s <= x < e)
                      for x, _ in timing)
    assert max_overlap >= 2, f"没有并发重叠（max={max_overlap}），线程池未生效"
    assert elapsed < serial * 0.85, f"并发未生效：{elapsed:.2f}s vs 串行 {serial:.2f}s"
    assert f"并发 {workers} 线程" in reports[0]["缺口"]


def test_reachable_endpoints_are_scheduled_first(monkeypatch, tmp_path):
    """workers=1 使顺序可观察：200 端点必须排在 403 前面，且一个都不删。"""
    monkeypatch.setattr(registry, "CACHE_PATH", tmp_path / "cache.json")
    seen: list = []
    with _client(6, 0.0, seen) as c:
        raw, reports, _ = registry.collect(_cfg(workers=1), _channels(6, probe=True),
                                           client=c)
    order = [i for i, _ in seen]
    assert len(order) == 6, "端点被删了"
    # probe=True 时后半段（i>=3）是 200；排序必须把它们整体提前
    assert order[:3] == [3, 4, 5], f"可达端点没有优先：{order}"
    assert sorted(order) == list(range(6))
    assert len(raw) == 6


def test_failed_endpoint_is_isolated_and_order_is_deterministic(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "CACHE_PATH", tmp_path / "cache.json")
    n, bad = 10, 4

    def _run():
        seen: list = []
        with _client(n, 0.02, seen, bad_index=bad) as c:
            return registry.collect(_cfg(8), _channels(n), client=c)

    raw1, _, failed1 = _run()
    raw2, _, failed2 = _run()
    assert len(raw1) == n - 1 and len(raw2) == n - 1, "单源失败扩散了"
    assert list(failed1) == ["rss4"] and list(failed2) == ["rss4"]
    assert [r["title"] for r in raw1] == [r["title"] for r in raw2], \
        "并发下候选池顺序不确定，展示序会漂移"
