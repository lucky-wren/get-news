"""包完整性自检 + 部署链路回归。

两起事故各对应一组断言：
- `cannot import name 'appendix'`：仓库只同步了部分文件。
- 更隐蔽的一起：deploy.py 把允许的模块名写死在 ALLOWED_* 白名单里，
  新增 appendix.py / rules.py / sources/inbox.py / tests/conftest.py 后忘了同步，
  本地包再完整、推上去的 tree 仍然缺件，CI 报同样的错。
  因此这里**直接调用 deploy.push_files()**，断言 manifest.required 全在推送清单里。
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifest.json"


def _m() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


MODULES = [
    "newsbrief", "newsbrief.appendix", "newsbrief.clean", "newsbrief.model",
    "newsbrief.pipeline", "newsbrief.registry", "newsbrief.rules",
    "newsbrief.score", "newsbrief.sheet", "newsbrief.site", "newsbrief.__main__",
    "newsbrief.sources.geojson", "newsbrief.sources.html",
    "newsbrief.sources.inbox", "newsbrief.sources.rss", "newsbrief.sources.cctv",
]


def test_manifest_is_single_source_of_truth():
    assert MANIFEST.is_file(), "缺 manifest.json"
    m = _m()
    assert m["required"] and isinstance(m["required"], list)


def test_required_files_present():
    miss = [p for p in _m()["required"] if not (ROOT / p).is_file()]
    assert not miss, ("交付包缺运行必需文件：" + "、".join(miss)
                      + "；请整体替换 newsbrief/ 目录与 data/、tests/")


def _cur_xlsx():
    """本批成品：output/ 下按日期字典序最新的 daily_news_*_20h.xlsx。"""
    xs = sorted((ROOT / "output").glob("daily_news_*_20h.xlsx"))
    return xs[-1] if xs else None


def _is_checkout() -> bool:
    """CI 的「零网络自检」在采集之前跑，仓库里没有 output/ 成品是正常的。

    成品由后续「采集并出表」步生成；完整交付包才必须带齐。
    """
    import os
    # CI 的「零网络自检」在 checkout 之后、出表之前；GitHub 变量比
    # 「output 里有没有 xlsx」更可靠（上一步可能留下了产物目录）。
    return (_cur_xlsx() is None
            or bool(os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI")))


def test_deliverables_in_package():
    if _is_checkout():
        pytest.skip("CI checkout 零网络自检：运行时产物由构建步骤生成，非本步职责")
    miss = [p for p in _m()["deliverables"] if not (ROOT / p).is_file()]
    assert not miss, "完整交付包缺随包交付物：" + "、".join(miss)


@pytest.mark.parametrize("mod", MODULES)
def test_module_importable(mod):
    importlib.import_module(mod)


def test_source_registry_complete():
    from newsbrief import registry
    reg = registry.discover()
    assert {"rss", "html", "geojson", "inbox", "jsonp"} <= set(reg), (
        f"采集器注册不全：{sorted(reg)}；请确认 newsbrief/sources/ 下 5 个文件都在")


def test_deploy_push_list_covers_every_required_file():
    """核心回归：推什么，就必须在仓库里能 import 什么。

    直接调用 deploy.push_files(manifest)，不再依赖「本地目录能跑 pytest」这种
    会漏掉部署链路的假验证。缺一个文件，这里当场红。
    """
    import deploy
    m = _m()
    files = deploy.push_files(m)
    miss = deploy.preflight(m, files)
    assert not miss, ("deploy.py 推送清单缺以下必需文件（推上去 CI 必炸）："
                      + "、".join(miss))


def test_deploy_no_hardcoded_allowlist():
    """禁止再出现会随新增模块失效的白名单（只查赋值语句，允许注释里提历史）。"""
    import re
    src = (ROOT / "deploy.py").read_text(encoding="utf-8")
    for old in ("ALLOWED_MODULES", "ALLOWED_SOURCES", "ALLOWED_TESTS"):
        assert not re.search(r"^" + old + r"\s*=", src, re.M), \
            f"deploy.py 又出现硬编码白名单赋值 {old}"


def test_deploy_tree_verification_exists():
    """推送前后都要自检，且自检对象是 HEAD tree 而非本地目录。"""
    import deploy
    assert hasattr(deploy, "preflight") and hasattr(deploy, "verify_tree")
    src = (ROOT / "deploy.py").read_text(encoding="utf-8")
    assert "ls-tree" in src and "拒绝推送" in src


def test_verify_tree_survives_quotepath_and_garbled_decode(tmp_path, monkeypatch):
    """截图 32 回归：Windows 中文环境下 deploy 报「交付说明.md 没进新提交」。

    两个坑叠加：
      ① git 默认 core.quotepath=true，非 ASCII 路径被转义成八进制；
      ② text=True 按系统 locale（中文 Windows 常为 GBK）解码 git 的 UTF-8 输出，
         中文名变乱码。
    旧写法因此把「文件确实在 tree 里」误判成缺件，返回 1 → Deploy failed。

    新实现用 -z 原始字节 + 显式 UTF-8，并用 cat-file -e 兜底。
    """
    import os
    import subprocess

    import deploy

    repo = tmp_path / "r"
    repo.mkdir()
    env = os.environ.copy()

    def git(*a, **kw):
        return subprocess.run(["git", *a], cwd=repo,
                              capture_output=True, env=env, **kw)

    git("init", "-b", "main")
    git("config", "user.name", "t")
    git("config", "user.email", "t@t")
    git("config", "core.quotepath", "true")           # Windows 默认行为
    (repo / "交付说明.md").write_text("x", encoding="utf-8")
    (repo / "README.md").write_text("x", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "t")

    monkeypatch.setattr(deploy, "ROOT", repo)
    m = {"required": ["README.md", "交付说明.md"]}
    # 新实现：中文名与 ASCII 名都必须判定为「在 tree 里」
    assert deploy.verify_tree(m) == [], "中文文件名被误判为缺件（Deploy failed）"

    # 反证：旧写法（text=True、无 -z）在 GBK 解码下必然误报
    p = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"],
                       cwd=repo, capture_output=True, env=env)
    as_windows_would_decode = set(p.stdout.decode("gbk", "replace").splitlines())
    assert "交付说明.md" not in as_windows_would_decode, \
        "本条反证失效：GBK 解码居然没把中文名弄乱，请改用更强的模拟"
    # 旧写法的结果（用于说明根因，不作为交付判据）
    legacy = [r for r in m["required"] if r not in as_windows_would_decode]
    assert legacy == ["交付说明.md"], legacy


def test_verify_tree_uses_raw_bytes_not_locale_decode():
    """防退化：自检不得再依赖 text=True 的系统 locale 解码。"""
    src = (ROOT / "deploy.py").read_text(encoding="utf-8")
    assert '"-z"' in src, "必须用 ls-tree -z 拿原始字节，绕开引号转义"
    assert "text=False" in src, "必须拿 bytes，不能交给系统 locale 解码"
    assert 'decode("utf-8"' in src, "必须显式按 UTF-8 解码"
    assert "cat-file" in src, "必须有只看 returncode 的兜底"
    assert "core.quotepath=false" in src, "quotepath 也要显式关闭"


def test_fixed_download_matches_output():
    if _is_checkout():
        pytest.skip("CI checkout 零网络自检：固定地址与成品由构建步骤生成")
    a = _cur_xlsx()
    b = ROOT / "docs" / "downloads" / "latest.xlsx"
    assert a.is_file() and b.is_file() and a.read_bytes() == b.read_bytes(), (
        "docs/downloads/latest.xlsx 与 output 成品不一致，页面会挂旧批次")


# ── 2026-11-23 第三轮：渠道太少 + 摘要质量（用户上传的线上 latest.xlsx） ──

def test_no_stale_feeds_in_channel_ledger():
    """人民网 RSS 停在 2025-06-05、新华网 RSS 是 2022 疫情稿且无 pubDate；
    旧版把它们挂在 §23，12 个端点 82% 的 raw 全是超窗死 feed，看着渠道多实际没数据。
    """
    ch = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    eps = [e for g in ch["groups"] for e in g["endpoints"]]
    stale = [e["url"] for e in eps if "people.com.cn/rss" in e["url"]
             or "news_politics.xml" in e["url"] or "news_world.xml" in e["url"]]
    assert not stale, "停更死 feed 复活：" + "、".join(stale)
    assert len(ch["groups"]) == 31, "31 类逐类申报结构不能变"


def test_cctv_jsonp_channels_are_wired():
    """CCTV cmsdatainterface 当天 80 条/页带秒级 focus_date 与正文 brief，
    实测可达且旧版一个都没挂 —— 这是「渠道太少」的头号可修复根因。
    """
    ch = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    eps = [e for g in ch["groups"] for e in g["endpoints"]]
    js = [e for e in eps if e.get("type") == "jsonp"]
    assert len(js) >= 8, f"CCTV JSONP 端点太少：{len(js)}"
    assert all("news.cctv.com" in e["url"] and e.get("pages", 1) >= 1 for e in js)
    assert len(eps) >= 60, f"台账端点总数退化：{len(eps)}"
    assert not any("gdacs.org" in e["url"] for e in eps), \
        "GDACS 403 且首页会被抽成 register/login 壳，不得再挂"


def test_jsonp_collector_parses_real_shape():
    """真格式 JSONP（news({...})）→ 带秒级时间与导语的独立原文。"""
    import httpx
    from newsbrief.sources import cctv
    payload = ('news({"data":{"total":500,"list":[{'
               '"title":"三部门联合发布第六批鼓励研发申报儿童药品清单",'
               '"brief":"为深入贯彻全国卫生与健康大会精神，落实相关要求。鼓励研发创制儿童适用品种，满足儿童患者用药需求。",'
               '"focus_date":"2026-11-23 16:36:24",'
               '"url":"https://news.cctv.com/2026/11/23/ARTI123.shtml"}]}})')
    def handler(req):
        return httpx.Response(200, text=payload)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    rows = cctv.fetch(client, {"name": "央视网·要闻", "url": "https://x/n_1.jsonp",
                               "pages": 1, "max_items": 80}, 6)
    assert len(rows) == 1
    assert rows[0]["published"] == "2026-11-23 16:36:24"
    assert "满足儿童患者用药需求" in rows[0]["body"]
    assert rows[0]["evidence"] == "A"


def test_abstract_never_echoes_title():
    """用户表里 `日本熊本县发生4.1级地震 多地有震感。` / `黄金跌跌不休，何时反弹？。`
    是旧 make_abstract 用标题补第二段的产物；本版宁可返回 None 也不复读。
    """
    from newsbrief.clean import make_abstract
    assert make_abstract("日本熊本县发生4.1级地震 多地有震感",
                         "日本熊本县天草、芦北地区发生4.1级地震，震源深度10公里。") is None
    a = make_abstract("日本熊本县发生4.1级地震 多地有震感",
                      "日本熊本县天草、芦北地区发生4.1级地震，震源深度10公里。"
                      "当地多地有震感，暂无人员伤亡报告。")
    assert a and "日本熊本县发生4.1级地震 多地有震感。" not in a


def test_abstract_rejects_half_sentence_and_site_shell():
    from newsbrief.clean import make_abstract
    from newsbrief.model import nav_dump_reason
    assert make_abstract("据沙特《", "中新社北京11月21日电 据沙特《") is None
    assert make_abstract("register/login",
                         "register/login YouneedtoenableJavaScripttorunthisapp.。") is None
    assert "站点壳" in nav_dump_reason("register/login YouneedtoenableJavaScripttorunthisapp")


def test_usgs_filters_small_quakes_and_translates_place():
    """USGS 旧版 17 条远洋小震刷屏、英文地名被去空格后粘连、分类错挂公共卫生。"""
    from newsbrief.sources.geojson import cn_place, is_major
    assert not is_major(4.9, 598.0) and not is_major(5.4, 381.0)
    assert is_major(6.5, 598.0) and is_major(5.5, 10.0)
    assert "麦夸里岛" in cn_place("west of Macquarie Island")
    assert "181千米" in cn_place("181 km NE of Lospalos, Timor Leste")
    assert "境外海域" == cn_place("Some Unknown Faraway Place region")
    ch = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    g22 = next(g for g in ch["groups"] if g["id"] == 22)
    assert all(e["type"] == "geojson" for e in g22["endpoints"]), \
        "GDACS 首页 JS 壳不得再作为 html 源"


def test_traditional_chinese_is_simplified_in_live_only():
    """大公文汇繁体稿不得与简体主表混排；离线回放 abstract 已存在，不受影响。"""
    from newsbrief.clean import simplify, simplify_raw
    s = simplify("選委會選舉｜李慧琼：落實「愛國者治港」重要實踐 彰顯新選制優越性")
    assert "選" not in s and "舉" not in s and "显" in s
    raw = [{"title": "點新聞報道", "body": "順利舉行。"}]
    simplify_raw(raw)
    assert raw[0]["title"] == "点新闻报道" and "顺利举行" in raw[0]["body"]


def test_html_collector_rejects_site_shell_but_keeps_policy_notices():
    """只拒站点功能文本；清单 §2/§3 的政策「通知 / 公告 / 公示」属于要收的原文。"""
    from newsbrief.sources.html import _FUNC, _date_ok
    from newsbrief.model import nav_dump_reason
    for junk in ("PDF阅读软件下载", "无障碍阅读模式", "友情链接", "京ICP备05070218号",
                 "登录", "网站地图"):
        assert _FUNC.search(junk) or nav_dump_reason(junk), junk
    for keep in ("国务院办公厅关于2027年部分节假日安排的通知",
                 "市场监管总局关于12批次食品不合格情况的通告",
                 "关于召开2026年第三季度业绩说明会的公告"):
        assert not _FUNC.search(keep), f"政策原文被误杀：{keep}"
    assert not _date_ok("2030-01-01") and not _date_ok("2020-01-01"), \
        "远期未来/陈旧旧稿必须在采集层就拒掉"


def test_registry_dispatches_jsonp_type(monkeypatch, tmp_path):
    """新增采集器必须真的被注册表调度，不能只躺在目录里。"""
    import httpx
    from newsbrief import registry
    monkeypatch.setattr(registry, "CACHE_PATH", tmp_path / "cache.json")
    payload = ('news({"data":{"total":1,"list":[{"title":"测试要闻标题完整句",'
               '"brief":"第一句事实陈述完整。第二句事实补充完整。",'
               '"focus_date":"2026-11-23 16:36:24","url":"https://x/a.shtml"}]}})')
    def handler(req):
        return httpx.Response(200, text=payload)
    ch = {"groups": [{"id": 90, "name": "测试", "endpoints": [
        {"name": "测试CCTV", "url": "https://x/n_1.jsonp", "type": "jsonp", "pages": 1}]}]}
    cfg = {"network": {"timeout_seconds": 6, "html_articles_per_endpoint": 2}}
    reg = registry.discover()
    assert "jsonp" in reg
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        raw, reports, failed = registry.collect(cfg, ch, client=c)
    assert len(raw) == 1 and not failed and reports[0]["_got"] == 1


# ── 2026-11-23 第四轮：《资讯渠道清单》全量接入 ──

def test_channel_catalog_is_complete_and_probed():
    """691 渠道 / 503 去重 URL / 31 章全部结构化入包，逐 URL 带真发探测状态码。"""
    cat = json.loads((ROOT / "data" / "channel_catalog.json").read_text(encoding="utf-8"))
    assert cat["channels"] >= 691 and cat["unique_urls"] >= 503
    assert cat["sections"] == 31 and cat["source"] == "资讯渠道清单.md"
    assert len(cat["sha256"]) == 64
    assert cat["reachable_urls"] > 100 and cat["blocked_urls"] > 300
    # 每条 URL 都必须有探测结论，不能留空假装登记
    urls = [u for e in cat["entries"] for u in e["u"]]
    assert urls and all(isinstance(u, list) and len(u) == 2 and isinstance(u[1], int)
                        for u in urls), "每个入口都必须带真发探测状态码"
    # 清单章节与工程 31 类的映射必须全覆盖
    assert cat["group_coverage"] == list(range(1, 32))


def test_reachable_catalog_entries_are_wired_into_sources():
    """可达入口不能只躺在 catalog 里：必须按 31 类挂进活跃端点。"""
    ch = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    eps = [e for g in ch["groups"] for e in g["endpoints"]]
    wired = [e for e in eps if e.get("catalog_sec")]
    assert len(ch["groups"]) == 31
    assert len(wired) >= 60, f"清单接入端点太少：{len(wired)}"
    assert len(eps) >= 130, f"台账端点退化：{len(eps)}"
    assert all(e.get("level") in ("★", "☆", "✗") for e in wired)
    assert all(e.get("catalog_board") for e in wired)
    # 停更死 feed 与 JS 壳源不得复活
    assert not any("people.com.cn/rss" in e.get("url", "") for e in eps)
    assert not any("gdacs.org" in e.get("url", "") for e in eps)


def test_registry_reports_catalog_coverage(monkeypatch, tmp_path):
    """逐类申报必须写明清单登记数 / 可达数 / 已接入端点数。"""
    import httpx
    from newsbrief import registry
    monkeypatch.setattr(registry, "CACHE_PATH", tmp_path / "cache.json")
    cfg = {"network": {"timeout_seconds": 6, "html_articles_per_endpoint": 2}}
    ch = {"groups": [{"id": 90, "name": "测试", "endpoints": []}]}
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=""))) as c:
        _raw, reports, _failed = registry.collect(cfg, ch, client=c)
    r = reports[0]
    assert all(k in r for k in ("清单登记渠道", "清单可达入口", "已接入活跃端点"))
    assert "清单口径" in r["缺口"] and "《资讯渠道清单》" in r["缺口"]


def test_html_collector_extracts_list_page_without_optional_dependency(monkeypatch, tmp_path):
    """栏目页自带日期+描述时，即使没装 trafilatura 也能出独立原文条目。"""
    import httpx
    from newsbrief.sources import html as H
    monkeypatch.setattr(H, "trafilatura", None)
    listing = (
        '<html><body><ul>'
        '<li><a href="/2026/11/23/a.shtml">三部门发布儿童药品清单 满足用药需求</a>'
        '<span>2026-11-23 16:36</span>'
        '<p>为深入贯彻全国卫生与健康大会精神，鼓励研发创制儿童适用品种。'
        '该清单已对外公布，各地按要求组织落实。</p></li>'
        '<li><a href="/about.html">关于我们</a></li>'
        '</ul></body></html>')
    def handler(req):
        return httpx.Response(200, text=listing)
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        rows = H.fetch(c, {"name": "测试部委", "url": "https://x/list.html",
                           "max_articles": 4, "endpoint_seconds": 8}, 6)
    assert len(rows) == 1, rows
    assert rows[0]["published"].startswith("2026-11-23")
    assert "鼓励研发创制" in rows[0]["body"]
    assert rows[0]["source"] == "测试部委"


# ── 2026-11-23 第六轮：GitHub 网页上传「can't process that file」防护 ──

def test_all_shipped_filenames_are_ascii():
    """交付清单里的文件名一律 ASCII：中文名会触发网页上传的兜底报错。

    语义边界（截图 33）：这里验的是「本工程声明要交付的文件」，即
    manifest.required + deliverables，而不是 CI checkout 工作区里碰巧有哪些
    文件。仓库历史里可能残留旧版 交付说明.md，那是部署状态问题，由
    workflow 的「清理旧版残留文件」步与 deploy 的 verify_head_state 兜底，
    不该让「交付物命名」这条断言背锅。
    """
    m = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    declared = list(m["required"]) + list(m["deliverables"])
    bad = [x for x in declared if not x.isascii()]
    assert not bad, f"交付清单仍有非 ASCII 文件名：{bad}"
    assert "交付说明.md" not in declared, "manifest 不得再声明旧中文名"
    assert "DELIVERY_NOTES.md" in m["required"]
    assert (ROOT / "DELIVERY_NOTES.md").is_file()

    # 不放宽：若工作区里真还留着旧中文名，必须同时具备 CI 清理步，
    # 否则明确要求重新部署（保持「仓库状态不对」的信号，不静默吞掉）。
    if (ROOT / "交付说明.md").exists():
        wf = (ROOT / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")
        assert "清理旧版残留文件" in wf, (
            "工作区残留 交付说明.md 且 workflow 无清理步：请用最新包重新运行 deploy.bat")


def test_head_state_guard_and_workflow_cleanup_exist():
    """截图 33 回归：deploy 必须能挡住「旧 workflow + 旧中文名」被当成成功。"""
    import deploy
    assert hasattr(deploy, "verify_head_state"), "deploy 缺少 HEAD 状态强校验"
    dep = (ROOT / "deploy.py").read_text(encoding="utf-8")
    assert "HEAD 的 daily.yml 不是 build/deploy 双 job 版" in dep
    assert "清理旧版残留文件" in (ROOT / ".github" / "workflows" / "daily.yml").read_text(
        encoding="utf-8")
    m = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert ".github/workflows/daily.yml" in m["required"], (
        "workflow 不在推送清单里，仓库会一直停在旧版单 job")


def test_readme_first_cards_web_upload_trap():
    """包根排障卡必须存在且写清：别拖 zip、用原始域名、双击 deploy.bat。"""
    t = (ROOT / "README_FIRST.txt").read_text(encoding="utf-8")
    assert all(k in t for k in ("DO NOT upload the .zip", "deploy.bat", "github.com",
                               "GitHub Actions", "Unzip"))
    m = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert "README_FIRST.txt" in m["required"]
    assert "DELIVERY_NOTES.md" in m["required"]


def test_deploy_bat_guards_unzipped_dir():
    """bat 防护与语义：拒绝未解压目录、纯 ASCII、零 &&、换行不混用。

    截图 34 检讨：这里**不能**断言 checkout 工作区一定是 CRLF。
    actions/checkout 在 Linux runner 上会做文本规范化（core.autocrlf=input），
    把 bat 变成纯 LF 是完全正常的 Git 行为；我上一版把「打包时的字节」
    当成「runner 的字节」，本地 bare→clone 恰好没规范化就假绿了。
    换行是否 CRLF 由 .gitattributes 与 workflow 规范化步保证，见下一条测试。
    """
    b = (ROOT / "deploy.bat").read_bytes()
    assert b"manifest.json not found here" in b
    assert b"if not exist manifest.json goto :notunzipped" in b
    crlf, lf = b.count(b"\r\n"), b.count(b"\n") - b.count(b"\r\n")
    assert crlf == 0 or lf == 0, f"CRLF 与 LF 混用：CRLF={crlf} 裸LF={lf}"
    assert b"\r" not in b.replace(b"\r\n", b""), "存在孤立 CR"
    assert b.count(b"\n") >= 20, f"行数异常：{b.count(b'\n')}"
    assert all(x < 128 for x in b) and b"&&" not in b


def test_bat_crlf_is_guaranteed_by_attributes_and_workflow():
    """截图 34 回归：CRLF 必须由机制保证，而不是靠 CI 恰好没规范化。"""
    ga = ROOT / ".gitattributes"
    assert ga.is_file(), "缺 .gitattributes，Git 换行会随平台漂移"
    assert "*.bat text eol=crlf" in ga.read_text(encoding="utf-8")
    m = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert ".gitattributes" in m["required"], "声明了但不会推上仓库，等于没写"

    wf = (ROOT / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")
    assert "规范化 deploy.bat 换行" in wf, "CI 缺少字节级自愈步"
    dep = (ROOT / "deploy.py").read_text(encoding="utf-8")
    assert "def normalize_deploy_bat" in dep, "deploy 推送前缺少字节级规范化"

    # 交付物本身仍严格 CRLF（zip / 本地包），这一条不放宽
    b = (ROOT / "deploy.bat").read_bytes()
    assert b.count(b"\r\n") >= 20 and b.count(b"\n") == b.count(b"\r\n"), (
        "交付包里的 deploy.bat 必须是 CRLF")


def test_deploy_hint_only_warns_about_zip():
    """目录里有 zip 只打印提示，绝不影响 preflight 的判定结果。"""
    import deploy
    m = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    files = deploy.push_files(m)
    assert deploy.preflight(m, files) == []
    assert hasattr(deploy, "hint_if_not_unzipped")


def test_page_has_window_setting_and_matches_current_batch():
    """用户要的「时间窗口设置」必须是页面上能填、能应用、能重跑的控件，
    不是口径表里一行文字；页面数据还必须是本批成品，不能挂历史 js。
    """
    if _is_checkout():
        pytest.skip("CI checkout 零网络自检：页面由构建步骤生成")
    import json
    import re
    from openpyxl import load_workbook
    docs = ROOT / "docs"
    raw = (docs / "news-data.js").read_text(encoding="utf-8")
    d = json.loads(raw[raw.index("=") + 1:].rstrip().rstrip(";"))
    h = (docs / "index.html").read_text(encoding="utf-8")
    assert all(k in h for k in ('id="ws"', 'id="we"', 'id="apply"', 'id="rerun"',
                                'type="datetime-local"', "inWin", "左开右闭"))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", d["window_start"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", d["window_end"])
    assert d["window_start"] < d["window_end"]
    tm = d["time_meta"]["main"]
    assert len(tm) == len(d["main"]) and all(x["pub"] and x["basis"] for x in tm)
    wb = load_workbook(_cur_xlsx(), data_only=True)
    xrows = [list(r) for r in wb["处理后的数据"].iter_rows(min_row=2, values_only=True)]
    wb.close()
    assert d["main"] == xrows, "页面主表与成品主表不一致（线上挂了旧批次）"
    # workflow 版本差异不由本测试判红：CI 可能按旧定义触发，checkout 到的
    # daily.yml 没有 inputs 字面量。契约交给 test_production_live 的稳健检查，
    # 推送时由 deploy.verify_head_state() 强校验。
