"""Phase 7.2.3：导演协调层的模式、版本与失败契约。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kantoku.config import ToolError
from kantoku.core.conversations import InteractionMode
from kantoku.core.runtime.models import ExecutionStatus
from kantoku.core.runtime.store import RuntimeStore
from kantoku.core.skills import SkillLoader, SkillRegistry
from kantoku.domains.comic.coordinator import (
    ComicDirectorCoordinator,
    DirectorCoordinatorRequest,
    director_execution_summary,
)
from kantoku.domains.comic.critic import DirectorCriticEngine
from kantoku.domains.comic.models import (
    CharacterAsset,
    ComicAsset,
    ComicProjectInput,
)
from kantoku.domains.comic.projects import ComicProjectStore

ROOT = Path(__file__).parents[1]
SKILLS = [
    "comic.creative_understanding",
    "comic.visual_direction",
    "comic.cinematography",
    "comic.director_critic",
    "comic.director_assemble",
]


def _parts() -> dict[str, dict[str, Any]]:
    decision = {
        "intent_summary": "少女在雨夜迎战，突出个人选择与环境压力",
        "narrative_context": "东方仙侠世界的雨夜战斗开始前",
        "emotional_target": "孤独但坚定，最终形成震撼感",
        "audience_experience": "先感到环境压力，再注意到人物决心",
        "narrative_focus": "少女与竹林环境之间的尺度关系",
        "hard_constraints": ["东方仙侠", "少女", "雨夜", "战斗"],
    }
    plan = {
        "visual_strategy": "让竹林纵深承托孤立的迎战者",
        "visual_focus": "雨夜中的少女与拔剑动作",
        "subject_environment_relation": "人物清晰，环境规模仍可见",
        "composition_strategy": "保留前中后景层次引导视线",
        "color_strategy": "冷雨环境与少量剑光对比",
        "creative_choices": ["先建立环境压力，再聚焦人物回应。"],
    }
    camera = {
        "shot_size": "远景渐进中近景",
        "camera_angle": "随动作略低机位",
        "camera_distance": "先远后近",
        "spatial_feel": "竹林形成自然纵深",
        "lens_or_spatial_feel": "自然透视",
        "lighting": "冷色散射光照亮雨幕",
        "light_source": "天光与剑光",
        "light_direction": "侧后方轮廓光",
        "color_relationship": "蓝灰环境对比暖色剑光",
        "depth_strategy": "雨丝、人物、竹林分层",
        "material_language": "湿润石面与布料反光",
    }
    critic = {
        "verdict": "pass",
        "public_summary": "硬约束均已保留，视觉选择有叙事依据。",
        "confidence": 0.9,
    }
    draft = {
        "schema_version": 2,
        "visual_direction": plan["visual_strategy"],
        "storytelling_goal": decision["intent_summary"],
        "camera_language": "先建立空间，再靠近人物",
        "composition": plan["composition_strategy"],
        "lighting": camera["lighting"],
        "color_language": plan["color_strategy"],
        "emotion": decision["emotional_target"],
        "character_focus": plan["visual_focus"],
        "constraints": decision["hard_constraints"],
        "creative_choices": plan["creative_choices"],
        "creative_decision": decision,
        "director_plan": plan,
        "cinematography": camera,
        "critic_result": critic,
    }
    return {
        SKILLS[0]: {"creative_decision": decision},
        SKILLS[1]: {"director_plan": plan},
        SKILLS[2]: {"cinematography": camera},
        SKILLS[3]: {"critic_result": critic},
        SKILLS[4]: {"director_spec": draft},
    }


def _setup(
    tmp_path: Path,
) -> tuple[ComicDirectorCoordinator, ComicProjectStore, RuntimeStore, list[str]]:
    project_store = ComicProjectStore(tmp_path / "comic.db")
    runtime_store = RuntimeStore(tmp_path / "runtime.db")
    registry = SkillRegistry()
    SkillLoader(ROOT / "skills", project_root=ROOT).load(registry)
    calls: list[str] = []
    parts = _parts()

    def stage_executor(skill_id: str, _inputs: Any, _context: Any) -> dict[str, Any]:
        calls.append(skill_id)
        return parts[skill_id]

    coordinator = ComicDirectorCoordinator(
        registry=registry, runtime_store=runtime_store,
        project_store=project_store, stage_executor=stage_executor,
        critic_engine=DirectorCriticEngine(lambda _messages: json.dumps({
            "public_summary": "用户约束完整，视觉因果有依据。", "confidence": 0.9,
            "findings": [], "suggested_patches": [],
        })),
    )
    return coordinator, project_store, runtime_store, calls


def _project(store: ComicProjectStore) -> str:
    snapshot = store.create(ComicProjectInput.model_validate({
        "title": "东方仙侠少女雨夜战斗",
        "brief": {
            "original_request": "东方仙侠少女雨夜战斗，要震撼",
            "hard_constraints": ["东方仙侠", "少女", "雨夜", "战斗"],
        },
    }))
    return snapshot.project.project_id


def _request(
    store: ComicProjectStore, project_id: str, mode: str, **kwargs: Any,
) -> DirectorCoordinatorRequest:
    return DirectorCoordinatorRequest.model_validate({
        "snapshot": store.get(project_id),
        "task": "东方仙侠少女雨夜战斗，要震撼",
        "execution_mode": mode,
        "trace_id": "trace-director-contract",
        **kwargs,
    })


def _event_names(runtime_store: RuntimeStore, run_id: str) -> list[str]:
    return [event.payload["director_event"] for event in runtime_store.list_events(run_id)]


@pytest.mark.parametrize("failed_stage", SKILLS[:2])
def test_each_generation_failure_retains_real_completed_output_and_trace(
    tmp_path: Path, failed_stage: str,
) -> None:
    coordinator, store, runtime, _calls = _setup(tmp_path)
    project_id = _project(store)
    original = coordinator.stage_executor

    def execute(skill_id: str, inputs: Any, context: Any) -> dict[str, Any]:
        if skill_id == failed_stage:
            raise ToolError("阶段失败的离线回归替身")
        return original(skill_id, inputs, context)

    coordinator.stage_executor = execute
    with pytest.raises(ToolError, match="阶段失败的离线回归替身"):
        coordinator.execute(_request(store, project_id, "professional"))
    run = runtime.list_runs()[0]
    completed = SKILLS[:SKILLS.index(failed_stage)]
    assert run.status is ExecutionStatus.FAILED
    assert run.state["completed_stages"] == completed
    assert set(run.state["stage_outputs"]) == set(completed)
    assert "director_candidate" not in run.state
    summary = director_execution_summary(run)
    expected = ["completed"] * len(completed) + ["failed"]
    expected += ["waiting"] * (5 - len(expected))
    assert [item["status"] for item in summary["stage_statuses"]] == expected
    failure = summary["failure"]
    assert failure["stage_name"] == failed_stage.removeprefix("comic.")
    assert failure["trace_id"] == "trace-director-contract"
    assert failure["error_id"] == run.state["error_id"]
    assert failure["input_version"]["creative_brief"] == 1
    assert set(failure["output_before_failure"]) == set(completed)
    restarted = RuntimeStore(runtime.path)
    assert director_execution_summary(restarted.get_run(run.id)) == summary
    assert store.get(project_id).project.director_version is None


def test_fast_uses_one_core_run_and_saves_reviewed_v2(tmp_path: Path) -> None:
    coordinator, store, runtime, calls = _setup(tmp_path)
    project_id = _project(store)

    result = coordinator.execute(_request(store, project_id, "fast"))

    assert calls == SKILLS[:3]
    assert result.director_spec.schema_version == 2
    assert result.director_spec.critic_result.verdict == "pass"
    assert store.get_director(project_id) == result.director_spec
    assert runtime.get_run(result.run_id).status == ExecutionStatus.COMPLETED
    assert len(runtime.list_runs(domain="comic", interaction_mode=InteractionMode.AUTONOMOUS)) == 1
    names = _event_names(runtime, result.run_id)
    assert names[0] == "director_run_started" and names[-1] == "director_run_completed"
    assert "director_mode_selected" in names
    assert names.count("director_stage_completed") == 5
    assert "director_critic_completed" in names


def test_professional_records_each_stage_and_public_critic(tmp_path: Path) -> None:
    coordinator, store, runtime, calls = _setup(tmp_path)
    project_id = _project(store)

    result = coordinator.execute(_request(store, project_id, "professional"))

    assert calls == SKILLS[:3]
    assert result.director_spec.critic_result is not None
    assert result.director_spec.critic_result.verdict == "pass"
    run = runtime.get_run(result.run_id)
    assert run.state["completed_stages"] == SKILLS
    assert run.state["project_version_after"] == store.get(project_id).project.current_version
    assert "director_critic_completed" in _event_names(runtime, result.run_id)
    assert len(runtime.list_runs(domain="comic", interaction_mode=InteractionMode.GUIDED)) == 1
    for event in runtime.list_events(result.run_id):
        assert event.payload["trace_id"] == result.trace_id
        assert event.payload["project_id"] == project_id


def test_skill_failure_is_traced_without_fabricated_spec(tmp_path: Path) -> None:
    coordinator, store, runtime, calls = _setup(tmp_path)
    project_id = _project(store)
    original = coordinator.stage_executor

    def failing_executor(skill_id: str, inputs: Any, context: Any) -> dict[str, Any]:
        if skill_id == SKILLS[1]:
            raise ToolError("视觉导演执行失败")
        return original(skill_id, inputs, context)

    coordinator.stage_executor = failing_executor
    with pytest.raises(ToolError, match="视觉导演执行失败"):
        coordinator.execute(_request(store, project_id, "professional"))

    run = runtime.list_runs(domain="comic")[0]
    assert run.status == ExecutionStatus.FAILED
    assert run.state["failed_skill_id"] == SKILLS[1]
    assert run.state["error_id"].startswith("ERR-")
    assert _event_names(runtime, run.id)[-1] == "director_failed"
    assert runtime.list_events(run.id)[-1].payload["skill_id"] == SKILLS[1]
    with pytest.raises(ToolError, match="尚无导演方案"):
        store.get_director(project_id)

    coordinator.stage_executor = original
    resumed = coordinator.execute(_request(
        store, project_id, "professional",
        previous_run_id=run.id, rerun_from="visual_direction",
    ))
    assert resumed.director_spec.schema_version == 2
    assert runtime.get_run(run.id).status == ExecutionStatus.FAILED
    assert runtime.get_run(resumed.run_id).status == ExecutionStatus.COMPLETED


def test_asset_version_change_marks_downstream_stale(tmp_path: Path) -> None:
    coordinator, store, runtime, _calls = _setup(tmp_path)
    project_id = _project(store)

    def asset(version: int) -> ComicAsset:
        return ComicAsset(
            asset_id="character-1", project_id=project_id,
            name="少女", details=CharacterAsset(appearance="黑发少女"),
            version=version, project_version=version,
            created_at=datetime.now(UTC), source="created",
        )

    first = coordinator.execute(_request(store, project_id, "fast", assets=[asset(1)]))
    second = coordinator.execute(_request(
        store, project_id, "fast", assets=[asset(2)], prior_spec=first.director_spec,
    ))

    assert first.director_spec.asset_versions == {"asset:character-1": 1}
    assert second.director_spec.asset_versions == {"asset:character-1": 2}
    assert second.stale_dependents == ["storyboard", "shot", "prompt_artifact"]
    assert runtime.get_run(second.run_id).state["stale_dependents"] == second.stale_dependents
    created = next(
        event for event in runtime.list_events(second.run_id)
        if event.payload["director_event"] == "director_spec_created"
    )
    assert created.payload["stale_dependents"] == second.stale_dependents


def test_professional_can_rerun_from_a_stage_using_persisted_outputs(tmp_path: Path) -> None:
    coordinator, store, runtime, calls = _setup(tmp_path)
    project_id = _project(store)
    first = coordinator.execute(_request(store, project_id, "professional"))
    calls.clear()

    again = coordinator.execute(_request(
        store, project_id, "professional",
        previous_run_id=first.run_id, rerun_from="cinematography",
    ))

    assert calls == [SKILLS[2]]
    assert again.director_spec.version == first.director_spec.version + 1
    assert runtime.get_run(again.run_id).state["completed_stages"] == SKILLS
    assert again.run_id != first.run_id


def test_coordinator_only_dispatches_director_skills(tmp_path: Path) -> None:
    coordinator, store, runtime, calls = _setup(tmp_path)
    coordinator.execute(_request(store, _project(store), "fast"))

    assert set(calls) <= set(SKILLS)
    assert all("image" not in skill_id for skill_id in calls)
    assert len(runtime.list_runs(domain="comic")) == 1
