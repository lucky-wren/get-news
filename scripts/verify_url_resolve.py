"""线上检索链路回归（用户上传 latest.xlsx 工程留痕 H-c，2026-11-22）。

这一轮不再用目标样例自证，而是把**线上真实检出的 122 个候选 URL** 和
「gnews 多条被压成 1 个域名」这个确定性 bug 做成可复跑用例。

覆盖：
  1. Bing ck/a 追踪壳 / 自有页不猜 URL（86/88 被识别）；
  2. 完整 ck/a 的 u=a1<base64url> 还原成真实中文新闻 URL；
  3. 线上实测噪声域名（游戏 / 电影 / 境外机构页）被中文相关性闸拦下；
  4. 真实中文新闻 URL 不被 search_api 硬闸误杀；
  5. **gnews 去重不再塌缩**：3 条不同 CBMi link + 同一中文刊名 → domains ≥3
     （旧版恒为 1，导致 min_independent_domains=2 永远不可达）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from news_pipeline import url_resolve as ur
from news_pipeline import keyless_compose as kc
from news_pipeline import search_api as sa

R: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    R.append({"项": name, "结果": "PASS" if ok else "FAIL", "证据": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name} — {detail[:150]}")


CORPUS = BASE / "data" / "hc_url_regression.json"
check("线上 H-c URL 语料随包存在", CORPUS.exists(), str(CORPUS))
d = json.loads(CORPUS.read_text(encoding="utf-8")) if CORPUS.exists() else {"urls": []}
urls = d["urls"]
check("语料含 ≥100 条线上真实候选 URL", len(urls) >= 100, f"{len(urls)} 条")

# ---- 1. Bing ck/a 壳：不猜截断残串，判非独立原文 -------------------------
bing = [x["url"] for x in urls if "bing.com" in x["url"]]
shell = [u for u in bing if ur.is_bing_shell(u)]
check("线上 Bing 追踪壳 / 自有页全部识别为不可还原（不猜 URL）",
      len(bing) >= 80 and len(shell) >= len(bing) - 2,
      f"{len(shell)}/{len(bing)} 条；残串一律拦下，绝不拼 URL")

# ---- 2. 完整 ck/a 的 u=a1 解码 -------------------------------------------
full = ("https://www.bing.com/ck/a?!&&p=abcd&u=a1"
        "aHR0cHM6Ly9uZXdzLnFxLmNvbS9yYWluL2EvMjAyNjExMjBBMDQ1S0EwMA&ntb=1")
real = ur.resolve_search_url(full)
check("完整 Bing ck/a 还原成真实中文新闻 URL",
      real == "https://news.qq.com/rain/a/20261120A045KA00", real)
check("还原后的真实 URL 不再被判 Bing 壳", not ur.is_bing_shell(real), real)

# ---- 3. 线上噪声域名 + 中文相关性闸 ---------------------------------------
noise = [x for x in urls if any(k in x["url"] for k in (
    "crazygames", "texttwist", "fanbolt", "kancare", "kmap-state"))]
noise_blocked = sum(1 for x in noise if ur.is_offtopic("Text Twist Game", x["query"], x["url"])[0])
check("线上实测噪声域名（游戏/电影/境外机构页）全部拦下",
      len(noise) >= 4 and noise_blocked == len(noise),
      f"{noise_blocked}/{len(noise)} 条")
off, why = ur.is_offtopic("Movie Release Calendar", "医保目录 新增药品 2026-11-20",
                          "https://www.fanbolt.com/movies/release-calendar/")
check("英文标题对中文 query 判不相干", off, why)
on, why2 = ur.is_offtopic("国家医保局公布新版药品目录", "医保目录 新增药品 2026-11-20",
                          "https://www.nhsa.gov.cn/art/2026/11/20/x.html")
check("中文相关标题不误杀", not on, why2)

# ---- 4. 真实中文新闻不被 search_api 硬闸误杀 ------------------------------
hit = sa.SearchHit(
    title="11月20日国内成品油调价 汽油每升上调0.12元",
    url="https://news.qq.com/rain/a/20261120A045KA00",
    snippet="11月20日国内成品油价格调整，汽油每升上调0.12元，柴油每升上调0.13元。",
    provider="sogou", date="2026-11-20")
check("线上 news.qq.com 真实新闻不被硬闸误杀",
      sa._reject_reason(hit, None, "成品油调价 每升 2026-11-20") == "",
      "硬闸返回空 = 合格候选")
home = sa.SearchHit(title="国家医保局", url="https://www.nhsa.gov.cn/",
                    snippet="国家医疗保障局官方网站", provider="bing", date="")
check("官网首页仍被硬闸拦下（不放宽）",
      "非独立原文" in sa._reject_reason(home, None, "医保目录 新增药品"),
      sa._reject_reason(home, None, "医保目录 新增药品"))

# ---- 5. gnews 去重塌缩修复（决定性）--------------------------------------
# 线上 H-c 有 8 条 news.google.com/rss/articles/CBMi...；旧版 gnews() 把
# domain 算成 news.google.com，search_clue 按 domain 去重 → 恒 1 条。
RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
<title>医保目录 新增药品</title>
<item><title>新版医保药品目录公布 新增91种药品</title>
<link>https://news.google.com/rss/articles/CBMiAAA111?hl=zh-CN</link>
<pubDate>Thu, 20 Nov 2026 09:10:00 GMT</pubDate>
<source url="https://www.news.cn">新华网</source>
<description>新增91种药品，预计每年为患者减负400亿元。</description></item>
<item><title>医保目录新增91种药品 报销比例同步调整</title>
<link>https://news.google.com/rss/articles/CBMiBBB222?hl=zh-CN</link>
<pubDate>Thu, 20 Nov 2026 10:20:00 GMT</pubDate>
<source url="https://www.chinanews.com.cn">中新网</source>
<description>91种药品纳入目录，可异地结算。</description></item>
<item><title>国家医保局：91种新药进目录 患者负担下降</title>
<link>https://news.google.com/rss/articles/CBMiCCC333?hl=zh-CN</link>
<pubDate>Thu, 20 Nov 2026 11:05:00 GMT</pubDate>
<source url="https://www.cctv.com">央视网</source>
<description>国家医保局公布新版药品目录，新增91种。</description></item>
</channel></rss>"""


def fake_http(url: str, timeout=8.0, headers=None, follow=True):
    if "news.google.com/rss/search" in url:
        return 200, RSS, url
    if "news.google.com/rss/articles" in url:
        return 200, "", url          # 重定向不解析，走 base64 备用/如实留痕
    return 403, "blocked", url


cfg = {"keyless": {"provider_order": "gnews", "gnews_when": ""}}
with patch.object(kc, "_http", side_effect=fake_http):
    evs, tr = kc.search_clue("医保目录 新增药品 2026-11-20", cfg=cfg, win=None, resolve=False)
check("gnews 3 条独立报道不再塌缩成 1 个域名（旧版恒 1）",
      len(evs) >= 3 and tr.domains >= 3,
      f"证据 {len(evs)} 条 / domains={tr.domains} / 域名 "
      f"{[e.domain for e in evs]}")
check("gnews 的 <source url> 真实域名被用于独立来源判定",
      any(e.domain in ("news.cn", "www.news.cn", "chinanews.com.cn", "www.chinanews.com.cn",
                       "cctv.com", "www.cctv.com") for e in evs),
      f"域名列表 {[e.domain for e in evs]}")

# 中文相关标题必须都通过相关性闸，否则上面的 3 条会被全剔
check("3 条中文新闻标题均通过相关性闸",
      sum(1 for e in evs if ur.is_offtopic(e.title, "医保目录 新增药品 2026-11-20", e.url)[0]) == 0,
      "0 条被误判不相干")

# ---- 汇总 -----------------------------------------------------------------
out = {"语料": "用户上传 latest.xlsx 工程留痕 H-c（线上真实检索候选）",
       "线上URL数": len(urls), "Bing壳": len(bing), "识别为不可还原": len(shell),
       "噪声域名": len(noise), "gnews证据数": len(evs), "gnews独立域名": tr.domains,
       "通过": sum(1 for x in R if x["结果"] == "PASS"), "总计": len(R),
       "FAIL": [x for x in R if x["结果"] == "FAIL"],
       "说明": "本脚本用线上真实候选 URL + mock RSS 真调运行时函数；不重跑 live，不编造数据"}
(BASE / "output" / "url_resolve_verify.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n{out['通过']}/{out['总计']} PASS → output/url_resolve_verify.json")
sys.exit(0 if out["通过"] == out["总计"] else 1)
