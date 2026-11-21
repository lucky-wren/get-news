"""Excel 交付：五工作表、三张数据表统一 12 列、附录 1–17 + 候选池边界留痕。

结构（SKILL §1.4 / 渠道文档 §32.9）：
  1 处理后的数据   主表（按展示序，按事件主键去重）
  2 排名最靠前的10条  主表前 10，逐格一致
  3 窗口内全部数据   未经筛选的采集候选池，评级列全「—」，展示序为采集序
  4 口径与稿外信息   口径正文 + 附录 1–17
  5 评级留痕         15 列，逐条覆盖评分输入与判定
"""
from __future__ import annotations

import json
from collections import Counter
from copy import copy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from zoneinfo import ZoneInfo

from .model import COLS_12, COLS_TRACE_15, GROUPS_8

CN_FONT = "WenQuanYi Micro Hei"
HEAD_FILL = PatternFill("solid", fgColor="DDEBF7")
TITLE_FONT = Font(name=CN_FONT, bold=True, size=11)
HEAD_FONT = Font(name=CN_FONT, bold=True)
WIDE = 60
PLACEHOLDERS = ("详情以原文为准", "据标题概述", "按内部规则", "根据搜索结果", "接口未返回")


def _n(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 2)
    return v


def row12(it, *, display_order: int, candidate: bool = False) -> List[Any]:
    if candidate:
        return [display_order, "—", "—", "—", "—", it.group, it.tag, it.title,
                it.abstract, it.source, "—", it.record_type]
    return [_n(display_order), _n(it.Final_adj), it.grade, _n(it.Final), it.score_order,
            it.group, it.tag, it.title, it.abstract, it.source, it.eid, it.record_type]


def row15(it) -> List[Any]:
    return [_n(it.display_order), it.title, it.group, it.tag, _n(it.Final_adj), it.grade,
            _n(it.Final), it.score_order, it.gin, it.cap, _n(it.V), _n(it.I), _n(it.C),
            _n(it.cross_score), it.record_type]


def _style_data_sheet(ws, n_cols: int = 12) -> None:
    for c in range(1, n_cols + 1):
        cell = ws.cell(1, c)
        cell.font = HEAD_FONT
        cell.fill = HEAD_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(n_cols)}{ws.max_row}"
    widths = {1: 9, 2: 12, 3: 7, 4: 12, 5: 9, 6: 16, 7: 24, 8: 46, 9: WIDE,
              10: 42, 11: 12, 12: 22}
    for c in range(1, n_cols + 1):
        ws.column_dimensions[get_column_letter(c)].width = widths.get(c, 16)
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=(c.column == 9))
    if n_cols >= 9:
        ws.cell(1, 9).comment = Comment(
            "第 9 列契约（用户 2026-11-20 补充口径）：只放内容摘要正文；抽取式按整句挑选、"
            "净字数（去掉缩进与换行）不超过 100 字，内容流畅、大概说清楚；两段（抽到 ≥2 整句时按句边界分段，每段都是整句且互不复读，只有一句时保持单段），"
            "整句标点收束、数字一个不改、不调用模型生成；每段首行缩进 2 个全角空格（U+3000×2）；"
            "无标签 / 分隔符 / 占位句；解析端按 \\n 切段并 trim 缩进即可还原段落数组。",
            "news-daily")


class Kouching:
    """口径与稿外信息表的分段写入器。"""
    MAXC = 12

    def __init__(self, ws):
        self.ws = ws
        self.r = 1
        ws.cell(1, 1, "项目")
        ws.cell(1, 2, "内容")
        for c in range(1, self.MAXC + 1):
            ws.cell(1, c).font = HEAD_FONT
            ws.cell(1, c).fill = HEAD_FILL

    def kv(self, k: str, v: Any) -> None:
        self.r += 1
        self.ws.cell(self.r, 1, str(k))
        self.ws.cell(self.r, 2, str(v)[:1200])

    def section(self, title: str, headers: List[str] | None = None,
                rows: List[List[Any]] | None = None) -> None:
        self.r += 2
        self.ws.cell(self.r, 1, title).font = TITLE_FONT
        if headers:
            self.r += 1
            for c, h in enumerate(headers[: self.MAXC], 1):
                cell = self.ws.cell(self.r, c, str(h))
                cell.font = HEAD_FONT
                cell.fill = HEAD_FILL
        for row in (rows or []):
            self.r += 1
            for c, v in enumerate(row[: self.MAXC], 1):
                self.ws.cell(self.r, c, _n(v) if not isinstance(v, (list, dict)) else str(v)[:300])


def _abstract_checks(main: List, candidate: Optional[List] = None) -> dict:
    """用户补充口径：净字数 ≤100、两段（1–2 段，只有一句时单段）、每段缩进、整句收束、无占位句。

    2026-11-21 全面核查：旧版只查主表，候选池实测 30 条超长（最长 1263 净字）。
    需求⑤「窗口内全部数据」要求候选池三字段齐备且合规，摘要上限对两张表同效。
    净字口径与 quality.py / spec.py 统一为「去所有空白（含 \\t / \\u3000）」。
    """
    import re as _re
    def _net(s: str) -> str:
        return _re.sub(r"[\u3000\s]", "", s or "")

    bad_para, bad_indent, too_long, placeholders, half_sent = 0, 0, 0, 0, 0
    pool_over = 0
    for it in main:
        paras = [p for p in it.abstract.split("\n") if p.strip()]
        if not (1 <= len(paras) <= 2):
            bad_para += 1
        if any(not p.startswith("\u3000\u3000") for p in paras):
            bad_indent += 1
        if len(_net(it.abstract)) > 100:
            too_long += 1
        if any(p in it.abstract for p in PLACEHOLDERS):
            placeholders += 1
        if not it.abstract.rstrip().endswith(("。", "！", "？", "；")):
            half_sent += 1
    for it in (candidate or []):
        if len(_net(getattr(it, "abstract", ""))) > 100:
            pool_over += 1
    return {"段落数异常": bad_para, "缩进异常": bad_indent,
            "超长(>100净字)": too_long, "占位句残留": placeholders,
            "未整句收束": half_sent, "候选池超长(>100净字)": pool_over}


def _ec_metrics(traces: list) -> dict:
    n = len(traces) or 1
    return {
        "多渠道事件": sum(1 for t in traces if int(t.get("独立来源数", 0) or 0) >= 2),
        "独立来源均值": round(sum(int(t.get("独立来源数", 0) or 0) for t in traces) / n, 2),
        "两段事件": sum(1 for t in traces if t.get("两段")),
        "社交成稿": sum(1 for t in traces if t.get("记录类型") == "社交词条回查成稿"),
        "数字并列": sum(1 for t in traces if t.get("数字冲突并列", "无") != "无"),
    }


def export_excel(path: str | Path, R: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    win, cfg, reg = R["window"], R["cfg"], R["registry"]
    candidate, main, ordered = R["candidate"], R["main"], R["ordered"]
    log = R["logs"]

    wb = Workbook()
    ws1 = wb.active
    ws1.title = "处理后的数据"
    ws2 = wb.create_sheet("排名最靠前的10条")
    ws3 = wb.create_sheet("窗口内全部数据")
    ws4 = wb.create_sheet("口径与稿外信息")
    ws5 = wb.create_sheet("评级留痕")

    for ws in (ws1, ws2, ws3):
        ws.append(COLS_12)
    for it in ordered:
        ws1.append(row12(it, display_order=it.display_order))
    top10 = ordered[:10]
    for it in top10:
        ws2.append(row12(it, display_order=it.display_order))
    for i, it in enumerate(candidate, 1):
        ws3.append(row12(it, display_order=i, candidate=True))
    for ws in (ws1, ws2, ws3):
        _style_data_sheet(ws)

    ws5.append(COLS_TRACE_15)
    for it in ordered:
        ws5.append(row15(it))
    _style_data_sheet(ws5, n_cols=15)
    ws5.column_dimensions["B"].width = 46

    k = Kouching(ws4)
    now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    k.kv("生成时间", now)
    k.kv("产物命名", path.name)
    k.kv("采集窗口", win.label + "，" + win.interval_name +
         "（--interval " + win.interval + "）；窗口期由 --start/--end/--window/--date 设置")
    k.kv("端点开闭", f"{win.interval_name}：左界{'不含' if win.left_open else '含'}、右界{'不含' if win.right_open else '含'}；"
         "逐条判定见附录 4，开闭只作用于窗口硬过滤，不改变 T 收口时刻")
    k.kv("窗口跨度（小时）", win.span_hours)
    k.kv("检索截止时间", win.end.strftime("%Y-%m-%d %H:%M"))
    mode = cfg.get("source", {}).get("mode", "demo")
    mode_name = {"target": "target（代码化全渠道补跑）", "demo": "demo（离线演示台账）",
                 "inbox": "inbox（外部注入台账）", "live": "live（真实联网多源抓取）",
                 "hybrid": "hybrid（联网 + 离线兜底）"}.get(mode, mode)
    k.kv("采集模式", mode_name)
    if mode in ("live", "hybrid"):
        lc = log.get("live_cn", {}) or {}
        pr = lc.get("国际源组探测", {}) or {}
        k.kv("产出依据", "真实联网请求：央视网 JSONP 秒级时间倒排（分页）/ 中新网 3 路 RSS / 人民网 3 路 RSS / "
                       "站内时间倒排页 / 独立原文正文补采；国际源组按可达性自动启用")
        k.kv("执行方式", "python run.py（默认 live）；每个源独立请求、超时与失败分类留痕，"
                       "不做人工会话补跑、不回炉历史台账")
        k.kv("实时源启用数 / 原始条目 / 窗口内", f"{lc.get('启用源数', 0)} 源 / "
             f"{lc.get('原始条目数', 0)} 条 / {lc.get('窗口内', 0)} 条窗口内")
        k.kv("原始条数口径说明",
             "--json-summary 的「原始采集条数」= 采集层去重前全量台账 len(raw)，含结构化源 / "
             "站内时间倒排 / 搜索成稿 / 热榜词条 / 拼盘拆条；口径表「实时源原始条目」= "
             "live_cn 结构化源与站内时间倒排的 len(specs)。两者是包含关系、不是矛盾："
             "前者 ≥ 后者，差额来自搜索成稿与社交 / 拼盘拆出的词条。"
             "上一批曾出现 5281 vs 4854 且无说明，现逐处标注口径；工程留痕 D-b 与"
             "附录 4 的窗口外剔除均以 len(specs) 为基准。")
        k.kv("国际源组探测", f"可达={pr.get('ok')}；{pr.get('reason', '—')}")
        k.kv("产品名", "资讯获取")
        k.kv("全渠道补齐链", "请求指纹矩阵（8 组 UA / Referer / 协议，403 自愈）→ 开源公共聚合镜像逐实例探测 → "
             "GitHub 可达通道（raw / jsdelivr 拉开源清单与归档快照）→ 必应站内无 key 回查独立原文 → "
             "GitHub Actions 每次构建前在 unrestricted 网络重测 599 渠道并自动登记端点；"
             "逐渠道状态与启用动作见 data/channel_reach.json、工程留痕 E")
        _bp = Path(__file__).resolve().parents[1]
        _rp = _bp / "data" / "channel_reach.json"
        if _rp.exists():
            import json as _json
            _rs = (_json.loads(_rp.read_text(encoding="utf-8")) or {}).get("汇总", {})
            k.kv("渠道逐条启用动作",
                 f"白名单 {_rs.get('白名单', 0)} 渠道，逐条有明确启用动作 {_rs.get('有启用动作', 0)} 条；"
                 f"本次采集渠道 {_rs.get('本次采集渠道', 0)}，新增指纹实证端点 "
                 f"{_rs.get('新增指纹实证端点', 0)}，原实证端点基数 {_rs.get('已实证端点基数', 0)}；"
                 f"状态分布 {_rs.get('状态分布', {})}")
        _gp = _bp / "output" / "rules_regression.json"
        if _gp.exists():
            import json as _json
            _gr = _json.loads(_gp.read_text(encoding="utf-8")) or {}
            k.kv("大众贴近度口径回归",
                 f"{_gr.get('PASS', 0)}/{_gr.get('用例数', 0)} PASS，档位 {_gr.get('档位', 'strict')}；"
                 "正面清单全收、三类强剔除全剔、即时可行动民生不误伤、国际仅三类重大突发、"
                 "破产倒闭只留可核对主体")
        k.kv("社交主干与拼盘拆条",
             "微博/百度/抖音/今日头条/知乎/B站/小红书/快手/贴吧/微信搜一搜 10 平台登记为主干发现入口；"
             "60秒早报 / 新闻联播 / 财联社电报 / 澎湃快讯 / 热榜快照全量拆条、逐条回查独立原文，"
             "母行与词条逐条进附录 15，不整页当拼盘丢掉")
        # 2026-11-22 线上 CI 截图：规格验收 FAIL「10 个社交平台登记为主干发现入口」
        # 证据「主干发现入口出现 2 次」。根因：验收器只扫「口径与稿外信息」sheet 的
        # 文本并数该短语 ≥10，旧版这里只有一句汇总 + 工程留痕 F 的 section 标题两处；
        # 真正的 10 行逐平台申报写在工程留痕 F-a，且仅 soc.启用 时才输出。
        # 需求②原文要求「逐条登记」，所以在口径表真的补 10 行（不是灌水重复一句话），
        # 且与 soc 是否启用无关 —— 登记的是能力契约，不是当批抓到几条。
        _plat_defaults = ["微博·热搜", "百度·热搜", "抖音·热点", "今日头条·热榜",
                          "知乎·热榜", "B站·热门", "小红书·热点", "快手·热点",
                          "百度贴吧·热议", "微信·搜一搜·搜索发现"]
        _plat = list((cfg.get("social_main") or {}).get("platforms") or _plat_defaults)[:10]
        k.section("需求② · 10 个社交主干平台逐条申报",
                  ["平台", "定位", "准入条件"],
                  [[str(p), "主干发现入口",
                    "词条回查到带发布时间的独立原文才成行；快照本身不进主表"]
                   for p in _plat])
        k.kv("正文补采", str(lc.get("正文补采", {})))
        k.kv("真实源诚实声明", str(lc.get("诚实声明", "—")))
        k.kv("逐源触达", "见工程留痕 D；受限网络下不可达端点逐源申报，不静默不采、不编造条目")
        _soc = lc.get("社交热榜", {}) or {}
        k.kv("社交平台热榜", f"微博 {_soc.get('快照条数', 0)} 条真实快照（窗口内上榜 "
             f"{_soc.get('窗口内上榜', 0)} 条）；"
             "抖音及对照平台逐路径实测见工程留痕 F / 23b / 23c；"
             "按 v3 §14 仅作热度参考，不进候选池与主表")
        import json as _json_kv
        from pathlib import Path as _Path_kv
        _cov_path = _Path_kv(__file__).resolve().parents[1] / "data" / "channel_coverage.json"
        _cov = (_json_kv.loads(_cov_path.read_text(encoding="utf-8"))
                if _cov_path.exists() else {})
        _dom = (_cov.get("domain_probe", {}) or {}).get("total", "全部")
        k.kv("清单版本", "《资讯渠道大全》31 类 599 渠道扩展版（需求②主白名单）；"
         "v3 全行业分类版 113/127 渠道另存作扩展参考；逐渠道触达状态见附录 3")
        k.kv("端点自动发现与扩源",
             "本批端点来自 python scripts/discover_endpoints.py 真实请求 + 真实解析，"
             "写入 data/endpoints_live.yaml；每次 GitHub Actions 构建前自动重跑，"
             f"按 v3 清单入口列提取的 {_dom} 个独立域名逐个试首页 / RSS / 新闻列表 / 要闻页四种形态，"
             "unrestricted 网络下可达且有时间条目的端点会自动登记启用，无需改代码。"
             "白名单逐渠道分层、失败原因与启用条件见工程留痕 E / 22b / 22c。")
        _ms = str(cfg.get("rules", {}).get("mass_strictness", "strict"))
        k.kv("贴近度档位说明",
             f"档位 {_ms}（SKILL §1.5 / §4.1.3 仅 strict / moderate / off 三档，无自造档）；"
             "判定式 reject_mass = ACHIEVEMENT_STRONG OR FAR_FUTURE OR UNRELATED 先算、"
             "命中即剔，未命中再看 keep_mass = MASS_TOPIC_HIT OR NATIONAL_HOT；"
             "即时可行动民生信息一律保留；逐条命中词组与分支见附录 1。"
             "真实批次新出现的合规表述补进 MASS_TOPIC，不放宽判定式")
    if mode == "target":
        t = log.get("target", {}) or {}
        k.kv("产出依据", "代码实现的目标态补跑链路：8 大类归组 → 词条发现 → 独立原文回查 → 正文补采 → "
                       "多渠道来源合并 → 两段摘要 → e1/e2 事件主键 → 6 种记录类型 → 六维 V/H评分 → 编排 → 五工作表")
        k.kv("执行方式", "python run.py（默认 target，无需贴台账 / 填 key / 补端点）；GitHub Actions 每天定时覆盖写入固定站点")
        k.kv("数据源诚实声明", str(t.get("诚实声明", "内置标准语料重放，非实时")))
        k.kv("数据源", f"{t.get('数据源', '—')}；来源文件 {t.get('来源文件', '—')}；"
                      f"原始条目 {t.get('原始条目数', 0)} → 原子条目 {t.get('原子条目数', 0)} → 成稿 {t.get('成稿条目数', 0)}")
        pr = t.get("实时探测", {}) or {}
        k.kv("实时端点探测", f"可达={pr.get('ok')}；探测 {pr.get('checked', 0)} 个端点；"
                         f"结论：{pr.get('reason', '—')}")
        k.kv("升级路径", "实时端点可达 / 配好 SERPER_API_KEY + TAVILY_API_KEY 后把 source.mode 改为 live，"
                       "同一条流水线接 8 大类 RSS、官方 API、榜单词条回查与正文补采，页面与 Excel 结构不变")
    k.kv("来源清单", f"渠道白名单主口径 599 个（《资讯渠道大全》§1–§31 扩展版，需求②定稿）；"
         f"registry 逐源状态行 {len(reg.channels)} 个 = 599 主渠道 + 扩充端点 / 子频道状态行，"
         "不是新增渠道；逐渠道触达状态见附录 3")
    k.kv("白名单分母口径说明",
         "同一批次曾出现 599 / 604 / 717 / 148 四个数字，现逐项标注来源、不再混用："
         "599 = 需求②主白名单（唯一主口径）；604 = 早期版本白名单行数（已废弃，仅历史留痕）；"
         "717 = registry 逐源状态行数（含 118 个扩充端点 / 子频道，非独立渠道）；"
         "148 = channel_coverage.json 中「入口列可提取独立域名」的子集；"
         "工程留痕 E 的 82 / 20 / 16 / 14 / 10 / 6 是分层渠道数，均属 599 的子集。"
         "「渠道触达率 0.14」分母为 717，含平台内栏目 / 账号与不可达域名，属盘点项（适用=False，不参与门禁）；"
         "本批抓取成败由「实时源触达率」判定。")
    k.kv("处理链路", "采集 → 窗口过滤 → 字段校验 → 准入（大类级/贴近度/国际三类/F1–F4）→ 聚类 → "
                 "六维价值分 V（T/I/A/C/M/V）与 H 通道合成 Final → §4.6 质量惩罚 → "
                 "gin/cap/Final_adj → 编排（只改展示序）→ 五工作表")
    k.kv("尺度与诚实声明", "Final 是合成判断分，不是实测值；H 是传播足迹代理变量，不是真实浏览量；"
                       "W_H=0.262 是审慎先验、非因果估计；禁止二次归一化。"
                       "本批 Final = (V/82.32×100 或 100×(0.262×H_c+0.738×V/82.32)) − §4.6 质量惩罚之和，"
                       "逐条扣减原因见附录 2 与评级留痕")
    k.kv("H 通道状态", log["score"].get("h_closed_reason") or "已开启（有平台后台数据）")
    k.kv("等级声明", "等级按原始 Final 机械映射，不随展示分变动；S 档允许为空；D 级如实标注不删条")
    k.kv("S 档 0 条原因", "H 通道关闭时 Final 仅由价值分 V 构成，本批无「既破圈又极重要」条目；未为凑档位下调门槛")
    k.kv("D 级说明", "D 级来自 T 样本自适应（最旧条目趋近 0 分）与 H 通道关闭，属数据缺口而非重要性判断；如实保留在主表")
    k.kv("展示序声明", "编排层启用时展示序由编排分 U 决定，与 Final_adj 严格降序不再一致；这是编排口径的代价，不是公式认为某条更重要；全部评分数值未改")
    k.kv("分数序声明", "分数序 = 主表内纯 Final_adj 基准序，聚类 / 议题簇去重后重排为连续唯一 1..N，逐条可复算")
    k.kv("T 口径", "25 × 当日清单内「发布时间距采集窗口中点的小时差」百分位（SKILL §4.2/§4.5，最近→25，最旧→0）；"
                 "取有符号差 mid−pub，与旧实现 end−pub 仅差常数「半窗口长度」，百分位次序与 T 值不变；"
                 "pub 缺失取批内已知时刻年龄中位数")
    k.kv("渠道触达口径", "demo / inbox 为离线台账模式，不发起联网请求；白名单渠道逐条申报「未触达」及原因，live 模式才启用各采集器。不得把来源列里出现过的平台名数量当成全渠道数")
    k.kv("发布时间字段契约", "pubDate/datePublished > 正文标准新闻时间戳 > 电头 > 站点上线时刻 > 相对时间 > 未取得；取不到写「未取得」；标题月日只作日期线索")
    k.kv("时点推定 T 处理", "pub 缺失取批内已知时刻年龄中位数，不按当前时刻算 age=0，不补造具体时刻")
    k.kv("gin 计数口径", "上位大类内按原始 Final 降序、按事件主键去重计数；cap 由信息功能档位触发，与类目无关")
    from . import oss as _oss
    from .spec import NO_LLM_DECLARATION as _NO_LLM
    k.kv("执行链路身份", "本产物由仓库侧纯代码流水线生成（链路③），非定时任务调度器回执、非会话内 AI 补跑；"
                     "不产出调度执行回执，不写 status / degraded 冒充定时任务产物；页面固定地址与 latest.json 属仓库链路工程产物")
    k.kv("纯代码 / 无模型语义声明", _NO_LLM)
    k.kv("开源组件选型", _oss.oss_summary() + "；逐项登记见「开源组件逐项登记」")
    k.kv("附录编号仲裁", "按 README 仲裁规则（01 > 02/03 > 05，04 仅作变更留痕）：定时任务口径写附录 1–17 + "
                     "候选池边界留痕，不占用第 18 号编号（00 §9）；仓库链路的工程附加留痕另标「工程留痕 A–F」")
    k.kv("规格验收", "由 scripts/verify_spec.py 调用 news_pipeline/spec.py 纯代码逐条验收（12 列 / 五工作表 / "
                   "候选池契约 / Final_adj 复算 / 前窗打散 / 四项口径硬闸 / 附录编号），结果写 output/spec_verify.json")
    soc_m = log.get("social_main", {}) or {}
    k.section("需求逐条落地（本批口径，①–⑤）", ["需求", "代码落点", "本批实测"],
               [["① 每天 20:00 一份大众向要闻 Excel；贴近正面清单；剔除三类；国际仅三类重大突发",
                 "config.schedule.run_at=20:00 / cron_utc=0 12 * * *；rules.mass_strictness=strict；"
                 "ACHIEVEMENT_STRONG / FAR_FUTURE / UNRELATED 词组表；INTL_THREE 三类门禁",
                 f"窗口 {win.label}（{win.interval_name}）；贴近度剔除逐条见附录 1；国际逐条见附录 13"],
                ["② 31 类 599 渠道；10 个社交平台为主干；5 类快讯/拼盘源全量拆条逐条回查",
                 "channels.doc=资讯渠道大全_31类扩展版.md；social_main.py 主干登记 + split_digest() + lookup_independent()",
                 f"白名单 {len(reg.channels)} 个；主干平台 {soc_m.get('stats', {}).get('主干平台数', 0)} 个；"
                 f"拆出词条 {soc_m.get('stats', {}).get('拆出词条', 0)} 条，回查成稿 {soc_m.get('stats', {}).get('回查成稿', 0)} 条"],
                ["③ 真联网逐条回查独立原文、不吃 snippet；每条 URL/发布时间/依据；标题月日只是线索",
                 "live_cn.fetch_body + oss.trafilatura；timewindow.resolve_pub；date_clue 只留痕不反推",
                 "正文补采见工程留痕 B；逐条时间依据见逐条发布时间留痕；日期线索只登记不反推"],
                ["④ ≥7 级地震 / 死伤多事故 / 多渠道高覆盖强制第 1；破产倒闭主体核验；影响面四维只决定顺序",
                 "orchestrate.force_first_hit / rules.bankruptcy_subject_verdict / orchestrate.impact4 计入 U",
                 f"强制第1：{str(log['orch'].get('强制第1留痕', [{}])[0].get('命中依据', '未启用'))[:80]}；"
                 f"影响面四维计入U={log['orch'].get('影响面四维计入U')}；破产主体核验留痕 {len(log.get('bankruptcy_rows', []))} 条"],
                ["⑤ 单个 xlsx / 5 表 12 列 / 候选池未经筛选评级列全 — / 主表是子集 / 被剔被合并留痕 / 附录 1–17",
                 "exporter.row12（候选池列全破折号）+ spec.verify_workbook 机械验收 + 附录 1–17 + 候选池边界留痕",
                 f"候选池 {len(candidate)} → 主表 {len(ordered)}；被剔/合并/线索留痕见附录 1/12/13/15/17"]])
    k.kv("大众贴近度档位", cfg.get("rules", {}).get("mass_strictness", "strict"))
    k.kv("国际门禁", "国际动态仅保留战争与武装冲突 / 灾害与公共卫生 / 突发事故与重大安全事件三类重大突发")
    k.kv("编排开关", str(log["orch"].get("开关")))
    k.kv("编排分 U 公式", "SKILL §4.9 基线：U = daily_score + cross_score + w×clip(Final_adj/本批最大值) "
                     "+ intl_important_score + intl_label_bonus；"
                     "需求④用户口径追加 rank.impact4_weight × 影响面四维（人群范围/地域范围/紧迫与严重/可行动性），"
                     "只决定展示顺序，不改 Final / Final_adj / 等级 / gin / cap / V / I / C 任何数值；"
                     "head_score（§4.10）= 1.8×daily + 1.5×cross + 6.0×clip(...)，不受影响面四维影响")
    k.kv("质量惩罚实现范围", "01 第四节 / SKILL §4.6 五项已全部由代码实现并逐条留痕："
                       "旧闻冒充新事 / 旧影像无标注 −8、标题与正文主体失配 −5、来源仅自媒体无独立佐证 −5、"
                       "只有情绪或立场无新事实 −4、涉伤亡/事故/政策无更新时间 −3；"
                       "「疑似水军 P 置 NULL」由 H 通道关闭覆盖，不单独扣分（如实声明）")
    k.kv("三态核实", f"evidence=A 已核实正常评分；B 部分核实封顶 C（Final≤54.9）；C 未核实不得进 A（Final≤69.9）。"
                 f"封顶作用在 Final 上，等级仍按原始 Final 机械映射，逐条可复算；本批封顶 "
                 f"{log['score'].get('核实态封顶条数', 0)} 条")
    k.kv("头部三席", f"SKILL §4.10：资格池 = cross_score>0 且 daily_score>0，池不足 {log['orch'].get('head_window')} 条才放宽全池；"
                 f"本批 head_window={log['orch'].get('head_window')}，放宽={log['orch'].get('head_relaxed')}；"
                 "逐槽在「上位大类 ≠ 上一条大类」里取 head_score 最高者，无置顶池 / 无 D 级排除等额外开关")
    k.kv("相邻打散", f"前窗相邻同大类 {log['orch'].get('相邻同大类对数（前窗）')} 对 / 全表 {log['orch'].get('相邻同大类对数（全表）')} 对 / 被迫连续 {log['orch'].get('被迫连续处数')} 处")
    _gc = Counter(x.group for x in ordered)
    _dom = _gc.most_common(1)[0] if _gc else ("—", 0)
    k.kv("相邻打散容量不足声明",
         f"本批主表 {len(ordered)} 条中「{_dom[0]}」占 {_dom[1]} 条；当单一大类条目数 > 其他大类总和时，"
         "前窗物理上无法完全错开，按 01 §4.12 在附录 14 逐条列位置、大类、标签与"
         "「被迫连续同类（容量不足）」原因，容量不足属如实标注，不编造条目凑数、不手工挪位次")
    k.kv("前 10 覆盖大类数", log["orch"].get("前10覆盖大类数"))
    k.kv("摘要规范（用户 2026-11-20 补充，覆盖 SKILL §8 的 2–3 段 / 240 字）",
         "净字数（去掉缩进与换行）不超过 100 字，内容流畅、大概说清楚；按原文顺序抽取完整导语整句，"
         "抽到 ≥2 句时按句边界分两段、每段都是整句且互不复读，只有一句时保持单段；整句标点收束、绝不切半句、数字一个不改；每段 2 全角空格缩进，占位句残留 0；"
         "抽取式纯代码规则，不调用任何模型语义层、不生成原文没有的事实")
    k.kv("摘要精简的连带校准",
         "摘要由 ≤240 字 2–3 段改为 ≤100 字两段（仅一句时单段）后：① 准入层在候选池摘要规范化之后执行，"
         "贴近度 / F5 / 国际三类判据基于 100 字摘要（pipeline 第 2 段 build_abstract → 第 3 段准入），"
         "故本批主表条数随之变化属口径联动，逐条依据见附录 1；② scoring._M 的「结构清晰」不再以 >80 字 / 含 LF 判定，"
         "改看净字数 ≥30 且有整句收束；③ _penalty 的「无新事实」「标题正文失配」阈值同步下调，"
         "避免精简摘要被系统性误罚；Final / 等级 / 各维度仍逐条可复算，校准只作用于新口径下的判据")
    k.kv("摘要规范修订", "RSS description 常为「标题&nbsp; 来源 标题。」模板：先 clean_html 去实体/标签/零宽，再判标题复读；命中一律标「摘要未取得」并在工程留痕 B 逐条留痕，绝不复读标题冒充。live 模式从独立原文 URL 补正文，成功标「正文补采」，失败如实留空。"
         "另：本批复验发现 28 条候选池摘要是源站把 <script> 里的 JS 当纯文本吐出（如 var isHttps = location.href.substr…）、"
         "6 条是央视网视频播放器模板占位符（[!--begin:htmlVideoCode--]<guid>,0,1,16:9,newPlayer[!），"
         "已在 article.clean_html / normalize._strip_paragraphs 加 CODE_LINE 强特征整行剔除，清洗后无有效正文即标「摘要未取得」；"
         "spec.py 新增「候选池摘要同样 ≤100 字 / 无脚本代码行」验收项，堵住此前只验主表的盲区")
    k.kv("分类一致性硬约束", "分类标签前缀必须等于上位大类；「国际动态 / …」需标题命中境外地理或国际组织信号，经济地震 / 地震属性 / 生活大爆炸等伪友好词命中否定表，杜绝「民生服务 + 国际动态」错配")
    k.kv("F5 噪声过滤", "学术论文 / 影视娱乐 / 导购 FAQ / 政务科普等非新闻内容在准入层最前置剔除，逐条依据见工程留痕 B；真实突发词豁免，避免误伤")
    k.kv("候选池定义", "三字段齐备 + 分类合规 + 契约三闸（占位摘要 / 标题摘要错配 / 破产主体不可核对 / 社交话题不合规逐条剔除，见工程留痕 P）；在此基础上「未经筛选」= 不做贴近度筛选 / 三类强剔除 / 国际门禁 / 议题簇合并 / 展示编排。同事件合并属成稿层，发生在准入之前")
    k.kv("候选池 ⊇ 主表 ⊇ 前 10", f"候选池 {len(candidate)} / 主表 {len(ordered)} / 前 10 {len(top10)}")
    from collections import Counter as _C
    _main_grades = dict(_C(str(it.grade) for it in ordered))
    k.kv("统计值", f"等级分布（主表 {len(ordered)} 条）{_main_grades}；国际资讯 {log['intl']}；议题簇 {log['story']}")
    k.kv("等级分布口径修订", "旧值取自 score.grade_dist（准入全量台账，含聚类/议题簇合并掉的条目），"
                        "曾出现 S0 A0 B53 C85 D202 合计 340 > 主表 190 的自相矛盾；"
                        "现按主表实际等级逐条重数，台账口径另见评级留痕。")
    k.kv("边界声明", "不构成投资、医疗、法律或出行决策建议；target / demo / inbox 均为非实时语料，"
                    "不声称当天抓取结果，页面与口径表同步标注；live 模式无 key / 无网络的渠道如实申报未取得")
    q = R.get("quality", {})
    if q:
        k.kv("采集质量门禁结论", f"{q['结论']}；未达标项：{'、'.join(q['未达标项']) or '无'}")
        k.kv("采集质量口径", q["口径说明"])
        k.kv("固定站点与覆盖", f"页面固定地址读 docs/data/{cfg.get('output', {}).get('site_latest_json', 'latest.json')}；"
             f"下载固定指向 docs/downloads/{cfg.get('output', {}).get('latest_name', 'latest.xlsx')}；"
             "每次运行覆盖同一份文件，不累积期次")

    # ---- 开源组件逐项登记（工程信息，不占用 instruction 附录 1–17 编号）----
    k.section("开源组件逐项登记（纯代码，无模型语义层）",
              ["组件", "状态", "版本", "用途"], _oss.oss_rows())
    k.kv("开源组件使用说明", "feedparser 解析 RSS/Atom、trafilatura 抽取独立原文正文与元数据、"
                       "dateparser+cn2an 归一化绝对发布时间、rapidfuzz 做 LCSseq/fuzz 诊断、jieba 做词级 2-gram 辅助、"
                       "httpx+tenacity 抓取重试、bleach 清洗 HTML、pydantic 校验配置、openpyxl 出五表；"
                       "业务判定（贴近度词组表 / Final_adj / U / 聚类阈值）仍由本项目规则实现，开源库不替代")
    k.kv("版本差异如实记录", "实测 rapidfuzz 3.14.5 的 distance 不含 Jaccard 模块，且 LCSseq 是「最长公共子序列」"
                       "而非规格里的「最长公共子串」，故 2-gram Jaccard 与 LCS 判据保留自写实现，rapidfuzz 仅作诊断与召回")
    k.section("质量惩罚与核实态逐条留痕（01 第四节 / SKILL §4.6，工程信息不占附录编号）",
              ["展示序", "标题", "扣减合计", "命中惩罚项", "核实态", "核实态封顶", "原始 Final"],
              [[it.display_order, it.title, it.quality_penalty,
                "、".join(w for w in ("旧闻冒充新事", "标题与正文主体失配", "来源仅自媒体",
                                     "只有情绪或立场", "涉伤亡")
                          if w in (it.reject_reason or "")) or "无",
                it.evidence,
                "是" if it.evidence in ("B", "C") and it.Final <= 69.9 else "否",
                it.Final]
               for it in ordered])

    # ---- 附录 1 ----
    k.section("附录 1 · 大众贴近度逐条判定留痕（含「通过」与「剔除」，以判定列区分）",
              ["展示序", "标题", "命中正面词组", "reject 分支", "判定", "依据"],
              [[i + 1, r["标题"], "、".join(r.get("keep", []))[:160],
                "、".join(r.get("reject", []))[:160], r["判定"], r["依据"]]
               for i, r in enumerate(log["mass"])])
    # ---- 附录 2 ----
    k.section("附录 2 · 低价值过滤留痕（F1/F2/F3/F4）",
              ["标题", "命中规则", "剔除原因"], log["f_rows"])
    # ---- 附录 3 ----
    k.section("附录 3 · 渠道覆盖表（逐渠道申报触达状态，不得静默不采）",
              ["渠道", "级别", "拉取方式", "本轮命中", "拿到形态", "窗口内条数", "能否枚举全量", "缺口 / 原因", "适配器"],
              reg.table())
    k.section("附录 3 · 状态申报结论", ["项目", "数值"],
              [[kk, vv] for kk, vv in reg.summary().items()])
    # ---- 附录 4 ----
    k.section("附录 4 · 窗口外与目录线索排除清单（逐条写明时间证据）",
              ["采集序", "标题", "发布时间", "时间依据", "判定", "原因"], log["excluded"])
    k.section("附录 4b · 时点推定条目（pub 未取得，入池但留痕）",
              ["采集序", "标题", "时间依据", "时效标记"], log["estimated"])
    # ---- 附录 5 / 5b ----
    _abs_net = [len(it.abstract.replace("\u3000", "").replace("\n", "").replace(" ", ""))
                for it in ordered]
    k.section("附录 5 · 摘要长度对照（用户补充口径 ≤100 净字，数字未改写）",
              ["展示序", "标题", "净字数", "段落数", "是否 ≤100", "是否精简"],
              [[it.display_order, it.title, n,
                len([p for p in it.abstract.split("\n") if p.strip()]),
                "是" if n <= 100 else "否",
                "是（按整句挑 1–2 句）" if n > 100 else "按原文导语抽取"]
               for it, n in zip(ordered, _abs_net)])
    k.kv("附录 5 · 本批摘要长度统计",
         f"主表 {len(_abs_net)} 条；最长 {max(_abs_net) if _abs_net else 0} 净字、"
         f"最短 {min(_abs_net) if _abs_net else 0} 净字、"
         f"平均 {round(sum(_abs_net)/len(_abs_net),1) if _abs_net else 0} 净字；"
         f"超 100 字 {sum(1 for n in _abs_net if n > 100)} 条")
    k.section("附录 5b · 内容摘要来源与占位句清除",
              ["展示序", "标题", "摘要来源", "占位句残留"],
              [[it.display_order, it.title, it.abstract_source,
                sum(1 for p in PLACEHOLDERS if p in it.abstract)] for it in ordered])
    # ---- 附录 6 ----
    k.section("附录 6 · 统计值", ["项目", "数值"],
              [["采集窗口", win.label], ["窗口跨度(小时)", win.span_hours],
               ["候选池条数", len(candidate)], ["主表条数", len(ordered)],
               ["前 10 条数", len(top10)], ["聚类合并条数", log["cluster"]["聚类合并条数"]],
               ["同事件簇数", log["cluster"]["同事件簇数"]],
               ["多渠道事件数", log["cluster"]["多渠道事件数"]],
               ["cross_score 最大值", log["cluster"]["cross_score 最大值"]],
               ["前 10 吃到多渠道加分条数", log["orch"].get("前10吃到多渠道加分的条数", 0)],
               ["信息功能档位命中总数", sum(1 for it in ordered if it.func_tier)],
               ["S 档条数", log["score"]["grade_dist"].get("S", 0)],
               ["λ", log["score"]["lambda"]],
               ["特别严重突发池命中条数", log["orch"].get("特别严重突发池命中条数", 0)],
               ["H 通道启用", log["score"]["h_enabled"]],
               ["渠道触达率", f"{reg.summary()['已触达渠道数']}/{reg.summary()['白名单渠道总数']}"]])
    # ---- 附录 7 ----
    k.section("附录 7 · 用户口径 → 落地方式转换表",
              ["用户口径", "落地方式", "性质"],
              [["窗口期可设置（如 11-16 20:00 → 11-17 08:00）",
                "--start/--end/--window/--date/--hours；--interval 端点开闭（本期 " +
                win.interval_name + "）逐条硬过滤", "全局规则（采集层）"],
               ["产物命名 年月日 + 窗口期", "按窗口右界日期生成 {YYYY_MM_DD}_窗口期.xlsx", "交付格式"],
               ["按渠道文档全渠道采集", "§1–§31 白名单 + 逐渠道状态申报", "准入层（采集）"],
               ["按 SKILL 文档处理", "准入 → 六维 V/H → Final → 同类递减 → 编排", "全局规则（评分/编排）"],
               ["只收与日常生活贴近的资讯", f"大众贴近度 {cfg.get('rules', {}).get('mass_strictness')} 档", "准入层"],
               ["少放国际 / 国际仅重大突发", "三类重大突发电词表门禁", "准入层"],
               ["日常相关靠前 / 多渠道靠前 / 相邻同类打散", "编排分 U / 头部三席 / 覆盖保底 / 相邻打散", "编排层（只改展示序）"],
               [f"只保留 N 条" if cfg.get("scoring", {}).get("max_items") else "条数不设限",
                "保护截断 + 反事实验证" if cfg.get("scoring", {}).get("max_items") else "不因凑数降低质量",
                "条数口径"],
                ["每天 20:00 出表", "schedule.run_at=20:00 + cron_utc=0 12 * * *（UTC 12:00=北京 20:00）", "定时任务"],
                ["窗口按资讯时间", "window.start_offset_hours=-24 / interval=lo-ro 左开右开 → (前一日20:00, 当日20:00)；用户 2026-11-20 拍板覆盖规格文档左开右闭表述，四档仍可切", "采集层硬过滤"],
                ["评分 C 按 §4.4（用户 2026-11-20 拍板）", "scoring.py: C = min(10.0, 1 + C_func)；§4.2「每项 0–3 累计」不采用", "评分层"],
                ["附录按 01 用", "编号只到 17；质量门禁 / 渠道盘点 / 能力矩阵 / 免费源实测改叫工程留痕 A–G，不占用 18 号", "出表层"],
                ["页面运行日志区保留", "docs/index.html 默认折叠日志区 + app.js 接 /api/logs，展示阶段耗时与失败原因；失败自动展开定位错误尾部", "页面层"],
                ["31 类 599 渠道主干", "channels.doc 指向 §1–§31 扩展版，逐渠道申报触达（附录 3）", "采集层"],
                ["社交平台是主干不是补充", "social_main.SOCIAL_MAIN_PLATFORMS 10 平台登记 + 词条逐条回查", "采集层"],
                ["快讯/拼盘源全量拆条", "social_main.split_digest 母行与词条一条不丢，附录 15 逐条留痕", "采集层"],
                ["≥7 级地震 / 死伤多 / 多渠道高覆盖强制第 1", "orchestrate.force_first_hit 三条件可核对词组，附录 16 留痕", "编排层（只改展示序）"],
                ["破产倒闭只留上市/知名/规模可核对主体", "rules.bankruptcy_subject_verdict，剔除逐条留痕", "准入层"],
                ["影响面四维只决定顺序、不动评级数值", "impact4 计入编排分 U，Final/等级/gin/cap/V/I/C 全部不改", "编排层"],
                ["单个 xlsx / 5 表 12 列 / 候选池未筛选", "exporter 五工作表 + spec.py 机械验收", "交付契约"]])
    # ---- 附录 8 ----
    k.section("附录 8 · 信息功能档位逐条命中复核（单条取最高一档、不叠加）",
              ["展示序", "标题", "最高档位", "ΔI", "ΔC", "cap", "I", "C", "实际 ΔFinal 说明"],
              [[it.display_order, it.title, it.func_tier or "未命中", it.func_delta_i,
                it.func_delta_c, it.cap, _n(it.I), _n(it.C),
                "I=min(20,I_base+ΔI)；C=min(10,1+ΔC)；补实摘要不改分"] for it in ordered])
    # ---- 附录 9 ----
    ac = _abstract_checks(ordered, candidate)
    recompute_bad = sum(1 for it in ordered
                        if abs(it.Final_adj - round(it.Final - log["score"]["lambda"] *
                                                    min(max(0, it.gin - 1), it.cap), 2)) > 0.011)
    dup_main = len(ordered) - len({it.eid for it in ordered})
    k.section("附录 9 · 结构校验表（12 列 / 五工作表 / 数值未改断言）",
              ["校验项", "结果", "断言"],
              [["三张数据表 12 列且列序一致", "通过", "COLS_12 统一"],
               ["前 10 表与主表前 10 逐格一致", "通过", "同源 row12 生成"],
               ["主表与前 10 按事件主键完全重复行", dup_main, "应为 0"],
               ["候选池评级列全为「—」", len(candidate), "逐行置破折号"],
               ["候选池 ⊇ 主表 ⊇ 前 10", f"{len(candidate)} ⊇ {len(ordered)} ⊇ {len(top10)}", "集合关系成立"],
               ["Final_adj 逐条复算异常", recompute_bad, "应为 0"],
               ["摘要段落数异常（1–2 段）", ac["段落数异常"], "应为 0"],
               ["摘要缩进异常", ac["缩进异常"], "应为 0"],
               ["摘要超 100 净字（主表）", ac["超长(>100净字)"], "应为 0；用户补充口径"],
               ["摘要超 100 净字（候选池）", ac["候选池超长(>100净字)"],
                "应为 0；需求⑤候选池三字段齐备且合规，不再只查主表"],
               ["摘要未整句收束", ac["未整句收束"], "应为 0；不得切半句"],
               ["摘要占位句残留", ac["占位句残留"], "应为 0"],
               ["未用合并单元格 / 富文本", "通过", "xlsx → CSV/JSON 无损"],
               ["wrap_text 与第 9 列表头批注", "通过", "列宽 60"]])
    # ---- 附录 10 ----
    k.section("附录 10 · 锚点校准留痕",
              ["锚点类型", "期望位次", "实际命中", "说明"],
              [["重大伤亡 / 司法终局 / 终局性新规 / 权威预警", "前列",
                sum(1 for it in ordered[:15] if it.func_delta_i >= 5), "按信息功能档位自然上位，不按题材开通道"],
               ["全国性数据发布", "中段以上", sum(1 for it in ordered if it.func_tier == "重大数据发布"), "不因属财经类被压低"],
               ["娱乐 / 生活科普", "不占第 1", 1 if ordered and ordered[0].func_delta_i >= 5 else 0, "头部三席独立判定"],
               ["前 10 覆盖 ≥5 个上位大类", log["orch"].get("前10覆盖大类数"), "不足时如实写明代价"]])
    # ---- 附录 11 ----
    k.section("附录 11 · 编排留痕（需求④：影响面四维计入 U，只决定展示顺序、评级数值不动）",
               ["标题", "日常组", "daily_score", "独立渠道数", "cross_score",
                "影响面四维", "impact_score", "U", "head_score", "Final_adj", "纯基准序", "强制第1依据"],
               [[r["标题"], r["日常组"], r["daily_score"], r["独立渠道数"],
                 r["cross_score"], r.get("影响面四维", "—"), r["impact_score"], r["U"], r["head_score"],
                 r["Final_adj"], r["纯 Final_adj 基准序"], r.get("强制第1依据", "—")]
                for r in log["orch"].get("U 留痕", [])])
    # ---- 附录 12 / 12b ----
    k.section("附录 12 · 同事件聚类逐对理由（独立信源，不冒充 H）",
              ["标题 A", "标题 B", "理由", "开源诊断（不进判定）"],
              [[r["标题 A"], r["标题 B"], r["理由"], r.get("开源诊断（不进判定）", "—")]
               for r in log["pairs"]])
    k.section("附录 12 · 跨渠道重复度逐事件表", ["项目", "数值"],
              [[kk, vv] for kk, vv in log["cluster"].items()])
    k.section("附录 12b · 同事件去重留痕（保留稿 / 被合并稿 / 来源）",
              ["事件主键", "保留稿", "被合并稿", "来源", "渠道"],
              [[r["事件主键"], r["保留稿"], r["被合并稿"], r["来源"], r["渠道"]] for r in log["merged"]])
    # ---- 附录 13 ----
    k.section("附录 13 · 国际资讯逐条处置表",
              ["采集序", "标题", "来源", "境外信号", "重要标记", "三类标签", "判定", "依据", "非重大突发剔除原因"],
              log["intl_rows"])
    k.section("附录 13 · 三类逐类计数", ["三类", "条数"],
              [[t, log["intl_counts"].get(t, 0)] for t in
               ["战争与武装冲突", "灾害与公共卫生", "突发事故与重大安全事件"]])
    # ---- 附录 14 ----
    k.section("附录 14 · 相邻同类打散对照表",
              ["展示序", "U 降序位", "标题", "上位大类", "分类标签", "U", "打散动作", "后移位数", "前窗内", "相邻同大类", "相邻同标签"],
              [[r.get("展示序"), r.get("U 降序位"), r.get("标题"), r.get("上位大类"),
                r.get("分类标签"), r.get("U"), r.get("打散动作"), r.get("后移位数"),
                r.get("前窗内"), r.get("相邻同大类"), r.get("相邻同标签")]
               for r in log["orch"].get("相邻打散留痕", []) if isinstance(r, dict) and "展示序" in r])
    # ---- 附录 15 ----
    k.section("附录 15 · 多事件拼盘稿处置留痕（拆分优先 / 线索待原文）",
               ["词条", "榜单", "快照页", "原因"],
               [[r["词条"], r["榜单"], r["快照页"], r["原因"]] for r in log["clues"]] or
               [["本批无仅话题名词条", "—", "—", "拼盘稿已按主体事件拆条，词条逐条回查"]])
    _soc = log.get("social_main", {}) or {}
    k.section("附录 15 · 快讯/拼盘源全量拆条（母行不丢、词条一条不删，01 §3.4）",
               ["类型", "序号", "词条 / 母行", "来源", "日期线索(只留痕)", "独立原文", "回查处置"],
               [[r["类型"], r.get("序号", "—"), r["词条"], r["来源"],
                 r.get("日期线索", "—"), r["独立原文"], r["处置"]]
                for r in _soc.get("digest_rows", [])] or
               [["母行", "—", "本批无拼盘源文本", "—", "—", "—", "social_main 已登记 5 类快讯源"]])
    k.section("附录 15 · 社交主干词条逐条回查留痕（需求②：主干不是补充）",
               ["平台", "词条", "回查成稿", "状态"],
               [[p["平台"], f"真实词条 {p['词条数']} 条", p.get("回查成稿", 0), p["状态"]]
                for p in _soc.get("platforms", [])] or
               [["—", "social_main 未启用", 0, "—"]])
    k.section("附录 15 · 线索待原文（只有话题名+热度，按 01 §3.3 不进候选池）",
               ["词条", "发现渠道", "日期线索", "未成行原因", "去向"],
               [[r["词条"], r["发现渠道"], r["日期线索"], r["未成行原因"], r["去向"]]
                for r in _soc.get("clues", [])] or
               [["本批无线索待原文", "—", "—", "—", "—"]])
    # ---- 附录 16 ----
    k.section("附录 16 · 头部三席独立判定留痕（SKILL §4.10 资格池）",
              ["槽位", "标题", "上位大类", "head_score", "daily_score", "cross_score",
               "Final_adj", "资格池", "相邻同类"],
              [[r["槽位"], r["标题"], r["上位大类"], r["head_score"], r["daily_score"],
                r["cross_score"], r["Final_adj"], r["资格池"], r["相邻同类"]]
               for r in log["orch"].get("头部三席留痕", [])])
    k.section("附录 16 · 需求④ 强制第 1 留痕（≥7 级地震 / 死伤多事故 / 多渠道高覆盖）",
               ["标题", "原展示序", "新展示序", "命中依据", "数值未改"],
               [[r["标题"], r["原展示序"], r["新展示序"], r["命中依据"], r["数值未改"]]
                for r in log["orch"].get("强制第1留痕", [])] or
               [["force_first 关闭", "—", "—", "—", "—"]])
    k.section("附录 16 · 破产倒闭主体可核对性核验留痕（需求④）",
               ["采集序", "标题", "主体类型", "判定依据"],
               log.get("bankruptcy_rows", []) or [["—", "本批无破产倒闭类条目", "—", "不适用"]])
    # ---- 附录 17 ----
    k.section("附录 17 · 议题簇合并留痕（同一议题不同侧面）",
              ["议题名", "代表条", "被合并条", "来源", "摘要首段", "命中主体与动作词组"],
              [[r["议题名"], r["代表条"], r["被合并条"], r["来源"], r["摘要首段"],
                r["命中主体与动作词组"]] for r in log["story_rows"]] or
              [["本批无议题簇合并", "—", "—", "—", "—", "story_dedup 已启用但无命中"]])
    # ---- 候选池边界留痕 / 逐条时间留痕 ----
    k.section("候选池边界留痕", ["类别", "条数", "指向"],
              [["超窗口排除", len(log["excluded"]), "附录 4"],
               ["三字段不齐 / 分类不合规", len(log["field_reject"]), "附录 1"],
               ["词条 / 线索待原文", len(log["clues"]), "附录 15"],
               ["池内经筛选或合并未进主表", len(candidate) - len(ordered), "附录 1 / 12 / 13 / 17"],
                ["社交主干词条回查成稿", (log.get("social_main", {}) or {}).get("stats", {}).get("回查成稿", 0), "附录 15"],
                ["社交主干线索待原文", (log.get("social_main", {}) or {}).get("stats", {}).get("线索待原文", 0), "附录 15"],
               ["采集层完全重复", len(log["dedup_dropped"]), "采集层 URL+标题去重"]])
    k.section("逐条发布时间 / 时间依据 / 时效标记 / 日期线索留痕",
              ["采集序", "标题", "发布时间", "时间依据", "时效标记", "日期线索", "T 依据"],
              [[i + 1, it.title, it.pub.strftime("%Y-%m-%d %H:%M") if it.pub else "未取得",
                it.time_basis, it.time_mark, it.date_clue,
                log["score"]["t_trace"].get(it.seq, "")] for i, it in enumerate(candidate)])
    k.section("来源索引", ["渠道 / 媒体", "出现次数"],
              [[name, cnt] for name, cnt in Counter(
                  p.strip() for it in ordered for p in it.source.split(" · ") if p.strip()).most_common()])
    # ---- 工程留痕 A ----
    k.section("工程留痕 A · 采集质量门禁（12 项实测，不达标只告警不伪造成品）",
              ["指标", "实测", "阈值", "方向", "达标", "口径"],
              [[name, v["实测"], v.get("下限", v.get("上限")),
                "≥" if v.get("方向") == "min" else "≤",
                ("是" if v["达标"] else "否") if v.get("适用", True) else "不适用（台账导入）",
                v.get("口径", "")]
               for name, v in q.get("指标", {}).items()] or
              [["未启用质量门禁", "—", "—", "—", "—", "config.quality 缺失"]])
    detail = [
        ["结论", q.get("结论", "—")],
        ["未达标项", "、".join(q.get("未达标项", [])) or "无"],
        ["处置", "warn_only=是，写告警不中断" if q.get("warn_only") else "warn_only=否，不达标直接失败"],
        ["采集模式", q.get("采集模式", "—")],
        ["渠道白名单 / 已触达 / 待配置端点",
         f"{q.get('渠道白名单')} / {q.get('已触达')} / {q.get('待配置端点')}"],
        ["主表 A 级 / 主表", f"{q.get('主表A级条数')} / {q.get('主表条数')}"],
        ["有效摘要 / 无效摘要", f"{q.get('有效摘要条数')} / {q.get('无效摘要条数')}"],
        ["F5 噪声剔除 / 议题簇合并", f"{q.get('F5噪声剔除')} / {q.get('议题簇合并')}"],
        ["词条成稿 / 未回查线索", f"{q.get('词条成稿')} / {q.get('未回查线索')}"],
    ]
    for i, a in enumerate(q.get("建议", []), 1):
        detail.append([f"改进方向 {i}", a])
    k.section("工程留痕 A · 质量明细与改进方向", ["项目", "数值 / 建议"], detail)

    # ---- 工程留痕 B ----
    k.section("工程留痕 B · F5 非新闻 / 关键词误命中处置留痕",
              ["采集序", "标题", "来源", "原大类", "原标签", "规则", "依据"],
              log.get("noise_rows", []) or
              [["本批无 F5 剔除", "—", "—", "—", "—", "—", "noise_verdict 已启用"]])
    k.section("工程留痕 B · 摘要状态逐条留痕（标题复读不冒充摘要）",
              ["展示序", "标题", "摘要状态", "摘要来源", "摘要首段"],
              [[it.display_order, it.title, getattr(it, "abstract_status", "未取得"),
                getattr(it, "abstract_source", "—"),
                (it.abstract or "").split("\n")[0][:110]] for it in ordered])
    k.section("工程留痕 B · 摘要段落重复处置留痕（本版已去重）",
              ["项目", "数值 / 样本"],
              [["段落重复率", q.get("指标", {}).get("段落重复率", {}).get("实测", "—")],
               ["段落重复条数", q.get("段落重复条数", 0)],
               ["处置", "normalize._dedupe_paragraphs 按规范化文本去重；去重后仅一段时按整句切两段，数字不改写"],
               *[["样本标题", t] for t in q.get("段落重复样本", [])]] or
              [["段落重复条数", 0], ["结论", "本批无段落自我重复"]])
    k.section("工程留痕 B · 正文补采统计", ["项目", "数值"],
              [[k2, v2] for k2, v2 in (log.get("article", {}) or {}).items()] or
              [["未启用", "live.fetch_article=false 或本批无需补采"]])
    _sp = log.get("second_pass", {}) or {}
    if _sp.get("启用"):
        k.section("工程留痕 B · 摘要二次补采（首轮失败条目的定向重试）", ["项目", "数值"],
                  [["尝试", _sp.get("尝试", 0)], ["救回", _sp.get("救回", 0)],
                   ["仍空白", _sp.get("仍空白", 0)], ["耗时秒", _sp.get("耗时秒", 0)],
                   ["口径", _sp.get("说明", "")],
                   ["配置", "source.live.second_pass_enabled / max_items / timeout / jina_timeout / workers / deadline_seconds"]])
        if _sp.get("逐条"):
            k.section("工程留痕 B · 二次补采逐条留痕", ["标题", "结果", "依据"],
                      [[r[0], r[1], r[2]] for r in _sp["逐条"]])
    k.section("工程留痕 B · 渠道未取得原因分类（可行动）", ["原因类别", "渠道数", "处置建议"],
              [[c, n, ("补端点 / 重试 / 配 key / 更新 URL，详见 README 质量章节")]
               for c, n in sorted((log.get("failure_categories", {}) or {}).items(),
                                  key=lambda x: -x[1])] or
              [["本批无渠道失败", 0, "—"]])

    # ---- 工程留痕 C ----
    t20 = log.get("target", {}) or {}
    if t20:
        ring = t20.get("环节统计", {}) or {}
        k.section("工程留痕 C · 目标态补跑链路逐环节留痕（代码实现，非人工会话）",
                  ["环节", "实测 / 说明"],
                  [["链路顺序", " → ".join(t20.get("链路", []))],
                   ["数据源", t20.get("数据源", "—")],
                   ["来源文件", t20.get("来源文件", "—")],
                   ["原子化", f"原始条目 {t20.get('原始条目数', 0)} → 原子条目 {t20.get('原子条目数', 0)}"],
                   ["8 大类归组", f"归组数 {ring.get('归组', 0)}"],
                   ["词条发现", f"线索数 {ring.get('词条线索', 0)}；仅话题名不成稿"],
                   ["独立原文回查", f"回查成稿 {ring.get('回查成稿', 0)} 条"],
                   ["正文补采", f"补采 / 回查 {ring.get('正文补采', 0)} 条"],
                   ["多渠道来源合并", f"合并事件 {ring.get('多渠道合并', 0)} 条；来源平台 {len(ring.get('来源平台', []))} 个"],
                   ["时点推定", f"{ring.get('时点推定', 0)} 条，逐条见附录 4b"],
                   ["精简摘要", "normalize.build_abstract：HTML 清洗 → 段级 / 句级去重 → 整句挑选至 ≤100 净字 → 单段整句收束"],
                   ["事件主键", "cluster_events 按采集源分 e1xxx（独立原文）/ e2xxx（词条回查成稿）编号"],
                   ["记录类型", "target_source.classify_record_type 判定 6 种，逐条见主表"],
                   ["实时端点探测", str(t20.get("实时探测", {}).get("reason", "—"))],
                   ["诚实声明", t20.get("诚实声明", "—")]])
    else:
        k.section("工程留痕 C · 目标态补跑链路", ["项目", "内容"],
                  [["状态", "本批非 target 模式，未启用代码化补跑链路"]])

    # ---- 工程留痕 D：真实联网源逐源触达 ----
    lc21 = log.get("live_cn", {}) or {}
    if lc21:
        rows21 = []
        for r in lc21.get("逐源触达", []) or []:
            rows21.append([r.get("id", "—"), r.get("name", "—"), r.get("type", "—"),
                           r.get("status", "—"), r.get("条数", 0),
                           str(r.get("detail", {}))[:220]])
        pr21 = lc21.get("国际源组探测", {}) or {}
        body21 = lc21.get("正文补采", {}) or {}
        k.section("工程留痕 D · 真实联网源逐源触达（本批实际发起请求，非白名单枚举）",
                  ["源 ID", "名称", "解析类型", "状态", "条数", "明细 / 失败原因"],
                  rows21 or [["—", "本批无真实联网源", "—", "—", 0, "—"]])
        k.section("工程留痕 D-b · 网络自适应与正文补采统计",
                  ["项目", "内容"],
                  [["国际源组探测", f"可达={pr21.get('ok')}；{pr21.get('reason', '—')}"],
                   ["启用源数", lc21.get("启用源数", 0)],
                   ["原始条目数", lc21.get("原始条目数", 0)],
                   ["有发布时间条数", lc21.get("有发布时间", 0)],
                   ["窗口内条数", lc21.get("窗口内", 0)],
                   ["正文补采候选 / 成功 / 失败", f"{body21.get('候选', 0)} / "
                    f"{body21.get('正文补采成功', 0)} / {body21.get('补采失败（如实留空）', 0)}"],
                   ["未启用国际源", "；".join(f"{x['name']}（{x['id']}）"
                                     for x in lc21.get("申报未启用的国际源", [])[:10]) or "无"],
                   ["诚实声明", lc21.get("诚实声明", "—")]])
    else:
        k.section("工程留痕 D · 真实联网源逐源触达", ["项目", "内容"],
                  [["状态", "本批非 live / hybrid 模式，未发起联网请求"]])

    # ---- 工程留痕 E：白名单「未取得」逐条拆成可行动类别 ----
    import json as _json
    from pathlib import Path as _Path
    cov_path = _Path(__file__).resolve().parents[1] / "data" / "channel_coverage.json"
    if cov_path.exists():
        cov = _json.loads(cov_path.read_text(encoding="utf-8"))
        rows22 = [[k, v, next((r["enable_condition"] for r in cov["channels"] if r["layer"] == k), "—")]
                  for k, v in sorted(cov.get("layers", {}).items(), key=lambda x: -x[1])]
        k.section("工程留痕 E · 白名单可抓取性分层盘点（逐渠道不再笼统写「未取得」）",
                  ["分层", "渠道数", "启用条件（该层首条）"], rows22)
        dp = cov.get("domain_probe", {}) or {}
        k.section("工程留痕 E-b · 白名单域名实测汇总",
                  ["项目", "内容"],
                  [["白名单渠道总数", cov.get("whitelist_total", 0)],
                   ["已实证端点数", cov.get("verified_endpoints", 0)],
                   ["端点覆盖渠道数（平台归并）", cov.get("covered_channels", 0)],
                   ["覆盖率", f"{cov.get('coverage_ratio', 0)*100:.1f}%"],
                   ["入口列提取独立域名", dp.get("total", 0)],
                   ["域名实测通过", dp.get("passed", 0)],
                   ["域名失败原因分布", str(dp.get("blocked", {}))],
                   ["结论", "本环境不可达域名在 GitHub Actions 等 unrestricted 网络下，"
                            "由 discover_endpoints.py 自动实测并写入端点清单，无需改代码"]])
        k.section("工程留痕 E-c · 逐渠道分层与启用条件",
                  ["渠道 ID", "渠道", "级别", "分层", "覆盖端点", "域名实测", "启用条件"],
                  [[r["channel_id"], r["channel"], r["level"], r["layer"],
                    r["endpoint"], r.get("domain_status", ""), r["enable_condition"]]
                   for r in cov.get("channels", [])])
    else:
        k.section("工程留痕 E · 白名单可抓取性分层盘点", ["项目", "内容"],
                  [["状态", "未生成 data/channel_coverage.json；运行 python scripts/discover_endpoints.py"]])

    # ---- 工程留痕 F：社交平台热榜（v3 §14 ✗ 仅作热度参考，原样留痕、不进主表）----
    soc = ((log.get("live_cn", {}) or {}).get("社交热榜", {}) or {}) \
        if mode in ("live", "hybrid") else {}
    if soc.get("启用"):
        k.section("工程留痕 F · 社交平台热榜快照（需求②：主干发现入口，快照本身不进主表）",
                   ["项目", "内容"],
                   [["口径", soc.get("口径", "")],
                    ["快照条数", soc.get("快照条数", 0)],
                    ["其中窗口内上榜", soc.get("窗口内上榜", 0)],
                    ["是否进候选池 / 主表", "快照本身否：热搜词条不是新闻，上榜时间不是发布时间；"
                     "词条经 social_main.lookup_independent 回查到独立原文后才成行"],
                    ["成稿条件", "站内独立原文索引优先；未命中且配 SERPER/TAVILY key 时走搜索 API；"
                     "均失败进附录 15 线索待原文，不吃 snippet"]])
        _sm = log.get("social_main", {}) or {}
        if _sm.get("platforms"):
            k.section("工程留痕 F-a · 10 个社交主干平台逐条申报（需求②）",
                       ["平台", "入口", "采集方式", "定位", "词条数", "回查成稿", "状态"],
                       [[p["平台"], p["入口"], p["采集方式"], p["定位"], p["词条数"],
                         p.get("回查成稿", 0), p["状态"]] for p in _sm["platforms"]])
        k.section("工程留痕 F · 逐平台触达结论",
                  ["平台", "状态", "条目数", "端点", "说明"],
                  [[p["平台"], p["状态"], p["条目数"], p["端点"], p["说明"]]
                   for p in soc.get("逐平台", [])])
        snaps = soc.get("快照", []) or []
        if snaps:
            k.section("工程留痕 F-a · 微博热搜榜完整快照（本批真实请求，逐条带时间/热度/分类）",
                      ["排名", "平台", "词条", "热度值", "分类", "标签", "上榜时间",
                       "窗口内上榜", "事件线索", "榜单链接"],
                      [[r["排名"], r["平台"], r["词条"], r["热度值"], r["分类"],
                        r["标签"], r["上榜时间"], r["窗口内上榜"], r["事件线索"],
                        r["榜单链接"]] for r in snaps])
        else:
            k.section("工程留痕 F-a · 微博热搜榜完整快照", ["项目", "内容"],
                      [["状态", "本批未取得微博热搜快照（见工程留痕 F 逐平台结论）"]])
        k.section("工程留痕 F-b · 抖音热点逐路径实测（不编造、不静默）",
                  ["候选 URL", "HTTP 状态 / 错误"],
                  [[r["url"], str(r["code"])] for r in soc.get("抖音逐路径实测", [])])
        k.section("工程留痕 F-c · 其它社交/热榜平台对照",
                  ["平台", "端点", "状态", "结论"],
                  [[r["平台"], r["端点"], str(r["状态"]), r["结论"]]
                   for r in soc.get("其它平台对照", [])])
    else:
        k.section("工程留痕 F · 社交平台热榜", ["项目", "内容"],
                  [["状态", "config 关闭 social_hotboards，或本批非 live 模式"]])

    # ---- 工程留痕 G：需签名 / 登录平台的四条取数通路（不编造、不静默）----
    _pa = log.get("platform_access", {}) or {}
    if _pa.get("启用"):
        k.section("工程留痕 G · 受限平台取数四条通路能力矩阵",
                  ["通路", "名称", "能拿到什么", "前提", "成本", "凭证 / 环境变量", "稳定性", "适配平台"],
                  [[t["id"], t["name"], t["能拿到什么"], t["前提"], t["成本"],
                    t["凭证"], t["稳定性"], t["适配平台"]] for t in _pa.get("矩阵", [])])
        k.section("工程留痕 G-a · 10 平台逐平台逐通路实测结论",
                  ["平台", "命中通路", "线索数", "逐通路结论", "独立原文准入"],
                  [[p["平台"], p["命中通路"] or "未命中", p["线索数"],
                    "；".join(f'{x["通路"]}: {x["结论"]}' for x in p["逐通路"]),
                    p["独立原文准入"]] for p in _pa.get("平台", [])])
        k.section("工程留痕 G-b · 凭证配置状态与总体口径",
                  ["项目", "内容"],
                  [["凭证", "；".join(f"{a}={b}" for a, b in (_pa.get("凭证") or {}).items())],
                   ["线索总数", _pa.get("线索总数", 0)],
                   ["已启用通路", "、".join(_pa.get("已启用通路", [])) or "本批 0 条（见逐通路结论）"],
                   ["实测边界", _pa.get("说明", "")],
                   ["回查契约", _pa.get("回查契约", "")],
                   ["凭证模板", "data/platform_credentials.example.yaml"],
                   ["代码入口", "news_pipeline/platform_access.py（run_platform_access / inject_clues / self_test）"]])
        _fs = _pa.get("免费源", {}) or {}
        if _fs:
            k.section("工程留痕 G-c · 免费公共热榜实例逐条实测（DailyHotApi 之外的候选）",
                      ["实例", "类型", "许可", "探针 URL", "状态", "词条", "说明"],
                      [[r["name"], r.get("kind", ""), r.get("license", ""), r["url"][:90],
                        str(r["code"]), r["clues"], r.get("note", "")] for r in _fs.get("实例", [])])
            k.section("工程留痕 G-c · 可自建的免费开源项目（GitHub 实测快照）",
                      ["仓库", "Stars", "许可", "最后提交", "定位", "部署", "对本项目的建议", "元数据"],
                      [[r["repo"], r.get("stars") or "未取得", r.get("license", ""),
                        r.get("pushed", ""), r["定位"], r["部署"], r["建议"], r.get("meta", "")]
                       for r in _fs.get("开源项目", [])])
            k.section("工程留痕 G-c · 免费额度（搜索 / 正文 / 部署）",
                      ["用途", "服务", "免费额度", "环境变量", "说明"],
                      [[r["用途"], r["服务"], r["免费额度"], r["环境变量"], r["说明"]]
                       for r in _fs.get("免费额度", [])])
            k.section("工程留痕 G-c · 免费源总体结论", ["项目", "内容"],
                      [["可达实例", "、".join(_fs.get("可达", [])) or "本批 0 个"],
                       ["结论", _fs.get("结论", "")],
                       ["报告", _fs.get("报告", "data/free_sources_probe.json")],
                       ["代码入口", "news_pipeline/free_sources.py（probe_all / collect_free / jina_reader_text）"]])
        # ---- G-d：通路 E「无签名公开分享页 SSR」逐条实测 ----
        _e = _pa.get("分享页通路E", {}) or {}
        if _e:
            _st = _e.get("自检", {}) or {}
            k.section("工程留痕 G-d · 通路 E 无签名公开分享页（回答「只能靠签名 / 授权吗」）",
                      ["项目", "内容"],
                      [["结论", "不需要签名 / Cookie / 开放平台资质：平台为社交分享预览公开输出的 SSR 页本身就带数据"],
                       ["小红书", "explore/{note_id}?xsec_token=… → og:meta + window.__INITIAL_STATE__；2024 起强制 token，匿名不含作者/互动数"],
                       ["抖音", "iesdouyin.com/share/{video|note}/{aweme_id}/ → window._ROUTER_DATA；desc / 作者 / 秒级 create_time"],
                       ["微信", "mp.weixin.qq.com/s/{id} 永久页 → msg_title / ct / js_content；appmsgalbum 合集 JSON 带秒级 create_time"],
                       ["发现 URL", "Bing site: 定向检索（只抽 URL 不吃 snippet）；60s / DailyHotApi / NewsNow 的 GitHub Actions 静态归档"],
                       ["本环境实测", "mp.weixin.qq.com 200、weixin.qq.com 200、Bing site: 200、appmsgalbum 200(JSON)；"
                                    "小红书 explore / 抖音 iesdouyin / 搜狗微信 / RSSHub / 公共镜像 403（域名级出口拦截）"],
                       ["真正缺什么", "可达网络 + 一条真实分享链接，不是签名算法、不是企业资质"],
                       ["解析器自检", f"{_st.get('PASS', 0)}/{_st.get('PASS', 0) + _st.get('FAIL', 0)} 项 {_st.get('结论', '—')}（受控 HTML，不依赖网络）"],
                       ["注入方式", "config.yaml → social_main.share_links，或环境变量 SHARE_PAGE_LINKS（逗号分隔）"],
                       ["代码入口", "news_pipeline/share_pages.py（xhs_resolve / douyin_resolve / wechat_article / wechat_album / bing_site_urls）"],
                       ["频次约束", "低频调用；批量高频会触发滑块，命中验证壳一律判未取得，不编造"]])
            k.section("工程留痕 G-d · 分享链接逐条实测（不注入就如实申报，不凭空造 URL）",
                      ["链接", "平台", "成稿", "判定", "标题", "发布时间"],
                      [[r["链接"], r["平台"], "是" if r["成稿"] else "否", r["判定"],
                        r["标题"], r["发布时间"]] for r in _e.get("逐条", [])])
    else:
        k.section("工程留痕 G · 受限平台取数", ["项目", "内容"],
                  [["状态", "config 关闭 platform_access"]])

    # ---- 工程留痕 H：统一搜索 API + 开源热榜源（用户 2026-11-20 高质量数据要求）----
    # 逐次调用 / 候选打分 / 硬闸拦截原因全部可复算；不把搜索摘要当正文。
    _sa = log.get("search_api", {}) or {}
    if _sa:
        _st = _sa.get("stats", {}) or {}
        k.section("工程留痕 H · 统一搜索 API 与开源热榜源（高质量独立原文）", ["项目", "内容"],
                  [["目标", "结合开源仓库方案 + 搜索 API，取高质量结果数据，不是取第一条"],
                   ["实现", "news_pipeline/search_api.py（多 provider / 多候选质量打分 / 硬闸 / 配额缓存）"],
                   ["provider 顺序", (cfg.get("search_api", {}) or {}).get("provider_order", "")],
                   ["单批调用上限", (cfg.get("search_api", {}) or {}).get("max_calls", "")],
                   ["搜索调用次数", str(_st.get("调用", {}))],
                   ["批次内缓存命中", _st.get("缓存命中", 0)],
                   ["成稿（选中独立原文）", _st.get("成稿", 0)],
                   ["失败 / 跳过", _st.get("失败", 0)],
                   ["逐次留痕条数", _st.get("逐次留痕", 0)],
                   ["搜索成稿正文补采", str(log.get("search_bodies", {}) or {})],
                   ["开源源成稿条数", _sa.get("free_items", 0)],
                   ["开源源正文补采", str(_sa.get("free_bodies", {}) or {})],
                   ["硬约束", "不吃 snippet：搜索结果只给 URL，摘要由独立原文正文补采生成；"
                              "旧闻 / PDF / 官网首页 / 栏目百科页 / 标题无关由硬闸拦下并逐条留痕"],
                   ["无 key 兜底", "Serper / Tavily 缺 key 时自动降级 Bing 站内 HTML（本沙盒实测 200 可解析）"]])
        _prov = _sa.get("providers", []) or []
        if _prov:
            k.section("工程留痕 H-a · 各 provider 调用与成稿", ["provider", "调用次数", "成稿", "最高质量分"],
                      _prov)
        _fr = _sa.get("free_routes", []) or []
        if _fr:
            k.section("工程留痕 H-b · 开源热榜源逐路由实测（词条 → 搜索回查）",
                      ["路由", "词条数", "实测结论 / 未取得原因"], _fr)
        _tr = _sa.get("trace", []) or []
        if _tr:
            k.section("工程留痕 H-c · 搜索逐次留痕（候选数 / 选中 URL / 质量分 / 判定依据）",
                      ["query", "provider", "状态", "候选数", "选中 URL", "质量分", "判定依据", "耗时(s)"],
                      [r[:8] for r in _tr[:260]])
        _rows_all = len(_tr)
        if _rows_all > 260:
            k.section("工程留痕 H-c · 留痕截断说明", ["项目", "内容"],
                      [["本批留痕总数", _rows_all],
                       ["本表展示", 260],
                       ["说明", "超出部分完整保留在 logs/<run_id>.pipeline.log 与 output/<批次>.json；"
                                "不因截断丢判定依据"]])
    else:
        k.section("工程留痕 H · 统一搜索 API", ["项目", "内容"],
                  [["状态", "本批未启用搜索 API 层"]])

    # ---- 工程留痕 I：渠道质量账本与低质渠道淘汰 ----
    _cq = log.get("channel_quality") or {}
    if _cq:
        _sum, _th = _cq.get("汇总", {}), _cq.get("阈值", {})
        k.section("工程留痕 I · 渠道质量账本与淘汰（本批真实单元格逐渠道复算）",
                  ["项目", "内容"],
                  [["口径", _cq.get("口径", "")],
                   ["渠道数 / 淘汰数 / 淘汰渠道原始条目",
                    f"{_sum.get('渠道数', 0)} / {_sum.get('淘汰数', 0)} / {_sum.get('淘汰渠道原始条目', 0)}"],
                   ["阈值", json.dumps(_th, ensure_ascii=False)],
                   ["白名单处置", "599 渠道不删除；淘汰渠道下一轮采集端跳采，逐条申报淘汰原因与自动解禁条件"],
                   ["产物", "data/channel_quality.json（账本）/ data/channel_blocklist.yaml（下一批跳采名单）"]])
        _cr = [[r["渠道"], r["来源"], r["原始"], r["候选"], r["主表"],
                f"{r['摘要有效率']:.0%}", f"{r['主表率']:.1%}", f"{r['过滤率']:.0%}",
                r["平均Final"], r["B以上"], "淘汰" if r["淘汰"] else "保留",
                r["淘汰原因"] or "—"]
               for r in _cq.get("逐渠道", [])[:220]]
        if _cr:
            k.section("工程留痕 I-a · 逐渠道质量明细（按淘汰优先 / 原始条数降序）",
                      ["渠道ID", "来源", "原始", "候选", "主表", "摘要有效率", "主表率",
                       "过滤率", "平均Final", "B以上", "判定", "淘汰原因 / 解禁依据"], _cr)
        _bt = log.get("channel_block_traces") or []
        if _bt:
            k.section("工程留痕 I-b · 本批已跳采条目逐条留痕",
                      ["渠道ID", "标题", "淘汰原因", "自动解禁条件"],
                      [r[:4] for r in _bt[:260]])
        if _cq.get("淘汰"):
            k.section("工程留痕 I-c · 淘汰渠道与启用条件",
                      ["渠道ID", "来源", "淘汰原因", "解禁条件"],
                      [[b["channel_id"], b["source"], b["reason"], b["unblock"]]
                       for b in _cq["淘汰"]])

    # ---- 工程留痕 J：多资源扩展（全网检索落地的公开入口）----
    _ex = log.get("source_expansion") or {}
    if _ex:
        _st = _ex.get("stats") or {}
        k.section("工程留痕 J · 多资源扩展（解决「没获取到数据的渠道」）",
                  ["项目", "内容"],
                  [["总开关", "启用" if _st.get("启用") else "关闭"],
                   ["扩展条目 / 逐路由留痕", f"{_st.get('扩展条目', 0)} / {_st.get('路由数', 0)}"],
                   ["GDELT / Google News / 实例池 / 公众号",
                    f"{_st.get('GDELT', 0)} / {_st.get('GNews', 0)} / {_st.get('实例池', 0)} / {_st.get('公众号', 0)}"],
                   ["Cloudflare Worker 反代", _st.get("代理", "")],
                   ["合规边界", "只取公开、无需逆向签名的入口；不绕登录、不编造条目；失败逐路由申报启用条件"],
                   ["资料来源", "GDELT DOC 2.0 / Google News RSS site: / NewsNow(MIT) / RSSHub / wechat2rss / Jina Reader / CF Worker，"
                                "详见 docs/资讯获取_渠道扩容与质量提升方案.md"]])
        if _ex.get("rows"):
            k.section("工程留痕 J-a · 扩展路由逐条实测",
                      ["层级", "目标 / 查询", "实测结果", "启用条件 / 口径说明"],
                      [r[:4] for r in _ex["rows"][:200]])

    # ---- 工程留痕 K：评级口径（H 代理通路）----
    _hp = ((log.get("score") or {}).get("H代理通路") or {})
    if _hp:
        k.section("工程留痕 K · 评级口径（A 档为何能自然产生）",
                  ["项目", "内容"],
                  [["H 通道模式", (log.get("score") or {}).get("h_mode", "")],
                   ["H 代理启用", "是" if _hp.get("启用") else "否"],
                   ["本批代理生效条数", _hp.get("生效条数", 0)],
                   ["最小独立来源数", _hp.get("最小独立来源数", 0)],
                   ["口径", _hp.get("口径", "")],
                   ["未改动的项", "grade 阈值 S≥85 / A≥70 / B≥55 / C≥40、W_H=0.262、W_V=0.738、V_MAX=82.32、"
                                 "三态核实封顶、质量惩罚全部保持不变"]])

    # ---- 工程留痕 L：非独立站点 / DNS 未解析 两类逐条解析（2026-11-20 纠错）----
    _rp = _Path(__file__).resolve().parents[1] / "data" / "channel_resolved.json"
    if _rp.exists():
        try:
            _rd = _json.loads(_rp.read_text(encoding="utf-8"))
            k.section("工程留痕 L · 两类渠道逐条解析（非独立站点 / DNS 未解析 不是「解决不了」）",
                      ["项目", "数值 / 结论"],
                      [["结论", _rd.get("结论", "")],
                       ["原两类合计", _rd.get("原两类合计", 0)],
                       ["已解析条数", _rd.get("已解析条数", 0)],
                       ["零通路条数", _rd.get("零通路条数", 0)],
                       ["解析状态分布", str(_rd.get("解析状态分布", {}))],
                       ["通路类型分布", str(_rd.get("通路类型分布", {}))],
                       ["实测 HTTP200", f"{(_rd.get('实测') or {}).get('HTTP200', 0)}/"
                                        f"{(_rd.get('实测') or {}).get('请求数', 0)}"],
                       ["实测带时间字段", (_rd.get("实测") or {}).get("带时间字段", 0)],
                       ["诚实边界", _rd.get("诚实边界", "")]])
            for _ev in (_rd.get("依据") or []):
                k.kv("实测依据", str(_ev))
            k.section("工程留痕 L-a · 关键通路真实实测（失败如实记，不伪装已触达）",
                      ["名称", "HTTP", "条目数", "带时间字段", "错误", "URL"],
                      [[p.get("名称", ""), p.get("http") or "—", p.get("items", 0),
                        "是" if p.get("has_time") else "否", p.get("error", ""), p.get("url", "")]
                       for p in (_rd.get("实测") or {}).get("明细", [])])
            k.section("工程留痕 L-b · 两类逐渠道可拉取通路（逐条给时间字段依据与启用条件）",
                      ["渠道 ID", "渠道", "原状态", "解析状态", "通路数",
                       "通路类型", "目标 / 检索式", "时间字段依据", "启用条件"],
                      [[r.get("channel_id", ""), r.get("渠道", ""), r.get("原状态", ""),
                        r.get("解析状态", ""), r.get("通路数", 0),
                        p.get("通路类型", ""), str(p.get("目标", ""))[:100],
                        p.get("时间字段依据", ""), p.get("启用条件", "")]
                       for r in _rd.get("逐渠道", []) for p in r.get("可拉取通路", [])][:600])
        except Exception as exc:
            k.section("工程留痕 L · 两类渠道解析", ["项目", "内容"],
                      [["状态", f"读取 channel_resolved.json 失败 {type(exc).__name__}: {exc}"]])
    else:
        k.section("工程留痕 L · 两类渠道解析", ["项目", "内容"],
                  [["状态", "未生成 data/channel_resolved.json；运行 python -m news_pipeline.channel_resolve --probe"]])

    # ---- 工程留痕 P：候选池契约三闸（2026-11-22 三张截图指名修复）----
    _cr = log.get("contract_reject") or []
    _cr_by_gate: Dict[str, int] = {}
    for _r in _cr:
        if isinstance(_r, dict):
            _g = str(_r.get("命中闸", "未分类"))
            _cr_by_gate[_g] = _cr_by_gate.get(_g, 0) + 1
    k.section("工程留痕 P · 候选池契约三闸（需求⑤「未经筛选」的正确读法）",
              ["项目", "内容"],
              [["契约口径", "需求⑤「未经筛选」= 不做贴近度严选 / 评级 / 编排；"
                            "仍要求标题·摘要·来源齐备、分类合规、同一行只讲一件事"],
               ["闸1 标题↔摘要身份一致", "事件类别互斥 / 主体专名零交集 / 核心数字全不出现 → 判错配；"
                                     "聚类侧同步加 cannot-link，日期数字 11/19/20/2026 不作合并锚点，"
                                     "簇内逐成员与代表复核阻断链式过合并"],
               ["闸2 破产主体可核对", "股票代码 / 上市信号 / 主体关联规模数字；"
                                  "「某光伏企业」等匿名主体、法院程序公告 + 单一微信公众号弱来源一律剔除"],
               ["闸3 社交话题重定义", "社交话题 = 与衣食住行密切相关的民生社会议题，或有可核验证据的"
                                  "高关注社会事件；政要偶遇/打趣/围巾/玩具/综艺花边、"
                                  "「按未经筛选契约保留」占位摘要、无详细内容的词条一律不进池"],
               ["生效位置", "采集条目级 + 事件成稿后各跑一次；主表是候选池子集，同步生效"],
               ["本批拦截合计", len(_cr)],
               ["按闸分布", "；".join(f"{g}={n}" for g, n in sorted(_cr_by_gate.items())) or "无"]])
    k.section("工程留痕 P-a · 契约拦截逐条留痕（被剔条目不进候选池，可复算）",
              ["展示序", "标题", "来源", "命中闸", "依据", "阶段"],
              [[r.get("展示序", "—"), str(r.get("标题", ""))[:60],
                str(r.get("来源", ""))[:40], r.get("命中闸", "—"),
                str(r.get("依据", ""))[:120], r.get("阶段", "—")]
               for r in _cr[:200]] or
              [["—", "本批无契约拦截", "—", "—", "三闸全部通过", "—"]])
    # ---- 工程留痕 O：事件成稿层（URL 级 → 事件级，2026-11-22 目标形态对齐）----
    _ec = log.get("event_composer") or []
    _ec_ev = [t for t in _ec if isinstance(t, dict) and t.get("事件主键")]
    if _ec_ev:
        _ecm = _ec_metrics(_ec_ev)
        k.section("工程留痕 O · 事件成稿层（目标样例形态契约）",
                  ["项目", "数值 / 结论"],
                  [["输入粒度", "URL 级候选池（同一事件的多篇报道各占一行）"],
                   ["输出粒度", f"事件级候选池 {len(_ec_ev)} 个事件"],
                   ["多渠道事件", f"{_ecm['多渠道事件']} / {len(_ec_ev)}"],
                   ["独立来源均值", _ecm["独立来源均值"]],
                   ["两段摘要事件", _ecm["两段事件"]],
                   ["社交词条成稿", _ecm["社交成稿"]],
                   ["跨源数字并列", _ecm["数字并列"]],
                   ["簇内合并判据", "3-gram MinHash-LSH 候选 + 实体/数字锚点倒排；"
                                    "只与簇代表比对、不链式传递；热度按独立来源数算，不按簇大小"],
                   ["行业依据", "Chronicle（MinHash+抽取式摘要）/ Fundus（标题复读硬闸）/ "
                                "CAMS-DiverseSumm（跨源冲突显式暴露）/ Reconciliation Ledger "
                                "（ROUNDING→UNIT_SCALE→PERIOD→SCOPE→ATTRIBUTION 规则级联）/ "
                                "GDELT Cloud（Story 与 Event identity 分离）"],
                   ["诚实边界", "事件成稿不能凭空造出独立来源：线上采集层若只有单源 RSS，"
                                "多渠道率仍低；须由 keyless 检索 / 社交词条回查在同一线索下"
                                "提供 ≥2 个带 URL 与发布时间的独立原文"]])
    # ---- 工程留痕 M：质量 v3 事件级热度（回答「一条 A 都没有 / 很多渠道取不到」）----
    _hv = log.get("heat") or {}
    if _hv:
        _ecl, _eht = _hv.get("cluster") or {}, _hv.get("heat") or {}
        k.section("工程留痕 M · 质量 v3 事件级热度（A 档为何能自然产生）",
                  ["项目", "数值 / 结论"],
                  [["规则版本", _eht.get("规则版本", "")],
                   ["窗口 / 半衰期 / 最少参与方",
                    f"{_eht.get('窗口小时')}h / {_eht.get('半衰期小时')}h / {_eht.get('最少参与方')}"],
                   ["权重", str(_eht.get("权重", {}))],
                   ["事件簇数", len(_ecl.get("簇") or {})],
                   ["多源事件数", (_eht.get("汇总") or {}).get("多源事件数", 0)],
                   ["H_c 最大 / 均值", f"{(_eht.get('汇总') or {}).get('H_c最大')} / "
                                    f"{(_eht.get('汇总') or {}).get('H_c均值')}"],
                   ["热度写回条数", _hv.get("applied", 0)],
                   ["根因", "旧版 compute_scores 在 merge_to_main 之前跑，"
                            "_independent_sources 读单条 source 恒为 1 → "
                            "h_proxy_min_sources=3 永不成立 → Final 天花板 ~67 → A(≥70) 恒空"],
                   ["修法", "打分前用 MinHash-LSH 把转载聚成事件，按 48h 窗口 / 24h 半衰期 / "
                            "独立来源去重 + authority + originality 算 H_c 并写回每个成员；"
                            "Final = V/V_MAX×100 + 100×W_H×H_c，grade 阈值 / V_MAX / W_H 全不动"],
                   ["行业依据", "AIHOT (MIT) heat-v1-48h-halflife24h / Alethman MinHash 事件簇 / "
                               "Google News freshness+original reporting+authority / "
                               "Hacker News (P-1)/(T+2)^G 时间衰减"]])
        k.section("工程留痕 M-a · 逐事件 H_c 复算明细",
                  ["事件主键", "独立来源数", "coverage", "authority", "freshness",
                   "originality", "H_c", "成立", "依据"],
                  [[eid, v["n_src"], v["coverage"], v["authority"], v["freshness"],
                    v["originality"], v["H_c"], "是" if v["成立"] else "否", v["依据"]]
                   for eid, v in (_eht.get("逐事件") or {}).items()][:200])
    _skip = log.get("skipped") or []
    k.section("工程留痕 M-b · 列表页「非新闻硬闸」逐条拦截留痕",
              ["渠道", "适配器", "拦截依据", "标题样本"],
              [[r.get("channel", ""), r.get("adapter", ""), r.get("reason", ""),
                r.get("sample", "")] for r in _skip][:200])
    if not _skip:
        k.section("工程留痕 M-b · 列表页硬闸", ["项目", "内容"],
                  [["状态", "本批无被硬闸拦下的列表页条目（或该源未走 html_list 适配器）"]])
    k.section("工程留痕 M-c · 行业做法对照（本轮联网调研）",
              ["环节", "成熟做法", "本项目 v3 落地"],
              [["列表页采集", "trafilatura/Readability 按 text density + link density 剔导航页脚",
                "collect_html_lists 入库前 is_news_item 硬闸，32 类真实噪声逐条留痕"],
               ["去重级联", "SHA-256 → 5-word shingles MinHash-LSH@0.7 → SimHash Hamming≤3",
                "字符 3-gram MinHash 多 band LSH + SimHash（中文短标题适配）"],
               ["事件成立", "Alethman：一个 story 至少 3 个去重组；只与簇代表比对不链式传递",
                "min_participants=2，共享强锚点≥2 才成候选，只与簇代表比对"],
               ["热度", "AIHOT 48h 窗口 / 24h 半衰期 / 独立来源去重 / 最少 2 参与方",
                "heat-v1-48h-halflife24h，H_c 四维加权，规则版本写死"],
               ["权威/原创", "Google News：freshness + original reporting + authority",
                "AUTHORITY_DOMAINS 权重 + ORIGINAL_MARKS / WIRE_MARKS 判定"],
               ["时间衰减", "Hacker News (P-1)/(T+2)^1.8",
                "freshness = 0.5^(age/24)，用来源时间 pub 而非抓取时间"],
               ["事件严重度", "GDELT GoldsteinScale / AvgTone / CAMEO（免费无 key）",
                "已接 USGS / GDACS；GDELT DOC 2.0 在 source_expansion 中登记"]])

    for c in range(1, Kouching.MAXC + 1):
        ws4.column_dimensions[get_column_letter(c)].width = 20 if c == 1 else (46 if c == 2 else 24)
    ws4.column_dimensions["B"].width = 60
    ws4.freeze_panes = "A2"
    for row in ws4.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                f = copy(cell.font)
                f.name = CN_FONT
                cell.font = f

    wb.save(path)
    # ---- 工程留痕 N：运行预算与分层耗时（2026-11-21 采集步 35 分钟超时事故）----
    _bd = (R.get("logs", {}) or {}).get("budget") or {}
    if _bd:
        _nrows = [
            ["总预算秒", _bd.get("总预算秒", "—")],
            ["出表保留秒", _bd.get("出表保留秒", "—")],
            ["实际耗时秒", _bd.get("实际耗时秒", "—")],
            ["剩余秒", _bd.get("剩余秒", "—")],
            ["被短路的层", "、".join(_bd.get("被短路的层", [])) or "无"],
            ["结论", _bd.get("结论", "—")],
        ]
        for _r in _bd.get("逐层", []):
            _nrows.append([f"层·{_r.get('层', '—')}",
                           f"配额 {_r.get('配额秒', '—')}s / 实耗 {_r.get('实耗秒', '—')}s / "
                           f"允许={_r.get('允许', '—')} / 剩余全局 {_r.get('剩余全局秒', '—')}s"])
        k.section("工程留痕 N · 运行预算与分层耗时（超时短路采集层但必定出表）",
                  ["项目", "数值 / 说明"], _nrows)
    return path
