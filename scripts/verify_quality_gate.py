# -*- coding: utf-8 -*-
"""契约三闸回归（2026-11-22 用户三张截图）。

用截图里的**真实行**做负例，用目标样例里的合规行做正例，全部真调运行时函数。
输出 output/quality_gate_verify.json，任一 FAIL 即 rc=1。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from news_pipeline import quality_gate as qg          # noqa: E402
from news_pipeline import event_composer as ec        # noqa: E402
from news_pipeline.model import RawItem               # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append({"项": name, "结果": "PASS" if cond else "FAIL", "明细": str(detail)[:200]})
    return bool(cond)


def mk(title, abstract, source="", group="民生服务", tag="综合", record_type="联网检索独立原文"):
    return RawItem(title=title, abstract=abstract, source=source, group=group,
                   tag=tag, record_type=record_type, has_event=True,
                   abstract_status="有效", seq=1)


# ---------------------------------------------------------------- 截图1：跨事件串味
S1_TITLE = "证券行情 A股11月19日：沪指收3577.62点跌0.28% 创业板指跌0.79%"
S1_ABSTRACT = ("临平区劳动人事争议仲裁院公告，魏樱樱、陈华丽与杭州伯喜服饰有限公司劳动争议二案11月19日开庭。"
               "创业板指收报2293.34点，跌0.79%，成交3101.21亿元；交易所行情")
S1_SOURCES = "青岛市交通运输局 · 青岛政务网 · 股票数据库 · 交易所行情 · 杭州市临平区人民政府"

it1 = mk(S1_TITLE, S1_ABSTRACT, S1_SOURCES, "财经市场", "证券行情")
mis, mwhy = qg.headline_abstract_mismatch(S1_TITLE, S1_ABSTRACT)
check("截图1 标题↔摘要错配被识别", mis, mwhy)
cok, gate, cwhy = qg.contract_check(it1)
check("截图1 契约层拦截该行", not cok and gate == "标题摘要错配", f"{gate}: {cwhy}")

a = mk(S1_TITLE, "11月19日沪指收报3577.62点，跌0.28%；创业板指跌0.79%。", "股票数据库 · 交易所行情",
       "财经市场", "证券行情")
b = mk("临平区劳动人事争议仲裁院公告，魏樱樱、陈华丽与杭州伯喜服饰有限公司劳动争议二案11月19日开庭",
       "两申请人分别要求支付未按约支付的补偿金21000元、24497元。", "杭州市临平区人民政府",
       "社会治理与养老", "就业与劳动关系")
groups, traces = ec.cluster([a, b], max_cluster=6)
check("截图1 两个不同事件不再并成一簇", len(groups) == 2,
      f"簇数={len(groups)}；判据={[t.get('判据') for t in traces[-2:]]}")
blk, bwhy = qg.pair_cannot_link(a, b)
check("截图1 cannot-link 硬否决生效", blk, bwhy)

mixed = ec.compose_abstract([a, b], limit=100, rep_title=a.title)
check("截图1 摘要不含临平劳动争议内容",
      all(w not in mixed for w in ("临平", "伯喜服饰", "魏樱樱")), mixed[:90])
ev = ec.build_event([a, b], seq=1, cfg={"event_composer": {"max_sources": 6}})
check("截图1 来源列不含不相干政府主体",
      "杭州市临平区人民政府" not in ev.source and "青岛" not in ev.source, ev.source)
check("日期数字不作合并锚点",
      qg.date_only_anchor({"11", "19", "20", "2026"}) and
      not qg.date_only_anchor({"3577.62", "0.28"}))

# ---------------------------------------------------------------- 截图2：匿名破产主体
S2 = mk("缙云法院受理某光伏企业破产清算 债权申报12月18日截止",
        "缙云县人民法院公告（2026）浙1122破18号：11月2日裁定受理某光伏科技公司破产清算。"
        "11月11日指定管理人，债权人应于12月18日前申报债权，12月23日召开首次债权人会议。",
        "微信公众号 · 车链法观", "财经市场", "企业倒闭 / 资不抵债")
bok, bkind, bwhy = qg.bankruptcy_contract(S2)
check("截图2 匿名「某光伏企业」+ 弱来源被拦", not bok, f"{bkind}: {bwhy}")
cok2, gate2, _ = qg.contract_check(S2)
check("截图2 契约层按破产主体闸拦截", not cok2 and gate2 == "破产主体不可核对", gate2)
LISTED = mk("中国华君（00377.HK）公告：华君地产集团被申请破产清算",
            "法院10月21日受理、11月5日指定管理人；公司称预期不会对整体经营产生重大不利影响。",
            "东方财富网 · 21财经 · 港交所公告", "财经市场", "企业倒闭 / 资不抵债")
bok3, bkind3, _ = qg.bankruptcy_contract(LISTED)
check("截图2 反例：带股票代码的上市主体通过", bok3, bkind3)
check("法院裸「公告」不再被当上市信号", "公告" not in __import__(
    "news_pipeline.rules", fromlist=["LISTED_SIGNALS"]).LISTED_SIGNALS)

# ---------------------------------------------------------------- 截图3：社交话题花边 / 占位
PLACE = ("百度热搜第6、抖音热榜前列；。该词条来自当日榜单快照与聚合日报，"
         "按未经筛选契约保留，未作贴近度判定。")
G1 = mk("深圳市民在街头偶遇各国政要", PLACE,
        "百度 · 热搜 · 抖音 · 热点 · 老杜评舆", "文娱与热榜", "社交话题", "联网检索独立原文")
G2 = mk("特朗普会见普京时打趣问「这是谁」",
        "云合微博热搜 15:35 榜第7、头条热榜前列，三国元首座驾同框的现场互动引发围观。"
        "该词条来自当日榜单快照与聚合日报，按未经筛选契约保留，未作贴近度判定。",
        "微博 · 热搜 · 今日头条 · 热榜 · 云合数据", "文娱与热榜", "社交话题")
sok1, swhy1 = qg.social_topic_contract(G1)
sok2, swhy2 = qg.social_topic_contract(G2)
check("截图3 政要偶遇花边被拦", not sok1, swhy1)
check("截图3 政要打趣花边被拦", not sok2, swhy2)
ph, phwhy = qg.placeholder_verdict(PLACE)
check("截图3 占位摘要不算「摘要齐备」", ph, phwhy)
check("截图3 两行契约层均拦截",
      not qg.contract_check(G1)[0] and not qg.contract_check(G2)[0])
# 无占位文本、只有花边措辞，也要被「社交话题」闸拦住
G3 = mk("普京到访深圳高校演讲 多国政要现身深圳街头",
        "抖音热榜显示，深圳市民在街头偶遇各国政要，澳大利亚总理现身超市试吃和牛，"
        "现场群众纷纷合影打卡，相关话题讨论量持续走高。",
        "抖音·热点 · 百度·热搜 · 老杜评舆", "文娱与热榜", "社交话题")
sok3, swhy3 = qg.social_topic_contract(G3)
check("截图3 无占位文本的花边同样被拦（社交话题闸）",
      not sok3 and "花边" in swhy3, swhy3)

# 目标样例里的合规社交成稿行（不得误杀）
GOOD = [
    mk("「养老保险缴够15年就能退休」系谣言 人社局辟谣",
       "厦门市人社局辟谣网传「缴够15年即可坐等退休」：自2030年起最低缴费年限每年提高6个月、逐步升至20年。"
       "1970年后出生男性等三类人群尤其需继续缴费，「协商不缴」的约定违法且无效。",
       "百度·热搜 · 厦门市人民政府 · 西奥说", "民生服务", "医保社保", "社交词条回查成稿"),
    mk("女子3年盒马下单400余次「仅退款」 涉1.7万元被刑拘",
       "西安公安11月17日通报，一女子三年下单400余次、虚构口味不好等理由申请仅退款，商品全留下。"
       "累计造成盒马损失1.7万余元，因涉嫌诈骗已被经开分局依法刑事拘留。",
       "微博·热搜 · 人民网上海 · 今日头条", "社会治理与养老", "消费维权", "社交词条回查成稿"),
    mk("3岁女童被生父女友捂死案一审判无期 生母申请限制减刑",
       "11月18日，苏州中院一审以故意杀人罪判处被告人邱某无期徒刑，剥夺政治权利终身。"
       "孩子生母表示将提起抗诉并申请限制减刑，被告作案时系孕妇，依法不适用死刑。",
       "微博·热搜 · 极目新闻 · 新民晚报", "社会治理与养老", "社会议题", "社交词条回查成稿"),
    mk("医保钱包2.0全部上线 380个统筹地区可代缴居民医保费",
       "国家医保局等推进职工医保个人账户跨省共济，医保钱包2.0版已在各省上线，共济范围扩至近亲属。"
       "截至11月15日，380个统筹地区支持代缴居民医保费，累计141.09万人次、5.4亿元。",
       "央视财经 · 微博·法治资讯 · 国家医保局", "民生服务", "医保 / 社保"),
]
for i, g in enumerate(GOOD, 1):
    ok, gate, why = qg.contract_check(g)
    check(f"目标样例合规行 {i} 不被误杀", ok, f"{gate}: {why}")

# 普通新闻不得被三闸误伤
NORMAL = mk("11月LPR报价出炉 5年期和1年期利率均维持不变",
            "中国央行将一年期和五年期贷款市场报价利率（LPR）分别维持在3%和3.5%不变，为连续18个月保持不变。",
            "央视网新闻", "财经市场", "财经市场 / 房地产 / 地方债务")
check("普通财经新闻通过三闸", qg.contract_check(NORMAL)[0], qg.contract_check(NORMAL)[2])

# ------------------------------------------------- 拦过头回归（2026-11-22 复验发现）
# 上一版把「数字锚点不重合」当硬闸，误杀了目标样例里三条同事件分项/背景数字；
# 又把 `Item.text()`（仅 title+abstract）用于破产主体判定，看不到来源列的权威信源，
# 误杀了带「港交所公告 / 东方财富网 / 21财经」的上市主体。以下逐条锁死不许复发。
FALSE_POSITIVES = [
    ("地方债汇总↔分项数字不重合仍属同事件",
     "11月19日全国发行16只地方政府债 合计230.228亿元",
     "　　其中一般债3只89.03亿元、专项债13只141.20亿元；。",
     "21财经 · 腾讯证券 · 地方政府公开发债数据", "财经市场", "综合", "联网检索独立原文"),
    ("A股沪指↔创业板分项与跨源并列不判错配",
     "A股11月19日：沪指收3577.62点跌0.28% 创业板指跌0.79%",
     "　　创业板指收报2293.34点，跌0.79%，成交3101.21亿元。\n"
     "　　11月19日上证指数收报3577.62点，跌0.28%，成交6369.09亿元；交易所行情记3101.21亿元、股票数据库记636。",
     "股票数据库 · 交易所行情", "财经市场", "综合", "联网检索独立原文"),
    ("电池结论数字↔背景数字不判错配",
     "首批电车电池脱保潮：换电池8万元 车残值仅3万元",
     "　　36氪11月19日报道，2025—2032年近2000万辆新能源车将跨过电池质保线。",
     "36氪 · 汽车公社 · 车质网", "科技与产业", "新能源", "社交词条回查成稿"),
    ("带权威多源的上市主体不得因首句丢失被误杀",
     "中国华君附属华君地产被申请破产清算",
     "　　法院10月21日受理、11月5日指定管理人；。\n"
     "　　公司称预期不会对整体经营及偿债能力产生重大不利影响。",
     "东方财富网 · 21财经 · 港交所公告", "财经市场", "企业倒闭 / 资不抵债", "联网检索独立原文"),
    ("医药/CRO 成本属用户点名的工厂制药医药议题",
     "实验猴报价涨至30万元一只 头部CRO存栏约9万只",
     "界面新闻调查，实验猴现货报价已从去年底10万元涨至30万元，订单排到明年春节后。",
     "微博 · 热搜 · 抖音 · 热点 · 界面新闻 · 新浪财经", "科技与产业", "制药与医药", "社交词条回查成稿"),
    ("中文数量词「一名中国公民遇害」也算可核验证据",
     "尼日利亚东北部袭击致一名中国公民遇害 使馆发布安全提醒",
     "新华社11月17日电，尼日利亚包奇州11月14日发生袭击，一名中国公民遇害、一名警察殉职。",
     "百度 · 热搜 · 新华社 · 中国驻尼日利亚使馆", "国际动态", "突发事故", "社交词条回查成稿"),
]
for name, t, a, s, g, tag, rt in FALSE_POSITIVES:
    it = mk(t, a, s, g, tag, rt)
    ok, gate, why = qg.contract_check(it)
    check(f"误伤回归：{name}", ok, f"{gate}: {why}")

# 机构会务动态仍必须被拦（用户①「国家机构动态统统不要」）
for t, a in [
    ("APEC第33次领导人非正式会议在深圳举行 中美元首会晤",
     "11月18日至19日APEC第三十三次领导人非正式会议在深圳举行，中美元首在深圳会晤并发表和平宣言。"),
    ("习近平主持APEC第33次领导人非正式会议并发表重要讲话",
     "澎湃热点榜首为会议详讯，属会务与外事动态。"),
]:
    it = mk(t, a, "央视网 · 百度 · 热搜 · 微博 · 抖音", "国际动态", "社交话题")
    ok, gate, why = qg.contract_check(it)
    check("机构会务动态仍被拦", not ok, f"{gate}: {why}")

# ------------------------------- 线上真实批次回归（用户上传 latest.xlsx，2026-11-22）
# 用户指出「跑出来的数据跟你自测的不一样」：此前所有回归用的是目标样例语料，
# 而线上是央视网单源时间倒排批次。把用户上传的 246 行候选池 + 23 行主表抽成
# 随包语料，逐行真调运行时函数，锁死本轮修复不回退。
import collections  # noqa: E402
from pathlib import Path  # noqa: E402
from news_pipeline import rules as _rules  # noqa: E402
from news_pipeline.normalize import polish_abstract  # noqa: E402

CORPUS = ROOT / "data" / "latest_batch_regression.json"
if not CORPUS.exists():
    check("线上批次回归语料随包存在", False, str(CORPUS))
else:
    _d = json.loads(CORPUS.read_text(encoding="utf-8"))
    _pool, _main = _d["候选池"], _d["主表"]
    check("线上批次回归语料随包存在", len(_pool) == 246 and len(_main) == 23,
          f"候选池 {len(_pool)} / 主表 {len(_main)}")

    def _mkrow(r):
        return RawItem(title=r["标题"], abstract=r["内容摘要"], source=r["来源"],
                       group=r["上位大类"], tag=r["分类标签"], record_type=r["记录类型"],
                       seq=1, has_event=True, abstract_status="有效")

    _gates = collections.Counter()
    _blocked_titles = set()
    for r in _pool:
        ok, gate, _why = qg.contract_check(_mkrow(r))
        if not ok:
            _gates[gate] += 1
            _blocked_titles.add(r["标题"])
    check("线上 246 行契约闸确实拦截（不再 0 拦截）", sum(_gates.values()) >= 5,
          f"拦截 {sum(_gates.values())} 条，按闸 {dict(_gates)}")
    def _is_triple(t):
        parts = [x for x in t.split(" ") if len(x) >= 8]
        # 必须存在「重复片段」才算拼接；纯英文长标题按空格切成 3 段不算
        return (len(parts) >= 3 and len(set(parts)) < len(parts)) or "2026-11-2" in t
    _triples = [r["标题"] for r in _pool if _is_triple(r["标题"])]
    check("标题三连复读 / 日期残串行全部被拦",
          bool(_triples) and all(t in _blocked_titles for t in _triples),
          f"共 {len(_triples)} 行，命中 {sum(1 for t in _triples if t in _blocked_titles)} 行")

    # 来源身份闸独立单测（语料里乱拼来源行同时是三连复读，被标题闸先拦）
    _fake = mk("11月LPR报价出炉 5年期和1年期利率均维持不变",
               "中国央行将一年期和五年期贷款市场报价利率分别维持在3%和3.5%不变。",
               "教育部 · 国家统计局 · 中国工程院", "财经市场", "综合")
    _sok, _swhy = qg.source_identity_verdict(_fake)
    check("乱拼非媒体机构来源被识别", not _sok, _swhy)
    _good = mk("财政部公布1—10月财政收支情况",
               "财政部公布2026年1—10月全国一般公共预算收入情况。",
               "财政部 · 税务总局", "财经市场", "综合")
    check("正文含对应机构名的多机构来源不误杀",
          qg.source_identity_verdict(_good)[0])

    # 明确贴近民生的合规行不得被契约闸误杀
    _must_keep_key = ["大风蓝色预警", "LPR报价", "冷空气影响", "官方辟谣",
                      "幼儿园等闲置校舍", "委内瑞拉遭网络电信诈骗", "建材市场大检查"]
    _mis = []
    for r in _pool:
        if any(k in r["标题"] for k in _must_keep_key):
            ok, gate, why = qg.contract_check(_mkrow(r))
            if not ok:
                _mis.append((r["标题"][:36], gate))
    check("民生预警 / 辟谣 / LPR / 领保等合规行不误杀", not _mis, str(_mis[:3]))

    # F5：主表里的机构动态 / 宣传稿 / 军备升级必须剔
    def _f5row(r):
        return type("X", (), {"text": lambda self: r["标题"] + " " + r["内容摘要"],
                              "title": r["标题"], "abstract": r["内容摘要"],
                              "source": r["来源"], "group": r["上位大类"],
                              "tag": r["分类标签"]})()
    _f5_block = [r["标题"] for r in _main if not _rules.noise_verdict(_f5row(r))[0]]
    check("主表机构动态 / 赛事颁奖 / 国际军备升级被 F5 剔除",
          len(_f5_block) >= 7, f"剔除 {len(_f5_block)} 条：{'；'.join(t[:22] for t in _f5_block[:4])}")
    check("F5 不误杀大风预警 / 冷空气 / 辟谣 / LPR",
          all(not any(k in t for k in _must_keep_key) for t in _f5_block),
          f"被剔标题 {_f5_block[:3]}")

    # 成稿层摘要格式清理（电头 / 记者署名 / 坏标点）
    _dirty = ("新华社北京11月20日电 题：抢在前、备在先——能源全线迎战今冬“冲峰大考”\n"
              "新华社记者戴小河、王悦阳\n"
              "入冬以来，多股冷空气接连影响我国，北方地区供暖全面展开。")
    _clean = polish_abstract(_dirty)
    check("摘要清掉电头 / 记者署名且不篡改正文数字",
          "新华社记者" not in _clean and "题：" not in _clean
          and "入冬以来，多股冷空气接连影响我国" in _clean, _clean[:90])
    _bad = polish_abstract("　　创业板指收报2293.34点，跌0.79%，成交3101.21亿元；。\n"
                           "　　昨天白天，北京晴天在线。）")
    check("摘要修掉「；。」与结尾「。）」", "；。" not in _bad and "。）" not in _bad
          and "2293.34" in _bad, _bad[:90])

    (ROOT / "output" / "latest_batch_audit.json").write_text(json.dumps({
        "语料": "用户上传 latest.xlsx（2026-11-22 线上批次）",
        "候选池行数": len(_pool), "主表行数": len(_main),
        "契约拦截": sum(_gates.values()), "按闸": dict(_gates),
        "标题三连复读行": len(_triples), "F5主表剔除": _f5_block,
        "说明": "线上真实批次回归；拦截数远小于 246 是刻意的——候选池按需求⑤不做贴近度严选，政务稿留池不进主表",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

# ---------------------------------------------------------------- 汇总
out = ROOT / "output" / "quality_gate_verify.json"
out.parent.mkdir(parents=True, exist_ok=True)
passed = sum(1 for r in RESULTS if r["结果"] == "PASS")
payload = {"总计": len(RESULTS), "通过": passed,
           "失败": len(RESULTS) - passed, "结果": RESULTS,
           "契约口径": "未经筛选 ≠ 字段不齐/分类不合规/跨事件串味照收"}
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
for r in RESULTS:
    print(f"[{r['结果']}] {r['项']} — {r['明细'][:110]}")
print(f"\n{passed}/{len(RESULTS)} PASS → {out}")
sys.exit(0 if passed == len(RESULTS) else 1)
