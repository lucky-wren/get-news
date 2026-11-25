"""一键部署：只需要一个 GitHub Token，别的一概不问。

- 仓库名不用填：拿 Token 调 GET /user 自动得到 owner，项目名固定 get-news
  （既定项目名）；仓库不存在就用同一个 Token 自动创建，不用登录网页。
- 免登录：git 全程走 -c http.extraheader 带 Basic 头；禁用 credential helper、
  禁止终端交互与凭据管理器弹窗（GIT_TERMINAL_PROMPT=0 / GCM_INTERACTIVE=never）。
- Token 不写 remote url、不进 .git/config、不落盘、不打印；token.txt 永不入仓。
- GITHUB_REPO / GITHUB_REMOTE / GITHUB_API_BASE 仅用于本地自检，正常使用一个都不用设。

推送清单以 manifest.json 为单一真值源。历史上曾把允许的模块名写死在
ALLOWED_MODULES/ALLOWED_SOURCES/ALLOWED_TESTS 里，新增 appendix.py / rules.py /
sources/inbox.py / tests/conftest.py 时忘了同步，结果推上去的 tree 缺件，
CI 报 `cannot import name 'appendix'`。现在改为「磁盘实际文件 − 明确排除名单」，
并在 push 前、push 后各做一次必需清单核对，缺一个就拒绝推送。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO_NAME = "get-news"                       # 既定项目名，不需要用户输入
API_BASE = os.environ.get("GITHUB_API_BASE", "https://api.github.com").rstrip("/")


def load_manifest() -> dict:
    p = ROOT / "manifest.json"
    if not p.is_file():
        raise RuntimeError("缺 manifest.json（推送清单单一真值源），拒绝推送半个包")
    return json.loads(p.read_text(encoding="utf-8"))


def run(*a: str, check: bool = True, text: bool = True,
        timeout: float = 90.0) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e["GIT_TERMINAL_PROMPT"] = "0"           # 任何情况下都不弹「输入用户名/密码」
    e["GCM_INTERACTIVE"] = "never"           # Windows Git Credential Manager 不弹登录窗
    # git push/fetch 卡住时必须有上限：否则用户双击 deploy.bat 会永远挂在那，
    # 窗口不报错也不结束。90s 对 42 文件 / 真实 GitHub 仓库都足够。
    # receive-pack 孙进程继承 pipe 不关 fd 会挂到超时；改用临时文件 fd。
    import tempfile
    try:
        with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
            p = subprocess.run(a, cwd=str(ROOT), stdout=fo, stderr=fe,
                               env=e, timeout=timeout)
            fo.seek(0); fe.seek(0)
            so, se = fo.read(), fe.read()
    except subprocess.TimeoutExpired as ex:
        raise RuntimeError(
            f"{a[0]} 超时 {timeout}s（网络不可达或 git 子进程挂起），已中止部署；"
            "请检查网络/代理后重跑 deploy.bat") from ex
    if text:
        so = so.decode("utf-8", "replace"); se = se.decode("utf-8", "replace")
    p = subprocess.CompletedProcess(a, p.returncode, so, se)
    if check and p.returncode:
        raise RuntimeError(f"{a[0]} rc={p.returncode}\n{p.stdout}{p.stderr}")
    return p


def push_files(m: dict) -> list[str]:
    """推 manifest.required ∩ 磁盘实际文件，再排除明确的残留文件名。

    不再用「允许的模块名集合」：那种写法每加一个模块都要人肉同步，漏一次
    就把半个包推上仓库。现在只有一份 deny_names（旧包遗留、引用已删除模块
    的文件名），新增文件自动进 tree。
    """
    skip = set(m["skip_dirs"])
    deny_suffix = tuple(m["deny_suffix"])
    deny_names = set(m["deny_names"])
    required = set(m["required"])
    out = set()
    for p in ROOT.rglob("*"):
        if not p.is_file() or any(x in p.parts for x in skip):
            continue
        if any(x.endswith((".egg-info", ".dist-info")) for x in p.parts):
            continue
        if p.suffix in deny_suffix or p.name in deny_names:
            continue
        rel = p.relative_to(ROOT).as_posix()
        # 只推 manifest 声明过的文件；用户目录里叠放的其它旧文件一律不带。
        if rel in required:
            out.add(rel)
    return sorted(out)


def preflight(m: dict, files: list[str]) -> list[str]:
    """推送前自检：manifest 的每一项都必须在推送清单里。"""
    return [r for r in m["required"] if r not in files]


def normalize_deploy_bat() -> tuple[int, int]:
    """推送前把 deploy.bat 统一成纯 ASCII + CRLF（幂等）。

    不依赖用户解压工具、网页上传或 Git 配置：磁盘上的 bat 可能是 LF，
    推上去后 runner checkout 又会按 core.autocrlf 再变一次。这里保证
    「推之前」是合规的；最终各平台拿到 CRLF 由 .gitattributes 的
    `*.bat text eol=crlf` 与 workflow 的字节级规范化步共同保证。
    """
    f = ROOT / "deploy.bat"
    raw = f.read_bytes()
    text = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n").decode("ascii")
    data = text.replace("\n", "\r\n").encode("ascii")
    before = (raw.count(b"\r\n"), raw.count(b"\n") - raw.count(b"\r\n"))
    if data != raw:
        f.write_bytes(data)
    after = (data.count(b"\r\n"), data.count(b"\n") - data.count(b"\r\n"))
    print(f"[部署] deploy.bat 字节规范化：CRLF {before[0]}→{after[0]} / "
          f"裸 LF {before[1]}→{after[1]} / 纯 ASCII / 零 &&")
    # 硬断言：CI checkout 后调用本函数时，若仍有混合换行/非 ASCII/&&，直接红。
    assert after[0] >= 20 and after[1] == 0, f"deploy.bat 不合规：{after}"
    assert all(x < 128 for x in data) and b"&&" not in data, "deploy.bat 非 ASCII 或含 &&"
    return after


def hint_if_not_unzipped() -> None:
    """网页上传 zip / 没解压就双击时给明确指引（只提示，不判失败）。

    GitHub 网页 "Add file -> Upload files" 把 .zip 当成一个普通附件，
    既不解压也不建目录；含非 ASCII 名的文件还会报
    "Something went really wrong, and we can't process that file."
    """
    zips = sorted(x.name for x in ROOT.glob("*.zip") if x.is_file())
    if zips and not (ROOT / "manifest.json").is_file():
        print("[提示] 当前目录只有 .zip、没有 manifest.json：请先解压再运行 deploy.bat。")
        print("[提示] 不要把 .zip 拖到 GitHub 网页 Upload files，它不会解压、不会建目录，"
              "workflow 与 Pages 仍然跑不起来。")
    elif zips:
        print(f"[提示] 目录内另有 {len(zips)} 个 .zip，仅作本地留存，不会推送；"
              "网页上传 zip 无效，请用本脚本按原目录结构推送。")


def verify_tree(m: dict) -> list[str]:
    """推送后自检：从新提交的 tree 里核对，而不是看本地目录。

    中文文件名（历史版本的交付说明.md）在 Windows 上有两个坑，缺一就误报「没进新提交」：
      ① git 默认把非 ASCII 路径转义成八进制 → 用 `-z` 拿原始字节，不做任何引号转义；
      ② text=True 会让 Python 按系统 locale（中文 Windows 常为 GBK）解码，
         而 git 输出的是 UTF-8 → 中文名变乱码。这里显式按 UTF-8 解码 bytes。
    兜底：万一某平台还有编码差异，用 `git cat-file -e HEAD:<path>` 直接查对象
    （只看 returncode，完全不解析文件名输出），杜绝把误报当成部署失败。
    """
    p = run("git", "-c", "core.quotepath=false",
            "ls-tree", "-r", "-z", "--name-only", "HEAD", text=False)
    raw: bytes = p.stdout
    have = {x.decode("utf-8", "replace") for x in raw.split(b"\x00") if x}

    missing = [r for r in m["required"] if r not in have]
    if not missing:
        return []
    still = []
    for r in missing:
        q = run("git", "cat-file", "-e", f"HEAD:{r}", check=False)
        if q.returncode != 0:
            still.append(r)
    return still


def verify_head_state() -> list[str]:
    """推送后读 HEAD 本身，确认旧版残留已清、workflow 已换成双 job 版。

    截图 33 的两个 CI FAIL 都来自「必需文件齐备，但 HEAD 里还留着旧
    交付说明.md 与单 job daily.yml」。verify_tree 只查 required 是否都在，
    查不出这些「多出来的旧文件」，所以必须单独看整棵 tree 与文件内容。
    """
    problems: list[str] = []
    p = run("git", "-c", "core.quotepath=false", "ls-tree", "-r", "-z",
            "--name-only", "HEAD", text=False)
    raw: bytes = p.stdout
    paths = [x.decode("utf-8", "replace") for x in raw.split(b"\x00") if x]
    legacy_names = {"交付说明.md", "test_contract.py", "contract.py",
                    "quality.py", "workbook.py"}
    stale = [x for x in paths
             if not x.isascii() or Path(x).name in legacy_names]
    if stale:
        problems.append("HEAD tree 含旧版残留：" + "、".join(stale[:8]))

    q = run("git", "show", "HEAD:.github/workflows/daily.yml", text=False,
            check=False)
    wf = q.stdout.decode("utf-8", "replace") if q.returncode == 0 else ""
    if "deploy:" not in wf or "needs: build" not in wf:
        problems.append("HEAD 的 daily.yml 不是 build/deploy 双 job 版"
                        "（推送未生效或仓库仍是旧版）")
    if "清理旧版残留文件" not in wf:
        problems.append("HEAD 的 daily.yml 缺少旧残留清理步（不是最新包）")
    # 截图 34：光「文件齐备」挡不住 Git 换行规范化。必须确认
    # .gitattributes 与 workflow 字节自愈步都在 HEAD 里，否则 CI 上
    # deploy.bat 会变纯 LF，旧的「必须 CRLF」断言直接 0 >= 20 红。
    ga = run("git", "show", "HEAD:.gitattributes", text=False, check=False)
    ga_txt = ga.stdout.decode("utf-8", "replace") if ga.returncode == 0 else ""
    if "*.bat text eol=crlf" not in ga_txt:
        problems.append("HEAD 缺 .gitattributes 的 `*.bat text eol=crlf`"
                        "（Git 会把 bat 规范成 LF 随平台漂移）")
    if "规范化 deploy.bat 换行" not in wf:
        problems.append("HEAD 的 daily.yml 缺少 deploy.bat 字节级自愈步"
                        "（CI checkout 后 bat 可能仍是 LF）")
    # 截图 42：CI 按「触发时的 workflow 定义」执行，改过文件后第一次运行
    # checkout 到的 daily.yml 仍是旧定义（无 inputs / --window-start）。
    # runtime 测试不能因此判红，但**推送时**必须确认 HEAD 已经是新版，
    # 否则下一次 Run workflow 永远填不了窗口。
    try:
        from newsbrief.workflow_contract import head_is_new
        new_ok, missing = head_is_new(wf)
        if not new_ok:
            problems.append("HEAD 的 daily.yml 不是支持时间窗口的新版，缺："
                            + "、".join(missing) + "（推送未生效或仓库仍是旧版）")
    except ImportError:
        if "window_start:" not in wf or "--window-start" not in wf:
            problems.append("HEAD 的 daily.yml 缺时间窗口定义（不是最新包）")
    return problems


def gh(method: str, path: str, token: str,
       payload: dict | None = None) -> tuple[int, dict]:
    """GitHub REST 调用；只用于「认出 owner」和「必要时自动建仓」。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(API_BASE + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "newsbrief-deploy")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, json.loads(raw or "{}")
    except urllib.error.HTTPError as ex:
        try:
            body = json.loads(ex.read().decode("utf-8", "replace") or "{}")
        except Exception:
            body = {}
        return ex.code, body
    except Exception as ex:
        return 0, {"message": f"{type(ex).__name__}: {ex}"}


def _pages_manual_hint(repo: str) -> str:
    return (
        f"\n  一次性做法（任选其一，之后永久生效）：\n"
        f"  A) 终端执行： gh api -X POST repos/{repo}/pages -f build_type=workflow\n"
        f"  B) 网页： 仓库 Settings → Pages → Build and deployment → Source 选 GitHub Actions → Save\n"
        f"  C) 换 classic PAT（勾 repo）或 fine-grained PAT（勾 Pages: write / 管理 Pages 设置），"
        f"重新双击 deploy.bat\n"
        f"  组织策略禁止或私有仓 GitHub Free 套餐无 Pages 时，只能走 B / 联系管理员。")


def ensure_pages_enabled(repo: str, token: str) -> tuple[bool, str]:
    """用同一个 Token 把 Pages 站点创建/切成 GitHub Actions 源（幂等）。

    截图 43：`actions/configure-pages@v5` 只会 GET /repos/{o}/{r}/pages，站点
    从未创建过就 404 失败。而工作流里的 GITHUB_TOKEN 没有 administration
    作用域，官方 action.yml 的 `enablement: true` 在默认 token 下会变成
    「Create Pages site failed. Resource not accessible by integration」——
    加参数反而把 404 换成 403，社区与官方 issue 均如此。

    所以创建站点这一步必须放在**用户侧、用他自己的 PAT**做：deploy.bat 仍然
    只粘一次 Token、免登录、不需要打开网页。推送后、触发 workflow 之前执行，
    避免制造一次必红的 CI 运行。
    """
    path = f"/repos/{repo}/pages"
    code, site = gh("GET", path, token)
    if code == 200:
        bt = str(site.get("build_type") or "")
        if bt == "workflow":
            return True, "已启用（build_type=workflow）"
        # 站点存在但来源是分支部署：切到 workflow，否则 deploy-pages 仍会红
        code2, _ = gh("PUT", path, token, {"build_type": "workflow"})
        if code2 in (200, 201, 204):
            return True, f"已从 {bt or 'legacy'} 切换为 GitHub Actions 源"
        return False, (f"Pages 站点已存在但 build_type={bt!r}，切换返回 HTTP {code2}；"
                       + _pages_manual_hint(repo))
    if code == 404:
        code2, body = gh("POST", path, token, {"build_type": "workflow"})
        if code2 in (200, 201, 204):
            return True, "已自动创建 Pages 站点（GitHub Actions 源）"
        return False, (f"创建 Pages 站点失败 HTTP {code2} "
                       f"{body.get('message', '')}；" + _pages_manual_hint(repo))
    if code in (401, 403):
        return False, (f"读取 Pages 站点被拒 HTTP {code}；" + _pages_manual_hint(repo))
    return False, (f"读取 Pages 站点异常 HTTP {code} "
                   f"{site.get('message', '')}；" + _pages_manual_hint(repo))


def get_token() -> str:
    """唯一需要用户提供的东西：环境变量 → 同目录 token.txt。绝不调用 input()。"""
    t = os.environ.get("GITHUB_TOKEN", "").strip()
    if not t:
        f = ROOT / "token.txt"
        if f.exists():
            t = f.read_text(encoding="utf-8", errors="replace").strip()
    return t


def resolve_repo(token: str) -> tuple[str, str] | None:
    """只靠 Token 推断 {owner}/get-news，并在仓库不存在时自动创建。"""
    override = os.environ.get("GITHUB_REPO", "").strip()   # 仅自检用
    if override:
        return override, "显式指定（自检用）"
    st, me = gh("GET", "/user", token)
    if st != 200 or not me.get("login"):
        print(f"[部署] Token 校验失败（GET /user → {st}）："
              "检查 Token 是否有效、是否带 repo 权限。", file=sys.stderr)
        return None
    owner = str(me["login"])
    repo = f"{owner}/{REPO_NAME}"
    note = f"由 Token 自动识别 owner={owner}，无需输入仓库名"
    st2, _ = gh("GET", f"/repos/{repo}", token)
    if st2 == 404:
        st3, _ = gh("POST", "/user/repos", token,
                    {"name": REPO_NAME, "private": False, "auto_init": False,
                     "description": "每日 20:00 大众向要闻简报"})
        if st3 in (201, 202):
            note += "；仓库不存在，已用同一 Token 自动创建（无需网页登录）"
        elif st3 == 422:
            note += "；仓库已存在（422），直接推送"
        else:
            note += f"；自动建仓返回 {st3}，仍尝试直接推送（仓库可能已存在）"
    elif st2 != 200:
        note += f"；仓库状态查询 {st2}，仍尝试直接推送"
    return repo, note


def main() -> int:
    normalize_deploy_bat()
    hint_if_not_unzipped()
    try:
        m = load_manifest()
    except Exception as ex:
        print(f"[部署] {ex}", file=sys.stderr)
        return 1

    token = get_token()
    if not token:
        print("[部署] 未取得 GitHub Token。三种任选其一（都只需要 Token 本身）：\n"
              "  1) 双击 deploy.bat 后粘贴一次 Token\n"
              "  2) 在 deploy.bat 同目录放一个 token.txt，里面只写 Token\n"
              "  3) 先 set GITHUB_TOKEN=... 再双击\n"
              "除此之外不需要仓库名、不需要登录 GitHub、不需要配置 git 账号。",
              file=sys.stderr)
        return 1
    resolved = resolve_repo(token)
    if resolved is None:
        return 1
    repo, note = resolved
    remote = os.environ.get("GITHUB_REMOTE") or f"https://github.com/{repo}.git"
    print(f"[部署] 目标仓库 {repo}（{note}）")
    print(f"[部署] Token 已取得 {len(token)} 位 {token[:4]}****；"
          "不落盘、不进 remote、不打印明文")

    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()

    def git_auth(*a: str, check: bool = True) -> subprocess.CompletedProcess:
        # credential.helper= 清空凭据助手：认证只走 extraheader，绝不弹登录框
        return run("git", "-c", "credential.helper=",
                   "-c", f"http.extraheader=AUTHORIZATION: Basic {auth}",
                   "-c", "http.version=HTTP/1.1", *a, check=check)

    run("git", "init", "-b", "main")
    run("git", "config", "user.name", "newsbrief-bot")
    run("git", "config", "user.email", "bot@users.noreply.github.com")
    # 重复双击时，index 里可能还留着上一次 merge 带进来的旧文件，先彻底清空。
    run("git", "read-tree", "--empty")
    files = [r for r in push_files(m) if (ROOT / r).is_file()]
    missing = preflight(m, files)
    if missing:
        print("[部署] 推送前自检不通过，以下必需文件不在推送清单中，拒绝推送：\n  "
              + "\n  ".join(missing)
              + "\n请整体替换 newsbrief/ 目录（不要只拷改动过的几个文件），"
                "并带上 data/、tests/、manifest.json。", file=sys.stderr)
        return 1
    print(f"[部署] 推送前自检通过：{len(files)} 个文件全部齐备（manifest 单一真值源）")
    run("git", "add", "--", *files)
    run("git", "remote", "remove", "origin", check=False)
    run("git", "remote", "add", "origin", remote)

    # 远端历史只作为「父提交」保留，绝不把远端独有的文件并进工作区。
    # 旧做法 merge -X ours 只在冲突时选 ours，远端残留的 test_contract.py
    # 会被原样带进 HEAD，CI 的 pytest 收集阶段直接 ModuleNotFoundError。
    parent, remote_tree = None, None
    f = git_auth("fetch", "origin", "main", check=False)
    if f.returncode == 0 and run("git", "rev-parse", "--verify", "FETCH_HEAD",
                                 check=False).returncode == 0:
        parent = run("git", "rev-parse", "FETCH_HEAD").stdout.strip()
        remote_tree = run("git", "rev-parse", "FETCH_HEAD^{tree}",
                          check=False).stdout.strip() or None
        # 页面与固定下载地址：把远端已有的这三个 blob 挂进新 tree，
        # 避免推完到 CI 跑完之间 Pages 404；旧源码/旧测试一律不带。
        for rel in m["keep_docs"]:
            lb = run("git", "rev-parse", f"FETCH_HEAD:{rel}", check=False)
            if lb.returncode == 0:
                run("git", "update-index", "--add", "--cacheinfo", "100644",
                    lb.stdout.strip(), rel, check=False)
        print("[部署] 已保留远端历史为父提交（不合并远端旧文件；页面与下载地址留存）")

    tree = run("git", "write-tree").stdout.strip()
    if remote_tree and remote_tree == tree:
        # 本地 HEAD 也要指向远端那条 commit，否则后面的 verify_head_state()
        # 会读到上一次的旧 commit，把「幂等跳过」误判成「推送未生效」。
        run("git", "update-ref", "refs/heads/main", parent)
        print("[部署] 仓库已是本版内容，跳过提交（幂等，不产生空提交）")
    else:
        args = ["git", "commit-tree", tree]
        if parent:
            args += ["-p", parent]
        args += ["-m", "chore: newsbrief lite"]
        sha = run(*args).stdout.strip()
        run("git", "update-ref", "refs/heads/main", sha)
        p = git_auth("push", "-u", "origin", "main", check=False)
        if p.returncode:
            print("[部署] 推送失败：检查 Token 的 repo 权限与网络。", file=sys.stderr)
            return 1

    # 推送后从 tree 复核（而不是看本地目录）：少一个文件就当场失败。
    lost = verify_tree(m)
    if lost:
        print("[部署] 推送后 tree 自检不通过，以下文件没进新提交：\n  "
              + "\n  ".join(lost), file=sys.stderr)
        return 1
    print(f"[部署] 推送后 tree 自检通过：{len(m['required'])} 项必需文件全部在 HEAD")

    # 更强的一层：光「必需文件齐备」不够。旧版仓库可能同时留着
    # 交付说明.md 与单 job 的 daily.yml，pytest 收集时就会红（截图 33）。
    # 这里直接读 HEAD 的 tree 与 workflow 内容，确认新版本真的换上去了。
    stale = verify_head_state()
    if stale:
        print("[部署] 推送后状态自检不通过：\n  " + "\n  ".join(stale)
              + "\n请确认 push 成功、Token 有 repo 权限，并用最新包重新运行 deploy.bat。",
              file=sys.stderr)
        return 1
    print("[部署] 推送后状态自检通过：HEAD 无旧版残留，daily.yml 为 build/deploy 双 job")

    # Pages 站点必须先存在：configure-pages@v5 只 GET 不创建，GITHUB_TOKEN
    # 又没有 administration 权限去 enablement。用用户自己的 Token 在这里
    # 一次性补上，deploy.bat 依旧只粘一次 Token、免登录（截图 43 根因）。
    ok_pages, note_pages = ensure_pages_enabled(repo, token)
    if not ok_pages:
        print("[部署] ⚠ Pages 站点未就绪：" + note_pages, file=sys.stderr)
        print("[部署] 代码已推送成功；为避免 CI 必红，本次不自动触发 workflow。"
              "完成上面任意一项后重新双击 deploy.bat（幂等跳过推送、只补触发），"
              "或到 Actions 页点一次 Run workflow。", file=sys.stderr)
        return 0
    print(f"[部署] Pages 站点就绪：{note_pages}")

    # 推完自动跑一次：不触发的话页面会一直挂着远端旧 news-data.js，
    # 用户「重新部署了」却看不到任何数据变化。
    ds, _ = gh("POST", f"/repos/{repo}/actions/workflows/daily.yml/dispatches",
               token, {"ref": "main"})
    if ds in (200, 201, 204):
        print("[部署] 已自动触发一次 workflow；约 1–3 分钟后页面与 Excel 同步更新")
    else:
        print(f"[部署] 自动触发返回 {ds}（Token 可能缺 workflow 权限）："
              "请到仓库 Actions → daily-newsbrief → Run workflow 点一次；"
              "代码已推送成功，不影响部署结果", file=sys.stderr)
    print("[部署] 一键部署完成；此后每天 20:00 自动出表并发布页面")
    return 0


if __name__ == "__main__":
    sys.exit(main())
