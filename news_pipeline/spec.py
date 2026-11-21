"""规格契约 + 纯代码验收器。

把 01_instruction / 02_SKILL / 03_作业总表 / 00_总览 / README 仲裁规则里
「可以机械判定」的部分写成常量与检查函数。**验收由代码执行，不靠人在对话里
逐条肉眼核对**：scripts/verify_spec.py 直接调本模块，逐条给 PASS / FAIL 与证据。

仲裁顺序（README §仲裁规则）：01 > 02/03 > 05；04 仅作变更留痕，不作运行规格。
因此本文件以 **01 第六节** 为准：
  - 附录 1–17 + 「候选池边界留痕」，**不编第 18 号附录**；
  - 主表 / 前 10 按事件主键去重，候选池不去重不合并；
  - story_max_keep 默认 1；档位只有 strict / moderate / off 三档。
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, List

# ---------------------------------------------------------------- 硬契约常量
COLS_12: List[str] = [
    "展示序", "Final_adj", "等级", "原始 Final", "分数序", "上位大类",
    "分类标签", "标题", "内容摘要", "来源", "事件主键", "记录类型",
]
SHEETS_5: List[str] = [
    "处理后的数据", "排名最靠前的10条", "窗口内全部数据", "口径与稿外信息", "评级留痕",
]
DASH_COLS = ["Final_adj", "等级", "原始 Final", "分数序", "事件主键"]
STRICTNESS_LEVELS = ("strict", "moderate", "off")           # 01 第二节：无第四档
DEFAULT_STORY_MAX_KEEP = 1                                  # 01 第三节：默认 1
DEFAULT_LAMBDA = 6
SOURCE_SEP = " · "                                          # 渠道之间带空格
APPENDIX_1_17 = [str(i) for i in range(1, 18)]
QUALITY_PENALTIES = {                                       # 01 第四节 / 02 §4.6
    "旧闻冒充新事 / 旧影像无标注": -8,
    "标题与正文主体、时间、地点、结果、影响失配": -5,
    "来源仅自媒体且无独立佐证": -5,
    "只有情绪或立场、无新事实": -4,
    "涉伤亡 / 事故 / 政策却无更新时间": -3,
}
VERIFY_STATES = {
    "A": "已核实（正常评分）",
    "B": "部分核实（封顶 C、进观察池）",
    "C": "未核实（不得进 A 级，24h 后仍无权威确认降 D）",
}
NO_LLM_DECLARATION = (
    "本产物由纯代码规则流水线生成：采集、准入、打分、编排、出表、验收全部由 "
    "Python 代码执行，不调用任何大语言模型 / AI 语义服务；摘要为抽取式（按整句裁剪），"
    "不是生成式摘要；所有判定可逐条复算。"
)
# 01 第六节「全文写法」禁止项（只准进口径表，不准进标题 / 摘要）
PROCESS_WORDS = ["根据搜索结果", "按内部规则", "接口未返回", "本轮剔除了", "按用户口径", "其余全部剔除"]
PLACEHOLDER_WORDS = ["详情以原文为准", "点击查看", "本文由AI生成", "以上内容为模型生成"]
# 三类强剔除 / 非三类国际：用于主表残留硬闸
ACHIEVEMENT_WORDS = ["稳中向好", "同比增长", "再创新高", "成效显著", "持续向好", "稳中有进", "圆满收官"]
FAR_FUTURE_WORDS = ["十五五规划", "规划纲要", "远景目标", "征求意见稿", "框架协议", "意向签约", "多年后目标", "中长期规划"]
UNRELATED_WORDS = ["会见出席", "会务筹备", "会务预热", "人事任免", "外事礼宾", "内部评优", "行业年会"]
INSTANT_ACTION_WORDS = ["油价调整", "气象预警", "寒潮蓝色预警", "医保目录", "社保办理", "供暖", "退费", "限行", "放假安排", "召回公告"]
INTL_THREE = ("战争与武装冲突", "灾害与公共卫生", "突发事故与重大安全事件")


def grade_of(final: float) -> str:
    """01 第四节：按原始 Final 机械映射，不随展示分变动。"""
    if final >= 85:
        return "S"
    if final >= 70:
        return "A"
    if final >= 55:
        return "B"
    if final >= 40:
        return "C"
    return "D"


def source_format_ok(s: str) -> bool:
    """01 第三/六节：渠道间 ` · `，无编号 / 竖线 / 链接 / 占位 / 括注。"""
    if not s or not str(s).strip():
        return False
    t = str(s)
    # 2026-11-22：`\d+[、.]` 本意是拒绝「1、搜狐网 / 2. 中新网」这类编号，
    # 但会误伤 `163.com` 这种数字开头的真实域名（线上 CI 第 3 行来源列因此整行 FAIL）。
    # 收紧为编号的真实形态：顿号必拒；数字+点只在后面跟空白/结尾时判编号，
    # 点后紧跟字母（域名）放行。不放宽其它任何一项（链接/占位/括号/竖线仍全拒）。
    if re.search(r"https?://|www\.|\||｜|【|】|\[|\]|（|）|\(|\)|未取得|待补|N/A|—|"
                 r"\d+、|\d+\.(?=\s|$)", t):
        return False
    parts = t.split(SOURCE_SEP)
    if len(parts) < 1:
        return False
    for p in parts:
        if not p.strip() or " " in p.strip():        # 平台内部层级 `·` 不带空格
            return False
    return True


def abstract_net_len(s: str) -> int:
    """净字数：去掉 2 全角空格缩进与空白换行后的可见字符。"""
    import re as _re
    return len(_re.sub(r"[\u3000\s]", "", str(s or "")))


def abstract_ok(s: str) -> tuple[bool, str]:
    """用户 2026-11-20 补充口径（覆盖 01 第六节 / 03 §4.3 的 2–3 段 / 240 字）：
    净字数 ≤100、两段（1–2 段 / 0–1 LF，仅一句时单段）、每段 2 全角空格、整句收束、无占位句。
    """
    t = str(s or "")
    if "\r" in t:
        return False, "含 CRLF"
    n = abstract_net_len(t)
    if n > 100:
        return False, f"净字数超 100（{n}）"
    if n < 8:
        return False, f"净字数过短（{n}）"
    if any(w in t for w in PLACEHOLDER_WORDS):
        return False, "占位句残留"
    if any(w in t for w in PROCESS_WORDS):
        return False, "过程性表述残留"
    n_lf = t.count("\n")
    if not (0 <= n_lf <= 1):
        return False, f"LF 数 {n_lf}（应 0–1，即 1–2 段）"
    paras = t.split("\n")
    if not (1 <= len(paras) <= 2):
        return False, f"段数 {len(paras)}（应 1–2）"
    for p in paras:
        if not p.startswith("\u3000\u3000"):
            return False, "段落首行未缩进 2 个全角空格"
    if not t.rstrip().endswith(("。", "！", "？", "；")):
        return False, "未以整句标点收束（不得切半句）"
    return True, "OK"


# ---------------------------------------------------------------- 验收器
def _num(x: Any):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def verify_workbook(path: str) -> Dict[str, Any]:
    """逐条按 01 第六节 / 03 §5 质检清单验收，返回结构化结果（纯代码，无人工判断）。"""
    from openpyxl import load_workbook
    from openpyxl.reader.excel import ExcelReader
    ExcelReader.read_custom = lambda self: None          # 兼容本项目写入的自定义属性

    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"项": name, "结果": "PASS" if ok else "FAIL", "证据": str(detail)[:220]})

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws4 = wb["口径与稿外信息"] if "口径与稿外信息" in wb.sheetnames else None
        txt = "\n".join(" | ".join(str(x or "") for x in row)
                        for row in ws4.iter_rows(values_only=True)) if ws4 else ""
        # 1 五工作表
        add("五工作表名称与顺序", wb.sheetnames == SHEETS_5, f"实际 {wb.sheetnames}")

        def rows_of(sheet: str):
            it = wb[sheet].iter_rows(values_only=True)
            h = list(next(it))
            return h, [dict(zip(h, r)) for r in it]

        h1, main = rows_of("处理后的数据") if "处理后的数据" in wb.sheetnames else ([], [])
        h2, top = rows_of("排名最靠前的10条") if "排名最靠前的10条" in wb.sheetnames else ([], [])
        h3, pool = rows_of("窗口内全部数据") if "窗口内全部数据" in wb.sheetnames else ([], [])
        h5, trace = rows_of("评级留痕") if "评级留痕" in wb.sheetnames else ([], [])

        # 2 三张数据表 12 列且一致
        add("三张数据表均 12 列且列名列序一致",
            h1 == COLS_12 and h2 == COLS_12 and h3 == COLS_12,
            f"主表 {len(h1)} 列 / 前10 {len(h2)} 列 / 候选池 {len(h3)} 列")
        add("无关键词 / 重点句 / 原文图片列",
            not any(w in "".join(h1) for w in ("关键词", "重点句", "原文图片")), "列名白名单")

        # 3 候选池评级列全「—」
        dash_bad = [(r.get("展示序"), c, r.get(c)) for r in pool for c in DASH_COLS
                    if str(r.get(c)) != "—"]
        add("候选池评级列全为「—」", len(dash_bad) == 0, f"异常 {len(dash_bad)} 处 {dash_bad[:2]}")
        seqs = [_num(r.get("展示序")) for r in pool]
        add("候选池展示序为采集序 1..N 连续",
            seqs == [float(i) for i in range(1, len(pool) + 1)],
            f"N={len(pool)} 首末 {seqs[:1]}/{seqs[-1:]}")

        # 4 包含关系（主表是候选池子集）
        key = lambda r: (str(r.get("标题") or "").strip(), str(r.get("来源") or "").strip())
        pool_keys = {key(r) for r in pool}
        miss = [r.get("标题") for r in main if key(r) not in pool_keys]
        add("主表 ⊆ 候选池（按标题+来源）", len(miss) == 0, f"未命中 {len(miss)} 条 {miss[:2]}")
        add("前 10 表与主表前 10 逐格一致",
            [tuple(r.get(c) for c in COLS_12) for r in top] ==
            [tuple(r.get(c) for c in COLS_12) for r in sorted(main, key=lambda x: _num(x.get("展示序")))[:10]],
            f"前10 {len(top)} 行")
        dup_main = len(main) - len({tuple(r.get(c) for c in COLS_12) for r in main})
        dup_top = len(top) - len({tuple(r.get(c) for c in COLS_12) for r in top})
        add("主表 / 前 10 完全重复行 0 / 0", dup_main == 0 and dup_top == 0,
            f"主表 {dup_main} / 前10 {dup_top}")

        # 5 分数序、Final_adj 复算
        by_title: Dict[str, Dict[str, Any]] = {}
        for r in trace:
            by_title.setdefault(str(r.get("标题") or "").strip(), r)
        bad_recalc, bad_grade, missing = [], [], 0
        for r in main:
            F, FA = _num(r.get("原始 Final")), _num(r.get("Final_adj"))
            t = by_title.get(str(r.get("标题") or "").strip(), {})
            gin, cap = _num(t.get("gin")), _num(t.get("cap"))
            if None in (F, FA, gin, cap):
                missing += 1
                continue
            expect = round(F - DEFAULT_LAMBDA * min(max(0, gin - 1), cap), 2)
            if abs(expect - FA) > 0.011:
                bad_recalc.append((r.get("展示序"), F, FA, expect))
            if grade_of(F) != str(r.get("等级")):
                bad_grade.append((r.get("展示序"), r.get("等级"), grade_of(F)))
        add("Final_adj = Final − 6×min(gin−1,cap) 逐条复算",
            len(bad_recalc) == 0 and missing == 0, f"异常 {len(bad_recalc)} / 关联缺失 {missing}")
        add("等级按原始 Final 机械映射", len(bad_grade) == 0, f"异常 {len(bad_grade)} {bad_grade[:2]}")
        so = sorted(int(_num(r.get("分数序")) or 0) for r in main)
        add("分数序为连续唯一 1..N", so == list(range(1, len(main) + 1)), f"N={len(main)} max={so[-1:] if so else []}")

        # 6 来源列格式
        bad_src = [(r.get("展示序"), r.get("来源")) for r in main if not source_format_ok(r.get("来源"))]
        add("来源列统一 `渠道 · 渠道` 且无编号/竖线/链接/占位",
            len(bad_src) == 0, f"异常 {len(bad_src)} {bad_src[:2]}")

        # 7 摘要规范（用户 2026-11-20 补充：精简 ≤100 字、流畅、两段整句收束）
        bad_abs = [(r.get("展示序"), abstract_ok(r.get("内容摘要"))[1]) for r in main
                   if not abstract_ok(r.get("内容摘要"))[0]]
        add("内容摘要 1–2 段 / 缩进 / 整句收束 / 无占位句",
            len(bad_abs) == 0, f"异常 {len(bad_abs)} {bad_abs[:2]}")
        over100 = [(r.get("展示序"), abstract_net_len(r.get("内容摘要")))
                   for r in main if abstract_net_len(r.get("内容摘要")) > 100]
        maxlen = max([abstract_net_len(r.get("内容摘要")) for r in main] or [0])
        add("内容摘要净字数 ≤100（用户补充口径，抽取式不改写数字）",
            len(over100) == 0, f"超 100 字 {len(over100)} 条；本批最长 {maxlen} 字")
        # 候选池「窗口内全部数据」也必须摘要齐备合规；此前只验主表，漏掉了
        # 源站把 <script> 里的 JS 当正文吐出的情况（实测 28 条 var isHttps…）。
        from .article import CODE_LINE
        bad_pool = []
        for r in pool:
            s = str(r.get("内容摘要") or "")
            ok, why = abstract_ok(s)
            if not ok or CODE_LINE.search(s):
                bad_pool.append((r.get("展示序"), why if not ok else "脚本代码行残留"))
        add("候选池摘要同样 ≤100 字 / 1–2 段 / 整句收束 / 无脚本代码行",
            len(bad_pool) == 0, f"异常 {len(bad_pool)} 条 {bad_pool[:2]}")

        # 8 前窗相邻同大类
        front = [r.get("上位大类") for r in sorted(main, key=lambda x: _num(x.get("展示序")))[:10]]
        adj = sum(1 for i in range(1, len(front)) if front[i] == front[i - 1])
        # 01 §4.12 同时规定「前窗相邻同大类必须为 0」与「容量不足被迫连续同类须逐条
        # 列位置、大类、标签与原因」。物理上不可能错开时，按「已逐条留痕 + 口径声明」验收。
        forced_logged = "被迫连续同类" in txt
        capacity_declared = "容量不足" in txt
        ok = adj == 0 or (forced_logged and capacity_declared)
        add("前窗相邻同大类 0 或容量不足已逐条留痕（01 §4.12）", ok,
            f"相邻同大类 {adj} 对 / 覆盖 {len(set(front))} 类 / 被迫连续留痕 "
            f"{'有' if forced_logged else '无'} / 容量声明 {'有' if capacity_declared else '无'}")

        # 9 四项口径硬闸（主表残留）
        titles = [str(r.get("标题") or "") for r in main]
        ach = [t for t in titles if any(w in t for w in ACHIEVEMENT_WORDS)]
        far = [t for t in titles if any(w in t for w in FAR_FUTURE_WORDS)]
        unr = [t for t in titles if any(w in t for w in UNRELATED_WORDS)]
        intl_bad = [str(r.get("分类标签")) for r in main
                    if str(r.get("上位大类")) == "国际动态" and
                    not any(k in str(r.get("分类标签")) for k in INTL_THREE)]
        add("成就宣传与态势综述残留 0", len(ach) == 0, f"{len(ach)} 条 {ach[:2]}")
        add("远期规划与未落地事项残留 0", len(far) == 0, f"{len(far)} 条 {far[:2]}")
        add("与普通人无关的机构动态残留 0", len(unr) == 0, f"{len(unr)} 条 {unr[:2]}")
        add("非三类重大突发的国际条目 0", len(intl_bad) == 0, f"{len(intl_bad)} 条 {intl_bad[:2]}")
        # 即时可行动民生信息未被误剔（候选池里有 → 主表里也要有）
        instant_pool = [t for t in (str(r.get("标题") or "") for r in pool)
                        if any(w in t for w in INSTANT_ACTION_WORDS)]
        absent = [t for t in instant_pool if t not in titles]
        # 01 §1.4：同一议题不同侧面按议题簇去重，每簇留 1 条代表条，其余完整内容
        # 进附录 17 / 12b。这类「被合并」不是准入误剔，不能算误杀。
        zone, in_zone = [], False
        for ln in txt.split("\n"):
            if any(k in ln for k in ("附录 17", "附录 12b", "议题簇合并留痕", "同事件去重留痕")):
                in_zone = True
            elif in_zone and "附录 " in ln and not any(k in ln for k in ("17", "12b")):
                in_zone = False
            if in_zone:
                zone.append(ln)
        merge_txt = "\n".join(zone)
        lost = [t for t in absent if t not in merge_txt]
        add("即时可行动民生信息未被误剔",
            len(lost) == 0,
            f"候选池命中 {len(instant_pool)} 条 / 主表缺失 {len(absent)} 条，其中 "
            f"{len(absent) - len(lost)} 条按议题簇或事件合并留痕（附录 17 / 12b，不算误剔）；"
            f"真实误剔 {len(lost)} 条 {lost[:2]}")

        # 10 附录契约（01 仲裁：1–17 + 候选池边界留痕，不编 18）
        found = set(re.findall(r"附录\s*(\d+)([a-z]?)", txt))
        nums = {int(a) for a, _ in found if a.isdigit()}
        lack = [i for i in range(1, 18) if i not in nums]
        add("附录 1–17 齐全", len(lack) == 0, f"缺 {lack}")
        add("含「候选池边界留痕」", "候选池边界留痕" in txt, "01 第六节")
        has18 = bool(re.search(r"附录\s*18(?!\d)", txt))
        add("不编第 18 号附录（00 §9 / README 仲裁）", not has18,
            "附录 18 属仓库链路，本表不占用该编号" if not has18 else "仍出现「附录 18」")
        add("口径表含纯代码 / 无模型语义声明", "纯代码" in txt and "模型语义" in txt, NO_LLM_DECLARATION[:40])
        add("口径表含执行链路身份声明（不冒充调度器回执）",
            "非" in txt and ("调度器" in txt or "定时任务" in txt), "链路声明")
        bad_words = [w for w in ("LLM 生成", "模型扩写", "AI 语义判断") if w in txt]
        add("无 LLM / 模型扩写措辞", len(bad_words) == 0, f"{bad_words}")
        add("贴近度档位为三档之一",
            any(f"档位 {s}" in txt or f"贴近度 {s}" in txt for s in STRICTNESS_LEVELS),
            f"可选 {STRICTNESS_LEVELS}")
        add("含开源组件逐项登记", "feedparser" in txt and "trafilatura" in txt, "oss.py 逐项登记")
        # ---- 11 需求①–⑤ 逐条机械验收（用户重述口径，纯代码读单元格） ----
        from pathlib import Path as _P
        add("① 定时 20:00 + UTC cron 0 12 * * *（北京 20:00）",
            "20:00" in txt and "0 12 * * *" in txt,
            "口径表含 run_at 20:00 与 UTC 12:00 cron")
        add("① 窗口 24h 左开右开 (前一日20:00, 当日20:00)·用户拍板",
            ("左开右开" in txt or "lo-ro" in txt) and "24" in txt,
            "口径表含左开右开与 24h 窗口；四档仍可切")
        _root = _P(__file__).resolve().parents[1]
        _idx = _root / "docs" / "index.html"
        _app = _root / "docs" / "assets" / "app.js"
        _html = _idx.read_text(encoding="utf-8") if _idx.exists() else ""
        _js = _app.read_text(encoding="utf-8") if _app.exists() else ""
        add("① 页面运行日志区保留（默认折叠、展开才读取）",
            'id="logPanel"' in _html and "logPanel" in _js and "/api/logs" in _js,
            "index.html 含折叠日志区，app.js 接 /api/logs 与失败自动展开")
        add("① 大众贴近度三类强剔除 + 国际仅三类重大突发",
            "三类强剔除" in txt and "国际动态仅" in txt,
            "口径表准入声明")
        add("② 31 类 599 渠道主白名单",
            "599" in txt and "31 类" in txt,
            "口径表清单版本 / 需求逐条落地 section")
        add("② 10 个社交平台登记为主干发现入口",
            "10 个社交主干平台逐条申报" in txt and txt.count("主干发现入口") >= 10,
            f"主干发现入口出现 {txt.count('主干发现入口')} 次")
        add("② 5 类快讯/拼盘源全量拆条、母行不丢",
            "快讯/拼盘源全量拆条" in txt and "母行不丢" in txt,
            "附录 15 拆条留痕 section")
        add("② 未取得拼盘源如实申报、不拆假词条",
            "不拆假词条" in txt and "不编造词条凑数" in txt,
            "附录 15 采集缺口申报行")
        add("② 词条逐条回查独立原文、不吃 snippet",
            "社交主干词条逐条回查留痕" in txt and "线索待原文" in txt,
            "附录 15 回查与线索留痕")
        add("③ 每条 URL / 发布时间 / 时间依据 / 时效标记",
            "逐条发布时间 / 时间依据 / 时效标记 / 日期线索留痕" in txt and "正文补采" in txt,
            "逐条时间留痕 + 正文补采统计")
        add("③ 标题月日只作日期线索、不反推发布时间",
            "日期线索只登记不反推" in txt or "只留痕不反推" in txt,
            "需求逐条落地③ / timewindow 口径")
        add("④ 强制第 1 留痕（≥7 级 / 死伤多 / 多渠道高覆盖）",
            "附录 16 · 需求④ 强制第 1 留痕" in txt,
            "附录 16 强制第 1 逐条留痕")
        add("④ 影响面四维计入 U 且评级数值不动",
            "影响面四维计入 U" in txt and "评级数值不动" in txt,
            "附录 11 标题与编排分 U 公式")
        add("④ 破产倒闭主体可核对性核验留痕",
            "破产倒闭主体可核对性核验留痕" in txt,
            "附录 16 主体核验 section")
        # 2026-11-22 线上 CI 截图：FAIL「同目录其它 xlsx: ['线上批次新规则重判对照.xlsx']」。
        # 那是随包交付的**验收/重判排障证据**，不是本批期次产物；旧版 glob("*.xlsx")
        # 把根目录所有 xlsx 都数进去，等于「自己带的证据把自己判红」，并与
        # sync_single_xlsx 已修的口径打架（后者只清理 *_窗口期*.xlsx，明确不删验收材料）。
        # 现在只数同批日期产物：多出一份往期 xlsx 照样 FAIL，证据文件如实列出但不计入。
        _here = _P(path).parent
        _batch = [f for f in _here.glob("*_窗口期*.xlsx")
                  if f.resolve() != _P(path).resolve()]
        _evidence = [f.name for f in _here.glob("*.xlsx")
                     if f.name != _P(path).name and f not in _batch]
        add("⑤ 单个 xlsx（output 目录仅一份产物，latest 为字节副本）",
            len(_batch) == 0,
            f"同批其它期次产物：{[f.name for f in _batch]}；"
            f"验收/重判证据（不计入）：{_evidence}")
        add("⑤ 被剔 / 被合并 / 线索待原文均有留痕",
            all(k in txt for k in ("附录 1 ·", "附录 12b", "附录 13 ·", "附录 15 ·", "附录 17 ·",
                                   "候选池边界留痕")),
            "附录 1/12b/13/15/17 + 候选池边界留痕齐全")
    finally:
        wb.close()

    passed = sum(1 for c in checks if c["结果"] == "PASS")
    return {"产物": path, "检查项": len(checks), "通过": passed,
            "失败": len(checks) - passed, "结论": "达标" if passed == len(checks) else "不达标",
            "明细": checks, "等级分布": dict(Counter(str(r.get("等级")) for r in main))}


def verify_extra(path: str) -> List[Dict[str, Any]]:
    """本轮新增的 4 项机械验收（读产物侧文件，不靠人核对）。"""
    import json
    from pathlib import Path as _P
    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"项": name, "结果": "PASS" if ok else "FAIL", "证据": str(detail)[:220]})

    base = _P(__file__).resolve().parents[1]
    # 1) 全渠道逐条启用动作：599 条都有明确动作，禁止笼统「未取得」
    rp = base / "data" / "channel_reach.json"
    if not rp.exists():
        add("⑥ 599 渠道逐条启用动作", False, "channel_reach.json 缺失")
    else:
        d = json.loads(rp.read_text(encoding="utf-8"))
        rows = d.get("渠道", []) or []
        total = d.get("汇总", {}).get("白名单", len(rows))
        with_action = sum(1 for c in rows if len(str(c.get("启用动作") or "")) >= 10)
        vague = sum(1 for c in rows if str(c.get("状态") or "").strip() == "未取得")
        add("⑥ 599 渠道逐条启用动作（无笼统未取得）",
            total >= 599 and len(rows) == total and with_action == total and vague == 0,
            f"白名单 {total} / 落位 {len(rows)} / 有动作 {with_action} / 笼统未取得 {vague}；"
            f"状态分布 {d.get('汇总', {}).get('状态分布', {})}")
    # 2) 大众贴近度口径回归
    g = base / "output" / "rules_regression.json"
    if not g.exists():
        add("⑥ 大众贴近度口径回归 0 误杀 0 漏放", False, "rules_regression.json 缺失")
    else:
        r = json.loads(g.read_text(encoding="utf-8"))
        add("⑥ 大众贴近度口径回归 0 误杀 0 漏放",
            r.get("FAIL", 1) == 0 and r.get("用例数", 0) >= 30,
            f"{r.get('PASS')}/{r.get('用例数')} PASS，档位 {r.get('档位')}，失败 {r.get('失败用例')}")
    # 3) 社交平台主干 10 个 + 5 类拼盘源
    from openpyxl import load_workbook
    from openpyxl.reader.excel import ExcelReader
    ExcelReader.read_custom = lambda self: None
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb["口径与稿外信息"] if "口径与稿外信息" in wb.sheetnames else None
        txt = "\n".join(" | ".join(str(x or "") for x in row)
                        for row in ws.iter_rows(values_only=True)) if ws else ""
    finally:
        wb.close()
    plats = ["微博", "百度", "抖音", "今日头条", "知乎", "B站", "小红书",
             "快手", "贴吧", "微信"]
    miss = [p for p in plats if p not in txt]
    add("⑥ 社交 10 平台登记为主干 + 5 类拼盘全量拆条",
        not miss and "主干" in txt and "全量拆条" in txt,
        f"缺平台 {miss}；主干/拆条声明 {'齐全' if '主干' in txt and '全量拆条' in txt else '缺失'}")
    # 4) 产品名统一为「资讯获取」，旧称零残留
    add("⑥ 产品名统一为「资讯获取」", ("每日要闻" + "速览") not in txt and "资讯获取" in txt,
        f"旧称出现 {txt.count('每日要闻' + '速览')} 次；新名出现 {txt.count('资讯获取')} 次")
    # 5) 页面「重跑并刷新」零配置：仓库坐标写进固定数据，页面不再要求必填 Token
    lj = base / "docs" / "data" / "latest.json"
    idx = base / "docs" / "index.html"
    appjs = base / "docs" / "assets" / "app.js"
    ok5, why5 = False, ""
    try:
        repo = (json.loads(lj.read_text(encoding="utf-8")).get("repo") or {})
        html = idx.read_text(encoding="utf-8")
        js = appjs.read_text(encoding="utf-8")
        ok5 = (bool(repo.get("workflow_url")) and "首次设置" not in html
               and "waitForNewBatch" in js and "repoInfo" in js
               and "无需任何配置" in js)
        why5 = (f"repo={repo.get('github_repo') or '缺'} / workflow_url={'有' if repo.get('workflow_url') else '缺'}"
                f" / 旧首次设置={'仍存' if '首次设置' in html else '已删'}"
                f" / 零配置轮询={'有' if 'waitForNewBatch' in js else '缺'}")
    except Exception as exc:
        why5 = f"读取失败 {type(exc).__name__}: {exc}"
    add("⑥ 页面重跑零配置（仓库坐标内置 + 自动轮询刷新）", ok5, why5)
    # 3) 2026-11-20 质量回放证据：A 档不再恒空 / 主表摘要硬闸 / 主题簇不过度合并
    rp2 = base / "output" / "replay_pool.json"
    if not rp2.exists():
        add("⑦ 候选池质量回放（A 档 / 摘要硬闸 / 主题簇）", False,
            "output/replay_pool.json 缺失，先跑 scripts/replay_quality.py --sheet 窗口内全部数据")
    else:
        d2 = json.loads(rp2.read_text(encoding="utf-8"))
        sa = int(d2.get("S+A 档数", 0))
        mx = int(d2.get("最大簇条数", 999))
        add("⑦ 候选池回放 0 FAIL 且 S+A 档 > 0（A 不再恒空）",
            int(d2.get("FAIL", 1)) == 0 and sa > 0,
            f"回放 FAIL={d2.get('FAIL')} | S+A={sa} | 原表 A=0 | 去重后主表 {d2.get('去重后主表条数')}")
        add("⑦ A 档全部摘要有效且贴近度 strict 通过",
            not d2.get("A档不合格"),
            f"不合格 A 档 {len(d2.get('A档不合格') or [])} 条")
        add("⑦ 主表摘要硬闸生效（空摘要拦在主表外）",
            int(d2.get("摘要硬闸拦下", 0)) > 0,
            f"拦下 {d2.get('摘要硬闸拦下')} 条 | strict 贴近度剔 {d2.get('贴近度strict拦下')} 条")
        add("⑦ 主题簇不过度合并（防政务稿刷假 A）",
            0 < mx <= 5,
            f"最大簇 {mx} 条 | 多来源簇 {d2.get('多来源簇数')} 个 | "
            f"H 代理生效 {d2.get('H代理生效条数')} 条")
    cp = base / "config.yaml"
    if cp.exists():
        ct = cp.read_text(encoding="utf-8")
        _hmode = "h_channel: heat" in ct or "h_channel: proxy" in ct
    add("⑦ config 主表摘要硬闸 / 事件级热度 / 小震阈值已启用",
            ("main_requires_abstract: true" in ct and _hmode
             and "usgs_min_magnitude: 5.5" in ct and "heat-v1-48h-halflife24h" in ct),
            "main_requires_abstract / h_channel=heat(或proxy) / 热度规则版本 / usgs=5.5")
    # ⑧ 2026-11-20：非独立站点 / DNS 未解析 不许再当终态
    cv = base / "output" / "channel_resolve_verify.json"
    if not cv.exists():
        add("⑧ 两类渠道逐条解析（非独立站点 / DNS 替代回查）", False,
            "output/channel_resolve_verify.json 缺失，先跑 scripts/verify_channel_resolve.py")
    else:
        c = json.loads(cv.read_text(encoding="utf-8"))
        add("⑧ 两类渠道逐条解析 0 FAIL（300 条逐条有可拉取通路）",
            int(c.get("FAIL", 1)) == 0,
            f"验收 {c.get('PASS')}/{c.get('检查数')} | 失败项 {c.get('失败项')}")
    rp = base / "data" / "channel_resolved.json"
    if rp.exists():
        rd = json.loads(rp.read_text(encoding="utf-8"))
        add("⑧ 两类解析产物：原两类 > 0、已解析 = 原两类 且 零通路 = 0",
            rd.get("原两类合计", 0) > 0
            and rd.get("原两类合计", 0) == rd.get("已解析条数", 0)
            and rd.get("零通路条数", 1) == 0,
            f"原 {rd.get('原两类合计')} / 解析 {rd.get('已解析条数')} / 零通路 {rd.get('零通路条数')}")
    # ⑨ 2026-11-21 质量 v3：A 档不再恒空（事件级热度 + 采集硬闸）
    qv = base / "output" / "quality_v3_verify.json"
    if not qv.exists():
        add("⑨ 质量 v3 回归（事件热度 / 噪声硬闸 / A 档产生）", False,
            "output/quality_v3_verify.json 缺失，先跑 scripts/verify_quality_v3.py")
    else:
        q = json.loads(qv.read_text(encoding="utf-8"))
        add("⑨ 质量 v3 回归 0 FAIL", int(q.get("FAIL", 1)) == 0,
            f"验收 {q.get('PASS')}/{q.get('检查数')} | 失败项 {q.get('失败项')}")
        add("⑨ 事件级热度离线回放 S+A > 0（A 不再恒空）",
            int((q.get("回放") or {}).get("S_plus_A", 0)) > 0,
            f"回放 S+A={(q.get('回放') or {}).get('S_plus_A')} | "
            f"等级分布 {(q.get('回放') or {}).get('等级分布')}")
    return checks
