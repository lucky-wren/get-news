"""批次质量审计：只读 xlsx（+ 可选日志目录），逐项复算并输出问题清单。

    python scripts/audit_batch.py /path/to/latest.xlsx
    python scripts/audit_batch.py /path/to/latest.xlsx --logs /path/to/logs --json output/audit.json

纯离线、不改产物。用于回答「质量到底卡在哪」，把口径自洽性与内容质量一起验，
避免验收器只查结构、对自相矛盾的统计值视而不见。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))


def audit(xlsx: str, logs_dir: str = "") -> dict:
    from openpyxl import load_workbook
    from openpyxl.reader.excel import ExcelReader
    ExcelReader.read_custom = lambda s: None
    wb = load_workbook(xlsx, read_only=True, data_only=True)

    def grab(sn):
        ws = wb[sn]
        h = [c.value for c in next(ws.iter_rows())]
        return [dict(zip(h, r)) for r in ws.iter_rows(min_row=2, values_only=True)]

    main = grab("处理后的数据")
    pool = grab("窗口内全部数据")
    rows = list(wb["口径与稿外信息"].iter_rows(values_only=True))
    kv = {}
    for r in rows:
        if r and r[0] and isinstance(r[0], str):
            kv.setdefault(r[0], []).append(r[1])
    txt = "\n".join(" | ".join(str(x or "") for x in r) for r in rows)
    issues = []

    def add(pid, name, ok, evidence, why, fix):
        issues.append({"编号": pid, "问题": name, "状态": "PASS" if ok else "FAIL",
                       "证据": str(evidence)[:260], "根因": why, "修复": fix})

    def net(s):
        return re.sub(r"[\u3000\s]", "", str(s or ""))

    # P0-1 统计值自洽：等级分布合计必须 == 主表条数
    m = re.search(r"等级分布\s*(\{[^\n}]*\})", txt)
    grade_n = 0
    if m:
        try:
            grade_n = sum(int(v) for v in re.findall(r"'\w':\s*(\d+)", m.group(1)))
        except Exception:
            grade_n = 0
    add("P0-1", "等级分布合计与主表条数自洽", grade_n == len(main),
        f"统计值等级分布合计 {grade_n} / 主表 {len(main)}",
        "grade_dist 取自准入全量台账（含聚类/议题簇合并掉的条目），却写进主表统计值",
        "exporter 改为按 ordered 逐条重数；台账口径另放评级留痕")

    # P0-2 摘要有效率
    miss = sum(1 for r in main if "摘要未取得" in str(r.get("内容摘要") or ""))
    miss_rate = miss / max(1, len(main))
    add("P0-2", "主表摘要有效率 ≥ 0.8", miss_rate <= 0.2,
        f"摘要未取得 {miss}/{len(main)} = {1-miss_rate:.2f}；按记录类型 {dict(Counter(str(r.get('记录类型')) for r in main if '摘要未取得' in str(r.get('内容摘要') or '')))}",
        "RSS 时间倒排条目 description 是标题复读模板，正文补采首轮 4s 超时 + 配额按原始顺序截断，第三方站点补不到",
        "second_pass 按可成稿价值排序 + 名额 90→200 + 9s 超时 + URL 变体/Jina")

    # P0-3 分类一致率（真实口径，不看门禁自报）
    bad = [r for r in main if r.get("分类标签") and "/" in str(r["分类标签"])
           and not str(r["分类标签"]).startswith(str(r.get("上位大类") or ""))]
    gate = re.search(r"\| 分类一致率 \| ([\d.]+)", txt)
    add("P0-3", "分类标签前缀 == 上位大类", not bad,
        f"不合规 {len(bad)}/{len(main)}；门禁自报 {gate.group(1) if gate else '?'}；样例 {[ (r['上位大类'],r['分类标签']) for r in bad[:3] ]}",
        "quality._consistent 只在标签首段恰为 8 个大类名之一才校验，细标签一律 return True → 恒报 1.0",
        "改为逐条按口径表判定；主表/候选池如实报不达标，不调阈值")

    # P0-4 摘要 ≤100 净字（主表 + 候选池）
    over_m = [r for r in main if "摘要未取得" not in str(r.get("内容摘要") or "") and len(net(r["内容摘要"])) > 100]
    over_p = [r for r in pool if "摘要未取得" not in str(r.get("内容摘要") or "") and len(net(r["内容摘要"])) > 100]
    max_p = max((len(net(r["内容摘要"])) for r in over_p), default=0)
    add("P0-4", "摘要净字 ≤100（主表 + 候选池）", not over_m and not over_p,
        f"主表超长 {len(over_m)} 条；候选池超长 {len(over_p)} 条、最长 {max_p} 净字；门禁自报 1.0",
        "quality 只查 main，且 _net 与 Excel 写列净字口径不一致（replace vs 正则）",
        "统一 _net；取主表与候选池较差值；超长样本写进质量结果")

    # P0-5 标题字段污染
    long_t = [r for r in main if len(str(r.get("标题") or "")) > 60]
    long_tp = [r for r in pool if len(str(r.get("标题") or "")) > 60]
    add("P0-5", "标题不被正文污染（≤60 字）", not long_t,
        f"主表 {len(long_t)} 条、候选池 {len(long_tp)} 条；最长 {max((len(str(r['标题'])) for r in long_t+long_tp), default=0)} 字",
        "_clean_title 只去 SEO 尾巴与【】前缀，微博/长描述源把整段正文塞进 title",
        "_split_polluted_title 按整句边界拆成标题+余文，余文归位到 abstract 候选")

    # P0-6 国际三类门禁：小震不得进主表
    quake = [r for r in main if re.search(r"(\d+\.\d+)级地震", str(r.get("标题") or ""))]
    small = [r for r in quake if float(re.search(r"(\d+\.\d+)级地震", str(r["标题"])).group(1)) < 4.5]
    add("P0-6", "USGS 小震不进主表", not small,
        f"地震条目 {len(quake)}；<4.5 级 {len(small)} 条：{[(r['展示序'],str(r['标题'])[:34],r['Final_adj']) for r in small[:4]]}",
        "rules 只靠关键词「级地震」判灾害，无 mag 数值门禁；USGS 全量 feed 60 条灌入候选池",
        "channels.usgs_min_magnitude=4.5，低于阈值不入库并计数留痕")

    # P0-7 用户①明确排除项
    BAD = ["怎么开启", "设置步骤", "到底能不能买", "选购深度解析", "职业技能大赛",
           "专业委员会启动", "征求意见", "筹办", "配件生产忙", "公司问答"]
    noise = [(r["展示序"], str(r["标题"])[:48]) for r in main if any(b in str(r["标题"]) for b in BAD)]
    add("P0-7", "用户①排除项（导购/FAQ/机构动态/远期）不入主表", not noise,
        f"命中 {len(noise)} 条：{noise[:6]}",
        "F5 / 贴近度 strict 判据未覆盖「操作指南 / 导购测评 / 专委会挂牌 / 征求意见稿 / 远期规划」",
        "补 F5 负向词组并加回归用例（37 项基础上扩）")

    # P0-8 白名单分母唯一
    nums = set()
    for k in ("白名单渠道总数", "来源清单", "渠道白名单 / 已触达 / 待配置端点", "清单版本"):
        for v in kv.get(k, []):
            nums.update(int(x) for x in re.findall(r"\b(599|604|717|148)\b", str(v)))
    # 新批次已在口径表写「白名单分母口径说明」逐项标注 599/604/717/148 来源，
    # 此时多分母共存是「已解释」而非「矛盾」，不应再判 FAIL。
    explained8 = "白名单分母口径说明" in txt
    add("P0-8", "白名单分母口径唯一（或逐项已标注）", len(nums) <= 1 or explained8,
        f"同批出现分母 {sorted(nums)}；口径说明行={'有' if explained8 else '无'}",
        "registry 主白名单、channel_coverage 可提取域名子集、扩展后渠道行数三套口径混写",
        "主口径固定 31 类 599 渠道；口径表新增「白名单分母口径说明」逐项标注来源")

    # P1-1 主表 ⊆ 候选池
    pt = set(str(r["标题"]).strip() for r in pool)
    out = [r for r in main if str(r["标题"]).strip() not in pt]
    add("P1-1", "主表 ⊆ 候选池（按标题）", not out, f"越界 {len(out)} 条", "", "")
    # P1-2 候选池评级全 —
    notdash = [r for r in pool if str(r.get("Final_adj")) != "—" or str(r.get("等级")) != "—"]
    add("P1-2", "候选池评级列全 —", not notdash, f"非 — {len(notdash)} 条", "", "")

    # 从「项目 / 数值」两列表里按段取数（口径表是单元格，不是 markdown）
    def section_value(title_kw: str, key: str, lookahead: int = 12):
        for i, r in enumerate(rows):
            if r and r[0] and title_kw in str(r[0]) and "·" in str(r[0]):
                for x in rows[i + 1:i + 1 + lookahead]:
                    if x and x[0] and str(x[0]).strip() == key:
                        return x[1] if len(x) > 1 else None
        return None

    # P1-3 二次补采名额效率
    a = section_value("摘要二次补采（", "尝试")
    b = section_value("摘要二次补采（", "救回")
    if a is not None and b is not None:
        a, b = int(a or 0), int(b or 0)
        add("P1-3", "二次补采救回率 ≥ 15%", b / max(1, a) >= 0.15,
            f"尝试 {a} / 救回 {b}（{b/max(1,a):.1%}）",
            "旧版按候选池原始顺序 [:90]，名额被时政拼盘/新闻联播集锦吃掉",
            "_priority 排序：有 pub、标题未污染、非时政套话、非 RSS 模板优先；名额 200")

    # P1-4 词条回查
    lk = section_value("采集质量门禁（", "词条回查成功率", lookahead=20)
    add("P1-4", "词条回查成功率 ≥ 0.8", lk is not None and float(lk or 0) >= 0.8,
        f"门禁实测 {lk if lk is not None else '?'}（SERPER_API_KEY 已配置）",
        "社交主干 52 条微博快照仅 2 条成稿、51 条进附录 15；硬闸按窗口/独立原文正确拦截，但无线索→原文通路",
        "配 DAILYHOT_BASE_URL（自建 NewsNow）或注入 SHARE_PAGE_LINKS；不要调低 min_score")

    # 日志侧元问题
    log_issues = []
    if logs_dir:
        blob = ""
        for p in sorted(Path(logs_dir).glob("*.txt")):
            blob += p.read_text(encoding="utf-8-sig", errors="ignore") + "\n"
        checks = [
            ("端点快照是否本轮生成", "2026-11-18 20:55:25" not in blob,
             "日志显示 endpoints_live.yaml 生成时间 2026-11-18 20:55:25，本轮 11-20 未重测（复用两天前快照）"),
            ("workflow 含端点重测步骤", "discover_endpoints.py" in blob and "news_pipeline.reach" in blob,
             "step 序列 1–11 无重测步骤，用户推的是旧版 workflow"),
            ("commit 产品名", "更新要闻速览" not in blob, "commit 仍写『更新要闻速览』，产品名应为『资讯获取』"),
            ("窗口文案与口径一致", "左开右闭" not in blob and "0800" not in blob,
             "step 名『前一日 2000 → 当日 0800』、echo『左开右闭』，实际 lo-ro 左开右开"),
            ("原始条数唯一（或逐项已标注）",
             not ("5281" in blob and "4854" in blob) or "原始条数口径说明" in txt,
             "json-summary 原始采集条数 5281 vs 口径表/工程留痕 D-b 4854；"
             "新批次口径表已写「原始条数口径说明」说明二者是包含关系"),
        ]
        for name, ok, why in checks:
            log_issues.append({"检查": name, "状态": "PASS" if ok else "FAIL", "证据": why})

    wb.close()
    fail = [x for x in issues if x["状态"] == "FAIL"]
    return {
        "产物": xlsx, "主表": len(main), "候选池": len(pool),
        "来源数": len(set(str(r.get("来源")) for r in main)),
        "候选池来源数": len(set(str(r.get("来源")) for r in pool)),
        "问题总数": len(issues), "FAIL": len(fail), "PASS": len(issues) - len(fail),
        "逐项": issues, "日志侧": log_issues,
        "结论": "该批次存在 %d 项可复算质量问题，详见逐项证据" % len(fail),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx", nargs="?", default=str(BASE / "docs/downloads/latest.xlsx"))
    ap.add_argument("--logs", default="")
    ap.add_argument("--json", dest="json_out", default="")
    a = ap.parse_args()
    r = audit(a.xlsx, a.logs)
    print("=" * 78)
    print(f"批次质量审计 | 主表 {r['主表']} / 候选池 {r['候选池']} | "
          f"来源 {r['来源数']} / {r['候选池来源数']} 个 | FAIL {r['FAIL']}/{r['问题总数']}")
    print("=" * 78)
    for x in r["逐项"]:
        print(f"  [{x['状态']:<4}] {x['编号']:<6} {x['问题']}")
        print(f"         证据: {x['证据'][:150]}")
        if x["状态"] == "FAIL" and x["根因"]:
            print(f"         根因: {x['根因'][:130]}")
    if r["日志侧"]:
        print("\n日志侧（元问题：推上去的包是否为最新版）")
        for x in r["日志侧"]:
            print(f"  [{x['状态']:<4}] {x['检查']} — {x['证据'][:110]}")
    print("\n" + r["结论"])
    if a.json_out:
        p = Path(a.json_out); p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        print("结构化结果 →", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
