"""单镜生图与对账入口：先预览，明确确认后提交，恢复时只查询。"""

from __future__ import annotations

import argparse
import io
import json
import sys
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path

from kantoku.config import BudgetError, ConfigError, KantokuError, ToolError, get_settings
from kantoku.core import budget
from kantoku.perception.review import get_rework_item
from kantoku.schemas.media import ImageGenerationResult
from kantoku.tools.image_batch import (
    ImageBatch,
    build_recipe_batch,
    generate_batch,
    parse_image_batch,
)
from kantoku.tools.image_gen import ImageProvider, gen_image, reconcile_image
from kantoku.tools.jimeng import VolcengineJimengProvider
from kantoku.tools.openai_image import AlibabaQwenImageProvider, OpenAIImageProvider


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("必须填写正整数") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("必须填写正整数")
    return value


def _money_fen(text: str) -> int:
    """人工账单输入以元表示，拒绝四舍五入改变原始金额。"""
    try:
        amount = Decimal(text)
        if not amount.is_finite() or amount < 0 or amount > Decimal(2**63 - 1) / 100:
            raise ValueError
        fen = amount * 100
        if fen != fen.to_integral_value():
            raise ValueError
        return int(fen)
    except (InvalidOperation, ValueError):
        raise argparse.ArgumentTypeError("费用须为非负人民币金额，精确到分，如 0.30") from None


def _provider() -> ImageProvider:
    provider = get_settings().image.provider
    if provider == "volcengine-jimeng":
        return VolcengineJimengProvider()
    if provider == "openai-gpt-image":
        return OpenAIImageProvider()
    if provider == "alibaba-qwen-image":
        return AlibabaQwenImageProvider()
    raise ConfigError("当前生图供应商没有可用适配器", detail=provider)


def _preflight() -> int:
    """只验证本地配置、凭据存在性和请求约束，不发网络请求。"""
    settings = get_settings()
    if not settings.image.force_single:
        raise ConfigError("生产生图要求 image.force_single=true")
    provider = _provider()
    provider.validate_request(
        prompt="preflight",
        shot_no=1,
        reference_urls=(),
        seed=None,
    )
    estimate = budget.estimate_image_fen()
    print("本地生图自检通过；没有提交任务，也没有产生费用。")
    print(
        f"供应商：{settings.image.provider}；模型：{settings.image.model}；"
        f"尺寸：{settings.image.width}x{settings.image.height}；单图模式：已开启。"
    )
    print(
        f"默认单次预占：{estimate} 分；项目上限："
        f"{settings.budget.image_project_cny} 元；账期：UTC"
        f"{settings.budget.accounting_utc_offset_hours:+d}。"
    )
    print("这只证明本地已就绪；服务开通状态和真实价格仍须用一个镜头验证。")
    return 0


def _doctor() -> int:
    """只执行供应商支持的免计费诊断，不提交或预占。"""
    provider = _provider()
    result = provider.check_access()
    label = "即梦签名" if get_settings().image.provider == "volcengine-jimeng" else "模型访问"
    if result.authenticated is False:
        detail = "; ".join(
            str(value) for value in (result.code, result.message, result.request_id) if value
        )
        raise ConfigError(f"{label}鉴权失败", detail=detail or None)
    if result.authenticated is None:
        print(f"{label}未被明确拒绝，但仍无法确认成功；没有创建任务、没有预占预算。")
    else:
        print(f"{label}鉴权通过；没有创建任务、没有预占预算。")
    if result.request_id:
        print(f"供应商诊断请求：{result.request_id}")
    if not result.service_ready:
        print(
            f"服务可用性尚未确认：code={result.code}；message={result.message}。"
            "不存在任务的查询不能代替一次真实小额生成。"
        )
        return 2
    print("查询接口已确认可用。")
    print("该检查不代表生图必定成功；真实生成仍须单独确认费用。")
    return 0


def _print_result(request_id: str, result: ImageGenerationResult) -> int:
    print(f"请求 ID：{request_id}；状态：{result.status}")
    if result.provider_job_id:
        print(f"供应商任务：{result.provider_job_id}")
    if result.path is not None:
        print(f"图片：{result.path.resolve()}")
        if not result.path.is_file():
            print("原图片文件已缺失，请核对原任务文件；没有自动重新生成。")
            return 1
    if result.actual_fen is None:
        print("实际费用：待对账，预占仍保留。")
    else:
        print(f"实际费用：{result.actual_fen} 分")
    if result.error:
        print(f"原因：{result.error}")
    if result.status == "unknown":
        print("请使用 query 查询同一请求；不要更换请求 ID 再次生成。")
        return 2
    if result.status == "failed":
        print("任务未取得可用图片；请核对账单后再决定是否返工。")
        return 1
    return 0


def _generate(args: argparse.Namespace) -> int:
    settings = get_settings()
    prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    if not prompt or len(prompt) > settings.image.prompt_max_chars:
        raise ToolError(f"提示词须为 1 至 {settings.image.prompt_max_chars} 个字符")
    if not settings.image.force_single:
        raise ConfigError("单镜入口要求 image.force_single=true，请先修正配置")
    if args.seed is not None and args.seed < 0:
        raise ToolError("seed 不能为负数")
    if len(args.reference_url) > 10 or any(not url.strip() for url in args.reference_url):
        raise ToolError("参考图须为 0 至 10 个非空 URL")
    for value in (args.project, args.episode, args.request_id):
        if not value.strip():
            raise ToolError("项目、集数和请求 ID 不能为空")
    estimated = args.estimate_fen if args.estimate_fen is not None else budget.estimate_image_fen()
    print(f"单镜预览：{args.project}/{args.episode}/镜{args.shot}；模型：{settings.image.model}")
    print(f"请求 ID：{args.request_id}；预估：{estimated} 分；输出：1 张")
    print(f"项目生图上限：{settings.budget.image_project_cny} 元；提交时重新检查余额。")
    if not args.confirm_paid:
        print(
            "预览完成，未提交、未预占。核对 API 单次费用后使用 --estimate-fen 和 --confirm-paid。"
        )
        return 0
    if args.estimate_fen is None:
        raise BudgetError("请按当前 API 计费填写 --estimate-fen；客户端积分估算不能直接确认付费")
    if args.rework_of is not None:
        rework = get_rework_item(args.rework_of)
        if (
            rework is None
            or rework.status != "approved"
            or rework.target_request_id != args.request_id
        ):
            raise ToolError("返工尚未人工批准，或新请求 ID 与批准记录不一致")
    provider = _provider()
    return _print_result(
        args.request_id,
        gen_image(
            prompt,
            args.shot,
            project=args.project,
            episode=args.episode,
            client_request_id=args.request_id,
            provider=provider,
            est_fen=estimated,
            reference_urls=args.reference_url,
            seed=args.seed,
        ),
    )


def _query(request_id: str) -> int:
    record = budget.get_reservation(request_id)
    if record is None:
        raise BudgetError("找不到该请求 ID，请从 --ledger 台账复制")
    saved = budget.load_generation_result(request_id)
    if saved is not None and saved.status != "unknown":
        return _print_result(request_id, saved)
    if record.status in {"settled", "released"}:
        print(f"请求 {record.reservation_id} 已完成费用处理：{record.actual_fen} 分。")
        return 0
    if record.provider_job_id is None:
        if saved is not None:
            _print_result(request_id, saved)
        print("任务未返回供应商 ID，请人工核对平台任务记录；本次没有重新提交。")
        return 2
    provider = _provider()
    if record.provider != provider.generation_identity().get("provider"):
        raise ConfigError(
            "原生图任务供应商与当前配置不同",
            detail="请用原供应商配置查询旧任务；不会用新供应商查询或重新提交",
        )
    return _print_result(request_id, reconcile_image(request_id, provider=provider))


def _execute_batch(batch: ImageBatch, args: argparse.Namespace) -> int:
    """预览或执行已校验批次；真实供应商只在明确付费确认后初始化。"""
    settings = get_settings()
    if not settings.image.force_single:
        raise ConfigError("批次要求 image.force_single=true")
    if any(len(s.prompt) > settings.image.prompt_max_chars for s in batch.shots):
        raise ToolError("批次中有提示词超过配置上限")
    estimate = args.estimate_fen if args.estimate_fen is not None else budget.estimate_image_fen()
    print(f"批次：{batch.project}/{batch.episode}，{len(batch.shots)} 镜；每镜 {estimate} 分。")
    print(f"首次整批预估：{estimate * len(batch.shots)} 分；已有记录不重复预占。")
    if not args.confirm_paid:
        print("仅预览，未预占、未提交；核价后添加 --estimate-fen 和 --confirm-paid。")
        return 0
    if args.estimate_fen is None:
        raise BudgetError("批量确认前必须通过 --estimate-fen 填写核实后的 API 单次费用上界")
    result = generate_batch(batch, provider=_provider(), est_fen=estimate)
    codes = [_print_result(key, item) for key, item in result.results.items()]
    if result.pending_request_ids:
        print("尚未提交，保留预占：" + ", ".join(result.pending_request_ids))
        print("先处理停止的请求，再沿用原清单继续；取消时逐条释放未提交预占。")
    return max(codes, default=0)


def _batch(args: argparse.Namespace) -> int:
    batch = parse_image_batch(Path(args.manifest).read_text(encoding="utf-8"))
    return _execute_batch(batch, args)


def _recipe_batch(args: argparse.Namespace) -> int:
    reference_map: object = {}
    if args.reference_map is not None:
        try:
            reference_map = json.loads(Path(args.reference_map).read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ToolError("参考素材映射不是合法 JSON") from error
    if not isinstance(reference_map, dict):
        raise ToolError("参考素材映射必须是 JSON 对象")
    batch = build_recipe_batch(
        project=args.project,
        episode=args.episode,
        prompt_version=args.prompt_version,
        reference_urls_by_asset_id=reference_map,
    )
    print(f"配方版本：{args.prompt_version}；已自动生成稳定请求 ID。")
    return _execute_batch(batch, args)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="监督酱 · 单镜生图与账单核对")
    actions = parser.add_subparsers(dest="action")
    actions.add_parser("preflight", help="本地上线自检，不联网、不产生费用")
    actions.add_parser("doctor", help="免计费诊断生图供应商，不创建生图任务")
    generate = actions.add_parser("generate", help="预览一个镜头，确认后提交一次")
    generate.add_argument("--prompt-file", required=True, help="UTF-8 提示词文本文件")
    generate.add_argument("--project", required=True, help="单条视频固定项目 ID，返工时沿用")
    generate.add_argument("--episode", required=True)
    generate.add_argument("--shot", type=_positive_int, required=True)
    generate.add_argument("--request-id", required=True, help="本次候选的固定 ID，恢复时沿用")
    generate.add_argument("--reference-url", action="append", default=[])
    generate.add_argument("--seed", type=int)
    generate.add_argument(
        "--estimate-fen", type=_positive_int, help="核实过的单次 API 费用上界（分）"
    )
    generate.add_argument("--confirm-paid", action="store_true", help="确认此单镜新增费用")
    generate.add_argument("--rework-of", help="返工时填写被人工拒绝的原请求 ID")
    batch = actions.add_parser("batch", help="清单预览或整批预占后串行生成")
    batch.add_argument("manifest", help="UTF-8 生图清单 JSON")
    batch.add_argument("--estimate-fen", type=_positive_int)
    batch.add_argument("--confirm-paid", action="store_true")
    recipes = actions.add_parser("batch-recipes", help="从数据库配方直接预览或生成")
    recipes.add_argument("--project", required=True, help="单条视频固定项目 ID")
    recipes.add_argument("--episode", required=True)
    recipes.add_argument("--prompt-version", required=True)
    recipes.add_argument("--reference-map", help="素材 ID 到 URL 的 UTF-8 JSON 对象")
    recipes.add_argument("--estimate-fen", type=_positive_int)
    recipes.add_argument("--confirm-paid", action="store_true")
    query = actions.add_parser("query", help="只查询原任务，绝不重新提交")
    query.add_argument("request_id")
    settle = actions.add_parser("settle", help="回填平台真实账单，不发网络请求")
    settle.add_argument("request_id")
    settle.add_argument("--actual-cny", type=_money_fen, required=True)
    settle.add_argument("--confirm-bill", action="store_true", help="已核实该任务的最终费用")
    release = actions.add_parser("release", help="释放未提交或已确认失败且免费的任务")
    release.add_argument("request_id")
    release.add_argument("--confirm-no-charge", action="store_true")
    status = actions.add_parser("status", help="离线汇总一条视频的实扣、预占和剩余生图预算")
    status.add_argument("project")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    """执行一项生图操作；0 完成，1 失败，2 待查询/对账，130 用户中断。"""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.action is None:
        parser.print_help()
        return 0
    try:
        if args.action == "status":
            summary = budget.summarize_budget(args.project)
            print(f"项目：{summary.project}；共 {summary.task_count} 条任务记录。")
            print(f"已结算：{summary.settled_fen} 分；仍预占：{summary.held_fen} 分。")
            available = (
                "未设置项目上限" if summary.available_fen is None else f"{summary.available_fen} 分"
            )
            print(f"项目生图可用预算：{available}；提交时还会核对日/集/镜上限。")
            print(f"未结算记录：{summary.unbilled_count}；状态未知：{summary.unknown_count}。")
            print("只包含已入台账的生图费用，其他视频制作成本须另行留预算。")
            return 0
        if args.action == "preflight":
            return _preflight()
        if args.action == "doctor":
            return _doctor()
        if args.action == "generate":
            return _generate(args)
        if args.action == "batch":
            return _batch(args)
        if args.action == "batch-recipes":
            return _recipe_batch(args)
        if args.action == "query":
            return _query(args.request_id)
        if args.action == "settle":
            if not args.confirm_bill:
                raise BudgetError("核对最终账单后添加 --confirm-bill 才能结算")
            record = budget.settle(args.request_id, args.actual_cny)
            print(f"请求 {record.reservation_id} 已结算：{record.actual_fen} 分。")
        elif args.action == "release":
            if not args.confirm_no_charge:
                raise BudgetError("确认任务未扣费后添加 --confirm-no-charge 才能释放")
            record = budget.release(args.request_id)
            print(f"请求 {record.reservation_id} 已释放预占。")
    except BudgetError as error:
        print(f"预算处理停止：{error}", file=sys.stderr)
        return 1
    except KantokuError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("错误：无法读取输入文件或写入运行数据。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断；提交过的任务仍保留预占，请查询原请求。", file=sys.stderr)
        return 130
    return 0


def main() -> None:
    """支持 Windows UTF-8 终端和输出重定向。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(run())


if __name__ == "__main__":
    main()
