"""采集窗口与发布时间口径。

窗口契约（用户 2026-11-20 拍板覆盖规格文档中的端点开闭表述）：
  (start, end)  左开右开，Asia/Shanghai，默认 (前一日 20:00, 当日 20:00)。
任意窗口可用 --start/--end 或 --window "2026-11-16 20:00~2026-11-17 08:00" 指定，
窗口只影响「采集条目是否入候选池」，不影响后续评级公式。

端点开闭可用 --interval 指定四种组合（规格文档写左开右闭，以用户拍板为准）：
  lo-ro 左开右开 (a,b)（默认）  / lo-rc 左开右闭 (a,b]
  lc-ro 左闭右开 [a,b)          / lc-rc 左闭右闭 [a,b]
开闭只作用于窗口硬过滤，不改变 T 的基准时刻（取窗口中点，SKILL §4.2/§4.5）。

时间字段优先级（渠道文档 §0.4）：
  pubDate/datePublished > 正文标准新闻时间戳 > 电头 > 站点上线时刻 > 相对时间 > 未取得。
标题里的月日只是日期线索，绝不充当发布时间。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

TZ_CN = ZoneInfo("Asia/Shanghai")

_DATETIME_PATTERNS = [
    r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日]?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?",
    r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日]?\s+(\d{1,2})[时点]",
    r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})(?::(\d{2}))?",
    r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2})[时点]",
    r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日]?",
    r"(\d{1,2})月(\d{1,2})日",
]
_ELECTRIC = re.compile(r"(?:新华社|中新社|本报|央视|电讯)?\s*(\d{1,2})月(\d{1,2})日电")
_RELATIVE = re.compile(r"(\d+)\s*(?:hours?|小时|minutes?|分钟|days?|天)\s*(?:ago|前)", re.I)
_DATE_CLUE = re.compile(r"(?:\d{4}[-/年])?\d{1,2}[-/月]\d{1,2}[日]?|\d{4}-\d{2}-\d{2}")


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ_CN)
    return dt.astimezone(TZ_CN)


def parse_datetime_text(value: str, hint_year: int | None = None) -> Tuple[Optional[datetime], str]:
    """把任意时间字面值解析为 aware datetime，返回 (时间, 依据)。无法解析返回 (None, 依据)。"""
    if value is None:
        return None, "未取得"
    s = str(value).strip()
    if not s or s in ("未取得", "未知", "None", "null", "-"):
        return None, "未取得"

    # ISO 8601 / RFC3339
    try:
        iso = s.replace("Z", "+00:00")
        return _aware(datetime.fromisoformat(iso)), "字段 pubDate/datePublished（ISO）"
    except (ValueError, TypeError):
        pass
    # RSS RFC822
    try:
        return _aware(parsedate_to_datetime(s)), "字段 pubDate（RFC822）"
    except (ValueError, TypeError, IndexError):
        pass

    year = hint_year or datetime.now(TZ_CN).year
    for i, pat in enumerate(_DATETIME_PATTERNS):
        m = re.search(pat, s)
        if not m:
            continue
        g = m.groups()
        try:
            if i in (0, 1, 4):      # 含 4 位年份的完整日期
                y, mo, d = int(g[0]), int(g[1]), int(g[2])
                hh = int(g[3]) if len(g) > 3 and g[3] else 0
                mm = int(g[4]) if len(g) > 4 and g[4] else 0
                ss = int(g[5]) if len(g) > 5 and g[5] else 0
                basis = "正文标准新闻时间戳" if i < 2 else "站点上线时刻（时点推定）"
            else:                   # 「11月17日 14:20」按窗口年份补全
                y = year
                mo, d = int(g[0]), int(g[1])
                hh = int(g[2]) if len(g) > 2 and g[2] else 12
                mm = int(g[3]) if len(g) > 3 and g[3] else 0
                ss = int(g[4]) if len(g) > 4 and g[4] else 0
                basis = "正文标准新闻时间戳" if i < 4 else "站点上线时刻（时点推定）"
            return _aware(datetime(y, mo, d, hh, mm, ss)), basis
        except (ValueError, TypeError):
            continue

    m = _ELECTRIC.search(s)
    if m:
        try:
            dt = _aware(datetime(year, int(m.group(1)), int(m.group(2)), 12, 0))
            return dt, "电头（新华社11月17日电，时刻推定）"
        except ValueError:
            pass
    if _RELATIVE.search(s):
        # 相对时间只登记依据，不换算成 ISO 写进发布时间（SKILL §11.1）
        return None, f"近一天收录仅保证抓取窗口（相对时间：{s}）"
    # 开源库兜底：dateparser + cn2an 只补「绝对时间」；相对时间串与缺年份表达
    # 一律保持「未取得」，不擅自补年、不换算（01 第一节 / 02 §11.1 硬约束）。
    from . import oss
    dt, basis = oss.loose_datetime(s, hint_year=year,
                                   base=datetime.now(TZ_CN), tz=TZ_CN)
    if dt is not None:
        return dt, basis
    return None, "未取得（无法解析的时间字面值）"


def extract_date_clues(text: str) -> str:
    hits = _DATE_CLUE.findall(text or "")
    seen, out = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return " / ".join(out[:6])


INTERVALS = {
    "lo-rc": (True, False, "左开右闭"),
    "lo-ro": (True, True, "左开右开"),
    "lc-rc": (False, False, "左闭右闭"),
    "lc-ro": (False, True, "左闭右开"),
}


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    tz: str = "Asia/Shanghai"
    interval: str = "lo-ro"

    @property
    def right_date(self) -> datetime:
        return self.end

    @property
    def left_open(self) -> bool:
        return INTERVALS[self.interval][0]

    @property
    def right_open(self) -> bool:
        return INTERVALS[self.interval][1]

    @property
    def interval_name(self) -> str:
        return INTERVALS[self.interval][2]

    @property
    def label(self) -> str:
        fmt = "%Y-%m-%d %H:%M"
        l, r = ("(", "[")[not self.left_open], (")", "]")[not self.right_open]
        return f"{l}{self.start.strftime(fmt)}, {self.end.strftime(fmt)}{r}  {self.tz}"

    @property
    def midpoint(self) -> datetime:
        return self.start + (self.end - self.start) / 2

    @property
    def span_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600

    @property
    def default_filename(self) -> str:
        return f"{self.end.strftime('%Y_%m_%d')}_窗口期.xlsx"

    def contains(self, dt: datetime) -> bool:
        d = _aware(dt)
        left = self.start < d if self.left_open else self.start <= d
        right = d < self.end if self.right_open else d <= self.end
        return left and right

    def boundary_reason(self, dt: datetime) -> str:
        """逐条写明端点判定依据，用于窗口外排除留痕。"""
        d = _aware(dt)
        fmt = "%Y-%m-%d %H:%M"
        if self.left_open and d <= self.start:
            return f"超左界：发布时间 {d.strftime(fmt)} ≤ 窗口左界 {self.start.strftime(fmt)}（左开，不含）"
        if not self.left_open and d < self.start:
            return f"超左界：发布时间 {d.strftime(fmt)} < 窗口左界 {self.start.strftime(fmt)}"
        if self.right_open and d >= self.end:
            return f"超右界：发布时间 {d.strftime(fmt)} ≥ 窗口右界 {self.end.strftime(fmt)}（右开，不含）"
        if not self.right_open and d > self.end:
            return f"超右界：发布时间 {d.strftime(fmt)} > 窗口右界 {self.end.strftime(fmt)}"
        return "窗口内实证"

    def as_dict(self) -> dict:
        return {
            "start": self.start.isoformat(), "end": self.end.isoformat(),
            "label": self.label, "span_hours": self.span_hours,
            "interval": self.interval, "interval_name": self.interval_name,
            "left_open": self.left_open, "right_open": self.right_open,
            "default_filename": self.default_filename,
        }


def _parse_one(s: str) -> datetime:
    s = (s or "").strip()
    dt, _ = parse_datetime_text(s)
    if dt is None:
        raise ValueError(f"无法解析时间：{s!r}（示例：2026-11-16 20:00）")
    return dt


def build_window(*, start: str | None = None, end: str | None = None,
                 date: str | None = None, window: str | None = None,
                 hours: float | None = None, tz: str = "Asia/Shanghai",
                 interval: str = "lo-ro") -> Window:
    if interval not in INTERVALS:
        raise ValueError(f"--interval 应为 {list(INTERVALS)} 之一，收到 {interval!r}")
    """四种入口互相兼容，优先级：window > start/end > date。"""
    global TZ_CN
    TZ_CN = ZoneInfo(tz)
    tzinfo = ZoneInfo(tz)

    if window:
        parts = re.split(r"\s*(?:~|～|--|至|→|->|/)\s*", window.strip(), maxsplit=1)
        if len(parts) != 2:
            raise ValueError("--window 格式应为 '2026-11-16 20:00~2026-11-17 08:00'")
        st, en = _parse_one(parts[0]), _parse_one(parts[1])
    elif start and end:
        st, en = _parse_one(start), _parse_one(end)
    else:
        if date:
            d, _ = parse_datetime_text(date)
            if d is None:
                raise ValueError(f"无法解析 --date：{date!r}")
        else:
            d = datetime.now(tzinfo)
        en = _aware(datetime(d.year, d.month, d.day, 20, 0))
        if hours is not None:
            st = en - timedelta(hours=float(hours))
        else:
            st = en - timedelta(hours=24)

    if en <= st:
        raise ValueError(f"窗口右界必须晚于左界：{st} -> {en}")
    return Window(_aware(st), _aware(en), tz, interval)


def resolve_pub(item, win: Window) -> Tuple[Optional[datetime], str, str]:
    """回填发布时间 / 时间依据 / 时效标记；返回 (pub, basis, mark)。"""
    pub = item.pub
    basis = item.time_basis or ""
    mark = item.time_mark or ""

    if pub is None and item.pub_raw and item.pub_raw != "未取得":
        pub, auto_basis = parse_datetime_text(item.pub_raw, hint_year=win.end.year)
        if not basis:
            basis = auto_basis

    # 往年旧页：按原发布年份剔除留痕（渠道文档 §0.4 / §36.2）
    if pub is not None:
        pub = _aware(pub)
        if abs((pub - win.midpoint).days) > 180:
            item.reject_stage = "窗口过滤"
            item.reject_reason = f"往年旧页按原发布年份剔除（{pub.date()}）"
            item.admitted = False
            return pub, basis, "往期日期线索·需核对"
        if not mark:
            mark = "窗口内实证" if win.contains(pub) else "正文日期推定"

    if pub is None:
        if "相对时间" in basis or "近一天" in basis or "抓取窗口" in basis:
            mark = mark or "近一天推定"
        else:
            mark = mark or "当日速览推定"
        basis = basis or "近一天收录仅保证抓取窗口"
    if not item.date_clue:
        item.date_clue = extract_date_clues(item.text())
    return pub, basis, mark


def window_verdict(item, win: Window, *, allow_estimated: bool = True,
                   strict_window: bool = False) -> Tuple[bool, str]:
    """逐条窗口判定 + 超界留痕原因；端点开闭由 win.interval 决定。"""
    pub = item.pub
    if pub is None:
        if strict_window:
            return False, "strict_window=true 且发布时间未取得 → 不入池"
        # 2026-11-21 质量 v3：站内时间倒排源抓取的是列表页，发布时间未取得时
        # 旧版「当日速览推定」无条件放行，把 2026-03~10 的 CNCERT 旧漏洞公告、
        # 11-12 / 10-24 的政务旧稿全放进 (11-19 20:00, 11-20 20:00) 窗口。
        # 这类条目没有可核对的发布时间，按窗口是唯一硬过滤的口径拒绝；
        # 逐条进附录 4b 写明原因，不推测补写、不编造时间。
        if getattr(item, "record_type", "") == "站内时间倒排":
            return False, "站内时间倒排条目发布时间未取得 → 不入池（不做时点推定，见附录 4b）"
        if allow_estimated:
            # 2026-11-21 全面核查：旧版对任何 record_type 都无条件「当日速览推定」，
            # 2026-03~10 的旧公告、11-12 政务旧稿因此混进 11-20 窗口（audit 实测）。
            # 现在只接受两类可核对线索：① 源站明确给相对时间；② 日期线索落在窗口
            # 覆盖的自然日内。标题里的月日仍只是线索、不反推发布时间（见模块头注释）。
            basis_txt = str(getattr(item, "time_basis", "") or "")
            if any(k in basis_txt for k in ("相对时间", "近一天", "抓取窗口", "小时前", "分钟前")):
                return True, "时点推定入池（源站给相对时间，见附录 4b）"
            clue = str(getattr(item, "date_clue", "") or "")
            m = re.search(r"(?:(\d{4})[-/年])?(\d{1,2})[-/月](\d{1,2})", clue)
            if m:
                year = int(m.group(1)) if m.group(1) else win.start.year
                try:
                    d = datetime(year, int(m.group(2)), int(m.group(3)), tzinfo=TZ_CN).date()
                    if win.start.date() <= d <= win.end.date():
                        return True, "时点推定入池（日期线索落在窗口内，见附录 4b）"
                    return False, f"日期线索 {d.isoformat()} 不在窗口内 → 不入池（附录 4b）"
                except ValueError:
                    pass
            return False, "发布时间未取得且无可核对日期线索 → 不入池（附录 4b）"
        return False, "发布时间未取得且未开启时点推定"
    reason = win.boundary_reason(pub)
    return (True, "窗口内实证") if reason == "窗口内实证" else (False, reason)
