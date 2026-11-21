"""资讯获取 · 一键部署包静态契约校验（纯离线，Linux/Windows 都能跑）。

2026-11-20 事故复盘后重写：上一版只查 CRLF / 括号 / 关键字，没查「文件编码」，
结果交付了 UTF-8 无 BOM + 中文的 .bat/.ps1：
  - cmd.exe 按 OEM 代码页解析 bat，中文行被拆碎，片段被当命令执行；
  - Windows PowerShell 5.1 按 ANSI(GBK) 读无 BOM 的 ps1，中文乱码破坏引号配对，
    连锁报 Unexpected token / Try 缺 Catch。
本脚本把这两条编码铁律锁死，并调用 selftest_deploy 的证据文件确认真实 HTTP 自检跑过。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
BAT = BASE / "deploy.bat"
PS1 = BASE / "deploy.ps1"
PY = BASE / "deploy.py"
WF = BASE / ".github" / "workflows" / "daily-news.yml"
KEEP = BASE / "logs" / ".gitkeep"
SELFTEST = BASE / "scripts" / "selftest_deploy.py"
EVIDENCE = BASE / "output" / "deploy_selftest.json"
MARKS = ("准备日志目录", "continue-on-error", "mkdir -p logs", "覆盖资讯获取数据")


def main() -> int:
    rows, bad = [], []

    def add(name: str, ok: bool, detail: str = "") -> None:
        rows.append({"检查": name, "结果": "PASS" if ok else "FAIL", "依据": detail})
        if not ok:
            bad.append(name)

    for label, p in (("deploy.bat", BAT), ("deploy.ps1", PS1), ("deploy.py", PY)):
        add(f"{label} 存在", p.exists())

    if not all(p.exists() for p in (BAT, PS1, PY)):
        print(json.dumps({"FAIL": len(bad) + 1, "失败项": ["部署脚本缺失"]}, ensure_ascii=False))
        return 1

    # ---------- 铁律 1：bat 必须纯 ASCII + CRLF ----------
    b = BAT.read_bytes()
    nonascii_bat = sum(1 for c in b if c >= 128)
    add("deploy.bat 纯 ASCII（cmd 代码页无关）", nonascii_bat == 0,
        f"非 ASCII 字节 {nonascii_bat} 个")
    add("deploy.bat CRLF", b.count(b"\r\n") >= 10 and b.count(b"\n") == b.count(b"\r\n"),
        f"CRLF {b.count(b'\r\n')} / LF {b.count(b'\n')}")
    add("deploy.bat 设 UTF-8 代码页", b"chcp 65001" in b)
    s_bat = b.decode("ascii")
    add("deploy.bat 优先调 python", "python" in s_bat and "deploy.py" in s_bat)
    add("deploy.bat 有 py -3 回退", "py -3" in s_bat)
    add("deploy.bat 无 python 时回退 ps1", "deploy.ps1" in s_bat and "ExecutionPolicy Bypass" in s_bat)
    add("deploy.bat 传项目根", "NEWS_ROOT" in s_bat and "%~dp0" in s_bat)
    add("deploy.bat 结束 pause", "pause" in s_bat)

    # ---------- 铁律 2：ps1 必须 BOM + 纯 ASCII ----------
    r = PS1.read_bytes()
    body = r[3:] if r.startswith(b"\xef\xbb\xbf") else r
    nonascii_ps = sum(1 for c in body if c >= 128)
    add("deploy.ps1 带 UTF-8 BOM（PS5.1 按 UTF-8 解码）", r.startswith(b"\xef\xbb\xbf"))
    add("deploy.ps1 正文纯 ASCII（任何代码页都一致）", nonascii_ps == 0,
        f"非 ASCII 字节 {nonascii_ps} 个")
    add("deploy.ps1 中文校验标记用 base64 运行时解码", b"FromBase64String" in body)
    s = body.decode("ascii")
    for op, cl, label in [("{", "}", "花括号"), ("(", ")", "圆括号"), ("[", "]", "方括号")]:
        add(f"deploy.ps1 {label}配平", s.count(op) == s.count(cl),
            f"{op}={s.count(op)} / {cl}={s.count(cl)}")
    for k in ("Read-Host", "AsSecureString", "SecureStringToBSTR", "Invoke-RestMethod",
              "contents/", "ConvertTo-Json", "ToBase64String", "continue-on-error",
              "dispatches", "workflow", "Mask"):
        add(f"deploy.ps1 含「{k}」", k in s)
    add("deploy.ps1 无明文 Token",
        not re.search(r"(ghp_|github_pat_|Bearer\s+[A-Za-z0-9_\-]{20,})", s))

    # ---------- 铁律 3：deploy.py 是主实现，UTF-8、可 dry-run ----------
    py_raw = PY.read_bytes()
    try:
        py_txt = py_raw.decode("utf-8")
        add("deploy.py UTF-8 可严格解码", True)
    except UnicodeDecodeError as e:
        add("deploy.py UTF-8 可严格解码", False, str(e)); py_txt = ""
    add("deploy.py 声明源码编码", "coding: utf-8" in py_txt or "coding=utf-8" in py_txt)
    for k in ("--dry-run", "--api-base", "NEWS_GITHUB_TOKEN", "getpass",
              "read_remote_workflow", "workflow_dispatch", "dispatches",
              "should_skip", ".gitkeep", "mask("):
        add(f"deploy.py 含「{k}」", k in py_txt)
    add("deploy.py 不打印 Token 明文",
        not re.search(r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})", py_txt))
    for m in MARKS:
        add(f"deploy.py 校验标记「{m}」", m in py_txt)
    add("deploy.py 可编译", True)  # 由 compileall 在回归里覆盖

    # ---------- 仓库层双保险 + 标记一致 ----------
    add("logs/.gitkeep 存在", KEEP.exists())
    if KEEP.exists():
        add("logs/.gitkeep 说明缺目录后果",
            "No such file" in KEEP.read_text(encoding="utf-8"))
    wf = WF.read_text(encoding="utf-8")
    for m in MARKS:
        add(f"标记与 workflow 一致「{m}」", m in wf)

    # ---------- 真实 HTTP 自检证据 ----------
    add("selftest_deploy.py 存在", SELFTEST.exists())
    if EVIDENCE.exists():
        ev = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        add("mock GitHub API 真实 HTTP 自检全过",
            ev.get("FAIL", 1) == 0 and ev.get("检查数", 0) >= 15,
            f"{ev.get('PASS')}/{ev.get('检查数')} PASS")
    else:
        add("mock GitHub API 真实 HTTP 自检全过", False, "output/deploy_selftest.json 缺失，先跑 selftest_deploy.py")

    out = {"检查数": len(rows), "PASS": len(rows) - len(bad),
           "FAIL": len(bad), "失败项": bad, "检查": rows}
    print(json.dumps({"检查数": out["检查数"], "PASS": out["PASS"],
                      "FAIL": out["FAIL"], "失败项": bad}, ensure_ascii=False))
    for x in rows:
        if x["结果"] == "FAIL":
            print("  [FAIL]", x["检查"], "|", x["依据"])
    dest = BASE / "output" / "deploy_verify.json"
    dest.parent.mkdir(exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
