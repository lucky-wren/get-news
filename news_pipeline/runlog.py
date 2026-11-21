"""运行日志：一次运行一个 run_id，落盘 + jsonl 索引 + 阶段计时。

设计目标（排查问题时能回答"哪一步、多久、多少条、为什么失败"）：
  - 每次运行写 logs/<run_id>.<title>.log，纯文本、按时间序、带级别与 key=value；
  - logs/runs.jsonl 是运行索引，供页面「最近运行」列表与 /api/logs 读取；
  - checkpoint() 记录阶段耗时，metric() 记录各阶段条数，subprocess() 收录子进程全量输出；
  - error() 写完整 traceback，finish() 定稿状态并写索引；
  - 流水线单独跑（run.py）与服务调用（serve.py）用同一个 run_id 关联两份日志。

零第三方依赖；全局函数在没有 active RunLog 时静默，不影响作为库被调用。
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TZ_CN = ZoneInfo("Asia/Shanghai")
_LOCK = threading.Lock()
_CURRENT: "RunLog | None" = None
RUN_ID_RE = r"^\d{8}_\d{6}_[0-9a-f]{6}$"


def _now() -> str:
    return datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def new_run_id() -> str:
    return datetime.now(TZ_CN).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


def fmt_kv(kv: dict) -> str:
    out = []
    for k, v in kv.items():
        if v is None:
            v = "—"
        if isinstance(v, (dict, list, tuple)):
            v = json.dumps(v, ensure_ascii=False, default=str)
        out.append(f"{k}={v}")
    return "  ".join(out)


class RunLog:
    def __init__(self, log_dir: str | Path, *, run_id: str | None = None,
                 title: str = "pipeline", index: bool = True, echo: bool = True):
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or os.environ.get("NEWS_RUN_ID") or new_run_id()
        self.title = title
        self.path = self.dir / f"{self.run_id}.{title}.log"
        self.index_path = self.dir / "runs.jsonl"
        self.do_index = index
        self.echo = echo
        self.t0 = time.time()
        self._last = self.t0
        self.started = datetime.now(TZ_CN).isoformat(timespec="seconds")
        self.stages: list[dict] = []
        self.metrics: dict[str, Any] = {}
        self.status = "running"
        self.error_text = ""
        self.file = ""
        self.meta: dict[str, Any] = {}
        self._write(f"===== 运行日志 {self.run_id} / {title} =====")
        self.event("INFO", "启动", 时间=self.started, 时区="Asia/Shanghai",
                   日志=str(self.path), pid=os.getpid())

    # ---------- 基础写入 ----------
    def _write(self, line: str) -> None:
        with _LOCK:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        if self.echo:
            print(line, flush=True)

    def event(self, level: str, msg: str, **kv: Any) -> None:
        extra = ("  " + fmt_kv(kv)) if kv else ""
        self._write(f"{_now()} [{level:<5}] {msg}{extra}")

    def kv(self, title: str, mapping: dict) -> None:
        self._write(f"{_now()} [INFO ] ── {title} ──")
        for k, v in mapping.items():
            if isinstance(v, (dict, list, tuple)):
                v = json.dumps(v, ensure_ascii=False, default=str)
            self._write(f"{_now()} [INFO ]   {k}: {v}")

    def checkpoint(self, name: str, **kv: Any) -> float:
        """记录上一阶段耗时；返回本阶段秒数。阶段名按调用顺序形成耗时表。"""
        now = time.time()
        dt = round(now - self._last, 3)
        self._last = now
        self.stages.append({"阶段": name, "耗时_秒": dt})
        self.event("INFO", f"阶段完成 · {name}", 耗时秒=dt, **kv)
        return dt

    def metric(self, **kv: Any) -> None:
        self.metrics.update(kv)
        self.event("INFO", "计数", **kv)

    def subprocess(self, label: str, stdout: str = "", stderr: str = "",
                   returncode: int | None = None, limit: int = 20000) -> None:
        self._write(f"{_now()} [INFO ] ── 子进程 {label} 退出码={returncode} ──")
        for stream, text in (("stdout", stdout or ""), ("stderr", stderr or "")):
            if not text.strip():
                continue
            clipped = text if len(text) <= limit else (
                f"…（前 {limit} 字符，共 {len(text)} 字符）\n" + text[:limit])
            self._write(f"{_now()} [INFO ]   [{stream}]\n" + clipped.rstrip())

    def error(self, exc: BaseException, label: str = "异常") -> None:
        self.error_text = f"{type(exc).__name__}: {exc}"
        self.event("ERROR", label, 类型=type(exc).__name__, 信息=str(exc)[:600])
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self._write(f"{_now()} [ERROR] ── traceback ──\n" + tb.rstrip())

    def finish(self, status: str = "ok", *, error: str = "", file: str = "",
               **meta: Any) -> dict:
        self.status = status
        self.error_text = self.error_text or error
        self.file = file
        self.meta.update(meta)
        total = round(time.time() - self.t0, 3)
        self.event("INFO", "运行结束", 状态=status, 总耗时秒=total,
                   产物=file or "—", 阶段数=len(self.stages),
                   错误=self.error_text[:200] or "—")
        self._write(f"{_now()} [INFO ] ── 阶段耗时表 ──")
        for s in self.stages:
            self._write(f"{_now()} [INFO ]   {s['阶段']:<24} {s['耗时_秒']:>8.3f}s")
        row = {
            "run_id": self.run_id, "title": self.title, "status": status,
            "started": self.started, "ended": datetime.now(TZ_CN).isoformat(timespec="seconds"),
            "elapsed_seconds": total, "file": file, "error": self.error_text,
            "log_file": self.path.name, "stages": self.stages,
            "metrics": self.metrics, **self.meta,
        }
        if self.do_index:
            with _LOCK:
                with self.index_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        return row

    def as_dict(self) -> dict:
        return {"run_id": self.run_id, "log_file": self.path.name,
                "log_path": str(self.path), "status": self.status,
                "stages": self.stages, "metrics": self.metrics}


# ---------- 全局便捷接口（供 pipeline.py 无侵入调用） ----------
def set_current(rl: "RunLog | None") -> None:
    global _CURRENT
    _CURRENT = rl


def current() -> "RunLog | None":
    return _CURRENT


def log(level: str, msg: str, **kv: Any) -> None:
    if _CURRENT is not None:
        _CURRENT.event(level, msg, **kv)


def checkpoint(name: str, **kv: Any) -> None:
    if _CURRENT is not None:
        _CURRENT.checkpoint(name, **kv)


def metric(**kv: Any) -> None:
    if _CURRENT is not None:
        _CURRENT.metric(**kv)


def read_index(log_dir: str | Path, limit: int = 20) -> list[dict]:
    p = Path(log_dir) / "runs.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    # 同一 run_id 的 pipeline / serve 两份合并成一条展示
    merged: dict[str, dict] = {}
    for r in rows:
        rid = r.get("run_id", "")
        if rid not in merged:
            merged[rid] = dict(r)
        else:
            m = merged[rid]
            for k in ("window", "interval", "interval_name", "source", "file",
                      "candidate", "main", "excluded", "estimated", "error", "log_url"):
                if not m.get(k) and r.get(k):
                    m[k] = r[k]
            if r.get("status") == "error":
                m["status"] = "error"
            m["titles"] = sorted(set(m.get("titles", [r.get("title", "")]) + [r.get("title", "")]))
    out = list(merged.values())
    out.sort(key=lambda x: x.get("started", ""), reverse=True)
    return out[:max(1, min(limit, 10000))]


def read_log_text(log_dir: str | Path, run_id: str) -> tuple[str, list[str]]:
    import re
    if not re.fullmatch(RUN_ID_RE, run_id):
        raise ValueError("非法 run_id")
    d = Path(log_dir)
    files = sorted(d.glob(f"{run_id}.*.log"))
    if not files:
        raise FileNotFoundError(run_id)
    parts = []
    for p in files:
        parts.append(f"\n===== {p.name} =====\n" + p.read_text(encoding="utf-8", errors="replace"))
    return "".join(parts), [p.name for p in files]


def prune(log_dir: str | Path, keep: int = 200) -> int:
    """保留最近 keep 次运行，删除更早的 .log 并重写索引，避免无限增长。"""
    d = Path(log_dir)
    d.mkdir(parents=True, exist_ok=True)
    rows = read_index(d, limit=100000)
    keep_rows = rows[:keep]
    keep_ids = {r["run_id"] for r in keep_rows}
    active = os.environ.get("NEWS_RUN_ID", "")
    if active:
        keep_ids.add(active)
    now = time.time()
    removed = 0
    for p in d.glob("*.log"):
        rid = p.name.split(".")[0]
        # 当前运行、索引内保留项、一小时内新建的日志都不清理
        if rid in keep_ids or (now - p.stat().st_mtime) < 3600 or p.name in ("access.log", "server.log"):
            continue
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    idx = d / "runs.jsonl"
    with _LOCK:
        idx.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in reversed(keep_rows)),
                       encoding="utf-8")
    return removed
