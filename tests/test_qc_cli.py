"""人工终审 CLI 的确认门、记录和队列输出测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kantoku.schemas.media import ImageGenerationResult
from kantoku.schemas.qc import HumanQcLabel, ReworkItem
from kantoku.shells import qc_cli
from kantoku.tools.prompt_factory import PromptRecipe


def _review_args() -> list[str]:
    return [
        "review",
        "request-001",
        "--platform",
        "抖音",
        "--deliverable-type",
        "promo_keyframe",
        "--business-goal",
        "提高产品咨询量",
        "--key-message",
        "低成本也能做出专业视觉",
        "--first-glance-goal",
        "第一眼看到产品主体",
        "--visual-style",
        "克制的商业摄影",
        "--genre",
        "都市治愈漫剧",
        "--audience",
        "18-30 岁女性",
        "--cinematography-notes",
        "人物位于左侧三分线，冷暖光分离",
        "--cinematography-requirements",
        "主体明确，冷暖光分离",
        "--review-seconds",
        "120",
        "--persona-consistency",
        "4",
        "--reason",
        "画面文字乱码",
        "--hands-ok",
        "--no-watermark",
        "--composition-ok",
        "--reject",
        "--failure-reason",
        "garbled_text",
    ]


def test_review_requires_explicit_human_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorder = SimpleNamespace(called=False)
    monkeypatch.setattr(qc_cli, "record_human_review", lambda *_: setattr(recorder, "called", True))

    assert qc_cli.run(_review_args()) == 1

    assert "--confirm-human" in capsys.readouterr().err
    assert recorder.called is False


def test_rejected_review_is_recorded_without_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    image = tmp_path / "result.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(
        qc_cli,
        "load_generation_result",
        lambda _: ImageGenerationResult(
            path=image,
            provider_job_id="provider-001",
            status="succeeded",
        ),
    )
    saved: list[HumanQcLabel] = []

    def record(_: str, label: HumanQcLabel) -> None:
        saved.append(label)

    monkeypatch.setattr(qc_cli, "record_human_review", record)

    assert qc_cli.run([*_review_args(), "--confirm-human"]) == 0

    assert len(saved) == 1
    label = saved[0]
    assert label.approved is False
    assert label.deliverable_type == "promo_keyframe"
    assert label.business_goal == "提高产品咨询量"
    assert label.failure_reasons == ["garbled_text"]
    assert "没有重新生成" in capsys.readouterr().out


def test_queue_lists_reason_and_never_generates(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        qc_cli,
        "list_rework_queue",
        lambda **_: [
            ReworkItem(
                source_request_id="request-001",
                failure_reasons=["garbled_text"],
                reason="画面文字乱码",
                status="pending",
                target_request_id=None,
                created_at="2026-09-13 00:00:00",
                updated_at="2026-09-13 00:00:00",
            )
        ],
    )

    assert qc_cli.run(["queue"]) == 0

    output = capsys.readouterr().out
    assert "request-001" in output
    assert "garbled_text" in output


def test_rework_plan_prints_targeted_constraints_without_generation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = ReworkItem(
        source_request_id="request-001",
        failure_reasons=["unclear_message", "style_mismatch"],
        reason="信息层级混乱且风格偏离参考",
        status="pending",
        created_at="2026-09-13 00:00:00",
        updated_at="2026-09-13 00:00:00",
    )
    monkeypatch.setattr(qc_cli, "get_rework_item", lambda _: item)

    assert qc_cli.run(["rework-plan", "request-001"]) == 0

    output = capsys.readouterr().out
    assert "核心信息" in output
    assert "美术风格" in output
    assert "没有触发生图" in output


def test_approved_rework_creates_traceable_recipe_without_paid_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = ReworkItem(
        source_request_id="request-001",
        failure_reasons=["facial_expression"],
        reason="笑容僵硬，情绪不像真实抓拍",
        status="approved",
        target_request_id="request-001-v2",
        created_at="2026-09-13 00:00:00",
        updated_at="2026-09-13 00:01:00",
    )
    base = PromptRecipe(
        episode="ep01",
        shot_no=1,
        prompt_version="prompt-v1",
        prompt="原始镜头提示词",
        model="image-model",
        size="2560x1440",
    )
    saved: list[PromptRecipe] = []
    monkeypatch.setattr(qc_cli, "get_rework_item", lambda _: item)
    monkeypatch.setattr(
        qc_cli,
        "get_reservation",
        lambda _: SimpleNamespace(
            project="video-001", episode="ep01", shot_no=1, model="image-model"
        ),
    )
    monkeypatch.setattr(qc_cli, "load_recipe", lambda *_: base)
    monkeypatch.setattr(qc_cli, "save_recipe", saved.append)
    output_path = tmp_path / "rework.txt"

    assert (
        qc_cli.run(
            [
                "rework-recipe",
                "request-001",
                "--base-prompt-version",
                "prompt-v1",
                "--output",
                str(output_path),
            ]
        )
        == 0
    )

    assert len(saved) == 1
    assert saved[0].source_request_id == "request-001"
    assert saved[0].target_request_id == "request-001-v2"
    assert "只修正以下问题" in output_path.read_text(encoding="utf-8")
    output = capsys.readouterr().out
    assert "没有调用模型" in output
    assert "没有触发生图或费用" in output


def test_preflight_only_reads_local_vision_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    key_reads = 0

    def read_key() -> str:
        nonlocal key_reads
        key_reads += 1
        return "fake-key"

    settings = SimpleNamespace(
        llm=SimpleNamespace(
            model_vision="vision-model",
            vision_max_tokens=512,
            vision_max_image_bytes=1024,
            vision_api_key=read_key,
        )
    )
    monkeypatch.setattr(qc_cli, "get_settings", lambda: settings)

    assert qc_cli.run(["preflight"]) == 0

    assert key_reads == 1
    assert "没有调用模型" in capsys.readouterr().out


def test_label_image_builds_dataset_without_external_call(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    image = tmp_path / "candidate.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nlocal-image")
    dataset = tmp_path / "labels.jsonl"
    common = _review_args()[2:]
    arguments = [
        "label-image",
        str(image),
        str(dataset),
        "--label-id",
        "qc-local-001",
        *common,
        "--confirm-human",
    ]

    assert qc_cli.run(arguments) == 0
    assert qc_cli.run(arguments) == 0

    lines = dataset.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    output = capsys.readouterr().out
    assert "没有触发生图或费用" in output
    assert "没有重复写入" in output
