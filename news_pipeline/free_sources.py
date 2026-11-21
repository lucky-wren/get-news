"""免费 / 开源数据源适配层（通路 A 的多实例 fallback）。

回答「DailyHotApi 之外还有什么」：把三类东西做成**可实测、可切换、可审计**的池，
而不是继续只押一个地址：

  1. FREE_HOT_APIS  无需 key 的公共热榜 API / 聚合实例（vvhan / tenapi / tophub /
                     uapis / oioweb / pearktrue / 60s / newsnow 公共实例 / DailyHotApi 实例…）
  2. OPEN_PROJECTS  可自建的免费开源项目（含 GitHub API 实测 stars / 许可证 / 最后提交）
  3. FREE_QUOTAS    免费额度的搜索 / 正文 API（Serper / Tavily / Jina Reader / DataXAPI…）

所有公共实例都不保证 SLA、不保证合规授权，代码只做「实测可达 → 归一 → 逐条回查」，
不把公共实例的返回直接当新闻；拿不到就逐条写原因，不从别处编数据。
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ssl._create_default_https_context = ssl._create_unverified_context
BASE = Path(__file__).resolve().parents[1]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ---------------------------------------------------------------- 1. 免费公共热榜 API 池
# url 里 {route} 会替换成平台路由；probe 用 weibo 作探针（覆盖最广、最稳）。
FREE_HOT_APIS: List[Dict[str, Any]] = [
    {"id": "dailyhot_official", "name": "DailyHotApi 官方演示实例",
     "url": "https://api-hot.imsyy.top/{route}", "style": "dailyhot", "routes": "20+",
     "license": "MIT", "kind": "开源项目公共实例",
     "note": "作者个人实例，证书/可用性不稳；建议自建"},
    {"id": "dailyhot_vercel", "name": "DailyHotApi Vercel 实例",
     "url": "https://daily-hot.vercel.app/{route}", "style": "dailyhot", "routes": "20+",
     "license": "MIT", "kind": "开源项目公共实例", "note": "免费额度共享，可能被限流"},
    {"id": "newsnow_public", "name": "NewsNow 公共实例 /api/s",
     "url": "https://newsnow.busiyi.world/api/s?id={route}&latest", "style": "newsnow",
     "routes": "50+", "license": "MIT", "kind": "开源项目公共实例",
     "note": "返回 {status,items:[{title,url}]}；公共实例 30 分钟缓存"},
    {"id": "vvhan", "name": "韩小韩 / vvhan 热榜 API（开源）",
     "url": "https://api.vvhan.com/api/hotlist/{vv}", "style": "vvhan",
     "routes": "10+", "license": "开源", "kind": "免费公共 API",
     "note": "需把 route 映射成 wbHot/douyinHot/zhihuHot/baiduRD 等"},
    {"id": "tenapi", "name": "TenAPI 热榜",
     "url": "https://tenapi.cn/v2/{ten}hot", "style": "tenapi", "routes": "8+",
     "license": "开源", "kind": "免费公共 API", "note": "路径按平台命名，响应 {data:[...]}"},
    {"id": "tophub_fun", "name": "tophub.fun 聚合",
     "url": "https://api.tophub.fun/api/hot/{route}", "style": "generic",
     "routes": "10+", "license": "—", "kind": "免费公共 API", "note": "第三方实例，稳定性未知"},
    {"id": "uapis", "name": "UAPI 热榜",
     "url": "https://uapis.cn/api/v1/misc/hotboard?type={route}", "style": "generic",
     "routes": "10+", "license": "—", "kind": "免费公共 API", "note": "无需 key"},
    {"id": "oioweb", "name": "OIOWeb 热榜",
     "url": "https://api.oioweb.cn/api/common/HotList?type={route}", "style": "generic",
     "routes": "10+", "license": "—", "kind": "免费公共 API", "note": "无需 key"},
    {"id": "pearktrue", "name": "PearkTrue DailyHot",
     "url": "https://api.pearktrue.cn/api/dailyhot/?title={cn}", "style": "generic",
     "routes": "20+", "license": "—", "kind": "免费公共 API", "note": "title 传中文平台名"},
    {"id": "60s_viki", "name": "vikiboss/60s 公共实例（开源）",
     "url": "https://60s.viki.moe/v2/{route}", "style": "sixty",
     "routes": "热榜/60s/汇率", "license": "开源", "kind": "开源项目公共实例",
     "note": "静态 JSON + CDN，每日更新；可自建 Cloudflare Workers"},
    {"id": "60s_api_viki", "name": "60s 备用实例",
     "url": "https://60s-api.viki.moe/v2/{route}", "style": "sixty",
     "routes": "同上", "license": "开源", "kind": "开源项目公共实例", "note": "备用域名"},
    {"id": "rsshub_official", "name": "RSSHub 官方实例",
     "url": "https://rsshub.app/{rss}", "style": "rss", "routes": "万物皆 RSS",
     "license": "AGPL-3.0", "kind": "开源项目公共实例",
     "note": "路由 /weibo/search/hot、/douyin/hot、/zhihu/hot、/toutiao/hot"},
    {"id": "gmit", "name": "GMIT 抖音热榜",
     "url": "https://api.gmit.vip/Api/DouyinHot", "style": "generic", "routes": "抖音",
     "license": "—", "kind": "免费公共 API", "note": "仅抖音，无需 key"},
    {"id": "hot_imsyy", "name": "DailyHot 前端站 API",
     "url": "https://hot.imsyy.top/api/{route}", "style": "dailyhot", "routes": "20+",
     "license": "MIT", "kind": "开源项目公共实例", "note": "与官方实例互为备份"},
]

# ---------------------------------------------------------------- 2. 免费开源项目清单（GitHub API 实测快照）
# 数据获取时间 2026-11-19；HTTPError 表示当次 API 限流未取得，不代表项目不存在。
OPEN_PROJECTS: List[Dict[str, Any]] = [
    {"repo": "imsyy/DailyHotApi", "stars": 4100, "license": "MIT", "pushed": "2026-03-11",
     "lang": "TypeScript", "定位": "热榜 JSON/RSS API，20+ 路由", "部署": "Docker / Vercel / Node",
     "建议": "通路 A 主选；路由随上游改版失效，须逐路由实测", "meta": "搜索资料"},
    {"repo": "ourongxing/newsnow", "stars": 21900, "license": "MIT", "pushed": "2026-11",
     "lang": "TypeScript", "定位": "50+ 实时热榜阅读器，自带 /api/s JSON",
     "部署": "Cloudflare Pages（免费 10 万次/天）/ Vercel / Docker",
     "建议": "比 DailyHotApi 更适合当常驻源；建议自建避免公共实例限流", "meta": "GitHub API 实测"},
    {"repo": "DIYgod/RSSHub", "stars": 47105, "license": "AGPL-3.0", "pushed": "2026-11-19",
     "lang": "TypeScript", "定位": "万物皆 RSS，路由最多", "部署": "Docker / Vercel / Zeabur",
     "建议": "降级神器；注意 AGPL-3.0，嵌入自家产品前评估许可", "meta": "GitHub API 实测"},
    {"repo": "sansan0/TrendRadar", "stars": 63732, "license": "GPL-3.0", "pushed": "2026-11-15",
     "lang": "Python", "定位": "35 平台热榜 + 关键词筛选 + 推送 + MCP + SQLite 历史",
     "部署": "GitHub Actions / Docker",
     "建议": "只可参考架构，GPL-3.0 不可嵌入本项目代码；数据源仍是 newsnow /api/s", "meta": "GitHub API 实测"},
    {"repo": "vikiboss/60s", "stars": None, "license": "开源", "pushed": "—",
     "lang": "TypeScript", "定位": "60 秒早报 + 微博/抖音/知乎/B站/小红书热搜静态 JSON",
     "部署": "Cloudflare Workers / Deno / Bun / Node / Docker",
     "建议": "每天 60 秒读报这类拼盘源可直接用它；GitHub Actions 定时生成静态 JSON", "meta": "搜索资料"},
    {"repo": "NanmiCoder/MediaCrawler", "stars": 71359, "license": "NOASSERTION", "pushed": "2026-11-17",
     "lang": "Python", "定位": "小红书/抖音/快手/B站/微博 关键词采集（playwright + stealth）",
     "部署": "本地 / 服务器，需登录态",
     "建议": "属通路 D，不是热榜 API；需 Cookie/代理/无头浏览器，别放进日报主链路", "meta": "GitHub API 实测"},
    {"repo": "ikenxuan/amagi", "stars": None, "license": "—", "pushed": "—",
     "lang": "Node.js", "定位": "多平台 Node SDK，封装抖音/B站/快手/小红书核心接口",
     "部署": "npm 本地", "建议": "通路 D 的可选签名适配层，需自持环境", "meta": "搜索资料"},
    {"repo": "Johnserf-Seed/f2", "stars": 2742, "license": "Apache-2.0", "pushed": "2026-10-19",
     "lang": "Python", "定位": "抖音 a_bogus / X-Bogus 签名与会话令牌管理",
     "部署": "本地", "建议": "通路 D 的参考实现；本项目不内置 JS VM 逆向", "meta": "GitHub API 实测"},
    {"repo": "ReaJason/xhs", "stars": 2239, "license": "MIT", "pushed": "2025-07-01",
     "lang": "Python", "定位": "小红书 x-s/x-t 签名封装（需 playwright）",
     "部署": "本地", "建议": "通路 D；最后提交已一年多，改版风险高", "meta": "GitHub API 实测"},
    {"repo": "imsyy/DailyHot", "stars": 913, "license": "MIT", "pushed": "2024-11-05",
     "lang": "Vue", "定位": "DailyHotApi 配套前端", "部署": "静态站",
     "建议": "只要 API，前端非必需", "meta": "GitHub API 实测"},
    {"repo": "searxng/searxng", "stars": None, "license": "AGPL-3.0", "pushed": "—",
     "lang": "Python", "定位": "免费元搜索聚合，可自建无 key 全网检索",
     "部署": "Docker", "建议": "可做 Bing 站内回查的自建替代，解决词条→独立原文", "meta": "搜索资料"},
]

# ---------------------------------------------------------------- 3. 免费额度（搜索 / 正文 / 聚合）
FREE_QUOTAS: List[Dict[str, str]] = [
    {"用途": "词条→独立原文回查", "服务": "Serper", "免费额度": "2,500 次一次性（无需信用卡）",
     "环境变量": "SERPER_API_KEY", "说明": "本项目已对接 lookup_article；一次跑批约几十次，够用很久"},
    {"用途": "词条→独立原文回查", "服务": "Tavily", "免费额度": "1,000 credits/月",
     "环境变量": "TAVILY_API_KEY", "说明": "已对接；返回自带清洗后正文"},
    {"用途": "独立原文正文补采", "服务": "Jina Reader r.jina.ai",
     "免费额度": "无 key 20 RPM；注册 10M tokens", "环境变量": "JINA_API_KEY（可选）",
     "说明": "本轮已接入 fetch_article_text 降级链，trafilatura 抽不到时自动试"},
    {"用途": "正文/网页转 Markdown", "服务": "Jina Reader", "免费额度": "10M tokens 一次性",
     "环境变量": "JINA_API_KEY", "说明": "ReaderLM-v2 自托管为 CC-BY-NC 4.0，商用需许可"},
    {"用途": "抖音/小红书/微信热榜", "服务": "DataXAPI", "免费额度": "新用户 50 次；最多 333 次试用",
     "环境变量": "HOT_API_KEY", "说明": "通路 C；失败不收费"},
    {"用途": "多平台热榜/搜一搜", "服务": "TikHub", "免费额度": "部分端点 allow_free_credit",
     "环境变量": "HOT_API_KEY", "说明": "榜单系列 31 个接口均可用免费额度"},
    {"用途": "热榜免费看（网页）", "服务": "Okdata", "免费额度": "11 个平台实时热榜免费看",
     "环境变量": "—", "说明": "API 调用按次计费；免费页只适合人工抽查"},
    {"用途": "SERP 搜索", "服务": "SerpApi / Bright Data / Apify / Exa",
     "免费额度": "250 次/月 · 5,000 次/月 · $5/月 · $10/月",
     "环境变量": "（自定义）", "说明": "可按项目替换 Bing 站内回查；注意是否需信用卡"},
    {"用途": "免费部署", "服务": "Cloudflare Pages / Vercel / GitHub Actions",
     "免费额度": "10 万 Functions 调用/天 / 免费额度 / 2,000 分钟/月",
     "环境变量": "—", "说明": "NewsNow / DailyHotApi / 60s 都能白嫖常驻，比 Notebook 稳"},
]

# ---------------------------------------------------------------- 路由映射
VV = {"weibo": "wbHot", "douyin": "douyinHot", "zhihu": "zhihuHot", "baidu": "baiduRD",
      "toutiao": "toutiaoHot", "bilibili": "biliHot", "kuaishou": "ksHot",
      "tieba": "tiebaHot", "thepaper": "thepaperHot", "36kr": "36krHot"}
TEN = {"weibo": "weibo", "douyin": "douyin", "zhihu": "zhihu", "baidu": "baidu",
       "toutiao": "toutiao", "bilibili": "bilibili"}
RSS = {"weibo": "weibo/search/hot", "douyin": "douyin/hot", "zhihu": "zhihu/hot",
       "toutiao": "toutiao/hot", "baidu": "baidu/hot", "bilibili": "bilibili/ranking/0/3/1",
       "36kr": "36kr/newsflash", "thepaper": "thepaper/featured"}
CN = {"weibo": "微博", "douyin": "抖音", "zhihu": "知乎", "baidu": "百度",
      "toutiao": "今日头条", "bilibili": "哔哩哔哩", "kuaishou": "快手",
      "tieba": "贴吧", "thepaper": "澎湃新闻", "36kr": "36氪",
      "xiaohongshu": "小红书", "wechat": "微信"}


def build_url(p: Dict[str, Any], route: str) -> str:
    return (p["url"].replace("{route}", route)
            .replace("{vv}", VV.get(route, "wbHot"))
            .replace("{ten}", TEN.get(route, "weibo"))
            .replace("{rss}", RSS.get(route, "weibo/search/hot"))
            .replace("{cn}", urllib.parse.quote(CN.get(route, "微博"))))


def _http(url: str, timeout: float = 5.0) -> Tuple[int, str, str]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept": "application/json,application/xml,text/xml,text/html,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9", "Referer": "https://www.baidu.com/"}),
                timeout=timeout) as r:
            return r.status, r.read(300000).decode(
                r.headers.get_content_charset() or "utf-8", "replace"), ""
    except urllib.error.HTTPError as e:
        return e.code, "", f"HTTP {e.code}"
    except Exception as e:
        return 0, "", f"{type(e).__name__}"


def _rows_from_json(d: Any) -> List[dict]:
    """从各家 JSON 里挖出数组，不做语义判断。"""
    if isinstance(d, list):
        return [x for x in d if isinstance(x, dict)]
    if not isinstance(d, dict):
        return []
    for key in ("data", "items", "result", "list", "news", "word_list", "hotList", "rows"):
        v = d.get(key)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
        if isinstance(v, dict):
            for k2 in ("list", "items", "word_list", "band_list", "realtime", "data"):
                v2 = v.get(k2)
                if isinstance(v2, list):
                    return [x for x in v2 if isinstance(x, dict)]
    return []


def normalize_clues(style: str, text: str) -> List[Dict[str, str]]:
    """统一成 {title, hot, url, time}。只归一字段，不把热度当正文。"""
    out: List[Dict[str, str]] = []
    if not text.strip():
        return out
    if style == "rss" or text.lstrip().startswith("<"):
        try:
            root = ET.fromstring(text)
            for i, node in enumerate(root.findall(".//item"), 1):
                t = (node.findtext("title") or "").strip()
                if t:
                    out.append({"title": t, "hot": "", "url": (node.findtext("link") or "").strip(),
                                "time": (node.findtext("pubDate") or "").strip(), "index": str(i)})
        except Exception:
            pass
        return out
    try:
        rows = _rows_from_json(json.loads(text))
    except Exception:
        # 有些公共 API 返回 JSONP / HTML 内嵌，保守抽 title/word 字段
        rows = []
        for m in re.finditer(r'"(?:title|word|name)"\s*:\s*"([^"\n]{4,90})"', text):
            rows.append({"title": m.group(1)})
    for i, x in enumerate(rows, 1):
        title = str(x.get("word") or x.get("title") or x.get("name") or x.get("query")
                    or x.get("show_name") or x.get("hotword") or "").strip()
        if not title or title in {o["title"] for o in out}:
            continue
        out.append({"title": title,
                    "hot": str(x.get("hot") or x.get("hot_value") or x.get("hotValue")
                               or x.get("heat") or x.get("score") or x.get("index") or ""),
                    "url": str(x.get("url") or x.get("link") or x.get("mobil_url")
                               or x.get("mobileUrl") or ""),
                    "time": str(x.get("time") or x.get("pubDate") or x.get("onboard_time")
                                or x.get("create_time") or ""),
                    "index": str(i)})
    return out[:80]


# ---------------------------------------------------------------- 4. 实测 + 取数
def probe_all(timeout: float = 5.0, use_cache: bool = True,
              cache_hours: float = 6.0) -> Dict[str, Any]:
    """逐个真实请求免费公共实例（用 weibo 作探针），结果落 data/free_sources_probe.json。"""
    if os.environ.get("FREE_SOURCES_OFFLINE") == "1":
        return {"generated_at": datetime.now().isoformat(timespec="seconds"),
                "探针": "offline", "超时": timeout, "可达": [], "实例": [],
                "开源项目": OPEN_PROJECTS, "免费额度": FREE_QUOTAS,
                "结论": "离线自检：跳过免费实例实测"}
    cp = BASE / "data" / "free_sources_probe.json"
    if use_cache and cp.exists():
        try:
            old = json.loads(cp.read_text(encoding="utf-8"))
            age = datetime.now() - datetime.fromisoformat(old.get("generated_at", "2000-01-01"))
            if age < timedelta(hours=cache_hours):
                return old
        except Exception:
            pass

    def one(p: Dict[str, Any]) -> Dict[str, Any]:
        url = build_url(p, "weibo")
        code, text, err = _http(url, timeout)
        clues = normalize_clues(p["style"], text) if code == 200 else []
        return {"id": p["id"], "name": p["name"], "url": url, "code": code,
                "bytes": len(text), "error": err, "clues": len(clues),
                "ok": code == 200 and bool(clues), "kind": p["kind"],
                "license": p["license"], "note": p["note"]}

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        rows = list(ex.map(one, FREE_HOT_APIS))
    data = {"generated_at": datetime.now().isoformat(timespec="seconds"),
            "探针": "weibo", "超时": timeout,
            "可达": [r["id"] for r in rows if r["ok"]],
            "实例": rows,
            "开源项目": OPEN_PROJECTS,
            "免费额度": FREE_QUOTAS,
            "结论": ("本网络下 %d/%d 个免费公共实例可达，可自动取数"
                     % (sum(1 for r in rows if r["ok"]), len(rows))
                     if any(r["ok"] for r in rows)
                     else "本网络下全部免费公共实例被拦/超时（代码不编数据）；"
                          "换 unrestricted 网络或自建 Cloudflare Pages 即自动启用")}
    cp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def collect_free(platform_route: str, timeout: float = 5.0) -> Tuple[List[Dict[str, str]], str]:
    """按 probe 的可达实例并发取词；探针全拦时短路，不重复撞墙。"""
    rep = probe_all(timeout)
    ids = rep.get("可达", [])
    if not ids and "全部" in str(rep.get("结论", "")):
        # weibo 探针已证实整批域名级被拦，换路由也不会有奇迹；避免 10 平台 × 14 实例超时
        return [], ("免费公共实例探针（weibo）全部不可达，本批跳过逐路由请求；"
                    "换 unrestricted 网络或自建实例后由 --probe 自动重测启用")
    candidates = [p for p in FREE_HOT_APIS if p["id"] in ids]
    if not candidates:
        return [], "探针无可达实例（见 data/free_sources_probe.json）"

    def one(p: Dict[str, Any]) -> Tuple[str, int, str, List[Dict[str, str]]]:
        url = build_url(p, platform_route)
        code, text, err = _http(url, timeout)
        return (p["name"], code, err, normalize_clues(p["style"], text) if code == 200 else [])

    tried: List[str] = []
    with cf.ThreadPoolExecutor(max_workers=min(8, len(candidates))) as ex:
        for name, code, err, clues in ex.map(one, candidates):
            if clues:
                return clues, f"{name} → {len(clues)} 条"
            tried.append(f"{name}:{code or err}")
    return [], "免费实例并发实测：" + "；".join(tried[:8])


def jina_reader_text(url: str, timeout: float = 4.0, max_chars: int = 6000) -> str:
    """免费正文补采回退：r.jina.ai 把任意 URL 转干净 Markdown（无 key 20 RPM）。

    只在 trafilatura 与内置正则都抽不到时调用；返回空串表示未取得，绝不编造。
    """
    if not url or not url.startswith(("http://", "https://")):
        return ""
    if os.environ.get("FREE_SOURCES_JINA", "1") == "0":
        return ""
    key = os.environ.get("JINA_API_KEY", "")
    h = {"User-Agent": UA, "Accept": "text/plain,text/markdown,*/*",
         "X-Return-Format": "markdown"}
    if key:
        h["Authorization"] = "Bearer " + key
    try:
        req = urllib.request.Request("https://r.jina.ai/" + url, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read(400000).decode("utf-8", "replace")
        # 去掉 Jina 的元数据头与图片行，只保留正文段落
        lines = [ln.strip() for ln in text.splitlines()
                 if len(ln.strip()) >= 20
                 and not re.match(r"^(Title:|URL Source:|Published Time:|Markdown Content:|!\[|\[!\[|Image \d+:)", ln.strip())]
        return "\n".join(lines)[:max_chars]
    except Exception:
        return ""


def self_test() -> Dict[str, Any]:
    errs: List[str] = []
    if len(FREE_HOT_APIS) < 10:
        errs.append("免费公共实例池应 ≥10")
    if len(OPEN_PROJECTS) < 8:
        errs.append("开源项目清单应 ≥8")
    if len(FREE_QUOTAS) < 6:
        errs.append("免费额度清单应 ≥6")
    samples = [
        ("dailyhot", '{"code":200,"data":[{"title":"寒潮预警","hot":123,"url":"https://x/a"}]}'),
        ("newsnow", '{"status":"success","items":[{"title":"油价下调","url":"https://x/b"}]}'),
        ("vvhan", '{"success":true,"data":[{"title":"医保目录","hot":"999","url":"https://x/c"}]}'),
        ("tenapi", '{"code":200,"data":[{"name":"APEC","hotValue":"88"}]}'),
        ("sixty", '{"code":200,"data":[{"word":"地方债","hot_value":"77"}]}'),
        ("rss", '<rss><channel><item><title>台风预警</title><link>https://x/d</link></item></channel></rss>'),
    ]
    for style, raw in samples:
        got = normalize_clues(style, raw)
        if len(got) != 1 or not got[0]["title"]:
            errs.append(f"{style} 归一失败")
    if normalize_clues("generic", "not json at all"):
        errs.append("非 JSON 不应产出无标题词条")
    if build_url(FREE_HOT_APIS[3], "douyin").find("douyinHot") < 0:
        errs.append("vvhan 路由映射错误")
    if jina_reader_text("", 1.0):
        errs.append("空 URL 不应返回正文")
    return {"ok": not errs, "errors": errs,
            "checked": ["实例池", "开源清单", "免费额度", "6 种返回结构归一",
                        "非JSON零产出", "路由映射", "Jina空URL零产出"]}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="免费 / 开源数据源池：实测与取数")
    ap.add_argument("--probe", action="store_true", help="实测全部免费公共实例")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--collect", default="", help="按平台路由取词，如 douyin / weibo")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--timeout", type=float, default=5.0)
    a = ap.parse_args()
    if a.self_test:
        print(json.dumps(self_test(), ensure_ascii=False, indent=2))
        return 0
    if a.collect:
        clues, note = collect_free(a.collect, a.timeout)
        print(json.dumps({"route": a.collect, "clues": len(clues), "note": note,
                          "sample": clues[:3]}, ensure_ascii=False, indent=2))
        return 0
    rep = probe_all(a.timeout, use_cache=not a.no_cache)
    print(f"{'实例':<32}{'状态':<10}{'词条':<8}说明")
    print("-" * 86)
    for r in rep["实例"]:
        print(f"{r['name'][:30]:<32}{str(r['code']):<10}{r['clues']:<8}{r['note'][:34]}")
    print("\n可达：", "、".join(rep["可达"]) or "无")
    print("结论：", rep["结论"])
    print(f"报告 → {BASE / 'data' / 'free_sources_probe.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
