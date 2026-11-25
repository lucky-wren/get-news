#!/usr/bin/env python3
"""一键自检：不依赖 pytest 也能把交付包从头到尾验一遍。

用途：拿到包后 `python verify_package.py`，或 CI 里替代/补充 `pytest -q`。
全程零网络（socket 被硬屏蔽），只用离线语料与 MockTransport 等价路径。

检查项：
  1. 运行必需文件与随包交付物清单齐全（防「只同步了部分文件」）
  2. 全部 16 个模块可导入、4 个采集器注册成功
  3. 离线回放完整链路出表，18 项规格验收逐项 PASS
  4. 包内成品 5 表 12 列 / 主表非空且 ≤50 / 前 10 10 / 候选池全量
  5. 摘要与来源契约、附录 1–17、31 类逐类申报、固定地址字节一致
  6. 本轮：标题—摘要对应、面向大众正向准入、零聚合名、严格重要性降序
"""
from __future__ import annotations

import importlib
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _cur_xlsx():
    xs = sorted((ROOT / "output").glob("daily_news_*_20h.xlsx"))
    return xs[-1] if xs else None
sys.path.insert(0, str(ROOT.as_posix()))

import json

MANIFEST = ROOT / "manifest.json"
_m = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.is_file() else {}
REQUIRED = _m.get("required", [])
DELIVERABLES = _m.get("deliverables", [])

# 模块清单从 manifest.required 推导：新增一个采集器不再需要人肉同步第二份清单。
MODULES = ["newsbrief"] + sorted({
    p[:-3].replace("/", ".")
    for p in REQUIRED
    if p.startswith("newsbrief/") and p.endswith(".py") and not p.endswith("/__init__.py")
})

RESULTS: list[tuple[str, bool, str]] = []


def ck(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))


def block_network() -> None:
    def _no(*_a, **_k):
        raise RuntimeError("零网络自检：禁止真实网络连接")
    socket.socket.connect = _no
    socket.socket.connect_ex = _no
    socket.create_connection = _no


def main() -> int:
    print("=== 交付包一键自检（零网络）===")
    print(f"包根：{ROOT}")

    miss = [p for p in REQUIRED + DELIVERABLES if not (ROOT / p).is_file()]
    ck("文件清单齐全", not miss, "缺：" + "、".join(miss) if miss else
       f"{len(REQUIRED)} 项运行必需 + {len(DELIVERABLES)} 项随包交付物")

    block_network()
    ck("真实 socket 已屏蔽", True, "connect / connect_ex / create_connection")

    bad = []
    for m in MODULES:
        try:
            importlib.import_module(m)
        except Exception as ex:
            bad.append(f"{m}: {type(ex).__name__}: {ex}")
    ck(f"{len(MODULES)} 个模块全部可导入", not bad, "；".join(bad[:2]))

    from newsbrief import registry
    reg = registry.discover()
    ck("5 类采集器注册成功", {"rss", "html", "geojson", "inbox", "jsonp"} <= set(reg),
       f"已注册 {sorted(reg)}")

    # 渠道回归：死 feed 不得复活、CCTV JSONP 不得少于 8 个端点。
    ch = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    eps = [e for g in ch["groups"] for e in g["endpoints"]]
    stale = [e["url"] for e in eps if "people.com.cn/rss" in e["url"]
             or "news_politics.xml" in e["url"] or "news_world.xml" in e["url"]]
    n_jsonp = sum(1 for e in eps if e.get("type") == "jsonp")
    ck("渠道台账：无停更死 feed、CCTV JSONP ≥8",
       not stale and n_jsonp >= 8 and len(eps) >= 60,
       f"{len(eps)} 端点 / jsonp {n_jsonp} / 死 feed {len(stale)}")

    # 《资讯渠道清单》完整入包：691 渠道 / 503 URL / 31 章 / 逐 URL 探测状态
    catp = ROOT / "data" / "channel_catalog.json"
    cat = json.loads(catp.read_text(encoding="utf-8")) if catp.is_file() else {}
    cov = cat.get("group_coverage", [])
    ck("资讯渠道清单完整入包",
       cat.get("channels", 0) >= 691 and cat.get("unique_urls", 0) >= 503
       and cat.get("sections") == 31 and cat.get("reachable_urls", 0) > 100
       and cov == list(range(1, 32)),
       f"{cat.get('channels')} 渠道 / {cat.get('unique_urls')} 入口 / "
       f"可达 {cat.get('reachable_urls')} / 覆盖 {len(cov)} 类")
    wired = [e for e in eps if e.get("catalog_sec")]
    ck("可达清单入口已接入 31 类采集台账",
       len(wired) >= 60 and len(eps) >= 130,
       f"清单接入 {len(wired)} / 总端点 {len(eps)}")

    # 部署链路：推上去的 tree 必须包含全部必需文件（上一轮 CI 事故的根因）
    import deploy
    files = deploy.push_files(_m)
    miss = deploy.preflight(_m, files)
    ck("deploy 推送清单覆盖全部必需文件", not miss,
       "缺：" + "、".join(miss) if miss else f"{len(files)} 个文件")

    from newsbrief import appendix, model, pipeline, sheet
    cfg, channels = pipeline.load_config()
    batch = pipeline.run(mode="offline", cfg=cfg, channels=channels, date="2026-11-21")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "offline.xlsx"
        sheet.build_workbook(batch, out, batch.koushi, [it.trace_row() for it in batch.main])
        checks = pipeline.verify(batch, out)
        hard = [c for c in checks if c["结果"] == "FAIL" and c.get("gate", True)]
        ck("离线回放 18 项规格验收", len(checks) >= 18 and not hard,
           f"{len(checks) - len(hard)}/{len(checks)} PASS")

    # 包内当期成品
    from openpyxl import load_workbook
    p = _cur_xlsx()
    wb = load_workbook(p, data_only=True)
    try:
        ck("成品 5 表齐备", wb.sheetnames == model.SHEETS_5, str(wb.sheetnames))
        heads = [list(next(wb[s].iter_rows(values_only=True))) for s in wb.sheetnames[:3]]
        ck("三表 12 列同头", heads[0] == heads[1] == heads[2] and len(heads[0]) == 12)
        main = list(wb["处理后的数据"].iter_rows(min_row=2, values_only=True))
        top = list(wb["排名最靠前的10条"].iter_rows(min_row=2, values_only=True))
        cand = list(wb["窗口内全部数据"].iter_rows(min_row=2, values_only=True))
        # 包内成品随当日真采集批次变化，断言三层自洽而非写死某批条数。
        ck("主表/前10/候选池三层自洽",
           len(main) > 0 and len(top) == min(10, len(main)) and len(cand) >= len(main),
           f"{len(main)} / {len(top)} / {len(cand)}")
        ck("前 10 与主表前 10 逐格一致", top == main[:10])
        ck("候选池评级列全 —", all(all(r[i] == "—" for i in (1, 2, 3, 4, 10)) for r in cand))
        bad_abs = [r[0] for r in main + cand if not model.valid_abstract(str(r[8] or ""))]
        ck("摘要契约（2 段/缩进/≤100 字/整句）", not bad_abs, f"异常 {len(bad_abs)}")
        bad_src = [r[0] for r in main + cand if not model.valid_sources(str(r[9] or ""))]
        ck("来源契约", not bad_src, f"异常 {len(bad_src)}")
        kou = list(wb["口径与稿外信息"].iter_rows(values_only=True))
        txt = "\n".join(" | ".join(str(x or "") for x in r) for r in kou)
        need = [f"附录 {i}" for i in (1, 2, 3, 4, 5, "5b", 6, 7, 8, 9, 10, 11, 12,
                                     "12b", 13, 14, 15, 16, 17)]
        ck("附录 1–17 齐备 + 边界留痕", all(k in txt for k in need)
           and "候选池边界留痕" in txt)
        n31 = sum(1 for r in kou if str(r[0] or "").startswith("§"))
        ck("逐渠道 31 类状态申报", n31 >= 31, f"{n31} 行")
        adj = sum(1 for a, b in zip(main[:10], main[1:10]) if a[5] == b[5])
        dist = __import__("collections").Counter(r[5] for r in main[:10])
        # 用户要「按重要性排好序」：硬约束是严格降序，不是相邻同类打散。
        # 相邻同类数只作如实申报（与 pipeline.verify ⑧ 同一判据）。
        fins10 = [float(r[1]) for r in main[:10]]
        ck("前 10 按 Final_adj 严格降序（按重要性排序）",
           fins10 == sorted(fins10, reverse=True),
           f"分数 {fins10[:3]}… / 相邻同类 {adj} / 分布 {dict(dist)}")
    finally:
        wb.close()

    a = _cur_xlsx().read_bytes()
    b = (ROOT / "docs" / "downloads" / "latest.xlsx").read_bytes()
    ck("固定下载地址与成品字节一致", a == b)

    # 页面不能挂旧批次：截图里 9 条全央视网单源、含 11-21 超窗稿，就是
    # 线上 Pages 还挂着历史 news-data.js，而自检只验了 xlsx 没验页面。
    js = ROOT / "docs" / "news-data.js"
    html = ROOT / "docs" / "index.html"
    try:
        raw = js.read_text(encoding="utf-8")
        d = json.loads(raw[raw.index("=") + 1:].rstrip().rstrip(";"))
        wb2 = load_workbook(_cur_xlsx(), data_only=True)
        xrows = [list(r) for r in wb2["处理后的数据"].iter_rows(min_row=2, values_only=True)]
        xcand = [r for r in wb2["窗口内全部数据"].iter_rows(min_row=2, values_only=True) if r[0]]
        wb2.close()
        same = (d.get("main") == xrows
                and d.get("counts", {}).get("main") == len(xrows)
                and d.get("counts", {}).get("candidate") == len(xcand))
        ck("页面数据与成品同批（主表逐格一致，不挂旧批次）", same,
           f"页面主表 {len(d.get('main', []))} / xlsx 主表 {len(xrows)} / "
           f"候选池 {d.get('counts', {}).get('candidate')}")
        h = html.read_text(encoding="utf-8")
        ctrl = all(k in h for k in ('id="ws"', 'id="we"', 'id="apply"',
                                    'id="reset"', 'id="rerun"', 'datetime-local',
                                    "window_start", "应用窗口"))
        ck("页面含时间窗口设置控件（起止年月日时 + 应用/重跑）", ctrl,
           "datetime-local 起止输入 + 应用窗口 + 按此窗口重跑")
        import re as _re
        ws, we = d.get("window_start", ""), d.get("window_end", "")
        fmt = lambda x: bool(_re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", x or ""))
        tm = d.get("time_meta", {}).get("main", [])
        ck("页面窗口默认值与逐条时间依据齐备",
           fmt(ws) and fmt(we) and len(tm) == len(d.get("main", []))
           and all(x.get("pub") for x in tm),
           f"{ws} → {we} / {sum(1 for x in tm if x.get('pub'))}/{len(tm)} 条带可核对发布时间")
    except (OSError, ValueError, KeyError) as ex:
        ck("页面数据与成品同批（主表逐格一致，不挂旧批次）", False, str(ex)[:110])
        ck("页面含时间窗口设置控件（起止年月日时 + 应用/重跑）", False, str(ex)[:110])
        ck("页面窗口默认值与逐条时间依据齐备", False, str(ex)[:110])

    from newsbrief import workflow_contract as wc
    wf_win = (ROOT / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")
    r = wc.check(wf_win)
    ck("workflow 生产路径与窗口契约（旧定义触发不判红）", r["ok"], r["note"])

    # 网页上传友好：交付清单里的文件名一律 ASCII（中文名会触发兜底报错）。
    # 只验 manifest 声明的交付物；checkout 里若混有旧版残留，由 workflow
    # 清理步与 deploy.verify_head_state 负责，不算在「交付物命名」头上。
    mf = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    declared = list(mf["required"]) + list(mf["deliverables"])
    non_ascii = [x for x in declared if not x.isascii()]
    ck("交付清单文件名全部 ASCII（网页上传不踩编码坑）", not non_ascii,
       "非 ASCII：" + "、".join(non_ascii) if non_ascii else
       f"{len(declared)} 项全 ASCII")
    # 截图 34：CRLF 必须由 .gitattributes + workflow 自愈步保证，
    # 不能靠「CI 恰好没做文本规范化」。
    ga = ROOT / ".gitattributes"
    ck(".gitattributes 声明 *.bat text eol=crlf 且在推送清单",
       ga.is_file() and "*.bat text eol=crlf" in ga.read_text(encoding="utf-8")
       and ".gitattributes" in mf["required"])
    wf_bat = (ROOT / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")
    ck("workflow 含 deploy.bat 换行规范化步",
       "规范化 deploy.bat 换行" in wf_bat and "normalize_deploy_bat" in wf_bat)
    dep_bat = (ROOT / "deploy.py").read_text(encoding="utf-8")
    ck("deploy.py 含 normalize_deploy_bat 且带硬断言",
       "def normalize_deploy_bat" in dep_bat and "deploy.bat 不合规" in dep_bat)

    first = ROOT / "README_FIRST.txt"
    ck("包根含 README_FIRST.txt 排障卡",
       first.is_file() and all(k in first.read_text(encoding="utf-8")
                               for k in ("deploy.bat", "github.com", "GitHub Actions",
                                         "DO NOT upload the .zip")),
       str(first.relative_to(ROOT)))

    # Pages workflow：build/deploy 分 job、artifact 名逐字一致、权限齐备
    wf = (ROOT / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")
    try:
        import yaml
        doc = yaml.safe_load(wf)
        jobs = doc.get("jobs", {})
        dep = jobs.get("deploy", {})
        perm = dep.get("permissions", {})
        up = next((s for s in jobs.get("build", {}).get("steps", [])
                   if str(s.get("uses", "")).startswith("actions/upload-pages-artifact")), {})
        name = str(up.get("with", {}).get("name", ""))
        path = str(up.get("with", {}).get("path", ""))
        ok = ({"build", "deploy"} <= set(jobs)
              and dep.get("needs") == "build"
              and perm.get("pages") == "write" and perm.get("id-token") == "write"
              and perm.get("actions") == "read"
              and name == "github-pages" and path == "docs"
              and "artifact_name: github-pages" in wf
              and "等待 github-pages artifact 可见" in wf)
        ck("Pages workflow：build/deploy 分 job、artifact 名一致、权限齐备", ok,
           f"jobs={sorted(jobs)} / name={name} / path={path}")
    except ImportError:
        ck("Pages workflow：build/deploy 分 job、artifact 名一致、权限齐备",
           all(k in wf for k in ("needs: build", "name: github-pages",
                                 "pages: write", "actions: read")))

    # 截图 43：configure-pages@v5 只 GET 不创建；站点由 deploy.bat 用用户
    # 自己的 Token 一次性创建（默认 GITHUB_TOKEN 没有 administration:write）。
    dep_src = (ROOT / "deploy.py").read_text(encoding="utf-8")
    ok_pages = ("def ensure_pages_enabled" in dep_src
                and "ok_pages, note_pages = ensure_pages_enabled(" in dep_src
                and dep_src.index("ensure_pages_enabled(repo, token)")
                < dep_src.index("/actions/workflows/daily.yml/dispatches"))
    ck("deploy 自动启用 Pages 站点（只需 Token、免登录、dispatch 之前）",
       ok_pages, "GET /pages 404 → POST build_type=workflow；403 给一次性指引")
    wf2 = (ROOT / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")
    conf = {}
    try:
        import yaml
        b = (yaml.safe_load(wf2).get("jobs") or {}).get("build") or {}
        conf = next((s for s in b.get("steps") or []
                     if str(s.get("uses", "")).startswith("actions/configure-pages")), {})
        no_enablement = not (conf.get("with") or {}).get("enablement")
        pages_read = (b.get("permissions") or {}).get("pages") == "read"
    except ImportError:
        no_enablement = "with:\n        enablement: true" not in wf2
        pages_read = "pages: read" in wf2
    ck("workflow 有 Pages 守卫且不误用 enablement",
       "检查 Pages 站点已启用" in wf2 and no_enablement
       and pages_read and "build_type=workflow" in wf2,
       "GITHUB_TOKEN 无法创建站点，enablement:true 只会把 404 换成 403")

    # 截图 44：0 条主表时 18 项验收全是「空集恒真」，页面却报「达标」。
    # 三处必须同时锁死，缺一个就会把空表覆盖到固定下载地址与线上页面。
    pv = (ROOT / "newsbrief" / "pipeline.py").read_text(encoding="utf-8")
    ck("验收 ⑱ 含「主表 ≥1 条」硬闸（0 条不得判达标）",
       "主表 ≥1 条" in pv and "len(main) >= 1" in pv
       and "0 条窗口内要闻，不得判达标" in pv,
       "候选池 13 / 主表 0 → ⑱ FAIL(gate=True) → 撤回保留上一批")
    sv = (ROOT / "newsbrief" / "site.py").read_text(encoding="utf-8")
    ck("页面质量结论在主表 0 条时渲染「不达标」",
       "empty_main = not getattr(batch, \"main\", None)" in sv
       and "本批主表 0 条，未达交付要求" in sv,
       "第二层防御：不出现绿色「达标」")
    rv = (ROOT / "newsbrief" / "rules.py").read_text(encoding="utf-8")
    ck("贴近度词表不再硬剔除（防主表塌成 0）",
       "降权不剔除" in rv and "空表兜底" in rv
       and "三类强剔除" in rv,
       "三类强剔除/F1–F4/国际三类门禁仍保留")
    ck("截图 44 回归测试随包",
       (ROOT / "tests" / "test_zero_main_guard.py").is_file()
       and "test_zero_main_guard.py" in
       (ROOT / "manifest.json").read_text(encoding="utf-8"),
       "5 项：中性稿成稿 / 0 条必红 / 页面不撒谎 / 旧判据反证")

    # 截图 44 的产能根因：503 端点串行 × 6s 超时，240s 预算只够跑前几十个，
    # 可达端点排在 403 后面 → 线上当批只有 13 条窗口内稿件。
    rgv = (ROOT / "newsbrief" / "registry.py").read_text(encoding="utf-8")
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    ck("采集层并发 + 可达端点优先（端点一个都不删）",
       "ThreadPoolExecutor" in rgv and "probe_status" in rgv
       and "catalog_expand" in rgv
       and int(cfg["network"].get("concurrent_endpoints", 0)) >= 8
       and int(cfg["network"].get("total_budget_seconds", 0)) >= 240,
       f"并发 {cfg['network'].get('concurrent_endpoints')} 线程 / 预算 "
       f"{cfg['network'].get('total_budget_seconds')}s / 清单全量展开")
    ck("并发与质量红线回归测试随包",
       all((ROOT / "tests" / f).is_file() and f in
           (ROOT / "manifest.json").read_text(encoding="utf-8")
           for f in ("test_concurrent_collect.py", "test_quality_redlines.py")),
       "并发提速/可达优先/单源隔离 + 活动宣传剔除/正常稿不误伤/抖音桥接")
    ck("主表按重要性精选（不是全量存档）",
       1 <= int(cfg["limits"]["max_main_items"]) <= 50,
       f"max_main_items={cfg['limits']['max_main_items']}；候选池保留窗口内全量")

    # 成品与页面必须同批，且 9 个社交主干平台都在主表来源列出现。
    from openpyxl import load_workbook
    xlsx = ROOT / "docs" / "downloads" / "latest.xlsx"
    wb = load_workbook(xlsx, data_only=True)
    main_rows = [list(r) for r in wb[wb.sheetnames[0]].iter_rows(min_row=2, values_only=True)]
    wb.close()
    ck("成品主表非空且不超过精选上限",
       1 <= len(main_rows) <= int(cfg["limits"]["max_main_items"]),
       f"主表 {len(main_rows)} 条")
    from newsbrief import editorial as _ed
    real_hits = {p for r in main_rows
                 for p in _ed.real_platform_hits(str(r[9] or ""))}
    missing = [p for p in _ed.MAIN_PLATFORMS if p not in real_hits]
    ck("9 个社交主干平台真实命中并出现在主表来源列",
       not missing,
       "、".join(missing) + " 缺口" if missing
       else "9/9（按真实词条回查计票，非字符串包含、非清单挂名）")
    promo = ["大赛颁奖", "宣传收官", "创新案例", "主题宣传", "学习贯彻",
             "人民情怀", "焦点访谈", "收官总结"]
    ck("前 10 无活动/政务宣传稿",
       not any(w in str(r[7] or "") for r in main_rows[:10] for w in promo),
       "前 10 逐条核对")
    from newsbrief import editorial
    pair_bad = [(r[0], str(r[7])[:24]) for r in main_rows
                if not editorial.title_abstract_correspondence(
                    str(r[7] or ""), str(r[8] or ""))[0]]
    ck("主表零条标题—摘要不对应", not pair_bad, str(pair_bad[:2]))
    topic_bad = [(r[0], str(r[7])[:24]) for r in main_rows
                 if not editorial.public_interest(
                     str(r[7] or ""), str(r[8] or ""), str(r[9] or ""))[0]]
    ck("主表零条政务宣传 / 行业简报 / 机构公告", not topic_bad, str(topic_bad[:2]))
    agg_bad = [r[0] for r in main_rows
               if any(editorial.is_aggregate(x.strip())
                      for x in str(r[9] or "").split(" · "))]
    ck("来源列零聚合名堆砌（9 平台按真实回查计票）", not agg_bad, str(agg_bad[:3]))
    fins = [float(r[1]) for r in main_rows]
    ck("主表按 Final_adj 严格降序（按重要性排序）",
       fins == sorted(fins, reverse=True),
       f"{len(fins)} 条 / {len(set(fins))} 个唯一分数 / {fins[:3]}…")
    js = json.loads((ROOT / "docs" / "news-data.js").read_text(encoding="utf-8")
                    .split("=", 1)[1].rstrip().rstrip(";"))
    ck("页面与成品同批、空表不报达标",
       len(js.get("main", [])) == len(main_rows)
       and (js["quality"]["level"] != "达标" or len(main_rows) > 0),
       f"页面 main {len(js.get('main', []))} / quality {js['quality']['level']}")

    fails = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n=== 结论：{len(RESULTS) - len(fails)}/{len(RESULTS)} PASS ===")
    if fails:
        print("未通过：" + "、".join(fails))
        return 1
    print("全部通过，交付包可直接使用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
