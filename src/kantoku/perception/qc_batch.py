"""视觉预筛批次：限制调用数、逐张落盘并支持断点续跑。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from tempfile import NamedTemporaryFile

from openai.types.chat import ChatCompletionMessage

from kantoku.config import ToolError
from kantoku.core.llm import vision_chat
from kantoku.perception.qc import load_qc_predictions, qc_image, validate_qc_request
from kantoku.schemas.qc import HumanQcLabel, QcPrediction


def _write_predictions(path: Path, predictions: Sequence[QcPrediction]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    content = "\n".join(item.model_dump_json() for item in predictions) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(path)
    except (OSError, UnicodeError) as error:
        raise ToolError(
            "无法保存视觉质检预测",
            detail=f"path={path}；error={type(error).__name__}",
        ) from error


def _validate_output_path(path: Path) -> None:
    """在付费调用前验证结果目录可创建且现有目标不是目录。"""
    if path.exists() and not path.is_file():
        raise ToolError("视觉质检结果路径必须是文件", detail=f"path={path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".probe",
        ) as stream:
            stream.write("")
    except (OSError, UnicodeError) as error:
        raise ToolError(
            "视觉质检结果目录不可写",
            detail=f"path={path.parent}；error={type(error).__name__}",
        ) from error


def _validate_pending_labels(labels: Sequence[HumanQcLabel]) -> None:
    """整批本地预检，确保不会在已产生费用后才发现后续坏输入。"""
    for label in labels:
        validate_qc_request(
            label.image_path,
            target_platform=label.target_platform,
            genre=label.genre,
            target_audience=label.target_audience,
            deliverable_type=label.deliverable_type,
            business_goal=label.business_goal,
            key_message=label.key_message,
            first_glance_goal=label.first_glance_goal,
            visual_style=label.visual_style,
            cinematography_requirements=label.cinematography_requirements,
            persona_reference=label.persona_reference,
        )


def run_qc_batch(
    labels: Sequence[HumanQcLabel],
    output_path: Path,
    *,
    max_calls: int,
    confirm_paid: bool = False,
    caller: Callable[..., ChatCompletionMessage] = vision_chat,
) -> list[QcPrediction]:
    """只处理尚无结果的样本；本次缺口超过授权调用数时零调用。"""
    if not labels:
        raise ToolError("视觉质检批次不能为空")
    if type(max_calls) is not int or max_calls <= 0:
        raise ToolError("视觉质检最大调用数必须是正整数")
    existing = load_qc_predictions(output_path) if output_path.exists() else []
    label_ids = [label.id for label in labels]
    if len(set(label_ids)) != len(label_ids):
        raise ToolError("视觉质检批次存在重复样本 ID")
    existing_by_id = {prediction.id: prediction for prediction in existing}
    unknown = sorted(existing_by_id.keys() - set(label_ids))
    if unknown:
        raise ToolError("预测文件包含当前标注集之外的样本", detail=f"ids={unknown[:3]}")
    pending = [label for label in labels if label.id not in existing_by_id]
    if not pending:
        return [existing_by_id[label_id] for label_id in label_ids]
    if not confirm_paid:
        raise ToolError("批量视觉质检可能产生费用，必须先明确确认")
    if len(pending) > max_calls:
        raise ToolError(
            "待质检数量超过本次授权调用数",
            detail=f"pending={len(pending)}；max_calls={max_calls}",
        )
    _validate_pending_labels(pending)
    _validate_output_path(output_path)

    completed = list(existing)
    for label in pending:
        result = qc_image(
            label.image_path,
            target_platform=label.target_platform,
            genre=label.genre,
            target_audience=label.target_audience,
            deliverable_type=label.deliverable_type,
            business_goal=label.business_goal,
            key_message=label.key_message,
            first_glance_goal=label.first_glance_goal,
            visual_style=label.visual_style,
            cinematography_requirements=label.cinematography_requirements,
            persona_reference=label.persona_reference,
            confirm_paid=True,
            caller=caller,
        )
        completed.append(QcPrediction(id=label.id, result=result))
        _write_predictions(output_path, completed)
    completed_by_id = {prediction.id: prediction for prediction in completed}
    return [completed_by_id[label_id] for label_id in label_ids]
