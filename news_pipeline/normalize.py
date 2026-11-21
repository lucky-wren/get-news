"""字段规范化（渠道文档 §0.3 / §0.4）与摘要撰写规范。

第 9 列「内容摘要」是 12 列里唯一需要生成的展示字段。硬约束：
  - **用户 2026-11-20 补充口径（覆盖 SKILL §8 的「2–3 段 / 240 字」）**：
    摘要净字数（去掉缩进与换行后的可见字符）**不超过 100 字**，内容要**流畅、大概说清楚**；
    形态对齐目标样例：**两段**（抽到 ≥2 个整句时按句边界分段，每段都是整句且互不复读），
    只有一句时保持单段；绝不为了凑段数把同一句对半切。段数由 config.abstract.paragraphs 注入。
  - 仍是**抽取式纯代码**：从独立原文正文 / 有效原文摘要里按原文顺序挑整句，
    整句 / 整分句边界收束，绝不 `[:100]` 切半句，**数字一个不改**，不调用任何模型语义层。
  - 先清洗 HTML 实体 / 标签 / 零宽字符；RSS 常给「标题&nbsp; 来源 标题。」模板，
    这类复读**绝不冒充摘要**，明确标「摘要未取得」并在质量门禁里计数；
  - 段级 + 句级去重（目标态样例里 49/94 条把同一段正文复制了 2–4 次）；
  - 剔除记者署名 / 电头 / 相关阅读等噪声句，优先保留含数字、主体动作、结果影响的导语整句。
"""
from __future__ import annotations

import os
import re
from typing import List, Tuple

from .article import CODE_LINE, clean_html, compact, is_title_echo
from .model import RawItem
from .timewindow import resolve_pub

INDENT = "\u3000\u3000"
# 净字数上限；默认 100（用户拍板）。优先级：config.yaml abstract.max_net_chars
# > 环境变量 NEWS_ABSTRACT_MAX > 100。由 pipeline.apply_abstract_limit(cfg) 在跑批前注入。
MAX_LEN = int(os.environ.get("NEWS_ABSTRACT_MAX", "100"))
PARAGRAPHS = 2          # 目标样例形态；由 pipeline.apply_abstract_limit(cfg) 注入


def set_abstract_paragraphs(n: int | None) -> int:
    """设置本批次摘要段数（1=单段 / 2=两段，默认 2），返回生效值。"""
    global PARAGRAPHS
    if n is None:
        return PARAGRAPHS
    try:
        v = int(n)
        if v in (1, 2, 3):
            PARAGRAPHS = v
    except (TypeError, ValueError):
        pass
    return PARAGRAPHS


def set_abstract_limit(n: int | None) -> int:
    """设置本批次摘要净字数上限，返回生效值。None / 非法值忽略。

    注意：spec.py 的验收器固定按 100 字校验（规格契约，不随配置放宽）；
    把上限调大只会让生成端放宽、验收判不达标，这是刻意的防回退设计。
    """
    global MAX_LEN
    if n is None:
        return MAX_LEN
    try:
        v = int(n)
        if 20 <= v <= 1000:
            MAX_LEN = v
    except (TypeError, ValueError):
        pass
    return MAX_LEN
PLACEHOLDER = re.compile(r"详情以原文为准|具体内容以.*?为准|据标题概述|更多.*?详见原文|"
                         r"点击[查阅]*原文|本文根据搜索结果|按内部规则|接口未返回|详见报道|"
                         r"原标题|点击查看|阅读原文|责任编辑|编辑[:：]")
SENT_END = re.compile(r"(?<=[。！？；!?;])")
CLAUSE_END = "。！？；!?;"
CLAUSE_SPLIT = re.compile(r"(?<=[，,、；;：:])")
MISSING_TEXT = "摘要未取得：源站仅返回标题复读或未开放正文，已按口径留痕，不以标题冒充摘要。"
# 整行都是站点元数据时整行丢弃；只删关键词会留下「：某地发生事故」这种残缺片段
META_LINE = re.compile(
    r"^(?:原标题|点击查看|阅读原文|责任编辑|责编|编辑[:：]|记者\b|记者\s|通讯员|"
    r"来源[:：]|图片来源|版权声明|未经授权|如需转载|扫描|二维码|点击上方|分享到|"
    r"相关新闻|延伸阅读|更多报道|阅读全文|订阅|下载客户端|返回顶部)")

# 纯元数据 / 站点导航句：整句命中且很短时直接丢，避免占掉 100 字预算
NOISE_SENT = re.compile(
    r"记者|通讯员|编辑[:：]|责任编辑|责编|校对|审核[:：]|原标题|点击查看|阅读原文|"
    r"扫描|二维码|关注我们|点击上方|投稿|热线|版权声明|未经授权|如需转载|返回顶部|"
    r"分享到|图片来源|来源[:：]|相关新闻|延伸阅读|更多报道|阅读全文|订阅|下载客户端")
# 电头 / 括注里的来源署名：只清前缀，不整句丢（新闻导语常带「新华社北京11月19日电」）
ELECTRIC_HEAD = re.compile(
    r"^\s*(?:新华社|中新网|中央广播电视总台|央视新闻|央视网|人民网|本报|澎湃新闻|财联社|"
    r"经济日报|光明日报|中国新闻网)[^\s，,]{0,18}(?:电|讯|消息|报道)[，,\s]*")
PAREN_META = re.compile(r"（[^）]{0,50}(?:记者|编辑|来源|图片|摄)[^）]{0,50}）")
# 「大概说清楚」的可核对信号：数字 / 金额 / 人数 / 主体动作 / 结果影响
FACT = re.compile(
    r"\d|％|%|万|亿|元|人|起|次|死亡|受伤|伤亡|失联|发布|宣布|决定|发生|造成|导致|"
    r"下调|上调|调整|停产|裁员|破产|获批|实施|开工|竣工|召回|预警|启动|暂停|恢复|"
    r"通报|回应|表示|指出|要求|预计|同比|环比|新增|减少|关闭|开业|通车|涨价|降价|"
    r"补贴|缴费|报销|投产|签署|通过|落地|受理|立案|判决|处罚|召回|疏散|救援")


def _net(text: str) -> str:
    """净字数：去掉缩进 / 空白 / 换行后的可见字符（用户说的「不超过 100 字」按此计）。"""
    return re.sub(r"[\u3000\s]", "", str(text or ""))


def _strip_paragraphs(raw: str) -> List[str]:
    parts = re.split(r"[\r\n]+", clean_html(raw))
    out = []
    for p in parts:
        p = p.strip().lstrip("\u3000 ").strip()
        if (META_LINE.search(p) and len(_net(p)) < 40) or CODE_LINE.search(p):
            continue
        p = PLACEHOLDER.sub("", p).strip()
        p = re.sub(r"^[，,、；;：:\s]+", "", p).strip()   # 残留的残缺标点
        if p and not re.fullmatch(r"[\s\u3000]*", p):
            out.append(p)
    return out


def _sentences(text: str) -> List[str]:
    ss = [s.strip() for s in SENT_END.split(text) if s.strip()]
    return ss or [text.strip()] if text.strip() else []


def _clean_sentence(s: str) -> str:
    s = ELECTRIC_HEAD.sub("", s)
    s = PAREN_META.sub("", s)
    return re.sub(r"\s{2,}", " ", s).strip()


def _is_noise(s: str) -> bool:
    # 长句即便带署名也多半是正文，只丢短元数据句，避免误伤新闻事实
    return len(_net(s)) < 30 and bool(NOISE_SENT.search(s))


def polish_abstract(text: str) -> str:
    """对**已成稿**摘要做纯格式清理，不改内容、不改数字、不压缩、不拆段。

    线上批次（2026-11-22 latest.xlsx）暴露：事件成稿层 / keyless 层的摘要绕过
    `build_abstract`，于是「新华社记者戴小河、王悦阳」「央视网消息（新闻联播）」
    「；。」「。）。」「经核查。」这类电头、署名、拼接坏标点直接进了 Excel。
    本函数只清格式与站点元数据，两段结构与每段整句原样保留。
    """
    if not text:
        return text
    out: List[str] = []
    for para in re.split(r"[\r\n]+", str(text)):
        p = para.strip()
        if not p:
            continue
        # 整行是记者署名 / 电头 / 站点元数据 → 整行丢（不删半句）
        if META_LINE.search(p) and len(_net(p)) < 40:
            continue
        if re.fullmatch(r"[\s\u3000]*[\(（]?\s*[）)]?[\s\u3000]*[。.;；]*", p):
            continue
        p = ELECTRIC_HEAD.sub("", p)
        # 电头后残留的「题：」（新华社通稿模板），只清前缀不动正文
        p = re.sub(r"^\s*题[:：]\s*", "", p)
        p = PAREN_META.sub("", p)
        # 行内记者署名（「新华社记者甲、乙」）只清署名片段，保留后面正文
        p = re.sub(r"(?:新华社|中新网|央视网|本报|澎湃新闻|财联社|经济日报|光明日报)"
                   r"记者[\u4e00-\u9fff]{2,6}(?:、[\u4e00-\u9fff]{2,6})*", "", p)
        # 拼接坏标点：「；。」→「。」、「，。」→「。」、「。）。」/「。）」→「。」
        p = re.sub(r"[；;]\s*[。.]", "。", p)
        p = re.sub(r"[，,、]\s*[。.]", "。", p)
        p = re.sub(r"[。.]\s*[）)]\s*[。.]?\s*$", "。", p)
        p = re.sub(r"[。.]\s*[）)]\s*[。.]", "。", p)
        p = re.sub(r"[。.]{2,}", "。", p)
        p = re.sub(r"^[，,、；;：:\s]+", "", p).strip()
        p = p.rstrip("，,、；;：: ")
        # 结尾残破（「经核查。」这类截断半句保留内容，只补规范句号）
        if p and p[-1] not in "。！？；.!?;":
            p += "。"
        if p:
            out.append(p)
    return "\n".join(out)


def _is_informative(s: str) -> bool:
    return bool(FACT.search(s))


def _dedupe_sentence_list(sents: List[str]) -> List[str]:
    seen, out = set(), []
    for s in sents:
        key = compact(s)
        if key and key not in seen:
            seen.add(key)
            out.append(s)
    return out


def _truncate_clause(s: str, limit: int | None = None) -> str:
    """单句就超预算：按整分句累加，再在标点边界收束，绝不切破数字或半个词。
    limit 用于「第二句只剩 room 字」的场景；默认用全局 MAX_LEN。

    若后面还有短结论分句（「决定…」「造成…」「生效」等），腾出预算补回来，
    避免「铺陈了一大串状语却没说结果」——仍是原文分句的原序抽取，不改写任何字。
    """
    cap = int(limit or MAX_LEN)
    cls = [c for c in CLAUSE_SPLIT.split(s) if c.strip()]
    out: List[str] = []
    acc = 0
    for c in cls:
        if acc + len(_net(c)) > cap and out:
            break
        out.append(c)
        acc += len(_net(c))
    if not out:
        cut = s[:cap]
        # 末尾若是半个数字 / 半个英文词，退到最后一个完整边界
        cut = re.sub(r"(?:\d+\.\d*|[A-Za-z]+)$", "", cut) or s[:cap]
        out = [cut]
        acc = len(_net(cut))
    rest = cls[len(out):]
    concl = [c for c in rest
             if len(_net(c)) <= 26
             and re.search(r"决定|调整|发布|宣布|造成|导致|死亡|受伤|实施|执行|生效|启用|停产|裁员|破产", c)]
    if concl:
        c = concl[-1]
        while out and acc + len(_net(c)) > cap:
            acc -= len(_net(out.pop()))
        if acc + len(_net(c)) <= cap:
            out.append(c)
    text = "".join(out).rstrip("，,、；;：: ").strip()
    if not text:
        return ""
    return text if text.endswith(tuple(CLAUSE_END)) else text + "。"


def _condense(paras: List[str]) -> List[str]:
    """核心：按原文顺序挑整句 → 贪心累加到 ≤100 净字 → 至少一句含事实 → 单段收束。

    新闻导语本来就在最前面，按原序取 1–2 句即可「大概说清楚」且最流畅；
    不做生成式改写、不调 LLM、不改写任何数字。
    """
    sents: List[str] = []
    for p in paras:
        for s in _sentences(p):
            s = _clean_sentence(s)
            if s and not _is_noise(s):
                sents.append(s)
    sents = _dedupe_sentence_list(sents)
    if not sents:
        return []

    out: List[str] = []
    acc = 0
    for s in sents:
        n = len(_net(s))
        if acc + n <= MAX_LEN:
            out.append(s)
            acc += n
            continue
        if not out:                       # 首句就超预算 → 整分句截断
            clipped = _truncate_clause(s)
            if clipped:
                out.append(clipped)
                acc = len(_net(clipped))
            # 截断后常还有余额，继续收下一句短结论，避免「说了半句没说清结果」
            continue
        # 第二句整句放不下时，在剩余预算内按整分句收束，尽量形成第二段
        # （目标样例 34/34 两段；仍不切半句、不改写数字）。
        room = MAX_LEN - acc
        if room >= 16:
            clipped = _truncate_clause(s, limit=room)
            if (len(_net(clipped)) >= 12
                    and not _near_dup("".join(out), clipped)):
                out.append(clipped)
        break                             # 到边界就停，保证导语完整、不塞无关细节

    # 若已选的句子全是空话，在剩余预算里补一句含事实的整句（仍按原序）
    if out and not any(_is_informative(x) for x in out):
        room = MAX_LEN - sum(len(_net(x)) for x in out)
        for s in sents[len(out):]:
            if _is_informative(s) and len(_net(s)) <= room:
                out.append(s)
                break

    text = re.sub(r"^[，,、；;：:\s]+", "", "".join(out).strip())
    text = re.sub(r"[，,、；;：:]{2,}", "，", text)   # 拼接处不出现连续标点
    if not text:
        return []
    if not text.endswith(tuple(CLAUSE_END)):
        text += "。"
    # 最后一道硬闸：极端情况下再按整句从尾部删，绝不硬切
    while len(_net(text)) > MAX_LEN:
        ss = _sentences(text)
        if len(ss) <= 1:
            text = _truncate_clause(text)
            break
        text = "".join(ss[:-1]).strip()
    return [text]


def _dedupe_paragraphs(paras: List[str]) -> List[str]:
    """段级去重：规范化后去重，保留首次出现顺序。"""
    seen, out = set(), []
    for p in paras:
        key = compact(p)
        if key and key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _dedupe_sentences(text: str) -> str:
    """句级去重：台账常见「A。B。A。B。」，段级去重后仍需句级再收一次。"""
    return "".join(_dedupe_sentence_list(_sentences(text))) or text


def _format(paras: List[str]) -> str:
    """按 PARAGRAPHS 输出 1–3 段；每段保留 2 全角空格缩进（不占 100 净字）。"""
    ps = [p for p in paras if p][:max(1, PARAGRAPHS)]
    return (INDENT + ("\n" + INDENT).join(ps)).replace("\r", "") if ps else ""


def _bigrams(s: str) -> set:
    t = _net(s)
    return {t[i:i + 2] for i in range(len(t) - 1)} or {t}


def _near_dup(a: str, b: str) -> bool:
    """字符 2-gram Jaccard > 0.45 视为换个说法的重复，不切成两段。"""
    x, y = _bigrams(a), _bigrams(b)
    return bool(x and y and len(x & y) / len(x | y) > 0.45)


def _split_two(text: str) -> List[str]:
    """把连续整句按句边界切成两段：两段都 ≥12 净字，且互不重复（含近似重复）。"""
    ss = _sentences(text)
    if len(ss) < 2:
        return [text]
    for k in range(1, len(ss)):
        a, b = "".join(ss[:k]).strip(), "".join(ss[k:]).strip()
        if (len(_net(a)) >= 12 and len(_net(b)) >= 12 and a != b
                and not _near_dup(a, b)):
            return [a, b]
    return [text]


def _finish(paras: List[str]) -> str:
    """段级去重 → 句级去重 → 挑整句精简到 ≤100 净字 → 按配置分段、整句收束。"""
    paras = _dedupe_paragraphs(paras)
    paras = [_dedupe_sentences(p) for p in paras]
    paras = _condense(paras)
    if not paras:
        return _format([MISSING_TEXT])
    if PARAGRAPHS >= 2 and len(paras) == 1:
        paras = _split_two(paras[0])
    return _format(paras)


def build_abstract(item: RawItem) -> Tuple[str, str]:
    """返回 (合规摘要, 摘要来源标记)，并写入 item.abstract_status。"""
    from .rules import template_noise
    raw = clean_html(item.abstract or "")
    body = clean_html(item.abstract_body or "")

    # 0. 站点模板 / 备案页脚 / UTF-8 mojibake 污染（展示序 17/59「ð📰盖世…」、
    #    65「互联网新闻信息服务许可证…」、80「故障预警：$JPM」）。
    #    这类文本不是新闻正文，一律判「未取得」，绝不拿它凑摘要字数。
    if template_noise(raw) or template_noise(body):
        item.abstract_status = "未取得"
        return _format([MISSING_TEXT]), "未取得（站点模板 / 备案页脚 / 乱码污染，已留痕）"

    # 1. 正文补采优先（独立原文，信息量最高）
    if len(compact(body)) >= 40 and not is_title_echo(item.title, body, item.source):
        paras = _strip_paragraphs(body)
        if paras:
            item.abstract_status = "正文补采"
            return _finish(paras), "独立原文正文补采（按整句精简至 ≤100 字，数字未改写）"

    # 2. 原始摘要可用（非模板复读、长度足够）
    if len(compact(raw)) >= 15 and not is_title_echo(item.title, raw, item.source):
        paras = _strip_paragraphs(raw)
        if paras:
            item.abstract_status = "有效"
            return _finish(paras), "原文摘要（HTML 清洗 / 整句精简至 ≤100 字，数字未改写）"

    # 3. 标题复读 / 摘要缺失：如实标注，不冒充
    item.abstract_status = "未取得"
    return _format([MISSING_TEXT]), "未取得（源站标题复读 / 正文未开放，已留痕）"


def validate_fields(item: RawItem) -> Tuple[bool, str]:
    """候选池准入：标题 / 来源 / 分类三字段齐备。

    摘要「未取得」**仍留在候选池**（需求⑤要求窗口内全部数据齐备可查、逐条留痕），
    但必须显式标记；pipeline 准入层的 main_requires_abstract 硬闸会把它拦在
    主表之外。由 quality 门禁统计摘要有效率；绝不允许用标题复读充当有效摘要。
    """
    if not item.title.strip():
        return False, "标题缺失"
    if not item.source.strip():
        return False, "来源缺失"
    if not compact(item.abstract) and not compact(item.abstract_body):
        return False, "摘要与正文均缺失（词条/线索不进候选池，见附录 15）"
    if not item.group or not item.tag:
        return False, "上位大类 / 分类标签不合规"
    if not item.abstract_status:
        item.abstract_status = "未取得"
    return True, "三字段齐备、分类合规（摘要状态：" + item.abstract_status + "）"


def clean_title_text(s: str, max_len: int = 60) -> str:
    """2026-11-21 全面核查：旧版只做 clean_html + 合并空白，微博/长描述源把整段
    正文塞进标题（实测主表 1 条、候选池 11 条，最长 78 字，audit P0-5 FAIL）。
    12 列契约的「标题」字段只放标题，正文归「内容摘要」；这里按整句边界收束，
    不做半词截断，尾部的省略号 / 日期线索尾巴一并去掉。
    """
    s = re.sub(r"\s+", " ", clean_html(s or "").replace("\n", " ")).strip()
    s = re.sub(r"\s*(?:\.{3}|…|\[\d{1,2}-\d{1,2}\])\s*$", "", s).strip()
    if len(s) <= max_len:
        return s
    # 优先在 <=max_len 的最后一个整句边界收束（避免切出半句）
    cut = -1
    for m in re.finditer(r"[。！？；]", s[:max_len]):
        cut = m.end()
    if cut >= 15:
        return s[:cut].strip()
    return s[:max_len].rstrip("，,、：:；; ").strip()


def normalize_item(item: RawItem, win, *, legacy_groups: bool = False) -> RawItem:
    """清洗 + 时间字段回填（优先级见渠道文档 §0.4）+ 去重主键。"""
    from .rules import classify
    item.title = clean_title_text(item.title)
    item.source = clean_html(item.source)
    item.source = re.sub(r"\s*·\s*", " · ", item.source).strip(" ·")
    item.abstract = clean_html(item.abstract)
    item.abstract_body = clean_html(item.abstract_body or "")
    item.group, item.tag = classify(item, legacy=legacy_groups)
    pub, basis, mark = resolve_pub(item, win)
    item.pub, item.time_basis, item.time_mark = pub, basis, mark
    item.dedup_key = f"{item.url}|{item.title}" if item.url else f"{item.channel}|{item.title}"
    return item


def dedup_candidate(items: List[RawItem]) -> Tuple[List[RawItem], List[dict]]:
    """采集层只去「URL + 标题」完全重复；同标题不同渠道一律保留。"""
    seen, kept, dropped = set(), [], []
    for it in items:
        if it.dedup_key in seen:
            dropped.append({"标题": it.title, "渠道": it.channel, "URL": it.url,
                            "原因": "采集层完全重复（URL+标题）"})
            continue
        seen.add(it.dedup_key)
        kept.append(it)
    return kept, dropped
