"""文件化 Skill 与 Video Capability 测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_image_gen import _settings

from kantoku.capabilities.video import (
    MockVideoProvider,
    VideoGenerationRequest,
    VideoService,
)
from kantoku.config import BudgetError, ToolError
from kantoku.config.settings import ROOT, VideoSettings
from kantoku.core import budget
from kantoku.core.runtime.models import ArtifactType
from kantoku.core.runtime.store import RuntimeStore
from kantoku.core.skills import SkillLoader, SkillRegistry


def test_skill_loader_discovers_file_skills() -> None:
    registry = SkillRegistry()
    metadata = SkillLoader(ROOT / "skills", project_root=ROOT).load(registry)
    assert {item.id for item in metadata} == {
        "comic.archive_image",
        "comic.compose_prompt",
        "commerce.calculate_pricing",
        "commerce.localize_listing",
        "commerce.product_image",
    }
    assert all(item.handler_ref and not Path(item.handler_ref.split(":")[0]).is_absolute()
               for item in metadata)
    prompt = registry.execute("comic.compose_prompt", {
        "subject": "雨夜便利店", "purpose": "叙事静帧",
        "audience": "普通观众", "style": "纪实",
    }, {})
    assert "雨夜便利店" in prompt["prompt"]


def test_skill_manifest_validation(tmp_path: Path) -> None:
    directory = tmp_path / "skills" / "broken"
    directory.mkdir(parents=True)
    (directory / "manifest.yaml").write_text("id: broken\n", encoding="utf-8")
    (directory / "handler.py").write_text(
        "def execute(inputs, context): return {}\n", encoding="utf-8"
    )
    with pytest.raises(ToolError, match="manifest"):
        SkillLoader(tmp_path / "skills", project_root=tmp_path).discover()


def test_commerce_skills_support_zh_and_ru_locale() -> None:
    registry = SkillRegistry()
    SkillLoader(ROOT / "skills", project_root=ROOT).load(registry)
    listing = {"title": "便携阅读灯", "description": "柔和照明"}
    zh = registry.execute(
        "commerce.localize_listing", {"listing": listing, "locale": "zh-CN"}, {}
    )
    assert zh["localized_listing"] == listing

    def translator(value: dict[str, object], locale: str) -> dict[str, object]:
        return {**value, "title": "Портативная лампа", "translated_by": "llm-mock",
                "locale": locale}

    ru = registry.execute(
        "commerce.localize_listing", {"listing": listing, "locale": "ru-RU"},
        {"translator": translator},
    )
    assert ru["localized_listing"]["title"] == "Портативная лампа"


def test_mock_video_full_path_is_budgeted_idempotent_and_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "video.db")
    monkeypatch.setattr(budget, "get_settings", lambda: settings)
    store = RuntimeStore(settings.storage.sqlite_path)
    run = store.create_run("comic", "video", {}, "video")
    image = store.create_artifact(
        type=ArtifactType.IMAGE, run_id=run.id, node_id="archive",
        source="test", location="mock://image.png",
    )
    service = VideoService(
        store, MockVideoProvider(),
        VideoSettings(enabled=True, estimated_fen=1, max_fen=1),
    )
    request = VideoGenerationRequest(
        request_id="video-test-001", run_id=run.id, node_id="video",
        image_artifact_id=image.id, prompt="轻微镜头推进", project="comic", shot_no=1,
    )
    first = service.generate(request)
    second = service.generate(request)
    assert first == second
    assert first.type is ArtifactType.VIDEO
    assert first.metadata["mock"] is True
    assert budget.get_reservation(request.request_id).actual_fen == 0


def test_video_disabled_and_budget_reject_before_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "reject.db")
    monkeypatch.setattr(budget, "get_settings", lambda: settings)
    store = RuntimeStore(settings.storage.sqlite_path)
    run = store.create_run("comic", "video", {}, "video")
    image = store.create_artifact(
        type=ArtifactType.IMAGE, run_id=run.id, node_id="image", source="test"
    )
    request = VideoGenerationRequest(
        request_id="video-reject-001", run_id=run.id, node_id="video",
        image_artifact_id=image.id, prompt="motion", project="comic", shot_no=1,
    )
    with pytest.raises(ToolError, match="未启用"):
        VideoService(store, MockVideoProvider(), VideoSettings(enabled=False)).generate(request)
    with pytest.raises(BudgetError, match="预算不足"):
        VideoService(
            store, MockVideoProvider(),
            VideoSettings(enabled=True, estimated_fen=2, max_fen=1),
        ).generate(request)
    assert budget.get_reservation(request.request_id) is None
