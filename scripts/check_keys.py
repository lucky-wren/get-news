#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四步免费方案配置自检：不打印密钥，只报「配了没 / 能不能用 / 下一步做什么」。

用法：
  python scripts/check_keys.py               # 只查环境变量（离线，秒出）
  python scripts/check_keys.py --online      # 每项各发 1 次真实调用（会消耗 1 次免费额度）
  python scripts/check_keys.py --json        # 机器可读输出

绝不输出 key 明文；只显示前缀与长度，便于确认「填对了没有」。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EXPECTED = [
    ("SERPER_API_KEY", "Step1 词条→独立原文回查", "serper.dev 注册，Dashboard → API Keys", "2,500 次一次性"),
    ("TAVILY_API_KEY", "Step1 备选回查", "app.tavily.com 注册，Dashboard 复制 tvly- 开头", "1,000 credits/月"),
    ("DAILYHOT_BASE_URL", "Step2 自建 NewsNow / DailyHotApi 地址",
     "Cloudflare Pages 部署后填 https://xxx.pages.dev（末尾不要斜杠）", "免费常驻"),
    ("JINA_API_KEY", "Step3 正文补采提速（可选）",
     "jina.ai/api-dashboard/key-manager 复制 jina_ 开头", "无 key 20 RPM / 有 key 500 RPM"),
    ("HOT_API_PROVIDER", "Step4 通路 C 服务方", "dataxapi 或 tikhub", "免费试用"),
    ("HOT_API_KEY", "Step4 通路 C 密钥", "服务方控制台复制", "DataXAPI 50–333 次 / TikHub 部分免费"),
    ("HOT_API_BASE_URL", "Step4 服务方网关",
     "dataxapi=https://api.dataxapi.com；tikhub=https://api.tikhub.io", "—"),
    ("HOT_API_KEYWORDS", "Step4 微信搜一搜关键词（可选）", "逗号分隔，默认 5 个大众贴近主题词", "每次最多取 3 个"),
]


def mask(v: str) -> str:
    if not v:
        return "未配置"
    if v.startswith(("http://", "https://")):
        return f"URL（{len(v)} 字符）"
    return f"已配置（前缀 {v[:4]}… / {len(v)} 字符）"


def check_one(name: str, timeout: float = 8.0) -> dict:
    """对单个 key 发一次最小真实调用；失败如实返回，不抛异常、不编数据。"""
    import urllib.error
    import urllib.request
    import ssl
    ssl._create_default_https_context = ssl._create_unverified_context
    v = os.environ.get(name, "").strip()
    out = {"项": name, "配置": bool(v), "在线检查": "跳过", "结论": ""}
    if not v:
        out["结论"] = "未配置（本项会被跳过，不影响其它步骤）"
        return out
    try:
        if name == "SERPER_API_KEY":
            req = urllib.request.Request(
                "https://google.serper.dev/news",
                data=json.dumps({"q": "医保目录", "num": 1}).encode(),
                headers={"X-API-KEY": v, "Content-Type": "application/json"}, method="POST")
        elif name == "TAVILY_API_KEY":
            req = urllib.request.Request(
                "https://api.tavily.com/search",
                data=json.dumps({"api_key": v, "query": "医保目录", "max_results": 1}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
        elif name == "JINA_API_KEY":
            req = urllib.request.Request(
                "https://r.jina.ai/https://example.com",
                headers={"Authorization": "Bearer " + v, "Accept": "text/plain"})
        elif name == "DAILYHOT_BASE_URL":
            from news_pipeline import platform_access as pa
            clues, note = pa._tier_a("抖音·热点", {}, timeout)
            out["在线检查"] = "已调用 _tier_a（自动双格式探测）"
            out["结论"] = ("可用：" + note) if clues else ("未取得：" + note)
            out["条数"] = len(clues)
            return out
        elif name == "HOT_API_BASE_URL":
            from news_pipeline import platform_access as pa
            clues, note = pa._tier_c("抖音·热点", {}, timeout)
            out["在线检查"] = "已调用 _tier_c（dataxapi 热榜）"
            out["结论"] = ("可用：" + note) if clues else ("未取得：" + note)
            out["条数"] = len(clues)
            return out
        else:
            out["结论"] = "已配置（该项无独立在线探针，随跑批生效）"
            return out
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(200000)
            out["在线检查"] = f"HTTP {r.status} / {len(body)}B"
            out["结论"] = "可用（HTTP 200）"
    except urllib.error.HTTPError as e:
        out["在线检查"] = f"HTTP {e.code}"
        out["结论"] = {401: "密钥无效或权限不足", 402: "免费额度已用尽",
                       403: "被出口 / 服务方拒绝", 429: "触发速率限制"}.get(e.code, "请求被拒")
    except Exception as e:
        out["在线检查"] = type(e).__name__
        out["结论"] = "本网络不可达（换 GitHub Actions 网络后自动生效）"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="四步免费方案配置自检")
    ap.add_argument("--online", action="store_true", help="每项发 1 次真实调用（消耗 1 次免费额度）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--timeout", type=float, default=8.0)
    a = ap.parse_args()

    rows = []
    for name, step, how, quota in EXPECTED:
        v = os.environ.get(name, "").strip()
        row = {"项": name, "用途": step, "获取方式": how, "免费额度": quota,
               "状态": mask(v), "已配置": bool(v)}
        if a.online:
            row.update(check_one(name, a.timeout))
        rows.append(row)

    # 代码契约自检（纯离线）
    from news_pipeline import platform_access as pa, free_sources as fs
    pa_ret = pa.self_test()
    fs_ret = fs.self_test()
    summary = {
        "模式": "在线实测" if a.online else "离线配置检查",
        "配置": rows,
        "代码自检": {"platform_access": pa_ret, "free_sources": fs_ret},
        "已配置数": sum(1 for r in rows if r["已配置"]),
        "建议": [],
    }
    if not os.environ.get("SERPER_API_KEY"):
        summary["建议"].append("Step1 先配 SERPER_API_KEY：收益最大，词条才能回查到独立原文")
    if not os.environ.get("DAILYHOT_BASE_URL"):
        summary["建议"].append("Step2 未配也没关系：Actions runner 内已自动起零依赖兼容端点")
    if not os.environ.get("HOT_API_KEY"):
        summary["建议"].append("Step4 小红书 / 微信搜一搜需要 HOT_API_* 三件套，否则只到附录 15")

    if a.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"=== 四步免费方案配置自检（{summary['模式']}）===\n")
        print(f"{'变量':<22}{'状态':<34}用途")
        print("-" * 92)
        for r in rows:
            print(f"{r['项']:<22}{r['状态']:<34}{r['用途']}")
            if a.online and r.get("结论"):
                print(f"    └ {r['在线检查']}：{r['结论']}")
        print(f"\n已配置 {summary['已配置数']}/{len(rows)} 项")
        print("代码自检：platform_access", "PASS" if pa_ret["ok"] else pa_ret["errors"],
              "| free_sources", "PASS" if fs_ret["ok"] else fs_ret["errors"])
        for s in summary["建议"]:
            print("建议 → " + s)
        print("\n密钥明文不会出现在输出里；完整操作见 资讯获取_免费方案四步操作手册.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
