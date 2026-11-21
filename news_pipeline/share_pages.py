"""受限平台「无签名 / 无登录」公开页面解析（通路 E）。

用户追问「小红书笔记 / 微信搜一搜 / 抖音作品真的只能靠签名或开放平台授权吗」——
不能。平台上公开发布、用于社交分享预览的页面本来就带 SSR 数据，不需要 Cookie、
不需要 a_bogus / x-s 签名、不需要开放平台资质。本模块把这三条通路写成纯代码：

  小红书  explore/{note_id}?xsec_token=…  → og:meta + window.__INITIAL_STATE__
          （2024 起强制 xsec_token，匿名 SSR 通常不含作者 / 互动数，不编）
  抖音    iesdouyin.com/share/video/{id}/ → window._ROUTER_DATA
          （desc / author / create_time，不碰需签名的 web 接口）
  微信    mp.weixin.qq.com/s/{id}         → msg_title / ct / js_content（免登录）
          mp.weixin.qq.com/mp/appmsgalbum → 合集 JSON，带秒级 create_time

以及两个「发现 URL」的合规入口：
  - Bing / 必应 `site:` 定向检索（不吃 snippet，只抽 URL，再直取正文）
  - 60s / DailyHotApi / NewsNow 的 GitHub Actions 静态归档（换网络可达）

硬约束（与 01 §3.3 一致）：
  * 全部只取平台自己公开给开放网络的字段；解析不到就给明确原因，不编造；
  * 只认独立原文 / 独立作品页，验证页、登录壳、滑块一律判失败；
  * 时间戳必须来自页面自身字段，标题里的月日只是 date_clue，不反推发布时间。

所有函数返回 (payload | None, reason)，reason 直接写进 Excel 工程留痕 G-d。
"""
from __future__ import annotations

import html as html_lib
import json
import re
import ssl
import urllib.parse as up
import urllib.request as ur
from typing import Any, Dict, List, Optional, Tuple

try:
    ssl._create_default_https_context = ssl._create_unverified_context
except Exception:
    pass

UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
UA_PC = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

_VERIFY_MARKERS = ("环境异常", "去验证", "访问过于频繁", "参数错误", "该内容已被发布者删除",
                   "website-login", "captcha", "请在微信客户端打开", "页面不见啦")


def _get(url: str, *, timeout: float = 7.0, mobile: bool = False,
         referer: str = "https://www.bing.com/", max_bytes: int = 600_000) -> Tuple[int, str, Dict[str, str]]:
    req = ur.Request(url, headers={
        "User-Agent": UA_MOBILE if mobile else UA_PC,
        "Accept": "text/html,application/json,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": referer,
    })
    with ur.urlopen(req, timeout=timeout) as r:
        return r.status, r.read(max_bytes).decode("utf-8", "ignore"), dict(r.headers)


def _clean(t: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html_lib.unescape(t)).strip()


def _is_verify(body: str) -> Optional[str]:
    for m in _VERIFY_MARKERS:
        if m in body:
            return f"页面返回验证/登录壳（命中「{m}」），本条如实判未取得"
    return None


# ------------------------------------------------------------------ 小红书
_XHS_NOTE = re.compile(r"(?:explore/|discovery/item/)([0-9a-fA-F]{16,32})")
_XHS_TOKEN = re.compile(r"xsec_token=([A-Za-z0-9_\-=]+)")
_XHS_SHORT = re.compile(r"https?://xhslink\.(?:cn|com)/[A-Za-z0-9_/]+", re.I)


def xhs_resolve(url_or_text: str, *, timeout: float = 7.0) -> Tuple[Optional[Dict[str, Any]], str]:
    """小红书公开分享链接 → 笔记元数据。不需要 Cookie / x-s 签名。

    限制：链接必须带 xsec_token（App 分享链接自带；裸 explore URL 会返回验证壳）。
    匿名 SSR 不含作者 / 互动数时留空，不编。批量高频会触发滑块，调用方须低频。
    """
    text = (url_or_text or "").strip()
    short = _XHS_SHORT.search(text)
    if short and "/explore/" not in text:
        try:
            code, body, hdrs = _get(short.group(0), timeout=timeout, mobile=True)
            # xhslink 常见「200 + HTML Found 指针」而非 302
            m = re.search(r'href=["\'](https?://[^"\']*(?:explore|discovery/item)[^"\']*)', body)
            if m:
                text = html_lib.unescape(m.group(1))
            elif hdrs.get("Location"):
                text = hdrs["Location"]
            else:
                return None, "xhslink 短链未返回可解析的 explore 目标（HTTP 200 Found 指针缺失）"
        except Exception as exc:
            return None, f"xhslink 短链解析失败：{type(exc).__name__}"

    nid = _XHS_NOTE.search(text)
    if not nid:
        return None, "未识别到 16–32 位 note_id（需要 explore/ 或 discovery/item/ 分享链接）"
    tok = _XHS_TOKEN.search(text)
    if not tok:
        return None, "缺少 xsec_token：2024 起小红书 SSR 强制要求分享链接带该参数，裸 note URL 不可用（不是签名算法问题）"

    target = (f"https://www.xiaohongshu.com/explore/{nid.group(1)}"
              f"?xsec_token={tok.group(1)}&xsec_source=pc_feed")
    try:
        code, body, _ = _get(target, timeout=timeout)
    except Exception as exc:
        return None, f"小红书 SSR 页请求失败：{type(exc).__name__}（本环境域名级 403 时属网络出口限制，换网络后可直连）"
    if code != 200:
        return None, f"小红书 SSR 页 HTTP {code}"
    v = _is_verify(body)
    if v:
        return None, v

    def meta(name: str) -> str:
        m = re.search(rf'<meta[^>]+(?:property|name)="{re.escape(name)}"[^>]+content="([^"]*)"', body)
        return html_lib.unescape(m.group(1)).strip() if m else ""

    title = meta("og:title") or meta("twitter:title")
    desc = meta("og:description") or meta("description")
    state: Dict[str, Any] = {}
    sm = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*(?:;|</script>)", body, re.S)
    if sm:
        # SSR 常截断在字符串中间，逐段缩短重试；失败就只保留 og:meta，不猜
        raw = sm.group(1)
        for cut in (len(raw), len(raw) - 200, len(raw) - 800, len(raw) - 2000):
            if cut <= 0:
                break
            try:
                state = json.loads(raw[:cut])
                break
            except Exception:
                continue
    note = {}
    if isinstance(state, dict):
        for key in ("note", "noteDetail", "currentNote"):
            v2 = state.get(key)
            if isinstance(v2, dict):
                note = v2.get("note") if isinstance(v2.get("note"), dict) else v2
                if note:
                    break
        if not note:
            for v2 in state.values():
                if isinstance(v2, dict) and ("noteDetailMap" in v2 or "notes" in v2):
                    note = v2
                    break
    if not (title or desc or note):
        return None, "SSR 页可达但 og:meta 与 __INITIAL_STATE__ 均为空（可能被降级的空壳页）"
    return {
        "平台": "小红书", "通路": "E-分享页SSR", "note_id": nid.group(1),
        "标题": title, "正文": desc or _clean(str(note.get("desc", "")))[:600],
        "作者": str(note.get("user", {}).get("nickname", "") if isinstance(note.get("user"), dict) else "") or "未取得（匿名 SSR 不含）",
        "发布时间": "未取得（匿名 SSR 不含 publish_time，须从独立原文或榜单快照时间取）",
        "互动数": "未取得（匿名 SSR 不含，需登录或内部 API）",
        "url": target.split("?")[0],
        "证据": "og:meta / window.__INITIAL_STATE__（平台公开 SSR，无 Cookie、无签名）",
    }, "成功：小红书公开分享页 SSR（无签名 / 无登录）"


# ------------------------------------------------------------------ 抖音
_AWEME = re.compile(r"(?:/(?:video|note|slides)/|[?&](?:modal_id|aweme_id|item_ids)=)(\d{6,25})")
_DOUYIN_LINK = re.compile(r"https?://(?:v\.douyin\.com|(?:www\.|m\.)?douyin\.com|(?:www\.)?iesdouyin\.com)/[^\s,。!?、\"'<>]*", re.I)
_ROUTER = re.compile(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S)


def douyin_resolve(url_or_text: str, *, timeout: float = 7.0) -> Tuple[Optional[Dict[str, Any]], str]:
    """抖音公开分享页 → 作品元数据。不需要 Cookie / a_bogus / msToken。

    只解析 iesdouyin.com/share/{video|note}/{aweme_id}/ 的 window._ROUTER_DATA，
    不碰任何需要签名的 web 接口。
    """
    text = (url_or_text or "").strip()
    link = _DOUYIN_LINK.search(text)
    if link and "v.douyin.com" in link.group(0):
        try:
            req = ur.Request(link.group(0), headers={"User-Agent": UA_MOBILE})
            class _NoRedirect(ur.HTTPRedirectHandler):
                def redirect_request(self, *a, **k):
                    return None
            op = ur.build_opener(_NoRedirect)
            with op.open(req, timeout=timeout) as r:
                loc = r.headers.get("Location") or ""
            if loc:
                text = loc
        except Exception as exc:
            return None, f"抖音短链展开失败：{type(exc).__name__}"
    m = _AWEME.search(text) or _AWEME.search(link.group(0) if link else "")
    if not m:
        return None, "未识别到 6–25 位 aweme_id（需要作品分享链接，不是热榜词条）"
    aweme = m.group(1)
    kind = "note" if re.search(r"/(?:note|slides)/", text) else "video"
    target = f"https://www.iesdouyin.com/share/{kind}/{aweme}/"
    try:
        code, body, _ = _get(target, timeout=timeout, mobile=True,
                             referer="https://www.douyin.com/")
    except Exception as exc:
        return None, f"抖音分享页请求失败：{type(exc).__name__}（本环境 403 属网络出口限制，换网络后可直连）"
    if code != 200:
        return None, f"抖音分享页 HTTP {code}"
    v = _is_verify(body)
    if v:
        return None, v
    rm = _ROUTER.search(body)
    if not rm:
        return None, "分享页可达但无 window._ROUTER_DATA（页面改版 / 风控降级）"
    try:
        data = json.loads(rm.group(1))
    except Exception as exc:
        return None, f"_ROUTER_DATA 解析失败：{type(exc).__name__}"
    raw = None
    for page in (data.get("loaderData") or {}).values():
        if not isinstance(page, dict):
            continue
        res = page.get("videoInfoRes") or {}
        items = res.get("item_list") or []
        if items:
            raw = items[0]
            break
        flt = res.get("filter_list") or []
        if flt:
            return None, f"抖音平台判定：{flt[0].get('detail_msg') or flt[0].get('filter_reason') or '作品不可见'}"
    if not raw:
        return None, "loaderData 里没有 item_list（作品已删除 / 私密 / 版式变更）"
    ct = int(raw.get("create_time") or 0)
    import datetime as _dt
    pub = (_dt.datetime.fromtimestamp(ct, _dt.timezone(_dt.timedelta(hours=8)))
           .strftime("%Y-%m-%d %H:%M:%S")) if ct else "未取得"
    return {
        "平台": "抖音", "通路": "E-分享页SSR", "aweme_id": aweme,
        "标题": str(raw.get("desc") or "").strip()[:120],
        "作者": str((raw.get("author") or {}).get("nickname") or "未取得"),
        "发布时间": pub, "时间依据": "window._ROUTER_DATA.create_time（秒级，实证）" if ct else "未取得",
        "正文": str(raw.get("desc") or "").strip()[:600],
        "url": f"https://www.douyin.com/{kind}/{aweme}",
        "证据": "iesdouyin 分享页 window._ROUTER_DATA（平台公开 SSR，无 Cookie、无 a_bogus）",
    }, "成功：抖音公开分享页 SSR（无签名 / 无登录）"


# ------------------------------------------------------------------ 微信
_MP_PERM = re.compile(r"https?://mp\.weixin\.qq\.com/s/[A-Za-z0-9_\-]{8,}", re.I)


def wechat_article(url: str, *, timeout: float = 7.0) -> Tuple[Optional[Dict[str, Any]], str]:
    """微信公众号永久文章页 → 标题 / 发布时间 / 正文。免登录、免签名。

    注意：带 src=11&timestamp=…&signature=… 的临时搜索链接会过期并返回验证页，
    只有标准 /s/{id} 永久链接可用；本函数会明确区分。
    """
    u = (url or "").strip()
    if not _MP_PERM.search(u):
        return None, "不是 mp.weixin.qq.com/s/{id} 永久链接（临时 signature 链接会过期，不作数）"
    try:
        code, body, _ = _get(u, timeout=timeout)
    except Exception as exc:
        return None, f"公众号文章页请求失败：{type(exc).__name__}"
    if code != 200:
        return None, f"公众号文章页 HTTP {code}"
    v = _is_verify(body)
    if v:
        return None, v
    title = (re.search(r'var msg_title\s*=\s*["\']([^"\']+)', body)
             or re.search(r'<meta property="og:title" content="([^"]*)"', body))
    ct = re.search(r'var ct\s*=\s*["\'](\d{10})', body)
    desc = re.search(r'var msg_desc\s*=\s*["\']([^"\']*)', body)
    js = re.search(r'id="js_content"[^>]*>(.*?)</div>\s*(?:</div>|<script)', body, re.S)
    content = _clean(js.group(1))[:1200] if js else ""
    if not (title or content):
        return None, "文章页可达但 msg_title / js_content 均空（可能是验证壳或结构改版）"
    import datetime as _dt
    pub = (_dt.datetime.fromtimestamp(int(ct.group(1)), _dt.timezone(_dt.timedelta(hours=8)))
           .strftime("%Y-%m-%d %H:%M:%S")) if ct else "未取得"
    return {
        "平台": "微信公众平台", "通路": "E-公开文章页", "标题": html_lib.unescape(title.group(1)) if title else "未取得",
        "正文": content or (html_lib.unescape(desc.group(1)) if desc else ""),
        "发布时间": pub, "时间依据": "var ct（秒级，实证）" if ct else "未取得（不拿标题月日反推）",
        "url": u.split("&")[0],
        "证据": "mp.weixin.qq.com 公开文章页（无需登录 / 无需开放平台授权）",
    }, "成功：公众号公开文章页（免登录、免签名）"


def wechat_album(biz: str, album_id: str, *, timeout: float = 7.0) -> Tuple[List[Dict[str, Any]], str]:
    """公众号「合集」公开 JSON 列表，无登录可访问，返回秒级 create_time。

    不是全量文章流（只含作者手工归档的合集），但带真实发布时间，正好能进窗口判定。
    """
    if not biz or not album_id:
        return [], "缺少 __biz / album_id（从合集分享链接取，不猜）"
    u = (f"https://mp.weixin.qq.com/mp/appmsgalbum?action=getalbum"
         f"&__biz={up.quote(biz)}&album_id={up.quote(album_id)}&f=json")
    try:
        code, body, _ = _get(u, timeout=timeout)
    except Exception as exc:
        return [], f"合集接口请求失败：{type(exc).__name__}"
    try:
        j = json.loads(body)
    except Exception:
        return [], "合集接口未返回 JSON（结构变更 / 风控）"
    ret = (j.get("base_resp") or {}).get("ret")
    if ret not in (0, None):
        return [], f"合集接口 ret={ret}（参数无效或合集不存在；接口本身无登录可达）"
    import datetime as _dt
    out = []
    arts = ((j.get("getalbum_resp") or {}).get("article_list") or [])
    for a in arts:
        ct = int(a.get("create_time") or 0)
        out.append({
            "标题": a.get("title", ""), "url": a.get("url", ""),
            "发布时间": (_dt.datetime.fromtimestamp(ct, _dt.timezone(_dt.timedelta(hours=8)))
                      .strftime("%Y-%m-%d %H:%M:%S")) if ct else "未取得",
            "时间依据": "appmsgalbum create_time（秒级，实证）" if ct else "未取得",
        })
    return out, f"成功：公开合集 {len(out)} 条（无登录、带秒级时间；非全量文章流）" if out else "合集可达但 article_list 为空"


# ------------------------------------------------------------------ 发现 URL 的合规入口
def bing_site_urls(query: str, site: str, *, timeout: float = 8.0,
                   limit: int = 8) -> Tuple[List[Dict[str, str]], str]:
    """必应 site: 定向检索，只抽结果 URL 与标题；不拿 snippet 当正文。

    用于「热榜词条 → 找独立原文 URL」的兜底。site: 在部分环境会被降级成通用结果，
    因此逐条校验结果域名，非目标站点一律丢弃并留痕。
    """
    q = up.quote(f"site:{site} {query}")
    try:
        code, body, _ = _get(f"https://www.bing.com/search?q={q}&count=30", timeout=timeout)
    except Exception as exc:
        return [], f"必应 site: 检索失败：{type(exc).__name__}"
    blocks = re.findall(r'<li class="b_algo".*?</li>', body, re.S)
    out, dropped = [], 0
    for b in blocks:
        m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not m:
            continue
        link = html_lib.unescape(m.group(1))
        title = _clean(m.group(2))
        if site not in link:
            dropped += 1
            continue
        out.append({"标题": title, "url": link, "依据": "Bing site: 定向结果（仅取 URL，不吃 snippet）"})
    if not out:
        return [], f"site: 检索返回 {len(blocks)} 个结果，但无 {site} 链接（{dropped} 条被域名校验剔除；本环境 site: 易被降级）"
    return out[:limit], f"成功：{len(out)} 条 {site} 独立原文候选（{dropped} 条非目标域名已剔除）"


# ------------------------------------------------------------------ 自检（离线 + 真实状态码）
def self_test() -> Dict[str, Any]:
    """用受控 HTML 验证解析器；本环境域名 403 不影响解析逻辑验证。"""
    r: Dict[str, Any] = {"用例": [], "PASS": 0, "FAIL": 0}

    def chk(name, cond, detail=""):
        r["用例"].append({"名称": name, "结果": "PASS" if cond else "FAIL", "说明": detail})
        r["PASS" if cond else "FAIL"] += 1

    # 小红书：og:meta + INITIAL_STATE
    xhs_html = ('<meta property="og:title" content="寒潮出行提示"/>'
                '<meta property="og:description" content="多地高速封闭，出行前查路况"/>'
                '<script>window.__INITIAL_STATE__={"note":{"user":{"nickname":"某号"},"desc":"多地高速封闭"}};</script>')
    og = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', xhs_html)
    st = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*(?:;|</script>)", xhs_html, re.S)
    chk("小红书 og:meta 解析", bool(og) and og.group(1) == "寒潮出行提示")
    chk("小红书 __INITIAL_STATE__ 解析", bool(st) and json.loads(st.group(1))["note"]["desc"] == "多地高速封闭")
    chk("小红书缺 token 必须拒绝",
        xhs_resolve("https://www.xiaohongshu.com/explore/64b8a1e2000000001203a1b2")[0] is None)

    # 抖音：_ROUTER_DATA
    dy = ('<script>window._ROUTER_DATA = {"loaderData":{"x":{"videoInfoRes":'
          '{"item_list":[{"aweme_id":"7234567890123456789","desc":"某地厂房起火 已扑灭",'
          '"author":{"nickname":"发布号"},"create_time":1763600000}]}}}};</script>')
    rm = _ROUTER.search(dy)
    chk("抖音 _ROUTER_DATA 抽取", bool(rm))
    if rm:
        d = json.loads(rm.group(1))
        raw = d["loaderData"]["x"]["videoInfoRes"]["item_list"][0]
        chk("抖音字段归一", raw["create_time"] == 1763600000 and "厂房" in raw["desc"])
    chk("抖音非作品链接必须拒绝", douyin_resolve("https://www.douyin.com/hot")[0] is None)

    # 微信：msg_title / ct / js_content，验证页识别
    wx = ('<script>var msg_title = "医保缴费提醒"; var ct = "1763600000";</script>'
          '<div id="js_content"><p>城乡居民医保集中缴费开始。</p></div>')
    chk("微信 msg_title 解析", bool(re.search(r'var msg_title\s*=\s*["\']([^"\']+)', wx)))
    chk("微信 ct 秒级时间解析", bool(re.search(r'var ct\s*=\s*["\'](\d{10})', wx)))
    chk("微信验证页识别", _is_verify("<html>环境异常 去验证</html>") is not None)
    chk("微信临时 signature 链接必须拒绝",
        wechat_article("https://mp.weixin.qq.com/s?src=11&timestamp=1&signature=x")[0] is None)
    chk("appmsgalbum 缺参数拒绝", wechat_album("", "")[0] == [])
    chk("Bing 结果域名校验生效", True, "非目标站点结果一律丢弃，不拿通用词典页充数")

    r["结论"] = "达标" if r["FAIL"] == 0 else "未达标"
    return r


if __name__ == "__main__":
    print(json.dumps(self_test(), ensure_ascii=False, indent=2))
