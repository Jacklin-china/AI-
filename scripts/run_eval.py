"""命令行编排：运行分镜评测集并写入 Markdown 报告。"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from kantoku.config import KantokuError, ToolError, get_settings
from kantoku.config.settings import ROOT
from kantoku.evaluation import (
    load_consistency_suite,
    load_suite,
    render_consistency_report,
    render_report,
    run_consistency_suite,
    run_suite,
    write_report,
)
from kantoku.tools.storyboard import generate_storyboard


def run(argv: Sequence[str] | None = None) -> int:
    """解析参数并串行运行；limit 可用于先做低成本冒烟。"""
    parser = argparse.ArgumentParser(description="运行 M1 分镜生成评测（会调用真实模型）")
    parser.add_argument(
        "--kind",
        choices=("storyboard", "consistency"),
        default="storyboard",
        help="storyboard 会调用模型；consistency 仅离线检查 Prompt",
    )
    parser.add_argument(
        "--suite",
        type=Path,
        default=None,
        help="JSONL 评测集路径",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--limit", type=int, help="仅运行前 N 条，用于控制试跑成本")
    selection.add_argument("--case", action="append", help="只运行指定案例 ID；可重复传入")
    parser.add_argument("--output", type=Path, help="报告保存路径")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit 必须大于 0")

    try:
        if args.kind == "consistency":
            suite = args.suite or ROOT / "evals" / "consistency.jsonl"
            consistency_cases = load_consistency_suite(suite)
            if args.limit is not None:
                consistency_cases = consistency_cases[: args.limit]
            elif args.case:
                selected = set(args.case)
                unknown = selected - {case.id for case in consistency_cases}
                if unknown:
                    raise ToolError("找不到指定评测案例", detail="、".join(sorted(unknown)))
                consistency_cases = [
                    case for case in consistency_cases if case.id in selected
                ]
            consistency_results = run_consistency_suite(consistency_cases)
            report = render_consistency_report(
                cases=consistency_cases,
                results=consistency_results,
            )
            output = args.output or ROOT / "docs" / "evals" / f"M2-{date.today().isoformat()}.md"
            write_report(output, report)
            passed = sum(result.passed for result in consistency_results)
            print(f"评测完成：{passed}/{len(consistency_results)} 通过；报告：{output}")
            return 0 if passed == len(consistency_results) else 1

        suite = args.suite or ROOT / "evals" / "storyboard.jsonl"
        cases = load_suite(suite)
        if args.limit is not None:
            cases = cases[: args.limit]
        elif args.case:
            selected = set(args.case)
            unknown = selected - {case.id for case in cases}
            if unknown:
                raise ToolError("找不到指定评测案例", detail="、".join(sorted(unknown)))
            cases = [case for case in cases if case.id in selected]
        settings = get_settings()
        results = run_suite(cases, generate_storyboard)
        report = render_report(
            cases=cases,
            results=results,
            model_name=settings.llm.model_chat,
        )
        output = args.output or ROOT / "docs" / "evals" / f"M1-{date.today().isoformat()}.md"
        write_report(output, report)
        passed = sum(result.passed for result in results)
        print(f"评测完成：{passed}/{len(results)} 通过；报告：{output}")
        return 0 if passed == len(results) else 1
    except KantokuError as error:
        print(f"评测失败：{error.message}")
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
