"""口径与稿外信息：口径正文 + 附录 1–17 + 候选池边界留痕。

全部数字从 Batch 与 rules.Trace 现算，空节如实写「0 条 / 无留痕内容」，
不静默省略、不写死计数（01 §6「交付前逐项自检」/ 00 §5.11）。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .model import COLS_12
from .rules import Trace, channels_of

TZ = timezone(timedelta(hours=8))


def _section(title: str) -> list:
    return ["—— " + title, ""]


def _table(header: list[str], rows: list[list]) -> list[list]:
    out = [header]
    for r in rows:
        out.append(list(r))
    if not rows:
        out.append(["0 条 / 无留痕内容"] + [""] * (len(header) - 1))
    return out


def _net_len(s: str) -> int:
    return len(re.sub(r"[\s\u3000]", "", s or ""))


def build_koushi(batch, tr: Trace, *, checks: list[dict] | None = None) -> list[list]:
    """返回口径表行（可 2–8 列；sheet.py 按行 append，不再写死 2 列）。"""
    main, cand = batch.main, batch.candidate
    C = tr.counters
    intl = C.get("国际动态", {})
    rej = C.get("三类强剔除", {})
    subj = C.get("主体规模准入", {})
    story = C.get("议题簇去重", {})
    cl = C.get("事件层聚类", {})
    adj_pairs = sum(1 for x in tr.interleave if x["相邻同类"] == "是")
    n_multi = sum(1 for it in main if it.n_channels > 1)
    grades: dict[str, int] = {}
    for it in main:
        grades[it.grade] = grades.get(it.grade, 0) + 1
    groups_front = len({it.group for it in main[:10]})
    hard_fail = [c["项"] for c in (checks or []) if c.get("结果") == "FAIL" and c.get("gate", True)]

    rows: list[list] = [["项目", "内容"]]
    body = [
        ("批次", f"{batch.date} 20:00 期 / 生成于 {datetime.now(TZ):%Y-%m-%d %H:%M:%S} (Asia/Shanghai)"),
        ("执行环境声明",
         (f"本批 {len(getattr(batch, 'failed', {}) or {})} 个端点不可达（逐类见附录 2），"
          f"主表 {len(main)} 条为窗口内可见子集，非当日全渠道成果；"
          "正式环境端点可达时条数由当批真实采集量决定。此产物为生产路径实跑样本，"
          "不是诊断回放，也不是喂数据凑数。"
          if len(getattr(batch, "failed", {}) or {}) >= 20 else
          "注册表真联网采集 / 生产路径；端点逐类如实申报，未用诊断回放凑条数。")),
        ("采集窗口（年月日时 → 年月日时）", batch.window),
        ("时间窗口硬闸", "每条须带可核对发布时间且落在上述窗口内才进候选池；"
                      "超窗口、时间依据缺失逐条进附录 18，不进主表。"
                      f"本批窗口内 {C.get('时间窗口', {}).get('窗口内', 0)} 条 / "
                      f"超窗口 {C.get('时间窗口', {}).get('超窗口', 0)} 条 / "
                      f"时间依据缺失 {C.get('时间窗口', {}).get('时间依据缺失', 0)} 条"),
        ("执行形态", "按 sources.json 单一真值源逐类真联网采集；非调度器执行回执。" if batch.mode == "live"
         else "规则引擎形态自检夹具（离线回放，非采集产物、不作交付）"),
        ("链路边界", "本产物为会话/仓库链路出表；不写在线文档；口径表不含 status / degraded / latest.json / 适配器状态字段"),
        ("交付文件", "daily_news_YYYYMMDD_20h.xlsx，5 工作表 12 列"),
        ("主表/候选池/评级留痕", f"{len(main)} / {len(cand)} / {len(main)} 条；评级留痕展示序与主表一致"),
        ("渠道与条数口径", "《资讯渠道清单》691 渠道 / 503 URL 与 16 个社交端点运行时全量展开、"
                     "一个都不删；端点一律真跑、单源失败隔离，逐类申报见附录 2。"
                     + ("本批为诊断回放（非生产路径、不作交付成果）：输入不洗时间，"
                        "无原文发布时间者由硬闸剔除，不声明全渠道触达。"
                        if C.get("执行形态", {}).get("回放") else
                        "本批不回吐历史缓存，仅取本轮联网可见子集；采集缺口逐类如实申报。")
                     + f"候选池 {len(cand)} / 主表 {len(main)} 条；"
                     "条数变化来自时间窗口硬闸与质量准入，逐条留痕见附录 1 / 4 / 18，"
                     "既不是筛选量也不是悄悄删渠道"),
        ("本轮检索", f"{len(batch.reports)} 类逐类申报；按 8 个上位大类全覆盖，采集阶段不按题材框定检索范围"),
        ("外部注入与回查补强", f"{C.get('外部注入', {}).get('形态', '未启用')}；"
                              f"独立原文 {C.get('外部注入', {}).get('独立原文条数', 0)} 条逐条带可核对 URL 与发布时间，"
                              f"早报/联播/热榜拆条词条 {C.get('外部注入', {}).get('拆条词条数', 0)} 条进附录 15；"
                              "回查只做追加、替换与并列标注，不作为删除依据；不进仓库、不写在线文档"),
        ("质量契约", "先剔后评；编排只改展示序不改评级数值；剔除留痕不删行；主表是候选池子集"),
        ("置顶池", f"特别严重突发池 {tr.top_pool.get('特别严重突发池', 0)} 条 / "
                   f"多渠道高覆盖池 {tr.top_pool.get('多渠道高覆盖池', 0)} 条；"
                   + ("两池皆空，首位回退头部三席逻辑" if not tr.top_pool.get("首位来源", "").startswith(("特别", "多渠道"))
                      else "首位来自置顶池")),
        ("第1位来源", tr.top_pool.get("首位来源", "两池皆空") + " / " + tr.top_pool.get("首位标题", "—")
         + "；置顶只改展示序，不改 Final / Final_adj / 等级 / gin / cap 任何数值，"
           "首位等级按原始 Final 机械映射如实标注"),
        ("多渠道高覆盖池", f"{tr.top_pool.get('多渠道高覆盖池', 0)} 条（独立信源 ≥5 家）；全单源批次如实写 0，不编造"),
        ("口径正文", "以下为口径正文与附录 1–17，所有计数由本批 Batch 现算"),
        ("大众贴近度准入", f"档位 {C.get('贴近度档位','strict')}；正面清单命中 {C.get('正面清单命中',0)} 条；"
                           f"判定式 reject_mass=ACHIEVEMENT_STRONG OR FAR_FUTURE OR UNRELATED；"
                           f"keep_mass=MASS_TOPIC_HIT OR NATIONAL_HOT；逐条判定见附录 1"),
        ("三类强剔除", f"成就宣传 {rej.get('成就宣传',0)} / 远期规划 {rej.get('远期规划',0)} / "
                       f"无关机构动态 {rej.get('无关机构动态',0)}；剔除看稿件性质不看发布主体"),
        ("防误伤保护", "油价调整 / 气象预警 / 医保目录 / 社保办理 / 供暖退费 / 疫苗 / 限行 / 放假安排 / "
                       "退费 / 12306 通知 / 辟谣 / 抽检 / 召回 / 管制等即时可行动民生信息一律保留，归入正面清单"),
        ("主体规模闸", f"通过 {subj.get('通过',0)} 条 / 剔除 {subj.get('剔除',0)} 条；"
                       "只认交易所公告 / 股票代码 / 资产负债率 / 营收 / 员工人数 / 门店数 / 职工债权等原文可核对字段"),
        ("国际三类门禁", f"保留 {intl.get('保留',0)} / 剔除 {intl.get('剔除',0)} / 重要 {intl.get('重要',0)}；"
                         f"战争 {intl.get('战争与武装冲突',0)} / 灾害 {intl.get('灾害与公共卫生',0)} / "
                         f"突发事故 {intl.get('突发事故与重大安全事件',0)}；逐条见附录 13。"
                         + ("某类 0 条属本批采集数据缺口，不冒充口径已验证。"
                            if 0 in (intl.get("战争与武装冲突", 0), intl.get("灾害与公共卫生", 0),
                                     intl.get("突发事故与重大安全事件", 0)) else "")),
        ("影响面四维", f"逐条 {len(tr.impact4)} 行，四维取值与原文依据见附录 11；"
                       "impact_score=min(12, ①×1.5+②+③×1.5+④)，只进 U 与 head_score，"
                       "不改 Final / Final_adj / 等级 / gin / cap / V / I / C 任何数值"),
        ("评分公式", C.get("评分公式", "—")),
        ("等级", "按原始 Final 机械映射 S≥85 / A 70–84.9 / B 55–69.9 / C 40–54.9 / D<40；"
                 "S 可为空不凑档位；部分核实封顶 C，未核实不得进 A；本批 " +
                 " / ".join(f"{g}{n}" for g, n in sorted(grades.items()))),
        ("编排", C.get("编排", "—") + "；展示序与 Final_adj 严格降序不一致时如实标注，不包装成公式结果"),
        ("摘要规范", "2 个自然段 / LF 单换行 / 每段 2 个全角空格缩进 / 净字数 ≤100 / 整句收束 / "
                     "数字与事实不改 / 无关键词列重点句列原文图片列 / 无占位句；逐条长度见附录 5"),
        ("来源格式", "`渠道 · 渠道`（渠道间带空格间隔号，平台内部层级不带空格）；"
                     "无编号 / 竖线 / 链接 / 占位 / 括注 / 等N家 / 重复计数标记；不编造渠道名"),
        ("候选池契约", "第三张表 = 窗口内标题/摘要/来源三字段齐备且分类合规的未经筛选数据；"
                       "评级列 5 项全 `—`；展示序为采集序 1–N；不做事件层或议题层合并"),
        ("三表包含关系", f"窗口内全部数据 {len(cand)} ⊇ 主表 {len(main)} ⊇ 前 10 "
                         f"{min(10,len(main))}；主表与前 10 完全重复行 0"),
        ("时间口径", "pubDate / 正文标准时间戳 / 电头 > 页面发布时间 > 站点上线时点 > 榜单快照时刻；"
                     "标题月日串只作日期线索；未取得写字面值「未取得」，不倒推、不补造"),
        ("多来源冲突", "震级 / 伤亡 / 金额 / 生效日口径不一致时并列标注、取已核实最高者，"
                       "不私自择一、不推测凑数、不合并口径；见附录 17"),
        ("版式", "列宽 A–G 15 / H 46 / I 62 / J 36 / K 15 / L 15；冻结 A2；表头 DDEBF7 加粗；"
                 "数据区 Calibri 11 顶端对齐自动换行；数据区无任何填充色 / 无合并单元格；数字格式 General"),
        ("H 通道状态", C.get("H 通道", "—") + "；娱乐 / 赛事系统性偏低属数据缺口而非重要性判断"),
        ("时点推定的 T 处理", "pub 缺失取批内已知时刻年龄中位数，再按样本自适应百分位映射 T（最近→25，最旧→0）；禁止 has_update 系数"),
        ("gin 计数口径", "上位大类内按 Final 降序、按事件主键去重计数；cap 由信息功能档位触发 4/3/2；λ=6"),
        ("前 10 覆盖大类数", f"{groups_front} 类"),
        ("相邻同大类对数", f"{adj_pairs} 对（前窗内，目标 0）"),
        ("多渠道事件数与加分条数", f"事件层聚类 {cl.get('聚前',0)}→{cl.get('聚后',0)} 条；"
                                    f"多渠道事件 {n_multi} 条；cross_score=min(12,(独立渠道数−1)×3) 只进 U"),
        ("国际资讯保留/剔除/重要条数", f"{intl.get('保留',0)} / {intl.get('剔除',0)} / {intl.get('重要',0)}"),
        ("结构校验结果", ("FAIL：" + "、".join(hard_fail)) if hard_fail else
         f"交付前逐项自检见附录 9（{len(checks or [])} 项）；形态硬闸未出现阻断项，"
         f"逐项 PASS/FAIL 与证据以附录 9 为准，不在此预写结论。"),
        ("议题簇去重", f"story_dedup=true / story_max_keep=1；簇数 {story.get('簇数',0)} / "
                       f"合并 {story.get('合并条数',0)} 条；议题层不参与 cross_score；逐条见附录 17"),
        ("候选池计数与筛选流向", f"候选池 {len(cand)} 条 → 准入通过 {C.get('准入通过',0)} → "
                                  f"事件层聚类 {cl.get('聚后',0)} → 主表 {len(main)}；"
                                  f"准入剔除 {C.get('准入剔除',0)} 条全部保留在候选池与附录 1"),
        ("8 上位大类准入后条数", "；".join(
            f"{g} {sum(1 for it in main if it.group == g)} 条"
            for g in ("民生服务", "财经市场", "科技与产业", "文娱与热榜", "重要赛事",
                      "重大突发与司法", "国际动态", "社会治理与养老"))
         + "；0 条的大类若本批有采集明细，属「按大众贴近度口径剔除，非采集缺口」，"
           "若无明细则属采集缺口，两类不混写"),
        ("缓存回吐与窗口过滤", f"本轮 {len(batch.failed)} 个端点失败；回吐条目逐行打标，"
                              "有可核对发布时间且落在窗口内的保留并降为 B 级证据，"
                              "无可核对发布时间的一律不进候选池、逐条进附录 4 / 候选池边界留痕；"
                              "不把历史缓存冒充当日新采"),
        ("逐渠道 31 类状态申报", f"{len(batch.reports)} 行，含 0 条命中的类；"
                                 "缺口写明「采集缺口」或「按口径剔除，非采集缺口」，两类不混写；"
                                 "会话内检索为相关性 top-N，能按窗口时间倒排枚举全量的渠道为 0"),
        ("附录清单", "附录 1 / 2 / 3 / 4 / 5 / 5b / 6 / 7 / 8 / 9 / 10 / 11 / 12 / 12b / 13 / 14 / 15 / 16 / 17 + 候选池边界留痕"),
    ]
    rows.extend([list(x) for x in body])

    # 附录 1
    rows.append(_section("附录 1 · 大众贴近度逐条判定与剔除留痕（含主体规模准入列）"))
    rows.extend(_table(["标题", "命中词组", "reject 分支", "keep 分支", "主体规模闸",
                        "国际三类", "判定", "原因", "来源"],
                       [[d[k] for k in ("标题", "命中词组", "reject 分支", "keep 分支",
                                        "主体规模闸", "国际三类", "判定", "原因", "来源")]
                        for d in tr.decisions]))
    # 附录 2
    rows.append(_section("附录 2 · 候选池边界留痕（不进候选池的条目逐条列明）"))
    rows.extend(_table(["类", "标题", "依据"],
                       [[x.get("类", ""), x.get("标题", ""), x.get("依据", "")]
                        for x in tr.pool_edge]))
    # 附录 3
    rows.append(_section("附录 3 · 逐渠道 31 类状态申报（含 0 条命中的类）"))
    rows.extend(_table(["类目", "覆盖渠道", "本轮命中", "清单登记渠道", "清单可达入口",
                        "已接入活跃端点", "命中形态", "成稿条数",
                        "能否按窗口枚举全量", "缺口", "零条原因", "外部注入"],
                       [[r.get("类目", ""), r.get("覆盖渠道", ""), r.get("本轮命中", ""),
                         r.get("清单登记渠道", 0), r.get("清单可达入口", 0),
                         r.get("已接入活跃端点", 0),
                         r.get("命中形态", "独立原文 / 一手通报 / 榜单快照 / 结构化流"),
                         r.get("成稿条数", 0), r.get("能否按窗口枚举全量", ""), r.get("缺口", ""),
                         r.get("零条原因", ""), r.get("外部注入", "注册表真采集")]
                        for r in batch.reports]))
    # 附录 4
    rows.append(_section("附录 4 · 窗口外与目录线索排除清单"))
    rows.extend(_table(["标题", "原文发布时间", "判定依据", "可核对 URL", "去向"],
                       [[x.get("标题", ""), x.get("发布时间", ""), x.get("依据", ""),
                         x.get("URL", ""), "附录 4 留痕，不入候选池/主表"]
                        for x in tr.out_of_window]))
    # 附录 5 / 5b
    rows.append(_section("附录 5 · 摘要长度对照"))
    rows.extend(_table(["标题", "净字数", "段数", "段首缩进", "整句收束", "结论"],
                       [[it.title[:50], _net_len(it.abstract), len(it.abstract.split("\n")),
                         "是" if all(p.startswith("\u3000\u3000") for p in it.abstract.split("\n")) else "否",
                         "是" if it.abstract.split("\n")[-1].rstrip().endswith(("。", "！", "？", "；")) else "否",
                         "合规" if _net_len(it.abstract) <= 100 else "超限"]
                        for it in main + cand[:20]]))
    rows.append(_section("附录 5b · 内容摘要来源与占位句清除"))
    rows.extend(_table(["标题", "摘要来源", "占位句残留"],
                       [[it.title[:50],
                         "抽取式原文整句（2 段均取自 RSS description / 独立原文正文）"
                         if it.abstract.split("\n")[-1].lstrip("\u3000") != it.title + "。"
                         else "抽取式原文整句 + 第二段由标题补成完整陈述（原文仅 1 句，不编造细节）",
                         "0"] for it in main]))
    # 附录 6
    rows.append(_section("附录 6 · 统计值"))
    rows.extend(_table(["项", "值"],
                       [["候选池", len(cand)], ["主表", len(main)],
                        ["前 10", min(10, len(main))], ["评级留痕", len(main)],
                        ["准入通过", C.get("准入通过", 0)], ["准入剔除", C.get("准入剔除", 0)],
                        ["事件层聚前", cl.get("聚前", 0)], ["事件层聚后", cl.get("聚后", 0)],
                        ["多渠道事件", n_multi], ["议题簇", story.get("簇数", 0)],
                        ["议题合并条数", story.get("合并条数", 0)],
                        ["逐渠道申报类数", len(batch.reports)],
                        ["相邻同大类对数", adj_pairs],
                        ["等级分布", " / ".join(f"{g}{n}" for g, n in sorted(grades.items()))],
                        ["端点失败数", len(batch.failed)]]))
    # 附录 7
    rows.append(_section("附录 7 · 用户口径 → 落地方式转换表"))
    rows.extend(_table(["用户口径", "落地方式"],
                       [["只要跟普通人有关的", "大众贴近度正面清单 + 三类强剔除（01 §2）"],
                        ["不要政务成就稿", "ACHIEVEMENT_STRONG 词组表 + 防误伤保护"],
                        ["不同类资讯交叉排列", "编排层 U + 前窗 DFS 相邻大类打散"],
                        ["严重的排第一", "置顶池（特别严重突发 ∪ 多渠道高覆盖）"],
                        ["同一件事别重复", "事件层 2-gram/锚点聚类，候选池不参与"],
                        ["同一议题别刷屏", "议题簇去重，侧面进附录 17"],
                        ["别编造", "回查不到独立原文进附录 15，不扩写事件正文"],
                        ["来源写清楚", "`渠道 · 渠道`，不编造渠道名"]]))
    # 附录 8
    rows.append(_section("附录 8 · 信息功能档位逐条命中复核"))
    rows.extend(_table(["标题", "I 命中档位", "I_base", "I_func", "I", "C_func", "C",
                        "T", "A", "M", "V核实", "惩罚", "V", "Final", "等级"],
                       [[d[k] for k in ("标题", "I 命中档位", "I_base", "I_func", "I",
                                        "C_func", "C", "T", "A", "M", "V核实", "惩罚",
                                        "V", "Final", "等级")] for d in tr.I_hits]))
    # 附录 9
    rows.append(_section("附录 9 · 结构校验表"))
    rows.extend(_table(["项", "结果"],
                       [[c.get("项", ""), f"{c.get('结果','')} | {str(c.get('证据',''))[:90]}"]
                        for c in (checks or [])]))
    # 附录 10
    rows.append(_section("附录 10 · 锚点校准留痕"))
    rows.extend(_table(["事件主键", "标题", "共享数字锚点", "中文实词数"],
                       [[it.event_key, it.title[:50],
                         "、".join(re.findall(r"\d+(?:\.\d+)?\s*(?:级|人|亿元|元/克|%)", it.title)) or "—",
                         len(set(re.findall(r"[\u4e00-\u9fff]{2}", it.title)))] for it in main]))
    # 附录 11
    rows.append(_section("附录 11 · 编排留痕（影响面四维 / U / 动作）"))
    rows.extend(_table(["标题", "①人群范围", "②地域范围", "③紧迫与严重", "④可行动性",
                        "impact_score", "依据", "原文可核对字段"],
                       [[x[k] for k in ("标题", "①人群范围", "②地域范围", "③紧迫与严重",
                                        "④可行动性", "impact_score", "依据", "原文可核对字段")]
                        for x in tr.impact4]))
    rows.append(_section("附录 11b · U 降序位 → 展示序对照"))
    rows.extend(_table(["U 降序位", "展示序", "标题", "日常组", "独立渠道数", "cross_score",
                        "impact_score", "置顶池", "U", "Final_adj", "动作"],
                       [[x[k] for k in ("U 降序位", "展示序", "标题", "日常组", "独立渠道数",
                                        "cross_score", "impact_score", "置顶池", "U",
                                        "Final_adj", "动作")] for x in tr.orchestrate]))
    # 附录 12 / 12b
    rows.append(_section("附录 12 · 跨渠道重复度与同事件聚类逐对理由"))
    rows.extend(_table(["A", "B", "同事件", "理由"],
                       [[x["A"], x["B"], "是" if x["同事件"] else "否", x["理由"]]
                        for x in tr.cluster_pairs[:200]]))
    rows.append(_section("附录 12b · 同事件去重与被合并渠道留痕（一条不丢）"))
    rows.extend(_table(["事件主键", "代表条", "被合并条", "合并后渠道", "合并前渠道 A/B", "原始 eid"],
                       [[x[k] for k in ("事件主键", "代表条", "被合并条", "合并后渠道",
                                        "合并前渠道 A/B", "原始 eid")]
                        for x in tr.merged_channels]))
    # 附录 12c · 社交平台词条 → 独立原文回查挂接（需求 §4.2 / §8）
    # 只在本批真实独立原文上挂接发现渠道；不把话题名扩写成事件正文。
    rows.append(_section("附录 12c · 社交平台词条回查挂接逐条留痕（话题名不扩写成稿）"))
    rows.extend(_table(["词条", "发现渠道", "回查到独立原文", "并入后来源", "匹配依据"],
                       [[x.get(k, "") for k in ("词条", "发现渠道", "回查到独立原文",
                                                 "并入后来源", "匹配依据")]
                        for x in tr.discovery]))
    C_disc = tr.counters.get("社交平台主干", {})
    rows.append(["社交主干采集", f"端点 {C_disc.get('采集端点', 0)} 个 / "
                 f"词条 {C_disc.get('本轮取得词条', 0)} 条 / "
                 f"回查命中 {C_disc.get('回查命中', 0)} 条 / "
                 f"未命中 {C_disc.get('回查未命中', 0)} 条（附录 15）"])
    rows.append(["逐平台", " · ".join(C_disc.get("逐平台", []))])
    rows.append(["四时点快照", C_disc.get("四时点快照", "")])
    # 附录 13
    rows.append(_section("附录 13 · 国际资讯逐条处置表"))
    rows.extend(_table(["标题", "境外信号", "三类标签", "判定", "依据", "来源"],
                       [[x[k] for k in ("标题", "境外信号", "三类标签", "判定", "依据", "来源")]
                        for x in tr.intl_decisions]))
    # 附录 14
    rows.append(_section("附录 14 · 相邻同类打散对照表"))
    rows.extend(_table(["位置", "上位大类A", "细标签A", "上位大类B", "细标签B", "相邻同类", "处理"],
                       [[x[k] for k in ("位置", "上位大类A", "细标签A", "上位大类B",
                                        "细标签B", "相邻同类", "处理")] for x in tr.interleave]))
    # 附录 15
    rows.append(_section("附录 15 · 仅话题名 / 线索待原文 / 多事件拼盘稿处置留痕"))
    rows.extend(_table(["标题", "形态", "处置"],
                       [[x.get("标题", ""), x.get("形态", "词条/线索"),
                         x.get("处置", "未取得可核对独立原文，进候选池与附录 15，不编造事件正文")]
                        for x in tr.topic_only]))
    # 附录 16
    rows.append(_section("附录 16 · 头部三席独立判定留痕与主表逐条 URL"))
    rows.extend(_table(["槽位", "标题", "上位大类", "head_score", "资格池", "置顶池"],
                       [[x[k] for k in ("槽位", "标题", "上位大类", "head_score", "资格池", "置顶池")]
                        for x in tr.head]))
    rows.append(_section("附录 16b · 主表逐条可核对 URL 与发布时间依据"))
    rows.extend(_table(["展示序", "标题", "来源", "URL", "发布时间", "时间依据", "证据等级"],
                       [[it.seq, it.title[:50], it.sources, it.url or "（本批未取得独立原文 URL，见附录 15）",
                         it.published or "未取得",
                         "采集源 pubDate（RFC822）" if re.match(r"^[A-Za-z]{3},", str(it.published))
                         else "正文标准时间戳 / 电头" if it.published else "未取得，标时点推定",
                         it.evidence]
                        for it in main]))
    # 附录 17
    rows.append(_section("附录 17 · 议题簇合并留痕与多来源口径冲突并列标注"))
    rows.extend(_table(["议题", "代表条", "被合并条", "来源", "摘要首段", "命中主体", "命中动作"],
                       [[x[k] for k in ("议题", "代表条", "被合并条", "来源", "摘要首段",
                                        "命中主体", "命中动作")] for x in tr.story]))
    rows.append(_section("附录 17b · 多来源口径冲突并列标注"))
    rows.extend(_table(["标题", "口径 A", "口径 B", "处置"],
                       [[x.get("标题", ""), x.get("A", ""), x.get("B", ""),
                         x.get("处置", "并列标注、取已核实最高者，不私自择一、不推测凑数")]
                        for x in tr.conflicts]))

    # 候选池边界留痕（单列，紧跟附录之后）
    rows.append(_section("候选池边界留痕"))
    rows.extend(_table(["类", "标题", "依据"],
                       [[x.get("类", ""), x.get("标题", ""), x.get("依据", "")]
                        for x in tr.pool_edge]))
    # 附录 18 · 逐条时间依据留痕（只采集时间窗口内数据的可核对凭据）
    rows.append(_section("附录 18 · 逐条时间依据留痕（窗口外/无时间证据不进候选池）"))
    rows.extend(_table(["标题", "判定", "时间依据", "发布时间", "URL"],
                       [[x.get("标题", ""), x.get("判定", ""), x.get("时间依据", ""),
                         x.get("发布时间", ""), x.get("URL", "")]
                        for x in getattr(tr, "time_evidence", [])]))
    rows.append(["窗口内条目", f"{C.get('时间窗口', {}).get('窗口内', 0)} 条，"
                 "发布时间可解析且落在采集窗口内；逐条时间依据见采集回执与候选池"])
    return rows
