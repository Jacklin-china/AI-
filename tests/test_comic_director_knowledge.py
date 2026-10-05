"""Director knowledge is loaded from existing Skills and reaches the actual text call."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from loguru import logger
from test_comic_creation_mode import _execute
from test_comic_creation_mode import app as app_fixture
from test_comic_creation_mode import model as model_fixture
from test_comic_director_coordinator import SKILLS, _project

from kantoku.config import ToolError
from kantoku.core.skills import SkillLoader, SkillRegistry
from kantoku.domains.comic import coordinator
from kantoku.domains.comic.director_knowledge import MODEL_STAGES, load_director_skill_context
from kantoku.domains.comic.models import ComicAssetDraft, ComicProjectInput

app = app_fixture
model = model_fixture
ROOT = Path(__file__).parents[1]
KNOWLEDGE = ROOT / "skills/comic/director/knowledge"


def _registry() -> SkillRegistry:
    registry = SkillRegistry()
    SkillLoader(ROOT / "skills", project_root=ROOT).load(registry)
    return registry


def test_declared_director_knowledge_resolves_all_six_documents():
    registry = _registry()
    paths = set()
    for skill_id in MODEL_STAGES:
        loaded = load_director_skill_context(registry.get(skill_id).metadata)
        assert loaded["skill_id"] == skill_id and loaded["knowledge_refs"]
        assert len(loaded["sha256"]) == 64 and loaded["content"]
        paths.update(Path(item["path"]).name for item in loaded["documents"])
        assert len({item["path"] for item in loaded["documents"]}) == len(loaded["documents"])
    assert paths == {
        "storytelling.md", "cinematography.md", "camera_language.md",
        "lighting.md", "composition.md", "shot_design.md",
    }


@pytest.mark.parametrize("invalid", ["missing", "empty", "unknown_ref", "outside", "oversize"])
def test_invalid_knowledge_is_rejected_before_model_call(tmp_path, invalid):
    metadata = _registry().get("comic.creative_understanding").metadata
    destination = tmp_path / "skills/comic/director/knowledge"
    shutil.copytree(KNOWLEDGE, destination)
    path = destination / "storytelling.md"
    if invalid == "missing":
        path.unlink()
    elif invalid == "empty":
        path.write_text("", encoding="utf-8")
    elif invalid == "unknown_ref":
        metadata = metadata.model_copy(update={"knowledge_refs": ("missing.reference",)})
    elif invalid == "outside":
        metadata = metadata.model_copy(update={"handler_ref": "../outside/handler.py:execute"})
    else:
        path.write_text("a" * 16001, encoding="utf-8")
    with pytest.raises(ToolError, match="导演知识加载失败"):
        load_director_skill_context(metadata, project_root=tmp_path)


@pytest.mark.parametrize("interaction,mode,enabled", [
    ("guided", "professional", True), ("guided", "fast", True),
    ("autonomous", "fast", False), ("autonomous", "professional", False),
])
def test_existing_director_call_receives_knowledge_request_and_pinned_assets(
    app, model, monkeypatch, interaction, mode, enabled,
):
    request = "生成一个电影级角色战斗画面"
    project_id = app.comic_projects.create(ComicProjectInput.model_validate({
        "title": "离线导演知识验收", "brief": {"original_request": request},
    })).project.project_id
    drafts = [
        {"name": "主角", "details": {
            "kind": "character", "appearance": "黑发少女", "outfit": "深蓝战斗服",
        }},
        {"name": "战斗世界", "details": {
            "kind": "scene", "location": "雨夜竹林", "time_of_day": "夜晚",
            "weather": "细雨", "lighting": "月光", "atmosphere": "危险",
            "environment_features": ["破损石阶"],
        }},
        {"name": "风格设定", "details": {
            "kind": "style", "art_direction": "写实电影概念画",
            "color_language": "蓝灰环境和少量暖色高光", "materials": "湿润布料",
            "camera_language": "叙事明确的空间关系",
        }},
    ]
    assets = [app.comic_assets.create(
        project_id, ComicAssetDraft.model_validate(draft),
        expected_project_version=app.comic_projects.get(project_id).project.current_version,
    ) for draft in drafts]
    conversation = app.create_conversation({"interaction_mode": interaction, "domain": "comic"})
    original = app._comic_director_model
    seen = []

    def capture(messages):
        if any(f'"title": "{name}"' in messages[0]["content"] for name in (
            "CreativeDecision", "DirectorPlan", "CinematographyPlan",
        )):
            seen.append(messages)
        return original(messages)

    def forbidden(*args, **kwargs):
        pytest.fail("Director knowledge must not submit an image")

    monkeypatch.setattr(app, "_comic_director_model", capture)
    monkeypatch.setattr(app.image_service.provider, "submit", forbidden)
    logs = []
    sink = logger.add(lambda record: logs.append(record.record["message"]))
    try:
        result = _execute(app, project_id, mode, task=request,
                          conversation_id=conversation["id"],
                          asset_ids=[asset.asset_id for asset in assets])
    finally:
        logger.remove(sink)
    assert result["status"] == "completed" and len(seen) == 3
    run = app.runtime_store.get_run(result["run_id"])
    provenance = run.state["director_skill_context"]
    if enabled:
        assert set(provenance) == set(SKILLS[:3])
        for skill_id, messages in zip(SKILLS[:3], seen, strict=True):
            payload = json.loads(messages[1]["content"])
            assert payload["context"]["creative_brief"]["original_request"] == request
            assert {asset["asset_id"] for asset in payload["context"]["relevant_assets"]} == {
                asset.asset_id for asset in assets
            }
            assert "Director Skill Context" in messages[0]["content"]
            knowledge = load_director_skill_context(app.skills.get(skill_id).metadata)
            assert knowledge["content"] in messages[0]["content"]
            assert provenance[skill_id]["sha256"] == knowledge["sha256"]
            assert "content" not in provenance[skill_id]  # metadata only in Run/logs
        events = [e for e in app.runtime_store.list_events(run.id)
                  if e.payload.get("director_event") == "director_skill_loaded"]
        assert len(events) == 3 and all(e.payload["director_skill_loaded"] for e in events)
        assert all(e.payload["trace_id"] == run.state["trace_id"] for e in events)
        lines = [line for line in logs if "director_skill_loaded=true" in line]
        assert len(lines) == 3 and all(f"project_id={project_id}" in line for line in lines)
        assert all(f"trace_id={run.state['trace_id']}" in line for line in lines)
    else:
        assert provenance == {}
        assert all("Director Skill Context" not in messages[0]["content"] for messages in seen)
        assert not [line for line in logs if "director_skill_loaded=true" in line]
    assert not app.runtime_store.list_media_jobs(conversation["id"])


def test_missing_knowledge_exposes_module_reason_and_trace_without_a_model_request(
    app, model, monkeypatch, tmp_path,
):
    original = load_director_skill_context
    monkeypatch.setattr(coordinator, "load_director_skill_context",
                        lambda metadata: original(metadata, project_root=tmp_path))
    project_id = _project(app.comic_projects)
    with pytest.raises(ToolError, match="无法读取 storytelling.md") as caught:
        _execute(app, project_id, "professional")
    assert model == []
    failure = caught.value._kantoku_public_failure
    assert failure["trace_id"] != "-" and failure["error_id"]
    assert "storytelling.md" in failure["safe_message"]
    run = app.runtime_store.list_runs(domain="comic")[0]
    assert run.state["failed_skill_id"] == "comic.creative_understanding"
    assert run.state["director_skill_context"] == {}
    assert run.status.value == "failed" and run.state["error_id"] == failure["error_id"]
    assert app.comic_projects.director_versions(project_id) == []
