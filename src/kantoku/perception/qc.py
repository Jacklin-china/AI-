"""人工质检集读取与上线前完整性校验。"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path

from openai.types.chat import ChatCompletionMessage, ChatCompletionMessageParam
from pydantic import ValidationError

from kantoku.config import SchemaError, ToolError, get_settings
from kantoku.core.llm import vision_chat
from kantoku.schemas.qc import (
    CreativeDeliverable,
    HumanQcLabel,
    QcBaseline,
    QcBinaryCheck,
    QcFailureReason,
    QcPrediction,
    QcResult,
    QcTriageItem,
)

_SUPPORTED_IMAGE_SUFFIXES = {".jpeg", ".jpg", ".png", ".webp"}
_QC_SYSTEM_PROMPT = """你是 AI 视觉作品质检员，只做预筛，不能代替人工终审。
必须只返回一个 JSON 对象，字段和类型严格遵守给定 Schema，不要输出 Markdown。
broken_hands：手指缺失、多余、粘连、严重扭曲时为 true；看不清时不要武断，降低 confidence。
watermark：出现平台水印、生成器标记或不应存在的角标时为 true。
composition_ok：主体明确、裁切合理、景别/机位服务叙事且无明显乱码干扰时才为 true。
persona_consistency：仅依据提供的人设或参考描述给 1–5；信息不足时降低 confidence 并在 reason 说明。
reason 还要检查表情是否符合内容强度、材质与雨水等物理关系是否自然、是否存在塑料感或明显 AI 痕迹。
结合交付物类型、商业目标、核心信息和第一眼目标，检查主体层级是否清楚、视觉钩子是否有效、
美术风格是否统一；缺少这些信息时只能降低 confidence，不能自行虚构客户需求。
confidence 仅表示本次预筛把握，不能用它自动通过、报废或触发付费返工。
reason 必须简洁指出可核对的画面证据。目标平台、题材、受众和摄影要求只用于判断适配性，
不要声称存在统一的“大众审美”。
JSON Schema：{schema}
"""


def _image_media_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _read_image(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise ToolError(
            "无法读取质检图片",
            detail=f"path={path}；error={type(error).__name__}",
        ) from error


def _validate_image(path: Path) -> bytes:
    if path.suffix.lower() not in _SUPPORTED_IMAGE_SUFFIXES:
        raise ToolError("质检样本图片格式不支持", detail=f"path={path}")
    data = _read_image(path)
    if _image_media_type(data) is None:
        raise ToolError("质检样本不是有效的 PNG、JPEG 或 WebP 图片", detail=f"path={path}")
    return data


def validate_qc_request(
    path: Path,
    *,
    target_platform: str,
    genre: str,
    target_audience: str,
    cinematography_requirements: str,
    deliverable_type: CreativeDeliverable = "still_image",
    business_goal: str | None = None,
    key_message: str | None = None,
    first_glance_goal: str | None = None,
    visual_style: str | None = None,
    persona_reference: str | None = None,
) -> None:
    """只做单张视觉质检请求的本地完整性检查，不调用模型。"""
    context = {
        "deliverable_type": deliverable_type,
        "target_platform": target_platform,
        "genre": genre,
        "target_audience": target_audience,
        "business_goal": business_goal,
        "key_message": key_message,
        "first_glance_goal": first_glance_goal,
        "visual_style": visual_style,
        "cinematography_requirements": cinematography_requirements,
        "persona_reference": persona_reference,
    }
    optional = {
        "business_goal",
        "key_message",
        "first_glance_goal",
        "visual_style",
        "persona_reference",
    }
    required = {key: value for key, value in context.items() if key not in optional}
    if any(not isinstance(value, str) or not value.strip() for value in required.values()):
        raise ToolError("目标平台、题材、受众和摄影要求不能为空")
    if any(
        value is not None and (not isinstance(value, str) or not value.strip())
        for key, value in context.items()
        if key in optional
    ):
        raise ToolError("可选的创作目标或参考信息不能为空字符串")
    data = _validate_image(path)
    maximum = get_settings().llm.vision_max_image_bytes
    if not data or len(data) > maximum:
        raise ToolError(
            "待质检图片大小不符合配置",
            detail=f"bytes={len(data)}；max={maximum}",
        )


def qc_image(
    path: Path,
    *,
    target_platform: str,
    genre: str,
    target_audience: str,
    cinematography_requirements: str,
    deliverable_type: CreativeDeliverable = "still_image",
    business_goal: str | None = None,
    key_message: str | None = None,
    first_glance_goal: str | None = None,
    visual_style: str | None = None,
    persona_reference: str | None = None,
    confirm_paid: bool = False,
    caller: Callable[..., ChatCompletionMessage] = vision_chat,
) -> QcResult:
    """调用一次 VLM 预筛；不确认费用时零调用，结果永远等待人工终审。"""
    if not confirm_paid:
        raise ToolError("视觉质检可能产生费用，必须先明确确认")
    validate_qc_request(
        path,
        target_platform=target_platform,
        genre=genre,
        target_audience=target_audience,
        cinematography_requirements=cinematography_requirements,
        deliverable_type=deliverable_type,
        business_goal=business_goal,
        key_message=key_message,
        first_glance_goal=first_glance_goal,
        visual_style=visual_style,
        persona_reference=persona_reference,
    )
    context = {
        "deliverable_type": deliverable_type,
        "target_platform": target_platform,
        "genre": genre,
        "target_audience": target_audience,
        "business_goal": business_goal,
        "key_message": key_message,
        "first_glance_goal": first_glance_goal,
        "visual_style": visual_style,
        "cinematography_requirements": cinematography_requirements,
        "persona_reference": persona_reference,
    }
    data = _read_image(path)
    media_type = _image_media_type(data)
    if media_type is None:  # validate_qc_request 已检查；防御并发替换文件。
        raise ToolError("待质检文件在预检后发生变化")
    encoded = base64.b64encode(data).decode("ascii")
    messages: Sequence[ChatCompletionMessageParam] = [
        {
            "role": "system",
            "content": _QC_SYSTEM_PROMPT.format(
                schema=json.dumps(QcResult.model_json_schema(), ensure_ascii=False)
            ),
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "质检上下文：" + json.dumps(context, ensure_ascii=False),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{encoded}"},
                },
            ],
        },
    ]
    message = caller(messages, response_format={"type": "json_object"})
    if not message.content:
        raise SchemaError("视觉模型未返回质检 JSON")
    try:
        return QcResult.model_validate_json(message.content)
    except ValidationError as error:
        raise SchemaError(
            "视觉质检 JSON 校验失败",
            detail=type(error).__name__,
            raw=message.content,
        ) from None


def load_qc_labels(
    path: Path,
    *,
    min_count: int = 30,
    require_images: bool = True,
) -> list[HumanQcLabel]:
    """读取人工标注 JSONL；样本不足、重复或图片缺失时拒绝跑基线。"""
    if type(min_count) is not int or min_count <= 0:
        raise ToolError("质检集最小样本数必须是正整数")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ToolError(
            "无法读取质检标注集",
            detail=f"path={path}；error={type(error).__name__}",
        ) from error

    labels: list[HumanQcLabel] = []
    ids: set[str] = set()
    image_paths: set[Path] = set()
    image_digests: dict[str, str] = {}
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            label = HumanQcLabel.model_validate_json(line)
        except ValidationError as error:
            raise ToolError(
                "质检标注格式错误",
                detail=f"line={line_number}；error={type(error).__name__}",
            ) from error
        if label.id in ids:
            raise ToolError("质检样本 ID 重复", detail=f"line={line_number}；id={label.id}")

        image_path = label.image_path
        resolved = (
            image_path.resolve()
            if image_path.is_absolute()
            else (path.parent / image_path).resolve()
        )
        if resolved in image_paths:
            raise ToolError(
                "同一图片不能重复标注",
                detail=f"line={line_number}；id={label.id}",
            )
        if require_images and not resolved.is_file():
            raise ToolError(
                "质检样本图片不存在",
                detail=f"line={line_number}；id={label.id}",
            )
        if require_images:
            data = _validate_image(resolved)
            digest = hashlib.sha256(data).hexdigest()
            duplicate_id = image_digests.get(digest)
            if duplicate_id is not None:
                raise ToolError(
                    "同一图片内容不能重复标注",
                    detail=f"line={line_number}；id={label.id}；duplicate={duplicate_id}",
                )
            image_digests[digest] = label.id
        elif resolved.suffix.lower() not in _SUPPORTED_IMAGE_SUFFIXES:
            raise ToolError(
                "质检样本图片格式不支持",
                detail=f"line={line_number}；id={label.id}",
            )
        label = label.model_copy(update={"image_path": resolved})
        ids.add(label.id)
        image_paths.add(resolved)
        labels.append(label)

    if len(labels) < min_count:
        raise ToolError(
            "质检标注样本不足",
            detail=f"当前={len(labels)}；至少={min_count}",
        )
    return labels


def save_qc_label(path: Path, label: HumanQcLabel) -> bool:
    """原子追加一条人工真值；完全相同的重复操作视为成功且不重复写入。"""
    resolved_image = label.image_path.resolve()
    if not resolved_image.is_file():
        raise ToolError("质检样本图片不存在", detail=f"path={resolved_image}")
    new_digest = hashlib.sha256(_validate_image(resolved_image)).hexdigest()
    normalized = label.model_copy(update={"image_path": resolved_image})

    existing: list[HumanQcLabel] = []
    if path.exists():
        try:
            has_content = bool(path.read_text(encoding="utf-8").strip())
        except (OSError, UnicodeError) as error:
            raise ToolError("无法读取质检标注集", detail=type(error).__name__) from error
        if has_content:
            existing = load_qc_labels(path, min_count=1)

    for current in existing:
        if current.id == normalized.id:
            if current == normalized:
                return False
            raise ToolError("质检样本 ID 已存在且内容不同", detail=f"id={normalized.id}")
        current_digest = hashlib.sha256(_read_image(current.image_path)).hexdigest()
        if current_digest == new_digest:
            raise ToolError(
                "同一图片内容不能重复标注",
                detail=f"id={normalized.id}；duplicate={current.id}",
            )

    records = [*existing, normalized]
    parent = path.parent.resolve()
    serialized: list[str] = []
    for record in records:
        try:
            stored_path = record.image_path.resolve().relative_to(parent)
        except ValueError:
            stored_path = record.image_path.resolve()
        payload = record.model_dump(mode="json")
        payload["image_path"] = stored_path.as_posix()
        serialized.append(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text("\n".join(serialized) + "\n", encoding="utf-8", newline="\n")
        temporary.replace(path)
    except (OSError, UnicodeError) as error:
        temporary.unlink(missing_ok=True)
        raise ToolError("无法保存质检标注集", detail=type(error).__name__) from error
    return True


def load_qc_predictions(path: Path) -> list[QcPrediction]:
    """读取视觉模型预测 JSONL，并拒绝坏行、空集和重复 ID。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ToolError(
            "无法读取质检预测集",
            detail=f"path={path}；error={type(error).__name__}",
        ) from error
    predictions: list[QcPrediction] = []
    ids: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            prediction = QcPrediction.model_validate_json(line)
        except ValidationError as error:
            raise ToolError(
                "质检预测格式错误",
                detail=f"line={line_number}；error={type(error).__name__}",
            ) from error
        if prediction.id in ids:
            raise ToolError(
                "质检预测 ID 重复",
                detail=f"line={line_number}；id={prediction.id}",
            )
        ids.add(prediction.id)
        predictions.append(prediction)
    if not predictions:
        raise ToolError("质检预测集不能为空", detail=f"path={path}")
    return predictions


def evaluate_qc_baseline(
    labels: Sequence[HumanQcLabel],
    predictions: Sequence[QcPrediction],
    *,
    target_accuracy: float = 0.9,
    min_covered: int = 5,
) -> QcBaseline:
    """按人工真值评测，并选出满足目标准确率且覆盖最多样本的最低阈值。"""
    if not labels:
        raise ToolError("人工质检集不能为空")
    if not 0 < target_accuracy <= 1:
        raise ToolError("目标准确率必须大于 0 且不超过 1")
    if type(min_covered) is not int or min_covered <= 0:
        raise ToolError("阈值最小覆盖数必须是正整数")
    labels_by_id = {label.id: label for label in labels}
    predictions_by_id = {prediction.id: prediction for prediction in predictions}
    if len(labels_by_id) != len(labels) or len(predictions_by_id) != len(predictions):
        raise ToolError("质检基线中存在重复样本 ID")
    if labels_by_id.keys() != predictions_by_id.keys():
        missing = sorted(labels_by_id.keys() - predictions_by_id.keys())
        extra = sorted(predictions_by_id.keys() - labels_by_id.keys())
        raise ToolError(
            "人工标签与模型预测样本不一致",
            detail=f"missing={missing[:3]}；extra={extra[:3]}",
        )

    pairs = [
        (labels_by_id[sample_id].result, predictions_by_id[sample_id].result)
        for sample_id in labels_by_id
    ]
    count = len(pairs)
    broken_correct = sum(
        actual.broken_hands == predicted.broken_hands for actual, predicted in pairs
    )
    watermark_correct = sum(actual.watermark == predicted.watermark for actual, predicted in pairs)
    composition_correct = sum(
        actual.composition_ok == predicted.composition_ok for actual, predicted in pairs
    )
    persona_error = sum(
        abs(actual.persona_consistency - predicted.persona_consistency)
        for actual, predicted in pairs
    )

    check_order: tuple[QcBinaryCheck, ...] = ("broken_hands", "watermark", "composition")
    accuracies: dict[QcBinaryCheck, float] = {
        "broken_hands": broken_correct / count,
        "watermark": watermark_correct / count,
        "composition": composition_correct / count,
    }
    active_checks = list(check_order)
    if any(accuracies[check] < target_accuracy for check in check_order):
        active_checks = sorted(
            check_order,
            key=lambda check: (-accuracies[check], check_order.index(check)),
        )[:2]

    selected_threshold: float | None = None
    selected_count = 0
    selected_accuracy: float | None = None
    for threshold in sorted({predicted.confidence for _, predicted in pairs}):
        covered = [
            (actual, predicted)
            for actual, predicted in pairs
            if predicted.confidence >= threshold
        ]
        if len(covered) < min_covered:
            continue
        exact = sum(
            all(
                (
                    actual.composition_ok == predicted.composition_ok
                    if check == "composition"
                    else getattr(actual, check) == getattr(predicted, check)
                )
                for check in active_checks
            )
            for actual, predicted in covered
        )
        accuracy = exact / len(covered)
        if accuracy >= target_accuracy:
            selected_threshold = threshold
            selected_count = len(covered)
            selected_accuracy = accuracy
            break

    return QcBaseline(
        sample_count=count,
        broken_hands_accuracy=broken_correct / count,
        watermark_accuracy=watermark_correct / count,
        hard_defect_accuracy=(broken_correct + watermark_correct) / (count * 2),
        composition_accuracy=composition_correct / count,
        persona_mean_absolute_error=persona_error / count,
        active_binary_checks=active_checks,
        confidence_threshold=selected_threshold,
        threshold_covered_count=selected_count,
        threshold_accuracy=selected_accuracy,
        low_confidence_count=count - selected_count,
    )


def triage_qc_predictions(
    predictions: Sequence[QcPrediction],
    baseline: QcBaseline,
) -> list[QcTriageItem]:
    """按实测阈值给人工排序；无可靠阈值时全部标为无法确定。"""
    if not predictions:
        raise ToolError("质检预测集不能为空")
    ids = [prediction.id for prediction in predictions]
    if len(set(ids)) != len(ids):
        raise ToolError("质检预测存在重复样本 ID")
    threshold = baseline.confidence_threshold
    active_checks = set(baseline.active_binary_checks)
    items: list[QcTriageItem] = []
    for prediction in predictions:
        result = prediction.result
        failures: list[QcFailureReason] = []
        if "broken_hands" in active_checks and result.broken_hands:
            failures.append("broken_hands")
        if "watermark" in active_checks and result.watermark:
            failures.append("watermark")
        if "composition" in active_checks and not result.composition_ok:
            failures.append("composition")
        disabled_signal = (
            ("broken_hands" not in active_checks and result.broken_hands)
            or ("watermark" not in active_checks and result.watermark)
            or ("composition" not in active_checks and not result.composition_ok)
        )
        if threshold is None or result.confidence < threshold:
            suggested = "undetermined"
            priority = "high"
            reason = f"低置信度或尚无可靠阈值；模型观察：{result.reason}"
        elif failures:
            suggested = "reject"
            priority = "high"
            reason = result.reason
        elif disabled_signal:
            suggested = "undetermined"
            priority = "high"
            reason = f"未启用的弱判定出现异常信号，交人工核对；模型观察：{result.reason}"
        else:
            suggested = "approve"
            priority = "normal"
            reason = result.reason
        items.append(
            QcTriageItem(
                id=prediction.id,
                suggested_decision=suggested,
                review_priority=priority,
                confidence=result.confidence,
                failure_reasons=failures,
                reason=reason,
            )
        )
    return items
