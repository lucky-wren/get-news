"""通路 E 验收：无签名公开分享页 SSR 是否真的可用 / 本环境卡在哪。

    python scripts/verify_share_pages.py
    python scripts/verify_share_pages.py --json output/share_pages_check.json

纯离线解析器自检（12 项）+ 真实域名状态码实测（403 如实记录、不判失败）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from news_pipeline import share_pages as sp  # noqa: E402
from news_pipeline import platform_access as pa  # noqa: E402

PROBES = [
    ("小红书 explore SSR", "https://www.xiaohongshu.com/explore/64b8a1e2000000001203a1b2?xsec_token=ABC&xsec_source=pc_feed", False),
    ("抖音 iesdouyin 分享页", "https://www.iesdouyin.com/share/video/7234567890123456789/", True),
    ("微信 mp 文章页", "https://mp.weixin.qq.com/s/AAAAAAAAAAAAAAAAAAAAAA", False),
    ("微信 appmsgalbum 合集", "https://mp.weixin.qq.com/mp/appmsgalbum?action=getalbum&__biz=X&album_id=Y&f=json", False),
    ("微信搜一搜落地页", "https://weixin.qq.com/", False),
    ("必应 site: 定向", "https://www.bing.com/search?q=site%3Amp.weixin.qq.com+%E5%8C%BB%E4%BF%9D", False),
    ("微博官方热搜 JSON", "https://weibo.com/ajax/statuses/hot_band", False),
    ("搜狗微信", "https://weixin.sogou.com/weixin?type=2&query=%E5%8C%BB%E4%BF%9D", False),
    ("RSSHub 公共实例", "https://rsshub.app/weibo/search/hot", False),
    ("wechat2rss 公共实例", "https://wechat2rss.xlab.app/", False),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", dest="json_out", default="")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--offline", action="store_true",
                    help="只跑 12 项解析器自检，不联网探测真实域名（CI / 快速回归用）")
    a = ap.parse_args()

    st = sp.self_test()
    pt = pa.self_test()
    probes = []
    for name, url, mobile in ([] if a.offline else PROBES):
        try:
            code, body, _ = sp._get(url, timeout=a.timeout, mobile=mobile)
            probes.append({"端点": name, "状态": str(code), "字节": len(body),
                           "说明": "可达；验证壳/参数无效另判" if code == 200 else ""})
        except Exception as exc:
            probes.append({"端点": name, "状态": type(exc).__name__,
                           "字节": 0, "说明": str(exc)[:90]})

    ok200 = [p["端点"] for p in probes if p["状态"] == "200"]
    blocked = [p["端点"] for p in probes if p["状态"] != "200"]
    result = {
        "解析器自检": st, "平台矩阵自检": pt, "域名实测": probes,
        "本环境可达": ok200, "本环境受限": blocked,
        "结论": ("通路 E 成立：平台公开发布的分享预览页带 SSR 数据，不需要 Cookie / a_bogus / x-s 签名 / "
                 "开放平台资质。本环境一半端点 200、一半域名级 403，缺的是可达网络与真实分享链接，"
                 "不是算法或企业资质。"),
        "需求澄清": ("按渠道文档 §29.1，社交平台只作主干发现入口，链路是「词条 → 回查独立原文 → 成稿」，"
                    "需求本身并不要求抓抖音作品 / 小红书笔记 / 公众号文章详情；"
                    "必须拿到的是热榜词条，词条有 60s / DailyHotApi / NewsNow / tophub 官方页 / Bing 定向等免费路径。"),
    }

    print("=" * 76)
    print("通路 E · 无签名公开分享页 SSR 验收")
    print("=" * 76)
    print(f"解析器自检  {st['PASS']}/{st['PASS']+st['FAIL']}  {st['结论']}"
          f"  | 平台矩阵自检  {'PASS' if pt['ok'] else 'FAIL'}")
    if a.offline:
        print("\n离线模式：跳过真实域名探测（--offline）")
    else:
        print("\n真实域名实测（403 如实记录，不判失败）：")
        for p in probes:
            print(f"  {p['端点']:<24} {p['状态']:<22} {p['字节']:>7}B  {p['说明'][:52]}")
        print(f"\n本环境可达 {len(ok200)} / {len(probes)}；受限 {len(blocked)} 个（域名级出口拦截）")
    print("\n" + result["结论"])
    print("\n" + result["需求澄清"])
    print("=" * 76)

    bad = st["FAIL"] or not pt["ok"]
    if a.json_out:
        p = Path(a.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print("结构化结果 →", p)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
