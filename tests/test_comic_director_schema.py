"""Phase 7.2 第一阶段：DirectorSpec v2 数据契约与 legacy 读取。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from kantoku.domains.comic.models import (
    ComicProjectInput,
    DirectorSpec,
    DirectorSpecDraft,
)
from kantoku.domains.comic.projects import ComicContextBuilder, ComicProjectStore


def _legacy_payload() -> dict[str, object]:
    return {
        "spec_id": "director-legacy",
        "project_id": "project-1",
        "creative_brief_version": 1,
        "version": 1,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "source": "model",
        "visual_direction": "雨夜中突出人物的孤立感",
        "storytelling_goal": "表现少女迎战前的决心",
        "camera_language": "先建立环境，再靠近人物",
        "composition": "让环境空间参与叙事",
        "lighting": "冷色环境光勾勒人物轮廓",
        "color_language": "冷色为主，保留少量暖色",
        "emotion": "孤独但坚定",
        "character_focus": "保留东方仙侠少女身份",
        "constraints": ["东方仙侠", "少女", "雨夜", "战斗"],
        "creative_choices": ["先展示环境，再突出人物。"],
    }


def _v2_draft() -> DirectorSpecDraft:
    return DirectorSpecDraft.model_validate({
        "schema_version": 2,
        "visual_direction": "让人物的孤立感与雨夜环境形成张力",
        "storytelling_goal": "表现少女迎战前的决心",
        "camera_language": "先建立环境，再靠近人物",
        "composition": "让环境空间参与叙事",
        "lighting": "冷色环境光勾勒人物轮廓",
        "color_language": "冷色为主，保留少量暖色",
        "emotion": "孤独但坚定",
        "character_focus": "保留东方仙侠少女身份",
        "constraints": ["东方仙侠", "少女", "雨夜", "战斗"],
        "creative_choices": ["先展示环境，再突出人物。"],
        "creative_decision": {
            "intent_summary": "让观众感到一个人面对未知力量的决心",
            "narrative_context": "战斗开始前的雨夜",
            "emotional_target": "孤独与坚定并存",
            "audience_experience": "先感受环境压力，再关注人物选择",
            "narrative_focus": "人物与环境的反差",
            "hard_constraints": ["东方仙侠", "少女", "雨夜", "战斗"],
            "soft_preferences": ["电影感", "冷色调"],
            "creative_freedom": ["构图优化", "环境细节"],
        },
        "director_plan": {
            "visual_strategy": "用环境尺度承托人物的孤立，再以动作建立反击力量",
            "visual_focus": "人物面对雨幕和竹林的关系",
            "subject_environment_relation": "人物占画面较小比例但保持清晰视觉锚点",
            "composition_strategy": "以纵深路径把视线引向人物",
            "color_strategy": "冷色环境与剑光暖色形成有限对比",
            "continuity_rules": ["角色服装和发饰保持一致"],
            "creative_choices": ["先展示环境压力，避免一开始就用特写消解空间。"],
            "risk_flags": ["雨幕过密可能遮挡角色动作"],
        },
        "cinematography": {
            "shot_size": "远景过渡到中近景",
            "camera_angle": "略低机位",
            "camera_distance": "先远后近",
            "spatial_feel": "有纵深的竹林雨幕",
            "lens_or_spatial_feel": "自然透视，保留环境层次",
            "lighting": "冷色散射环境光衬托角色轮廓",
            "light_source": "雨云散射光与剑光",
            "light_direction": "侧后方勾勒轮廓",
            "color_relationship": "蓝灰环境对比少量暖色高光",
            "depth_strategy": "前景雨丝、中景人物、后景竹林分层",
            "material_language": "湿润石面与布料反光",
        },
        "critic_result": {
            "verdict": "pass",
            "public_summary": "硬约束完整，导演策略与雨夜战斗目标一致。",
            "findings": [{
                "severity": "info",
                "category": "continuity",
                "message": "角色资产约束已被保留。",
            }],
            "suggested_patches": [{
                "field": "composition_strategy",
                "reason": "后续镜头继续保留竹林纵深。",
            }],
            "confidence": 0.92,
            "knowledge_refs": ["cinematography.spatial-contrast"],
        },
        "knowledge_refs": ["director.intent-first", "cinematography.spatial-contrast"],
    })


def test_director_spec_v2_round_trips_layered_decisions() -> None:
    draft = _v2_draft()
    spec = DirectorSpec(
        **draft.model_dump(),
        spec_id="director-v2",
        project_id="project-1",
        creative_brief_version=1,
        version=1,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        source="model",
    )

    restored = DirectorSpec.model_validate_json(spec.model_dump_json())

    assert restored.schema_version == 2
    assert restored.creative_decision is not None
    assert restored.director_plan is not None
    assert restored.cinematography is not None
    assert restored.critic_result is not None
    assert restored.critic_result.verdict == "pass"
    assert restored.knowledge_refs == [
        "director.intent-first", "cinematography.spatial-contrast",
    ]


def test_legacy_director_payload_is_read_without_fabricating_v2_layers() -> None:
    legacy = DirectorSpec.model_validate(_legacy_payload())

    assert legacy.schema_version == 1
    assert legacy.creative_decision is None
    assert legacy.director_plan is None
    assert legacy.cinematography is None
    assert legacy.critic_result is None
    assert legacy.knowledge_refs == []

    # Round-tripping a legacy record keeps it explicitly legacy.
    assert DirectorSpec.model_validate_json(legacy.model_dump_json()).schema_version == 1


def test_v2_requires_the_three_layered_director_decisions() -> None:
    with pytest.raises(ValueError, match="creative_decision"):
        DirectorSpecDraft.model_validate({
            "schema_version": 2,
            "visual_direction": "雨夜中的孤立感",
            "storytelling_goal": "表现迎战前的决心",
            "camera_language": "先远后近",
            "composition": "保留环境纵深",
            "lighting": "冷色环境光",
            "color_language": "冷色为主",
            "emotion": "孤独但坚定",
            "character_focus": "保持角色身份",
            "constraints": ["雨夜"],
            "creative_choices": ["环境先于人物"],
        })


def test_context_builder_exposes_v2_fields_without_a_second_context_model(tmp_path: Path) -> None:
    store = ComicProjectStore(tmp_path / "comic.db")
    project = store.create(ComicProjectInput.model_validate({
        "title": "东方仙侠雨夜",
        "brief": {
            "original_request": "东方仙侠少女雨夜战斗",
            "hard_constraints": ["东方仙侠", "少女", "雨夜", "战斗"],
        },
    }))
    spec = store.save_director(
        project.project.project_id, _v2_draft(), expected_project_version=1, source="model",
    )

    context = ComicContextBuilder.build(store.get(project.project.project_id), director=spec)
    director = context.stable_context["director_spec"]

    assert director["schema_version"] == 2
    assert director["creative_decision"]["intent_summary"].startswith("让观众")
    assert director["director_plan"]["visual_strategy"]
    assert director["cinematography"]["shot_size"] == "远景过渡到中近景"
    assert director["critic_result"]["verdict"] == "pass"
