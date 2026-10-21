"""Module 2 offline regression: full external Prompt, provenance, editing and upload."""

import base64
import json
import struct
import zlib
from http.client import HTTPConnection
from pathlib import Path

import pytest
from test_comic_prompts import _run, _setup
from test_home_quick_domain import _workspace_production_request
from test_home_quick_domain import app as app_fixture
from test_home_quick_domain import production as production_fixture
from test_web_studio import server as server_fixture

from kantoku.config import ToolError
from kantoku.core import budget
from kantoku.core.artifacts import ArtifactService
from kantoku.core.runtime.models import ArtifactLifecycle, ArtifactType, ExecutionStatus
from kantoku.domains.comic.models import ComicPromptDraft, ComicShotDraft
from kantoku.domains.comic.prompts import (
    CONTEXT_VERSION,
    PROMPT_SECTIONS,
    compiler_for_model,
    image_prompt,
)
from kantoku.shells import web_studio

app = app_fixture
production = production_fixture
server = server_fixture


def _sections():
    return {
        "director_summary": "保持当前导演执行方案",
        "character_context": {"identity": "东方仙侠少女阿青", "appearance": "黑发少女",
                              "age": "", "clothing": "青色披风", "fixed_features": "玉簪"},
        "world_context": {"world_setting": "东方仙侠世界", "location": "雨夜竹林",
                          "time": "雨夜", "environment": "山石与竹叶"},
        "style_context": {"art_style": "东方幻想电影", "photographic_style": "真实摄影质感",
                          "color": "冷色环境与暖色主体分离", "materials": "哑光布料与岩石"},
        "shot_context": {"action": "少女拔剑", "pose": "持剑",
                         "camera": "远景，低机位", "composition": "人物位于左侧三分之一",
                         "lighting_changes": "月光从右上方照射"},
        "negative_prompt": "禁止现代建筑、服装漂移、额外人物、文字水印",
    }


def test_external_prompt_uses_actual_attachment_without_treating_filename_as_a_retrieval_key():
    image = {"filename": "approved-reference.webp", "version": 2, "sha256": "a" * 64}
    draft = ComicPromptDraft(
        director_summary="延续当前画面", positive_prompt="保持身份，抬起左手",
        negative_prompt="文字水印",
        context_sources={"image_references": {"continuity": {
            "image": image, "changes": [{"field": "shot_context.pose", "quote": "抬起左手"}],
        }}},
    )
    text = image_prompt(draft)
    for required in [image["filename"], image["sha256"], "实际附带的参考图",
                     "不是图片地址或检索条件", "重命名附件或转为 PNG",
                     "没有附上这张参考图", "本轮允许的变化：抬起左手", "文字水印"]:
        assert required in text
    assert draft.context_sources["image_references"]["continuity"]["image"] == image
    draft.context_sources = {}
    assert image_prompt(draft) == "保持身份，抬起左手\n\nNegative Constraint / 禁止内容\n文字水印"


@pytest.mark.parametrize("emotion", ["充满爱意地直视镜头", "平静微笑", "若有所思地凝视远方"])
def test_complete_prompt_keeps_current_shot_expression_even_when_model_omits_it(tmp_path, emotion):
    runtime, projects, _assets, boards, prompts, original, _unused = _setup(tmp_path / "db")
    shot = boards.edit_shot(
        original.shot_id,
        ComicShotDraft.model_validate({
            **original.model_dump(include=set(ComicShotDraft.model_fields)),
            "action": "身体朝前，回头望向镜头，双手背在身后", "emotion": emotion,
        }),
        expected_project_version=projects.get(original.project_id).project.current_version,
        expected_version=original.version, status=original.status,
    )
    output = _sections()
    output["shot_context"].update(action="不属于当前镜头的模型动作", pose="回眸，双手背在身后")
    draft = _compile(prompts, shot.shot_id, lambda _: json.dumps(output, ensure_ascii=False))
    assert emotion in draft.shot_context
    assert shot.action in draft.shot_context
    assert "不属于当前镜头" not in draft.shot_context
    assert emotion not in draft.character_context
    assert draft.context_sources["input_lineage"]["execution_emotion"] == {
        "value": emotion, "field_path": "shot.emotion", "target_field": "shot_context.pose"}
    saved = _save(prompts, shot.shot_id, draft)
    assert saved.version == 1 and prompts.get(shot.shot_id).shot_context == draft.shot_context
    assert draft.shot_context in runtime.get_artifact(saved.artifact_id).metadata["final_prompt"]


@pytest.mark.parametrize("bound_character", [True, False])
def test_standing_and_battle_share_identical_bibles_only_shot_changes(
    tmp_path, bound_character,
):
    _runtime, projects, _assets, boards, prompts, shot, _u = _setup(tmp_path / "db")
    base = shot.model_dump(include=set(ComicShotDraft.model_fields))
    if not bound_character:
        base["character_asset_versions"] = []
    first_shot = boards.edit_shot(
        shot.shot_id, ComicShotDraft.model_validate(base | {"action": "站立远望"}),
        expected_project_version=projects.get(shot.project_id).project.current_version,
        expected_version=shot.version, status=shot.status,
    )
    first_output = _sections()
    first_output["shot_context"].update(action="站立远望", pose="站姿，双手自然下垂")
    first = _save(prompts, shot.shot_id, _compile(
        prompts, shot.shot_id, lambda _: json.dumps(first_output, ensure_ascii=False)))
    board = boards.get(shot.storyboard_id)
    second_shot = boards.add_shot(
        board.storyboard_id, ComicShotDraft.model_validate(base | {"action": "战斗姿态"}),
        expected_project_version=projects.get(shot.project_id).project.current_version,
        expected_storyboard_version=board.version,
    )
    changed = _sections()
    changed["character_context"].update(appearance="红发少女", clothing="银甲")
    changed["world_context"]["location"] = "现代城市"
    changed["style_context"]["art_style"] = "霓虹漫画"
    changed["negative_prompt"] = "模型试图替换禁止内容"
    changed["shot_context"].update(
        action="战斗姿态", pose="屈膝握剑", composition="人物位于右侧三分之一",
        camera="近景，平视", lighting_changes="月光从右上方照射，剑身投影变化",
    )
    second = _save(prompts, second_shot.shot_id, _compile(
        prompts, second_shot.shot_id, lambda _: json.dumps(changed, ensure_ascii=False)))
    for name in ("character_context", "world_context", "style_context", "negative_prompt"):
        assert getattr(second, name) == getattr(first, name)
    assert "站立远望" in first.shot_context and "战斗姿态" in second.shot_context
    assert first.shot_context != second.shot_context
    assert "站立" not in first.character_context and "战斗" not in second.character_context
    contexts = second.context_sources["structured_contexts"]
    assert set(contexts["character_context"]) == {
        "identity", "appearance", "age", "clothing", "fixed_features"}
    assert set(contexts["world_context"]) == {"location", "time", "world_setting", "environment"}
    assert set(contexts["style_context"]) == {
        "art_style", "photographic_style", "color", "materials"}
    assert set(contexts["shot_context"]) == {
        "action", "pose", "composition", "camera", "lighting_changes"}
    assert second.context_sources["field_provenance"]["character_context"][
        "source_artifact_id"] == first.artifact_id
    assert first_shot.subject == second_shot.subject


@pytest.mark.parametrize("section,field,value", [
    ("character_context", "pose", "站立远望"),
    ("world_context", "camera", "远景"),
    ("style_context", "lighting_changes", "主光方向右上方"),
    ("character_context", "age", "18岁\n姿态：站立远望"),
])
def test_compiler_rejects_cross_context_fields_without_saving_artifact(
    tmp_path, section, field, value,
):
    runtime, projects, _assets, _boards, prompts, shot, _u = _setup(tmp_path / "db")
    output = _sections()
    output[section][field] = value
    before = projects.get(shot.project_id).project.current_version
    with pytest.raises(ToolError, match="未返回有效结构") as caught:
        _compile(prompts, shot.shot_id, lambda _: json.dumps(output, ensure_ascii=False))
    assert "module=prompt.compiler" in caught.value.detail
    assert section in caught.value.detail
    assert prompts.versions(shot.shot_id) == []
    assert runtime.list_artifacts(type=ArtifactType.PROMPT) == []
    assert projects.get(shot.project_id).project.current_version == before


@pytest.mark.parametrize("section,field,value,dynamic", [
    ("character_context", "appearance", "黑发少女，战斗姿态", "战斗姿态"),
    ("character_context", "clothing", "青色披风，低机位", "低机位"),
    ("character_context", "fixed_features", "玉簪，月光从右上方照射", "月光从右上方照射"),
    ("world_context", "environment", "山石，站立远望的少女", "站立远望的少女"),
    ("style_context", "photographic_style", "真实摄影，平视，前景虚化", "前景虚化"),
    ("world_context", "environment", "云海，竖幅夜空主导", "竖幅夜空主导"),
    ("character_context", "appearance", "黑发少女，侧脸轮廓线利落", "侧脸轮廓线利落"),
])
def test_model_misplaced_shot_clauses_are_relocated_verbatim_with_source(
    tmp_path, section, field, value, dynamic,
):
    _runtime, _projects, _assets, _boards, prompts, shot, _u = _setup(tmp_path / "db")
    output = _sections()
    output[section][field] = value
    draft = _compile(prompts, shot.shot_id, lambda _: json.dumps(output, ensure_ascii=False))
    assert dynamic not in getattr(draft, section)
    assert dynamic in draft.shot_context
    moves = draft.context_sources["scope_relocations"]
    assert any(move["from"] == f"{section}.{field}" and move["to"].startswith("shot_context.")
               for move in moves)
    assert all(len(move["value_sha256"]) == 64 for move in moves)


def test_legacy_contaminated_bible_remains_readable_but_cannot_seed_new_shot(tmp_path):
    _runtime, projects, _assets, boards, prompts, shot, _u = _setup(tmp_path / "db")
    legacy = _compile(prompts, shot.shot_id)
    contaminated = "东方仙侠少女阿青，黑发少女，青色披风，站立远望，低机位"
    legacy = legacy.model_copy(update={
        "positive_prompt": legacy.positive_prompt.replace(legacy.character_context, contaminated),
        "character_context": contaminated,
        "context_sources": {"bible_contract": "identity-only-1"},
    })
    saved = _save(prompts, shot.shot_id, legacy)
    board = boards.get(shot.storyboard_id)
    next_shot = boards.add_shot(
        board.storyboard_id,
        ComicShotDraft.model_validate(shot.model_dump(include=set(ComicShotDraft.model_fields))
                                     | {"action": "战斗姿态"}),
        expected_project_version=projects.get(shot.project_id).project.current_version,
        expected_storyboard_version=board.version,
    )
    assert prompts.reused_context(next_shot, projects.get_director(shot.project_id)) == {}
    clean = _compile(prompts, next_shot.shot_id)
    assert "站立" not in clean.character_context and "低机位" not in clean.character_context
    assert prompts.get(shot.shot_id).character_context == saved.character_context
    current = _save(prompts, shot.shot_id, _compile(prompts, shot.shot_id))
    with pytest.raises(ToolError, match="字段不合法"):
        prompts.save(
            shot.shot_id, legacy,
            expected_project_version=projects.get(shot.project_id).project.current_version,
            expected_shot_version=shot.version, model_target="external",
            compiler_version=current.compiler_version, run_id=_run(prompts.runtime),
            source="restored", expected_version=current.version, restored_from_version=1,
        )
    assert prompts.get(shot.shot_id).artifact_id == current.artifact_id
    assert len(prompts.versions(shot.shot_id)) == 2


@pytest.mark.parametrize("bypass", ["forged_contract", "remove_sections"])
def test_manual_edit_cannot_disable_schema_or_put_pose_into_character(tmp_path, bypass):
    _runtime, projects, _assets, _boards, prompts, shot, _u = _setup(tmp_path / "db")
    current = _save(prompts, shot.shot_id, _compile(prompts, shot.shot_id))
    from kantoku.domains.comic.models import ComicPromptDraft

    draft = ComicPromptDraft.model_validate(current.model_dump(
        include=set(ComicPromptDraft.model_fields)))
    draft.context_sources = {"bible_contract": "legacy"}
    draft.positive_prompt = draft.positive_prompt.replace(
        "固定特征：玉簪", "固定特征：玉簪、站立远望")
    if bypass == "remove_sections":
        draft.context_version = None
    before = projects.get(shot.project_id).project.current_version
    with pytest.raises(ToolError, match="字段不合法|不能将结构化"):
        prompts.save(
            shot.shot_id, draft, expected_project_version=before,
            expected_shot_version=shot.version, model_target="external",
            compiler_version=current.compiler_version, run_id=_run(prompts.runtime),
            source="edited", expected_version=current.version,
        )
    assert prompts.get(shot.shot_id).artifact_id == current.artifact_id
    assert projects.get(shot.project_id).project.current_version == before


@pytest.mark.parametrize("style_camera", ["真实摄影质感", "远景，低机位"])
def test_scene_and_style_light_facts_are_scoped_to_shot(tmp_path, style_camera):
    from kantoku.domains.comic.models import ComicAssetDraft

    _runtime, projects, assets, boards, prompts, shot, _u = _setup(tmp_path / "db")
    refs = []
    for name, details in (
        ("雨夜月光", {"kind": "scene", "location": "雨夜竹林",
                    "lighting": "月光从右上方照射"}),
        ("东方幻想", {"kind": "style", "art_direction": "东方幻想电影",
                    "camera_language": style_camera, "lighting": "主光方向右上方"}),
    ):
        asset = assets.create(
            shot.project_id, ComicAssetDraft.model_validate({"name": name, "details": details}),
            expected_project_version=projects.get(shot.project_id).project.current_version,
        )
        refs.append({"asset_id": asset.asset_id, "version": asset.version})
    revised = boards.edit_shot(
        shot.shot_id, ComicShotDraft.model_validate(
            shot.model_dump(include=set(ComicShotDraft.model_fields))
            | {"scene_asset_versions": [refs[0]], "style_version": refs[1]}),
        expected_project_version=projects.get(shot.project_id).project.current_version,
        expected_version=shot.version, status=shot.status,
    )
    draft = _compile(prompts, revised.shot_id)
    assert "月光从右上方照射" in draft.shot_context
    assert "主光方向右上方" in draft.shot_context
    if style_camera == "远景，低机位":
        assert style_camera in draft.shot_context
        assert style_camera not in draft.style_context
    for name in ("character_context", "world_context", "style_context"):
        assert "主光方向" not in getattr(draft, name)
        assert "月光从" not in getattr(draft, name)


def test_edit_cannot_move_fixed_outfit_into_shot_to_bypass_asset_preservation(tmp_path):
    from kantoku.domains.comic.models import ComicPromptDraft

    runtime, projects, _assets, _boards, prompts, shot, _u = _setup(tmp_path / "db")
    current = _save(prompts, shot.shot_id, _compile(prompts, shot.shot_id))
    text = current.positive_prompt.replace("服装：青色披风\n", "")
    text = text.replace("动作：拔剑", "动作：拔剑，青色披风")
    assert "青色披风" in text  # 旧的全正文事实校验仍会通过，分段归属必须单独核验。
    draft = ComicPromptDraft.model_validate(
        current.model_dump(include=set(ComicPromptDraft.model_fields)) | {"positive_prompt": text})
    with pytest.raises(ToolError, match="字段不合法"):
        prompts.save(
            shot.shot_id, draft,
            expected_project_version=projects.get(shot.project_id).project.current_version,
            expected_shot_version=shot.version, expected_version=current.version,
            model_target=current.model_target, compiler_version=current.compiler_version,
            run_id=_run(runtime), source="edited",
        )
    assert prompts.get(shot.shot_id).artifact_id == current.artifact_id


def test_late_manual_edit_to_older_shot_wins_over_newer_shot_creation(tmp_path):
    from kantoku.domains.comic.models import ComicPromptDraft

    runtime, projects, _a, boards, prompts, shot, _u = _setup(tmp_path / "db")
    first = _save(prompts, shot.shot_id, _compile(prompts, shot.shot_id))
    board = boards.get(shot.storyboard_id)
    second_shot = boards.add_shot(
        board.storyboard_id,
        ComicShotDraft.model_validate(shot.model_dump(include=set(ComicShotDraft.model_fields))
                                     | {"action": "战斗姿态"}),
        expected_project_version=projects.get(shot.project_id).project.current_version,
        expected_storyboard_version=board.version,
    )
    _save(prompts, second_shot.shot_id, _compile(prompts, second_shot.shot_id))
    edited = prompts.save(
        shot.shot_id, ComicPromptDraft.model_validate(
            first.model_dump(include=set(ComicPromptDraft.model_fields)) | {
                "positive_prompt": first.positive_prompt.replace(
                    "固定特征：玉簪", "固定特征：玉簪、红绳")}),
        expected_project_version=projects.get(shot.project_id).project.current_version,
        expected_shot_version=shot.version, expected_version=first.version,
        model_target=first.model_target, compiler_version=first.compiler_version,
        run_id=_run(runtime), source="edited",
    )
    draft = _compile(prompts, second_shot.shot_id)
    assert draft.character_context == edited.character_context
    assert "红绳" in draft.character_context
    assert draft.context_sources["field_provenance"]["character_context"][
        "source_type"] == "manual_edit"
    assert draft.context_sources["structured_contexts"]["character_context"][
        "fixed_features"] == "玉簪、红绳"


def test_external_image_http_import_accepts_normal_file_size_and_lists_prompt_assets(
    app,
    production,
    server,
    monkeypatch,
    tmp_path,
):
    request, shot, prompt = _external(app, monkeypatch)
    monkeypatch.setattr(
        web_studio.get_settings().image, "output_dir", tmp_path / "images", raising=False
    )
    small = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aJ1sAAAAASUVORK5CYII="
    )
    payload = b"QA\0" + b"a" * 60000
    chunk = (
        struct.pack(">I", len(payload))
        + b"tEXt"
        + payload
        + struct.pack(">I", zlib.crc32(b"tEXt" + payload))
    )
    content = small[:-12] + chunk + small[-12:]
    body = json.dumps(
        {
            "expected_project_version": app.comic_projects.get(
                request["production_project_id"]
            ).project.current_version,
            "expected_shot_version": shot["version"],
            "expected_prompt_version": prompt["version"],
            "conversation_id": request["conversation_id"],
            "filename": "qa-user-image.png",
            "data_url": "data:image/png;base64," + base64.b64encode(content).decode(),
        }
    )
    assert len(body) > 65536
    connection = HTTPConnection("127.0.0.1", server, timeout=10)
    try:
        connection.request(
            "POST",
            f"/api/comic/shots/{shot['shot_id']}/external-image",
            body=body,
            headers={"X-Studio-Token": app.token, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        result = json.loads(response.read())
        assert response.status == 201, result
        artifact = app.runtime_store.get_artifact(result["artifact_id"])
        assert Path(artifact.location).read_bytes() == content
        connection.request(
            "GET", f"/api/comic/projects/{request['production_project_id']}/prompts",
            headers={"X-Studio-Token": app.token},
        )
        response = connection.getresponse()
        assets = json.loads(response.read())["prompts"]
        assert response.status == 200 and assets[0]["final_prompt"] == prompt["final_prompt"]
        assert assets[0]["external_image"]["artifact_id"] == artifact.id
        assert assets[0]["shot_sequence_number"] == 1
    finally:
        connection.close()


def _compile(prompts, shot_id, call=None):
    snapshot, director, board, shot, assets = prompts.source(shot_id)
    return compiler_for_model("external").compile(
        snapshot=snapshot,
        director=director,
        storyboard=board,
        shot=shot,
        assets=assets,
        model_target="external",
        complete_prompt=True,
        reused_context=prompts.reused_context(shot, director),
        model_call=call or (lambda _: json.dumps(_sections(), ensure_ascii=False)),
    )


def _save(prompts, shot_id, draft):
    snapshot, _director, _board, shot, _assets = prompts.source(shot_id)
    return prompts.save(
        shot_id,
        draft,
        expected_project_version=snapshot.project.current_version,
        expected_shot_version=shot.version,
        model_target="external",
        compiler_version="comic-prompt-1",
        run_id=_run(prompts.runtime),
    )


def test_complete_prompt_keeps_pinned_facts_and_excludes_other_character(tmp_path):
    runtime, _projects, _assets, _boards, prompts, shot, unrelated = _setup(tmp_path / "db")
    requests = []

    def call(messages):
        requests.append(messages)
        assert "JSON" in messages[0]["content"]
        return json.dumps(_sections(), ensure_ascii=False)

    draft = _compile(prompts, shot.shot_id, call)
    record = _save(prompts, shot.shot_id, draft)
    full = prompts.payload(record)["final_prompt"]
    for heading in [*PROMPT_SECTIONS.values(), "Negative Constraint / 禁止内容"]:
        assert full.count(heading) == 1
    for fact in ("阿青", "黑发少女", "青色披风", "雨夜竹林", "电影", "右上方", "文字水印"):
        assert fact in full
    assert unrelated.asset_id not in requests[0][1]["content"] and "银发少年" not in full
    sources = record.context_sources
    assert sources["source_type"] == "model_choice" and sources["model_request_id"]
    assert sources["user_facts"]["sha256"] and len(sources["asset_facts"]) == 2
    assert all(item["sha256"] and item["version"] == 1 for item in sources["asset_facts"])
    assert runtime.get_artifact(record.artifact_id).metadata["final_prompt"] == full
    assert not runtime.list_artifacts(type=ArtifactType.IMAGE)


def test_external_prompt_preserves_legacy_director_and_exposes_input_basis(tmp_path):
    runtime, projects, _a, _b, prompts, shot, _u = _setup(tmp_path / "db")
    requests = []
    draft = _compile(prompts, shot.shot_id,
                     lambda messages: requests.append(messages) or json.dumps(_sections()))
    context = json.loads(requests[0][1]["content"])
    director = projects.get_director(shot.project_id)
    # The legacy Director has useful direction even without the three v2 stage objects.
    assert "director_spec" not in context["stable_context"]
    assert "storyboard" not in context["stable_context"]
    assert context["shot_direction"]["director_spec"]["composition"] == director.composition
    assert context["shot_direction"]["director_spec"]["lighting"] == director.lighting
    record = _save(prompts, shot.shot_id, draft)
    lineage = record.context_sources["input_lineage"]
    assert lineage["brief"]["id"] == projects.get(shot.project_id).creative_brief.brief_id
    assert lineage["director"]["id"] == director.spec_id
    assert lineage["shot"]["id"] == shot.shot_id
    assert lineage["skill_methods"] == []
    assert lineage["skill_evidence_status"] == "not_recorded"
    for key in ("brief", "director", "shot"):
        assert len(lineage[key]["sha256"]) == 64 and lineage[key]["version"] == 1
    fields = record.context_sources["field_provenance"]
    assert [a["kind"] for a in fields["character_context"]["input_basis"]["assets"]] == [
        "character"]
    assert [a["kind"] for a in fields["world_context"]["input_basis"]["assets"]] == ["scene"]
    assert fields["shot_context"]["input_basis"]["shot"]["id"] == shot.shot_id
    assert fields["character_context"]["source_type"] == "model_choice"
    assert fields["character_context"]["trust_status"] == "creative_choice"
    full = prompts.payload(record)["final_prompt"]
    assert "外貌：黑发少女" in full and "服装：青色披风" in full
    assert "appearance:" not in full and "outfit:" not in full
    assert (runtime.get_artifact(record.artifact_id).metadata["context_sources"]
            == record.context_sources)


def test_skill_method_basis_is_carried_without_claiming_causality(app, production, monkeypatch):
    _request, _shot, prompt = _external(app, monkeypatch)
    lineage = prompt["context_sources"]["input_lineage"]
    # The existing Coordinator is used; these are fixture knowledge inputs, not a paid model test.
    assert lineage["skill_evidence_status"] == "recorded"
    assert {item["reference"] for item in lineage["skill_methods"]} == {
        "comic.creative_understanding", "comic.visual_direction", "comic.cinematography",
    }
    assert all(len(item["sha256"]) == 64 for item in lineage["skill_methods"])
    assert lineage["evidence_semantics"] == "input_basis_not_causal_attribution"
    for field in prompt["context_sources"]["field_provenance"].values():
        assert field["source_type"] == "model_choice" and field["trust_status"] == "creative_choice"
        assert field["input_basis"]["skill_methods"] == lineage["skill_methods"]


@pytest.mark.parametrize("mismatch", ["project", "shot", "version", "hash", "extra", "failed"])
def test_confirmation_projection_rejects_wrong_binding(tmp_path, mismatch):
    from kantoku.domains.comic.prompts import prompt_fingerprint

    runtime, _p, _a, _b, prompts, shot, _u = _setup(tmp_path / "db")
    prompt = _save(prompts, shot.shot_id, _compile(prompts, shot.shot_id))
    state = {"project_id": prompt.project_id, "shot_id": prompt.shot_id,
             "prompt_confirmation": {"artifact_id": prompt.artifact_id, "version": prompt.version,
                                     "sha256": prompt_fingerprint(prompt)}}
    if mismatch in {"project", "shot"}:
        state[mismatch + "_id"] = "other"
    elif mismatch in {"version", "hash", "extra"}:
        key, value = {"version": ("version", 2), "hash": ("sha256", "0" * 64),
                      "extra": ("unexpected", True)}[mismatch]
        state["prompt_confirmation"][key] = value
    run = runtime.create_run("comic", "comic.prompt.confirm", state, "prompt.confirm")
    runtime.update_run(run.id, state=state, current_node="prompt.confirm",
                       status=ExecutionStatus.FAILED if mismatch == "failed"
                       else ExecutionStatus.COMPLETED)
    assert not prompts.confirmed(prompt)


def test_confirmation_survives_more_than_100000_unrelated_runs(tmp_path, monkeypatch):
    from kantoku.domains.comic.prompts import prompt_fingerprint

    runtime, _p, _a, boards, prompts, shot, _u = _setup(tmp_path / "db")
    prompt = _save(prompts, shot.shot_id, _compile(prompts, shot.shot_id))
    state = {"project_id": prompt.project_id, "shot_id": prompt.shot_id,
             "prompt_confirmation": {"artifact_id": prompt.artifact_id, "version": prompt.version,
                                     "sha256": prompt_fingerprint(prompt)}}
    run = runtime.create_run("comic", "comic.prompt.confirm", state, "prompt.confirm")
    runtime.update_run(run.id, state=state, current_node="prompt.confirm",
                       status=ExecutionStatus.COMPLETED)
    # Only this isolated test database is populated. No production state is modified.
    with runtime._connect() as connection:
        connection.execute(
            "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<100001) "
            "INSERT INTO runs(id,domain,workflow,status,state_json,current_node,"
            "started_at,updated_at) "
            "SELECT 'unrelated-'||x,'comic','comic.prompt.compile','completed','{}',"
            "'prompt.compile',"
            "'2099-01-01T00:00:00+00:00','2099-01-01T00:00:00+00:00' FROM n")

    def forbidden(*_a, **_k):
        pytest.fail("Prompt payload must not hydrate the complete run/image/shot history")

    monkeypatch.setattr(runtime, "list_runs", forbidden)
    monkeypatch.setattr(runtime, "list_artifacts", forbidden)
    monkeypatch.setattr(boards, "shot_versions", forbidden)
    assert prompts.payload(prompt)["user_confirmed"] is True


def test_prompt_projection_only_attaches_its_ready_external_image(tmp_path, monkeypatch):
    runtime, _p, _a, boards, prompts, shot, _u = _setup(tmp_path / "db")
    prompt = _save(prompts, shot.shot_id, _compile(prompts, shot.shot_id))
    metadata = {"project_id": prompt.project_id, "shot_id": prompt.shot_id,
                "prompt_artifact_id": prompt.artifact_id, "sha256": "1" * 64}
    correct = runtime.create_artifact(
        type=ArtifactType.IMAGE, run_id=_run(runtime), node_id="external_image.import",
        source="comic.external_image", metadata=metadata)
    for wrong in ({"project_id": "other"}, {"shot_id": "other"},
                  {"prompt_artifact_id": "previous-prompt"}):
        runtime.create_artifact(type=ArtifactType.IMAGE, run_id=_run(runtime),
                                node_id="external_image.import", source="comic.external_image",
                                metadata=metadata | wrong)
    runtime.create_artifact(type=ArtifactType.IMAGE, run_id=_run(runtime),
                            node_id="external_image.import", source="comic.external_image",
                            status="failed", metadata=metadata)
    runtime.create_artifact(type=ArtifactType.IMAGE, run_id=_run(runtime),
                            node_id="image.generate", source="image.provider", metadata=metadata)

    def forbidden(*_a, **_k):
        pytest.fail("Projection must not load the entire image/shot history")

    monkeypatch.setattr(runtime, "list_artifacts", forbidden)
    monkeypatch.setattr(boards, "shot_versions", forbidden)
    assert prompts.payload(prompt)["external_image"]["artifact_id"] == correct.id
    assert prompts._external_image(prompt, content_sha256="1" * 64).id == correct.id
    assert prompts._external_image(prompt, content_sha256="2" * 64) is None
    runtime.change_artifact_lifecycle(correct.id, ArtifactLifecycle.TRASHED, 1)
    assert prompts.payload(prompt)["external_image"] is None
    assert prompts._external_image(prompt, content_sha256="1" * 64) is None
    runtime.purge_artifact(correct.id, 2)
    assert prompts._external_image(prompt, content_sha256="1" * 64) is None


def test_same_character_bibles_reuse_exact_prompt_revision_not_new_random_choice(tmp_path):
    _runtime, projects, _assets, boards, prompts, shot, _unrelated = _setup(tmp_path / "db")
    first = _save(prompts, shot.shot_id, _compile(prompts, shot.shot_id))
    board = boards.get(shot.storyboard_id)
    next_shot = boards.add_shot(
        board.storyboard_id,
        ComicShotDraft.model_validate(
            shot.model_dump(
                include={
                    "purpose",
                    "subject",
                    "environment",
                    "character_asset_versions",
                    "scene_asset_versions",
                }
            )
        ),
        expected_project_version=projects.get(shot.project_id).project.current_version,
        expected_storyboard_version=board.version,
    )
    next_shot = boards.edit_shot(
        next_shot.shot_id,
        ComicShotDraft.model_validate(
            next_shot.model_dump(
                include={
                    "purpose",
                    "environment",
                    "character_asset_versions",
                    "scene_asset_versions",
                }
            )
            | {"subject": "阿青右肩与青色披风的轮廓"}
        ),
        expected_project_version=projects.get(shot.project_id).project.current_version,
        expected_version=next_shot.version,
        status=next_shot.status,
    )
    changed = _sections() | {
        "character_context": "模型试图换成红发",
        "style_context": "模型随机选新风格",
    }
    draft = _compile(prompts, next_shot.shot_id, lambda _: json.dumps(changed, ensure_ascii=False))
    assert draft.character_context == first.character_context
    assert draft.style_context == first.style_context
    assert (
        draft.context_sources["reused_context"]["character_context"]["artifact_id"]
        == first.artifact_id
    )
    # Changed identity cannot inherit the previous character Bible even if the old Prompt exists.
    edited = boards.edit_shot(
        next_shot.shot_id,
        ComicShotDraft(purpose="另一角色", subject="阿白", environment="雪原"),
        expected_project_version=projects.get(shot.project_id).project.current_version,
        expected_version=next_shot.version,
        status=next_shot.status,
    )
    assert "character_context" not in prompts.reused_context(
        edited, projects.get_director(shot.project_id)
    )


def test_reused_manually_edited_bible_keeps_manual_origin(tmp_path):
    runtime, projects, _a, _b, prompts, shot, _u = _setup(tmp_path / "db")
    first = _save(prompts, shot.shot_id, _compile(prompts, shot.shot_id))
    revised_character = first.character_context.replace("固定特征：玉簪", "固定特征：玉簪、红绳")
    draft = first.model_copy(update={
        "positive_prompt": first.positive_prompt.replace(
            first.character_context, revised_character),
    })
    from kantoku.domains.comic.models import ComicPromptDraft

    edited = prompts.save(
        shot.shot_id, ComicPromptDraft.model_validate(draft.model_dump(
            include=set(ComicPromptDraft.model_fields))),
        expected_project_version=projects.get(shot.project_id).project.current_version,
        expected_shot_version=shot.version, expected_version=first.version,
        model_target=first.model_target, compiler_version=first.compiler_version,
        run_id=_run(runtime), source="edited")
    captured = []
    reused = _compile(prompts, shot.shot_id,
                      lambda messages: captured.append(messages) or json.dumps(_sections()))
    provenance = reused.context_sources["field_provenance"]["character_context"]
    assert reused.character_context == edited.character_context
    assert "玉簪" in reused.character_context
    assert provenance["source_type"] == "manual_edit"
    assert provenance["run_id"] == edited.context_sources["run_id"]
    assert provenance["source_artifact_id"] == edited.artifact_id
    assert provenance["model_request_id"] is None
    assert provenance["input_basis"]["source_prompt_artifact_id"] == edited.artifact_id
    context = json.loads(captured[0][1]["content"])
    assert set(context["reused_bibles"]) == {
        "character_context", "world_context", "style_context"}


@pytest.mark.parametrize(
    "missing", ["character_context", "world_context", "negative_prompt", "director_summary"]
)
def test_incomplete_model_output_never_creates_complete_artifact(tmp_path, missing):
    runtime, _p, _a, _b, prompts, shot, _u = _setup(tmp_path / "db")
    result = _sections()
    result.pop(missing)
    with pytest.raises(ToolError, match="缺少"):
        _compile(prompts, shot.shot_id, lambda _: json.dumps(result, ensure_ascii=False))
    assert not runtime.list_artifacts(type=ArtifactType.PROMPT)


def test_complete_compiler_declares_verbatim_constraints_and_reports_unique_missing_items(tmp_path):
    runtime, _p, _a, _b, prompts, shot, _u = _setup(tmp_path / "db")
    snapshot, director, board, current_shot, assets = prompts.source(shot.shot_id)
    constraint = "给我制作一个亚裔辣妹风格的照片"
    snapshot.creative_brief.hard_constraints = [constraint]
    director.constraints = [constraint]
    seen = []

    def call(messages):
        seen.append(messages)
        return json.dumps(_sections(), ensure_ascii=False)

    compiler = compiler_for_model("external")
    inputs = dict(snapshot=snapshot, director=director, storyboard=board, shot=current_shot,
                  assets=assets, model_target="external", complete_prompt=True)
    with pytest.raises(ToolError, match="遗漏不可变约束") as caught:
        compiler.compile(**inputs, model_call=call)
    context = json.loads(seen[0][1]["content"])
    assert context["required_constraints"] == [constraint, "青色披风"]
    assert "四段 Context 的相应字段正文" in seen[0][0]["content"]
    assert "missing_count=1" in caught.value.detail and "missing_indices=0" in caught.value.detail
    assert constraint not in caught.value.detail
    output = _sections()
    output["style_context"]["art_style"] = constraint
    draft = compiler.compile(**inputs, model_call=lambda _: json.dumps(output, ensure_ascii=False))
    assert constraint in draft.positive_prompt
    assert not runtime.list_artifacts(type=ArtifactType.PROMPT)


@pytest.mark.parametrize("failure", ["repaired", "still_missing", "provider_unknown"])
def test_external_prompt_repairs_missing_constraint_once_but_never_resubmits_unknown_call(
    app, production, monkeypatch, failure,
):
    constraint = "给我制作一个亚裔辣妹风格的照片"
    cid = app.create_conversation({"interaction_mode": "guided", "domain": "comic"})["id"]
    snapshot = app.create_comic_project({"title": "Prompt 约束回归", "brief": {
        "original_request": constraint, "hard_constraints": [constraint]}})
    pid = snapshot["project"]["project_id"]
    result = app.create_comic_director(pid, {
        "expected_project_version": 1, "conversation_id": cid,
        "creation_mode": "professional", "task": constraint})
    app.confirm_comic_director(pid, {
        "version": result["director_spec"]["version"],
        "expected_project_version": app.comic_projects.get(pid).project.current_version})
    board = app.create_comic_storyboard(pid, {
        "expected_project_version": app.comic_projects.get(pid).project.current_version,
        "generate": True})
    shot = board["shots"][0]
    seen = []

    def call(messages):
        seen.append(messages)
        if failure == "provider_unknown":
            raise ToolError("供应商结果未知", detail="module=provider result=unknown")
        output = _sections()
        if len(seen) == 2 and failure == "repaired":
            output["style_context"]["art_style"] = constraint
        return json.dumps(output, ensure_ascii=False)

    monkeypatch.setattr(app, "_comic_storyboard_model", call)
    request = {"expected_project_version": app.comic_projects.get(pid).project.current_version,
               "expected_shot_version": shot["version"], "image_mode": "external",
               "complete_prompt": True, "conversation_id": cid}
    if failure == "repaired":
        prompt = app.compile_comic_prompt(shot["shot_id"], request)
        assert constraint in prompt["positive_prompt"]
        assert len(app.comic_prompts.versions(shot["shot_id"])) == 1
        assert app.comic_prompts.get(shot["shot_id"]).artifact_id == prompt["artifact_id"]
        assert app.comic_prompts.payload(app.comic_prompts.get(shot["shot_id"]))["final_prompt"] \
            == prompt["final_prompt"]
    else:
        with pytest.raises(ToolError, match="固定约束仍不完整|供应商结果未知"):
            app.compile_comic_prompt(shot["shot_id"], request)
        assert not app.comic_prompts.versions(shot["shot_id"])
        assert not app.runtime_store.list_artifacts(type=ArtifactType.PROMPT)
    assert len(seen) == (1 if failure == "provider_unknown" else 2)
    if len(seen) == 2:
        assert seen[0][1]["content"] == seen[1][1]["content"]
        assert "missing_indices=0" in seen[1][0]["content"]
    assert app.comic_projects.get(pid).creative_brief.hard_constraints == [constraint]
    assert not app.runtime_store.list_artifacts(type=ArtifactType.IMAGE)


def _external(app, monkeypatch, *, compile_prompt=True):
    request = _workspace_production_request(app)
    project_id = request["production_project_id"]
    board = app.create_comic_storyboard(
        project_id,
        {
            "expected_project_version": request["expected_project_version"],
            "generate": True,
        },
    )
    shot = board["shots"][0]
    monkeypatch.setattr(
        app, "_comic_storyboard_model", lambda _: json.dumps(_sections(), ensure_ascii=False)
    )
    if not compile_prompt:
        return request, shot, None
    prompt = app.compile_comic_prompt(
        shot["shot_id"],
        {
            "expected_project_version": app.comic_projects.get(project_id).project.current_version,
            "expected_shot_version": shot["version"],
            "image_mode": "external",
            "complete_prompt": True,
            "conversation_id": request["conversation_id"],
        },
    )
    return request, shot, prompt


@pytest.mark.parametrize("existing_prompt", [False, True])
def test_http_prompt_connection_failure_preserves_sources_and_can_recover(
    app, production, server, monkeypatch, existing_prompt,
):
    """Exercise HTTP -> compiler -> shared LLM with a socket fault, without network calls."""
    from unittest.mock import MagicMock

    import httpx2
    from httpcore2 import ConnectError
    from openai import OpenAI
    from test_llm import _settings as llm_settings

    from kantoku.core import llm

    request, shot, previous = _external(app, monkeypatch, compile_prompt=existing_prompt)
    pid = request["production_project_id"]
    before = app.comic_projects.get(pid)
    before_versions = app.comic_prompts.versions(shot["shot_id"])
    before_artifacts = app.runtime_store.list_artifacts(type=ArtifactType.PROMPT)
    before_calls = list(production)
    monkeypatch.setattr(llm, "get_settings", llm_settings)
    monkeypatch.setenv("TEST_CHAT_KEY", "offline-only-key")
    transport_calls = []

    def fail_transport(http_request):
        transport_calls.append(http_request)
        try:
            try:
                raise OSError(10048, "sensitive proxy address must stay private")
            except OSError as error:
                raise ConnectError(error) from error
        except ConnectError as error:
            error.__cause__ = None
            raise httpx2.ConnectError("sensitive transport detail", request=http_request) from error

    client = OpenAI(
        base_url="https://offline.invalid/v1", api_key="offline-only-key", max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(fail_transport), trust_env=False),
    )
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(llm, "_build_client", factory)
    monkeypatch.setattr(web_studio, "chat", llm.chat)
    monkeypatch.setattr(app, "_comic_storyboard_model",
                        web_studio.StudioApplication._comic_storyboard_model)
    # No other text/image provider may be used as a hidden recovery path.
    monkeypatch.setattr(app, "_comic_constraint_model", lambda *_: pytest.fail(
        "A transport failure must not enter semantic repair"))
    payload = {
        "expected_project_version": before.project.current_version,
        "expected_shot_version": shot["version"], "image_mode": "external",
        "complete_prompt": True, "conversation_id": request["conversation_id"],
    }
    connection = HTTPConnection("127.0.0.1", server, timeout=10)
    try:
        headers = {"X-Studio-Token": app.token, "Content-Type": "application/json"}
        endpoint = f"/api/comic/shots/{shot['shot_id']}/prompt/compile"
        connection.request("POST", endpoint, json.dumps(payload), headers)
        response = connection.getresponse()
        failure = json.loads(response.read())
        assert response.status >= 400, failure
        assert failure["retryable"] is True
        assert failure["error_id"].startswith("ERR-") and failure["trace_id"]
        assert "端口冲突" in failure["safe_message"]
        assert "sensitive proxy" not in json.dumps(failure)
        factory.assert_called_once()
        assert len(transport_calls) == 1 and client.is_closed()
        assert web_studio._comic_single_attempt.get() is False
        assert app.comic_projects.get(pid) == before
        assert app.comic_prompts.versions(shot["shot_id"]) == before_versions
        assert app.runtime_store.list_artifacts(type=ArtifactType.PROMPT) == before_artifacts
        assert production == before_calls
        assert not app.runtime_store.list_artifacts(type=ArtifactType.IMAGE)
        failed_run = next(run for run in app.runtime_store.list_runs(project_id=pid)
                          if run.workflow == "comic.prompt.compile"
                          and run.status is ExecutionStatus.FAILED)
        assert failed_run.state.get("prompt_artifact_id") is None
        assert failed_run.state.get("media_preparation") is None
        connection.request("GET", f"/api/comic/shots/{shot['shot_id']}/prompt/versions",
                           headers=headers)
        history_response = connection.getresponse()
        history = json.loads(history_response.read())
        assert history_response.status == 200, history
        assert len(history["versions"]) == len(before_versions)
        if previous:
            assert history["versions"][0]["final_prompt"] == previous["final_prompt"]
        # Explicit retry after the network has recovered: the same sources produce one new version.
        monkeypatch.setattr(app, "_comic_storyboard_model", lambda _: json.dumps(_sections()))
        connection.request("POST", endpoint, json.dumps(payload), headers)
        response = connection.getresponse()
        recovered = json.loads(response.read())
        assert response.status == 201, recovered
        assert recovered["context_version"] == CONTEXT_VERSION and recovered["final_prompt"]
        assert recovered["media_preparation"]["prompt_status"] == "ready"
        assert len(app.comic_prompts.versions(shot["shot_id"])) == len(before_versions) + 1
        assert factory.call_count == 1
    finally:
        connection.close()
        client.close()


def test_manual_prompt_edit_keeps_sources_and_invalidates_confirmation(
    app, production, monkeypatch
):
    request, shot, prompt = _external(app, monkeypatch)
    project_id = request["production_project_id"]

    def current_version():
        return app.comic_projects.get(project_id).project.current_version

    confirmed = app.confirm_comic_prompt(
        shot["shot_id"],
        {
            "expected_project_version": current_version(),
            "expected_version": prompt["version"],
            "conversation_id": request["conversation_id"],
        },
    )
    assert confirmed["user_confirmed"]
    edited = app.edit_comic_prompt(
        shot["shot_id"],
        {
            "expected_project_version": current_version(),
            "expected_version": prompt["version"],
            "draft": {
                "director_summary": prompt["director_summary"],
                "positive_prompt": prompt["positive_prompt"].replace(
                    "左侧三分之一", "右侧三分之一"
                ),
                "negative_prompt": prompt["negative_prompt"],
                "context_sources": {"source_type": "user_fact", "trust_status": "confirmed_fact"},
            },
        },
    )
    assert edited["context_version"] == CONTEXT_VERSION and not edited["user_confirmed"]
    assert "右侧三分之一" in edited["shot_context"]
    assert edited["context_sources"]["source_type"] == "manual_edit"
    fields = edited["context_sources"]["field_provenance"]
    assert fields["shot_context"]["source_type"] == "manual_edit"
    assert (
        fields["character_context"]
        == prompt["context_sources"]["field_provenance"]["character_context"]
    )
    assert edited["context_sources"]["parent_artifact_id"] == prompt["artifact_id"]
    assert (
        edited["context_sources"]["director_sources"]
        == prompt["context_sources"]["director_sources"]
    )
    assert fields["shot_context"]["model_request_id"] is None
    assert (fields["shot_context"]["input_basis"]
            == prompt["context_sources"]["field_provenance"]["shot_context"]["input_basis"])
    assert app.comic_prompts.list(project_id)[0].version == 2
    assert len(app.comic_prompts.versions(shot["shot_id"])) == 2
    with pytest.raises(ToolError, match="版本已变化"):
        app.confirm_comic_prompt(
            shot["shot_id"],
            {
                "expected_project_version": current_version(),
                "expected_version": prompt["version"],
                "conversation_id": request["conversation_id"],
            },
        )
    assert not [r for r in app.runtime_store.list_runs() if r.workflow == "comic.production.v1"]
    assert not [a for a in app.runtime_store.list_approvals() if a.node_id == "cost_approval"]


def test_prompt_uses_current_shot_camera_and_keeps_director_light(app, production, monkeypatch):
    request, raw_shot, _prompt = _external(app, monkeypatch)
    original = app.comic_storyboards.get_shot(raw_shot["shot_id"])
    shot = app.comic_storyboards.edit_shot(
        original.shot_id,
        ComicShotDraft(
            purpose=original.purpose,
            subject=original.subject,
            environment=original.environment,
            shot_size="中景",
            camera_angle="侧面平视",
        ),
        expected_project_version=app.comic_projects.get(
            original.project_id
        ).project.current_version,
        expected_version=original.version,
        status=original.status,
    )
    draft = _compile(
        app.comic_prompts,
        shot.shot_id,
        lambda _: json.dumps(
            _sections() | {"shot_context": _sections()["shot_context"] | {
                "camera": "中景、侧面平视", "lighting_changes": "沿用导演的主光方向"}},
            ensure_ascii=False
        ),
    )
    assert "景别：中景" in draft.shot_context and "机位：侧面平视" in draft.shot_context
    director = app.comic_projects.get_director(request["production_project_id"])
    assert f"主光方向：{director.cinematography.light_direction}" in draft.shot_context
    assert "shot_size=" not in draft.shot_context and "light_direction=" not in draft.shot_context
    selected = draft.context_sources["input_lineage"]["execution_camera"]
    assert selected["shot_size"]["field_path"] == "shot.shot_size"
    assert selected["light_direction"]["field_path"] == "director.cinematography.light_direction"


def test_external_upload_registers_core_artifact_without_provider_budget_or_workflow(
    app,
    production,
    monkeypatch,
    tmp_path,
):
    request, shot, prompt = _external(app, monkeypatch)
    project_id = request["production_project_id"]
    monkeypatch.setattr(
        web_studio.get_settings().image, "output_dir", tmp_path / "images", raising=False
    )

    def forbidden(*_a, **_k):
        pytest.fail("External Prompt pipeline must not invoke image production or billing")

    for name in ("submit", "query", "check_access", "validate_request"):
        monkeypatch.setattr(app.image_service.provider, name, forbidden)
    for target in (
        "kantoku.core.budget.reserve",
        "kantoku.core.budget.settle",
        "kantoku.tools.image_gen.gen_image",
        "kantoku.domains.comic.services.create_task",
    ):
        monkeypatch.setattr(target, forbidden)
    ledger = budget.list_ledger(project=project_id)
    content = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aJ1sAAAAASUVORK5CYII="
    )
    body = {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "expected_shot_version": shot["version"],
        "expected_prompt_version": prompt["version"],
        "conversation_id": request["conversation_id"],
        "filename": "external-user-image.png",
        "data_url": "data:image/png;base64," + base64.b64encode(content).decode(),
    }
    result = app.import_comic_external_image(shot["shot_id"], body)
    duplicate = app.import_comic_external_image(shot["shot_id"], body)
    assert duplicate["artifact_id"] == result["artifact_id"]
    assert result["created"] is True and duplicate["created"] is False
    artifact = app.runtime_store.get_artifact(result["artifact_id"])
    assert Path(artifact.location).read_bytes() == content
    assert artifact.metadata["creation_mode"] == "external_upload"
    assert artifact.conversation_id == request["conversation_id"]
    assert artifact.metadata["prompt_artifact_id"] == prompt["artifact_id"]
    source = app.comic_stories._source_contexts([
        app.comic_media.get(artifact.id).model_dump(mode="json")])[0]
    assert source["source_kind"] == "prompt_output"
    assert source["instruction_artifact_id"] == prompt["artifact_id"]
    assert source["source_versions"]["prompt"] == prompt["version"]
    assert source["negative_prompt"] == prompt["negative_prompt"]
    assert (
        app.comic_prompts.payload(app.comic_prompts.get(shot["shot_id"]))["external_image"][
            "status"
        ]
        == "image_uploaded"
    )
    assert app.comic_storyboards.get_shot(shot["shot_id"]).version == shot["version"]
    assert budget.list_ledger(project=project_id) == ledger
    assert len(app.runtime_store.list_artifacts(type=ArtifactType.IMAGE)) == 1
    assert not [r for r in app.runtime_store.list_runs() if r.workflow == "comic.production.v1"]
    with pytest.raises(ToolError, match="文件内容"):
        app.import_comic_external_image(
            shot["shot_id"],
            {
                **body,
                "data_url": "data:image/png;base64," + base64.b64encode(b"fake image").decode(),
            },
        )
    failed = next(r for r in app.runtime_store.list_runs() if r.status.value == "failed")
    assert failed.state["error_id"] and failed.state["trace_id"]
    with pytest.raises(ToolError, match="版本已变化"):
        app.import_comic_external_image(shot["shot_id"], {**body, "expected_prompt_version": 99})
    service = ArtifactService(app.runtime_store)
    service.trash(artifact.id, 1)
    service.purge(artifact.id, 2)
    assert app.comic_prompts.payload(app.comic_prompts.get(shot["shot_id"]))[
        "external_image"] is None
    replacement = app.import_comic_external_image(shot["shot_id"], body)
    assert replacement["created"] and replacement["artifact_id"] != artifact.id
    fresh = app.runtime_store.get_artifact(replacement["artifact_id"])
    assert Path(fresh.location).read_bytes() == content
    assert app.runtime_store.get_artifact(artifact.id).lifecycle_state == ArtifactLifecycle.PURGED
    assert app.comic_image_versions(fresh.id, request["conversation_id"])["versions"][0][
        "artifact_id"] == fresh.id
    # An active record with lost bytes also cannot report a successful duplicate upload.
    from kantoku.config.runtime_paths import get_runtime_paths
    get_runtime_paths().remove_file(Path(fresh.location), file_type="artifact")
    with pytest.raises(ToolError, match="重传缺失原文件"):
        app.import_comic_external_image(shot["shot_id"], body)


def test_director_lineage_binding_accepts_owning_conversation(app, production, monkeypatch):
    """确认动作新建的导演修订没有对应 Run；会话归属必须按血缘判断。

    只按“当前导演版本”查找归属，会把同一会话自己推进出来的方案
    判成外部会话，Prompt 确认因此被拒。
    """
    request, shot, prompt = _external(app, monkeypatch)
    project_id = request["production_project_id"]
    assert app._comic_conversation_owns_director(project_id, request["conversation_id"])
    confirmed = app.confirm_comic_prompt(shot["shot_id"], {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "expected_version": prompt["version"],
        "conversation_id": request["conversation_id"],
    })
    assert confirmed["user_confirmed"]


def test_director_lineage_binding_still_rejects_stranger_conversation(
    app, production, monkeypatch,
):
    """放开归属不等于放开权限：从未执行过该作品导演阶段的会话仍必须被拒。"""
    request, shot, prompt = _external(app, monkeypatch)
    project_id = request["production_project_id"]
    stranger = app.create_conversation(
        {"interaction_mode": "guided", "domain": "comic"})["id"]
    assert not app._comic_conversation_owns_director(project_id, stranger)
    with pytest.raises(ToolError, match="必须绑定当前 Comic 创作会话"):
        app.confirm_comic_prompt(shot["shot_id"], {
            "expected_project_version": app.comic_projects.get(project_id).project.current_version,
            "expected_version": prompt["version"],
            "conversation_id": stranger,
        })
