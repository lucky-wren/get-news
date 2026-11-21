#!/usr/bin/env python3
"""按 01_instruction 第六节 / 03 §5 质检清单做纯代码验收。

用法：
    python scripts/verify_spec.py [xlsx 路径]
    python scripts/verify_spec.py --json output/spec_verify.json

不调用任何模型；每条结论都来自对 xlsx 单元格的机械读取与复算。
退出码：0 = 全部 PASS，1 = 有 FAIL。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from news_pipeline.spec import verify_workbook, verify_extra, NO_LLM_DECLARATION  # noqa: E402
from news_pipeline import oss  # noqa: E402

DEFAULT = Path(__file__).resolve().parents[1] / "docs" / "downloads" / "latest.xlsx"


def main() -> int:
    ap = argparse.ArgumentParser(description="资讯获取 xlsx 纯代码规格验收")
    ap.add_argument("xlsx", nargs="?", default=str(DEFAULT), help="待验收的 xlsx 路径")
    ap.add_argument("--json", dest="json_out", default="", help="另写结构化结果 JSON")
    args = ap.parse_args()

    p = Path(args.xlsx)
    if not p.exists():
        print(f"FAIL | 找不到产物 {p}")
        return 1
    cfg_problems = oss.config_check({"rules": {"mass_strictness": "strict"},
                                     "scoring": {"lambda_": 6}, "orchestrate": {}})
    reg = Path(__file__).resolve().parents[1] / "output" / "rules_regression.json"
    if not reg.exists():
        import subprocess
        import sys
        subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "verify_rules.py")],
                       capture_output=True)
    result = verify_workbook(str(p))
    result["明细"] = list(result["明细"]) + verify_extra(str(p))
    result["检查项"] = len(result["明细"])
    result["通过"] = sum(1 for c in result["明细"] if c["结果"] == "PASS")
    result["失败"] = result["检查项"] - result["通过"]
    result["结论"] = "达标" if result["失败"] == 0 else "不达标"
    print("=" * 78)
    print(f"纯代码规格验收 | {p.name} | {oss.oss_summary()}")
    print("=" * 78)
    for c in result["明细"]:
        mark = "PASS" if c["结果"] == "PASS" else "FAIL"
        print(f"{mark} | {c['项']:<42s} | {c['证据']}")
    print("-" * 78)
    print(f"配置强类型校验：{'PASS（0 问题）' if not cfg_problems else cfg_problems}")
    print(f"结论：{result['结论']}  {result['通过']}/{result['检查项']} 项通过")
    print(NO_LLM_DECLARATION)
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({**result, "开源组件": oss.OSS_STATUS,
                                   "配置校验": cfg_problems},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"结构化结果 → {out}")
    return 0 if result["结论"] == "达标" and not cfg_problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
