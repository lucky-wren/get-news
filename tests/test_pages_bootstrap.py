"""截图 43 回归：Pages 站点未创建导致 configure-pages@v5 404。

真起 ThreadingHTTPServer 模拟 GitHub REST，逐条核对 method / path / body /
Authorization，跑完 deploy.ensure_pages_enabled 的四种状态与幂等性。
静态契约另锁死：deploy 必须在 dispatch 之前调用它、workflow 不得用
GITHUB_TOKEN 的 enablement（那会变成 403）、必须有守卫步骤、外网仍被拦。
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import deploy
from newsbrief import workflow_contract as wc

ROOT = Path(deploy.__file__).resolve().parent


class _Handler(BaseHTTPRequestHandler):
    state: dict = {}
    calls: list = []

    def log_message(self, *a):
        pass

    def _reply(self, code: int, body: dict):
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _record(self, method: str):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        self.__class__.calls.append({
            "method": method,
            "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "accept": self.headers.get("Accept", ""),
            "body": json.loads(raw) if raw else None,
        })

    def do_GET(self):
        self._record("GET")
        mode = self.__class__.state.get("mode")
        if mode == "missing":
            self._reply(404, {"message": "Not Found"})
        elif mode == "forbidden":
            self._reply(403, {"message": "Resource not accessible by integration"})
        elif mode == "legacy":
            self._reply(200, {"build_type": "legacy"})
        else:
            self._reply(200, {"build_type": "workflow"})

    def do_POST(self):
        self._record("POST")
        if self.__class__.state.get("create") == "403":
            self._reply(403, {"message": "Resource not accessible by integration"})
        else:
            self._reply(201, {"build_type": "workflow"})
            self.__class__.state["mode"] = "workflow"   # 幂等：第二次 GET 就 200

    def do_PUT(self):
        self._record("PUT")
        self._reply(200, {"build_type": "workflow"})
        self.__class__.state["mode"] = "workflow"


@pytest.fixture
def api(monkeypatch):
    _Handler.state = {"mode": "missing"}
    _Handler.calls = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    monkeypatch.setattr(deploy, "API_BASE",
                        f"http://127.0.0.1:{srv.server_address[1]}")
    yield _Handler
    srv.shutdown()
    th.join(timeout=2)


def _posts(cls):
    return [c for c in cls.calls if c["method"] == "POST"]


def test_creates_pages_site_when_missing_and_is_idempotent(api):
    """404 → POST /pages build_type=workflow；再跑一次只 GET，不重复创建。"""
    ok, note = deploy.ensure_pages_enabled("acme/get-news", "ghp_TESTTOKEN")
    assert ok and "创建" in note, note
    posts = _posts(api)
    assert len(posts) == 1
    c = posts[0]
    assert c["path"] == "/repos/acme/get-news/pages"
    assert c["body"] == {"build_type": "workflow"}
    assert c["auth"] == "Bearer ghp_TESTTOKEN"
    assert "vnd.github+json" in c["accept"]

    ok2, note2 = deploy.ensure_pages_enabled("acme/get-news", "ghp_TESTTOKEN")
    assert ok2 and "已启用" in note2, note2
    assert len(_posts(api)) == 1, "第二次不应再 POST（幂等）"


def test_already_workflow_is_noop(api):
    api.state["mode"] = "workflow"
    ok, note = deploy.ensure_pages_enabled("acme/get-news", "tok")
    assert ok and "已启用" in note
    assert [c["method"] for c in api.calls] == ["GET"]


def test_switches_legacy_branch_source_to_actions(api):
    api.state["mode"] = "legacy"
    ok, note = deploy.ensure_pages_enabled("acme/get-news", "tok")
    assert ok and "切换" in note, note
    puts = [c for c in api.calls if c["method"] == "PUT"]
    assert len(puts) == 1 and puts[0]["body"] == {"build_type": "workflow"}
    assert not _posts(api), "站点已存在时不应 POST"


@pytest.mark.parametrize("mode", ["forbidden", "missing"])
def test_failure_gives_actionable_manual_hint(api, mode):
    api.state["mode"] = mode if mode == "forbidden" else "missing"
    if mode == "missing":
        api.state["create"] = "403"
    ok, note = deploy.ensure_pages_enabled("acme/get-news", "tok")
    assert ok is False
    assert "gh api -X POST repos/acme/get-news/pages -f build_type=workflow" in note
    assert "Settings → Pages" in note and "deploy.bat" in note
    assert "不抛异常" not in note  # 返回 bool，不抛


def test_deploy_calls_ensure_before_dispatch():
    src = (ROOT / "deploy.py").read_text(encoding="utf-8")
    i_ensure = src.index("ok_pages, note_pages = ensure_pages_enabled(")
    i_dispatch = src.index("/actions/workflows/daily.yml/dispatches")
    assert i_ensure < i_dispatch, "Pages 站点必须在触发 workflow 之前就绪"
    assert "def ensure_pages_enabled" in src
    assert "if not ok_pages:" in src and "本次不自动触发 workflow" in src


def test_workflow_has_guard_no_enablement_and_pages_permission():
    wf = (ROOT / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")
    import yaml
    d = yaml.safe_load(wf)
    b = d["jobs"]["build"]
    assert b["permissions"].get("pages") == "read"
    names = [s.get("name") for s in b["steps"]]
    assert any(n and "检查 Pages 站点已启用" in n for n in names)
    conf = next(s for s in b["steps"]
                if str(s.get("uses", "")).startswith("actions/configure-pages"))
    assert not (conf.get("with") or {}).get("enablement"), \
        "默认 GITHUB_TOKEN 没有 administration:write，enablement 只会把 404 换成 403"
    assert "gh api -X POST" in wf and "build_type=workflow" in wf
    r = wc.check(wf)
    assert r["ok"] and r["production"], r["note"]


def test_external_network_still_blocked():
    """放行 loopback 不等于放宽保护：任何非本机地址仍当场失败。"""
    with pytest.raises(RuntimeError, match="禁止真实外网连接"):
        socket.create_connection(("example.com", 80), timeout=0.2)
