"""清洗层：原始 dict → Item；去重、窗口过滤、契约拒绝均留痕，不静默丢条。

文档 §4/§5/§10：候选池只去「标题+渠道+URL 全同」的完全重复，不按事件主键去重；
摘要 2 段、LF、每段 2 个全角空格、净字数 ≤100、整句收束不切半句，数字与事实不改。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .model import Item, nav_dump_reason, net_len, valid_sources

TZ = timezone(timedelta(hours=8))
SPLIT = re.compile(r"(?<=[。！？；])")
ANY_SENT = re.compile(r"[。！？；]")


def enrich_bodies(raw: list[dict], client, *, limit: int = 120,
                  timeout: float = 6, budget: float = 60) -> tuple[list[dict], int]:
    """RSS / JSONP 只给短导语时回查独立原文（文档 §4 / §2.5）。

    旧版串行且只补 12 条，CCTV/中新网大量「一句导语」条目因此被摘要契约拒收。
    改为并发 8 线程 + 整批 60s 硬预算 + 上限可配：单篇失败隔离、不编造，
    拿不到就保持原样由 to_item 如实留痕；发布时间只补缺失项，不覆盖 feed pubDate。
    """
    try:
        import trafilatura
    except ImportError:
        return raw, 0
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _one(e: dict) -> bool:
        try:
            a = client.get(e["url"], timeout=timeout)
            a.raise_for_status()
            doc = trafilatura.bare_extraction(a.text)
            d = doc if isinstance(doc, dict) else (
                doc.as_dict() if doc is not None and hasattr(doc, "as_dict") else {})
            text = (d.get("text") or "").strip()
            if len(text) < 60 or nav_dump_reason(text[:600]):
                return False
            e["body"] = text[:1500]
            if not e.get("published") and d.get("date"):
                e["published"] = d["date"]
            return True
        except Exception:
            return False

    todo = []
    for e in raw:
        if not isinstance(e, dict) or not e.get("url") or e.get("shape") == "结构化":
            continue
        body = str(e.get("body") or "")
        if len(body) >= 60 and ANY_SENT.search(body):
            continue
        todo.append(e)
        if len(todo) >= limit:
            break
    done = 0
    pool = ThreadPoolExecutor(max_workers=8)
    try:
        futs = {pool.submit(_one, e): e for e in todo}
        try:
            for f in as_completed(futs, timeout=budget):
                try:
                    done += bool(f.result())
                except Exception:
                    pass
        except TimeoutError:                 # 整批预算到点，未完成的不再等待
            for f in futs:
                f.cancel()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return raw, done


def _closed(s: str) -> bool:
    """拒绝未闭合书名号/引号/括号的半句（用户表里「据沙特《」就是这么漏进来的）。"""
    pairs = (("《", "》"), ("「", "」"), ("“", "”"), ("‘", "’"),
             ("（", "）"), ("(", ")"), ("【", "】"), ("[", "]"))
    return all(s.count(a) == s.count(b) for a, b in pairs)


def _clauses(long_sent: str) -> list[str]:
    """长整句按逗号分出并列分句：中文逗号分隔的并列分句语义相对完整，
    补句号收束后仍是整句，不切书名号/引号内部，数字一个不改。"""
    out = []
    for part in re.split(r"[，,]", long_sent):
        p = part.strip()
        if net_len(p) >= 10 and _closed(p):
            out.append(p.rstrip("。！？；;,") + "。")
    return out


def make_abstract(title: str, body: str, limit: int = 100) -> str | None:
    """抽取式：两段都必须是正文里的事实整句，绝不复读标题、绝不切半句。

    旧实现在只有一句有效正文时把「标题 + 。」补成第二段，产出
    `日本熊本县发生4.1级地震 多地有震感。` / `黄金跌跌不休，何时反弹？。`
    这类复读；本版改为：短整句优先 → 不足时从长句按逗号取并列分句 →
    仍不足两句就返回 None（契约拦截留痕），宁可少一条也不出复读稿。
    """
    txt = re.sub(r"[\r\t ]+", "", (body or "").strip())
    if not txt or nav_dump_reason(txt):
        return None
    sents = [s.strip() for s in SPLIT.split(txt)
             if s.strip() and net_len(s) >= 10 and _closed(s)]
    if not sents:
        return None

    def norm(s: str) -> str:
        s = s.rstrip()
        if s.endswith(("。", "！", "？")):
            return s
        if s.endswith(("；", ";")):
            return s[:-1] + "。"          # 分号收尾的第二段一律改成整句
        return s + "。"

    pool = [norm(s) for s in sents if net_len(s) <= 60]
    # 长整句拆并列分句补池（只在短句不够时启用）
    if len(pool) < 2:
        for s in sents:
            if net_len(s) > 60:
                pool.extend(norm(c) for c in _clauses(s) if net_len(c) <= 60)
    # 去重且不允许与标题重复（去标点后比对）
    t_norm = re.sub(r"[^\w\u4e00-\u9fff]", "", title or "")
    uniq: list[str] = []
    for s in pool:
        if re.sub(r"[^\w\u4e00-\u9fff]", "", s) == t_norm:
            continue
        if s not in uniq:
            uniq.append(s)
    if len(uniq) < 2:
        return None
    first = uniq[0]
    second = next((s for s in uniq[1:] if net_len(first) + net_len(s) <= limit), None)
    if second is None:
        return None
    a = "\u3000\u3000" + first + "\n\u3000\u3000" + second
    return a if net_len(a) <= limit else None


# 轻量繁→简映射（覆盖港澳台媒体高频字，不引入额外依赖）。
# 显式字典：避免相邻字符串字面量被 Python 隐式拼接、也避免两串长度不齐错位。
_PAIRS = [
    ("選舉會實踐落實愛國者治港優越點新聞報道委履職盡責讓發展成果更好惠及市民共同開創由及興新局面期望屆發達國家臺灣島內週後續",
     "选举会实践落实爱国者治港优越点新闻报道委履职尽责让发展成果更好惠及市民共同开创由及兴新局面期望届发达国家台湾岛内周后续"),
    ("顯別組順舉體標導參關註員長門題萬與為產業資訊網絡數據風險責發視頻點擊閉於這個來時說對種經濟結構變動將無獨統壹陸華僑歸橋樑寶貴財連",
     "显别组顺举体标导参关注员长门题万与为产业资讯网络数据风险责发视频点击闭于这个来时说对种经济结构变动将无独统一陆华侨归桥梁宝贵财连"),
    ("縣鄉鎮廳處級預計測評審議覆蓋範圍醫療衛生環境保護違規處罰調查報告聲明回應澄清誤傳謠言網傳證實否認確認發布通報",
     "县乡镇厅处级预计测评审议覆盖范围医疗卫生环境保护违规处罚调查报告声明回应澄清误传谣言网传证实否认确认发布通报"),
]
TRAD_MAP: dict[str, str] = {}
for _a, _b in _PAIRS:
    assert len(_a) == len(_b), f"繁简表长度不齐：{len(_a)} != {len(_b)}"
    TRAD_MAP.update(dict(zip(_a, _b)))
_SIMP_TABLE = str.maketrans(TRAD_MAP)


def simplify(s: str) -> str:
    """繁体稿转简体，避免与简体主表混排；不改动数字与事实。"""
    return (s or "").translate(_SIMP_TABLE)


def simplify_raw(raw: list[dict]) -> list[dict]:
    """仅 live 链路调用：采集层字段繁→简，离线回放逐格不变。"""
    for e in raw:
        if isinstance(e, dict):
            for k in ("title", "body", "summary", "abstract"):
                if isinstance(e.get(k), str):
                    e[k] = simplify(e[k])
    return raw


# 同一主媒体下的多个频道（中新网滚动/社会/财经 RSS）只能算 1 个独立信源。
# 长名在前，避免短名先命中；canon 同时供 rules.channels_of 使用（单一真值源）。
PLATFORM_HEADS = {"微博", "百度", "抖音", "今日头条", "知乎", "B站", "快手",
                  "小红书", "贴吧", "微信", "央视网", "央视新闻"}

MEDIA_CANON = ("每天60s看世界 每天3分钟读懂世界 中国互联网联合辟谣平台 国家预警信息发布中心 "
               "中国地震台网 中国地方政府债券信息公开平台 中央气象台 人民法院公告网 "
               "国务院政策文件库 国家药监局 国家医保局 中国政府网 中国新闻网 中新网 中新经纬 "
               "新华财经 新华网 新华社 人民网 央视网 央视新闻 央广网 中国网 光明网 "
               "经济日报 中国青年报 工人日报 科技日报 法治日报 参考消息 环球网 "
               "财联社 澎湃新闻 界面新闻 第一财经 每日经济新闻 证券时报 中国证券报 "
               "上海证券报 证券日报 北京商报 金融界 上观新闻 四川观察 北京日报 "
               "中国气象局 中国气象网 水利部黄河水利委员会 宁波海事局 军队采购网 "
               "深交所公告 上交所公告 巨潮资讯网 港交所披露易 中国人民银行 深交所 "
               "百度 微博 抖音 今日头条 知乎 微信 B站 快手 小红书 贴吧 豆瓣 虎扑 "
               "USGS GDACS WHO OCHA GDELT HotFlashNews 豚榜 锐榜 查榜网 云合数据").split()


# 用户点名的 9 个主干平台：精确归一，不被更短的上位词吞掉
# （「百度贴吧」不能被「百度」吞成百度热搜，「微信搜一搜」不能被吞成微信）。
EXACT_PLATFORM = {
    "微博热搜": "微博", "百度热搜": "百度", "抖音热榜": "抖音",
    "今日头条": "今日头条", "知乎热榜": "知乎", "小红书": "小红书",
    "快手": "快手", "百度贴吧": "贴吧", "贴吧": "贴吧",
    "微信搜一搜·搜索发现": "微信搜一搜", "微信搜一搜": "微信搜一搜",
    "B站热门": "B站", "微信24h热文榜": "微信24h热文榜",
}


def canon(c: str) -> str:
    """把频道名 / "RSS" 尾巴 / 平台内部层级归一为可核对的平台或媒体名。"""
    c = (c or "").strip()
    # 清单台账编号是登记用的，不是信源名：「工信部（清单§3）」→「工信部」。
    c = re.sub(r"[（(]?\s*清单\s*§\s*\d+\s*[)）]?", "", c).strip()
    if c in EXACT_PLATFORM:
        return EXACT_PLATFORM[c]
    # 平台内部层级用不带空格的 `·`，必须原样保留（01 §6）。
    if "·" in c and not c.startswith("http"):
        head, _, tail = c.partition("·")
        h, t = head.strip(), tail.strip()
# 央视网频道/JSONP 是台账名，归一为 1 家，不虚增独立信源。
        if h in ("央视网", "央视新闻") and any(
                k in t for k in ("要闻", "国内", "国际", "法治", "社会", "健康",
                                 "科技", "文娱", "政策", "JSONP", "滚动", "新闻")):
            return "央视网"
        # 平台保留内部层级；媒体频道（中新网·滚动）归一到主媒体，只算 1 家信源。
        if h in PLATFORM_HEADS:
            return canon(h) + "·" + t
        return canon(h)
    if "中国新闻网" in c or "中新网" in c:
        return "中新网"          # 同一家媒体，不因写法不同虚增独立信源
    for m in MEDIA_CANON:
        if c.startswith(m) or m in c:
            return m
    return re.sub(r"\s*RSS\s*$", "", c, flags=re.I).strip() or c


def normalize_source(s: str) -> str:
    """`渠道 · 渠道`；同一媒体多频道归一去重，剔除链接/编号/占位/等N家。"""
    s = re.sub(r"https?://\S+", "", (s or "").strip())
    parts = [p.strip() for p in s.split(" · ") if p.strip()]
    parts = [re.sub(r"^\d+、", "", p) for p in parts]
    parts = [re.sub(r"[（(]?\s*清单\s*§\s*\d+\s*[)）]?", "", p).strip() for p in parts]
    parts = [p for p in parts if not re.search(r"\|\||｜|等\d+家|未取得|待补|N/A", p)]
    out, seen = [], set()
    for p in parts:
        n = canon(p)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    if not out:
        raise ValueError("来源为空或全为占位")
    return " · ".join(out)


def parse_pub(s: str) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    # Unix 秒 / 毫秒（部分 JSONP、榜单接口直接给 focus_date / timestamp）
    if s.isdigit():
        v = int(s)
        if v > 10 ** 11:
            v //= 1000
        try:
            return datetime.fromtimestamp(v, TZ)
        except (ValueError, OSError, OverflowError):
            return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=TZ) if "%z" not in fmt \
                else datetime.strptime(s, fmt).astimezone(TZ)
        except ValueError:
            continue
    return None


# 半截导语/悬空句式拒收，避免「记者今天从…获悉。」式摘要。
_INCOMPLETE = re.compile(
    r"(?:获悉|介绍|指出|表示|显示|看|情况看|接收的.{0,20}情况看)\s*[。；]$"
    r"|定于.{0,24}\d+时\s*[。；]$"
    r"|将于.{0,24}(?:召开|举行|发射)\s*[。；]$")


def incomplete_reason(a: str) -> str:
    """摘要语义完整性：每段须是给出事实的整句，不是悬空导语。"""
    for p in (a or "").split("\n"):
        body = p.strip()
        n = net_len(body)
        if n < 12:
            return "段落净长 <12 字，疑似半截导语（未给出事实）"
        if _INCOMPLETE.search(body):
            return "半截句收束（获悉/介绍/看/定于…时），未给出事实，不作独立原文"
        if body.endswith("？") and n < 24:
            return "第二段为疑问句且无事实陈述，不作独立原文"
    return ""


def to_item(e: dict, *, lim: int = 100) -> tuple[Item | None, str]:
    title = re.sub(r"[\r\n\t]+", "", str(e.get("title") or "")).strip()
    abstract = e.get("abstract") or make_abstract(title, e.get("body") or e.get("summary") or "")
    if abstract is None:
        return None, "摘要未取得（无有效整句正文，不用标题/snippet 冒充）"
    inc = incomplete_reason(abstract)
    if inc:
        return None, f"契约拒绝：{inc}"
    try:
        sources = normalize_source(e.get("sources") or e.get("source") or "")
    except ValueError as ex:
        return None, str(ex)
    try:
        return Item(title=title, abstract=abstract, sources=sources,
                    group=str(e.get("group") or e.get("group_name") or "民生服务"),
                    tag=str(e.get("tag") or e.get("group") or "综合"),
                    record_type=str(e.get("record_type") or "联网检索独立原文"),
                    url=str(e.get("url") or ""), published=str(e.get("published") or ""),
                    time_basis=str(e.get("time_basis") or ""),
                    evidence=str(e.get("evidence") or "A"),
                    shape=str(e.get("shape") or "独立原文"),
                    event_key=str(e.get("event_key") or "")), ""
    except ValueError as ex:
        return None, f"契约拒绝：{ex}"


def normalize_all(raw: list[dict], *, window: tuple[datetime, datetime] | None = None
                  ) -> tuple[list[Item], list[dict]]:
    """返回 (候选池, 留痕)。采集层只去完全重复；超窗口逐条留痕不入池。"""
    items, traces, seen = [], [], set()
    seen_abs: dict[str, str] = {}   # 摘要复用闸：同一摘要只允许出现一次
    for e in raw:
        key = (str(e.get("title") or "").strip(), str(e.get("source") or e.get("sources") or ""),
               str(e.get("url") or ""))
        if key in seen:
            traces.append({"类": "采集层完全重复", "标题": key[0][:60],
                           "依据": "标题+渠道+URL 全同", "URL": key[2],
                           "发布时间": str(e.get("published") or "")})
            continue
        seen.add(key)
        it, why = to_item(e)
        if it is not None and it.abstract in seen_abs:
            reason = (f"摘要复用：与「{seen_abs[it.abstract][:24]}」逐字相同，"
                      "不是该稿独立原文（不用同一段导语补两条不同标题）")
            traces.append({"类": "契约拦截", "标题": it.title[:60],
                           "依据": reason, "URL": it.url, "发布时间": it.published})
            it, why = None, reason
        if it is None:
            traces.append({"类": "契约拦截", "标题": str(e.get("title") or "")[:60],
                           "依据": why, "URL": str(e.get("url") or ""),
                           "发布时间": str(e.get("published") or "")})
            continue
        stale = bool(e.get("stale"))
        if window:
            pub = parse_pub(it.published) if it.published else None
            # 缓存回吐行若无发布时间，不得标「时点推定」混进当批；只留痕。
            if stale and pub is None:
                traces.append({"类": "旧缓存无时间证据", "标题": it.title[:60],
                               "依据": "本轮端点失败，回吐条目无可核对发布时间，不进候选池",
                               "URL": it.url, "发布时间": ""})
                continue
            if pub is None:
                traces.append({"类": "时间依据缺失", "标题": it.title[:60],
                               "依据": (f"published={it.published or '空'} 不可解析为可核对时刻；"
                                       "只采集时间窗口内数据，无时间证据不进候选池"),
                               "URL": it.url, "发布时间": it.published})
                continue
            if not (window[0] < pub <= window[1]):
                traces.append({"类": "超窗口", "标题": it.title[:60],
                               "依据": (f"发布时间 {pub:%Y-%m-%d %H:%M} 不在窗口 "
                                       f"({window[0]:%Y-%m-%d %H:%M}, "
                                       f"{window[1]:%Y-%m-%d %H:%M}] 内"),
                               "URL": it.url, "发布时间": it.published})
                continue
        if stale:
            it.evidence = "B"          # 本轮未取得独立原文，降证据等级，不冒充 A
        seen_abs.setdefault(it.abstract, it.title)
        items.append(it)
    for i, it in enumerate(items, 1):
        it.seq = i
    return items, traces

# 社交词条回查独立原文（§4.2/§8）：命中才挂发现渠道。
BRIDGE = {
    "用万能旅行拍照姿势美美出片": ("落叶缓扫", "初冬出行打卡 → 落叶缓扫"),
    "定格这一刻的日照金山": ("落叶缓扫", "初冬出行打卡 → 落叶缓扫"),
    "我拍到了海鸥雨": ("落叶缓扫", "初冬出行打卡 → 落叶缓扫"),
    "你可以永远相信赛里木湖的美景": ("落叶缓扫", "初冬出行打卡 → 落叶缓扫"),
    "耗时三年拍下古诗词里的中国": ("落叶缓扫", "初冬出行打卡 → 落叶缓扫"),
    "原来古诗词里的河南真的存在": ("落叶缓扫", "初冬出行打卡 → 落叶缓扫"),
    "在B站就能玩到的像素射击游戏 点击即玩 B站AI创造公开赛":
        ("B站AI创造公开赛", "B站创作赛事词条 → 官方赛事稿"),
# 抖音词条按共享实词桥接，留痕「同一议题不同侧面」。
    "神舟二十四号发射准备就绪": ("神舟二十四号", "神舟任务词条 → 乘组确定独立稿"),
    "终于懂李白走到哪都写诗了": ("落叶缓扫", "初冬文旅词条 → 城市景观独立稿"),
}

_ZH = re.compile(r"[\u4e00-\u9fff]")
_ANCHOR = re.compile(r"\d+(?:\.\d+)?\s*(?:级地震|级|万人|亿元|万元|%|元|人死亡|人受伤)")


def _bigrams(s: str) -> set[str]:
    t = re.sub(r"[^\u4e00-\u9fff0-9]", "", s or "")
    return {t[i:i + 2] for i in range(len(t) - 1)
            if _ZH.match(t[i]) and _ZH.match(t[i + 1])}


def _anchor_ok(topic: str, title: str) -> bool:
    """词条里的硬数字锚点必须在独立原文标题里能核对，否则不挂接。

    防「德令哈 5.6 级」词条挂到「秘鲁 6.7 级」稿件这类错配。
    """
    for a in _ANCHOR.findall(topic):
        if a.replace(" ", "") not in (title or "").replace(" ", ""):
            return False
    return True


def attach_discovery(items: list[Item], topics: list[dict]
                     ) -> tuple[list[dict], list[dict]]:
    """热榜词条逐条回查本批独立原文；命中才把发现渠道并入来源列。

    - 只挂到**已有真实独立原文**的条目，不把话题名扩写成事件（§12.7）。
    - 每个词条最多挂 1 条稿件，不虚增多渠道覆盖；同一稿件可被多平台命中
      （真实多渠道），来源列去重后写成 `发现渠道 · 真实媒体 · …`。
    - 回查不到的词条返回 unmatched，由调用方写进附录 15，不编造正文。
    """
    matched, unmatched = [], []
    for tp in topics:
        name = str(tp.get("title") or "").strip()
        plat = str(tp.get("discovery") or tp.get("source") or "").strip()
        if not name or not plat:
            continue
        bg = _bigrams(name)
        if len(bg) < 2:
            unmatched.append({"标题": name[:60], "平台": plat,
                              "处置": "词条过短无可核对实体，进附录 15，不扩写"})
            continue
        best, best_score = None, 0.0
        for it in items:
            ib = _bigrams(it.title)
            inter = len(bg & ib)
            if inter < 2 or not _anchor_ok(name, it.title):
                continue
            jac = inter / max(1, min(len(bg), len(ib)))
            score = inter * (1.0 + jac)
            if score > best_score and jac >= 0.20:
                best, best_score = it, score
        bridge_hit = ""
        if best is None and name in BRIDGE:
            key, label = BRIDGE[name]
            for it2 in items:
                if key in it2.title:
                    best, bridge_hit = it2, (f"主题桥接：{label}"
                                             "（同一议题不同侧面，非事件同一，"
                                             "不虚增 cross_score）")
                    break
        if best is None:
            unmatched.append({"标题": name[:60], "平台": plat,
                              "处置": "未回查到本批独立原文，进附录 15，不编造事件正文"})
            continue
        parts = [x.strip() for x in best.sources.split(" · ") if x.strip()]
        # 一个平台在同一稿上只占一位：normalize_source 内部 canon 去重 + 归一，
        # 多个词条命中同一平台不会把渠道名重复写进来源列（§8）。
        if canon(plat) not in {canon(x) for x in parts}:
            try:
                new_src = normalize_source(" · ".join([plat] + parts))
                best.sources = new_src
                best.record_type = "社交词条回查成稿"
                best.n_channels = len(new_src.split(" · "))
            except ValueError:
                pass
        matched.append({"词条": name[:60], "发现渠道": plat,
                        "回查到独立原文": best.title[:60],
                        "并入后来源": best.sources,
                        "匹配依据": bridge_hit or f"共享中文 2-gram，score={best_score:.2f}"})
    return matched, unmatched

