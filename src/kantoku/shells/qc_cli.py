"""W5 人工终审与质检集检查入口；任何操作都不会自动重新生图。"""

from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from kantoku.config import KantokuError, ToolError, get_settings
from kantoku.core.budget import get_reservation, load_generation_result
from kantoku.perception.qc import (
    evaluate_qc_baseline,
    load_qc_labels,
    load_qc_predictions,
    save_qc_label,
    triage_qc_predictions,
)
from kantoku.perception.qc_batch import run_qc_batch
from kantoku.perception.report import calculate_qc_economics
from kantoku.perception.review import (
    build_rework_plan,
    decide_rework,
    get_rework_item,
    list_rework_queue,
    record_human_review,
)
from kantoku.schemas.qc import HumanQcLabel, QcBaseline, QcFailureReason, QcResult
from kantoku.tools.archive import archive_reviewed_image, search_archived_images
from kantoku.tools.prompt_factory import (
    create_rework_recipe,
    load_recipe,
    save_recipe,
)

_FAILURE_REASONS: tuple[QcFailureReason, ...] = (
    "broken_hands",
    "watermark",
    "garbled_text",
    "composition",
    "persona_drift",
    "cinematography",
    "facial_expression",
    "ai_artifact",
    "physical_plausibility",
    "weak_visual_hook",
    "unclear_message",
    "style_mismatch",
    "audience_mismatch",
    "other",
)


def _bounded_int(minimum: int, maximum: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"必须是 {minimum}–{maximum} 的整数") from None
        if not minimum <= value <= maximum:
            raise argparse.ArgumentTypeError(f"必须是 {minimum}–{maximum} 的整数")
        return value

    return parse


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("必须是正整数") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return value


def _required_boolean(
    parser: argparse.ArgumentParser,
    *,
    destination: str,
    positive: str,
    negative: str,
) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(positive, dest=destination, action="store_true")
    group.add_argument(negative, dest=destination, action="store_false")


def _add_human_label_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--deliverable-type",
        choices=(
            "still_image",
            "poster",
            "video_keyframe",
            "promo_keyframe",
            "manga_panel",
            "other",
        ),
        default="still_image",
    )
    parser.add_argument("--platform", required=True)
    parser.add_argument("--genre", required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--business-goal")
    parser.add_argument("--key-message")
    parser.add_argument("--first-glance-goal")
    parser.add_argument("--visual-style")
    parser.add_argument("--style-reference")
    parser.add_argument("--persona-reference")
    parser.add_argument("--cinematography-requirements", required=True)
    parser.add_argument("--cinematography-notes", required=True)
    parser.add_argument("--review-seconds", type=_positive_int)
    parser.add_argument("--persona-consistency", type=_bounded_int(1, 5), required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument(
        "--failure-reason",
        action="append",
        choices=_FAILURE_REASONS,
        default=[],
    )
    _required_boolean(
        parser,
        destination="broken_hands",
        positive="--broken-hands",
        negative="--hands-ok",
    )
    _required_boolean(
        parser,
        destination="watermark",
        positive="--watermark",
        negative="--no-watermark",
    )
    _required_boolean(
        parser,
        destination="composition_ok",
        positive="--composition-ok",
        negative="--composition-bad",
    )
    _required_boolean(
        parser,
        destination="approved",
        positive="--approve",
        negative="--reject",
    )
    parser.add_argument("--confirm-human", action="store_true")


def _label_from_args(args: argparse.Namespace, image_path: Path, label_id: str) -> HumanQcLabel:
    decision = QcResult(
        broken_hands=args.broken_hands,
        watermark=args.watermark,
        composition_ok=args.composition_ok,
        persona_consistency=args.persona_consistency,
        confidence=1.0,
        reason=args.reason,
    )
    return HumanQcLabel(
        id=label_id,
        image_path=image_path,
        deliverable_type=args.deliverable_type,
        target_platform=args.platform,
        genre=args.genre,
        target_audience=args.audience,
        business_goal=args.business_goal,
        key_message=args.key_message,
        first_glance_goal=args.first_glance_goal,
        visual_style=args.visual_style,
        style_reference=args.style_reference,
        persona_reference=args.persona_reference,
        cinematography_requirements=args.cinematography_requirements,
        cinematography_notes=args.cinematography_notes,
        review_seconds=args.review_seconds,
        result=decision,
        approved=args.approved,
        failure_reasons=args.failure_reason,
    )


def _review(args: argparse.Namespace) -> int:
    if not args.confirm_human:
        raise ToolError("人工核对画面后添加 --confirm-human 才能保存终审结果")
    generation = load_generation_result(args.request_id)
    if generation is None or generation.status != "succeeded" or generation.path is None:
        raise ToolError("找不到该请求的成功图片，请先完成生图查询")
    label = _label_from_args(args, generation.path, args.label_id or args.request_id)
    record_human_review(args.request_id, label)
    outcome = "通过" if label.approved else "拒绝并加入返工队列"
    print(f"人工终审已保存：{args.request_id}；结果：{outcome}。")
    if label.review_seconds is None:
        print("未填写人工检查耗时；经营报告会将本条标为待补时长。")
    if not label.approved:
        print("失败原因：" + "、".join(label.failure_reasons))
        print("返工尚未获付费授权，程序没有重新生成图片。")
    return 0


def _label_image(args: argparse.Namespace) -> int:
    if not args.confirm_human:
        raise ToolError("人工核对画面后添加 --confirm-human 才能保存标注")
    label = _label_from_args(args, args.image, args.label_id)
    created = save_qc_label(args.output, label)
    if created:
        print(f"人工标注已保存：{label.id}；数据集={args.output.resolve()}")
    else:
        print(f"人工标注已存在且内容一致：{label.id}；没有重复写入。")
    if label.review_seconds is None:
        print("未填写人工检查耗时；经营报告会将本条标为待补时长。")
    print("这一步没有调用视觉模型，也没有触发生图或费用。")
    return 0


def _validate_dataset(args: argparse.Namespace) -> int:
    labels = load_qc_labels(args.path, min_count=args.min_count)
    approved = sum(label.approved for label in labels)
    print(f"质检集有效：{len(labels)} 张；通过 {approved}；拒绝 {len(labels) - approved}。")
    print("这里只验证人工真值完整性，没有调用视觉模型。")
    return 0


def _queue(args: argparse.Namespace) -> int:
    items = list_rework_queue(status=args.status)
    if not items:
        print(f"返工队列为空：status={args.status}。")
        return 0
    print(f"返工队列：status={args.status}；共 {len(items)} 条。")
    for item in items:
        reasons = "、".join(item.failure_reasons)
        target = item.target_request_id or "未分配"
        print(f"- 原请求={item.source_request_id}；原因={reasons}；新请求={target}")
    return 0


def _baseline(args: argparse.Namespace) -> int:
    labels = load_qc_labels(args.labels, min_count=args.min_count)
    predictions = load_qc_predictions(args.predictions)
    result = evaluate_qc_baseline(
        labels,
        predictions,
        target_accuracy=args.target_accuracy,
        min_covered=args.min_covered,
    )
    print(f"质检基线：{result.sample_count} 张。")
    print(
        f"崩手准确率={result.broken_hands_accuracy:.1%}；"
        f"水印准确率={result.watermark_accuracy:.1%}；"
        f"硬缺陷合计={result.hard_defect_accuracy:.1%}。"
    )
    print(
        f"构图一致率={result.composition_accuracy:.1%}；"
        f"人物一致性平均误差={result.persona_mean_absolute_error:.2f}。"
    )
    print("当前二元预筛项：" + "、".join(result.active_binary_checks))
    if len(result.active_binary_checks) < 3:
        print("已按实测一致率降级为最稳的 2 项；其余判断仍由人工完成。")
    if result.confidence_threshold is None:
        print("当前数据无法得到满足目标的可靠阈值；全部转人工终审。")
    else:
        print(
            f"阈值={result.confidence_threshold:.2f}；覆盖={result.threshold_covered_count}；"
            f"阈值内准确率={result.threshold_accuracy:.1%}；"
            f"低置信度={result.low_confidence_count}。"
        )
    if args.output is not None:
        _write_text(args.output, result.model_dump_json(indent=2) + "\n")
        print(f"基线结果={args.output.resolve()}")
    return 0


def _write_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(path)
    except (OSError, UnicodeError) as error:
        raise ToolError("无法保存质检输出", detail=type(error).__name__) from error


def _triage(args: argparse.Namespace) -> int:
    try:
        baseline = QcBaseline.model_validate_json(args.baseline.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        raise ToolError("无法读取质检基线", detail=type(error).__name__) from error
    except ValueError as error:
        raise ToolError("质检基线格式错误", detail=type(error).__name__) from error
    predictions = load_qc_predictions(args.predictions)
    items = triage_qc_predictions(predictions, baseline)
    content = "\n".join(item.model_dump_json() for item in items) + "\n"
    _write_text(args.output, content)
    high = sum(item.review_priority == "high" for item in items)
    print(f"人工终审队列已生成：{len(items)} 条；高优先级={high}。")
    print("所有条目仍需人工决定，程序没有自动通过、报废或返工。")
    return 0


def _archive(args: argparse.Namespace) -> int:
    result = archive_reviewed_image(args.request_id)
    decision = "通过" if result.approved else "拒绝"
    print(f"归档完成：{decision}；图片={result.image_path.resolve()}")
    print(f"标签={result.metadata_path.resolve()}")
    return 0


def _archives(args: argparse.Namespace) -> int:
    results = search_archived_images(
        project=args.project,
        episode=args.episode,
        shot_no=args.shot_no,
        character=args.character,
        approved=args.approved,
    )
    if not results:
        print("没有符合条件的归档素材。")
        return 0
    print(f"归档素材：共 {len(results)} 条。")
    for result in results:
        decision = "通过" if result.approved else "拒绝"
        characters = "、".join(result.characters) or "无角色标签"
        print(
            f"- {result.project}/{result.episode}/镜{result.shot_no}；{decision}；"
            f"角色={characters}；图片={result.image_path.resolve()}"
        )
    return 0


def _decide_rework(args: argparse.Namespace) -> int:
    if not args.confirm_human:
        raise ToolError("人工确认后添加 --confirm-human 才能保存返工决定")
    item = decide_rework(
        args.source_request_id,
        args.decision,
        target_request_id=args.target_request_id,
    )
    if item.status == "approved":
        print(f"返工已批准：原请求={item.source_request_id}；新请求={item.target_request_id}。")
        print("尚未预占或生成；后续仍须在生图命令明确填写费用上界并确认付费。")
    else:
        print(f"返工已取消：原请求={item.source_request_id}。")
    return 0


def _rework_plan(args: argparse.Namespace) -> int:
    item = get_rework_item(args.source_request_id)
    if item is None:
        raise ToolError("找不到返工项")
    plan = build_rework_plan(item)
    print(f"定向返工计划：原请求={plan.source_request_id}")
    print("保留：" + " ".join(plan.preserve_constraints))
    for index, directive in enumerate(plan.correction_directives, start=1):
        print(f"修正{index}：{directive}")
    print("人工证据：" + plan.evidence)
    print("计划没有触发生图；确认预算和费用后才能批准返工。")
    return 0


def _rework_recipe(args: argparse.Namespace) -> int:
    """把已批准返工转换为现有生图管线可消费的版本化配方。"""
    item = get_rework_item(args.source_request_id)
    if item is None:
        raise ToolError("找不到返工项")
    if item.status != "approved" or item.target_request_id is None:
        raise ToolError("返工尚未人工批准，不能创建可执行配方")
    reservation = get_reservation(item.source_request_id)
    if reservation is None:
        raise ToolError("找不到原生图任务的预算记录")
    base = load_recipe(
        reservation.episode,
        reservation.shot_no,
        args.base_prompt_version,
    )
    if base is None:
        raise ToolError("找不到原生图任务对应的基础 Prompt 配方")
    if base.model != reservation.model:
        raise ToolError("基础 Prompt 配方与原生图任务的模型不一致")
    recipe = create_rework_recipe(
        base,
        build_rework_plan(item),
        target_request_id=item.target_request_id,
    )
    save_recipe(recipe)
    if args.output is not None:
        _write_text(args.output, recipe.prompt + "\n")
    print(
        f"返工配方已就绪：{reservation.project}/{recipe.episode}/镜{recipe.shot_no}；"
        f"版本={recipe.prompt_version}。"
    )
    print(f"原请求={item.source_request_id}；新请求={item.target_request_id}。")
    if args.output is not None:
        print(f"提示词={args.output.resolve()}")
    print("没有调用模型、没有预占预算、没有触发生图或费用。")
    return 0


def _screen(args: argparse.Namespace) -> int:
    labels = load_qc_labels(args.labels, min_count=args.min_count)
    existing = load_qc_predictions(args.output) if args.output.exists() else []
    pending = len(labels) - len(existing)
    print(f"视觉预筛：总样本={len(labels)}；已有={len(existing)}；待调用={pending}。")
    if not args.confirm_paid:
        print("仅预览，没有调用视觉模型；确认费用后添加 --confirm-paid。")
        return 0
    predictions = run_qc_batch(
        labels,
        args.output,
        max_calls=args.max_calls,
        confirm_paid=True,
    )
    print(f"视觉预筛完成：{len(predictions)} 条；结果={args.output.resolve()}")
    print("模型结果只是预筛，仍须人工终审。")
    return 0


def _preflight() -> int:
    settings = get_settings()
    settings.llm.vision_api_key()
    print("视觉质检本地自检通过；没有调用模型，也没有产生费用。")
    print(
        f"模型={settings.llm.model_vision}；单次输出上限="
        f"{settings.llm.vision_max_tokens} tokens；图片上限="
        f"{settings.llm.vision_max_image_bytes} bytes。"
    )
    print("真实预筛仍需30张人工标签、调用数上限和明确费用确认。")
    return 0


def _report(args: argparse.Namespace) -> int:
    result = calculate_qc_economics(
        args.project,
        args.episode,
        expected_shots=args.expected_shots,
    )
    print(f"质量成本：{result.project}/{result.episode}；目标 {result.expected_shots} 镜。")
    print(
        f"首次通过={result.first_pass_approved_shots}/{result.expected_shots} "
        f"({result.first_pass_rate:.1%})；最终通过={result.final_approved_shots}/"
        f"{result.expected_shots} ({result.final_approval_rate:.1%})。"
    )
    print(
        f"生成尝试={result.generation_attempts}；平均每镜="
        f"{result.average_generations_per_shot:.2f}；返工={result.rework_count}。"
    )
    print(
        f"人工质检={result.human_review_minutes} 分钟；"
        f"缺少耗时记录={result.unmeasured_review_count} 条。"
    )
    cost = (
        "暂无可用镜头"
        if result.cost_per_approved_shot_fen is None
        else f"{result.cost_per_approved_shot_fen} 分/可用镜头"
    )
    print(
        f"已结算={result.settled_fen} 分；仍预占={result.held_fen} 分；"
        f"待对账={result.unbilled_count}；{cost}。"
    )
    print("闭环状态：" + ("已完成" if result.complete else "尚未完成"))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="监督酱 · 图片人工终审与返工队列")
    actions = parser.add_subparsers(dest="action")
    actions.add_parser("preflight", help="零网络检查视觉模型配置和密钥")

    review = actions.add_parser("review", help="人工终审一张已成功生成的图片")
    review.add_argument("request_id")
    review.add_argument("--label-id")
    _add_human_label_arguments(review)

    label_image = actions.add_parser("label-image", help="人工标注一张本地候选图")
    label_image.add_argument("image", type=Path)
    label_image.add_argument("output", type=Path)
    label_image.add_argument("--label-id", required=True)
    _add_human_label_arguments(label_image)

    dataset = actions.add_parser("validate-dataset", help="检查30张人工标注集")
    dataset.add_argument("path", type=Path)
    dataset.add_argument("--min-count", type=_positive_int, default=30)

    queue = actions.add_parser("queue", help="查看返工队列，不触发生成")
    queue.add_argument("--status", choices=("pending", "approved", "cancelled"), default="pending")
    baseline = actions.add_parser("baseline", help="用人工真值评测视觉模型预测")
    baseline.add_argument("labels", type=Path)
    baseline.add_argument("predictions", type=Path)
    baseline.add_argument("--min-count", type=_positive_int, default=30)
    baseline.add_argument("--min-covered", type=_positive_int, default=5)
    baseline.add_argument("--target-accuracy", type=float, default=0.9)
    baseline.add_argument("--output", type=Path)
    archive = actions.add_parser("archive", help="归档已完成人工终审的图片和标签")
    archive.add_argument("request_id")
    archives = actions.add_parser("archives", help="按项目、剧集、镜号或角色检索归档")
    archives.add_argument("--project")
    archives.add_argument("--episode")
    archives.add_argument("--shot-no", type=_positive_int)
    archives.add_argument("--character")
    decision_filter = archives.add_mutually_exclusive_group()
    decision_filter.add_argument("--approved", dest="approved", action="store_true")
    decision_filter.add_argument("--rejected", dest="approved", action="store_false")
    archives.set_defaults(approved=None)
    rework = actions.add_parser("decide-rework", help="人工批准或取消一个返工项")
    rework.add_argument("source_request_id")
    rework.add_argument("decision", choices=("approved", "cancelled"))
    rework.add_argument("--target-request-id")
    rework.add_argument("--confirm-human", action="store_true")
    plan = actions.add_parser("rework-plan", help="根据人工失败原因生成零调用定向返工计划")
    plan.add_argument("source_request_id")
    recipe = actions.add_parser("rework-recipe", help="把已批准返工保存为可执行 Prompt 配方")
    recipe.add_argument("source_request_id")
    recipe.add_argument("--base-prompt-version", required=True)
    recipe.add_argument("--output", type=Path, help="可选：另存为单镜生图提示词文件")
    screen = actions.add_parser("screen", help="批量调用视觉模型预筛，支持断点续跑")
    screen.add_argument("labels", type=Path)
    screen.add_argument("output", type=Path)
    screen.add_argument("--min-count", type=_positive_int, default=30)
    screen.add_argument("--max-calls", type=_positive_int, required=True)
    screen.add_argument("--confirm-paid", action="store_true")
    triage = actions.add_parser("triage", help="按实测阈值生成待人工终审队列")
    triage.add_argument("baseline", type=Path)
    triage.add_argument("predictions", type=Path)
    triage.add_argument("output", type=Path)
    report = actions.add_parser("report", help="从台账计算10镜质量和成本指标")
    report.add_argument("--project", required=True)
    report.add_argument("--episode", required=True)
    report.add_argument("--expected-shots", type=_positive_int, default=10)
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.action is None:
        parser.print_help()
        return 0
    try:
        if args.action == "review":
            return _review(args)
        if args.action == "label-image":
            return _label_image(args)
        if args.action == "preflight":
            return _preflight()
        if args.action == "validate-dataset":
            return _validate_dataset(args)
        if args.action == "queue":
            return _queue(args)
        if args.action == "baseline":
            return _baseline(args)
        if args.action == "archive":
            return _archive(args)
        if args.action == "archives":
            return _archives(args)
        if args.action == "decide-rework":
            return _decide_rework(args)
        if args.action == "rework-plan":
            return _rework_plan(args)
        if args.action == "rework-recipe":
            return _rework_recipe(args)
        if args.action == "screen":
            return _screen(args)
        if args.action == "triage":
            return _triage(args)
        if args.action == "report":
            return _report(args)
    except KantokuError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("错误：无法读取输入文件或写入运行数据。", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(run())


if __name__ == "__main__":
    main()
