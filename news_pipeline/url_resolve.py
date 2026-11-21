"""检索结果的 URL 还原与中文相关性判定（纯代码、无模型、无 Key）。

2026-11-22 线上 latest.xlsx 的工程留痕 H-c 逐次留痕暴露：
  * Bing HTML 104 次调用 0 成稿，候选 URL 全是
    ``https://www.bing.com/ck/a?!&&p=...``（Bing 点击追踪重定向）与
    ``bing.com/DefaultSearchEngine`` / ``/version`` / ``/RelatedSearch``；
    硬闸把它们全拦了是对的，但根因是**没把 ck/a 还原成真实原文 URL**。
  * Google News RSS 在 Actions 上真实返回了
    ``news.google.com/rss/articles/CBMi...``，却被判「候选不合格」；
    且 keyless_compose.gnews() 把 domain 算成 ``news.google.com``，
    search_clue 按 domain 去重时**所有 gnews 条目被压成 1 个**，
    min_independent_domains=2 永远达不到 → gnews 通道恒 0 成稿。
  * gnews / bing_rss 对中文查询返回了 crazygames.com（小游戏）、
    texttwist.info、fanbolt.com（电影日历）、kancare.ks.gov（美国州医保）
    这类与 query 零交集的页面，白占 max_redirects 名额。

本模块只做三件事，不碰评分、不改数字、不生成内容：
  1. Bing ``ck/a`` 的 ``u=a1<base64url>`` 解码还原；
  2. Google News ``/rss/articles/CBMi...`` 的 base64 备用还原（主路径仍是跟随重定向）；
  3. 中文 query ↔ 标题的相关性硬闸，挡掉英文噪声页。
"""
from __future__ import annotations

import base64
import re
import urllib.parse
from typing import Iterable

# Bing 自己的页面 / 点击追踪壳，永远不是独立原文
BING_SELF = re.compile(
    r"^https?://(?:www\.)?bing\.com/(?:ck/a|DefaultSearchEngine|browserextension|"
    r"version|RelatedSearch|search\?|images|videos|maps|news|translator|"
    r"shop|account|profile|rewards|homepage)", re.I)
BING_CK = re.compile(r"[?&]u=a1([A-Za-z0-9_\-]+)", re.I)
GNEWS_ARTICLE = re.compile(r"news\.google\.com/rss/articles/([A-Za-z0-9_\-]+)", re.I)

# 与中文民生资讯查询毫无关系的典型噪声域名（线上实测，不是猜测）
OFFTOPIC_DOMAIN = re.compile(
    r"crazygames|texttwist|fanbolt|kancare|kmap-state|addictinggames|"
    r"poki\.com|miniclip|armorgames|y8\.com|game|movie|release-calendar|"
    r"imdb|rottentomatoes|fandom|pinterest|quizlet|chegg|"
    r"\.us/provider|/ProviderEnrollment", re.I)

_CJK = re.compile(r"[\u4e00-\u9fff]")


def _b64url_decode(s: str) -> str:
    try:
        raw = s + "=" * (-len(s) % 4)
        return base64.urlsafe_b64decode(raw).decode("utf-8", "ignore")
    except Exception:
        return ""


def decode_bing_ck(url: str) -> str:
    """``bing.com/ck/a?...&u=a1aHR0cHM6...`` → ``https://真实原文``。

    实测（2026-11-22，线上 H-c 的 ``news.qq.com/rain/a/20261120A045KA00``
    格式）：``u=a1`` 前缀后的 base64url 就是目标 URL。解不出返回原串，
    由调用方按「非独立原文」拦下，不静默放行。
    """
    if not url or "bing.com/ck/a" not in url:
        return url
    m = BING_CK.search(url)
    if not m:
        return url
    real = _b64url_decode(m.group(1))
    return real if real.startswith(("http://", "https://")) else url


def is_bing_shell(url: str) -> bool:
    """Bing 自有页面 / 未还原的追踪壳，不能当独立原文。"""
    return bool(BING_SELF.search(url or ""))


def resolve_search_url(url: str) -> str:
    """统一入口：先还原 Bing ck/a，其余原样返回。"""
    u = decode_bing_ck(url or "")
    return "" if is_bing_shell(u) else u


def decode_gnews_article(url: str) -> str:
    """Google News RSS ``/rss/articles/CBMi...`` 的 base64 **备用**还原。

    2024 起 Google 改了编码，主流路径是跟随 302（keyless_compose.resolve_redirect）。
    这里只处理老格式 ``CBMi`` 开头、能解出明文 http(s) 的情况；解不出返回 ""，
    绝不猜 URL，交由跟随重定向或如实标「未取得」。
    """
    m = GNEWS_ARTICLE.search(url or "")
    if not m:
        return ""
    s = m.group(1)
    # 老格式是标准 base64（含 +/），新格式是 base64url 且内嵌 protobuf，不强解
    for candidate in (s, s + "=" * (-len(s) % 4)):
        try:
            raw = base64.b64decode(candidate, validate=False)
            hit = re.search(rb"https?://[\x20-\x7e]{8,}", raw)
            if hit:
                u = hit.group(0).decode("ascii", "ignore")
                # protobuf 尾部常带控制字符，截到第一个非 URL 字节
                u = re.split(r"[\x00-\x1f\x7f]", u)[0]
                if u.startswith(("http://", "https://")):
                    return u
        except Exception:
            continue
        try:
            raw2 = base64.urlsafe_b64decode(candidate)
            hit2 = re.search(rb"https?://[\x20-\x7e]{8,}", raw2)
            if hit2:
                u = re.split(r"[\x00-\x1f\x7f]", hit2.group(0).decode("ascii", "ignore"))[0]
                if u.startswith(("http://", "https://")):
                    return u
        except Exception:
            continue
    return ""


def cjk_bigrams(text: str) -> set:
    t = _CJK.sub(lambda m: m.group(0), str(text or ""))
    # 去掉日期数字与标点，只留中文 2-gram
    t = re.sub(r"[^一-鿿]", "", t)
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) >= 2 else set()


# 权威中文新闻源：端点时间流 / 正规媒体，标题换说法也放行（贴合度交给下游成稿）
TRUSTED_NEWS_DOMAIN = re.compile(
    r"(?:^|\.)(?:cctv|chinanews|people|xinhuanet|news\.cn|ce\.cn|gmw\.cn|cyol|"
    r"thepaper|cnr|gov\.cn|12306|caac|weather\.com\.cn|cnstock|stcn|cs\.com\.cn|"
    r"21jingji|yicai|eeo|jiemian|caixin|sohu|sina|qq\.com|163\.com|ifeng|hexun|"
    r"eastmoney|jrj|新华|人民|央视|中新)", re.I)
# 明确不是新闻的形态：百科 / 问答 / 经验 / 技术博客 / 影视 / 软件商店 / 电商
NON_NEWS_DOMAIN = re.compile(
    r"baike\.baidu|zhidao\.baidu|jingyan\.baidu|wenku\.baidu|"
    r"zhihu\.com/(?:question|column)|zhuanlan\.zhihu|"
    r"blog\.csdn|cnblogs\.com|jianshu|segmentfault|"
    r"microsoftstore|iqiyi|youku|wikipedia|douban\.com/subject|"
    r"taobao|tmall|jd\.com|pinduoduo|steam|crazygames", re.I)
NON_NEWS_TITLE = re.compile(
    r"百度百科|维基百科|自然数之一|怎么(?:看|办|样|设置|开启|下载)|如何|"
    r"教程|安装|下载|系统安装|Windows\s*11|Win11|官网|最新版|亲测|"
    r"免费观看|直播|游戏平台|邮箱|电视剧|综艺|试题|答案|范文|模板|"
    r"申请书怎么写|大全|排行榜|选购|测评", re.I)
# 新闻体裁词：换说法的报道若与 query 共享数字锚点，可宽恕放行
NEWS_HINT = re.compile(
    r"发生|发布|通报|提醒|表示|预计|启动|致|遇难|伤亡|地震|预警|"
    r"出炉|维持|上调|下调|辟谣|回应|公布|实施|落地|开工|开通", re.I)


def _num_anchors(s: str) -> set:
    """提取 ≥3 位数字或带小数点的数字锚点（排除泛化的月份 11/12）。"""
    return {m for m in re.findall(r"\d+(?:\.\d+)?", str(s or ""))
            if len(m) >= 3 or "." in m}


def is_offtopic(title: str, query: str, url: str = "") -> tuple[bool, str]:
    """中文 query ↔ 标题的相关性硬闸，挡掉检索召回的噪声页（不用于打分/成稿）。

    2026-11-22 真发请求复现：Bing RSS 对「德令哈5.6级地震」返回的全是 NBA 直播，
    对「11月LPR」返回 Windows 11 / 百度百科 11 / 爱奇艺《11》，对「医保91种药品」
    返回 163 邮箱问答——都是把查询里的数字当关键词误匹配。旧版为防误杀改成
    「中文标题一律保留」，结果这些垃圾被当成独立原文，形成 5 个独立域名的假
    多渠道合并并抬到 A 级（线上主表序 2 / 序 3 / 序 17 即由此而来）。

    现在用三层确定性判据，不靠一刀切放行：
      ① 权威新闻域名 → 放行（标题换说法不影响）；
      ② 百科/问答/教程/影视/电商等形态 → 拦；
      ③ 其余中文标题 → 至少 1 个共享中文 2-gram，或「新闻体裁词 + 共享数字锚点」→ 放行。
    """
    t, q = str(title or ""), str(query or "")
    if OFFTOPIC_DOMAIN.search(url or ""):
        return True, "命中已实测的噪声域名（游戏 / 电影 / 境外机构页）"
    if NON_NEWS_DOMAIN.search(url or ""):
        return True, "非新闻形态域名（百科 / 问答 / 教程 / 影视 / 电商）"
    tb, qb = cjk_bigrams(t), cjk_bigrams(q)
    if not tb:
        return True, "标题无中文 2-gram（英文 / 纯符号页）"
    if TRUSTED_NEWS_DOMAIN.search(url or ""):
        return False, ""
    if NON_NEWS_TITLE.search(t):
        return True, "标题命中非新闻形态词（教程 / 百科 / 安装 / 直播 / 问答）"
    shared = len(tb & qb)
    if shared >= 1:
        return False, ""
    anchors = _num_anchors(t) & _num_anchors(q)
    if anchors and NEWS_HINT.search(t):
        return False, ""
    return True, f"与线索零中文实词交集（共享 2-gram={shared}，数字锚点={sorted(anchors)}）"


def dedup_key(ev_url: str, ev_domain: str) -> str:
    """gnews 还原前的**临时去重键**：用 link 本身，避免多条 CBMi... 被压成 1 个。

    keyless_compose.search_clue 旧版按 ev.domain 去重，而 gnews 的 domain
    恒为 news.google.com → 所有条目塌缩成 1 条 → min_independent_domains=2
    数学上不可达。调用方改用本函数作第一道去重键，还原后再按真实域名二次去重。
    """
    return (ev_url or "").strip() or (ev_domain or "")


def filter_offtopic_pairs(pairs: Iterable[tuple[str, str, str]]) -> tuple[list, list]:
    """批量过滤 (title, url, query) 三元组，返回 (保留, 被拦明细)。"""
    keep, blocked = [], []
    for title, url, query in pairs:
        bad, why = is_offtopic(title, query, url)
        if bad:
            blocked.append({"标题": str(title)[:60], "URL": str(url)[:120], "依据": why})
        else:
            keep.append((title, url, query))
    return keep, blocked
