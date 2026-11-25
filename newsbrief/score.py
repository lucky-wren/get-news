"""评分（文档 §6.4）：Final = min(100, 30 + 6×I + 2×C + V + impact)；Final_adj = Final − 4.2。

等级按原始 Final 机械映射；impact 只进 Final 与 head_score，不改 I/C/V/gin/cap。
离线回放直接用基准台账的数值（逐条可复算），live 用同一套公式现算。
"""
from __future__ import annotations

import re

from .model import Item


def grade_of(final: float) -> str:
    return "A" if final >= 70 else "B" if final >= 60 else "C" if final >= 50 \
        else "D" if final >= 40 else "E"


def impact_of(it: Item) -> int:
    """置顶池 / 多渠道高覆盖池 → impact；各 0–3 的四维合成上限 12。"""
    t = it.title + it.abstract
    m = re.search(r"(\d+(?:\.\d+)?)\s*级地震", t)
    if m and float(m[1]) >= 7.0:
        return 12
    if re.search(r"死亡\s*\d+\s*人|伤亡\s*\d+\s*人|国家级应急响应|红/?橙预警", t):
        return 12
    n = len(it.sources.split(" · "))
    if n >= 5:
        return 8
    if n >= 3:
        return 6
    return 4


def score(it: Item, *, I: int = 5, C: int = 6, V: float = 10.0,
          cross: int = 0, gin: int = 1, cap: int = 4, impact: int | None = None) -> Item:
    imp = impact_of(it) if impact is None else impact
    final = min(100, 30 + 6 * I + 2 * C + V + imp)
    it.final = final
    it.final_adj = round(final - 4.2, 1)
    it.grade = grade_of(final)
    it.I, it.C, it.V, it.cross, it.gin, it.cap = I, C, V, cross, gin, cap
    return it


def cross_score(it: Item) -> int:
    """独立信源渠道数 − 1，×3，上限 12；只进编排，不冒充实测热度。"""
    return min(12, (len(it.sources.split(" · ")) - 1) * 3)


def apply_baseline(it: Item, sc: dict, tr: dict) -> Item:
    """离线回放：把基准台账的评分与评级留痕逐条挂到 Item 上。"""
    it.seq = int(sc["seq"])
    it.final = float(sc["final"])
    it.final_adj = float(sc["final_adj"])
    it.grade = str(sc["grade"])
    it.score_order = int(sc["score_order"])
    it.event_key = str(sc["event_key"])
    for k, key in (("gin", "gin"), ("cap", "cap"), ("V", "V"), ("I", "I"),
                   ("C", "C"), ("cross", "cross")):
        setattr(it, k, tr.get(key, getattr(it, k)))
    return it
