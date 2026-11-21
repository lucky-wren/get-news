"""渠道白名单（资讯渠道大全 §1–§31）与逐渠道状态申报（§35）。

三层能力中的第 1 层与第 3 层：
  白名单从渠道文档 Markdown 表格自动解析（不手写、不凭关键词现想）；
  采集后逐渠道申报「命中 / 形态 / 窗口内条数 / 能否枚举全量 / 缺口原因」，
  未触达一律写实原因，绝不编造条目凑数（§36.18）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .model import ChannelStatus

LEVELS = ("★", "☆", "✗")
_HEAD_RE = re.compile(r"^##\s+(\d+)\.\s+(.*)$")
_HEAD_CN_RE = re.compile(r"^##\s+([一二三四五六七八九十百]+)、\s*(.*)$")
_SUB_RE = re.compile(r"^###\s+(\d+(?:\.\d+)*)\s+(.*)$")
_TABLE_SPLIT = re.compile(r"^\|.*\|\s*$")
_SEP_ROW = re.compile(r"^\|[\s:\-|]+\|\s*$")
_CN_DIGIT = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
             "八": 8, "九": 9, "十": 10}


def _cn2int(s: str) -> int:
    """中文数字章节号转整数：一→1、十→10、十四→14、二十→20。"""
    if "十" in s:
        left, _, right = s.partition("十")
        return (_CN_DIGIT.get(left, 1) if left else 1) * 10 + (_CN_DIGIT.get(right, 0) if right else 0)
    return _CN_DIGIT.get(s, 99)


def parse_channel_doc(path: str | Path) -> List[dict]:
    """解析渠道表格行，兼容两种文档格式：

    - 旧版 31 类表：`## 1. 央媒…` / `### 0.1 …`
    - v3 全行业分类版：`## 一、央媒…` / `### 2.1 权威与产业研究`

    级别列允许带后缀（如 `✗ 仅参考热度`、`✗ 不采`），取首字符。
    表头 / 分隔行 / 统计表（末列非级别）一律不计。
    """
    text = Path(path).read_text(encoding="utf-8")
    out: List[dict] = []
    section, section_no, section_name, idx = "", 0, "", 0
    parent_name = ""
    for line in text.splitlines():
        stripped = line.strip()
        m = _HEAD_RE.match(stripped)
        mcn = _HEAD_CN_RE.match(stripped)
        msub = _SUB_RE.match(stripped)
        if m:
            section_no, parent_name = int(m.group(1)), m.group(2).strip()
            section, section_name, idx = str(section_no), parent_name, 0
            continue
        if mcn:
            section_no = _cn2int(mcn.group(1))
            parent_name = mcn.group(2).strip()
            section, section_name, idx = str(section_no), parent_name, 0
            continue
        if msub:
            # 子节不重置 idx，保证 channel_id 在同一章内唯一
            section_name = f"{parent_name} / {msub.group(2).strip()}"
            continue
        if not section or not _TABLE_SPLIT.match(stripped):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 3 or _SEP_ROW.match(stripped):
            continue
        raw_level = cells[-1].strip()
        level = raw_level[0] if raw_level else ""
        if level not in LEVELS:
            continue
        if cells[0] in ("渠道", "平台", "分类", "时点", "用途", "类别"):
            continue
        idx += 1
        out.append({
            "channel_id": f"S{section_no:02d}-{idx:03d}",
            "section": f"§{section_no} {parent_name}",
            "section_no": section_no,
            "subsection": section_name,
            "channel": cells[0],
            "entry": cells[1] if len(cells) >= 3 else "",
            "coverage": cells[2] if len(cells) >= 4 else "",
            "level": level,
        })
    return out


class ChannelRegistry:
    def __init__(self, doc_path: str | Path, extra_path: str | Path | None = None):
        self.channels: List[dict] = parse_channel_doc(doc_path)
        self.extra: List[dict] = []
        if extra_path and Path(extra_path).exists():
            data = yaml.safe_load(Path(extra_path).read_text(encoding="utf-8")) or {}
            for c in data.get("channels", []):
                c.setdefault("level", "☆")
                c.setdefault("section", "扩充渠道")
                c.setdefault("section_no", 99)
                c.setdefault("channel_id", f"X-{len(self.extra)+1:03d}")
                c.setdefault("coverage", "")
                c.setdefault("entry", "")
                c["extra"] = True
                self.extra.append(c)
        by_name = {c["channel"]: c for c in self.channels}
        for c in self.extra:
            if c["channel"] in by_name:
                by_name[c["channel"]].update({k: v for k, v in c.items() if v})
            else:
                self.channels.append(c)   # 新增扩充渠道同样进白名单与申报表
        self.status: Dict[str, ChannelStatus] = {}
        for c in self.channels:
            has_endpoint = any(c.get(k) for k in
                               ("rss_url", "api", "list_pages", "hotboard", "search"))
            self.status[c["channel_id"]] = ChannelStatus(
                channel=c["channel"], channel_id=c["channel_id"],
                section=c["section"], level=c["level"],
                fetch_method=self._default_method(c),
                hit="否", shape="", window_count=0,
                full_enumeration="否",
                gap=("已登记端点但离线 / demo 模式未发起请求；live 模式启用该采集器"
                     if has_endpoint else
                     "未配置可拉取端点：白名单登记，本轮未触达（不编造条目）"),
                adapter="endpoint-registered" if has_endpoint else "white-list-only",
            )

    @staticmethod
    def _default_method(c: dict) -> str:
        if c.get("hotboard"):
            return "四时点榜单快照"
        if c.get("api"):
            return "官方 API / 结构化数据"
        if c.get("rss_url"):
            return "RSS / 时间倒排"
        if c.get("list_pages"):
            return "站内时间倒排 / 公告列表"
        return "站内时间倒排 + RSS（待配置端点）"

    def with_endpoint(self) -> List[dict]:
        return [c for c in self.channels if any(
            c.get(k) for k in ("rss_url", "api", "list_pages", "hotboard", "search"))]

    def mark(self, channel_id: str, *, hit: bool, shape: str, count: int,
             full_enumeration: str, adapter: str, gap: str = "") -> None:
        st = self.status.get(channel_id)
        if not st:
            st = ChannelStatus(channel=channel_id, channel_id=channel_id,
                               section="扩充渠道", level="☆", adapter=adapter)
            self.status[channel_id] = st
        st.hit = "是" if hit else "否"
        st.shape = shape
        st.window_count = count
        st.full_enumeration = full_enumeration
        st.adapter = adapter
        st.fetch_method = self._default_method(
            next((c for c in self.channels if c["channel_id"] == channel_id), {}))
        st.gap = gap or ("—" if hit else "请求失败 / 无 key / 被出口策略拦截；如实申报未取得")

    def table(self) -> List[List]:
        return [st.row() for st in self.status.values()]

    def summary(self) -> dict:
        rows = list(self.status.values())
        full = sum(1 for s in rows if s.full_enumeration == "是")
        snapshot = sum(1 for s in rows if s.full_enumeration in ("快照制", "否") and s.hit == "是")
        hit = sum(1 for s in rows if s.hit == "是")
        return {
            "白名单渠道总数": len(rows),
            "已触达渠道数": hit,
            "能按窗口枚举全量的渠道数": full,
            "仅快照 / 相关性 top-N 的渠道数": snapshot,
            "未触达渠道数": len(rows) - hit,
            "★必采": sum(1 for s in rows if s.level == "★"),
            "☆补充": sum(1 for s in rows if s.level == "☆"),
            "✗热度参考需回查": sum(1 for s in rows if s.level == "✗"),
        }
