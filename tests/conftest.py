"""零网络自检 + 包完整性预检。

CI 的「零网络自检」步骤在依赖安装完成后运行。这里做两件事：
1. 会话启动前按 manifest.json 核对运行必需文件；缺件时直接给出中文清单，
   而不是等 `from newsbrief import ...` 抛出含糊的 ImportError。
2. 对每个测试屏蔽真实 socket 连接：任何偷偷联网的代码当场失败。
   httpx.MockTransport、离线语料 feed.json.gz、本地 xlsx 均不走 socket。

清单只来自 manifest.json 一份，与 deploy.py 推送清单同源。
"""
from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifest.json"


def _required() -> list[str]:
    if not MANIFEST.is_file():
        pytest.exit("缺 manifest.json（推送清单单一真值源），"
                    "请整体替换工程目录，不要只拷改动过的文件。", returncode=2)
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    # 自检步骤不要求成品/页面存在（CI 仓库里这些由构建步骤生成）
    return [p for p in m["required"]
            if not p.startswith(("output/", "docs/"))]


def pytest_sessionstart(session):
    miss = [p for p in _required() if not (ROOT / p).is_file()]
    if miss:
        pytest.exit(
            "交付包缺运行必需文件（这正是上一轮 CI 抛含糊 ImportError 的根因）：\n  "
            + "\n  ".join(miss)
            + "\n请勿只同步部分文件：整体替换 newsbrief/ 目录，"
              "并带上 data/、tests/、manifest.json。",
            returncode=2)


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """禁止真实外网连接；放行 127.0.0.1 的本地测试 HTTP server。

    Pages 自助启用的回归测试要真起 ThreadingHTTPServer 跑 GET/POST/PUT，
    这属于测试桩而非外网。语义不变：**任何非 loopback 连接仍然当场失败**，
    并另有测试反证这一点。
    """
    _orig_connect = socket.socket.connect
    _orig_connect_ex = socket.socket.connect_ex
    _orig_create = socket.create_connection

    def _loopback(addr) -> bool:
        try:
            host = addr[0] if isinstance(addr, (tuple, list)) else addr
            return str(host) in ("127.0.0.1", "::1", "localhost")
        except Exception:
            return False

    def _blocked_connect(self, address, *a, **kw):
        if _loopback(address):
            return _orig_connect(self, address, *a, **kw)
        raise RuntimeError(
            "零网络自检：禁止真实外网连接；采集请使用 httpx.MockTransport 或离线语料")

    def _blocked_connect_ex(self, address, *a, **kw):
        if _loopback(address):
            return _orig_connect_ex(self, address, *a, **kw)
        raise RuntimeError("零网络自检：禁止真实外网连接")

    def _blocked_create(address, *a, **kw):
        if _loopback(address):
            return _orig_create(address, *a, **kw)
        raise RuntimeError("零网络自检：禁止真实外网连接")

    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked_connect_ex)
    monkeypatch.setattr(socket, "create_connection", _blocked_create)
    yield
