"""Autonomous creative planning keeps user intent and completed-image context."""

from types import SimpleNamespace

from kantoku.capabilities.creative import (
    CreativeContext,
    compile_image_prompt,
    creative_brief,
    plan_creative_turn,
)


def test_followup_edits_last_image_without_requiring_a_template() -> None:
    previous = CreativeContext(
        subject="熊大", style="写实", composition="半身头像",
        background="树林", artifact_id="artifact-bear",
    )
    decision = plan_creative_turn(
        "把熊二的卡通照片跟熊大的一样给我就行", previous,
        trace_id="trace-creative-test",
        classify=lambda *_args, **_kwargs: SimpleNamespace(content=(
            '{"action":"image.edit","subject":"熊二","style":"写实",'
            '"composition":"半身头像","background":"树林","use_reference":true}'
        )),
    )
    assert decision.action == "image.edit"
    assert decision.use_reference
    assert decision.subject == "熊二"
    prompt = compile_image_prompt(decision, previous)
    assert "把熊二的卡通照片跟熊大的一样给我就行" in prompt
    assert "参考图此前主体：熊大" in prompt
    assert "本次主体：熊二" in prompt


def test_creative_planner_falls_back_to_image_edit_on_model_failure() -> None:
    previous = CreativeContext(subject="熊大", artifact_id="artifact-bear")

    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise TimeoutError("offline")

    decision = plan_creative_turn(
        "背景保持不变，换个人物", previous,
        trace_id="trace-creative-offline", classify=unavailable,
    )
    assert decision.action == "image.edit"
    assert decision.use_reference
    assert "背景保持不变，换个人物" in compile_image_prompt(decision, previous)


def test_explicit_edit_does_not_change_route_when_model_labels_it_generate() -> None:
    previous = CreativeContext(subject="小埋", artifact_id="artifact-previous")
    decision = plan_creative_turn(
        "换成海老名", previous, trace_id="trace-edit-stable",
        classify=lambda *_args, **_kwargs: SimpleNamespace(content=(
            '{"action":"image.generate","subject":"海老名","use_reference":false}'
        )),
    )
    assert decision.action == "image.edit"
    assert decision.use_reference
    assert decision.subject == "海老名"


def test_image_whereabouts_is_chat_not_another_paid_image() -> None:
    previous = CreativeContext(subject="熊大", artifact_id="artifact-bear")
    decision = plan_creative_turn("图片在哪里", previous, trace_id="trace-creative-where")
    assert decision.action == "chat"


def test_video_request_after_image_cannot_become_another_image() -> None:
    previous = CreativeContext(subject="上一张角色图", artifact_id="artifact-prior")
    decision = plan_creative_turn(
        "生成无一郎竹林战斗视频", previous, trace_id="trace-video-route",
        classify=lambda *_args, **_kwargs: SimpleNamespace(content=(
            '{"action":"image.generate","subject":"无一郎"}'
        )),
    )
    assert decision.action == "chat"


def test_untemplated_first_request_can_be_understood_as_image() -> None:
    decision = plan_creative_turn(
        "想看一座赛博朋克城市", None, trace_id="trace-creative-open",
        classify=lambda *_args, **_kwargs: SimpleNamespace(content=(
            '{"action":"image.generate","subject":"赛博朋克城市",'
            '"style":"赛博朋克","use_reference":false}'
        )),
    )
    assert decision.action == "image.generate"
    assert "想看一座赛博朋克城市" in compile_image_prompt(decision, None)


def test_correction_brief_explains_interpretation_instead_of_echoing_request() -> None:
    previous = CreativeContext(subject="另一只动物", artifact_id="artifact-prior")
    decision = plan_creative_turn(
        "我是说熊出没的吉吉国王", previous, trace_id="trace-brief-correction",
        classify=lambda *_args, **_kwargs: SimpleNamespace(content=(
            '{"action":"image.edit","subject":"其他人物",'
            '"style":"卡通动画","use_reference":true}'
        )),
    )
    brief = creative_brief(decision)
    assert decision.action == "image.edit"
    assert decision.subject == "熊出没的吉吉国王"
    assert "吉吉国王" in brief and "卡通动画" in brief
    assert "我是说熊出没的吉吉国王" not in brief
    assert "其他人物" not in brief
    assert "我是说熊出没的吉吉国王" in compile_image_prompt(decision, previous)
