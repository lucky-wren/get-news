"""生产路径端到端测试：workflow 真正执行的 `python -m newsbrief --mode live`。

背景（2026-11-24 用户两次指出的同一个错）：上一版所有验证都走
`--replay-capture`，把上一批 xlsx 当输入、还把 77 条一律按「回执收录时刻
2026-11-24 12:00」洗成当批窗口内，于是交付展示 18 条；而 CI / 用户环境跑
的 `python -m newsbrief --mode live` 从来没被端到端验过，真跑出来 66/9。

本测试用 MockTransport 零网络钉死生产路径，防止第三次绕开：
  1. 端点全失败：注入语料 + 本批快照必采，主表不能塌成个位数；
  2. 网络可达（CCTV 栏目页全量灌回）：采集层按窗口过滤，质量闸不误伤泛词；
  3. 时间窗口硬闸：候选池里每条都必须有可核对发布时间且落在窗口内；
  4. CI 采集步骤必须是生产路径，不能带 replay-capture；
  5. 诊断回放不得洗时间、不得覆盖固定下载地址与页面。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from newsbrief import pipeline, registry, rules

TZ = timezone(timedelta(hours=8))
WIN = (datetime(2026, 11, 23, 20, 0, tzinfo=TZ),
       datetime(2026, 11, 24, 20, 0, tzinfo=TZ))
SOCIAL = ["微博", "百度", "抖音", "今日头条", "知乎", "小红书",
          "快手", "贴吧", "微信搜一搜", "B站"]
PROPAGANDA = ["总书记", "人民情怀", "焦点访谈", "权威访谈", "学习贯彻",
              "宣讲团", "政绩观", "大国良性互动", "来深圳", "单行本",
              "记者手记", "全家福", "青铜如故", "系列重要讲话"]


def _run(handler):
    cfg, ch = pipeline.load_config()
    with httpx.Client(transport=httpx.MockTransport(handler),
                      follow_redirects=True) as c:
        return pipeline.run(mode="live", cfg=cfg, channels=ch,
                            date="2026-11-24", window=WIN, client=c)


def _blocked(req):
    return httpx.Response(403, content=b'"No policy rule matched"')


def _assert_clean(b, min_main: int):
    assert len(b.main) >= min_main, f"主表仅 {len(b.main)} 条，疑似质量闸误伤或采集层塌了"
    assert len(b.candidate) >= len(b.main), "主表必须是候选池子集"
    # 时间窗口硬闸：候选池每条都要有可核对时间且落在窗口内
    from newsbrief.clean import parse_pub
    for it in b.candidate:
        p = parse_pub(it.published)
        assert p is not None, f"候选池有无时间依据条目：{it.title[:30]}"
        assert WIN[0] < p <= WIN[1], f"候选池有超窗条目：{it.title[:30]} / {it.published}"
    # 主表不得混入政务宣传栏目稿
    for it in b.main:
        assert not any(w in it.title for w in PROPAGANDA), \
            f"主表混入宣传稿：{it.title[:40]}"
    # 9 个社交主干平台不能集体消失
    src = {c.strip() for it in b.main for c in it.sources.split(" · ") if c.strip()}
    hit = {p: sum(1 for s in src if p in s) for p in SOCIAL}
    assert sum(1 for v in hit.values() if v > 0) >= 7, \
        f"社交主干平台覆盖不足：{hit}"


def test_production_live_all_endpoints_blocked():
    """端点全失败（沙盒 / 出口拦截）：只能靠注入语料 + 本批快照，仍须成稿。"""
    b = _run(_blocked)
    _assert_clean(b, min_main=12)
    tc = b.rule_trace.counters.get("时间窗口", {})
    # 缓存回吐不得再灌几百条上一批旧稿
    assert tc.get("超窗口", 999) <= 40, f"超窗口留痕异常膨胀：{tc}"
    assert tc.get("时间依据缺失", 999) == 0 or tc.get("窗口内", 0) >= 12


def test_production_live_cctv_reachable_no_false_reject():
    """网络可达：CCTV 栏目页全量灌回时，采集层按窗口过滤、质量闸不误伤泛词。"""
    lst = []
    for i, (title, brief, ts) in enumerate([
        ("公安机关通报一起公共安全事件处置情况", "　　当地公安机关已发布通报。\n　　现场秩序已恢复正常。", "2026-11-24 09:00"),
        ("上海地铁12月1日起实施新票价 配套优惠同步调整", "　　记者从上海市发展改革委获悉。\n　　新票价方案同步公布。", "2026-11-24 10:00"),
        ("多部门推进汽车消费全链条扩容升级", "　　商务部等9部门出台措施。\n　　覆盖买车用车回收环节。", "2026-11-24 11:00"),
        ("从前10个月用电量透视经济向稳向新", "　　国家能源局发布数据。\n　　数字经济成为新引擎。", "2026-11-24 12:00"),
        ("全国先进制造业大会9月16日至17日在京召开", "　　会上传达了重要指示。\n　　制造业是立国之本。", "2026-09-16 09:00"),
    ]):
        d = datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        lst.append({"title": title, "brief": brief,
                    "focus_date": str(int(d.timestamp())),
                    "url": f"https://news.cctv.com/2026/11/24/t{i}.shtml"})
    payload = json.dumps({"data": {"list": lst}}, ensure_ascii=False)

    def handler(req):
        if "cmsdatainterface" in str(req.url):
            return httpx.Response(200, text="news(" + payload + ");",
                                  headers={"content-type": "application/javascript"})
        return _blocked(req)

    b = _run(handler)
    _assert_clean(b, min_main=12)
    # 「安全」「全链条」「透视」「扩容升级」是泛词，不能被当成剔除依据
    rejected = [d for d in b.rule_trace.decisions if d.get("判定") == "剔除"]
    for d in rejected:
        reason = str(d.get("原因", ""))
        assert "命中 安全" not in reason, f"泛词『安全』误伤：{d.get('标题')}"
        assert "扩容升级" not in reason and "全链条" not in reason, \
            f"泛词『扩容升级/全链条』误伤：{d.get('标题')}"
        assert "透视" not in reason and "晴雨表" not in reason, \
            f"泛词『透视/晴雨表』误伤：{d.get('标题')}"
    # 9 月旧稿必须在采集层就被挡掉，不能进候选池
    assert not any("9月16" in it.title for it in b.candidate), \
        "栏目页 9 月旧稿没被采集层窗口过滤挡住"


def test_workflow_runs_production_path_not_replay():
    """CI 采集步骤必须是生产路径；replay-capture 只能是诊断工具。

    截图 42：上一版把「workflow 文件里必须含 --window-start 字面量」写成硬
    断言，而 GitHub 按触发时的 workflow 定义执行，改过文件后第一次运行
    checkout 到的仍是旧定义 → 自检自己判红。现在只硬校验代码契约，
    inputs 的版本差异交给 workflow_contract.check() 容忍，推送时由
    deploy.verify_head_state() 强校验 HEAD 是新版。
    """
    from newsbrief import workflow_contract as wc
    wf = (pipeline.ROOT / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")
    r = wc.check(wf)
    assert r["has_collect"], "workflow 缺采集并出表步骤"
    assert r["production"], "workflow 不能拿诊断回放当生产采集"
    assert r["window_wired"], r["note"]
    main = (pipeline.ROOT / "newsbrief" / "__main__.py").read_text(encoding="utf-8")
    assert "diagnose = bool(args.replay_capture)" in main
    assert "if batch.mode == \"live\" and not diagnose:" in main, \
        "诊断回放不得覆盖固定下载地址与页面"


def test_workflow_contract_tolerates_old_definition():
    """反证：CI 用旧 workflow 定义触发时，契约检查不得判红；
    新定义必须校验到 inputs 与 CLI 传参。这是截图 42 两条 FAIL 的回归锁。
    """
    from newsbrief import workflow_contract as wc
    OLD = """name: daily-newsbrief
on:
  schedule:
    - cron: '0 12 * * *'
  workflow_dispatch:
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: 采集并出表
        run: python -m newsbrief --mode live
  deploy:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - id: deployment
        uses: actions/deploy-pages@v4
"""
    r = wc.check(OLD)
    assert r["ok"] and r["production"] and r["window_wired"] and r["old_definition"], \
        f"旧定义被误判为 FAIL：{r}"
    ok, missing = wc.head_is_new(OLD)
    assert not ok and missing, "推送时的 HEAD 强校验必须能认出旧定义"

    # 「新定义」必须内联，不能拿仓库当前文件当样本：CI checkout 里的
    # daily.yml 完全可能就是旧版，那样反证测试自己又会判红（截图 42 同款）。
    NEW = OLD.replace("  workflow_dispatch:\n", """  workflow_dispatch:
    inputs:
      window_start:
        description: '窗口左端'
        required: false
        default: ''
      window_end:
        description: '窗口右端'
        required: false
        default: ''
""").replace("        run: python -m newsbrief --mode live", """        env:
          WINDOW_START: ${{ inputs.window_start }}
          WINDOW_END: ${{ inputs.window_end }}
        run: |
          args=(--mode live)
          if [ -n "$WINDOW_START" ]; then args+=(--window-start "$WINDOW_START"); fi
          if [ -n "$WINDOW_END" ]; then args+=(--window-end "$WINDOW_END"); fi
          python -m newsbrief --mode live --window-start "$WINDOW_START" --window-end "$WINDOW_END"
""")
    r2 = wc.check(NEW)
    assert r2["ok"] and not r2["old_definition"], f"新版 workflow 契约未通过：{r2}"
    ok2, missing2 = wc.head_is_new(NEW)
    assert ok2 and not missing2, f"HEAD 强校验误判新版：缺 {missing2}"


def test_replay_capture_never_rewrites_publish_time():
    """诊断回放不能把「回执收录时刻」冒充原文发布时间洗进窗口。"""
    from newsbrief.__main__ import read_capture
    cap = pipeline.ROOT.parent / "inputs" / "latest (1).xlsx"
    if not cap.is_file():
        # 包内自检 / CI checkout 没有用户上传件：直接验代码文本契约
        src = (pipeline.ROOT / "newsbrief" / "__main__.py").read_text(encoding="utf-8")
        assert "按回执收录时刻填充" in src
        assert 'pub = ""' in src
        return
    rows = read_capture(cap, WIN)
    assert rows, "诊断输入为空"
    filled = [r for r in rows if r["published"] == "2026-11-24 12:00"]
    assert not filled, f"仍有 {len(filled)} 条被洗成 2026-11-24 12:00"
    assert any(not r["published"] for r in rows), \
        "无原文发布时间的条目应留空由硬闸剔除，而不是编一个时刻"
