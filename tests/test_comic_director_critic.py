"""Phase 7.2.4：公开证据审核、受限 Patch 和生成前门禁。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from loguru import logger
from test_comic_director_coordinator import SKILLS, _parts, _project, _request, _setup
from test_comic_prompts import _setup as prompt_setup

from kantoku.config import ToolError
from kantoku.config.observability import request_trace
from kantoku.core.runtime.models import ExecutionStatus
from kantoku.core.runtime.store import RuntimeStore
from kantoku.domains.comic.critic import (
    DirectorCriticEngine,
    director_hash,
    require_approved_director,
)
from kantoku.domains.comic.models import (
    CharacterAsset,
    ComicAsset,
    CreativeBriefInput,
    DirectorCriticPatch,
    DirectorSpec,
    DirectorSpecDraft,
)
from kantoku.domains.comic.prompts import compiler_for_model


def _spec() -> DirectorSpecDraft:
    return DirectorSpecDraft.model_validate(_parts()[SKILLS[4]]["director_spec"])


def _brief() -> CreativeBriefInput:
    return CreativeBriefInput(
        original_request="东方仙侠少女雨夜战斗，要震撼",
        hard_constraints=["东方仙侠", "少女", "雨夜", "战斗"],
    )


def _semantic(
    findings: list[dict[str, Any]] | None = None, patches: list[dict[str, Any]] | None = None,
) -> str:
    return json.dumps({
        "public_summary": "公开视觉策略审核结果。", "confidence": 0.9,
        "findings": findings or [], "suggested_patches": patches or [],
    }, ensure_ascii=False)


def _finding(spec: DirectorSpecDraft, *, severity: str = "warning") -> dict[str, str]:
    assert spec.director_plan is not None
    return {
        "code": "COMPOSITION_CAUSALITY", "severity": severity,
        "field_path": "director_plan.composition_strategy",
        "evidence": spec.director_plan.composition_strategy,
        "expected": "构图具体表现少女与环境的叙事关系",
        "suggested_action": "调整构图描述以连接环境压力与人物选择",
    }


def _patch(spec: DirectorSpecDraft) -> dict[str, str]:
    assert spec.director_plan is not None
    return {
        "field": "director_plan.composition_strategy",
        "expected_value": spec.director_plan.composition_strategy,
        "value": "人物置于竹林纵深尽头，用空旷前景突出孤立迎战的选择",
        "reason": "将环境压力与人物处境连接，保留雨夜战斗要求",
    }


def test_good_plan_passes_with_a_hash_bound_to_the_actual_spec() -> None:
    seen: list[list[dict[str, str]]] = []

    def model(messages: list[dict[str, str]]) -> str:
        seen.append(messages)
        return _semantic()

    outcome = DirectorCriticEngine(model).review_and_revise(_spec(), _brief())

    assert outcome.critic_result.verdict == "pass"
    assert outcome.revision_count == 0
    require_approved_director(outcome.director_spec)
    context = json.loads(seen[0][1]["content"])
    assert context["relevant_assets"] == []
    assert "chat_history" not in context


@pytest.mark.parametrize("field", ["director_plan.visual_strategy",
                                  "director_plan.creative_choices",
                                  "cinematography.light_direction",
                                  "cinematography.material_language"])
def test_valid_v2_review_fields_are_not_mistaken_for_patch_fields(field: str) -> None:
    spec = _spec()
    section, name = field.split(".")
    value = spec.model_dump()[section][name]
    evidence = value[0] if isinstance(value, list) else value
    finding = {**_finding(spec), "field_path": field, "evidence": evidence}
    result = DirectorCriticEngine(lambda _: _semantic([finding])).review(spec, _brief())
    assert result.verdict == "needs_revision"
    assert result.findings[0].field_path == field


def test_whitespace_only_evidence_difference_is_not_a_failure() -> None:
    spec = _spec()
    finding = _finding(spec)
    finding["evidence"] = " \n".join(finding["evidence"])
    result = DirectorCriticEngine(lambda _: _semantic([finding], [_patch(spec)])).review(
        spec, _brief(),
    )
    assert result.findings[0].code == "COMPOSITION_CAUSALITY"
    assert len(result.suggested_patches) == 1


def test_additive_optional_fields_keep_historical_approval_hash_compatible() -> None:
    spec = _spec()
    payload = spec.model_dump(include=set(DirectorSpecDraft.model_fields) - {"critic_result"})
    for field in ("style_boundary", "character_expression", "character_pose", "character_presence"):
        payload["director_plan"].pop(field)
    historical_hash = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    assert director_hash(spec) == historical_hash


def test_wrong_expected_value_is_review_needed_not_a_failed_task() -> None:
    patch = {**_patch(_spec()), "expected_value": "另一份历史方案的内容"}
    result = DirectorCriticEngine(lambda _messages: _semantic(
        [_finding(_spec())], [patch],
    )).review_and_revise(_spec(), _brief())
    assert result.needs_review
    assert result.revision_count == 0
    assert result.critic_result.suggested_patches == []


def test_duplicate_model_patch_is_applied_only_once() -> None:
    patch = _patch(_spec())
    replies = [_semantic([_finding(_spec())], [patch, patch]), _semantic()]
    result = DirectorCriticEngine(lambda _messages: replies.pop(0)).review_and_revise(
        _spec(), _brief(),
    )
    assert not result.needs_review
    assert result.revision_count == 1


def test_empty_template_quality_words_are_detected() -> None:
    data = _spec().model_dump()
    data["director_plan"]["composition_strategy"] = "cinematic masterpiece beautiful epic"
    spec = DirectorSpecDraft.model_validate(data)

    result = DirectorCriticEngine(lambda _messages: _semantic()).review(spec, _brief())

    assert result.verdict == "needs_revision"
    assert any(item.code == "EMPTY_QUALITY_WORDS" for item in result.findings)


def test_missing_hard_constraint_blocks_without_calling_model() -> None:
    def forbidden(_messages: Any) -> str:
        pytest.fail("硬约束缺失不应花费语义模型调用")

    spec = _spec().model_copy(update={"constraints": ["少女"]})
    result = DirectorCriticEngine(forbidden).review(spec, _brief())

    assert result.verdict == "blocked"
    assert result.allowed_patches == []
    assert result.findings[0].code == "HARD_CONSTRAINT_MISSING"


def test_semantic_hard_constraint_conflict_is_blocked() -> None:
    finding = _finding(_spec(), severity="error")
    finding["code"] = "HARD_CONSTRAINT_CONFLICT"
    result = DirectorCriticEngine(lambda _messages: _semantic([finding])).review(_spec(), _brief())
    assert result.verdict == "blocked"
    assert result.allowed_patches == []


def test_all_fifty_missing_constraints_still_produce_a_blocked_result() -> None:
    brief = CreativeBriefInput(
        original_request="一个有多项明确约束的创作需求",
        hard_constraints=[f"必须保留第{index}个要素" for index in range(50)],
    )
    spec_data = _spec().model_dump()
    spec_data["constraints"] = []
    spec_data["creative_decision"]["hard_constraints"] = []
    result = DirectorCriticEngine(lambda _messages: pytest.fail("结构性错误不调用模型")).review(
        DirectorSpecDraft.model_validate(spec_data), brief,
    )
    assert result.verdict == "blocked"
    assert len(result.findings) == 2


def test_asset_version_error_is_blocked_with_exact_evidence() -> None:
    asset = ComicAsset(
        asset_id="girl", project_id="project-1", version=2, project_version=2,
        name="少女", details=CharacterAsset(appearance="黑发"),
        created_at=datetime.now(UTC), source="edited",
    )
    spec = _spec().model_copy(update={"asset_versions": {"asset:girl": 1}})
    result = DirectorCriticEngine(lambda _messages: pytest.fail("版本冲突不调用模型")).review(
        spec, _brief(), assets=[asset],
    )
    assert result.verdict == "blocked"
    mismatch = next(item for item in result.findings if item.code == "ASSET_VERSION_MISMATCH")
    assert "1" in mismatch.evidence and "2" in mismatch.expected


def test_patch_whitelist_rejects_constraint_rewrites_and_stale_values() -> None:
    spec = _spec()
    result = DirectorCriticEngine(lambda _messages: _semantic([
        _finding(spec),
    ], [_patch(spec)])).review(spec, _brief())
    malicious = result.model_copy(update={"suggested_patches": [DirectorCriticPatch(
        field="creative_decision.hard_constraints", reason="尝试替换主体",
        value="少年", expected_value="少女",
    )]})
    with pytest.raises(ToolError, match="禁止修改"):
        DirectorCriticEngine.apply_patches(spec, malicious)
    changed = spec.model_copy(update={"composition": "用户已修改"})
    with pytest.raises(ToolError, match="不一致"):
        DirectorCriticEngine.apply_patches(changed, result)


def test_minor_patch_is_applied_once_and_passes_one_re_review() -> None:
    spec = _spec()
    replies = [_semantic([_finding(spec)], [_patch(spec)]), _semantic()]
    seen: list[str] = []

    def model(_messages: Any) -> str:
        return replies.pop(0)

    outcome = DirectorCriticEngine(model).review_and_revise(
        spec, _brief(), emit=lambda name, _payload: seen.append(name),
    )
    assert not outcome.needs_review
    assert outcome.revision_count == 1
    assert seen.count("director_patch_applied") == 1
    assert seen.count("director_critic_completed") == 2
    assert "director_revision_requested" in seen
    assert outcome.director_spec.constraints == spec.constraints
    assert outcome.director_spec.creative_decision == spec.creative_decision
    assert outcome.director_spec.asset_versions == spec.asset_versions
    assert outcome.director_spec.composition == _patch(spec)["value"]
    require_approved_director(outcome.director_spec)


def test_patch_aliases_are_normalized_before_whitelist_validation() -> None:
    spec = _spec()
    assert spec.creative_decision is not None
    assert spec.director_plan is not None
    assert spec.cinematography is not None
    findings = [
        {
            "code": "EMOTION_CAUSALITY", "severity": "warning",
            "field_path": "emotion", "evidence": spec.creative_decision.emotional_target,
            "expected": "情绪与故事处境建立具体关系", "suggested_action": "收紧情绪目标",
        },
        {
            "code": "VISUAL_FOCUS", "severity": "warning",
            "field_path": "director_spec.visual_direction",
            "evidence": spec.director_plan.visual_focus,
            "expected": "明确视觉焦点", "suggested_action": "收紧视觉焦点",
        },
        {
            "code": "CAMERA_LANGUAGE", "severity": "warning",
            "field_path": "camera_language", "evidence": spec.camera_language,
            "expected": "摄影语言服务故事", "suggested_action": "调整摄影语言",
        },
    ]
    patches = [
        {"field": "emotion", "expected_value": spec.emotion,
         "value": "孤独感来自人物与环境的距离", "reason": "连接故事处境"},
        {"field": "visual_direction", "expected_value": spec.visual_direction,
         "value": "人物面对雨夜竹林的选择", "reason": "明确视觉焦点"},
        {"field": "director_spec.camera_language", "expected_value": spec.camera_language,
         "value": "先建立空间压力，再靠近人物选择", "reason": "服务叙事"},
    ]
    replies = [_semantic(findings, patches), _semantic()]

    outcome = DirectorCriticEngine(lambda _messages: replies.pop(0)).review_and_revise(
        spec, _brief(),
    )

    assert outcome.revision_count == 1
    assert not outcome.needs_review
    assert outcome.director_spec.creative_decision.emotional_target == patches[0]["value"]
    assert outcome.director_spec.director_plan.visual_focus == patches[1]["value"]
    assert outcome.director_spec.cinematography.camera_language == patches[2]["value"]
    assert outcome.director_spec.emotion == patches[0]["value"]
    assert outcome.director_spec.visual_direction == patches[1]["value"]
    assert outcome.director_spec.camera_language == patches[2]["value"]


def test_brief_field_is_mapped_to_director_spec_without_failing_review() -> None:
    spec = _spec()
    finding = {
        "code": "INTENT_ALIGNMENT", "severity": "warning",
        "field_path": "creative_brief.original_request",
        "evidence": _brief().original_request,
        "expected": "创意理解准确概括用户请求",
        "suggested_action": "在创意理解中明确用户的原始叙事目标",
    }

    result = DirectorCriticEngine(
        lambda _messages: _semantic([finding]),
    ).review(spec, _brief())

    assert result.verdict == "needs_revision"
    assert result.findings[-1].field_path == "creative_decision.intent_summary"
    assert result.suggested_patches == []


def test_unknown_critic_field_is_warned_and_ignored() -> None:
    records: list[str] = []
    sink_id = logger.add(lambda record: records.append(str(record)))
    try:
        with request_trace("trace-ignore-field"):
            result = DirectorCriticEngine(lambda _messages: _semantic([{
                "code": "OUT_OF_SCOPE", "severity": "warning",
                "field_path": "asset.character_id", "evidence": "unknown",
                "expected": "不得审核资产身份", "suggested_action": "忽略该字段",
            }])).review(_spec(), _brief())
    finally:
        logger.remove(sink_id)

    assert result.verdict == "pass"
    assert result.findings == []
    log = "".join(records)
    assert "critic_warning" in log
    assert "received_field=asset.character_id" in log
    assert "action=ignored" in log
    assert "trace_id=trace-ignore-field" in log


def test_patch_adapter_maps_phase_73_director_fields() -> None:
    spec = _spec()
    assert spec.creative_decision is not None
    assert spec.director_plan is not None
    assert spec.cinematography is not None
    evidence = spec.director_plan.visual_focus
    findings = [
        {
            "code": "EMOTIONAL_TARGET", "severity": "warning",
            "field_path": "emotional_target", "evidence": spec.creative_decision.emotional_target,
            "expected": "情绪目标服务故事", "suggested_action": "调整情绪目标",
        },
        {
            "code": "COLOR_DIRECTION", "severity": "warning",
            "field_path": "color_direction", "evidence": spec.director_plan.color_strategy,
            "expected": "色彩形成叙事关系", "suggested_action": "调整色彩方向",
        },
        {
            "code": "CAMERA_ANGLE", "severity": "warning",
            "field_path": "camera_angle", "evidence": spec.cinematography.camera_angle,
            "expected": "机位服务人物处境", "suggested_action": "调整机位",
        },
        {
            "code": "CHARACTER_PRESENCE", "severity": "warning",
            "field_path": "character_presence", "evidence": evidence,
            "expected": "明确人物在画面中的存在方式", "suggested_action": "补充人物表现",
        },
    ]
    patches = [
        {"field": "emotional_target", "expected_value": spec.creative_decision.emotional_target,
         "value": "克制的决心来自独自迎战", "reason": "连接情境"},
        {"field": "color_direction", "expected_value": spec.director_plan.color_strategy,
         "value": "冷雨压低环境，剑光只强调人物选择", "reason": "连接叙事"},
        {"field": "camera_angle", "expected_value": spec.cinematography.camera_angle,
         "value": "平视跟随人物，避免固定英雄低机位", "reason": "避免公式化"},
        {"field": "character_presence", "expected_value": None,
         "value": "人物不以体量取胜，以静止姿态对抗环境运动", "reason": "明确人物表现"},
    ]
    replies = [_semantic(findings, patches), _semantic()]

    outcome = DirectorCriticEngine(
        lambda _messages: replies.pop(0),
    ).review_and_revise(spec, _brief())

    assert outcome.revision_count == 1
    assert not outcome.needs_review
    assert outcome.director_spec.creative_decision.emotional_target == patches[0]["value"]
    assert outcome.director_spec.director_plan.color_strategy == patches[1]["value"]
    assert outcome.director_spec.cinematography.camera_angle == patches[2]["value"]
    assert outcome.director_spec.director_plan.character_presence == patches[3]["value"]
    assert outcome.director_spec.character_focus == patches[3]["value"]


def test_model_patch_outside_whitelist_is_ignored_not_a_task_failure() -> None:
    spec = _spec()
    result = DirectorCriticEngine(lambda _messages: _semantic(
        [_finding(spec)],
        [{"field": "project.id", "expected_value": "current", "value": "other",
          "reason": "模型错误地尝试修改身份"}],
    )).review(spec, _brief())

    assert result.verdict == "needs_revision"
    assert result.allowed_patches == ["director_plan.composition_strategy"]
    assert result.suggested_patches == []


@pytest.mark.parametrize("field", [
    "creative_decision.hard_constraints", "asset_versions", "project_id",
    "style_bible", "director_spec.asset_versions",
])
def test_patch_normalizer_never_opens_identity_or_asset_fields(field: str) -> None:
    spec = _spec()
    result = DirectorCriticEngine(lambda _messages: _semantic([
        _finding(spec),
    ], [_patch(spec)])).review(spec, _brief())
    malicious = result.model_copy(update={"suggested_patches": [DirectorCriticPatch(
        field=field, reason="尝试修改受保护来源", value="other", expected_value="current",
    )]})
    with pytest.raises(ToolError, match="禁止修改"):
        DirectorCriticEngine.apply_patches(spec, malicious)


def test_second_review_failure_does_not_trigger_a_second_automatic_patch() -> None:
    calls: list[Any] = []

    def model(messages: Any) -> str:
        calls.append(messages)
        current = DirectorSpecDraft.model_validate(
            json.loads(messages[1]["content"])["director_spec"],
        )
        return _semantic([_finding(current)], [_patch(current)])

    events: list[str] = []
    outcome = DirectorCriticEngine(model).review_and_revise(
        _spec(), _brief(), emit=lambda name, _payload: events.append(name),
    )
    assert len(calls) == 2
    assert outcome.revision_count == 1
    assert outcome.needs_review
    assert events.count("director_patch_applied") == 1
    assert events[-1] == "director_review_blocked"


def test_semantic_review_rejects_cot_and_fabricated_evidence() -> None:
    bad = json.loads(_semantic())
    bad["reasoning"] = "PRIVATE_COT_SHOULD_NOT_BE_SAVED"
    engine = DirectorCriticEngine(lambda _messages: json.dumps(bad))
    records: list[str] = []
    sink_id = logger.add(lambda record: records.append(str(record)))
    try:
        with pytest.raises(ToolError, match="公开审核结构"):
            engine.review(_spec(), _brief())
    finally:
        logger.remove(sink_id)
    assert "PRIVATE_COT_SHOULD_NOT_BE_SAVED" not in "".join(records)
    with pytest.raises(ToolError, match="公开审核结构"):
        DirectorCriticEngine(lambda _messages: json.dumps({
            "public_summary": "字段缺失不能默认成功", "confidence": 0.9,
        })).review(_spec(), _brief())
    fabricated = _finding(_spec())
    fabricated["evidence"] = "不存在于输入的虚构导演方案"
    result = DirectorCriticEngine(lambda _messages: _semantic([fabricated])).review(
        _spec(), _brief(),
    )
    assert result.verdict == "needs_revision"
    assert result.findings[0].code == "UNVERIFIED_REVIEW_EVIDENCE"
    assert result.suggested_patches == []


def test_major_review_waits_in_existing_run_without_saving_a_spec(tmp_path: Path) -> None:
    coordinator, projects, runtime, _calls = _setup(tmp_path)
    project_id = _project(projects)
    coordinator.critic_engine = DirectorCriticEngine(
        lambda _messages: _semantic([_finding(_spec(), severity="error")]),
    )

    result = coordinator.execute(_request(projects, project_id, "professional"))

    assert result.needs_review and result.director_spec is None
    assert result.critic_result.verdict == "needs_revision"
    run = RuntimeStore(runtime.path).get_run(result.run_id)
    assert run.status == ExecutionStatus.WAITING
    assert run.state["task_status"] == "needs_review"
    assert run.state["director_candidate"]["schema_version"] == 2
    assert len(runtime.list_runs()) == 1
    names = [item.payload["director_event"] for item in runtime.list_events(run.id)]
    assert "director_critic_started" in names
    assert "director_critic_completed" in names
    assert "director_review_blocked" in names
    assert "director_spec_created" not in names
    with pytest.raises(ToolError, match="尚无导演方案"):
        projects.get_director(project_id)


def test_compiler_rejects_unreviewed_or_changed_v2_before_model_call(tmp_path: Path) -> None:
    _runtime, _projects, _assets, _boards, prompts, shot, _unrelated = prompt_setup(
        tmp_path / "comic.db",
    )
    snapshot, _old_director, board, current_shot, selected = prompts.source(shot.shot_id)
    spec = DirectorSpec(
        **_spec().model_dump(), project_id=snapshot.project.project_id,
        spec_id="v2", creative_brief_version=1, version=1,
        created_at=datetime.now(UTC), source="model",
    )
    with pytest.raises(ToolError, match="尚未通过"):
        compiler_for_model("qwen-image-3.0").compile(
            snapshot=snapshot, director=spec, storyboard=board, shot=current_shot,
            assets=selected, model_target="qwen-image-3.0",
            model_call=lambda _messages: pytest.fail("未通过审核不能调用编译模型"),
        )
    approved = DirectorCriticEngine(lambda _messages: _semantic()).review_and_revise(
        _spec(), _brief(),
    ).director_spec
    assert approved.critic_result.reviewed_spec_hash == director_hash(approved)
    with pytest.raises(ToolError, match="尚未通过"):
        require_approved_director(approved.model_copy(update={"lighting": "已改动"}))


def test_no_image_provider_is_called_during_review(monkeypatch: pytest.MonkeyPatch) -> None:
    from kantoku.tools import image_gen

    monkeypatch.setattr(image_gen, "gen_image", lambda *args, **kwargs: pytest.fail(
        "导演审核不得调用图片 Provider",
    ))
    outcome = DirectorCriticEngine(lambda _messages: _semantic()).review_and_revise(
        _spec(), _brief(),
    )
    assert outcome.critic_result.verdict == "pass"


def test_default_engine_uses_existing_shared_text_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from kantoku.core import llm

    calls: list[Any] = []

    def shared_chat(messages: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        assert json.loads(messages[1]["content"])["creative_brief"]["original_request"]
        return SimpleNamespace(content=_semantic())

    monkeypatch.setattr(llm, "chat", shared_chat)
    result = DirectorCriticEngine().review(_spec(), _brief())
    assert result.verdict == "pass"
    assert calls == [{"response_format": {"type": "json_object"}}]


def test_coordinator_persists_patch_events_and_the_approved_candidate(tmp_path: Path) -> None:
    coordinator, projects, runtime, _calls = _setup(tmp_path)
    project_id = _project(projects)
    replies = [_semantic([_finding(_spec())], [_patch(_spec())]), _semantic()]
    coordinator.critic_engine = DirectorCriticEngine(lambda _messages: replies.pop(0))

    result = coordinator.execute(_request(projects, project_id, "professional"))

    assert not result.needs_review
    require_approved_director(projects.get_director(project_id))
    assert result.director_spec.composition == _patch(_spec())["value"]
    run = runtime.get_run(result.run_id)
    assert run.state["revision_count"] == 1
    events = runtime.list_events(run.id)
    names = [item.payload["director_event"] for item in events]
    assert names.count("director_patch_applied") == 1
    assert names.count("director_critic_completed") == 2
    for event in events:
        assert event.payload["trace_id"] == result.trace_id
        assert event.payload["project_id"] == project_id


def test_review_failure_preserves_real_draft_without_approving_it(tmp_path: Path) -> None:
    coordinator, projects, runtime, _calls = _setup(tmp_path)
    project_id = _project(projects)

    def unavailable(_messages: Any) -> str:
        raise ToolError("审核模型暂不可用")

    coordinator.critic_engine = DirectorCriticEngine(unavailable)
    result = coordinator.execute(_request(projects, project_id, "professional"))
    run = runtime.list_runs()[0]
    assert run.status == ExecutionStatus.WAITING
    assert result.needs_review and result.director_spec is None
    assert result.director_draft is not None
    assert result.director_draft.creative_decision == _spec().creative_decision
    assert run.state["failed_skill_id"] == "comic.director_critic"
    assert run.state["error_id"].startswith("ERR-")
    assert run.state["completed_stages"] == SKILLS[:3]
    assert run.state["last_completed_step"] == "cinematography"
    assert run.state["stage_statuses"][SKILLS[3]] == "failed"
    failure = run.state["stage_failures"][SKILLS[3]]
    assert failure["trace_id"] == result.trace_id
    assert failure["input_version"]["creative_brief"] == 1
    assert set(failure["output_before_failure"]) == set(SKILLS[:3])
    assert failure["exception"]["type"] == "ToolError"
    names = [event.payload["director_event"] for event in runtime.list_events(run.id)]
    assert "director_draft_created" in names
    assert "director_critic_failed" in names
    assert "director_stage_failed" in names
    assert "director_critic_completed" not in names
    with pytest.raises(ToolError):
        require_approved_director(result.director_draft)
    assert projects.get(project_id).project.director_version is None


@pytest.mark.parametrize("patch", [
    {"field": "creative_brief.original_request", "value": "不能改用户需求"},
    {"field": "project_id", "value": "another-project"},
    {"field": "director_plan.composition_strategy", "value": "   "},
    {"field": "director_plan.composition_strategy", "value": {"reasoning": "private"}},
    {"field": "director_plan.composition_strategy", "value": "有效构图", "extra": True},
])
def test_invalid_patch_is_diagnostic_not_a_destroyed_director(patch: dict[str, Any]) -> None:
    spec = _spec()
    malformed = {**_patch(spec), **patch}
    engine = DirectorCriticEngine(lambda _: _semantic([_finding(spec)], [malformed]))
    result = engine.review_and_revise(spec, _brief())
    assert result.needs_review
    assert result.revision_count == 0
    assert any(item.code == "INVALID_PATCH" for item in result.critic_result.findings)
    assert result.critic_result.suggested_patches == []
    assert result.director_spec.director_plan == spec.director_plan
    assert result.director_spec.constraints == spec.constraints
    assert "private" not in result.model_dump_json()
    with pytest.raises(ToolError):
        require_approved_director(result.director_spec)


def test_patch_application_exception_retains_candidate_and_no_false_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    engine = DirectorCriticEngine(lambda _: _semantic([_finding(spec)], [_patch(spec)]))

    def fail_apply(_spec: Any, _result: Any) -> DirectorSpecDraft:
        raise ToolError("模拟 Patch 校验失败")

    monkeypatch.setattr(engine, "apply_patches", fail_apply)
    events: list[tuple[str, dict[str, Any]]] = []
    with request_trace("trace-patch-failure"):
        result = engine.review_and_revise(
            spec, _brief(), emit=lambda name, payload: events.append((name, payload)),
        )
    assert result.needs_review and result.revision_count == 0
    assert result.director_spec.director_plan == spec.director_plan
    failure = next(payload for name, payload in events if name == "director_patch_invalid")
    assert failure["error_id"].startswith("ERR-")
    assert failure["trace_id"] == "trace-patch-failure"
    assert not any(name == "director_patch_applied" for name, _payload in events)


def test_invalid_patch_diagnostics_do_not_overflow_review_schema() -> None:
    spec = _spec()
    findings = [{**_finding(spec), "severity": "info"} for _ in range(45)]
    patches = [{**_patch(spec), "field": "project_id"} for _ in range(20)]
    result = DirectorCriticEngine(lambda _: _semantic(findings, patches)).review(spec, _brief())
    assert len(result.findings) == 50
    assert result.findings[-1].code == "REVIEW_FINDINGS_TRUNCATED"
    assert result.suggested_patches == []


def test_draft_is_persisted_before_first_critic_call(tmp_path: Path) -> None:
    coordinator, projects, runtime, _calls = _setup(tmp_path)
    project_id = _project(projects)

    def review(_messages: Any) -> str:
        run = runtime.list_runs()[0]
        candidate = DirectorSpecDraft.model_validate(run.state["director_candidate"])
        assert candidate.schema_version == 2
        assert run.state["completed_stages"] == SKILLS[:3]
        assert run.state["draft_status"] == "generated"
        return _semantic()

    coordinator.critic_engine = DirectorCriticEngine(review)
    result = coordinator.execute(_request(projects, project_id, "fast"))
    assert result.director_spec is not None and not result.needs_review


def test_failure_after_one_patch_persists_revision_and_resume_reviews_it(tmp_path: Path) -> None:
    coordinator, projects, runtime, _calls = _setup(tmp_path)
    project_id = _project(projects)
    calls = 0

    def review(_messages: Any) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _semantic([_finding(_spec())], [_patch(_spec())])
        # 第二次审核尚未返回时，修订后的真实候选已经写入原 Run。
        candidate = runtime.list_runs()[0].state["director_candidate"]
        assert candidate["director_plan"]["composition_strategy"] == _patch(_spec())["value"]
        raise ToolError("第二次审核请求失败")

    coordinator.critic_engine = DirectorCriticEngine(review)
    first = coordinator.execute(_request(projects, project_id, "fast"))
    assert first.needs_review and first.director_spec is None
    assert calls == 2
    run = runtime.get_run(first.run_id)
    assert run.state["revision_count"] == 1
    assert first.director_draft.director_plan.composition_strategy == _patch(_spec())["value"]

    def rereview(messages: Any) -> str:
        spec = json.loads(messages[1]["content"])["director_spec"]
        assert spec["director_plan"]["composition_strategy"] == _patch(_spec())["value"]
        return _semantic()

    coordinator.critic_engine = DirectorCriticEngine(rereview)
    recovered = coordinator.execute(_request(
        projects, project_id, "fast", previous_run_id=first.run_id, rerun_from="director_critic",
    ))
    assert recovered.director_spec is not None
    assert recovered.director_spec.composition == _patch(_spec())["value"]
