"""人工终审与返工队列测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from kantoku.config import ToolError
from kantoku.core import budget
from kantoku.perception import review
from kantoku.schemas.media import ImageGenerationResult
from kantoku.schemas.qc import HumanQcLabel, QcResult


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "review.sqlite3"
    monkeypatch.setattr(budget, "_database_path", lambda: path)
    monkeypatch.setattr(review, "_database_path", lambda: path)
    return path


def _label(path: Path, *, approved: bool) -> HumanQcLabel:
    return HumanQcLabel(
        id="qc-001",
        image_path=path,
        target_platform="抖音",
        genre="都市治愈漫剧",
        target_audience="18-30 岁女性",
        style_reference="清透日系动画",
        cinematography_requirements="主体明确，冷暖光分离",
        cinematography_notes="冷暖光分离，人物处于左侧三分线",
        result=QcResult(
            broken_hands=False,
            watermark=False,
            composition_ok=approved,
            persona_consistency=4,
            confidence=0.9,
            reason="画面可用" if approved else "构图没有明确叙事焦点",
        ),
        approved=approved,
        failure_reasons=[] if approved else ["composition"],
    )


def _save_success(path: Path) -> None:
    budget.reserve(
        reservation_id="request-001",
        job="test-review",
        project="video-001",
        episode="ep01",
        shot_no=1,
        kind="image",
        est_fen=1,
        model="test-model",
    )
    budget.mark_submitted("request-001", provider_job_id="provider-001")
    budget.mark_outcome(
        "request-001", "succeeded", provider_job_id="provider-001"
    )
    budget.save_generation_result(
        "request-001",
        ImageGenerationResult(
            path=path,
            provider_job_id="provider-001",
            status="succeeded",
        ),
    )


def test_approved_review_does_not_create_rework(database: Path) -> None:
    image = database.parent / "approved.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    _save_success(image)

    review.record_human_review("request-001", _label(image, approved=True))

    assert review.list_rework_queue() == []


def test_rejected_review_creates_idempotent_reasoned_rework(database: Path) -> None:
    image = database.parent / "rejected.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    _save_success(image)
    label = _label(image, approved=False)

    review.record_human_review("request-001", label)
    review.record_human_review("request-001", label)

    queue = review.list_rework_queue()
    assert len(queue) == 1
    assert queue[0].source_request_id == "request-001"
    assert queue[0].failure_reasons == ["composition"]
    assert queue[0].status == "pending"


def test_review_rejects_wrong_image_and_silent_overwrite(database: Path) -> None:
    image = database.parent / "original.png"
    other = database.parent / "other.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    other.write_bytes(b"\x89PNG\r\n\x1a\n")
    _save_success(image)

    with pytest.raises(ToolError, match="不一致"):
        review.record_human_review("request-001", _label(other, approved=True))

    review.record_human_review("request-001", _label(image, approved=True))
    with pytest.raises(ToolError, match="禁止静默覆盖"):
        review.record_human_review("request-001", _label(image, approved=False))


def test_rework_requires_new_id_and_decision_is_immutable(database: Path) -> None:
    image = database.parent / "rejected.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    _save_success(image)
    review.record_human_review("request-001", _label(image, approved=False))

    with pytest.raises(ToolError, match="不同的新请求 ID"):
        review.decide_rework(
            "request-001", "approved", target_request_id="request-001"
        )

    approved = review.decide_rework(
        "request-001", "approved", target_request_id="request-001-v2"
    )
    assert approved.status == "approved"
    assert approved.target_request_id == "request-001-v2"
    assert review.get_rework_item("request-001") == approved
    assert review.decide_rework(
        "request-001", "approved", target_request_id="request-001-v2"
    ) == approved
    with pytest.raises(ToolError, match="禁止修改"):
        review.decide_rework("request-001", "cancelled")


def test_rework_plan_is_targeted_deduplicated_and_never_authorizes_payment() -> None:
    item = review.ReworkItem(
        source_request_id="request-001",
        failure_reasons=["facial_expression", "weak_visual_hook", "facial_expression"],
        reason="人物笑容僵硬，主体没有第一眼吸引力",
        status="pending",
        created_at="2026-09-13 00:00:00",
        updated_at="2026-09-13 00:00:00",
    )

    plan = review.build_rework_plan(item)

    assert len(plan.correction_directives) == 2
    assert "表情" in plan.correction_directives[0]
    assert "第一眼焦点" in plan.correction_directives[1]
    assert plan.requires_human_approval is True
    assert plan.evidence == item.reason
