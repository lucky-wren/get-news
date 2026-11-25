"""结构化数据源（USGS 地震 GeoJSON）：只保留对大众有信息价值的重大浅源事件。

旧版把 4.5_day 的 17 条远洋/深源小震全部灌进候选池，标题是
`M 5.5 - west of Macquarie Island`、正文是 `美国地质调查局记录，
westofMacquarieIsland发生5.5级地震…2026-11-23T16:01:26.457000+08:00`
（英文地名被清洗层去掉空格后粘连、ISO 时间堆砌、分类错挂「灾害与公共卫生」）。
这里改为：中文地名 + 中文时间 + 震级/深度门槛 + 正确分类，按 01 §1
「仅三类重大突发」执行，不拿远洋小震凑渠道数。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import httpx

from newsbrief.registry import source

TZ = timezone(timedelta(hours=8))

# USGS place 常见地名 → 中文；方位与距离单位也一并归一，避免空格被吞后粘连。
CN = {
    "Macquarie Island": "麦夸里岛", "Timor Leste": "东帝汶", "Lospalos": "洛斯帕洛斯",
    "Papua New Guinea": "巴布亚新几内亚", "Kokopo": "科科波", "Vanuatu": "瓦努阿图",
    "Port-Olry": "波特奥尔里", "Bitung": "比通", "Tobelo": "托贝洛", "Kuqa": "库车",
    "China": "中国", "Indonesia": "印度尼西亚", "Chile": "智利", "Peru": "秘鲁",
    "Carhuamayo": "卡瓦马约", "Fiji Islands": "斐济群岛", "Isangel": "伊桑格尔",
    "Iran": "伊朗", "Sumatra": "苏门答腊", "Java": "爪哇", "Honshu": "本州",
    "Hokkaido": "北海道", "Taiwan": "台湾", "Philippines": "菲律宾",
    "Kuril Islands": "千岛群岛", "Solomon Islands": "所罗门群岛", "Tonga": "汤加",
    "New Zealand": "新西兰", "Alaska": "阿拉斯加", "California": "加利福尼亚",
    "Mexico": "墨西哥", "Guatemala": "危地马拉", "Colombia": "哥伦比亚",
    "Ecuador": "厄瓜多尔", "Brazil": "巴西", "Argentina": "阿根廷",
    "Turkey": "土耳其", "Greece": "希腊", "Italy": "意大利", "Japan": "日本",
    "Afghanistan": "阿富汗", "Pakistan": "巴基斯坦", "Nepal": "尼泊尔",
    "Myanmar": "缅甸", "Kashmir": "克什米尔", "Xinjiang": "新疆",
    "Sichuan": "四川", "Yunnan": "云南", "Tibet": "西藏", "Qinghai": "青海",
    "region": "地区", "south of the": "以南海域", "off the coast of": "近海",
    "north of the": "以北海域", "west of the": "以西海域", "east of the": "以东海域",
}
DIRS = {"NE": "东北方向", "NW": "西北方向", "SE": "东南方向", "SW": "西南方向",
        "N": "以北", "S": "以南", "E": "以东", "W": "以西"}


def cn_place(place: str) -> str:
    s = str(place or "")
    for en, zh in CN.items():
        s = re.sub(re.escape(en), zh, s, flags=re.I)
    s = re.sub(r"\b(\d+)\s*km\s*(NE|NW|SE|SW|N|S|E|W)\s*of\b",
               lambda m: f"{DIRS[m.group(2)]}{m.group(1)}千米", s, flags=re.I)
    s = re.sub(r"\b(NE|NW|SE|SW|N|S|E|W)\s*of\b",
               lambda m: DIRS[m.group(1)], s, flags=re.I)
    s = re.sub(r"\b(\d+)\s*km\b", r"\1千米", s, flags=re.I)
    s = re.sub(r"\bwest of\b", "以西", s, flags=re.I)
    s = re.sub(r"\beast of\b", "以东", s, flags=re.I)
    s = re.sub(r"\bnorth of\b", "以北", s, flags=re.I)
    s = re.sub(r"\bsouth of\b", "以南", s, flags=re.I)
    s = s.replace("  ", " ").strip(" ,")
    # 仍含大段拉丁字母 → 不硬译，用「境外海域/地区」兜底，绝不输出粘连英文串。
    if len(re.findall(r"[A-Za-z]{3,}", s)) >= 2:
        return "境外海域"
    return s or "境外地区"


def is_major(mag: float, depth_km: float) -> bool:
    """仅重大突发三类：≥6.0 级一律保留；5.5–5.9 且浅源（≤100km）保留；
    深源（>300km）小震与 <5.5 远洋小震不进候选池（避免刷屏、不冒充要闻）。
    """
    if mag >= 6.0:
        return True
    if mag >= 5.5 and depth_km <= 100:
        return True
    return False


@source("geojson")
def fetch(client: httpx.Client, ep: dict, timeout: float) -> list[dict]:
    r = client.get(ep["url"], timeout=timeout)
    r.raise_for_status()
    data = r.json()
    out = []
    for f in data.get("features", [])[:60]:
        p = f.get("properties", {})
        mag = float(p.get("mag") or 0)
        depth = float((f.get("geometry", {}).get("coordinates") or [0, 0, 0])[2] or 0)
        if not is_major(mag, depth):
            continue
        t = datetime.fromtimestamp((p.get("time") or 0) / 1000, TZ)
        place = cn_place(p.get("place", ""))
        title = f"{place}发生{mag:g}级地震"
        body = (f"美国地质调查局记录，北京时间{t:%m月%d日%H时%M分}，"
                f"{place}发生{mag:g}级地震，震源深度{depth:g}千米。"
                f"该记录未包含人员伤亡与财产损失信息，不作推断。")
        out.append({
            "title": title, "url": p.get("url", ""), "body": body,
            "published": t.isoformat(), "source": ep["name"],
            "group_id": ep.get("group_id"), "group": "国际动态",
            "tag": "灾害与应急", "shape": "结构化", "evidence": "A",
        })
    return out
