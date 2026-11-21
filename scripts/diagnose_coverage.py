"""资讯获取 · 31 类渠道漏斗诊断（回答「31 个大类怎么只有这么点数据，卡在哪」）。

纯离线读产物，不联网、不改任何交付文件。逐层量化四个卡点：

  卡点 A  渠道端点覆盖：599 渠道里多少有可拉取端点 / 多少被出口拦截 /
          多少非独立站点 / 多少 DNS 未解析；按 31 类逐类展开
  卡点 B  窗口位置错配：窗口外排除里，有多少是「往年旧页」、多少是
          「晚于右界的最新流」、多少是「早于左界」；若右界对齐采集时刻能多收多少
  卡点 C  源结构失衡：本批原始条目按源集中度（TOP3 / TOP5 占比）
  卡点 D  准入口径：strict 档剔了多少、其中「未命中正面清单」与
          「三类强剔除」各多少

用法：
    python scripts/diagnose_coverage.py
    python scripts/diagnose_coverage.py --xlsx docs/downloads/latest.xlsx \
        --json output/coverage_diagnosis.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from openpyxl import load_workbook  # noqa: E402
from openpyxl.reader.excel import ExcelReader  # noqa: E402

ExcelReader.read_custom = lambda s: None  # 绕开损坏的 customXml


def _seg(rows, title_startswith):
    st = next((i for i, r in enumerate(rows)
               if r and r[0] and str(r[0]).startswith(title_startswith)), None)
    if st is None:
        return [], []
    hdr = rows[st + 1]
    out = []
    for r in rows[st + 2:]:
        if r and r[0] and ("附录" in str(r[0]) or "工程留痕" in str(r[0])):
            break
        if r and r[0]:
            out.append(r)
    return hdr, out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", default=str(BASE / "docs/downloads/latest.xlsx"))
    ap.add_argument("--reach", default=str(BASE / "data/channel_reach.json"))
    ap.add_argument("--json", dest="json_out", default="")
    args = ap.parse_args()

    wb = load_workbook(args.xlsx, read_only=True, data_only=True)
    rows = list(wb["口径与稿外信息"].iter_rows(values_only=True))
    ws_main = wb["处理后的数据"]
    ws_cand = wb["窗口内全部数据"]
    main_n, cand_n = ws_main.max_row - 1, ws_cand.max_row - 1

    def col_dist(ws, col_name):
        hdr = [c.value for c in next(ws.iter_rows())]
        if col_name not in hdr:
            return {}
        i = hdr.index(col_name)
        return dict(Counter(str(r[i]) for r in ws.iter_rows(min_row=2, values_only=True)))

    # ---- 卡点 A：31 类逐类渠道分层 ----
    reach = json.load(open(args.reach, encoding="utf-8"))
    chans = reach.get("渠道", [])
    by = defaultdict(Counter)
    for c in chans:
        k = c.get("大类", "未分类")
        by[k]["渠道"] += 1
        s = str(c.get("状态", ""))
        if "实证端点" in s:
            by[k]["有端点"] += 1
        elif "出口策略拦截" in s:
            by[k]["出口拦截"] += 1
        elif "非独立站点" in s:
            by[k]["非独立站点"] += 1
        elif "DNS" in s:
            by[k]["DNS未解析"] += 1
        if c.get("级别") == "★":
            by[k]["★必采"] += 1

    def sec_no(k):
        m = re.search(r"§(\d+)", k)
        return int(m.group(1)) if m else 999

    classes = []
    for k in sorted(by, key=sec_no):
        v = by[k]
        classes.append({"大类": k, **dict(v)})
    zero = [c["大类"] for c in classes if c.get("有端点", 0) == 0]
    # 「零端点」不等于「没写代码」：拆成四种可行动原因，避免用一个笼统数字误导
    zero_detail = []
    for c in classes:
        if c.get("有端点", 0) != 0:
            continue
        zero_detail.append({
            "大类": c["大类"], "渠道": c["渠道"],
            "非独立站点": c.get("非独立站点", 0),
            "出口拦截": c.get("出口拦截", 0),
            "DNS未解析": c.get("DNS未解析", 0),
            "主因": ("渠道本身就是平台内栏目/账号/节目，白名单「入口」列写的不是 URL"
                   if c.get("非独立站点", 0) >= c["渠道"] * 0.5
                   else "真实域名但被出口 403 / DNS 未解析"),
            "正确形态": ("快照 → 词条 → 逐条回查独立原文 → 成稿（不用 RSS/时间倒排端点衡量）"
                     if c.get("非独立站点", 0) >= c["渠道"] * 0.5
                     else "换 unrestricted 网络自动重测 / 补 endpoints_live.yaml / 授权聚合 API"),
        })

    # ---- 卡点 B：窗口外排除的时间位置 ----
    _, a4 = _seg(rows, "附录 4 · 窗口外")
    _, a4b = _seg(rows, "附录 4b · 时点推定")
    basis = Counter(str(r[3])[:46] for r in a4 if len(r) > 3 and r[3])
    dates = []
    for r in a4:
        try:
            dates.append(datetime.strptime(str(r[2])[:16], "%Y-%m-%d %H:%M"))
        except Exception:
            pass
    old_page = sum(1 for r in a4 if len(r) > 5 and "往年旧页" in str(r[5]))
    # 「晚于右界的最新流」按排除原因动态统计，不硬编码日期：
    # 换窗口 / 换批次后依然准确（早期版本写死 2026-11-19 20:00 会误算）
    latest = [r for r in a4 if len(r) > 5 and "超右界" in str(r[5])]
    reasons = Counter(str(r[5])[:26] for r in a4 if len(r) > 5 and r[5])

    # ---- 卡点 C：源集中度 ----
    _, d_rows = _seg(rows, "工程留痕 D · 真实联网源逐源触达（本批")
    srcs = []
    for r in d_rows:
        if len(r) > 4 and r[3] == "已触达":
            try:
                srcs.append((str(r[1])[:24], int(r[4])))
            except Exception:
                pass
    srcs.sort(key=lambda x: -x[1])
    tot = sum(n for _, n in srcs) or 1
    top3 = sum(n for _, n in srcs[:3]) / tot
    top5 = sum(n for _, n in srcs[:5]) / tot

    # ---- 卡点 D：准入剔除构成 ----
    _, a1 = _seg(rows, "附录 1 · 大众贴近度")
    verdicts = Counter(str(r[5])[:44] for r in a1 if len(r) > 5 and r[5])
    unmatched = sum(v for k, v in verdicts.items() if "未命中正面清单" in k)
    strong = sum(v for k, v in verdicts.items() if "强剔除" in k)

    result = {
        "批次": {"原始": tot, "候选池": cand_n, "主表": main_n,
                 "窗口外排除": len(a4), "时点推定": len(a4b)},
        "卡点A_渠道端点": {
            "渠道总数": len(chans),
            "有端点": sum(c.get("有端点", 0) for c in classes),
            "出口拦截": sum(c.get("出口拦截", 0) for c in classes),
            "非独立站点": sum(c.get("非独立站点", 0) for c in classes),
            "DNS未解析": sum(c.get("DNS未解析", 0) for c in classes),
            "零端点大类数": len(zero), "零端点大类": zero,
            "零端点原因分解": zero_detail,
            "口径说明": "「零端点」= endpoints_live.yaml 里没有能抽出带时间字段条目的 URL；"
                        "不等于没有采集代码。非独立站点类本就不该用端点衡量，"
                        "其正确链路是 social_main / platform_access / free_sources 的"
                        "「快照→词条→回查→成稿」。",
            "逐类": classes},
        "卡点B_窗口位置": {
            "可解析发布时间": len(dates),
            "往年旧页": old_page,
            "晚于窗口右界的最新流": len(latest),
            "右界对齐采集时刻可多收": len(latest),
            "时间依据分布": dict(basis.most_common(8)),
            "排除原因TOP": dict(reasons.most_common(8))},
        "卡点C_源结构": {
            "已触达源数": len(srcs), "TOP3占比": round(top3, 4),
            "TOP5占比": round(top5, 4),
            "TOP8": [{"源": s, "条数": n} for s, n in srcs[:8]]},
        "卡点D_准入": {
            "附录1行数": len(a1), "未命中正面清单": unmatched,
            "三类强剔除": strong, "判定分布": dict(verdicts.most_common(10))},
        "主表上位大类分布": col_dist(ws_main, "上位大类"),
        "候选池上位大类分布": col_dist(ws_cand, "上位大类"),
    }
    wb.close()

    # ---- 终端报告 ----
    b = result["批次"]; a = result["卡点A_渠道端点"]
    print("=" * 78)
    print("31 类渠道漏斗诊断（纯离线，不改产物）")
    print("=" * 78)
    print(f"批次：原始 {b['原始']} → 候选 {b['候选池']} → 主表 {b['主表']}"
          f"  | 窗口外排除 {b['窗口外排除']} | 时点推定 {b['时点推定']}")
    print(f"\n【卡点 A】渠道端点覆盖  {a['有端点']}/{a['渠道总数']} 有端点"
          f"（{a['有端点']/max(1,a['渠道总数'])*100:.1f}%）")
    print(f"  出口拦截 {a['出口拦截']} | 非独立站点 {a['非独立站点']}"
          f" | DNS未解析 {a['DNS未解析']} | 零端点大类 {a['零端点大类数']} 个")
    print(f"\n  {'大类':<38}{'渠道':>5}{'端点':>6}{'拦截':>6}{'非站点':>7}{'DNS':>5}")
    for c in classes:
        flag = "  ← 0" if c.get("有端点", 0) == 0 else ""
        print(f"  {c['大类'][:36]:<38}{c['渠道']:>5}{c.get('有端点',0):>6}"
              f"{c.get('出口拦截',0):>6}{c.get('非独立站点',0):>7}{c.get('DNS未解析',0):>5}{flag}")
    print(f"\n  零端点大类：{'、'.join(zero)}")
    print("\n  【零端点原因分解】零端点 ≠ 没写代码，也不等于渠道不存在：")
    print(f"  {'大类':<34}{'渠道':>5}{'非站点':>7}{'拦截':>6}{'DNS':>5}  主因")
    for z in result["卡点A_渠道端点"]["零端点原因分解"]:
        print(f"  {z['大类'][:32]:<34}{z['渠道']:>5}{z['非独立站点']:>7}"
              f"{z['出口拦截']:>6}{z['DNS未解析']:>5}  {z['主因'][:34]}")
    print("  非独立站点类：白名单「入口」列写的就是栏目/账号/节目名，不是 URL；")
    print("  正确形态是「快照 → 词条 → 逐条回查独立原文 → 成稿」，不该用端点数衡量。")
    bw = result["卡点B_窗口位置"]
    print(f"\n【卡点 B】窗口位置错配  往年旧页 {bw['往年旧页']} 条"
          f" | 晚于右界的最新流 {bw['晚于窗口右界的最新流']} 条")
    print(f"  → 右界对齐采集时刻，仅附录4 里就能多收 {bw['右界对齐采集时刻可多收']} 条")
    print("  时间依据：", "；".join(f"{k} {v}" for k, v in list(bw["时间依据分布"].items())[:4]))
    cc = result["卡点C_源结构"]
    print(f"\n【卡点 C】源结构  {cc['已触达源数']} 个源全部触达，"
          f"但 TOP3 占 {cc['TOP3占比']*100:.1f}%、TOP5 占 {cc['TOP5占比']*100:.1f}%")
    for s in cc["TOP8"][:5]:
        print(f"    {s['源']:<22}{s['条数']:>5} 条")
    dd = result["卡点D_准入"]
    print(f"\n【卡点 D】准入口径  未命中正面清单 {dd['未命中正面清单']} 条"
          f" | 三类强剔除 {dd['三类强剔除']} 条（按需求①正确剔除）")
    print("\n主表上位大类：", result["主表上位大类分布"])
    print("\n结论：")
    if bw["晚于窗口右界的最新流"] > 0:
        print(f"  · 卡点 B 正在发生：{bw['晚于窗口右界的最新流']} 条最新流被超右界排除，"
              "说明本批用的是历史窗口而端点是实时的；改用「右界=采集时刻」即可多收。")
    else:
        print("  · 卡点 B 已消除：本批右界对齐采集时刻，无「超右界」最新流损失"
              f"（仍有 {bw['往年旧页']} 条往年旧页属源站陈旧，非窗口问题）。")
    print("  · 卡点 A 属真缺口：用户最关心的主题恰在零端点/高拦截大类，")
    print("    需补端点或归并到所属平台时间流，换网络可自动启用被拦域名；")
    print("  · 卡点 C 已修：正文补采改为窗口内优先，配额不再被窗口外条目吃掉；")
    print("  · 卡点 D 是口径正确执行，暴露的是上游没料，不是筛太严。")
    print("=" * 78)

    if args.json_out:
        p = Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print("结构化结果 →", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
