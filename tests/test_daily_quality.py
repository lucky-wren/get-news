"""截图 42/43/44 与 latest.xlsx 回归：错配、政务稿、泛词误伤、挂名来源、
html 窗口漂移、⑳ 写死 True、成品逐条质量，全部钉进 pytest。"""
from __future__ import annotations

import inspect
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from newsbrief import editorial, pipeline
from newsbrief.model import Item
from newsbrief.sources import html as html_mod

ROOT = Path(__file__).resolve().parents[1]
TZ8 = timezone(timedelta(hours=8))

# 截图 42 / 43：标题与摘要讲的不是一件事
BAD_PAIRS = [
    ("陈吉宁被查，上海市委表态",
     "阿拉山口铁路口岸运力升级广交会观察：智能产品成获单“利器”AI长图：礼赞七十七载。\n"
     "　　绘就时代华章蓝色粮仓的科技“长征”——海洋探宝。"),
    ("千万级用户背书，华为Mate X8诠释大折叠旗舰新高度！",
     "实测无界方舟Edge-B：做最强端侧智能，真正成为生产力黄小艺月日千亿模型装进电脑，端侧不再做平替。\n"
     "　　Yoky月日AI创作的下一场竞争，是把作品做完AI办公如何真正懂得什么叫工作？"),
]

# 用户上传 latest.xlsx 里的政务宣传 / 行业简报 / 机构公告
BAD_TOPICS = [
    ("综合发挥纪律教育约束、保障激励作用（学习贯彻党的二十届五中全会精神）",
     "　　会议指出，要全面准确理解全会精神。\n　　与会同志一致表示要认真抓好落实。"),
    ("关于提醒ETF期权合约最后交易日、行权日、到期日的公告",
     "　　二、行权交收日，融资融券信用账户转到证券账户的合约标的不能用于当天期权的行权交收。\n"
     "　　三、认购期权权利方通过行权得到的合约标的在行权交收日下一交易日起可以卖出。"),
    ("焦点访谈｜讲清、讲准、讲实、讲透 这个宣讲团不一般！",
     "　　宣讲团走进基层，把全会精神讲清楚。\n　　干部群众纷纷表示深受启发。"),
    ("深市新观察｜大族激光：三十年淬炼 激光照见智造新图景",
     "　　企业坚持自主创新，持续深耕激光装备。\n　　三十年间产品线不断拓展。"),
]


def test_screenshot42_43_pairs_are_rejected():
    for t, a in BAD_PAIRS:
        ok, why = editorial.title_abstract_correspondence(t, a)
        assert not ok, (t, why)


def test_gov_industry_archive_rows_are_rejected():
    for t, a in BAD_TOPICS:
        ok, _, why = editorial.public_interest(t, a, "央视网")
        assert not ok, (t, why)


def test_generic_word_declaration_must_not_reject_real_news():
    """「声明」在摘要里不得误剔大众稿。"""
    t = "英伟达恢复黄仁勋总裁、CEO及董事职务"
    a = ("　　英伟达公司23日宣布，即日起恢复黄仁勋的公司总裁、首席执行官及董事职务。\n"
         "　　英伟达在声明中说，公司董事会一致通过黄仁勋复职的决定。")
    assert editorial.title_abstract_correspondence(t, a)[0]
    assert editorial.public_interest(t, a, "快手 · 中新网")[0]
    assert "声明" not in editorial._STRONG_EXCLUDE
    assert not editorial.public_interest(*BAD_TOPICS[0], "央视网")[0]


def test_aggregate_only_source_never_falls_back_to_fake_names():
    fake = "夸克/搜狗热点 · 腾讯新闻早报 / 网易 / 搜狐 / 新浪早报（清单§28）"
    assert editorial.clean_sources(fake, []) == ""
    assert editorial.real_platform_hits(fake) == []
    it = Item(title="英伟达恢复黄仁勋总裁、CEO及董事职务",
              abstract=("　　英伟达公司23日宣布恢复黄仁勋的总裁职务。\n"
                        "　　公司董事会一致通过黄仁勋复职的决定。"),
              sources=fake)
    kept, rejects, _ = editorial.select_daily([it], [])
    assert kept == []
    assert any("挂名" in r["原因"] for r in rejects)


def test_html_uses_batch_window_not_now_relative():
    src = inspect.getsource(html_mod)
    assert "_window" in src
    assert "timedelta(days=3)" not in src, "不许再用 now±3 天随日期漂移"
    assert "except Exception:\n                pass" in src, "trafilatura 异常必须降级"
    # 3 天前的稿在无窗口兜底仍应通过；旧 now±3 天会把它剔掉。
    three_days_ago = (datetime.now(TZ8) - timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
    assert html_mod._date_ok(three_days_ago), "3 天前的窗口内稿不得被相对日期误剔"
    assert not html_mod._date_ok("2020-01-01"), "明显陈旧存档稿仍须拒"
    assert not html_mod._date_ok("2030-01-01"), "远期未来稿仍须拒"


def test_html_trafilatura_exception_falls_back_and_window_is_hard():
    """CI 装 extract：bare_extraction 抛异常也必须降级出稿。"""
    article = ("<html><head><title>寒潮预警 多地提前启动供暖</title></head><body><article>"
               "<p>中央气象台11月22日发布寒潮蓝色预警，北方多地将出现大风降温天气，局地降温幅度可达10摄氏度以上。</p>"
               "<p>多地已提前启动供暖，民政部门开放临时避寒场所，公众需注意添衣保暖并关注临近预报。</p>"
               "<p>交通运输部门提醒，雨雪天气出行请留意高速管制与列车运行调整信息。</p>"
               "</article></body></html>")
    listing = ('<html><body><a href="/2026-11/22/1234567.html">寒潮预警 多地提前启动供暖</a>'
               '<a href="/about.html">关于我们</a></body></html>')

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=article if "1234567" in str(req.url) else listing)

    class Broken:
        @staticmethod
        def bare_extraction(_text):
            raise RuntimeError("trafilatura 在真实站点上炸了")

    orig = html_mod.trafilatura
    html_mod.trafilatura = Broken
    try:
        ep = {"name": "中国政府网", "url": "https://unit.test/list.html", "type": "html",
              "group_name": "中央政务部委总表",
              "_window": (datetime(2026, 11, 21, 20, 0, tzinfo=TZ8),
                          datetime(2026, 11, 22, 20, 0, tzinfo=TZ8))}
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            rows = html_mod.fetch(c, ep, 8)
            assert rows, "trafilatura 抛异常也必须降级到内置 <p> 抽取"
            assert all(r["published"] for r in rows)
            out = dict(ep, _window=(datetime(2026, 11, 23, 20, 0, tzinfo=TZ8),
                                    datetime(2026, 11, 24, 20, 0, tzinfo=TZ8)))
            assert html_mod.fetch(c, out, 8) == [], "窗口外必须 0 条"
    finally:
        html_mod.trafilatura = orig


def test_verify_20_is_a_real_assertion():
    src = inspect.getsource(pipeline.verify)
    assert 'ck("⑳ 9 个社交主干平台逐平台真实命中申报", True' not in src
    assert "len(plat_hit) >= len(editorial.MAIN_PLATFORMS) - len(missing)" in src
    assert 'ck("㉑ 来源列零清单挂名 / 零聚合名堆砌"' in src


def _same_batch_product(root=None):
    """完整包（期次成品与固定地址字节一致）才返回产物路径。

    deploy.keep_docs 会把远端旧 latest.xlsx 挂回 tree 防 Pages 404，而
    零网络自检跑在出表步之前；checkout 读旧产物必红（截图 45）。产物质量
    改由出表后 verify_package.py 硬验，pytest 不读 checkout 运行时产物。
    """
    root = root or ROOT
    latest = root / "docs" / "downloads" / "latest.xlsx"
    od = root / "output"
    period = sorted(od.glob("daily_news_*_20h.xlsx")) if od.is_dir() else []
    if not latest.is_file() or not period:
        return None
    return latest if period[-1].read_bytes() == latest.read_bytes() else None


def test_9_platforms_all_present_in_shipped_product():
    """完整包成品逐条硬验；checkout 由出表后 verify_package.py 守。"""
    xlsx = _same_batch_product()
    if xlsx is None:
        return
    from openpyxl import load_workbook
    wb = load_workbook(xlsx, data_only=True)
    rows = [list(r) for r in wb[wb.sheetnames[0]].iter_rows(min_row=2, values_only=True)]
    wb.close()
    assert rows
    assert not [r for r in rows if not editorial.title_abstract_correspondence(
        str(r[7] or ""), str(r[8] or ""))[0]]
    assert not [r for r in rows if not editorial.public_interest(
        str(r[7] or ""), str(r[8] or ""), str(r[9] or ""))[0]]
    assert not [r for r in rows if re.search(
        r"清单\s*§|（清单|\(清单|早报 / |热榜聚合", str(r[9] or ""))]
    hit = {p for r in rows for p in editorial.real_platform_hits(str(r[9] or ""))}
    assert hit <= set(editorial.MAIN_PLATFORMS), "来源列不得出现非主干平台挂名"
    eps = json.loads((ROOT / "data" / "social_endpoints.json").read_text(encoding="utf-8"))
    from newsbrief.clean import canon
    names = {canon(str(e.get("name") or "")) for e in eps.get("endpoints", [])}
    assert set(editorial.MAIN_PLATFORMS) <= names, "9 个主干平台端点必须全部在册"
    gap = [q for q in editorial.MAIN_PLATFORMS if q not in hit]
    if gap:
        kou = "\n".join(" | ".join(str(x or "") for x in r) for r in
                         load_workbook(xlsx, data_only=True)[
                             load_workbook(xlsx, data_only=True).sheetnames[3]]
                         .iter_rows(values_only=True))
        assert "缺口" in kou and all(q in kou for q in gap), "平台缺口须逐平台申报"
    fins = [float(r[1]) for r in rows]
    assert fins == sorted(fins, reverse=True), "按重要性（Final_adj）严格降序"
    assert all(r[0] == r[4] for r in rows), "展示序 = 分数序"


def test_deploy_run_does_not_hang_on_inherited_pipe():
    """孙进程持有 stdout 时 deploy.run() 仍须立即返回（双击不挂 90s）。"""
    import importlib, sys, time
    d = importlib.import_module("deploy")
    src = inspect.getsource(d.run)
    assert "capture_output=True" not in src and "TemporaryFile" in src
    code = ("import subprocess,sys;"
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(2)'],"
            "stdout=sys.stdout,stderr=sys.stderr);print('ok')")
    t0 = time.monotonic()
    out = d.run(sys.executable, "-c", code)
    assert "ok" in out.stdout and time.monotonic() - t0 < 1.5


def test_checkout_stale_xlsx_and_workflow_contract():
    """截图 45：checkout 旧 xlsx 不读；出表后必须有硬验步。"""
    import shutil, tempfile
    d = Path(tempfile.mkdtemp())
    try:
        (d / "docs" / "downloads").mkdir(parents=True)
        (d / "docs" / "downloads" / "latest.xlsx").write_bytes(b"stale")
        assert _same_batch_product(d) is None
    finally:
        shutil.rmtree(d, ignore_errors=True)
    wf = (ROOT / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")
    i = wf.index("python -m newsbrief")
    j = wf.index("git add docs/")
    assert "verify_package.py" in wf[i:j]


def test_deliverables_use_glob_not_fixed_date():
    """截图 46：CI 换日后产物名变了，写死日期的交付物清单必红。"""
    m = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    dv = [x for x in m["deliverables"] if x.startswith("output/")]
    assert dv and all("*" in x for x in dv), dv
    src = inspect.getsource(__import__("verify_package"))
    assert '"*" in p' in src and "glob" in src


def test_platform_contract_two_layers():
    """端点全纳入是硬闸；平台名只在有词条回查证据时挂，不桥接、不硬塞。"""
    import newsbrief.editorial as ed
    from newsbrief.clean import canon
    eps = json.loads((ROOT / "data" / "social_endpoints.json").read_text(encoding="utf-8"))
    names = {canon(str(e.get("name") or "")) for e in eps.get("endpoints", [])}
    assert set(ed.MAIN_PLATFORMS) <= names, "9 个主干平台端点必须全部在册"
    ed_src = inspect.getsource(ed)
    assert "MIN_SHARED_BIGRAMS" in ed_src and "MAX_PLATFORMS_PER_ITEM" not in ed_src
    assert "_bridge_missing_platforms" not in ed_src, "议题桥接硬塞不得复活"
    vp = inspect.getsource(__import__("verify_package"))
    assert "ck(\"平台缺口如实申报（零词条平台不伪装命中）\", True" not in vp
    assert "declared" in vp and "unmet" in vp

def test_bridge_only_when_shared_word_exists():
    """零词条平台绝不硬塞；有共享实词的词条才把平台挂进来源列。"""
    import newsbrief.editorial as ed
    from newsbrief.model import Item
    mk = lambda t, s: Item(t, "　　" + t + "。\n　　" + t + "。", s)
    a = mk("今日小雪各地气温下降明显", "中新网")
    log = ed.augment_platform_hits([a], [{"title": "今日小雪各地降温", "discovery": "百度贴吧"}])
    assert "贴吧" in ed.real_platform_hits(a.sources) and log["贴吧"]
    b = mk("央行开展逆回购操作", "中新网")
    log2 = ed.augment_platform_hits([b], [{"title": "旅行拍照姿势", "discovery": "小红书"}])
    assert "小红书" not in ed.real_platform_hits(b.sources) and log2["小红书"] == []

def test_social_topics_are_clues_not_dated_articles():
    """截图 46/47：榜单线索不得按快照 captured_at 卡窗口（换日曾整批剔光→0/9）。"""
    src = inspect.getsource(__import__("newsbrief.sources.social", fromlist=["x"]))
    assert "if window and not (window[0] < at <= window[1])" not in src
    assert '"published": ""' in src and "fallback" in src


def test_promo_and_industry_briefs_blocked_in_title():
    """机构成就宣传 / 行业简报不得漏进面向大众主表。"""
    import newsbrief.editorial as ed
    for w in ("服务保障", "治理观察", "全链条全周期全流程"):
        assert not ed.public_interest(
            "某市推进民生工程提供" + w + "见成效", "相关举措持续推进。", "中新网")[0], w
    for s in ("神舟二十四号成功发射 中国电科提供全链条全周期全流程服务保障",
              "广东网络餐饮治理观察：近50万商户背后的食安攻坚"):
        assert not ed.public_interest(s, "第一段事实。\n　　第二段背景。", "中新网")[0]


def test_reachable_landing_pages_and_proxy_are_configured():
    """渠道排查：实测 200 的栏目页必须真挂进清单，每端点不得再硬砍到 3 条。"""
    import json, httpx
    from pathlib import Path
    ROOT = Path(__file__).resolve().parents[1]
    cat = json.loads((ROOT / "data" / "channel_catalog.json").read_text(encoding="utf-8"))
    ok = [e for e in cat["entries"] if e.get("b") == "可达栏目页（实测 200）"
          and any(isinstance(u, list) and u[1] == 200 for u in e.get("u", []))]
    assert len(ok) >= 30, f"可达栏目页只登记 {len(ok)} 个"
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    assert cfg["network"]["html_articles_per_endpoint"] >= 12
    wf = (ROOT / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")
    assert "NEWSBRIEF_PROXY" in wf and "HTTPS_PROXY" in wf
    assert httpx.Client().trust_env, "client 不得关掉标准代理环境变量支持"


def test_policy_denial_is_reported_as_environment_not_dead_site():
    """403 policy_default_denied 必须归类为出口策略拒绝，不写成站点无数据。"""
    import inspect
    from newsbrief import registry, appendix
    assert "policy_default_denied" in inspect.getsource(registry)
    assert "出口策略拒绝" in inspect.getsource(registry)
    assert "端点可达性分类" in inspect.getsource(appendix)


def test_multi_batch_xlsx_picks_current_by_mtime():
    """多期次并存时，期次成品按生成时间认定，不能按文件名取到上一批。"""
    import tempfile, time, os
    import verify_package as V
    with tempfile.TemporaryDirectory() as d:
        from pathlib import Path
        out = Path(d) / "output"; out.mkdir()
        old = out / "daily_news_20261125_20h.xlsx"; old.write_bytes(b"old")
        cur = out / "daily_news_20261124_20h.xlsx"; cur.write_bytes(b"cur")
        os.utime(old, (time.time() - 100, time.time() - 100))
        os.utime(cur, (time.time(), time.time()))
        orig = V.ROOT
        V.ROOT = Path(d)
        try: assert V._cur_xlsx() == cur
        finally: V.ROOT = orig


def test_catalog_stays_compact():
    """清单只存数据：indent 格式化会多 65KB 空白并撑爆体积阈值。"""
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "data" / "channel_catalog.json"
    assert p.stat().st_size <= 140000, f"清单 {p.stat().st_size} 字节"


# ── 截图 47/48 + 清单渠道排查：5 条根因回归 ──────────────────────────
def test_no_gov_announcement_pages_as_news_endpoints():
    """政务部委/地方政府公告页只作台账留痕，不挂面向大众新闻采集端点。"""
    import json
    from pathlib import Path
    s = json.loads((Path(__file__).resolve().parents[1] / "sources.json").read_text(encoding="utf-8"))
    urls = [e.get("url", "") for g in s["groups"] for e in g.get("endpoints", [])]
    assert not any("linyi.gov.cn" in u or "临沂" in u for u in urls), \
        "临沂市人民政府被重新挂成新闻采集端点（截图 48）"
    assert not any("gov.cn/" in u and "/zhengce/" in u for u in urls)
    assert len([u for u in urls if "scroll-news" in u or "gmw.cn" in u]) >= 2, \
        "可达市场化栏目页被移除，渠道又变少"


def test_local_gov_promo_rejected():
    """地方公安/政务宣传稿即使蹭到「商圈」等大众词也不进主表。"""
    from newsbrief.editorial import public_interest
    ok, _, why = public_interest(
        "兰山公安：以“体系警务”解锁社区警务新路径",
        "高端商圈挨着老旧院落，海量人流叠加复杂业态，基层治理任务繁重。",
        "临沂市人民政府")
    assert ok is False and ("政务" in why or "警务" in why or "体系" in why), why


def test_hot_topic_cannot_pile_all_platforms_on_one_item():
    """证据制：平台名只在有词条回查证据时挂；不设人为上限，也不无证据硬塞。"""
    from newsbrief.model import Item
    from newsbrief.editorial import select_daily, MAIN_PLATFORMS, MIN_SHARED_BIGRAMS
    it = Item("黄仁勋闪电复出 英伟达董事会宣布恢复职务",
              "　　英伟达董事会宣布，黄仁勋恢复总裁兼首席执行官职务。\n"
              "　　距离这位 AI 芯片巨头掌门人宣布辞职，仅过去 11 天。", "中新网")
    it.I, it.C, it.V, it.final_adj = 4, 5, 12.0, 70
    kept, _, _ = select_daily([it], [])
    assert kept and "中新网" in kept[0].sources
    assert not any(p in kept[0].sources for p in MAIN_PLATFORMS), "零词条不得挂名"
    topics = [{"title": "黄仁勋 英伟达 复出", "discovery": p} for p in MAIN_PLATFORMS]
    kept2, _, st = select_daily([it], topics)
    hits = [p for p in MAIN_PLATFORMS if p in kept2[0].sources]
    assert len(hits) >= MIN_SHARED_BIGRAMS, "有证据的平台应如实挂上"
    assert kept2[0].record_type == "独立原文", "平台发现渠道不冒充独立信源"

def test_catalog_api_endpoints_not_treated_as_html():
    """§31 免 key 结构化源必须按 jsonapi/rss 解析，不能当 html 抽 <a>。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "newsbrief" / "registry.py").read_text(encoding="utf-8")
    assert 'gdeltproject.org/api/v2/doc' in src and '"jsonapi"' in src
    assert 'rsshub.app' in src and 'news.google.com/rss' in src
    from newsbrief.registry import discover
    assert "jsonapi" in discover(), "GDELT/搜索 API 采集器未注册"


def test_jsonapi_parses_gdelt_artlist_with_time_caveat():
    """GDELT seendate 转成可核对时间，并逐条标注非原文首发。"""
    import httpx
    from newsbrief.sources.rss import fetch_jsonapi
    sample = {"articles": [
        {"title": "测试稿：冷空气影响多地", "url": "https://example.com/a",
         "seendate": "20261124T120000Z"},
        {"title": "无时间稿", "url": "https://example.com/b"}]}
    c = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=sample)))
    rows = fetch_jsonapi(c, {"name": "GDELT", "group_id": 18,
                             "url": "https://api.gdeltproject.org/api/v2/doc/doc",
                             "group_name": "国际三类重大突发"}, 6)
    assert len(rows) == 2
    assert rows[0]["published"].startswith("2026-11-24")
    assert rows[0]["evidence"] == "B" and "非原文首发" in rows[0]["time_note"]


def test_nine_platforms_spread_across_independent_items():
    """9 平台各自有真实词条回查命中；不设单条上限、不桥接凑数。"""
    from newsbrief.model import Item
    from newsbrief.editorial import (augment_platform_hits, real_platform_hits,
                                     MAIN_PLATFORMS)
    mk = lambda t, s: Item(t, "　　" + t + "。\n　　" + t + "。", s)
    items = [mk(*x) for x in (
        ("黄仁勋重新出任英伟达CEO", "中新网"),
        ("神舟二十四号乘组名单公布", "央视新闻"),
        ("今日小雪各地降温", "中央气象台"),
        ("医保目录新增药品价格下调", "国家医保局"),
        ("台风黄色预警多趟航班取消", "中央气象台"),
    )]
    B = (("黄仁勋重新出任英伟达CEO", "微博热搜"), ("黄仁勋复职英伟达", "今日头条"),
         ("神舟二十四号乘组名单公布", "快手"), ("神舟二十四号发射时间", "百度热搜"),
         ("今日小雪多地降温", "知乎热榜"), ("小雪天气穿搭降温", "小红书"),
         ("医保目录调整药品", "微信搜一搜·搜索发现"), ("台风预警航班取消", "百度贴吧"),
         ("医保药品降价", "抖音热榜"))
    log = augment_platform_hits(items, [{"title": a, "discovery": b} for a, b in B])
    assert all(log[p] for p in MAIN_PLATFORMS), {p: len(v) for p, v in log.items()}
    assert set(MAIN_PLATFORMS) <= {p for it in items for p in real_platform_hits(it.sources)}


def test_user_batch_daily_quality_regressions():
    """用户上传批次 50 条里的真实硬伤：逐字夹具，规则层必须全部拦住/保住。"""
    from newsbrief.editorial import (title_abstract_correspondence, public_interest,
                                     pool_filter, MIN_SHARED_BIGRAMS)
    from newsbrief.model import Item
    from newsbrief.clean import normalize_source
    bad = [
        ("朱忠明召开上海市政府党组会议：把加强党性修养作为终身课题",
         "中共政治局委员、上海市委书记陈吉宁被查。", "今日头条 · 百度"),
        ("京城小精灵上线！北京城里藏着好多小可爱",
         "何小鹏：做机器人要比做汽车难倍都视频北京日报客户端全过程回顾！", "北京日报"),
        ("AI面试官上岗引关注 评论：需兼顾效率与公平AI面试官上岗引关注 评论：需兼顾效率与公平",
         "此前不少高校引入AI模拟面试设备，让求职者有更多练习机会。", "中新网"),
        ("8省市获批开展6G试点释放什么信号？专家：芯片、卫星制造等将迎来新机遇",
         "对话智源研究院院长王仲远：物理AI尚未迎来GPT-3时刻，行业需转向实证驱动。"
         "真正被冲击的是劣质产品亿元布局AI。", "每日经济新闻"),
        ("-2026中国饭店业高质量大会在三亚开幕",
         "开幕式上，中国饭店协会会长围绕行业品牌建设发表致辞。", "中新网"),
        ("秋日古籍展在图书馆延时开放 市民可预约参观",
         "市图书馆推出秋季古籍特展，多处阅读空间同步延时开放。"
         "市民可通过公众号预约。", "工人日报"),
        ("今日小雪各地降温",
         "今日小雪各地降温天气提醒都视频北京日报客户端站点提示！"
         "美股收盘指数波动，居民需注意添衣。", "北京日报"),
    ]
    for t, a, s in bad:
        assert not (title_abstract_correspondence(t, a)[0]
                    and public_interest(t, a, s)[0]), t[:28]
    good = [
        ("工作没着落，租房太早、酒店太贵？这些青年驿站懂你",
         "新华社贵阳11月24日电 共享办公区，创业青年对着电脑讨论项目思路。"
         "咖啡休闲区，毕业生凑在一起交流面试信息。", "新华网"),
        ("呼吸道疾病进入高发季 儿童什么时候需要就医",
         "新华社长沙11月24日电 进入冬季，呼吸道疾病进入高发期。"
         "一些家长面对孩子发烧、咳嗽容易紧张。", "中新网"),
        ("全国大部雨雪稀少迎回暖 最高气温20℃线将北抬至河北一带",
         "今明天我国雨雪范围明显收缩，后天起大部地区以晴朗干燥天气为主。"
         "监测显示，昨日云南南部出现大到暴雨。", "新华网"),
    ]
    for t, a, s in good:
        assert title_abstract_correspondence(t, a)[0], t[:24]
        assert public_interest(t, a, s)[0], t[:24]
    # 台账挂名在候选池层就被剥掉，绝不出现在来源列
    it = Item("冷空气持续影响 广东最低气温降至10.3℃",
              "　　中新网广州11月25日电 在冷空气持续影响下。"
              "\n　　广东多地24日白天到25日白天出现降雨。", "工信部（清单§3）")
    kept, edge = pool_filter([it], None)
    assert kept and "清单§" not in kept[0].sources
    assert normalize_source("工信部（清单§3）") == "工信部"
    assert MIN_SHARED_BIGRAMS >= 2 and "MAX_PLATFORMS_PER_ITEM" not in (
        Path(__file__).resolve().parents[1] / "newsbrief" / "editorial.py"
    ).read_text(encoding="utf-8")
