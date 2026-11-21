#!/usr/bin/env python3
"""本地运行服务：静态页面 + /api/run 真实执行流水线 + Excel 下载 + 运行日志。

零第三方依赖（标准库 http.server + subprocess 调 run.py / build_site.py）。
页面打开 http://127.0.0.1:8000 即可设置窗口、端点开闭、数据源，点「运行」后
服务端跑原版 Python 流水线，产出五工作表 Excel 并直接下载。

    python serve.py                         # 127.0.0.1:8000，托管 docs/
    python serve.py --port 8765 --host 0.0.0.0
    python serve.py --no-rebuild-site       # 默认每次跑完覆盖 docs/downloads/latest.xlsx
    python serve.py --log-dir logs --keep-logs 200

API：
    GET  /api/status                        能力探测（页面据此显示「本地运行」按钮）
    POST /api/run                           {start,end,interval,source,suffix?,rebuild_site?}
    GET  /api/download?file=...             下载 xlsx
    GET  /api/runs                          已有产物列表
    GET  /api/logs?limit=20                 最近运行索引（状态/窗口/条数/耗时/错误）
    GET  /api/logs/<run_id>                 合并返回 serve + pipeline 日志全文
    GET  /api/logs/<run_id>/download        下载该次运行的 .log 文本

日志：logs/<run_id>.serve.log、logs/<run_id>.pipeline.log、logs/runs.jsonl、
      logs/access.log。每次运行一个 run_id，页面与流水线日志用同一 ID 关联。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.parse
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from news_pipeline import runlog                    # noqa: E402

DOCS = BASE / "docs"
OUTPUT = BASE / "output"
DOWNLOADS = DOCS / "downloads"
LOG_DIR = BASE / "logs"
INTERVALS = {"lo-rc", "lo-ro", "lc-rc", "lc-ro"}
MODES = {"target", "demo", "inbox", "live", "hybrid"}
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}$")
SAFE_NAME = re.compile(r"^[\w\u4e00-\u9fff.\-]+\.xlsx$")


def now_cn() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


def access(line: str) -> None:
    """HTTP 访问日志（固定文件，带锁，供排查页面请求用）。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with runlog._LOCK:
        with (LOG_DIR / "access.log").open("a", encoding="utf-8") as f:
            f.write(f"{now_cn()} {line}\n")


def normalize_time(s: str) -> str:
    s = str(s or "").strip().replace("T", " ")
    if not TIME_RE.fullmatch(s):
        raise ValueError(f"时间格式应为 YYYY-MM-DD HH:MM，收到 {s!r}")
    datetime.strptime(s, "%Y-%m-%d %H:%M")
    return s


def safe_filename(name: str) -> Path:
    name = Path(str(name)).name
    if not SAFE_NAME.fullmatch(name):
        raise ValueError(f"非法文件名：{name!r}")
    p = DOWNLOADS / name
    if not p.exists():
        raise FileNotFoundError(name)
    return p


def xlsx_stats(path: Path, rl: runlog.RunLog | None = None) -> dict:
    try:
        from openpyxl import load_workbook
        from openpyxl.reader.excel import ExcelReader
        ExcelReader.read_custom = lambda self: None  # type: ignore[method-assign]
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            out = {
                "主表条数": wb["处理后的数据"].max_row - 1,
                "候选池条数": wb["窗口内全部数据"].max_row - 1,
                "前10条数": wb["排名最靠前的10条"].max_row - 1,
                "工作表": wb.sheetnames,
            }
            if rl:
                rl.event("INFO", "Excel 结构核对", **out)
            return out
        finally:
            wb.close()
    except Exception as exc:  # 统计失败不影响下载
        if rl:
            rl.event("WARN", "Excel 结构核对不可用", 原因=str(exc)[:200])
        return {"统计不可用": str(exc)[:120]}


def child_env(rl: runlog.RunLog) -> dict:
    env = os.environ.copy()
    env["NEWS_RUN_ID"] = rl.run_id
    env["NEWS_LOG_DIR"] = str(LOG_DIR)
    env["NEWS_LOG_INDEX"] = "0"     # 索引由服务端统一写，避免同一 run_id 两行
    return env


def execute(payload: dict, default_rebuild: bool, timeout: int,
            rl: runlog.RunLog) -> dict:
    rl.kv("请求参数", {k: v for k, v in payload.items() if k != "token"})
    start = normalize_time(payload.get("start", ""))
    end = normalize_time(payload.get("end", ""))
    interval = str(payload.get("interval") or "lo-ro")
    source = str(payload.get("source") or "demo")
    suffix = str(payload.get("suffix") or "").strip()
    if interval not in INTERVALS:
        raise ValueError(f"interval 应为 {sorted(INTERVALS)}")
    if source not in MODES:
        raise ValueError(f"source 应为 {sorted(MODES)}")
    if suffix and not re.fullmatch(r"[\w\u4e00-\u9fff.\-]{1,40}", suffix):
        raise ValueError("suffix 只允许中英文、数字、下划线、短横线与点号")
    if datetime.strptime(start, "%Y-%m-%d %H:%M") >= datetime.strptime(end, "%Y-%m-%d %H:%M"):
        raise ValueError("窗口右界必须晚于左界")
    rl.event("INFO", "参数校验通过", 起=start, 止=end, 端点开闭=interval,
             数据源=source, 后缀=suffix or "—")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(BASE / "run.py"),
           "--start", start, "--end", end,
           "--interval", interval, "--source", source,
           "--json-summary"]
    if suffix:
        cmd += ["--suffix", suffix]
    if source == "live":
        cmd += ["--rss", "--apis", "--html"]
    rl.event("INFO", "启动流水线子进程", 命令=" ".join(cmd), 超时秒=timeout,
             run_id=rl.run_id)

    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(BASE), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=child_env(rl))
    except subprocess.TimeoutExpired as exc:
        rl.subprocess("run.py（超时）",
                      stdout=(exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or ""),
                      stderr=(exc.stderr or b"").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or ""),
                      returncode=None)
        rl.checkpoint("流水线子进程（超时）")
        raise
    rl.subprocess("run.py", proc.stdout, proc.stderr, proc.returncode)
    rl.checkpoint("流水线子进程", 退出码=proc.returncode)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-1200:]
        raise RuntimeError(f"流水线退出码 {proc.returncode}：{tail}")

    # 从 --json-summary 产物定位本次 xlsx（避免同名旧文件误判）
    right_date = datetime.strptime(end, "%Y-%m-%d %H:%M").strftime("%Y_%m_%d")
    stem = f"{right_date}_窗口期{suffix}"
    xlsx = OUTPUT / f"{stem}.xlsx"
    summary_p = OUTPUT / f"{stem}.json"
    if not xlsx.exists():
        cands = sorted(OUTPUT.glob("*_窗口期*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not cands:
            raise RuntimeError("流水线未生成 xlsx 产物")
        xlsx = cands[0]
        summary_p = xlsx.with_suffix(".json")
        rl.event("WARN", "精确产物名未命中，回退取最新 xlsx", 命中=xlsx.name)
    summary = {}
    if summary_p.exists():
        try:
            summary = json.loads(summary_p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            rl.event("WARN", "运行摘要 JSON 解析失败", 原因=str(exc)[:160])
    rl.event("INFO", "流水线结果摘要", 产物=xlsx.name,
             候选池=summary.get("候选池条数"), 主表=summary.get("主表条数"),
             窗口外排除=summary.get("窗口外排除"), 时点推定=summary.get("时点推定入池"),
             run_id=summary.get("run_id", rl.run_id))

    dest = DOWNLOADS / xlsx.name
    shutil.copy2(xlsx, dest)
    rl.event("INFO", "产物已复制到下载目录", 文件=dest.name, 字节=xlsx.stat().st_size)

    rebuild = bool(payload.get("rebuild_site", default_rebuild))
    rebuilt = False
    if rebuild:
        bcmd = [sys.executable, str(BASE / "build_site.py"),
                "--xlsx", str(xlsx), "--summary", str(summary_p),
                "--mode", source, "--overwrite"]
        b = subprocess.run(bcmd, cwd=str(BASE), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=120, env=child_env(rl))
        rl.subprocess("build_site.py", b.stdout, b.stderr, b.returncode)
        rebuilt = b.returncode == 0
        if not rebuilt:
            rl.event("WARN", "页面归档同步失败（不影响 Excel 下载）", 退出码=b.returncode)
    rl.checkpoint("产物定位与页面归档", 同步归档=rebuilt)

    stats = xlsx_stats(xlsx, rl)
    rl.checkpoint("Excel 结构核对")
    # 覆盖式：站点固定文件是 latest.xlsx，下载入口也指向它（仓库地址不变）
    latest_name = "latest.xlsx"
    if rebuilt and (DOWNLOADS / latest_name).exists():
        dl_name = latest_name
    else:
        dl_name = xlsx.name
    result = {
        "ok": True,
        "file": dl_name,
        "source_xlsx": xlsx.name,
        "download_url": "/api/download?file=" + urllib.parse.quote(dl_name),
        "size": xlsx.stat().st_size,
        "generated_at": now_cn(),
        "elapsed_seconds": round(time.time() - t0, 2),
        "window": summary.get("采集窗口", f"({start}, {end})  Asia/Shanghai"),
        "interval": interval,
        "interval_name": summary.get("端点开闭", {
            "lo-rc": "左开右闭", "lo-ro": "左开右开",
            "lc-rc": "左闭右闭", "lc-ro": "左闭右开"}[interval]),
        "source": source,
        "candidate": summary.get("候选池条数", stats.get("候选池条数")),
        "main": summary.get("主表条数", stats.get("主表条数")),
        "excluded": summary.get("窗口外排除"),
        "estimated": summary.get("时点推定入池"),
        "grades": summary.get("等级分布", {}),
        "h_channel": summary.get("H通道", ""),
        "sheets": stats.get("工作表", []),
        "rebuilt_site": rebuilt,
        "command": " ".join(cmd[1:]),
        "run_id": rl.run_id,
        "log_url": "/api/logs/" + rl.run_id,
        "log_download": "/api/logs/" + rl.run_id + "/download",
        "stages": summary.get("阶段耗时", rl.stages),
    }
    rl.metric(**{k: result[k] for k in ("candidate", "main", "excluded", "estimated")})
    return result


class Handler(SimpleHTTPRequestHandler):
    server_version = "NewsDaily/1.0"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(DOCS), **kw)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")

    def _json(self, obj: dict, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _plain(self, text: str, filename: str, code: int = 200):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition",
                         "attachment; filename*=UTF-8''" + urllib.parse.quote(filename))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/api/status":
            return self._json({
                "ok": True, "service": "news-daily-local",
                "time": now_cn(), "tz": "Asia/Shanghai",
                "intervals": ["lo-ro", "lo-rc", "lc-ro", "lc-rc"],
                "modes": sorted(MODES),
                "default_interval": "lo-ro",
                "rebuild_site_default": self.server.rebuild_default,  # type: ignore[attr-defined]
                "pipeline": "run.py → 五工作表 Excel",
                "log_dir": str(LOG_DIR), "log_api": "/api/logs",
                "log_keep": self.server.log_keep,                     # type: ignore[attr-defined]
            })
        if u.path == "/api/runs":
            DOWNLOADS.mkdir(parents=True, exist_ok=True)
            rows = []
            for p in sorted(DOWNLOADS.glob("*.xlsx"), key=lambda x: x.stat().st_mtime, reverse=True):
                rows.append({"file": p.name, "size": p.stat().st_size,
                             "mtime": datetime.fromtimestamp(p.stat().st_mtime,
                                                             ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
                             "download_url": "/api/download?file=" + urllib.parse.quote(p.name)})
            return self._json({"ok": True, "runs": rows})
        if u.path == "/api/logs":
            try:
                limit = int((q.get("limit") or ["20"])[0])
            except ValueError:
                limit = 20
            rows = runlog.read_index(LOG_DIR, limit)
            for r in rows:
                r["log_url"] = "/api/logs/" + r["run_id"]
                r["log_download"] = "/api/logs/" + r["run_id"] + "/download"
            return self._json({"ok": True, "logs": rows, "log_dir": str(LOG_DIR)})
        if u.path.startswith("/api/logs/"):
            rest = u.path[len("/api/logs/"):].strip("/")
            download = rest.endswith("/download")
            rid = rest[:-len("/download")] if download else rest
            try:
                text, files = runlog.read_log_text(LOG_DIR, rid)
                return self._plain(text, f"{rid}.log") if download else self._json({
                    "ok": True, "run_id": rid, "files": files, "text": text})
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)}, 400)
            except FileNotFoundError:
                return self._json({"ok": False, "error": f"未找到 run_id={rid} 的日志"}, 404)
        if u.path == "/api/download":
            try:
                p = safe_filename((q.get("file") or [""])[0])
                data = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition",
                                 "attachment; filename*=UTF-8''" + urllib.parse.quote(p.name))
                self._cors()
                self.end_headers()
                self.wfile.write(data)
            except (ValueError, FileNotFoundError) as exc:
                return self._json({"ok": False, "error": str(exc)}, 404)
            return None
        return super().do_GET()

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/api/run":
            return self._json({"ok": False, "error": "未知接口"}, 404)
        rl = runlog.RunLog(LOG_DIR, title="serve", index=True, echo=False)
        rl.event("INFO", "收到运行请求", 客户端=self.client_address[0],
                 UA=self.headers.get("User-Agent", "—")[:120])
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 64 * 1024:
                raise ValueError("请求体为空或过大")
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("请求体应为 JSON 对象")
            result = execute(payload, self.server.rebuild_default,  # type: ignore[attr-defined]
                             self.server.run_timeout, rl)           # type: ignore[attr-defined]
            rl.event("INFO", "运行完成", 文件=result["file"],
                     候选池=result["candidate"], 主表=result["main"],
                     耗时秒=result["elapsed_seconds"])
            rl.finish("ok", file=result["file"], window=result["window"],
                      interval=result["interval"], interval_name=result["interval_name"],
                      source=result["source"], candidate=result["candidate"],
                      main=result["main"], excluded=result["excluded"],
                      estimated=result["estimated"],
                      log_url=result["log_url"])
            print(f"[{now_cn()}] 运行完成 {result['file']} | {result['interval_name']} | "
                  f"候选 {result['candidate']} / 主表 {result['main']} | "
                  f"{result['elapsed_seconds']}s | run_id={rl.run_id} | 日志 {rl.path.name}")
            return self._json(result)
        except subprocess.TimeoutExpired as exc:
            rl.error(exc, label="流水线超时")
            rl.finish("error")
            return self._json({"ok": False, "error": "流水线超时，请缩小窗口或改用 demo 模式",
                               "run_id": rl.run_id, "log_url": "/api/logs/" + rl.run_id}, 504)
        except (ValueError, RuntimeError, OSError) as exc:
            rl.error(exc)
            rl.finish("error", window=str(exc)[:200])
            return self._json({"ok": False, "error": str(exc),
                               "run_id": rl.run_id, "log_url": "/api/logs/" + rl.run_id}, 400)
        except Exception as exc:  # 兜底，不向页面泄露栈，但栈写入日志
            rl.error(exc, label="未预期异常")
            rl.finish("error")
            return self._json({"ok": False, "error": f"运行失败：{str(exc)[:300]}",
                               "run_id": rl.run_id, "log_url": "/api/logs/" + rl.run_id}, 500)

    def log_message(self, fmt, *args):
        line = fmt % args if args else fmt
        access(line)
        if "/api/" in line:
            super().log_message(fmt, *args)


def main() -> int:
    p = argparse.ArgumentParser(description="页面驱动的本地要闻运行服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--timeout", type=int, default=300, help="单次流水线超时秒数")
    p.add_argument("--no-rebuild-site", action="store_false", dest="rebuild_site",
                   help="跑完不覆盖固定站点文件（默认覆盖，保证页面地址不变）")
    p.add_argument("--log-dir", default="logs", help="日志目录，默认 logs/")
    p.add_argument("--keep-logs", type=int, default=200, help="启动时保留最近 N 次运行的日志")
    args = p.parse_args()

    global LOG_DIR
    LOG_DIR = Path(args.log_dir)
    if not LOG_DIR.is_absolute():
        LOG_DIR = BASE / LOG_DIR
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    DOCS.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    removed = runlog.prune(LOG_DIR, args.keep_logs)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.rebuild_default = args.rebuild_site       # 默认 True：覆盖式固定站点
    srv.run_timeout = args.timeout                # type: ignore[attr-defined]
    srv.log_keep = args.keep_logs                 # type: ignore[attr-defined]
    print("资讯获取 · 本地运行服务")
    print(f"  页面：    http://{args.host}:{args.port}/")
    print(f"  接口：    /api/status  /api/run  /api/download  /api/runs  /api/logs")
    print(f"  日志目录：{LOG_DIR}（保留最近 {args.keep_logs} 次，启动清理 {removed} 个旧日志）")
    print(f"  默认端点：左开右开 (a,b)   |  默认模式：live（config.yaml，可切）")
    print(f"  产物目录：{DOWNLOADS}")
    print("Ctrl+C 退出")
    access(f"服务启动 host={args.host} port={args.port} log_dir={LOG_DIR}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        access("服务停止（Ctrl+C）")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
