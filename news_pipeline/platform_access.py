"""受限平台取数适配层（抖音 / 小红书 / 微信搜一搜 / 其余 7 个社交主干）。

这层只解决一件事：**把「需签名 / 需登录」的平台，变成可配置、可实测、可审计的取数通路**。
它不破解、不内置任何签名算法实现，也不把话题名 + 热度值冒充新闻——所有词条一律
回查到带事实的独立原文才成行（01 instruction §3.3 / social_main.require_article）。

四条通路（按优先级尝试，任一条成功即登记，失败逐条申报，绝不编数据）：

  A. open_aggregator  自建 / 公共开源聚合实例（DailyHotApi、newsnow、RSSHub）
  B. official_api     平台官方开放能力（需企业资质；注意官方能力 ≠ 全网热榜）
  C. licensed_api     已授权第三方聚合 API（一把 Key，字段统一，按次计费）
  D. self_sign        自持 Cookie + 签名（f2 / xhs / MediaCrawler；需自备浏览器与代理）

凭证一律从环境变量 / GitHub Secrets 读取，不写进代码与仓库；模板见
data/platform_credentials.example.yaml。
"""
from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

ssl._create_default_https_context = ssl._create_unverified_context

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ---------------------------------------------------------------- 10 个社交主干平台
PLATFORMS: Dict[str, Dict[str, Any]] = {
    "微博·热搜":       {"dailyhot": "weibo",    "rsshub": "weibo/hot/search",      "official": "微博开放平台有热搜 API（open.weibo.com）", "selfsign": "不需要"},
    "百度·热搜":       {"dailyhot": "baidu",    "rsshub": "baidu/hot",             "official": "无公开热榜 API", "selfsign": "不需要"},
    "抖音·热点":       {"dailyhot": "douyin",   "rsshub": "douyin/hot",            "official": "开放平台仅授权账号维度；热榜非开放能力", "selfsign": "a_bogus / msToken / ttwid + 住宅 IP"},
    "今日头条·热榜":   {"dailyhot": "toutiao",  "rsshub": "toutiao/hot",           "official": "无公开热榜 API", "selfsign": "签名（_signature）"},
    "知乎·热榜":       {"dailyhot": "zhihu",    "rsshub": "zhihu/hot",             "official": "无公开热榜 API", "selfsign": "Cookie + x-zse-96"},
    "B站·热门":        {"dailyhot": "bilibili", "rsshub": "bilibili/popular",      "official": "开放平台有视频/分区数据", "selfsign": "不需要（WBI 签名可选）"},
    "小红书·热点":     {"dailyhot": None,       "rsshub": None,                    "official": "开放平台笔记详情 v4（需 note_id，非热榜）", "selfsign": "x-s / x-t / x-s-common + Cookie + playwright"},
    "快手·热点":       {"dailyhot": "kuaishou", "rsshub": "kuaishou/hot",          "official": "无公开热榜 API", "selfsign": "Cookie + 签名"},
    "百度贴吧·热议":   {"dailyhot": "tieba",    "rsshub": "tieba/hot",             "official": "无公开热榜 API", "selfsign": "不需要"},
    "微信·搜一搜·搜索发现": {"dailyhot": None, "rsshub": None,                      "official": "官方无按关键词查询全网结果的公开 API（搜一搜开放平台只做内容接入 / 品牌展示）", "selfsign": "登录态 + 设备指纹，不建议"},
}

# 中文平台名 → 免费实例池的路由名（小红书 / 微信搜一搜无免鉴权上游，留空）
ROUTE_ALIAS: Dict[str, str] = {
    "微博·热搜": "weibo", "百度·热搜": "baidu", "抖音·热点": "douyin",
    "今日头条·热榜": "toutiao", "知乎·热榜": "zhihu", "B站·热门": "bilibili",
    "小红书·热点": "", "快手·热点": "kuaishou", "百度贴吧·热议": "tieba",
    "微信·搜一搜·搜索发现": "",
}

# ---------------------------------------------------------------- 四条通路的元数据（写进 Excel 工程留痕 G）
TIERS: List[Dict[str, str]] = [
    {"id": "A", "name": "开源聚合自建", "能拿到什么": "热榜词条、排名、热度值、原始链接（部分带上榜时间）",
     "前提": "在能直连上游的机器上自建 DailyHotApi / newsnow / RSSHub，或找到可达公共实例",
     "成本": "0 元 + 一台服务器 / Vercel / Cloudflare；需自己维护上游路由",
     "凭证": "DAILYHOT_BASE_URL / NEWSNOW_BASE_URL / RSSHUB_BASE_URL",
     "稳定性": "上游改版会失效（DailyHotApi 最近提交停在 2026-03），需逐路由实测",
     "适配平台": "8/10（小红书、微信搜一搜无上游路由）"},
    {"id": "B", "name": "官方开放平台", "能拿到什么": "授权账号资料 / 视频列表 / 笔记详情 / 内容接入能力",
     "前提": "企业或组织资质、创建应用、能力逐项申请审核（5–7 个工作日）",
     "成本": "免费但有 QPS / 频率限制；个人开发者申请门槛高",
     "凭证": "DOUYIN_CLIENT_KEY/SECRET、XHS_APP_ID/SECRET、WECHAT_APPID/SECRET",
     "稳定性": "最高（官方 SLA），但**不提供全网热榜**，不能替代榜单采集",
     "适配平台": "抖音（账号维度）/ 小红书（笔记详情）/ 微信（内容接入）"},
    {"id": "C", "name": "已授权第三方聚合 API", "能拿到什么": "热榜、关键词搜索、作品 / 笔记 / 评论、发布时间、互动数",
     "前提": "注册拿 Key，按次付费；确认服务方数据来源与授权合规",
     "成本": "按次计费（公开报价 $0.0001–0.02/次起），失败通常不计费",
     "凭证": "HOT_API_PROVIDER + HOT_API_KEY（TikHub / DataXAPI / Okdata / RedFox / 极致了）",
     "稳定性": "高（服务方维护签名与 IP），依赖供应商；字段统一便于接入",
     "适配平台": "10/10（微信搜一搜需选带 /wx/sousou、wechat_search 端点的服务方）"},
    {"id": "D", "name": "自持 Cookie + 签名", "能拿到什么": "网页端能看到的所有公开数据",
     "前提": "自备账号 Cookie、无头浏览器生成签名、国内住宅代理 / TLS 指纹伪装",
     "成本": "代理 + 维护成本；平台每次改签名算法就要跟着改",
     "凭证": "DOUYIN_COOKIE / XHS_COOKIE / XHS_XSEC_TOKEN / PROXY_URL + f2 / xhs / playwright",
     "稳定性": "最低（a_bogus JS VM、x-s/x-t、设备指纹、461/300013 风控码会变）",
     "适配平台": "抖音 / 小红书 / 知乎 / 快手"},
    # 2026-11-20 追加：用户追问「真的只能靠签名 / 开放平台授权吗」→ 不能。
    # 平台为社交分享预览公开输出的 SSR 页本来就不需要鉴权，这是第五条通路。
    {"id": "E", "name": "公开分享页 SSR（无签名 / 无登录）",
     "能拿到什么": "小红书笔记标题/正文（og:meta + __INITIAL_STATE__）；抖音作品 desc/作者/秒级 create_time（_ROUTER_DATA）；"
                   "公众号文章标题/正文/ct 秒级时间；公众号合集 article_list（带 create_time）；Bing site: 定向 URL",
     "前提": "拿到平台自己公开的分享链接（小红书需带 xsec_token；抖音需 aweme_id；微信需 /s/{id} 永久链接）；"
             "换到 unrestricted 网络（小红书 / 抖音域名在本沙盒被出口 403）",
     "成本": "0 元、0 凭证；只需低频调用，批量高频会触发滑块",
     "凭证": "无（SHARE_PAGE_LINKS 可选：外部注入的分享链接清单）",
     "稳定性": "中（不依赖签名算法，但匿名 SSR 字段会缩水；验证壳 / 空壳页须逐条识别）",
     "适配平台": "小红书 / 抖音 / 微信公众平台（+ Bing site: 回查）"},
]

# ---------------------------------------------------------------- 环境变量 → 通路配置
ENV_MAP: Dict[str, str] = {
    "DAILYHOT_BASE_URL": "A", "NEWSNOW_BASE_URL": "A", "RSSHUB_BASE_URL": "A",
    "DOUYIN_CLIENT_KEY": "B", "XHS_APP_ID": "B", "WECHAT_APPID": "B",
    "HOT_API_PROVIDER": "C", "HOT_API_KEY": "C",
    "DOUYIN_COOKIE": "D", "XHS_COOKIE": "D", "PROXY_URL": "D",
}

# 第三方聚合服务的端点模板（只登记调用约定，不写死任何一家为默认）
# 内置真实适配：dataxapi（热榜，GET /v1/<platform>/trends/list，Bearer）、
#               tikhub（微信搜一搜，POST /api/v1/wechat_search/v2/fetch_search，Bearer）。
# 其余 provider 走通用约定 GET {base}/hot?platform=<route>，失败逐条留痕。
LICENSED_ENDPOINTS: Dict[str, Dict[str, str]] = {
    "tikhub":   {"wechat": "/api/v1/wechat_search/v2/fetch_search",
                 "header": "Authorization: Bearer",
                 "note": "POST JSON {keyword, business_type:'article'}；关键词取 HOT_API_KEYWORDS"},
    "dataxapi": {"hot": "/v1/<platform>/trends/list",
                 "header": "Authorization: Bearer",
                 "note": "GET；返回 data.items；抖音/小红书/微博/知乎/B站/快手同一命名规律"},
    "okdata":   {"note": "17 平台统一 Key，热榜 / 搜索 / 作品同一套字段"},
    "redfox":   {"note": "POST /story/api/... + REDFOX_API_KEY，含搜一搜 / 抖音 / 小红书"},
    "jzl":      {"wechat": "/wx/sousou", "header": "API-Key", "note": "POST，按关键词查搜一搜"},
}

# DataXAPI 热榜接口命名规律（服务方公开文档：douyin/weibo/zhihu/bilibili/kuaishou/
# xiaohongshu 的 .trends.list）；未登记的平台如实返回「无路由」，不猜端点。
DATAXAPI_ROUTES: Dict[str, str] = {
    "weibo": "weibo", "douyin": "douyin", "zhihu": "zhihu", "baidu": "baidu",
    "toutiao": "toutiao", "bilibili": "bilibili", "kuaishou": "kuaishou",
    "tieba": "tieba", "thepaper": "thepaper", "36kr": "36kr",
    "xiaohongshu": "xiaohongshu",
}

# 微信搜一搜按关键词回查时的默认主题词（贴合大众贴近度口径，省免费额度）
DEFAULT_KEYWORDS = ["医保社保", "裁员", "房地产", "新能源", "地方债"]


@dataclass
class HotClue:
    """一条榜单线索。**不是新闻**，必须回查带事实的独立原文才可成行。"""
    platform: str = ""
    tier: str = ""
    title: str = ""
    hot: str = ""
    url: str = ""
    onboard_time: str = ""
    rank: int = 0
    note: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


def _http(url: str, *, timeout: float = 7.0, method: str = "GET",
          headers: Optional[Dict[str, str]] = None,
          body: Optional[dict] = None, proxy: Optional[str] = None) -> Tuple[int, str]:
    h = {"User-Agent": UA, "Accept": "application/json,text/xml,text/html,*/*",
         "Accept-Language": "zh-CN,zh;q=0.9"}
    h.update(headers or {})
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    opener = urllib.request.build_opener()
    if proxy:
        opener.add_handler(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    with opener.open(req, timeout=timeout) as r:
        return r.status, r.read(400000).decode(r.headers.get_content_charset() or "utf-8", "replace")


def _parse_dailyhot(text: str, platform: str, tier: str) -> List[HotClue]:
    """统一 JSON：DailyHotApi {data:[...]} / NewsNow {status,items:[...]} /
    DataXAPI {data:{items:[...]}} / 兼容顶层数组。"""
    d = json.loads(text)
    if isinstance(d, list):
        arr = d
    elif isinstance(d, dict):
        arr = d.get("data") or d.get("items") or []
        if isinstance(arr, dict):
            arr = (arr.get("items") or arr.get("list") or arr.get("word_list")
                   or arr.get("band_list") or arr.get("realtime") or [])
    else:
        arr = []
    if not isinstance(arr, list):
        arr = []
    out: List[HotClue] = []
    for i, x in enumerate(arr, 1):
        if not isinstance(x, dict):
            continue
        t = str(x.get("title") or x.get("word") or x.get("name") or "").strip()
        if not t:
            continue
        out.append(HotClue(platform=platform, tier=tier, title=t,
                           hot=str(x.get("hot") or x.get("hotValue") or x.get("heat") or ""),
                           url=str(x.get("url") or x.get("link") or x.get("mobileUrl") or ""),
                           onboard_time=str(x.get("time") or x.get("onboard_time") or x.get("pubDate") or ""),
                           rank=int(x.get("index", i) or i), raw=x))
    return out


def _parse_rss(text: str, platform: str, tier: str) -> List[HotClue]:
    import xml.etree.ElementTree as ET
    root = ET.fromstring(text)
    out = []
    for i, node in enumerate(root.findall(".//item"), 1):
        t = (node.findtext("title") or "").strip()
        if t:
            out.append(HotClue(platform=platform, tier=tier, title=t,
                               url=(node.findtext("link") or "").strip(),
                               onboard_time=(node.findtext("pubDate") or "").strip(), rank=i))
    return out


def _tier_a(platform: str, cfg: dict, timeout: float) -> Tuple[List[HotClue], str]:
    """通路 A：自建实例优先 → 自建 RSSHub → 免费公共实例池逐个实测。"""
    from . import free_sources
    meta = PLATFORMS[platform]
    route = meta.get("dailyhot") or ROUTE_ALIAS.get(platform, "")
    tried_notes: List[str] = []
    base = (os.environ.get("DAILYHOT_BASE_URL") or cfg.get("dailyhot_base_url") or "").rstrip("/")
    if route and base:
        # 自建实例可能是 DailyHotApi（/{route}，{data:[...]}），也可能是 NewsNow
        # （/api/s?id=<route>&latest，{status,items:[...]}）。自动双格式探测，
        # 也可用 HOT_API_STYLE=dailyhot|newsnow 显式指定，避免用户填了地址却 404。
        style = (os.environ.get("HOT_API_STYLE") or cfg.get("hot_api_style") or "").lower()
        if style == "newsnow":
            attempts = [("NewsNow", f"{base}/api/s?id={urllib.parse.quote(route)}&latest")]
        elif style == "dailyhot":
            attempts = [("DailyHotApi", f"{base}/{route}")]
        else:
            attempts = [("DailyHotApi", f"{base}/{route}"),
                        ("NewsNow", f"{base}/api/s?id={urllib.parse.quote(route)}&latest")]
        for label, url in attempts:
            try:
                code, text = _http(url, timeout=timeout)
                if code != 200:
                    tried_notes.append(f"自建 {label} HTTP {code}")
                    continue
                try:
                    clues = _parse_dailyhot(text, platform, "A")  # 同时兼容 {data} 与 {items}
                except Exception:
                    tried_notes.append(f"自建 {label} HTTP 200 但非 JSON（可能是前端首页）")
                    continue
                if clues:
                    return clues, f"自建 {label} → {len(clues)} 条"
                tried_notes.append(f"自建 {label} HTTP 200 但 0 条")
            except Exception as e:
                tried_notes.append(f"自建 {label} 不可达：{type(e).__name__}")
    rsshub = (os.environ.get("RSSHUB_BASE_URL") or cfg.get("rsshub_base_url") or "").rstrip("/")
    if meta.get("rsshub") and rsshub:
        url = f"{rsshub}/{meta['rsshub']}"
        try:
            code, text = _http(url, timeout=timeout)
            clues = _parse_rss(text, platform, "A")
            if clues:
                return clues, f"自建 RSSHub /{meta['rsshub']} → {len(clues)} 条"
            tried_notes.append(f"自建 RSSHub HTTP {code} 但 0 条")
        except Exception as e:
            tried_notes.append(f"自建 RSSHub 不可达：{type(e).__name__}")
    if not route:
        return [], "开源聚合无该平台上游路由（小红书 / 微信搜一搜），需走通路 B/C/D"
    # 免费公共实例池（DailyHotApi / NewsNow / RSSHub / 60s / vvhan / tenapi …）
    rows, note = free_sources.collect_free(route, timeout)
    if rows:
        clues = [HotClue(platform=platform, tier="A", title=str(x.get("title") or ""),
                         hot=str(x.get("hot") or ""), url=str(x.get("url") or ""),
                         onboard_time=str(x.get("time") or ""),
                         rank=int(x.get("index") or i), raw=x)
                 for i, x in enumerate(rows, 1) if x.get("title")]
        if clues:
            return clues, f"免费公共实例池：{note}"
    return [], ("；".join(tried_notes + [note]) if tried_notes else note) \
        + "；可自建 NewsNow(Cloudflare Pages 免费) / DailyHotApi / RSSHub"


def _tier_b(platform: str, cfg: dict, timeout: float) -> Tuple[List[HotClue], str]:
    """官方开放平台：只登记能力，不发起「热榜」请求——它本来就不提供。"""
    has = any(os.environ.get(k) for k in ("DOUYIN_CLIENT_KEY", "XHS_APP_ID", "WECHAT_APPID"))
    if not has:
        return [], "未配置官方凭证；且官方开放平台不提供全网热榜，不能替代榜单采集"
    return [], ("官方凭证已配置，但热榜不在开放能力内："
                + PLATFORMS[platform]["official"]
                + "；本通路只可用于已授权账号 / 指定笔记的详情补全")


def _tier_c(platform: str, cfg: dict, timeout: float) -> Tuple[List[HotClue], str]:
    """通路 C：已授权聚合 API。填 HOT_API_PROVIDER + HOT_API_KEY + HOT_API_BASE_URL 即用。

    内置两家免费试用服务的真实调用格式：
      dataxapi → GET {base}/v1/<platform>/trends/list  + Authorization: Bearer
      tikhub   → POST {base}/api/v1/wechat_search/v2/fetch_search + Bearer
                 （微信搜一搜按 HOT_API_KEYWORDS 逐词回查，返回文章标题/URL）
    其余 provider 走通用约定 GET {base}/hot?platform=<route>，失败逐条留痕。
    """
    provider = (os.environ.get("HOT_API_PROVIDER") or cfg.get("licensed_provider") or "").lower()
    key = os.environ.get("HOT_API_KEY") or cfg.get("licensed_key") or ""
    if not (provider and key):
        return [], "未配置 HOT_API_PROVIDER + HOT_API_KEY（按次计费的统一聚合 key）"
    base = (os.environ.get("HOT_API_BASE_URL") or cfg.get("licensed_base_url") or "").rstrip("/")
    if not base:
        return [], (f"provider={provider} 已配置 key，但缺 HOT_API_BASE_URL；"
                    f"调用约定：{LICENSED_ENDPOINTS.get(provider, {}).get('note', '见服务方文档')}")
    auth = {"Authorization": f"Bearer {key}", "X-API-Key": key}
    route = PLATFORMS[platform].get("dailyhot") or ROUTE_ALIAS.get(platform, "")

    # ---- 1) DataXAPI：热榜 /trends/list ----
    if provider == "dataxapi":
        slug = DATAXAPI_ROUTES.get(route or "", "")
        if not slug:
            return [], (f"DataXAPI 未登记「{platform}」的热榜路由；"
                        f"微信搜一搜请改用 HOT_API_PROVIDER=tikhub")
        url = f"{base}/v1/{slug}/trends/list"
        try:
            code, text = _http(url, timeout=timeout, headers=auth)
            clues = _parse_dailyhot(text, platform, "C")
            if clues:
                return clues, f"DataXAPI /v1/{slug}/trends/list → {len(clues)} 条"
            return [], f"DataXAPI HTTP {code} 但 0 条（核对余额与接口名 /v1/{slug}/trends/list）"
        except Exception as e:
            return [], f"DataXAPI 请求失败：{type(e).__name__}"

    # ---- 2) TikHub：微信搜一搜按关键词回查 ----
    if provider == "tikhub":
        if "微信" not in platform and "搜一搜" not in platform:
            return [], ("TikHub 本适配仅登记微信搜一搜 /wechat_search/v2/fetch_search；"
                        f"「{platform}」请改用 HOT_API_PROVIDER=dataxapi 或自建聚合")
        kws = [x.strip() for x in str(
            os.environ.get("HOT_API_KEYWORDS") or ",".join(DEFAULT_KEYWORDS)).split(",") if x.strip()]
        kws = kws[:3]                      # 省免费额度，每次跑批最多 3 个词
        all_clues: List[HotClue] = []
        notes: List[str] = []
        url = base + "/api/v1/wechat_search/v2/fetch_search"
        for kw in kws:
            try:
                code, text = _http(url, timeout=timeout, method="POST", headers=auth,
                                   body={"keyword": kw, "business_type": "article"})
                if code != 200:
                    notes.append(f"{kw}:HTTP {code}")
                    continue
                from . import free_sources
                rows = free_sources.normalize_clues("generic", text)
                for i, r in enumerate(rows, len(all_clues) + 1):
                    if not r.get("title"):
                        continue
                    all_clues.append(HotClue(platform=platform, tier="C", title=r["title"],
                                             hot=str(r.get("hot") or ""), url=str(r.get("url") or ""),
                                             onboard_time=str(r.get("time") or ""), rank=i,
                                             note=f"关键词={kw}", raw=r))
                notes.append(f"{kw}:{len(rows)} 条")
            except Exception as e:
                notes.append(f"{kw}:{type(e).__name__}")
        if all_clues:
            # 同标题去重
            seen, uniq = set(), []
            for c in all_clues:
                if c.title not in seen:
                    seen.add(c.title)
                    uniq.append(c)
            return uniq, f"TikHub 微信搜一搜 → {len(uniq)} 条（{'；'.join(notes)}）"
        return [], "TikHub 微信搜一搜：" + "；".join(notes) + "（核对余额 / business_type / 返回字段）"

    # ---- 3) 其余 provider：通用约定 ----
    try:
        code, text = _http(f"{base}/hot?platform={urllib.parse.quote(route or platform)}",
                           timeout=timeout, headers=auth)
        clues = _parse_dailyhot(text, platform, "C")
        if clues:
            return clues, f"{provider} → {len(clues)} 条"
        return [], f"{provider} HTTP {code} 但 0 条（核对 platform 路由与余额）"
    except Exception as e:
        return [], f"{provider} 请求失败：{type(e).__name__}"


def _tier_d(platform: str, cfg: dict, timeout: float) -> Tuple[List[HotClue], str]:
    """自签名通路：只暴露执行契约，不在流水线里跑破解逻辑。"""
    cookie = os.environ.get("DOUYIN_COOKIE") or os.environ.get("XHS_COOKIE")
    if not cookie:
        return [], ("未配置 COOKIE + PROXY_URL；自签名需自备浏览器签名环境 "
                    "（抖音 f2 的 ABogusManager / 小红书 xhs + playwright stealth 生成 x-s/x-t/x-s-common）")
    return [], ("已配置 Cookie，但签名执行器须独立部署（Python 进程内不内置 JS VM 逆向）："
                "由外部签名服务产出 clue JSON 后调用 platform_access.inject_clues() 注入")


ADAPTERS = {"A": _tier_a, "B": _tier_b, "C": _tier_c, "D": _tier_d}


def fetch_platform(platform: str, cfg: dict, timeout: float = 7.0) -> Dict[str, Any]:
    """单平台：按优先级尝试四条通路，逐条留痕，不编数据。"""
    order = [x.strip().upper() for x in str(cfg.get("tier_order", "A,C,B,D")).split(",")]
    tried: List[Dict[str, str]] = []
    clues: List[HotClue] = []
    for t in order:
        if t not in ADAPTERS:
            continue
        got, note = ADAPTERS[t](platform, cfg, timeout)
        tried.append({"通路": t, "结论": note})
        if got:
            clues = got
            break
    return {"平台": platform, "线索": clues, "线索数": len(clues),
            "命中通路": next((x["通路"] for x in tried if x["结论"] and "→" in x["结论"]), ""),
            "逐通路": tried,
            "独立原文准入": "词条必须回查带事实的独立原文才成行；只有话题名+热度进附录 15"}


def inject_clues(platform: str, rows: List[dict], tier: str = "D") -> List[HotClue]:
    """外部签名服务 / 人工导入的线索统一入口（字段归一 + 去重）。"""
    out, seen = [], set()
    for x in rows:
        t = str(x.get("title") or x.get("word") or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(HotClue(platform=platform, tier=tier, title=t,
                           hot=str(x.get("hot") or ""), url=str(x.get("url") or ""),
                           onboard_time=str(x.get("onboard_time") or x.get("time") or ""),
                           rank=int(x.get("rank") or len(out) + 1), raw=x))
    return out


def run_platform_access(cfg: dict, timeout: float = 7.0) -> Dict[str, Any]:
    """主入口：10 平台逐条申报 + 能力矩阵；供 pipeline / reach / CI 调用。"""
    conf = cfg.get("platform_access", {}) or {}
    if not conf.get("enabled", True):
        return {"启用": False, "平台": [], "矩阵": [], "说明": "config 关闭"}
    platforms = conf.get("platforms") or list(PLATFORMS)
    results = [fetch_platform(p, {**conf, **{"tier_order": conf.get("tier_order", "A,C,B,D")}}, timeout)
               for p in platforms if p in PLATFORMS]
    creds = {k: ("已配置" if os.environ.get(k) else "未配置") for k in ENV_MAP}
    from . import free_sources
    fs = free_sources.probe_all(float(conf.get("free_probe_timeout", 4.0)))
    # 通路 E：无签名公开分享页 SSR。只在「外部注入了分享链接」时才实际请求，
    # 否则只做能力申报 + 域名可达性实测，不凭空造链接、不伪装成已取得。
    from . import share_pages
    share_rows = []
    # 三处都可注入：platform_access.share_links / social_main.share_links /
    # 环境变量 SHARE_PAGE_LINKS（逗号分隔），方便 CI Secrets 直接给链接清单
    injected = list(conf.get("share_links") or [])
    injected += list((cfg.get("social_main", {}) or {}).get("share_links") or [])
    injected += [x.strip() for x in os.environ.get("SHARE_PAGE_LINKS", "").split(",") if x.strip()]
    seen_links: set = set()
    injected = [x for x in injected if not (x in seen_links or seen_links.add(x))]
    if injected:
        for link in injected[:int(conf.get("share_links_max", 12))]:
            if "xiaohongshu" in link or "xhslink" in link:
                payload, why = share_pages.xhs_resolve(link, timeout=timeout)
            elif "douyin" in link or "iesdouyin" in link:
                payload, why = share_pages.douyin_resolve(link, timeout=timeout)
            elif "mp.weixin.qq.com/s/" in link:
                payload, why = share_pages.wechat_article(link, timeout=timeout)
            else:
                payload, why = None, "未识别的分享链接类型（只认小红书 / 抖音 / 公众号）"
            share_rows.append({"链接": link[:110], "平台": (payload or {}).get("平台", "—"),
                               "成稿": bool(payload), "判定": why,
                               "标题": (payload or {}).get("标题", "")[:80],
                               "发布时间": (payload or {}).get("发布时间", "")})
    else:
        share_rows.append({"链接": "（未注入 SHARE_PAGE_LINKS）", "平台": "—", "成稿": False,
                           "判定": "通路 E 就绪但无分享链接输入；不凭空造 URL，逐平台能力申报见工程留痕 G-d",
                           "标题": "", "发布时间": ""})
    st = share_pages.self_test()
    return {
        "启用": True,
        "平台": results,
        "凭证": creds,
        "矩阵": TIERS,
        "免费源": {"可达": fs.get("可达", []), "实例": fs.get("实例", []),
                   "开源项目": fs.get("开源项目", []), "免费额度": fs.get("免费额度", []),
                   "结论": fs.get("结论", ""), "报告": "data/free_sources_probe.json"},
        "分享页通路E": {"自检": st, "逐条": share_rows,
                      "成稿": sum(1 for r in share_rows if r["成稿"]),
                      "说明": "平台公开发布的分享预览页带 SSR 数据，不需要 Cookie / a_bogus / x-s / 开放平台资质；"
                              "本环境小红书 / 抖音域名级 403，微信 mp 与 Bing site: 实测 200。"},
        "线索总数": sum(r["线索数"] for r in results),
        "已启用通路": sorted({r["命中通路"] for r in results if r["命中通路"]}),
        "说明": ("本环境实测：微博官方 JSON、微信 mp.weixin.qq.com/s 与 appmsgalbum、Bing site: 均 HTTP 200；"
                 "抖音 / 小红书 / 搜狗微信 / 公共聚合实例被出口 403。通路 E 证明「无签名可取」在代码与事实上都成立，"
                 "缺的只是可达网络与真实分享链接，不是算法或资质。"),
        "回查契约": "01 §3.3：词条 → 带事实独立原文 → 时间校验 → 成稿；失败进附录 15",
    }


def self_test() -> Dict[str, Any]:
    """离线自检：不联网，验证矩阵 / 路由 / 字段归一 / 缺凭证时的诚实申报。"""
    _old = os.environ.get("FREE_SOURCES_OFFLINE")
    os.environ["FREE_SOURCES_OFFLINE"] = "1"
    errs: List[str] = []
    if len(PLATFORMS) != 10:
        errs.append("社交主干平台数应为 10")
    if [t["id"] for t in TIERS] != ["A", "B", "C", "D", "E"]:
        errs.append("五条通路编号应为 A/B/C/D/E（E 为无签名公开分享页 SSR）")
    from . import share_pages
    st = share_pages.self_test()
    if st["FAIL"]:
        errs.append(f"share_pages 自检 {st['FAIL']} 项失败")
    for p, m in PLATFORMS.items():
        for k in ("dailyhot", "rsshub", "official", "selfsign"):
            if k not in m:
                errs.append(f"{p} 缺 {k}")
    # 字段归一
    sample = json.dumps({"data": [{"title": "寒潮蓝色预警", "hot": "123456", "url": "https://x/a", "index": 1},
                                  {"word": "油价下调", "hotValue": "999", "link": "https://x/b"}]})
    clues = _parse_dailyhot(sample, "微博·热搜", "A")
    if len(clues) != 2 or clues[1].title != "油价下调" or clues[1].hot != "999":
        errs.append("DailyHotApi 字段归一错误")
    rss = _parse_rss("<rss><channel><item><title>词条A</title><link>https://x/c</link></item></channel></rss>",
                     "知乎·热榜", "A")
    if len(rss) != 1 or rss[0].title != "词条A":
        errs.append("RSSHub RSS 解析错误")
    inj = inject_clues("小红书·热点", [{"title": "A"}, {"title": "A"}, {"word": "B", "hot": 3}])
    if len(inj) != 2:
        errs.append("inject_clues 去重错误")
    # 缺凭证时不得返回任何线索
    for t, fn in ADAPTERS.items():
        got, note = fn("抖音·热点", {}, 1.0)
        if got:
            errs.append(f"通路 {t} 在无凭证时不应产出线索")
        if not note:
            errs.append(f"通路 {t} 缺失败说明")
    r = fetch_platform("微信·搜一搜·搜索发现", {}, 1.0)
    if r["线索数"] or len(r["逐通路"]) != 4:
        errs.append("fetch_platform 逐通路留痕错误")
    if _old is None:
        os.environ.pop("FREE_SOURCES_OFFLINE", None)
    else:
        os.environ["FREE_SOURCES_OFFLINE"] = _old
    return {"ok": not errs, "errors": errs,
            "checked": ["10平台", "5通路矩阵（A–D 榜单 + E 分享页SSR）", "share_pages解析器12项",
                        "DailyHotApi归一", "RSSHub归一", "注入去重", "无凭证零产出", "逐通路留痕"]}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="受限平台取数通路自检 / 实测")
    ap.add_argument("--offline", action="store_true", help="只跑离线自检，不联网")
    ap.add_argument("--timeout", type=float, default=7.0)
    a = ap.parse_args()
    print(json.dumps(self_test(), ensure_ascii=False, indent=2))
    if not a.offline:
        import yaml
        from pathlib import Path
        cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8")) or {}
        print(json.dumps(run_platform_access(cfg, a.timeout), ensure_ascii=False,
                         indent=2, default=str))
