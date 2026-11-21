#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资讯获取 · 一键部署（只需一个 GitHub Token，免 gh / 免浏览器登录）。

为什么不再用 PowerShell 5.1 当主实现：
  Windows PowerShell 5.1 读取「UTF-8 无 BOM」的 .ps1 时会按系统 ANSI(GBK) 解码，
  中文注释/字符串乱码并破坏引号配对，导致 Unexpected token、Try 缺 Catch 等连锁语法错。
  cmd.exe 解析「UTF-8 无 BOM + 中文」的 .bat 同样会把中文片段当成命令执行。
  本项目自带 Python（GitHub Actions 里也是 Python），改用 Python 后：
    - 源码 UTF-8 明确声明，Windows 下由 chcp 65001 + stdout.reconfigure 保证显示；
    - 逻辑可用 --dry-run 离线自检、可用 --api-base 指向本地 mock 做真实 HTTP 全链路验证。

功能：
  1. 收集项目文件（含 .github/ 隐藏目录，跳过日志/缓存/包）；
  2. 通过 GitHub Contents API 逐文件 PUT（自动带 sha 更新已有文件）；
  3. **读回远端 .github/workflows/daily-news.yml 校验四个新标记**，
     缺一个就明确提示「PAT 缺 workflow 权限」并返回 1；
  4. 校验通过自动 POST workflow_dispatch 触发一次运行；
  5. 全程不打印 Token 明文。

用法：
  双击 deploy.bat（推荐）          -> 自动调用本脚本
  python deploy.py --dry-run      -> 离线自检，不联网
  python deploy.py                -> 交互输入 Token 并部署
环境变量：NEWS_GITHUB_TOKEN / NEWS_REPO / NEWS_BRANCH / NEWS_ROOT / NEWS_API_BASE
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_REPO = "lucky-wren/get-news"
DEFAULT_API = "https://api.github.com"
WF_REL = ".github/workflows/daily-news.yml"
MARKS = ("准备日志目录", "continue-on-error", "mkdir -p logs", "覆盖资讯获取数据")

SKIP_DIRS = {".git", "__pycache__", "_sim", "_verify_final", "node_modules",
             ".pytest_cache", ".mypy_cache", ".idea", ".vscode"}
SKIP_EXT = {".pyc", ".log", ".tmp", ".out", ".zip", ".pyo", ".bak"}
SKIP_NAMES = {"deploy_selftest_state.json"}


def say(msg: str = "") -> None:
    print(msg, flush=True)


def mask(s: str, token: str) -> str:
    return s.replace(token, "***") if token else s


def _fix_stdout() -> None:
    """Windows cmd 默认 GBK，chcp 65001 后让 Python 用 UTF-8 输出中文。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


def repo_from_config(root: Path) -> str:
    p = root / "config.yaml"
    if p.exists():
        import re
        m = re.search(r'github_repo:\s*"([^"]+)"', p.read_text(encoding="utf-8", errors="replace"))
        if m and m.group(1).strip() and "换成你的" not in m.group(1):
            return m.group(1).strip()
    return DEFAULT_REPO


def should_skip(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    if any(d in SKIP_DIRS for d in parts):
        return True
    if Path(rel).suffix.lower() in SKIP_EXT:
        return True
    if parts[-1] in SKIP_NAMES:
        return True
    # logs/ 只放行 .gitkeep：让 checkout 后目录天然存在（旧 workflow 双保险）
    if parts[0] == "logs" and parts[-1] != ".gitkeep":
        return True
    return False


def collect(root: Path) -> list[tuple[str, Path]]:
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if not should_skip(rel):
            out.append((rel, p))
    return out


def quote_path(rel: str) -> str:
    return "/".join(urllib.parse.quote(seg, safe="") for seg in rel.split("/"))


def request(method: str, url: str, token: str, body: dict | None = None,
            timeout: int = 30) -> tuple[int, bytes]:
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "news-deploy",
        "Accept": "application/vnd.github+json",
    }
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(r.status), r.read()
    except urllib.error.HTTPError as e:
        return int(e.code), e.read()
    except Exception as e:
        raise RuntimeError(mask(str(e), token))


def contents_url(api: str, repo: str, rel: str, branch: str) -> str:
    return f"{api}/repos/{repo}/contents/{quote_path(rel)}?ref={urllib.parse.quote(branch)}"


def remote_sha(api: str, repo: str, rel: str, branch: str, token: str) -> str | None:
    code, raw = request("GET", contents_url(api, repo, rel, branch), token)
    if code == 404:
        return None
    if code != 200:
        raise RuntimeError(f"读取远端 {rel} 失败 HTTP {code}: {raw[:200].decode('utf-8', 'replace')}")
    return str((json.loads(raw.decode("utf-8")) or {}).get("sha") or "") or None


def put_file(api: str, repo: str, rel: str, full: Path, branch: str, token: str) -> str:
    sha = remote_sha(api, repo, rel, branch, token)
    body = {
        "message": f"deploy: 资讯获取一键部署 {rel}",
        "content": base64.b64encode(full.read_bytes()).decode("ascii"),
        "branch": branch,
    }
    if sha:
        body["sha"] = sha
    code, raw = request("PUT", f"{api}/repos/{repo}/contents/{quote_path(rel)}", token, body)
    if code in (200, 201):
        return "ok"
    txt = raw[:300].decode("utf-8", "replace")
    if code == 422 and ("same" in txt or "identical" in txt or "sha" in txt):
        return "unchanged"
    raise RuntimeError(f"上传 {rel} 失败 HTTP {code}: {mask(txt, token)}")


def read_remote_workflow(api: str, repo: str, branch: str, token: str) -> str:
    code, raw = request("GET", contents_url(api, repo, WF_REL, branch), token)
    if code != 200:
        raise RuntimeError(f"读回远端 workflow 失败 HTTP {code}（仓库可能还没建好）")
    d = json.loads(raw.decode("utf-8")) or {}
    b64 = (d.get("content") or "").replace("\n", "").replace("\r", "")
    return base64.b64decode(b64).decode("utf-8", "replace")


def dispatch(api: str, repo: str, branch: str, token: str) -> None:
    url = f"{api}/repos/{repo}/actions/workflows/daily-news.yml/dispatches"
    code, raw = request("POST", url, token, {"ref": branch})
    if code in (200, 204):
        say(f"✅ 已触发运行。去 https://github.com/{repo}/actions 看结果")
        say("   重跑后请核：『采集 → 五工作表 Excel』不再是 0s、CI 摘要显示 job 状态: success")
    else:
        say(f"⚠ 自动触发失败 HTTP {code}（不影响部署）：去 Actions 页手动点 Run workflow")


def main() -> int:
    _fix_stdout()
    ap = argparse.ArgumentParser(description="资讯获取一键部署（只需 GitHub Token）")
    ap.add_argument("--root", default=os.environ.get("NEWS_ROOT") or str(ROOT))
    ap.add_argument("--repo", default=os.environ.get("NEWS_REPO") or "")
    ap.add_argument("--branch", default=os.environ.get("NEWS_BRANCH") or "main")
    ap.add_argument("--token", default=os.environ.get("NEWS_GITHUB_TOKEN") or "")
    ap.add_argument("--api-base", default=os.environ.get("NEWS_API_BASE") or DEFAULT_API)
    ap.add_argument("--dry-run", action="store_true", help="只列文件与本地校验，不联网")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not (root / "config.yaml").exists():
        say(f"❌ 没找到 config.yaml：请把 deploy.bat / deploy.py 放在项目根目录。当前：{root}")
        return 1
    repo = args.repo.strip() or repo_from_config(root)
    branch = args.branch.strip() or "main"
    api = args.api_base.rstrip("/")

    say("资讯获取 · 一键部署")
    say(f"目标仓库：{repo}（分支 {branch}）")
    say(f"项目根：{root}")

    files = collect(root)
    say(f"待同步 {len(files)} 个文件（含 .github/ 隐藏目录；跳过日志/缓存/包）")
    must = ["deploy.bat", "deploy.py", WF_REL, "logs/.gitkeep", "config.yaml"]
    for m in must:
        if not any(rel == m for rel, _ in files):
            say(f"❌ 缺少关键文件 {m}")
            return 1

    if args.dry_run:
        say("\n--- dry-run：本地校验（不联网） ---")
        wf = (root / WF_REL).read_text(encoding="utf-8", errors="replace")
        missing = [m for m in MARKS if m not in wf]
        say(f"本地 workflow 新标记：{'齐全' if not missing else '缺失 ' + '、'.join(missing)}")
        say(f"logs/.gitkeep 随包：{'是' if (root / 'logs/.gitkeep').exists() else '否'}")
        say("文件样例：" + "、".join(rel for rel, _ in files[:6]))
        say(f"跳过检查：.pyc/.log 入包 {sum(1 for rel, _ in files if rel.endswith(('.pyc', '.log')))} 个")
        return 0 if not missing else 1

    token = args.token.strip()
    if not token:
        say("\nGitHub PAT 权限要求：")
        say("  classic PAT 勾 repo + workflow（改 .github/workflows 必须 workflow）")
        say("  fine-grained PAT 给 Contents 读写 + Workflows 读写（+ Actions 读写可自动触发）")
        try:
            token = getpass.getpass("请输入 GitHub Token（不回显，回车取消）: ").strip()
        except Exception:
            token = ""
    if not token:
        say("❌ Token 为空，已取消")
        return 1

    try:
        say("\n=== 上传到 GitHub ===")
        ok = unchanged = 0
        for i, (rel, full) in enumerate(files, 1):
            try:
                r = put_file(api, repo, rel, full, branch, token)
                ok += (r == "ok"); unchanged += (r == "unchanged")
            except RuntimeError as e:
                say(f"  [警告] {mask(str(e), token)}")
            if i % 25 == 0:
                say(f"  {i}/{len(files)}")
        say(f"上传完成：新建/更新 {ok}，内容相同跳过 {unchanged}，共 {len(files)} 个")

        # ---- 关键：读回远端校验，根治「以为推了其实 .github 没上去」 ----
        say("\n=== 校验远端 workflow（这一步决定问题是否真的解决） ===")
        remote = read_remote_workflow(api, repo, branch, token)
        missing = [m for m in MARKS if m not in remote]
        if missing:
            say(f"❌ 远端 workflow 仍是旧版，缺少：{'、'.join(missing)}")
            say("   最常见原因：PAT 没勾 workflow 权限，GitHub 静默拒绝改 .github/workflows/。")
            say("   请重生成 Token（classic 勾 repo+workflow，或 fine-grained 给 Workflows 读写）后重跑。")
            return 1
        say("✅ 远端 workflow 已更新：准备日志目录 / 增强步容错 / 新 commit 文案齐全")

        say("\n=== 触发一次 workflow_dispatch ===")
        dispatch(api, repo, branch, token)
        owner, name = repo.split("/", 1)
        say(f"\n完成。固定页面：https://{owner}.github.io/{name}/")
        return 0
    except Exception as e:
        say(f"\n❌ 部署未完成：{mask(str(e), token)}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
