"""资讯获取 · 搜索层质量回归（纯代码、离线、不联网）。

把「高质量结果数据」的硬闸做成可复跑用例，防止后续调阈值 / 放宽判定式
把官网首页、百科、UGC 专栏、旧闻、PDF 又放回主表。

用法：
    python scripts/verify_search_quality.py
    python scripts/verify_search_quality.py --json output/search_quality.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from news_pipeline import search_api as sa  # noqa: E402


try:
    from zoneinfo import ZoneInfo

    class Win:
        # 必须与真实 Window 一致带时区：naive/aware 混比较曾让旧闻闸门静默失效
        start = datetime(2026, 11, 18, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        end = datetime(2026, 11, 19, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
except ImportError:
    class Win:  # pragma: no cover
        start = datetime(2026, 11, 18, 20, 0)
        end = datetime(2026, 11, 19, 20, 0)


def hit(title, url, snippet, date="", source=""):
    return sa.SearchHit(title=title, url=url, snippet=snippet, date=date,
                        source=source, provider="bing")


Q = "医保目录 新增药品 2026-11-19"
GOOD_SNIPPET = "11月19日发布，新增91种药品，预计每年为患者减负400亿元，参保人报销比例同步调整。"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", dest="json_out", default="")
    args = ap.parse_args()

    w = Win()
    cases = []

    def check(name, ok, detail=""):
        cases.append({"用例": name, "通过": bool(ok), "说明": detail})

    # 1) 模块自带自检（打分排序 / 硬闸 / Bing 解析器 / 缓存）
    st = sa.self_test()
    check("search_api.self_test", st["通过"], "、".join(st["错误"]) or "全绿")

    # 2026-11-20 Actions 崩溃防回退：缓存空结果曾把 chosen_url 写成 int 0，
    # trace_rows() 在 pipeline 尾部抛 TypeError，12m11s 采集白跑、Excel 未落盘。
    try:
        sa.reset()
        sa.TRACE.append(sa.TraceRow(query="缓存空结果", provider="cache",
                                    status="无结果", chosen_url=0,  # type: ignore[arg-type]
                                    chosen_score=0.0))
        sa.TRACE.append(sa.TraceRow(query="正常命中", provider="bing", status="成功",
                                    chosen_url="https://www.gov.cn/a/2026-11-19/1.htm",
                                    chosen_score=61.2))
        rows = sa.trace_rows()
        check("trace_rows 容忍 int chosen_url", len(rows) == 2 and str(rows[0][4]) == "0",
              "首行选中URL=%r" % (rows[0][4] if rows else None))
        check("trace_rows 保留正常独立原文 URL",
              any(str(r[4]).startswith("https://") for r in rows))
        check("provider_summary / stats 不因脏留痕抛异常",
              isinstance(sa.provider_summary(), list) and sa.stats()["逐次留痕"] == 2)
    except Exception as exc:
        check("留痕序列化容错回归", False, f"{type(exc).__name__}: {exc}")

    good = hit("国家医保局发布新版医保目录 新增91种药品",
               "https://www.gov.cn/zhengce/202611/19/content_7050533.htm",
               GOOD_SNIPPET, "2026-11-19", "www.gov.cn")
    check("优质窗口内权威原文不被误拦",
          sa._reject_reason(good, w, Q) == "",
          sa._reject_reason(good, w, Q))
    check("优质候选分数显著高于低质样本",
          sa.score_hit(good, Q, w).score >= 60,
          "score=%s" % sa.score_hit(good, Q, w).score)

    bad = [
        ("官网首页", hit("国家医疗保障局", "https://www.nhsa.gov.cn/",
                     GOOD_SNIPPET, "2026-11-19", "www.nhsa.gov.cn")),
        ("常设栏目页", hit("寒潮预警 多地降温", "https://www.nmc.cn/publish/country/warning/cold.html",
                      GOOD_SNIPPET, "2026-11-19", "www.nmc.cn")),
        ("PDF 附件", hit("医保目录全文", "https://www.cnpharm.com/upload/a.pdf",
                     GOOD_SNIPPET, "2026-11-19", "www.cnpharm.com")),
        ("百度百科", hit("医疗保险制度", "https://baike.baidu.com/item/医疗保险制度/4210",
                     GOOD_SNIPPET, "2026-11-19", "baike.baidu.com")),
        ("知乎专栏 UGC", hit("医保目录解读 新增91种药品", "https://zhuanlan.zhihu.com/p/680612539",
                        GOOD_SNIPPET, "2026-11-19", "zhuanlan.zhihu.com")),
        ("百度知道 UGC", hit("医保目录新增药品怎么报销", "https://zhidao.baidu.com/question/821927681668043212.html",
                        GOOD_SNIPPET, "2026-11-19", "zhidao.baidu.com")),
        ("CSDN 博客 UGC", hit("裁员补偿计算方法", "https://blog.csdn.net/qq362196705/article/details/162044645",
                        GOOD_SNIPPET, "2026-11-19", "blog.csdn.net")),
        ("date 字段旧闻", hit("国家医保局发布新版医保目录 新增91种药品",
                         "https://www.gov.cn/zhengce/content_1.htm",
                         GOOD_SNIPPET, "2025-12-26", "www.gov.cn")),
        ("URL 年月目录旧闻（date 为空）", hit("国家医保局发布新版医保目录 新增91种药品",
                                 "https://www.gov.cn/zhengce/202608/content_7078874.htm",
                                 GOOD_SNIPPET, "", "www.gov.cn")),
        ("date 与 URL 日期冲突的旧闻", hit("国家医保局发布新版医保目录 新增91种药品",
                                 "https://www.gov.cn/zhengce/202608/content_7078874.htm",
                                 GOOD_SNIPPET, "2026-11-19", "www.gov.cn")),
        ("2025 年地方站旧闻", hit("洪水灾害救援通报 转移群众",
                          "https://slt.xinjiang.gov.cn/xjslt/c114445/202509/abc.shtml",
                          GOOD_SNIPPET, "", "slt.xinjiang.gov.cn")),
        ("栏目百科名标题", hit("寒潮 -中国气象局政府门户网站",
                       "https://www.cma.gov.cn/2011xzt/kpbd/ColdWave/",
                       GOOD_SNIPPET, "2026-11-19", "www.cma.gov.cn")),
        ("标题与检索词几乎无关", hit("天气预报查询入口",
                            "https://www.gov.cn/zhengce/202611/19/content_1.htm",
                            GOOD_SNIPPET, "2026-11-19", "www.gov.cn")),
    ]
    for name, h in bad:
        reason = sa._reject_reason(h, w, Q)
        check("拦下：" + name, bool(reason), reason)

    # 2) 无 key 的 provider 不计真实调用次数（避免 Excel 出现虚假的 16 次 Serper）
    sa.reset()
    cfg = {"search_api": {"provider_order": "serper,tavily,bing", "max_calls": 30,
                          "timeout": 0.01, "min_score": 18}}
    try:
        sa.search_best("离线自检查询 2026-11-19", win=w, cfg=cfg)
    except Exception:
        pass
    calls = sa.stats()["调用"]
    check("无 key 的 serper/tavily 不计调用次数",
          calls.get("serper", 0) == 0 and calls.get("tavily", 0) == 0,
          str(calls))
    check("调用计数只对真实发起请求的 provider 累加",
          calls.get("total", 0) == sum(v for k, v in calls.items() if k != "total"),
          str(calls))

    passed = sum(1 for c in cases if c["通过"])
    result = {"结论": "达标" if passed == len(cases) else "不达标",
              "通过": passed, "总数": len(cases), "用例": cases}
    print("搜索层质量回归：%s  %d/%d 项通过" % (result["结论"], passed, len(cases)))
    for c in cases:
        if not c["通过"]:
            print("  FAIL", c["用例"], "→", c["说明"])
    if args.json_out:
        p = Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print("结构化结果 →", p)
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
