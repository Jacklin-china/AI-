"""命令行入口：单轮 Agent 请求和最近调用记录；业务能力由内核提供。"""

from __future__ import annotations

import argparse
import io
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from kantoku.agent.context import build_context, estimate_tokens
from kantoku.agent.loop import run as run_agent
from kantoku.config import KantokuError, ToolError
from kantoku.config.logging_setup import setup_logging
from kantoku.core.budget import list_ledger
from kantoku.core.tracing import init_trace_table, recent_traces
from kantoku.memory import list_personas, parse_persona, save_persona
from kantoku.schemas.storyboard import parse_storyboard
from kantoku.tools.storyboard import load_storyboard


def _print_traces(limit: int) -> None:
    """打印最近记录，明确未知成本；这些是供应商调用记录而非账单。"""
    records = recent_traces(limit)
    if not records:
        print("暂无 trace 记录。")
    for record in records:
        cost = "未知" if record.cost_fen is None else f"{record.cost_fen} 分"
        status = "成功" if record.ok else "失败"
        if record.usage_reported is True:
            tokens = f"{record.in_tokens}/{record.out_tokens}（已报告）"
        elif record.usage_reported is False:
            tokens = "未知（供应商未报告有效用量）"
        else:
            tokens = "未知（历史记录或调用方未标记）"
        print(
            f"[trace] {record.ts} {record.kind} {status} {record.model} "
            f"耗时={record.latency_ms}ms "
            f"token={tokens} 成本={cost}"
        )


def _print_ledger(limit: int) -> None:
    """打印生图台账；未知实际费用不能显示为零。"""
    records = list_ledger(limit)
    if not records:
        print("暂无生图台账。")
        return
    for record in records:
        actual = "待对账" if record.actual_fen is None else f"{record.actual_fen} 分"
        provider_job = record.provider_job_id or "未返回"
        print(
            f"[ledger] 请求ID={record.reservation_id} "
            f"{record.created_at} {record.project}/{record.episode}/镜{record.shot_no} "
            f"状态={record.status} 预占={record.est_fen} 分 实际={actual} "
            f"模型={record.model} 供应商任务={provider_job}"
        )


def _ask(prompt: str) -> None:
    """发送一次有限步 Agent 请求；循环内部负责工具调用与防失控。"""
    print(run_agent(prompt))


def _print_context(episode: str, shot_no_text: str) -> None:
    """打印指定镜头的裁剪上下文和同口径 Token 近似对比。"""
    try:
        shot_no = int(shot_no_text)
    except ValueError:
        raise ToolError("镜号必须是正整数") from None
    if shot_no <= 0:
        raise ToolError("镜号必须是正整数")

    storyboard = load_storyboard(episode)
    if storyboard is None:
        raise ToolError("找不到指定剧集")
    shot = next((item for item in storyboard.shots if item.shot_no == shot_no), None)
    if shot is None:
        raise ToolError("找不到指定镜头")

    character_names = list(
        dict.fromkeys(name for item in storyboard.shots for name in item.characters)
    )
    full_payload = json.dumps(
        {
            "shots": [item.model_dump(mode="json") for item in storyboard.shots],
            "personas": [
                persona.model_dump(mode="json") for persona in list_personas(character_names)
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    cropped = build_context(shot, storyboard.shots)
    full_tokens = estimate_tokens(full_payload)
    cropped_tokens = estimate_tokens(cropped)
    reduction = 0.0 if full_tokens == 0 else 1 - cropped_tokens / full_tokens
    print(
        f"上下文裁剪（近似 Token，同口径比较）：{full_tokens} → {cropped_tokens}，"
        f"减少 {reduction:.1%}"
    )
    print(cropped)


def _interactive() -> None:
    print("监督酱 · 单轮问答；输入 exit/quit 退出，/traces 查看最近记录。")
    while True:
        try:
            prompt = input("监督酱 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            return
        if prompt.lower() in {"exit", "quit"}:
            return
        if not prompt:
            continue
        try:
            if prompt == "/traces":
                _print_traces(5)
            else:
                _ask(prompt)
        except KeyboardInterrupt:
            print("\n已中断等待。服务端任务状态仍可能未知，请勿立即重复提交。")
            return
        except KantokuError as error:
            print(f"错误：{error.message}", file=sys.stderr)


def run(argv: Sequence[str] | None = None) -> int:
    """返回退出码，便于脚本和测试调用；默认只展示用法，不发请求。"""
    parser = argparse.ArgumentParser(
        description="监督酱 Agent 命令行入口",
        epilog="单镜生图与对账：uv run python -m kantoku.shells.image_cli --help",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--chat", action="store_true", help="进入真实 API 单轮问答（可能计费）")
    action.add_argument("--ask", metavar="TEXT", help="真实 API 问答一次（可能计费）")
    action.add_argument("--traces", type=int, metavar="N", help="离线查看最近 N 条记录")
    action.add_argument("--ledger", type=int, metavar="N", help="离线查看最近 N 条生图台账")
    action.add_argument("--validate-storyboard", metavar="FILE", help="离线校验分镜 JSON")
    action.add_argument("--save-persona", metavar="FILE", help="离线校验并保存角色卡 JSON")
    action.add_argument("--personas", action="store_true", help="离线列出已保存的角色卡")
    action.add_argument(
        "--context",
        nargs=2,
        metavar=("EPISODE", "SHOT_NO"),
        help="离线显示指定镜头的裁剪上下文与近似 Token 对比",
    )
    args = parser.parse_args(argv)
    if not any(
        (
            args.chat,
            args.ask is not None,
            args.traces is not None,
            args.ledger is not None,
            args.validate_storyboard is not None,
            args.save_persona is not None,
            args.personas,
            args.context is not None,
        )
    ):
        parser.print_help()
        return 0
    if args.traces is not None and args.traces <= 0:
        parser.error("--traces 必须大于 0")
    if args.ledger is not None and args.ledger <= 0:
        parser.error("--ledger 必须大于 0")
    if args.ask is not None and not args.ask.strip():
        parser.error("--ask 不能为空")
    try:
        if args.validate_storyboard is not None:
            board = parse_storyboard(Path(args.validate_storyboard).read_text(encoding="utf-8"))
            print(f"分镜校验通过：{len(board.shots)} 镜")
            return 0
        if args.save_persona is not None:
            persona = parse_persona(Path(args.save_persona).read_text(encoding="utf-8"))
            save_persona(persona)
            print(f"角色卡已保存：{persona.name}")
            return 0
        setup_logging()
        init_trace_table()
        if args.traces is not None:
            _print_traces(args.traces)
        elif args.ledger is not None:
            _print_ledger(args.ledger)
        elif args.personas:
            personas = list_personas()
            if not personas:
                print("暂无角色卡。")
            for persona in personas:
                print(f"{persona.name}｜{persona.appearance}｜{persona.outfit}")
        elif args.context is not None:
            _print_context(args.context[0], args.context[1])
        elif args.ask is not None:
            _ask(args.ask)
        else:
            _interactive()
    except KantokuError as error:
        print(f"错误：{error.message}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("错误：无法读取输入文件或写入运行数据。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断。请先核对服务端状态再重试。", file=sys.stderr)
        return 130
    return 0


def main() -> None:
    """模块执行入口。"""
    # Windows 管道可能默认 GBK；统一标准流，避免重定向或终端工具看到乱码。
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(run())


if __name__ == "__main__":
    main()
