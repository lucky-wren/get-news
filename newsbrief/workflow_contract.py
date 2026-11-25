"""workflow 契约的单一真值源：给 pytest / verify_package / deploy 共用。

背景（截图 42 两条 CI FAIL）：上一版把「`.github/workflows/daily.yml` 里必须
含 `window_start:` / `--window-start` 字面量」写成了硬断言。但 GitHub Actions
按**触发该次运行时的 workflow 定义**执行：改过 workflow 之后第一次运行，
`actions/checkout` 取到的仍是旧定义（无 inputs、采集步骤是单行
`python -m newsbrief --mode live`）。于是「零网络自检」在这个必然出现的场景
里自己判自己 FAIL。

契约拆两层：
  - **代码契约（永远硬校验）**：采集步骤必须是生产路径（含
    `python -m newsbrief`、不含 `--replay-capture`）；诊断回放不得覆盖线上
    产物。这跟 workflow 版本无关，是真正要钉死的东西。
  - **版本相关（有则校验、无则允许）**：`workflow_dispatch.inputs` 定义过
    窗口就必须把值传给 CLI；旧定义没有 inputs 时允许缺省，由
    `deploy.verify_head_state()` 在**推送时**强校验 HEAD 是新版，而不是让
    runtime pytest 去判红。
"""
from __future__ import annotations

import re
from pathlib import Path

WINDOW_KEYS = ("window_start", "window_end")
WINDOW_FLAGS = ("--window-start", "--window-end")


def _parse_yaml(text: str) -> dict:
    try:
        import yaml
        d = yaml.safe_load(text)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _text_fallback(text: str) -> dict:
    """无 PyYAML 时的保守兜底：只认字面量，不误判。"""
    disp = re.search(r"^\s*workflow_dispatch:\s*$", text, re.M)
    inputs = []
    if disp:
        tail = text[disp.end():]
        for k in WINDOW_KEYS:
            if re.search(r"^\s+" + k + r":\s*$", tail, re.M):
                inputs.append(k)
    run = ""
    m = re.search(r"^\s*run:\s*\|?\s*$", text, re.M)
    if m:
        run = text[m.end():m.end() + 3000]
    else:
        run = "\n".join(l for l in text.splitlines()
                        if "python -m newsbrief" in l or "--window-" in l)
    return {"_fallback": True, "inputs": inputs, "run": run}


def check(text: str) -> dict:
    """返回契约检查结果；`ok` 为 False 才应判失败。"""
    d = _parse_yaml(text)
    if not d:
        fb = _text_fallback(text)
        inputs = list(fb["inputs"])
        run = fb["run"]
        has_collect = "python -m newsbrief" in run
    else:
        on = d.get("on", d.get(True, {})) or {}
        if not isinstance(on, dict):
            on = {}
        disp = on.get("workflow_dispatch") or {}
        ins = disp.get("inputs") or {} if isinstance(disp, dict) else {}
        inputs = list(ins) if isinstance(ins, dict) else []
        steps = (((d.get("jobs") or {}).get("build") or {}).get("steps") or [])
        collect = next(
            (s for s in steps
             if isinstance(s, dict) and "python -m newsbrief" in str(s.get("run") or "")),
            None)
        has_collect = collect is not None
        run = str((collect or {}).get("run") or "")

    production = ("python -m newsbrief" in run
                  and "--replay-capture" not in run)
    old_definition = not any(k in inputs for k in WINDOW_KEYS)
    # 新定义：inputs 齐备且采集步骤把值传给 CLI；旧定义：允许缺省（不判红）
    window_wired = (old_definition
                    or (all(k in inputs for k in WINDOW_KEYS)
                        and all(f in run for f in WINDOW_FLAGS)))
    return {
        "ok": has_collect and production and window_wired,
        "has_collect": has_collect,
        "production": production,
        "run": run,
        "inputs": inputs,
        "old_definition": old_definition,
        "window_wired": window_wired,
        "note": ("旧 workflow 定义触发（无 inputs）：允许缺省，"
                 "由 deploy 推送时的 HEAD 强校验兜底"
                 if old_definition else "新定义：inputs 与 CLI 传参齐备"),
    }


def check_file(path: str | Path) -> dict:
    try:
        return check(Path(path).read_text(encoding="utf-8"))
    except OSError as ex:
        return {"ok": False, "has_collect": False, "production": False,
                "run": "", "inputs": [], "old_definition": True,
                "window_wired": False, "note": f"读取失败：{ex}"}


# 推送时（而非 CI runtime）必须确认 HEAD 已经是新版 workflow
REQUIRED_HEAD_LITERALS = ("window_start:", "window_end:",
                          "--window-start", "--window-end")


def head_is_new(text: str) -> tuple[bool, list[str]]:
    missing = [k for k in REQUIRED_HEAD_LITERALS if k not in (text or "")]
    return (not missing), missing
