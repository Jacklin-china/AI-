"""Phase 7.2.2：导演 Skill manifest 与结构契约。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from kantoku.core.skills import SkillLoader, SkillRegistry
from kantoku.domains.comic.models import (
    CinematographyPlan,
    CreativeDecision,
    DirectorCriticResult,
    DirectorPlan,
    DirectorSpecDraft,
)

ROOT = Path(__file__).parents[1]
DIRECTOR_SKILLS = {
    "comic.creative_understanding": "creative_decision",
    "comic.visual_direction": "director_plan",
    "comic.cinematography": "cinematography",
    "comic.director_critic": "critic_result",
    "comic.director_assemble": "director_spec",
}
DIRECTOR_INPUTS = {
    "comic.creative_understanding": [
        "creative_brief", "project_context", "relevant_assets", "current_task",
    ],
    "comic.visual_direction": [
        "creative_decision", "character_assets", "scene_assets", "style_bible",
    ],
    "comic.cinematography": ["director_plan", "shot_context"],
    "comic.director_critic": ["director_spec"],
    "comic.director_assemble": [
        "creative_decision", "director_plan", "cinematography",
    ],
}


def _director_spec_v2() -> DirectorSpecDraft:
    return DirectorSpecDraft.model_validate({
        "schema_version": 2,
        "visual_direction": "让人物与环境的尺度差异表达战斗压力",
        "storytelling_goal": "表现少女在雨夜中做出迎战选择",
        "camera_language": "先建立空间，再靠近人物",
        "composition": "保留环境纵深并让人物成为视觉锚点",
        "lighting": "冷色散射光勾勒轮廓",
        "color_language": "冷色环境对比少量暖色高光",
        "emotion": "孤独但坚定",
        "character_focus": "保留角色身份和服装约束",
        "constraints": ["东方仙侠", "少女", "雨夜", "战斗"],
        "creative_choices": ["让环境先建立压力，再突出人物回应。"],
        "creative_decision": {
            "intent_summary": "表现人物面对未知力量时的决心",
            "narrative_context": "雨夜战斗开始前",
            "emotional_target": "孤独与坚定",
            "audience_experience": "先感受空间压力，再关注人物选择",
            "narrative_focus": "人物与环境的关系",
            "hard_constraints": ["东方仙侠", "少女", "雨夜", "战斗"],
        },
        "director_plan": {
            "visual_strategy": "用环境尺度承托人物的孤立",
            "visual_focus": "人物在雨夜竹林中的位置",
            "subject_environment_relation": "人物清晰但不吞没环境",
            "composition_strategy": "以纵深引导视线",
            "color_strategy": "冷色环境与暖色高光形成对比",
        },
        "cinematography": {
            "shot_size": "远景过渡到中近景",
            "camera_angle": "略低机位",
            "camera_distance": "先远后近",
            "spatial_feel": "有纵深的雨夜竹林",
            "lens_or_spatial_feel": "自然透视",
            "lighting": "冷色散射环境光衬托角色轮廓",
            "light_source": "散射天光与剑光",
            "light_direction": "侧后方轮廓光",
            "color_relationship": "蓝灰环境对比暖色高光",
            "depth_strategy": "前景雨丝、中景人物、后景竹林",
            "material_language": "湿润石面和布料反光",
        },
        "critic_result": {
            "verdict": "pass",
            "public_summary": "方案保留硬约束，视觉因果清晰。",
            "confidence": 0.9,
        },
    })


def _load_registry() -> SkillRegistry:
    registry = SkillRegistry()
    SkillLoader(ROOT / "skills", project_root=ROOT).load(registry)
    return registry


def test_director_manifests_are_discoverable_and_have_required_metadata() -> None:
    registry = _load_registry()
    metadata = {item.id: item for item in registry.list()}

    assert DIRECTOR_SKILLS.keys() <= metadata.keys()
    for skill_id, output_key in DIRECTOR_SKILLS.items():
        item = metadata[skill_id]
        assert item.domain == "comic"
        assert item.version == ("1.1.0" if skill_id in {
            "comic.director_critic", "comic.cinematography",
        } else "1.0.0")
        assert item.required_tools == ()
        assert item.input_schema["type"] == "object"
        assert item.input_schema["required"] == DIRECTOR_INPUTS[skill_id]
        assert item.output_schema["type"] == "object"
        assert item.output_schema["required"] == (
            ["public_decision", "structured_plan", "creative_reason"]
            if skill_id == "comic.cinematography" else [output_key])
        assert set(item.execution_policy["allowed_execution_modes"]) == {
            "fast", "professional",
        }
        assert item.execution_policy["provider_binding"] is False
        assert item.execution_policy["coordinator_owned"] is True
        assert item.execution_policy["contract_only"] is True
        if skill_id == "comic.director_assemble":
            assert item.execution_policy["optional_in_fast"] == ["critic_result"]
        assert item.knowledge_refs
        assert item.test_cases


def test_director_skill_contracts_validate_outputs_without_provider_or_asset_side_effects() -> None:
    registry = _load_registry()
    assets = [{"asset_id": "character-1", "version": 1}]
    context = {"relevant_assets": assets}
    original_context = deepcopy(context)
    draft = _director_spec_v2()

    outputs = {
        "comic.creative_understanding": {"creative_decision": draft.creative_decision},
        "comic.visual_direction": {"director_plan": draft.director_plan},
        "comic.cinematography": {"cinematography": draft.cinematography},
        "comic.director_critic": {"critic_result": draft.critic_result},
        "comic.director_assemble": {"director_spec": draft.model_dump()},
    }
    validators = {
        "comic.creative_understanding": CreativeDecision,
        "comic.visual_direction": DirectorPlan,
        "comic.cinematography": CinematographyPlan,
        "comic.director_critic": DirectorCriticResult,
        "comic.director_assemble": DirectorSpecDraft,
    }

    for skill_id, output_key in DIRECTOR_SKILLS.items():
        result = registry.execute(
            skill_id,
            {"_contract_output": outputs[skill_id]},
            context,
        )
        assert output_key in result
        validators[skill_id].model_validate(result[output_key])

    assert context == original_context
