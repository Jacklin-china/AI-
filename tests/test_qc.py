"""W5 人工质检契约与标注集校验测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from openai.types.chat import ChatCompletionMessage
from pydantic import ValidationError

from kantoku.config import SchemaError, ToolError
from kantoku.perception.qc import (
    evaluate_qc_baseline,
    load_qc_labels,
    qc_image,
    save_qc_label,
    triage_qc_predictions,
)
from kantoku.schemas.qc import HumanQcLabel, QcBaseline, QcPrediction, QcResult


def _record(image_path: str, *, sample_id: str = "qc-001") -> dict[str, object]:
    return {
        "id": sample_id,
        "image_path": image_path,
        "deliverable_type": "manga_panel",
        "target_platform": "抖音",
        "genre": "都市治愈漫剧",
        "target_audience": "18-30 岁女性",
        "business_goal": "让观众继续观看下一镜",
        "key_message": "老陈终于释怀",
        "first_glance_goal": "先看到人物克制的表情",
        "visual_style": "写实电影感",
        "style_reference": "清透日系动画",
        "persona_reference": "小雨，黑色齐耳短发，红色围裙",
        "cinematography_requirements": "主体明确，冷暖光分离",
        "cinematography_notes": "主体位于左侧三分线，冷暖光分离叙事空间",
        "result": {
            "broken_hands": False,
            "watermark": False,
            "composition_ok": True,
            "persona_consistency": 4,
            "confidence": 0.82,
            "reason": "人物与构图正常",
        },
        "approved": True,
        "failure_reasons": [],
    }


def test_qc_result_rejects_unknown_fields_and_invalid_confidence() -> None:
    with pytest.raises(ValidationError):
        QcResult(
            broken_hands=False,
            watermark=False,
            composition_ok=True,
            persona_consistency=4,
            confidence=1.1,
            reason="有效说明",
        )
    with pytest.raises(ValidationError):
        QcResult.model_validate(
            {
                **_record("image.png")["result"],
                "invented_score": 99,
            }
        )


def test_hard_failure_cannot_be_approved() -> None:
    record = _record("image.png")
    record["image_path"] = Path("image.png")
    result = record["result"]
    assert isinstance(result, dict)
    result["watermark"] = True

    with pytest.raises(ValidationError, match="硬缺陷"):
        HumanQcLabel.model_validate(record)


@pytest.mark.parametrize(
    "reason",
    ["facial_expression", "ai_artifact", "physical_plausibility"],
)
def test_human_label_supports_observed_soft_failure_reasons(reason: str) -> None:
    record = _record("image.png")
    record["image_path"] = Path("image.png")
    record["approved"] = False
    record["failure_reasons"] = [reason]

    label = HumanQcLabel.model_validate(record)

    assert label.failure_reasons == [reason]


def test_load_qc_labels_accepts_complete_unique_dataset(tmp_path: Path) -> None:
    records: list[dict[str, object]] = []
    for index in range(2):
        image = tmp_path / f"image-{index}.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([index]))
        records.append(_record(image.name, sample_id=f"qc-{index:03d}"))
    dataset = tmp_path / "labels.jsonl"
    dataset.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records),
        encoding="utf-8",
    )

    labels = load_qc_labels(dataset, min_count=2)

    assert [label.id for label in labels] == ["qc-000", "qc-001"]


def test_load_qc_labels_rejects_missing_or_duplicate_samples(tmp_path: Path) -> None:
    record = _record("missing.png")
    dataset = tmp_path / "labels.jsonl"
    dataset.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ToolError, match="图片不存在"):
        load_qc_labels(dataset, min_count=1)

    dataset.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for _ in range(2)),
        encoding="utf-8",
    )
    with pytest.raises(ToolError, match="ID 重复"):
        load_qc_labels(dataset, min_count=2, require_images=False)


def test_load_qc_labels_enforces_thirty_sample_gate(tmp_path: Path) -> None:
    dataset = tmp_path / "labels.jsonl"
    dataset.write_text(
        json.dumps(_record("image.png"), ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ToolError, match="样本不足"):
        load_qc_labels(dataset, require_images=False)


def test_save_qc_label_is_atomic_portable_and_idempotent(tmp_path: Path) -> None:
    image = tmp_path / "images" / "shot.png"
    image.parent.mkdir()
    image.write_bytes(b"\x89PNG\r\n\x1a\nimage-data")
    dataset = tmp_path / "labels.jsonl"
    record = _record(str(image), sample_id="qc-local-001")
    record["image_path"] = image
    label = HumanQcLabel.model_validate(record)

    assert save_qc_label(dataset, label) is True
    assert save_qc_label(dataset, label) is False

    stored = json.loads(dataset.read_text(encoding="utf-8"))
    assert stored["image_path"] == "images/shot.png"
    assert len(load_qc_labels(dataset, min_count=1)) == 1


def test_load_qc_labels_rejects_duplicate_image_content(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"\x89PNG\r\n\x1a\nsame-image")
    second.write_bytes(b"\x89PNG\r\n\x1a\nsame-image")
    dataset = tmp_path / "labels.jsonl"
    dataset.write_text(
        "\n".join(
            (
                json.dumps(_record(first.name, sample_id="qc-001"), ensure_ascii=False),
                json.dumps(_record(second.name, sample_id="qc-002"), ensure_ascii=False),
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ToolError, match="图片内容不能重复"):
        load_qc_labels(dataset, min_count=2)


def test_qc_image_requires_confirmation_before_read_or_call(tmp_path: Path) -> None:
    called = False

    def caller(*_: object, **__: object) -> ChatCompletionMessage:
        nonlocal called
        called = True
        return ChatCompletionMessage(role="assistant", content="{}")

    with pytest.raises(ToolError, match="确认"):
        qc_image(
            tmp_path / "missing.png",
            target_platform="抖音",
            genre="都市漫剧",
            target_audience="18-30 岁女性",
            cinematography_requirements="主体明确",
            caller=caller,
        )

    assert called is False


def test_qc_image_sends_supported_image_and_parses_strict_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nimage-data")
    monkeypatch.setattr(
        "kantoku.perception.qc.get_settings",
        lambda: type(
            "Settings",
            (),
            {"llm": type("Llm", (), {"vision_max_image_bytes": 1024})()},
        )(),
    )
    captured: dict[str, object] = {}

    def caller(messages: object, **kwargs: object) -> ChatCompletionMessage:
        captured["messages"] = messages
        captured.update(kwargs)
        return ChatCompletionMessage(
            role="assistant",
            content=json.dumps(
                {
                    "broken_hands": False,
                    "watermark": False,
                    "composition_ok": True,
                    "persona_consistency": 4,
                    "confidence": 0.82,
                    "reason": "主体明确",
                },
                ensure_ascii=False,
            ),
        )

    result = qc_image(
        image,
        target_platform="抖音",
        genre="都市漫剧",
        target_audience="18-30 岁女性",
        cinematography_requirements="主体明确，冷暖光分离",
        confirm_paid=True,
        caller=caller,
    )

    assert result.composition_ok is True
    assert captured["response_format"] == {"type": "json_object"}
    assert "data:image/png;base64," in str(captured["messages"])
    assert '"deliverable_type": "still_image"' in str(captured["messages"])


def test_qc_image_rejects_invalid_model_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nimage-data")
    monkeypatch.setattr(
        "kantoku.perception.qc.get_settings",
        lambda: type(
            "Settings",
            (),
            {"llm": type("Llm", (), {"vision_max_image_bytes": 1024})()},
        )(),
    )

    with pytest.raises(SchemaError, match="JSON 校验失败"):
        qc_image(
            image,
            target_platform="抖音",
            genre="都市漫剧",
            target_audience="18-30 岁女性",
            cinematography_requirements="主体明确",
            confirm_paid=True,
            caller=lambda *_args, **_kwargs: ChatCompletionMessage(
                role="assistant", content='{"confidence": 99}'
            ),
        )


def test_qc_baseline_selects_lowest_reliable_threshold() -> None:
    labels: list[HumanQcLabel] = []
    predictions: list[QcPrediction] = []
    for index, confidence in enumerate((0.95, 0.9, 0.85, 0.8, 0.4), start=1):
        actual = QcResult(
            broken_hands=index == 1,
            watermark=index == 2,
            composition_ok=index != 3,
            persona_consistency=4,
            confidence=1.0,
            reason="人工真值",
        )
        predicted = actual.model_copy(
            update={
                "watermark": not actual.watermark if index == 5 else actual.watermark,
                "confidence": confidence,
                "reason": "模型预测",
            }
        )
        failure_reasons = (
            ["broken_hands"]
            if actual.broken_hands
            else ["watermark"]
            if actual.watermark
            else ["composition"]
            if not actual.composition_ok
            else []
        )
        labels.append(
            HumanQcLabel(
                id=f"qc-{index}",
                image_path=Path(f"{index}.png"),
                target_platform="抖音",
                genre="都市漫剧",
                target_audience="18-30 岁女性",
                cinematography_requirements="主体明确",
                cinematography_notes="主体明确",
                result=actual,
                approved=not failure_reasons,
                failure_reasons=failure_reasons,
            )
        )
        predictions.append(QcPrediction(id=f"qc-{index}", result=predicted))

    result = evaluate_qc_baseline(
        labels,
        predictions,
        target_accuracy=1.0,
        min_covered=4,
    )

    assert result.confidence_threshold == 0.4
    assert result.threshold_covered_count == 5
    assert result.threshold_accuracy == 1.0
    assert result.low_confidence_count == 0
    assert result.active_binary_checks == ["broken_hands", "composition"]


def test_qc_baseline_rejects_mismatched_ids() -> None:
    result = QcResult(
        broken_hands=False,
        watermark=False,
        composition_ok=True,
        persona_consistency=4,
        confidence=0.8,
        reason="有效",
    )
    label = HumanQcLabel(
        id="human-1",
        image_path=Path("1.png"),
        target_platform="抖音",
        genre="都市漫剧",
        target_audience="18-30 岁女性",
        cinematography_requirements="主体明确",
        cinematography_notes="主体明确",
        result=result,
        approved=True,
    )

    with pytest.raises(ToolError, match="样本不一致"):
        evaluate_qc_baseline([label], [QcPrediction(id="model-1", result=result)])


def test_triage_never_replaces_human_final_decision() -> None:
    predictions = [
        QcPrediction(
            id="bad-high",
            result=QcResult(
                broken_hands=True,
                watermark=False,
                composition_ok=True,
                persona_consistency=4,
                confidence=0.95,
                reason="右手出现六指",
            ),
        ),
        QcPrediction(
            id="good-high",
            result=QcResult(
                broken_hands=False,
                watermark=False,
                composition_ok=True,
                persona_consistency=4,
                confidence=0.9,
                reason="未见明显硬缺陷",
            ),
        ),
        QcPrediction(
            id="low",
            result=QcResult(
                broken_hands=False,
                watermark=False,
                composition_ok=True,
                persona_consistency=3,
                confidence=0.5,
                reason="手部被遮挡",
            ),
        ),
    ]
    baseline = QcBaseline(
        sample_count=30,
        broken_hands_accuracy=0.95,
        watermark_accuracy=0.95,
        hard_defect_accuracy=0.95,
        composition_accuracy=0.8,
        persona_mean_absolute_error=0.6,
        confidence_threshold=0.8,
        threshold_covered_count=20,
        threshold_accuracy=0.95,
        low_confidence_count=10,
    )

    items = triage_qc_predictions(predictions, baseline)

    assert [item.suggested_decision for item in items] == [
        "reject",
        "approve",
        "undetermined",
    ]
    assert all(item.requires_human_review for item in items)
    assert items[0].failure_reasons == ["broken_hands"]
    assert items[2].review_priority == "high"


def test_triage_ignores_disabled_weak_check_but_still_requires_human() -> None:
    prediction = QcPrediction(
        id="weak-watermark",
        result=QcResult(
            broken_hands=False,
            watermark=True,
            composition_ok=True,
            persona_consistency=4,
            confidence=0.95,
            reason="水印判断在标注集上不稳定",
        ),
    )
    baseline = QcBaseline(
        sample_count=30,
        broken_hands_accuracy=0.95,
        watermark_accuracy=0.6,
        hard_defect_accuracy=0.775,
        composition_accuracy=0.93,
        persona_mean_absolute_error=0.5,
        active_binary_checks=["broken_hands", "composition"],
        confidence_threshold=0.8,
        threshold_covered_count=20,
        threshold_accuracy=0.95,
        low_confidence_count=10,
    )

    item = triage_qc_predictions([prediction], baseline)[0]

    assert item.suggested_decision == "undetermined"
    assert item.failure_reasons == []
    assert item.review_priority == "high"
    assert item.requires_human_review is True
