"""资讯获取 · 一键部署真实 HTTP 自检（纯本地 mock，不碰真实 GitHub）。

这是「这次真的验证过」的证据：起一个本地 ThreadingHTTPServer 模拟 GitHub
Contents API + workflow_dispatch，然后以子进程真跑 deploy.py 全流程。

两个场景：
  A 远端已是新版 -> rc=0、PUT 覆盖全部关键文件、dispatch=1、打印「远端 workflow 已更新」
  B 远端是旧版且 PUT 对 workflow 静默不生效（模拟 PAT 缺 workflow 权限）
    -> rc!=0、打印「仍是旧版 / workflow 权限」、dispatch=0

用法：python scripts/selftest_deploy.py
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
WF = BASE / ".github" / "workflows" / "daily-news.yml"
OLD_WF = """name: old
on:
  schedule:
    - cron: '0 12 * * *'
jobs:
  build-deploy:
    steps:
      - name: 归档数据回写仓库（供次日累积）
        run: echo '更新要闻速览'
"""

STATE = {"workflow": WF.read_bytes(), "puts": [], "dispatch": 0, "ignore_wf_put": False}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, obj=None):
        raw = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p.endswith("/contents/.github/workflows/daily-news.yml"):
            self._send(200, {
                "sha": "wfsha",
                "content": base64.b64encode(STATE["workflow"]).decode("ascii"),
                "encoding": "base64",
            })
        else:
            self._send(404, {"message": "Not Found"})

    def do_PUT(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(n) or b"{}")
        p = urllib.parse.urlparse(self.path).path
        rel = urllib.parse.unquote(p.split("/contents/", 1)[-1]) if "/contents/" in p else ""
        STATE["puts"].append(rel)
        if rel == ".github/workflows/daily-news.yml" and not STATE["ignore_wf_put"]:
            STATE["workflow"] = base64.b64decode(body.get("content", ""))
        self._send(200, {"content": {"sha": "newsha"}})

    def do_POST(self):
        if "dispatches" in self.path:
            STATE["dispatch"] += 1
        self._send(204)


def run_deploy(port: int, root: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({
        "NEWS_API_BASE": f"http://127.0.0.1:{port}",
        "NEWS_GITHUB_TOKEN": "fake_token_for_selftest",
        "NEWS_REPO": "lucky-wren/get-news",
        "NEWS_BRANCH": "main",
        "NEWS_ROOT": str(root),
        "PYTHONIOENCODING": "utf-8",
    })
    return subprocess.run(
        [sys.executable, str(root / "deploy.py")],
        env=env, cwd=str(root), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
    )


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    rows, bad = [], []

    def add(name, ok, detail=""):
        rows.append({"检查": name, "结果": "PASS" if ok else "FAIL", "依据": detail})
        if not ok:
            bad.append(name)

    try:
        # ---- 场景 A：正常部署 ----
        STATE.update(workflow=WF.read_bytes(), puts=[], dispatch=0, ignore_wf_put=False)
        r = run_deploy(port, BASE)
        out = (r.stdout or "") + (r.stderr or "")
        add("A 部署返回码 0", r.returncode == 0, f"rc={r.returncode}")
        add("A 打印远端校验通过", "远端 workflow 已更新" in out)
        add("A 自动触发 dispatch", STATE["dispatch"] == 1, f"dispatch={STATE['dispatch']}")
        for rel in (".github/workflows/daily-news.yml", "deploy.bat", "deploy.py",
                    "logs/.gitkeep", "config.yaml", "news_pipeline/rules.py"):
            add(f"A 上传关键文件 {rel}", rel in STATE["puts"])
        add("A 不上传日志/缓存",
            not any(x.endswith((".pyc", ".log")) for x in STATE["puts"]))
        add("A 上传 .github 隐藏目录",
            any(x.startswith(".github/") for x in STATE["puts"]))
        add("A 不打印 Token 明文", "fake_token_for_selftest" not in out)

        # ---- 场景 B：模拟 PAT 缺 workflow 权限（PUT 被静默忽略） ----
        STATE.update(workflow=OLD_WF.encode("utf-8"), puts=[], dispatch=0, ignore_wf_put=True)
        r2 = run_deploy(port, BASE)
        out2 = (r2.stdout or "") + (r2.stderr or "")
        add("B 旧 workflow 时返回非 0", r2.returncode != 0, f"rc={r2.returncode}")
        add("B 明确报仍是旧版", "仍是旧版" in out2)
        add("B 提示 workflow 权限", "workflow 权限" in out2 or "repo+workflow" in out2)
        add("B 不触发 dispatch", STATE["dispatch"] == 0, f"dispatch={STATE['dispatch']}")

        # ---- dry-run 离线 ----
        r3 = subprocess.run([sys.executable, str(BASE / "deploy.py"), "--dry-run"],
                            cwd=str(BASE), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60)
        add("dry-run 返回 0", r3.returncode == 0, f"rc={r3.returncode}")
        add("dry-run 本地标记齐全", "齐全" in (r3.stdout or ""))
        add("dry-run 不联网也成功", "文件样例" in (r3.stdout or ""))
    finally:
        srv.shutdown()
        srv.server_close()

    result = {"检查数": len(rows), "PASS": len(rows) - len(bad),
              "FAIL": len(bad), "失败项": bad, "检查": rows,
              "说明": "本地 mock GitHub API 真实 HTTP 全流程；未访问真实 GitHub"}
    print(json.dumps({k: result[k] for k in ("检查数", "PASS", "FAIL", "失败项")},
                     ensure_ascii=False))
    for x in rows:
        if x["结果"] == "FAIL":
            print("  [FAIL]", x["检查"], "|", x["依据"])
    (BASE / "output").mkdir(exist_ok=True)
    (BASE / "output" / "deploy_selftest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
