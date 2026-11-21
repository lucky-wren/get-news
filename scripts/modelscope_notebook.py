#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""魔搭 ModelScope Notebook（免费 CPU 实例）专用 · 通路 A 一键脚本。

零依赖：只用 Python 标准库，不需要 docker、不需要 npm、不需要 pip install。
魔搭 DSW 容器里通常没有 docker daemon，Node 也不一定带，所以这里直接实现一个
**DailyHotApi 兼容端点**（返回 {code:200, data:[{title,hot,url,index,time}]}），
项目里填 DAILYHOT_BASE_URL 即可直接对接，无需改任何代码。

三个模式，按顺序做：

  1) 先测网络（5 秒出结论，别先装东西）：
       python3 modelscope_notebook.py --probe
     → 输出每个平台真实 HTTP 状态，落 hot_probe.json
     → 若抖音/小红书仍是 403，说明魔搭这条出口也不通，直接看说明文档走 C 通路

  2) 抓一次快照（验证能取到数据）：
       python3 modelscope_notebook.py --once
     → 落 hot_snapshot_<日期>.json，可下载回灌到项目

  3) 起服务（给项目 / Actions 用）：
       python3 modelscope_notebook.py --serve --port 7860
     → GET /douyin、/weibo、/zhihu... 或 /hot?platform=douyin
     → 项目侧：export DAILYHOT_BASE_URL=http://127.0.0.1:7860

  契约自检（不联网，用内置演示数据验证字段归一）：
       python3 modelscope_notebook.py --self-test
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ssl._create_default_https_context = ssl._create_unverified_context
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ---------------------------------------------------------------- 平台 → 上游端点
# 抖音公共热榜端点本身不需要 a_bogus（需要签名的是账号 / 搜索 / 评论类接口）
UPSTREAM: dict[str, list[str]] = {
    "douyin": [
        "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/",
        "https://www.douyin.com/aweme/v1/web/hot/search/list/?device_platform=webapp&aid=6383",
        "https://www.douyin.com/aweme/v1/web/hot/search/billboard/",
    ],
    "weibo": ["https://weibo.com/ajax/statuses/hot_band",
              "https://weibo.com/ajax/side/hotSearch"],
    "zhihu": ["https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total?limit=50"],
    "baidu": ["https://top.baidu.com/board?tab=realtime"],
    "toutiao": ["https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"],
    "bilibili": ["https://api.bilibili.com/x/web-interface/popular?ps=50&pn=1"],
    "kuaishou": ["https://www.kuaishou.com/?isHome=1"],
    "tieba": ["https://tieba.baidu.com/hottopic/browse/topicList"],
    "thepaper": ["https://www.thepaper.cn/", "https://cache.thepaper.cn/content/consult/ajax/consult.json"],
    "36kr": ["https://36kr.com/"],
    # 小红书 / 微信搜一搜：DailyHotApi 无上游路由，且需 x-s / 登录态，
    # 这里只登记状态，不假装能抓（见 资讯获取_受限平台取数方案.md 通路 C/D）
    "xiaohongshu": ["https://www.xiaohongshu.com/explore"],
    "wechat": [],
}

ROUTE_ALIAS = {"微信·搜一搜·搜索发现": "wechat", "小红书·热点": "xiaohongshu",
               "B站·热门": "bilibili", "百度贴吧·热议": "tieba"}

DEMO = {
    "douyin": ["寒潮蓝色预警继续发布", "本轮成品油调价窗口开启"],
    "weibo": ["APEC欢迎宴会", "医保目录新增药品"],
    "zhihu": ["如何看待新能源车补贴退坡"],
}


def fetch(url: str, timeout: float = 7.0) -> tuple[int, str, str]:
    """返回 (状态码, 正文, 错误)。403/超时都如实返回，不吞异常。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json,text/html,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9", "Referer": "https://www.baidu.com/"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(300000).decode(
                r.headers.get_content_charset() or "utf-8", "replace"), ""
    except urllib.error.HTTPError as e:
        return e.code, "", f"HTTP {e.code}"
    except Exception as e:
        return 0, "", f"{type(e).__name__}: {str(e)[:80]}"


# ---------------------------------------------------------------- 上游解析 → 统一线索
def parse_upstream(route: str, text: str) -> list[dict]:
    """把各家返回归一成 {title, hot, url, index, time}。只取公开字段，不猜内容。"""
    out: list[dict] = []
    t = text.strip()
    if not t:
        return out
    # JSON 分支
    if t[0] in "{[":
        try:
            d = json.loads(t)
        except Exception:
            return out
        # 抖音 word / billboard
        arr = (d.get("word_list") or d.get("data") or d.get("word_list", [])
               if isinstance(d, dict) else d)
        if isinstance(d, dict) and isinstance(d.get("data"), dict):
            arr = d["data"].get("word_list") or d["data"].get("list") or []
        if isinstance(d, dict) and "band_list" in d.get("data", {}):
            arr = d["data"]["band_list"]
        # 微博 hot_band
        if isinstance(d, dict) and isinstance(d.get("data"), dict) and "band_list" not in d["data"]:
            arr = d["data"].get("realtime") or d["data"].get("hotgov") or arr
        # B站 popular
        if isinstance(d, dict) and isinstance(d.get("data"), dict) and "list" in d["data"]:
            arr = d["data"]["list"]
        # 知乎 hot-lists
        if isinstance(d, dict) and isinstance(d.get("data"), list):
            arr = d["data"]
        # 头条 hot-board
        if isinstance(d, dict) and isinstance(d.get("data"), list):
            arr = d["data"]
        if not isinstance(arr, list):
            arr = []
        for i, x in enumerate(arr, 1):
            if not isinstance(x, dict):
                continue
            title = (x.get("word") or x.get("title") or x.get("name")
                     or x.get("query") or x.get("show_name")
                     or (x.get("target") or {}).get("title")
                     or (x.get("note_card") or {}).get("display_title") or "")
            title = str(title).strip()
            if not title:
                continue
            hot = (x.get("hot_value") or x.get("hot") or x.get("heat_score")
                   or x.get("num") or x.get("hotValue") or x.get("score") or "")
            link = (x.get("url") or x.get("link") or x.get("short_url")
                    or (x.get("target") or {}).get("url") or "")
            tm = (x.get("onboard_time") or x.get("pubDate") or x.get("time")
                  or x.get("create_time") or "")
            out.append({"title": title, "hot": str(hot), "url": str(link),
                        "index": i, "time": str(tm)})
        return out
    # HTML / 内嵌 JSON 分支：只抽带标题的 JSON 片段，不做整页当拼盘
    for m in re.finditer(r'"(?:word|title|query|show_name)"\s*:\s*"([^"\n]{4,80})"', t):
        s = m.group(1).strip()
        if s and s not in {o["title"] for o in out}:
            out.append({"title": s, "hot": "", "url": "", "index": len(out) + 1, "time": ""})
    return out[:60]


def collect_route(route: str, timeout: float = 7.0) -> dict:
    urls = UPSTREAM.get(route, [])
    if not urls:
        return {"route": route, "ok": False, "clues": [], "tried": [],
                "reason": "无免鉴权上游路由（小红书 / 微信搜一搜需走通路 C 授权聚合或 D 自签名）"}
    tried, clues = [], []
    for u in urls:
        code, text, err = fetch(u, timeout)
        tried.append({"url": u[:110], "code": code, "bytes": len(text), "error": err})
        if code == 200 and text:
            c = parse_upstream(route, text)
            if c:
                clues = c
                break
    return {"route": route, "ok": bool(clues), "clues": clues, "tried": tried,
            "reason": "" if clues else ("上游 200 但未解析出词条" if any(
                x["code"] == 200 for x in tried) else "本网络不可达（403 / DNS / 超时）")}


# ---------------------------------------------------------------- 模式 1：网络实测
def run_probe(timeout: float, out: Path) -> dict:
    print("正在实测魔搭 Notebook 出口对各平台的连通性（约 10–30 秒）...\n")
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(lambda r: collect_route(r, timeout), UPSTREAM))
    print(f"{'平台':<14}{'结果':<10}{'词条':<8}说明")
    print("-" * 78)
    for r in res:
        print(f"{r['route']:<14}{('可取' if r['ok'] else '不可取'):<10}"
              f"{len(r['clues']):<8}{r['reason'] or '已解析出带标题词条'}")
        for t in r["tried"]:
            print(f"    └ {t['code']:<5} {t['bytes']:>7}B  {t['url']}")
    ok = [r["route"] for r in res if r["ok"]]
    blocked = [r["route"] for r in res if not r["ok"]]
    # 本脚本在 GitHub Actions 中跑的是 runner 出口，不是魔搭 Notebook 出口；
    # 截图里的「魔搭出口可用」是文案误导，改为如实标注当前执行环境。
    verdict = ("当前 runner 出口可用，通路 A 成立 → 由预探测一次性起服务，"
               "无需在采集步重复探测"
               if any(k in ok for k in ("douyin", "weibo", "zhihu", "toutiao"))
               else "当前 runner 出口同样被拦 → 别再折腾自建实例，按方案文档走通路 C（授权聚合 Key）")
    data = {"generated_at": datetime.now().isoformat(timespec="seconds"),
            "可取得": ok, "不可取得": blocked, "结论": verdict, "逐平台": res}
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "=" * 78)
    print("结论：" + verdict)
    print(f"完整报告 → {out}")
    return data


# ---------------------------------------------------------------- 模式 2：抓一次快照
def run_once(timeout: float, out: Path) -> dict:
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(lambda r: collect_route(r, timeout), UPSTREAM))
    snap = {"generated_at": datetime.now().isoformat(timespec="seconds"),
            "来源": "魔搭 Notebook CPU 实例 · 通路 A 自建聚合",
            "线索总数": sum(len(r["clues"]) for r in res),
            "平台": {r["route"]: r["clues"] for r in res},
            "口径": "线索仅为话题名+热度，必须逐条回查独立原文才成稿（01 §3.3）"}
    out.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"快照 → {out}（{snap['线索总数']} 条线索）")
    return snap


# ---------------------------------------------------------------- 模式 3：DailyHotApi 兼容服务
class Handler(BaseHTTPRequestHandler):
    timeout = 7.0
    demo = False

    def log_message(self, fmt, *args):
        sys.stderr.write("[hot] " + (fmt % args) + "\n")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        route = u.path.strip("/").split("/")[-1] or q.get("platform", [""])[0]
        route = ROUTE_ALIAS.get(route, route)
        if u.path.strip("/") in ("", "health"):
            body = {"code": 200, "service": "modelscope-hotapi",
                    "routes": sorted(UPSTREAM), "demo": self.demo,
                    "note": "DailyHotApi 兼容端点；线索须回查独立原文才成稿"}
        elif self.demo:
            words = [{"title": w, "hot": str(100000 - i * 137), "url": "",
                      "index": i + 1, "time": ""} for i, w in enumerate(DEMO.get(route, ["演示词条"]))]
            body = {"code": 200, "data": words, "demo": True, "route": route}
        else:
            r = collect_route(route, self.timeout)
            body = {"code": 200 if r["ok"] else 502, "data": r["clues"],
                    "route": route, "reason": r["reason"], "tried": r["tried"],
                    "fetched_at": datetime.now().isoformat(timespec="seconds")}
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)


def run_serve(port: int, timeout: float, demo: bool) -> None:
    Handler.timeout = timeout
    Handler.demo = demo
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"DailyHotApi 兼容端点已启动（demo={demo}）：", flush=True)
    print(f"  本机：      http://127.0.0.1:{port}/douyin", flush=True)
    print(f"  健康检查：  http://127.0.0.1:{port}/health", flush=True)
    print(f"  项目侧配置：export DAILYHOT_BASE_URL=http://127.0.0.1:{port}", flush=True)
    print("  Ctrl+C 停止；Notebook 实例停止后服务即消失（见方案文档的常驻说明）", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()


def self_test() -> dict:
    errs = []
    if len(UPSTREAM) != 12:
        errs.append("平台表应有 12 项（含小红书 / 微信占位）")
    if UPSTREAM["wechat"]:
        errs.append("微信搜一搜不应登记免鉴权端点")
    # JSON 归一：抖音 word_list / 微博 band_list / B站 list 三种结构
    samples = [
        ("douyin", '{"word_list":[{"word":"寒潮预警","hot_value":123456,"onboard_time":"2026-11-19 18:00"}]}'),
        ("weibo", '{"data":{"band_list":[{"word":"APEC欢迎宴会","hot":"998","onboard_time":"2026-11-19 18:32:21"}]}}'),
        ("bilibili", '{"data":{"list":[{"title":"新能源车渗透率","score":888}]}}'),
    ]
    for route, raw in samples:
        got = parse_upstream(route, raw)
        if len(got) != 1 or not got[0]["title"]:
            errs.append(f"{route} JSON 归一失败")
    if not parse_upstream("baidu", '<script>var d={"word":"油价下调"}</script>'):
        errs.append("HTML 内嵌 JSON 抽取失败")
    r = collect_route("wechat", 1.0)
    if r["ok"] or r["clues"]:
        errs.append("无上游路由时不应产出线索")
    return {"ok": not errs, "errors": errs,
            "checked": ["12 平台登记", "微信零端点", "抖音/微博/B站 JSON 归一",
                        "HTML 内嵌抽取", "无路由零产出"]}


def main() -> int:
    ap = argparse.ArgumentParser(description="魔搭 Notebook · 通路 A 自建热榜聚合")
    ap.add_argument("--probe", action="store_true", help="先测网络连通性（推荐第一步）")
    ap.add_argument("--once", action="store_true", help="抓一次全平台快照落 JSON")
    ap.add_argument("--serve", action="store_true", help="起 DailyHotApi 兼容 HTTP 服务")
    ap.add_argument("--self-test", action="store_true", help="离线契约自检")
    ap.add_argument("--port", type=int, default=7860)
    ap.add_argument("--timeout", type=float, default=7.0)
    ap.add_argument("--demo", action="store_true", help="服务用内置演示数据（仅验契约）")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    if a.self_test:
        print(json.dumps(self_test(), ensure_ascii=False, indent=2))
        return 0
    if a.serve:
        run_serve(a.port, a.timeout, a.demo)
        return 0
    if a.probe:
        run_probe(a.timeout, Path(a.out or "hot_probe.json"))
        return 0
    if a.once:
        day = datetime.now().strftime("%Y%m%d_%H%M")
        run_once(a.timeout, Path(a.out or f"hot_snapshot_{day}.json"))
        return 0
    ap.print_help()
    print("\n提示：先跑 --probe，5 秒看魔搭出口到底通不通，再决定后面做不做。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
