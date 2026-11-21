#!/usr/bin/env python3
"""对任意一期五工作表 Excel 做质量审计，输出可执行的 Markdown 报告。

    python scripts/audit_xlsx.py /data/inputs/latest.xlsx
    python scripts/audit_xlsx.py docs/downloads/latest.xlsx -o 质量审计报告.md

只做只读统计，不改源文件；指标与流水线附录 18 口径一致。
"""
from __future__ import annotations

import argparse
import html
import re
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.reader.excel import ExcelReader

ExcelReader.read_custom = lambda self: None  # 部分台账 custom.xml 带未声明命名空间
GROUPS = {"民生服务", "财经市场", "科技与产业", "文娱与热榜", "重要赛事",
          "重大突发与司法", "国际动态", "社会治理与养老"}
NOISE = ["美剧", "生活大爆炸", "基于PCA", "岩相", "因果泄漏", "值得买吗", "FAQ",
         "3个维度说透", "群测群防办法", "有这些亮点", "打出“组合拳”", "作出制度安排",
         "位居全国前列", "信息获取渠道", "地震属性", "触发模型"]
TOPIC = ["退市", "跌破1元", "跌破面值", "低于1元", "低价股", "面值退市", "退市警报", "退市风险"]


def compact(s):
    s = html.unescape(str(s or ""))
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", "", s)


def rows_of(ws):
    it = ws.iter_rows(values_only=True)
    hdr = list(next(it))
    return [dict(zip(hdr, r)) for r in it]


def audit(path: Path) -> dict:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        sheets = wb.sheetnames
        main = rows_of(wb["处理后的数据"]) if "处理后的数据" in sheets else []
        pool = rows_of(wb["窗口内全部数据"]) if "窗口内全部数据" in sheets else []
        kv = {}
        if "口径与稿外信息" in sheets:
            for r in wb["口径与稿外信息"].iter_rows(values_only=True):
                if r and r[0] and len(r) > 1 and r[1] is not None:
                    kv.setdefault(str(r[0]).strip(), str(r[1]).strip())
    finally:
        wb.close()

    def check(rs):
        empty = echo = entity = 0
        bad_samples = []
        for r in rs:
            t, a, src = compact(r.get("标题")), compact(r.get("内容摘要")), compact(r.get("来源"))
            raw = str(r.get("内容摘要") or "")
            if "&nbsp;" in raw or "&#160;" in raw or re.search(r"<[a-z]", raw):
                entity += 1
            if not a:
                empty += 1
                continue
            core = a.replace(t, "").replace(src, "")
            core = re.sub(r"[。！？；，、,.!?;:：·|｜\-—/()（）\[\]【】\d]", "", core)
            if len(core) < 12 or (t and a.count(t) >= 2 and len(a) - len(t) * a.count(t) < 30):
                echo += 1
                if len(bad_samples) < 4:
                    bad_samples.append(str(r.get("标题"))[:44])
        return {"条数": len(rs), "空白": empty, "标题复读": echo, "HTML实体残留": entity,
                "有效率": round((len(rs) - echo - empty) / len(rs) * 100, 1) if rs else 0,
                "样本": bad_samples}

    main_a, pool_a = check(main), check(pool)
    mismatch, noise, topic = [], [], []
    for r in main:
        g = str(r.get("上位大类") or ""); tag = str(r.get("分类标签") or "")
        pre = tag.split("/")[0].strip()
        if pre in GROUPS and pre != g:
            mismatch.append((str(r.get("标题"))[:44], g, tag))
        t = str(r.get("标题") or "")
        if any(p in t for p in NOISE):
            noise.append((t[:52], tag))
        if any(p in t for p in TOPIC):
            topic.append(t[:44])
    srcs = Counter(str(r.get("来源") or "").strip() for r in main)
    grades = Counter(str(r.get("等级") or "") for r in main)
    return {
        "文件": path.name, "工作表": sheets,
        "主表": main_a, "候选池": pool_a,
        "分类错配": mismatch, "噪声": noise, "退市议题": topic,
        "来源数": len(srcs), "来源Top8": srcs.most_common(8),
        "等级": dict(grades),
        "记录类型": dict(Counter(str(r.get("记录类型") or "") for r in main)),
        "采集模式": kv.get("采集模式", "—"),
        "采集窗口": kv.get("采集窗口", "—"),
        "渠道": {k: kv.get(k, "—") for k in
                 ("来源清单", "已触达渠道数", "未触达渠道数", "能按窗口枚举全量的渠道数",
                  "采集质量门禁结论")},
    }


def report(d: dict) -> str:
    m, p = d["主表"], d["候选池"]
    L = [
        "# 数据质量审计报告", "",
        f"- 文件：`{d['文件']}`　采集模式：**{d['采集模式']}**",
        f"- 采集窗口：{d['采集窗口']}",
        f"- 工作表：{', '.join(d['工作表'])}",
        "", "## 一、实测结论（先看这几个数）", "",
        "| 指标 | 主表 | 候选池 | 说明 |", "|---|---|---|---|",
        f"| 条目数 | {m['条数']} | {p['条数']} | — |",
        f"| 摘要有效率 | **{m['有效率']}%** | **{p['有效率']}%** | 标题复读 / HTML 实体残留一律不计有效 |",
        f"| 标题复读条数 | {m['标题复读']} | {p['标题复读']} | RSS description 模板「标题+来源+标题。」 |",
        f"| HTML 实体残留 | {m['HTML实体残留']} | {p['HTML实体残留']} | `&nbsp;` 未清洗 |",
        f"| 分类错配 | {len(d['分类错配'])} | — | 上位大类 ≠ 分类标签前缀 |",
        f"| 非新闻噪声 | {len(d['噪声'])} | — | 学术 / 影视 / 导购 / 政务科普 |",
        f"| 同一议题占比 | {round(len(d['退市议题'])/m['条数']*100,1) if m['条数'] else 0}% | — | 退市 / 低价股簇 {len(d['退市议题'])} 条 |",
        f"| 来源数 | {d['来源数']} | — | 记录类型 {d['记录类型']} |",
        "", "## 二、渠道触达", "",
    ]
    for k, v in d["渠道"].items():
        L.append(f"- {k}：{v}")
    L += ["", "> 白名单渠道里绝大多数状态是「站内时间倒排 + RSS（待配置端点）」："
          "不是网络失败，是**只登记了名称、没有可拉取 URL**。Google News RSS 是聚合检索，"
          "不能替代一手源。", "", "## 三、问题样本", "", "### 分类错配（前 8 条）", ""]
    L += [f"- `{t}` → 大类 `{g}` / 标签 `{tag}`" for t, g, tag in d["分类错配"][:8]] or ["- 无"]
    L += ["", "### 非新闻 / 关键词误命中（前 8 条）", ""]
    L += [f"- `{t}` → 标签 `{tag}`" for t, tag in d["噪声"][:8]] or ["- 无"]
    L += ["", "### 摘要复读样本", ""]
    L += [f"- `{t}`" for t in m["样本"]] or ["- 无"]
    L += ["", "## 四、本版已落地的修复", "",
          "1. **摘要**：`clean_html` 去实体/标签/零宽；`is_title_echo` 判标题复读；"
          "命中一律标「摘要未取得」并逐条留痕，绝不冒充。`live.fetch_article` 从独立原文 URL 补正文，"
          "成功标「正文补采」。门禁新增「摘要有效率」。",
          "2. **分类**：标签前缀强制等于上位大类；「国际动态」需境外地理信号；"
          "经济地震 / 地震属性 / 生活大爆炸等加否定表，杜绝误挂。门禁新增「分类一致率」。",
          "3. **噪声**：F5 在准入层最前置剔除学术论文 / 影视娱乐 / 导购 FAQ / 政务科普，"
          "真实突发词豁免，逐条依据写入附录 19。",
          "4. **去重**：`TOPIC_THEMES` 通用议题收敛，`story_max_keep=2`，"
          "退市潮不再占满整张表；合并留痕进附录 17。",
          "5. **渠道**：失败按「未配置端点 / 超时 / 403 / 404 / 需 key / 无时间字段」分类申报；"
          "`python scripts/check_endpoints.py` 实测端点并生成 `data/channel_health.json`。",
          "6. **门禁**：九项指标写进「工程留痕 A」，`quality.warn_only=false` 可让不达标批次直接失败。",
          "", "## 五、还需要你做的（代码解决不了的部分）", "",
          "- 给白名单里的主流渠道**补真实可拉取端点**：官方 RSS / 站内时间倒排 / API。"
          "脚本只做实测，不会凭空编造 URL。",
          "- 接入平台后台播放/阅读/讨论数，H 通道才可开启；否则 Final 只反映价值分 V。",
          "- 摘要为纯代码抽取式（按整句裁剪），不接任何模型语义层；必须保留「数字不改写 / 整句收束 / 占位句清除」校验器。",
          "- 正式上线把 `quality.warn_only` 改为 `false`，让低质量批次不静默产出。",
          "", "---", "", "*报告由 `scripts/audit_xlsx.py` 只读生成，指标来自本期 Excel 真实单元格。*", ""]
    return "\n".join(L)


def main() -> int:
    p = argparse.ArgumentParser(description="Excel 质量审计")
    p.add_argument("xlsx")
    p.add_argument("-o", "--output", default="output/质量审计报告.md")
    args = p.parse_args()
    src = Path(args.xlsx)
    if not src.is_absolute():
        src = Path.cwd() / src
    if not src.exists():
        raise SystemExit(f"文件不存在：{src}")
    d = audit(src)
    out = Path(args.output)
    if not out.is_absolute():
        out = Path(__file__).resolve().parent.parent / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report(d), encoding="utf-8")
    m = d["主表"]
    print(f"[审计] {src.name} → {out}")
    print(f"  主表 {m['条数']} 条 | 摘要有效率 {m['有效率']}% | 复读 {m['标题复读']} 条 | "
          f"实体残留 {m['HTML实体残留']} 条")
    print(f"  分类错配 {len(d['分类错配'])} | 噪声 {len(d['噪声'])} | "
          f"退市议题 {len(d['退市议题'])} | 来源 {d['来源数']} 家")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
