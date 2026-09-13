"""视觉质检批次的调用上限与断点续跑测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from openai.types.chat import ChatCompletionMessage

from kantoku.config import ToolError
from kantoku.perception import qc_batch
from kantoku.schemas.qc import HumanQcLabel, QcResult


def _label(tmp_path: Path, index: int) -> HumanQcLabel:
    image = tmp_path / f"image-{index}.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    return HumanQcLabel(
        id=f"qc-{index}",
        image_path=image,
        target_platform="抖音",
        genre="都市治愈漫剧",
        target_audience="18-30 岁女性",
        persona_reference="小雨，黑色齐耳短发，红色围裙",
        cinematography_requirements="主体明确，冷暖光分离",
        cinematography_notes="等待人工填写观察",
        result=QcResult(
            broken_hands=False,
            watermark=False,
            composition_ok=True,
            persona_consistency=4,
            confidence=1.0,
            reason="人工真值",
        ),
        approved=True,
    )


def _response() -> ChatCompletionMessage:
    return ChatCompletionMessage(
        role="assistant",
        content=json.dumps(
            {
                "broken_hands": False,
                "watermark": False,
                "composition_ok": True,
                "persona_consistency": 4,
                "confidence": 0.9,
                "reason": "主体明确",
            },
            ensure_ascii=False,
        ),
    )


def test_batch_rejects_over_call_limit_before_first_request(tmp_path: Path) -> None:
    calls = 0

    def caller(*_: object, **__: object) -> ChatCompletionMessage:
        nonlocal calls
        calls += 1
        return _response()

    with pytest.raises(ToolError, match="超过本次授权"):
        qc_batch.run_qc_batch(
            [_label(tmp_path, 1), _label(tmp_path, 2)],
            tmp_path / "predictions.jsonl",
            max_calls=1,
            confirm_paid=True,
            caller=caller,
        )

    assert calls == 0


def test_batch_validates_every_pending_image_before_first_paid_request(
    tmp_path: Path,
) -> None:
    calls = 0
    labels = [_label(tmp_path, 1), _label(tmp_path, 2)]
    labels[1].image_path.write_bytes(b"not-an-image")
    output = tmp_path / "predictions.jsonl"

    def caller(*_: object, **__: object) -> ChatCompletionMessage:
        nonlocal calls
        calls += 1
        return _response()

    with pytest.raises(ToolError, match="不是有效"):
        qc_batch.run_qc_batch(
            labels,
            output,
            max_calls=2,
            confirm_paid=True,
            caller=caller,
        )

    assert calls == 0
    assert not output.exists()


def test_batch_checks_output_writability_before_first_paid_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def caller(*_: object, **__: object) -> ChatCompletionMessage:
        nonlocal calls
        calls += 1
        return _response()

    monkeypatch.setattr(
        qc_batch,
        "NamedTemporaryFile",
        lambda **_: (_ for _ in ()).throw(PermissionError("private path")),
    )
    with pytest.raises(ToolError, match="结果目录不可写"):
        qc_batch.run_qc_batch(
            [_label(tmp_path, 1)],
            tmp_path / "locked" / "predictions.jsonl",
            max_calls=1,
            confirm_paid=True,
            caller=caller,
        )

    assert calls == 0


def test_batch_persists_each_result_and_resumes_without_recalling(tmp_path: Path) -> None:
    calls = 0

    def caller(*_: object, **__: object) -> ChatCompletionMessage:
        nonlocal calls
        calls += 1
        return _response()

    labels = [_label(tmp_path, 1), _label(tmp_path, 2)]
    output = tmp_path / "predictions.jsonl"
    first = qc_batch.run_qc_batch(
        labels,
        output,
        max_calls=2,
        confirm_paid=True,
        caller=caller,
    )
    second = qc_batch.run_qc_batch(
        labels,
        output,
        max_calls=1,
        confirm_paid=False,
        caller=caller,
    )

    assert len(first) == len(second) == 2
    assert calls == 2
    assert output.read_text(encoding="utf-8").count("\n") == 2
