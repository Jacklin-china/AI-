"""会话故事的不可变修订、人工确认与材料导出；复用媒体及 Core 记录。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_STORED, ZipFile

from pydantic import ValidationError

from kantoku.config import ToolError
from kantoku.config.observability import current_run_id, current_trace_id, public_error
from kantoku.config.runtime_paths import (
    atomic_write_bytes,
    get_runtime_paths,
    validate_runtime_path,
)
from kantoku.core.conversations import InteractionMode
from kantoku.core.runtime.models import ArtifactLifecycle, ArtifactRecord, ArtifactType, utc_now

from .constraints import image_edit_constraint_basis
from .director_provenance import value_hash
from .models import (
    ComicAsset,
    ComicPromptArtifact,
    ComicStoryDraft,
    ComicStoryReviewOutput,
    ComicStoryShot,
    CreativeConstraint,
    ImageVersion,
    ImageVersionRef,
)
from .prompts import prompt_constraints

STORY_CONTRACT = "comic-story-3"
STORY_SEMANTICS = (
    "这是由静态图片发展为视频的故事。图片及其生成来源描述起始画面，角色/物体身份、固定资产与"
    "明确持续生效的硬要求必须保持；起始姿态和机位不等于整段视频必须静止。"
    "image_sources的reference_role=initial_frame：constraints及原始要求是该起始画面的来源依据，"
    "scope、trust、source_verified只说明原要求的对象及可信来源，不自行证明它必须贯穿视频。"
    "持续性须根据source_quote、effective_requirement、固定资产及本轮要求的真实语义判断；"
    "原图片编译器的negative_prompt属于单张图片生成规避词，不能建立视频全程禁令。"
    "有合理时间顺序和连续性时，可加入后续动作、对白、表情、运镜与转场；"
    "不能仅因原图片提示词没写这些后续事件就判为错误，也不能把合理先后变化当作同时冲突。"
    "无明确精确数量要求时，近义的规模描述或文案不统一至多是优化建议。"
    "真正错误包括起始状态衔接缺失、同一时刻互斥要求、身份/硬约束冲突、时长无法容纳必需动作；"
    "必须结合本轮故事要求和对应图片来源判断，不得取消用户要求的合法剧情来迎合静态图描述。"
    "user_intent若存在，是本次整理前的用户框架，须保留其叙事目标、合法事件、角色及对白；"
    "有错的执行方式可调整，并补足前后动作衔接，时长、运镜和转场须协调，不要求逐字照抄。"
    "user_intent为空时，以当前draft及instruction为目标，不追溯旧稿撤销用户新编辑。"
)
_SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def _key(image) -> tuple[str, int]:
    return (
        (image["image_id"], image["version"])
        if isinstance(image, dict)
        else (image.image_id, image.version)
    )


def _parse(raw, schema):
    try:
        if not isinstance(raw, str) or len(raw) > 160000:
            raise ValueError("capacity")
        return schema.model_validate_json(raw)
    except (ValueError, ValidationError):
        raise ToolError(
            "故事模型未返回有效的公开结构", detail="module=comic.story schema=invalid"
        ) from None


class ComicStoryService:
    """只表达故事业务；运行生命周期与具体模型调用由既有 Shell 注入。"""

    def __init__(self, media, workspace=None):
        self.media = media
        self.projects, self.runtime = media.projects, media.runtime
        self.workspace = workspace

    def story_id(self, conversation_id):
        work_id = self.workspace.work_for_story(conversation_id) if self.workspace else None
        return "comic-story-" + value_hash(work_id or conversation_id)[:32]

    def _require_image(self, ref, anchor, conversation_id, *, connection=None,
                       allow_related=True, snapshot=None):
        frozen = (ImageVersion.model_validate_json(json.dumps(snapshot, ensure_ascii=False))
                  if snapshot is not None else None)
        work_id = (
            self.workspace.work_for_story(conversation_id, connection=connection)
            if self.workspace
            else None
        )
        if work_id:
            if anchor != work_id:
                raise ToolError("故事所属作品不一致")
            return self.workspace.require_story_image(
                ref, work_id, connection=connection, snapshot=frozen)
        return self.media.require_image(
            ref, anchor, conversation_id, connection=connection, allow_related=allow_related,
            snapshot=frozen,
        )

    def _conversation(self, conversation_id):
        conversation = self.runtime.get_conversation(conversation_id)
        if (
            conversation.domain != "comic"
            or conversation.interaction_mode != InteractionMode.GUIDED
        ):
            raise ToolError(
                "故事必须属于当前 Comic 创作会话", detail="module=comic.story owner=mode"
            )

    def _anchor(self, connection, conversation_id):
        self._conversation(conversation_id)
        if self.workspace and (
            work_id := self.workspace.work_for_story(conversation_id, connection=connection)
        ):
            return work_id
        story_id = self.story_id(conversation_id)
        rows = connection.execute(
            "SELECT project_id,payload_json FROM comic_entity_versions "
            "WHERE entity_type='story_anchor' AND entity_id=? ORDER BY version DESC",
            (story_id,),
        ).fetchall()
        owners = {row["project_id"] for row in rows}
        if len(owners) > 1:
            raise ToolError(
                "故事绑定出现冲突，请查看运行记录", detail="module=comic.story anchor=conflict"
            )
        if not rows:
            return None
        payload = json.loads(rows[0]["payload_json"])
        if (
            payload.get("conversation_id") != conversation_id
            or payload.get("anchor_project_id") not in owners
        ):
            raise ToolError("故事归属记录不一致", detail="module=comic.story anchor=identity")
        self.media.require_owner(
            payload["anchor_project_id"], conversation_id, connection=connection
        )
        return payload["anchor_project_id"]

    def anchor_for(self, conversation_id, anchor_project_id=None):
        """只读解析，首次绑定必须由写事务完成。"""
        with self.projects._connect() as connection:
            anchor = self._anchor(connection, conversation_id)
            if anchor and anchor_project_id and anchor != anchor_project_id:
                raise ToolError("当前聊天的故事已绑定另一作品，请重新读取故事")
            if not anchor and anchor_project_id:
                self.projects.get(anchor_project_id)
                self.media.require_owner(anchor_project_id, conversation_id, connection=connection)
            return anchor or anchor_project_id

    def _current(self, connection, anchor, conversation_id):
        if not anchor:
            return 0, {}
        return self.media._latest(connection, anchor, "story", self.story_id(conversation_id))

    def _prepare(self, connection, request, *, bind=True):
        anchor = self._anchor(connection, request.conversation_id)
        if anchor and anchor != request.anchor_project_id:
            raise ToolError("故事所属作品已固定，请重新读取后保存")
        anchor = anchor or request.anchor_project_id
        self.media._write_guard(connection, anchor, request)
        version, story = self._current(connection, anchor, request.conversation_id)
        if version != request.expected_story_version:
            raise ToolError(
                "故事已在其他页面更新，请核对当前版本；生成候选仍保留在原运行记录",
                detail="module=comic.story cas=story",
            )
        if bind and not self._anchor(connection, request.conversation_id):
            self.projects._insert_version(
                connection,
                project_id=anchor,
                entity_type="story_anchor",
                entity_id=self.story_id(request.conversation_id),
                version=1,
                created_at=utc_now(),
                payload_json=json.dumps(
                    {"conversation_id": request.conversation_id, "anchor_project_id": anchor},
                    ensure_ascii=False,
                ),
            )
        return anchor, story

    def conversation_candidates(self, conversation_id):
        self._conversation(conversation_id)
        if (
            self.workspace
            and (result := self.workspace.candidates(conversation_id, self)) is not None
        ):
            return result
        with self.projects._connect() as connection:
            rows = connection.execute(
                "SELECT project_id,entity_id,payload_json FROM comic_entity_versions v "
                "WHERE entity_type='image_selection' AND version=(SELECT MAX(version) "
                "FROM comic_entity_versions WHERE project_id=v.project_id "
                "AND entity_type=v.entity_type AND entity_id=v.entity_id) AND ("
                "EXISTS (SELECT 1 FROM comic_entity_versions i WHERE i.project_id=v.project_id "
                "AND i.entity_type='image' AND i.entity_id=v.entity_id "
                "AND i.version=json_extract(v.payload_json,'$.selected_version') "
                "AND json_extract(i.payload_json,'$.conversation_id')=?) OR "
                "EXISTS (SELECT 1 FROM artifacts a WHERE a.id=v.entity_id "
                "AND a.conversation_id=? AND a.source='comic.external_image' "
                "AND json_extract(a.metadata_json,'$.project_id')=v.project_id "
                "AND json_extract(v.payload_json,'$.selected_version')=1)) "
                "ORDER BY created_at,entity_id",
                (conversation_id, conversation_id),
            ).fetchall()
            result, unavailable = [], []
            for row in rows:
                selection = json.loads(row["payload_json"])
                ref = ImageVersionRef(
                    image_id=row["entity_id"], version=selection["selected_version"]
                )
                try:
                    image = self.media.get(ref.image_id, version=ref.version,
                                           connection=connection, _legacy_history=True)
                except ToolError as error:
                    unavailable.append({**ref.model_dump(mode="json"),
                                        "reason": error.message})
                    continue
                if image.conversation_id != conversation_id:
                    continue
                self.media.require_owner(image.project_id, conversation_id, connection=connection)
                item = {
                    **self.media.image_view(image),
                    "selection": self.media.selection_view(image.project_id, selection,
                        conversation_id=conversation_id, connection=connection),
                    "source_project_title": self.projects.get(image.project_id).project.title,
                }
                if not item["available"]:
                    item["error"] = item["unavailable_reason"]
                result.append(item)
        return {"images": result, "unavailable_images": unavailable}

    def _images(self, draft, anchor, conversation_id, *, previous=None, connection=None):
        """新引用核对当前采用，既有固定引用只核对内容和归属。"""
        trusted = {_key(item): item for item in previous or []}
        result = {}
        for index, shot in enumerate(draft.shots, 1):
            identity = _key(shot.image)
            if identity in result:
                continue
            try:
                image = self._require_image(
                    shot.image, anchor, conversation_id, connection=connection, allow_related=True,
                    snapshot=trusted.get(identity),
                )
            except ToolError as error:
                raise ToolError(
                    f"镜头 {index} 的图片不可用：{error.message}",
                    detail="module=comic.story stage=images shot_id=" + shot.shot_id,
                ) from error
            frozen = image.model_dump(mode="json")
            if identity in trusted:
                # 快照已经过同一媒体校验；保留原序列化与指纹，不改写历史。
                frozen = trusted[identity]
            else:
                if connection is None:
                    with self.projects._connect() as opened:
                        selection = self.media._latest(
                            opened, image.project_id, "image_selection", image.image_id
                        )[1]
                else:
                    selection = self.media._latest(
                        connection, image.project_id, "image_selection", image.image_id
                    )[1]
                if selection.get("selected_version") != image.version:
                    raise ToolError(
                        "新入镜图片必须是当前聊天明确采用的版本",
                        detail="module=comic.story image=unselected",
                    )
            result[identity] = frozen
        return sorted(result.values(), key=_key)

    def _source_contexts(self, images, *, previous=None):
        """仅读取固定图片实际关联的不可变来源，不把当前作品理解当历史图事实。"""
        frozen = {_key(item["image"]): item for item in previous or []}
        result = []
        for image in images:
            if _key(image) in frozen:
                result.append(frozen[_key(image)])
                continue
            with self.projects._connect() as connection:
                imported = self.media._latest(
                    connection,
                    image["project_id"],
                    "legacy_image",
                    f"{image['image_id']}:{image['version']}",
                )[1]
            if imported:
                source = imported["source_context"]
                result.append(
                    {
                        **source,
                        "image": {"image_id": image["image_id"], "version": image["version"]},
                        "source_project_id": image["project_id"],
                        "image_artifact_id": image["artifact_id"],
                        "legacy_source": {
                            "image_id": imported["original"]["image_id"],
                            "version": imported["original"]["version"],
                            "source_project_id": imported["original"]["project_id"],
                        },
                    }
                )
                continue
            source = {
                "image": {"image_id": image["image_id"], "version": image["version"]},
                "source_project_id": image["project_id"],
                "image_artifact_id": image["artifact_id"],
                "instruction_artifact_id": image["instruction_artifact_id"],
                "source_kind": image["provenance"],
                "constraints": [],
                "negative_prompt": "",
            }
            if not image["instruction_artifact_id"]:
                result.append(source)
                continue
            artifact = self.runtime.get_artifact(image["instruction_artifact_id"])
            owner = artifact.conversation_id
            if not owner and artifact.run_id:
                owner = self.runtime.get_run(artifact.run_id).state.get("conversation_id")
            if (
                artifact.type != ArtifactType.PROMPT
                or artifact.status != "ready"
                or artifact.lifecycle_state != ArtifactLifecycle.ACTIVE
                or owner != image["conversation_id"]
                or artifact.metadata.get("project_id") != image["project_id"]
            ):
                raise ToolError("图片固定指令的归属或状态不合法")
            if artifact.metadata.get("image_operation") == "edit":
                self.media.require_edit_confirmation(artifact.id)
                payload = artifact.metadata.get("payload", {})
                if (
                    artifact.metadata.get("sha256") != value_hash(payload)
                    or payload.get("basis", {}).get("source_image") != image["parent"]
                ):
                    raise ToolError("图片修改来源的指纹或父版本不一致")
                scope = dict(payload["scope"])
                receipt = payload["constraint_receipt"]["review"]
                scope["constraint_resolution"] = {
                    **scope.get("constraint_resolution", {}),
                    "constraint_replacements": receipt.get("constraint_replacements", []),
                }
                constraints = [
                    CreativeConstraint.model_validate(item)
                    for item in [
                        *payload["basis"]["constraints"],
                        *payload.get("edit_constraints", []),
                    ]
                ]
                constraints = [
                    item
                    for item in constraints
                    if item.status == "active" and item.origin != "model_choice"
                ]
                source.update(
                    source_kind="confirmed_edit",
                    constraints=image_edit_constraint_basis(scope, constraints),
                    authorized_requirements=scope["authorized_requirements"],
                    negative_prompt=payload["output"].get("negative_prompt", ""),
                )
            else:
                metadata = artifact.metadata
                with self.projects._connect() as connection:
                    prompt = ComicPromptArtifact.model_validate_json(
                        self.projects._version_payload(
                            connection,
                            image["project_id"],
                            "prompt",
                            metadata["prompt_id"],
                            artifact.version,
                        )
                    )
                    if (
                        prompt.artifact_id != artifact.id
                        or prompt.positive_prompt != metadata.get("positive_prompt")
                        or prompt.negative_prompt != metadata.get("negative_prompt")
                    ):
                        raise ToolError("图片 Prompt 与固定修订不一致")
                    assets = []
                    refs = [*prompt.character_asset_versions, *prompt.scene_asset_versions]
                    if prompt.style_version:
                        refs.append(prompt.style_version)
                    for ref in refs:
                        assets.append(
                            ComicAsset.model_validate_json(
                                self.projects._version_payload(
                                    connection,
                                    image["project_id"],
                                    "asset",
                                    ref.asset_id,
                                    ref.version,
                                )
                            )
                        )
                snapshot = self.projects.get(image["project_id"], version=prompt.project_version)
                constraints = prompt_constraints(snapshot, assets)
                source.update(
                    source_kind="prompt_output",
                    constraints=[
                        item.model_dump(mode="json")
                        for item in constraints
                        if item.status == "active" and item.origin != "model_choice"
                    ],
                    negative_prompt=prompt.negative_prompt,
                    source_versions={
                        "prompt": prompt.version,
                        "creative_brief": prompt.creative_brief_version,
                    },
                )
                if snapshot.creative_brief.constraint_contract_version != 2:
                    source["user_requirements"] = snapshot.creative_brief.hard_constraints
                    source["asset_requirements"] = [
                        item for asset in assets for item in asset.fixed_constraints
                    ]
            result.append(source)
        return result

    @staticmethod
    def _content_hash(draft, fixed_images, source_contexts):
        return value_hash(
            {
                "draft": draft.model_dump(mode="json"),
                "fixed_images": fixed_images,
                "source_contexts": source_contexts,
            }
        )

    def _insert_story(
        self, connection, request, draft, fixed_images, source, run_id, source_contexts
    ):
        now = utc_now()
        artifact_id = "artifact-" + uuid4().hex
        story = {
            **draft.model_dump(mode="json"),
            "story_id": self.story_id(request.conversation_id),
            "conversation_id": request.conversation_id,
            "anchor_project_id": request.anchor_project_id,
            "version": request.expected_story_version + 1,
            "content_sha256": self._content_hash(draft, fixed_images, source_contexts),
            "fixed_images": fixed_images,
            "source_contexts": source_contexts,
            "source": source,
            "run_id": run_id,
            "artifact_id": artifact_id,
            "created_at": now.isoformat(),
            "schema_version": 1,
        }
        self.runtime.insert_artifact(
            connection,
            ArtifactRecord(
                id=artifact_id,
                type=ArtifactType.DOCUMENT,
                run_id=run_id,
                conversation_id=request.conversation_id,
                node_id="story.save",
                source="comic.story",
                status="ready",
                created_at=now,
                version=story["version"],
                metadata={
                    "project_id": request.anchor_project_id,
                    "story_id": story["story_id"],
                    "sha256": story["content_sha256"],
                    "payload": story,
                    "request_id": request.request_id,
                    "trace_id": current_trace_id(),
                },
            ),
        )
        self.projects._insert_version(
            connection,
            project_id=request.anchor_project_id,
            entity_type="story",
            entity_id=story["story_id"],
            version=story["version"],
            payload_json=json.dumps(story, ensure_ascii=False),
            created_at=now,
        )
        return story

    @staticmethod
    def _draft(story):
        return ComicStoryDraft.model_validate(
            {key: story[key] for key in ("title", "summary", "instruction", "shots")}
        )

    FIELD_LABELS = {
        "title": "故事标题", "summary": "故事概述", "instruction": "本轮故事要求",
        "action": "动作", "camera": "运镜", "duration_seconds": "时长",
        "transition": "转场", "image": "图片",
    }

    @classmethod
    def _structural_findings(cls, draft):
        """先查能确定性判断的结构问题：指到镜头与字段，并给出改法。

        这类问题不需要模型，结论也不会因重跑而漂移；模型只负责内容一致性。
        """
        findings = []
        suggestions = {
            "action": "补充这一镜的动作，写清画面中的主体做了什么。",
            "camera": "补充这一镜的运镜，写清相机的运动方向或固定机位。",
            "transition": "补充与下一镜的衔接方式；最后一镜可填写结束方式。",
        }
        for field, label in (("title", "故事标题"), ("summary", "故事概述")):
            if not getattr(draft, field).strip():
                findings.append({
                    "severity": "error", "code": "missing_" + field, "shot_id": None,
                    "field": field, "evidence": "（空）", "message": f"缺少{label}。",
                    "suggestion": f"补充{label}，说明这段故事的主题或主要情节。",
                })
        if not draft.shots:
            findings.append({
                "severity": "error", "code": "missing_shots", "shot_id": None,
                "field": "image", "evidence": "（还没有镜头）",
                "message": "故事还没有任何镜头，无法核验。",
                "suggestion": "在右侧「本作品的已采用图片」勾选图片后点「AI 整理初稿」，"
                              "或点「添加镜头」逐个补充。",
            })
            return findings
        for index, shot in enumerate(draft.shots, 1):
            for field, label, code in (
                ("action", "动作", "missing_action"),
                ("camera", "运镜", "missing_camera"),
                ("transition", "转场", "missing_transition"),
            ):
                if not str(getattr(shot, field)).strip():
                    findings.append({
                        "severity": "error", "code": code, "shot_id": shot.shot_id,
                        "field": field, "evidence": "（空）",
                        "message": f"镜头 {index} 缺少{label}。",
                        "suggestion": suggestions[field],
                    })
            if shot.duration_seconds is None:
                findings.append({
                    "severity": "error", "code": "missing_duration", "shot_id": shot.shot_id,
                    "field": "duration_seconds", "evidence": "（未填写）",
                    "message": f"镜头 {index} 缺少时长。",
                    "suggestion": "填写 0.1–60 秒之间的时长，按讲述节奏估算即可。",
                })
            elif not 0.1 <= shot.duration_seconds <= 60:
                findings.append({
                    "severity": "error", "code": "duration_conflict", "shot_id": shot.shot_id,
                    "field": "duration_seconds", "evidence": str(shot.duration_seconds),
                    "message": f"镜头 {index} 的时长超出有效范围。",
                    "suggestion": "将这一镜的时长改为 0.1–60 秒，再重新校验。",
                })
        return findings

    @staticmethod
    def _structural_review(findings):
        return {"status": "needs_revision", "findings": findings}

    def _review_current(self, connection, story, model):
        """内容没变就沿用原校验结论：同一份初稿点两次校验，结论不能自相矛盾。

        换了校验模型则重新核验，避免旧模型的结论盖住新口径。
        """
        payload = self.media._latest(
            connection, story["anchor_project_id"], "story_review",
            f"{story['story_id']}:{story['version']}",
        )[1]
        return bool(
            payload
            and payload.get("content_sha256") == story["content_sha256"]
            and payload.get("contract") == STORY_CONTRACT
            and payload.get("model") == model
        )

    @staticmethod
    def _complete(draft):
        findings = ComicStoryService._structural_findings(draft)
        if findings:
            raise ToolError("；".join(item["message"] for item in findings))

    def _manifest(self, story):
        images, filenames = [], {}
        for image in story["fixed_images"]:
            content = (image["sha256"], image["mime_type"])
            if content not in filenames:
                filenames[content] = (
                    f"images/image-{len(filenames) + 1:03d}-{image['sha256'][:8]}"
                    f"{_SUFFIXES[image['mime_type']]}"
                )
            images.append(
                {
                    "image_id": image["image_id"],
                    "version": image["version"],
                    "artifact_id": image["artifact_id"],
                    "source_project_id": image["project_id"],
                    "sha256": image["sha256"],
                    "mime_type": image["mime_type"],
                    "filename": filenames[content],
                }
            )
        by_ref = {_key(image): image for image in images}
        shots = [
            {
                "shot_id": shot["shot_id"],
                "sequence_number": index,
                "image_id": shot["image"]["image_id"],
                "image_version": shot["image"]["version"],
                "filename": by_ref[_key(shot["image"])]["filename"],
                **{
                    field: shot[field]
                    for field in ("action", "camera", "duration_seconds", "transition")
                },
            }
            for index, shot in enumerate(story["shots"], 1)
        ]
        return {
            "story_id": story["story_id"],
            "version": story["version"],
            "content_sha256": story["content_sha256"],
            "images": images,
            "shots": shots,
        }

    @staticmethod
    def _text(story, manifest):
        lines = [
            story["title"] or "故事制作材料",
            f"故事版本：v{story['version']}",
            "以下为视频制作文字，请同时提供对应图片。",
            "",
            "故事摘要",
            story["summary"],
            "",
            "用户需求",
            story["instruction"],
        ]
        for shot in manifest["shots"]:
            duration = shot["duration_seconds"]
            duration = duration if duration is not None else "未填写"
            lines += [
                "",
                f"镜头 {shot['sequence_number']}｜{shot['filename']}",
                f"动作：{shot['action']}",
                f"运镜：{shot['camera']}",
                f"时长：{duration} 秒",
                f"衔接：{shot['transition']}",
            ]
        return "\n".join(lines) + "\n"

    def _export_record(self, artifact_id, *, connection=None):
        if connection is None:
            artifact = self.runtime.get_artifact(artifact_id)
        else:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
            if row is None:
                raise ToolError("找不到素材包产物")
            artifact = self.runtime._artifact(row)
        path = validate_runtime_path(Path(artifact.location or ""))
        if (
            artifact.type != ArtifactType.DOCUMENT
            or artifact.status != "ready"
            or artifact.lifecycle_state != ArtifactLifecycle.ACTIVE
            or artifact.source != "comic.story.export"
            or not artifact.location
            or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != artifact.metadata.get("sha256")
        ):
            raise ToolError("素材包已失效、缺失或内容已改变，请核对原导出")
        return {
            "artifact_id": artifact.id,
            "filename": path.name,
            "content_url": f"/api/artifacts/{artifact.id}/content",
            "sha256": artifact.metadata["sha256"],
            "story_version": artifact.metadata["story_version"],
            "available": True,
        }

    def view(self, conversation_id, *, version=None, connection=None):
        if connection is None:
            with self.projects._connect() as opened:
                return self.view(conversation_id, version=version, connection=opened)
        anchor = self._anchor(connection, conversation_id)
        current_version, story = self._current(connection, anchor, conversation_id)
        if version is not None:
            if not anchor:
                raise ToolError("找不到指定故事版本")
            story = json.loads(
                self.projects._version_payload(
                    connection, anchor, "story", self.story_id(conversation_id), version
                )
            )
        project_version = self.projects.get(anchor).project.current_version if anchor else None
        result = {
            "story": story or None,
            "review": None,
            "confirmation": None,
            "export_text": "",
            "manifest": None,
            "missing_images": [],
            "exports": [],
            "project_version": project_version,
            "story_version": story.get("version", 0),
            "current_story_version": current_version,
            "anchor_project_id": anchor,
        }
        if not story:
            return result
        identity = f"{story['story_id']}:{story['version']}"
        for kind in ("review", "confirmation"):
            payload = self.media._latest(connection, anchor, "story_" + kind, identity)[1]
            if payload.get("content_sha256") == story["content_sha256"]:
                result[kind] = payload
        if result["confirmation"] and result["confirmation"].get("review_sha256") != value_hash(
            result["review"]
        ):
            result["confirmation"] = None
        result["manifest"] = self._manifest(story)
        result["export_text"] = self._text(story, result["manifest"])
        for image in story["fixed_images"]:
            try:
                self._require_image(
                    ImageVersionRef(image_id=image["image_id"], version=image["version"]),
                    anchor,
                    conversation_id,
                    connection=connection,
                    allow_related=True,
                    snapshot=image,
                )
            except ToolError as error:
                affected = [shot for shot in story["shots"] if _key(shot["image"]) == _key(image)]
                result["missing_images"].extend(
                    {
                        "shot_id": shot["shot_id"],
                        "reason": str(error),
                        "source_project_id": image["project_id"],
                        "image_id": image["image_id"],
                        "version": image["version"],
                        "artifact_id": image["artifact_id"],
                        "error": str(error),
                    }
                    for shot in affected
                )
        rows = connection.execute(
            "SELECT payload_json FROM comic_entity_versions "
            "WHERE project_id=? AND entity_type='story_export' AND entity_id=? "
            "ORDER BY version DESC",
            (anchor, identity),
        ).fetchall()
        for row in rows:
            saved = json.loads(row["payload_json"])
            try:
                result["exports"].append(
                    self._export_record(saved["artifact_id"], connection=connection)
                )
            except ToolError as error:
                result["exports"].append({**saved, "available": False, "error": str(error)})
        return result

    def versions(self, conversation_id):
        with self.projects._connect() as connection:
            anchor = self._anchor(connection, conversation_id)
            rows = (
                connection.execute(
                    "SELECT payload_json FROM comic_entity_versions "
                    "WHERE project_id=? AND entity_type='story' AND entity_id=? "
                    "ORDER BY version DESC",
                    (anchor, self.story_id(conversation_id)),
                ).fetchall()
                if anchor
                else []
            )
        return {"versions": [json.loads(row["payload_json"]) for row in rows]}

    def _finish(self, connection, request, operation, *, run_id=None, **extra):
        result = {**self.view(request.conversation_id, connection=connection), **extra}
        if run_id:
            result["run_id"] = run_id
        return self.media._finish(
            connection, request.anchor_project_id, operation, request, result, advance_project=False
        )

    def save(self, request, *, run_id=None, imported_images=None):
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if saved := self.media.request_result(
                request.anchor_project_id, "story.save", request, connection=connection
            ):
                return saved
            anchor, previous = self._prepare(connection, request)
            if imported_images is not None:
                for image in imported_images:
                    mapping = self.media._latest(
                        connection,
                        image["project_id"],
                        "legacy_image",
                        f"{image['image_id']}:{image['version']}",
                    )[1]
                    if not mapping or mapping["original"]["sha256"] != image["sha256"]:
                        raise ToolError("历史故事引用必须具有已核验的导入映射")
            images = self._images(
                request.draft,
                anchor,
                request.conversation_id,
                previous=previous.get("fixed_images") or imported_images,
                connection=connection,
            )
            sources = self._source_contexts(images, previous=previous.get("source_contexts"))
            if self._content_hash(request.draft, images, sources) != previous.get("content_sha256"):
                self._insert_story(
                    connection, request, request.draft, images, "human_edit", run_id, sources
                )
            return self._finish(connection, request, "story.save", run_id=run_id)

    def _candidate(self, run_id, payload):
        run = self.runtime.get_run(run_id)
        self.runtime.update_run(
            run_id,
            status=run.status,
            state={**run.state, "story_candidate": payload},
            current_node=run.current_node,
            error=run.error,
        )

    def _execute(self, request, operation, run_id, action):
        if saved := self.media.claim_request(
            request.anchor_project_id, operation, request, run_id=run_id
        ):
            return saved
        try:
            return action()
        except Exception as error:
            try:
                self.media.fail_request(request.anchor_project_id, request, error)
            except Exception as persistence_error:
                public_error(
                    persistence_error,
                    component="comic.story.failure_record",
                    project_id=request.anchor_project_id,
                    request_id=request.request_id,
                    run_id=run_id,
                )
            raise

    @staticmethod
    def _evidence_quote(value, evidence):
        """只接受同字段原文或确定性等价表示，返回实际字段中的引用。"""
        fragment = evidence.strip()
        if not fragment:
            return None
        if isinstance(value, ImageVersionRef):
            if fragment == value.image_id:
                return fragment
            try:
                ref = ImageVersionRef.model_validate_json(fragment)
            except (ValueError, ValidationError):
                return None
            return json.dumps(value.model_dump(), ensure_ascii=False) if ref == value else None
        if isinstance(value, (int, float)):
            number = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(?:s|秒)?",
                                  fragment)
            return str(value) if number and float(number[1]) == value else None
        if not isinstance(value, str):
            return None
        if fragment in value:
            return fragment
        # 换行/连续空格属于排版；保留词边界，不删字或做语义模糊匹配。
        pattern = r"\s+".join(re.escape(part) for part in re.split(r"\s+", fragment))
        match = re.search(pattern, value)
        return match[0] if match else None

    def _review_findings(self, draft, output):
        shots = {shot.shot_id: (index, shot) for index, shot in enumerate(draft.shots, 1)}
        checked, issues = [], []
        for index, finding in enumerate(output.findings, 1):
            position = "故事"
            if finding.shot_id:
                entry = shots.get(finding.shot_id)
                if entry is None or finding.field not in {
                    "action", "camera", "duration_seconds", "transition", "image",
                }:
                    issues.append({"finding": index, "reason": "invalid_shot_or_field",
                                   "message": f"第 {index} 条故事审核引用了不存在的镜头或字段"})
                    continue
                position, shot = f"镜头 {entry[0]}", entry[1]
                value = getattr(shot, finding.field)
            elif finding.field in {"title", "summary", "instruction"}:
                value = getattr(draft, finding.field)
            else:
                issues.append({"finding": index, "reason": "missing_shot_id",
                               "message": f"第 {index} 条故事审核缺少对应镜头身份"})
                continue
            quote = self._evidence_quote(value, finding.evidence)
            if quote is None:
                issues.append({"finding": index, "field": finding.field,
                               "reason": "evidence_mismatch",
                               "message": f"{position} · {self.FIELD_LABELS[finding.field]}："
                               "故事审核证据与当前字段不一致，请重新校验"})
                continue
            finding.evidence = quote
            if not finding.suggestion.strip():
                finding.suggestion = f"请修改{self.FIELD_LABELS[finding.field]}：{finding.message}"
            checked.append(finding)
        if output.status != "pass" and not output.findings:
            issues.append({"reason": "missing_findings",
                           "message": "故事校验未给出问题位置和修改建议，请重新校验"})
        return checked, issues

    @staticmethod
    def _story_sources(source_contexts):
        """视频只接收有依据的起始帧来源；单图编译负向词不是用户的持续要求。"""
        return [{**{key: value for key, value in source.items() if key != "negative_prompt"},
                 "reference_role": "initial_frame"} for source in source_contexts]

    def _review_output(self, draft, images, model_call, source_contexts, *, user_intent=None):
        sources = [
            {key: image[key] for key in ("image_id", "version", "filename", "provenance")}
            for image in images
        ]
        messages = [
            {
                "role": "system",
                "content": "独立核验公开故事中的动作、运镜、时长、衔接与用户需求的一致性。"
                "图片身份仅为固定来源，不证明不同图片中的人物是同一角色；没有视觉证据时不要假装看过图片。"
                "image_sources是各图片起始画面的来源依据，仅适用于使用对应图片的镜头。"
                "scoped_replacement以effective_requirement为准，不能恢复被替代的旧目标；"
                "保留其他对象和未变属性，不把授权范围扩为整张图。"
                + STORY_SEMANTICS +
                "只返回给定Schema的公开JSON；finding引用实际字段的逐字evidence和shot_id，"
                "field必须是引文所属的当前字段，不得引用用户要求、其他字段或修复前的旧稿来充当该字段原文。"
                "时长引用数值；图片引用image_id或完整image对象；文字保留原文，不概括。"
                "并给出code（missing_title/missing_summary/missing_shots/missing_action/"
                "missing_camera/missing_transition/missing_duration/"
                "duration_conflict/camera_conflict/transition_conflict/image_conflict/"
                "requirement_conflict/continuity/other）与suggestion（一句可执行建议：改哪个字段、怎么改）。"
                "存在真实冲突为error/needs_revision，艺术建议为warning，未发现问题为pass/findings=[]。"
                "message只写一句简短问题，suggestion只写一句可执行修改，避免复述整段故事或长篇论证；"
                "详细依据只放evidence，不要重复到message或suggestion。"
                "不重新生成、不自动修复、不输出私有推理。Schema："
                + json.dumps(ComicStoryReviewOutput.model_json_schema(), ensure_ascii=False),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "draft": draft.model_dump(mode="json"),
                        "images": sources,
                        "image_sources": self._story_sources(source_contexts),
                        "user_intent": user_intent.model_dump(mode="json") if user_intent else None,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        verified, issues = {}, []
        for attempt in range(2):
            inputs = messages
            if attempt:
                inputs = [*messages, {"role": "user", "content": (
                    "上次公开审核结构或引文无效，只纠正一次。依据当前原稿重新返回完整公开审核JSON，"
                    "不得重写故事或输出私有推理。保留下列已核验发现；其余引文只能来自所指当前字段。"
                    + json.dumps({"issues": issues, "verified_findings": [
                        item.model_dump(mode="json") for item in verified.values()]},
                        ensure_ascii=False))}]
            # 仅成功返回后的契约错误可纠正；传输失败或结果未知直接退出，不重发模型请求。
            raw = model_call(inputs)
            try:
                output = _parse(raw, ComicStoryReviewOutput)
            except ToolError:
                issues = [{"reason": "invalid_public_structure",
                           "message": "故事模型未返回有效的公开结构"}]
            else:
                checked, issues = self._review_findings(draft, output)
                for finding in checked:
                    key = (finding.shot_id, finding.field, finding.code,
                           finding.evidence, finding.severity)
                    verified.setdefault(key, finding)
                if not issues and len(verified) <= 100:
                    output.findings = list(verified.values())
                    if any(item.severity == "error" for item in output.findings):
                        output.status = "needs_revision"
                    elif output.findings:
                        output.status = "warning"
                    return output.model_dump(mode="json")
                if not issues:
                    issues = [{"reason": "finding_capacity",
                               "message": "故事审核发现过多，请缩小校验范围"}]
            error = ToolError(issues[0]["message"], detail="module=comic.story.review "
                              f"reason={issues[0]['reason']} attempt={attempt + 1}")
            public_error(error, component="comic.story.review_contract", stage="review",
                         run_id=current_run_id())
            if attempt:
                raise error

    def _insert_review(self, connection, story, output, *, model, run_id,
                       validation_requested=False):
        identity = f"{story['story_id']}:{story['version']}"
        version, _ = self.media._latest(
            connection, story["anchor_project_id"], "story_review", identity
        )
        payload = {
            **output,
            "content_sha256": story["content_sha256"],
            "story_version": story["version"],
            "model": model,
            "run_id": run_id,
            "created_at": utc_now().isoformat(),
            "contract": STORY_CONTRACT,
            "validation_requested": validation_requested,
        }
        self.projects._insert_version(
            connection,
            project_id=story["anchor_project_id"],
            entity_type="story_review",
            entity_id=identity,
            version=version + 1,
            payload_json=json.dumps(payload, ensure_ascii=False),
            created_at=utc_now(),
        )
        return payload

    @staticmethod
    def _merge_user_draft(draft, skeleton):
        """固定用户镜头身份及图片，采纳协调后的制作字段，缺值才沿用原框架。"""
        if skeleton is None:
            return draft
        merged = draft.model_copy(deep=True)
        if skeleton.title.strip():
            merged.title = skeleton.title
        if skeleton.summary.strip():
            merged.summary = skeleton.summary
        if not skeleton.shots:
            return merged
        pool = list(draft.shots)
        shots = []
        user_ids = {shot.shot_id for shot in skeleton.shots}
        image_counts = {}
        for shot in skeleton.shots:
            image_counts[_key(shot.image)] = image_counts.get(_key(shot.image), 0) + 1
        unmatched_images = set()
        for user in skeleton.shots:
            index = next((position for position, item in enumerate(pool)
                          if item.shot_id == user.shot_id
                          and _key(item.image) == _key(user.image)), None)
            if index is None and image_counts[_key(user.image)] == 1:
                # 兼容模型未沿用 ID 的唯一同图结果，不能借用另一用户镜头或其他图片的内容。
                matches = [position for position, item in enumerate(pool)
                           if _key(item.image) == _key(user.image)
                           and item.shot_id not in user_ids]
                index = matches[0] if len(matches) == 1 else None
            model_shot = pool.pop(index) if index is not None else None
            if model_shot is None:
                unmatched_images.add(_key(user.image))
            shots.append(ComicStoryShot(
                shot_id=(user.shot_id or (model_shot.shot_id if model_shot
                                          else f"shot-{len(shots) + 1}")),
                image=user.image,
                action=(model_shot.action if model_shot and model_shot.action.strip()
                        else user.action),
                camera=(model_shot.camera if model_shot and model_shot.camera.strip()
                        else user.camera),
                duration_seconds=(model_shot.duration_seconds if model_shot
                                  and model_shot.duration_seconds is not None
                                  else user.duration_seconds),
                transition=(model_shot.transition if model_shot and model_shot.transition.strip()
                            else user.transition),
            ))
        used = {shot.shot_id for shot in shots}
        for extra in pool:
            if extra.shot_id in user_ids or _key(extra.image) in unmatched_images:
                continue
            if extra.shot_id in used:
                extra = extra.model_copy(update={"shot_id": f"shot-{uuid4().hex}"})
            used.add(extra.shot_id)
            shots.append(extra)
        merged.shots = shots
        return merged

    def _vision_payload(self, images, payload):
        content = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
        for image in images:
            artifact = self.runtime.get_artifact(image["artifact_id"])
            data = validate_runtime_path(Path(artifact.location)).read_bytes()
            if hashlib.sha256(data).hexdigest() != image["sha256"]:
                raise ToolError("图片内容在故事生成前发生变化")
            content.append({"type": "text",
                            "text": f"图片来源 {image['image_id']} v{image['version']}"})
            content.append({"type": "image_url", "image_url": {
                "url": f"data:{image['mime_type']};base64,"
                + base64.b64encode(data).decode("ascii")}})
        return content

    @staticmethod
    def _repair_scope(draft, findings):
        """以错误定位镜头；执行字段协同修正，真实交界冲突可联动下一镜。"""
        fields = {"action", "camera", "duration_seconds", "transition"}
        scope = {"shots": {}, "story": [], "create_shots": False}
        positions = {shot.shot_id: index for index, shot in enumerate(draft.shots)}
        for item in findings:
            if item.get("severity") != "error":
                continue
            shot_id, field = item.get("shot_id"), item["field"]
            if shot_id in positions and field in fields:
                scope["shots"][shot_id] = sorted(fields)
                if field == "transition" and item["code"] in {"continuity", "transition_conflict"}:
                    following = positions[shot_id] + 1
                    if following < len(draft.shots):
                        scope["shots"][draft.shots[following].shot_id] = sorted(fields)
            elif not shot_id and field in {"title", "summary"}:
                scope["story"].append(field)
            elif not draft.shots and item["code"] == "missing_shots":
                scope["create_shots"] = True
        return scope

    def _repair(self, draft, findings, images, source_contexts, vision_model_call, instruction,
                *, user_intent=None):
        """在出错镜头内协调制作字段，输出仍由独立审核决定是否可交付。"""
        if not findings:
            return None
        payload = {
            "instruction": instruction,
            "current_draft": draft.model_dump(mode="json"),
            "user_intent": user_intent.model_dump(mode="json") if user_intent else None,
            "issues": findings,
            "repair_scope": self._repair_scope(draft, findings),
            "images": [{key: image[key] for key in ("image_id", "version", "filename")}
                       for image in images],
            "image_sources": self._story_sources(source_contexts),
        }
        messages = [
            {"role": "system", "content": "按 issues 及 repair_scope 修正 current_draft。"
             "错误镜头的动作、运镜、时长与转场应一起协调；不能只改一个字段而留下相互矛盾的运动。"
             "已有用户填写内容也应纠正错误的执行方式，但必须保留原本合法剧情和对白。"
             "只修改repair_scope授权的字段，保留其余文字、shot_id、镜头顺序与图片；不新增无关内容。"
             "仅create_shots=true时可从所给images补建完整镜头。"
             + STORY_SEMANTICS +
             "每个镜头必须写清动作、运镜、时长与转场。只返回完整 ComicStoryDraft JSON。Schema："
             + json.dumps(ComicStoryDraft.model_json_schema(), ensure_ascii=False)},
            {"role": "user", "content": self._vision_payload(images, payload)},
        ]
        try:
            return _parse(vision_model_call(messages), ComicStoryDraft)
        except ToolError as error:
            public_error(error, component="comic.story.repair", stage="repair",
                         run_id=current_run_id())
            return None

    def _repair_once(self, draft, findings, images, source_contexts, vision_model_call,
                     instruction, allowed, *, user_intent=None):
        """一次有界修复：修完仍缺字段或引用未选图片就退回原稿，不连环重试。"""
        repaired = self._repair(draft, findings, images, source_contexts,
                                vision_model_call, instruction, user_intent=user_intent)
        if repaired is None:
            return draft
        # 模型返回完整 JSON，只采纳授权范围，图片、镜头身份与顺序不接受模型改写。
        restricted = draft.model_copy(deep=True)
        repaired_shots = {shot.shot_id: shot for shot in repaired.shots}
        scope = self._repair_scope(draft, findings)
        if scope["create_shots"]:
            restricted.shots = repaired.shots
        for field in scope["story"]:
            setattr(restricted, field, getattr(repaired, field))
        for shot_id, fields in scope["shots"].items():
            replacement = repaired_shots.get(shot_id)
            original = next((shot for shot in restricted.shots if shot.shot_id == shot_id), None)
            if original and replacement and _key(original.image) == _key(replacement.image):
                for field in fields:
                    setattr(original, field, getattr(replacement, field))
        repaired = restricted
        repaired.instruction = instruction
        if any(_key(shot.image) not in allowed for shot in repaired.shots):
            return draft
        if self._structural_findings(repaired):
            return draft
        return draft if repaired == draft else repaired

    def generate(self, request, *, vision_model_call, model_call, model, vision_model, run_id):
        if saved := self.media.request_result(request.anchor_project_id, "story.generate", request):
            return saved
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            anchor, previous = self._prepare(connection, request)
            probe = ComicStoryDraft(
                shots=[
                    {"shot_id": f"source-{index}", "image": ref.model_dump()}
                    for index, ref in enumerate(request.images)
                ]
            )
            images = self._images(probe, anchor, request.conversation_id,
                                  previous=previous.get("fixed_images"), connection=connection)
        source_contexts = self._source_contexts(images)
        skeleton = request.draft
        allowed = {_key(image) for image in images}
        if skeleton and any(_key(shot.image) not in allowed for shot in skeleton.shots):
            raise ToolError("已填写镜头的图片必须包含在本次整理范围内，请保留这些图片后再整理")

        def frozen_for(draft):
            frozen = [image for image in images
                      if _key(image) in {_key(shot.image) for shot in draft.shots}]
            return frozen, [item for item in source_contexts
                            if _key(item["image"]) in {_key(image) for image in frozen}]

        def retain(draft, frozen, frozen_sources, output=None):
            candidate = {
                "conversation_id": request.conversation_id,
                "anchor_project_id": anchor,
                "request": request.model_dump(mode="json"),
                "draft": draft.model_dump(mode="json"),
                "fixed_images": frozen,
                "source_contexts": frozen_sources,
                "content_sha256": self._content_hash(draft, frozen, frozen_sources),
                "vision_model": vision_model,
                "created_at": utc_now().isoformat(),
            }
            if output is not None:
                candidate.update(review=output, review_model=model, review_contract=STORY_CONTRACT)
            self._candidate(run_id, candidate)

        def review_candidate(draft, frozen, frozen_sources):
            try:
                return self._review_output(draft, frozen, model_call, frozen_sources,
                                           user_intent=skeleton)
            except ToolError as error:
                raise ToolError("初稿审核未完成，候选已保留，可恢复后重新校验。"
                                + error.message, detail=error.detail) from error

        def action():
            payload = {
                "instruction": request.instruction,
                "current_draft": skeleton.model_dump(mode="json") if skeleton else None,
                "user_intent": skeleton.model_dump(mode="json") if skeleton else None,
                "images": [{key: image[key] for key in ("image_id", "version", "filename")}
                           for image in images],
                "image_sources": self._story_sources(source_contexts),
            }
            messages = [
                {
                    "role": "system",
                    "content": "依据用户明确选中的图片和故事需求编排可编辑视频制作故事。"
                    "current_draft 是待整理的标题、概述与镜头框架：保留有效剧情与对白并补全空缺，"
                    "主动协调动作、运镜、时长与转场，纠正错误执行方式，不能照抄有矛盾的原稿。"
                    "框架与instruction共同表达用户意图；仅两者均无内容时才由你自行构思。"
                    "可以单图成段或多图连续，每镜头只能引用输入的image_id及version，"
                    "镜数不超过100；已有镜头须沿用shot_id及对应图片，不得遗漏或重排。"
                    "不把不同人像强行当同一角色，不虚构可见图片身份；动作与运镜不能冲突。"
                    "image_sources只约束对应图片，不将某张图的规则套在其他图；"
                    "scoped_replacement以有效新要求为准，不恢复旧目标，保留其他对象和未变属性。"
                    + STORY_SEMANTICS +
                    "只返回公开ComicStoryDraft JSON，完整填写动作、运镜、合理时长及衔接，"
                    "instruction保持用户原文；shot_id独立且不重复，不输出私有推理。Schema："
                    + json.dumps(ComicStoryDraft.model_json_schema(), ensure_ascii=False),
                },
                {"role": "user", "content": self._vision_payload(images, payload)},
            ]
            draft = self._merge_user_draft(
                _parse(vision_model_call(messages), ComicStoryDraft), skeleton)
            draft.instruction = request.instruction
            if any(_key(shot.image) not in allowed for shot in draft.shots):
                raise ToolError("故事模型引用了未明确选中的图片版本")
            frozen, frozen_sources = frozen_for(draft)
            retain(draft, frozen, frozen_sources)
            # 整理包含自动纠错；最多两次内容修正，每次结果仍须真实复核。
            structural = self._structural_findings(draft)
            repairs_left = 2 - bool(structural)
            before_repair = draft
            draft = self._repair_once(draft, structural, images,
                                      source_contexts, vision_model_call,
                                      request.instruction, allowed, user_intent=skeleton)
            frozen, frozen_sources = frozen_for(draft)
            structural = self._structural_findings(draft)
            if draft is not before_repair:
                retain(draft, frozen, frozen_sources)
            output = (self._structural_review(structural) if structural
                      else review_candidate(draft, frozen, frozen_sources))
            retain(draft, frozen, frozen_sources, output)
            for _ in range(repairs_left):
                if structural or output["status"] != "needs_revision":
                    break
                fixed = self._repair_once(draft, output["findings"], images, source_contexts,
                                          vision_model_call, request.instruction, allowed,
                                          user_intent=skeleton)
                if fixed is draft:
                    break
                fixed_frozen, fixed_sources = frozen_for(fixed)
                fixed_output = review_candidate(fixed, fixed_frozen, fixed_sources)
                draft, frozen, frozen_sources = fixed, fixed_frozen, fixed_sources
                output = fixed_output
                retain(draft, frozen, frozen_sources, output)
            if output["status"] == "needs_revision":
                raise ToolError("初稿自动修复尚未完成，候选已保留，当前故事未被替换。请重新整理。",
                                detail="module=comic.story reason=automatic_repair_incomplete "
                                f"findings={len(output['findings'])}")
            self._complete(draft)
            with self.projects._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._prepare(connection, request)
                self._images(
                    draft, anchor, request.conversation_id, previous=frozen, connection=connection
                )
                story = self._insert_story(
                    connection, request, draft, frozen, "model_generated", run_id, frozen_sources
                )
                self._insert_review(connection, story, output, model=model, run_id=run_id)
                return self._finish(connection, request, "story.generate", run_id=run_id)

        return self._execute(request, "story.generate", run_id, action)

    def review(self, request, *, model_call, model, run_id):
        if saved := self.media.request_result(request.anchor_project_id, "story.review", request):
            return saved
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            anchor, story = self._prepare(connection, request)
            if not story:
                raise ToolError("请先保存故事再核验")
            draft = self._draft(story)
            images = self._images(
                draft,
                anchor,
                request.conversation_id,
                previous=story["fixed_images"],
                connection=connection,
            )

        def action():
            # 先做确定性检查：缺字段这类问题不必问模型，坐标与改法都更准；
            # 内容没变则沿用上次结论，避免同一份初稿两次校验结论不一致。
            with self.projects._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._prepare(connection, request)
                structural = self._structural_findings(draft)
                if structural:
                    self._insert_review(connection, story, self._structural_review(structural),
                                        model="local_structural", run_id=run_id,
                                        validation_requested=True)
                    return self._finish(connection, request, "story.review", run_id=run_id)
                if self._review_current(connection, story, model):
                    review = self.view(request.conversation_id, connection=connection)["review"]
                    if not review.get("validation_requested"):
                        self._insert_review(connection, story, review, model=model, run_id=run_id,
                                            validation_requested=True)
                    return self._finish(connection, request, "story.review", run_id=run_id,
                                        review_reused=True)
            output = self._review_output(draft, images, model_call, story["source_contexts"])
            with self.projects._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._prepare(connection, request)
                self._images(
                    draft, anchor, request.conversation_id, previous=images, connection=connection
                )
                self._insert_review(connection, story, output, model=model, run_id=run_id,
                                    validation_requested=True)
                return self._finish(connection, request, "story.review", run_id=run_id)

        return self._execute(request, "story.review", run_id, action)

    def confirm(self, request, *, run_id=None):
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if saved := self.media.request_result(
                request.anchor_project_id, "story.confirm", request, connection=connection
            ):
                return saved
            anchor, story = self._prepare(connection, request)
            if not story:
                raise ToolError("请先保存故事再确认")
            draft = self._draft(story)
            # 先看校验结论：有未通过的问题时说清"先校验"，比只说某个字段没填更有用。
            view = self.view(request.conversation_id, connection=connection)
            review = view["review"]
            if (
                not review
                or not review.get("validation_requested")
                or review["status"] == "needs_revision"
                or any(item["severity"] == "error" for item in review["findings"])
            ):
                raise ToolError("当前故事尚未通过校验，请先校验或修改后重新校验")
            self._complete(draft)
            self._images(
                draft,
                anchor,
                request.conversation_id,
                previous=story["fixed_images"],
                connection=connection,
            )
            if review["status"] == "warning" and not request.accept_warnings:
                raise ToolError("当前故事有核验建议，请明确接受建议后再确认")
            identity = f"{story['story_id']}:{story['version']}"
            version, _ = self.media._latest(connection, anchor, "story_confirmation", identity)
            payload = {
                "story_version": story["version"],
                "content_sha256": story["content_sha256"],
                "confirmed": True,
                "accept_warnings": request.accept_warnings,
                "review_sha256": value_hash(review),
                "run_id": run_id,
                "created_at": utc_now().isoformat(),
            }
            self.projects._insert_version(
                connection,
                project_id=anchor,
                entity_type="story_confirmation",
                entity_id=identity,
                version=version + 1,
                payload_json=json.dumps(payload),
                created_at=utc_now(),
            )
            return self._finish(connection, request, "story.confirm", run_id=run_id)

    def restore(self, request, *, run_id=None):
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if saved := self.media.request_result(
                request.anchor_project_id, "story.restore", request, connection=connection
            ):
                return saved
            anchor, _ = self._prepare(connection, request)
            historical = json.loads(
                self.projects._version_payload(
                    connection,
                    anchor,
                    "story",
                    self.story_id(request.conversation_id),
                    request.version,
                )
            )
            draft = self._draft(historical)
            images = self._images(
                draft,
                anchor,
                request.conversation_id,
                previous=historical["fixed_images"],
                connection=connection,
            )
            self._insert_story(
                connection,
                request,
                draft,
                images,
                "history_restore",
                run_id,
                historical["source_contexts"],
            )
            return self._finish(connection, request, "story.restore", run_id=run_id)

    def adopt_candidate(self, request, *, run_id=None):
        original = self.runtime.get_run(request.candidate_run_id)
        candidate = original.state.get("story_candidate")
        if (
            original.domain != "comic"
            or original.workflow != "comic.story.generate"
            or not candidate
            or original.state.get("conversation_id") != request.conversation_id
            or candidate.get("conversation_id") != request.conversation_id
            or candidate.get("anchor_project_id") != request.anchor_project_id
        ):
            raise ToolError("生成候选不属于当前聊天故事")
        draft = ComicStoryDraft.model_validate(candidate["draft"])
        if candidate.get("content_sha256") != self._content_hash(
            draft, candidate["fixed_images"], candidate["source_contexts"]
        ):
            raise ToolError("生成候选来源指纹不一致")
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if saved := self.media.request_result(
                request.anchor_project_id, "story.adopt-candidate", request, connection=connection
            ):
                return saved
            anchor, _ = self._prepare(connection, request)
            images = self._images(
                draft,
                anchor,
                request.conversation_id,
                previous=candidate["fixed_images"],
                connection=connection,
            )
            story = self._insert_story(
                connection,
                request,
                draft,
                images,
                "candidate_recovery",
                run_id,
                candidate["source_contexts"],
            )
            if candidate.get("review") and candidate.get("review_contract") == STORY_CONTRACT:
                self._insert_review(
                    connection,
                    story,
                    candidate["review"],
                    model=candidate["review_model"],
                    run_id=original.id,
                )
            return self._finish(
                connection,
                request,
                "story.adopt-candidate",
                run_id=run_id,
                candidate_run_id=original.id,
            )

    def export_bundle(self, request, *, run_id=None, output_dir=None):
        operation = "story.exports"
        if saved := self.media.request_result(request.anchor_project_id, operation, request):
            self._export_record(saved["export_artifact_id"])
            return saved
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            anchor, story = self._prepare(connection, request)
            if not story:
                raise ToolError("请先完成故事再导出")
            current = self.view(request.conversation_id, connection=connection)
            self._complete(self._draft(story))
            if not current["confirmation"] or current["confirmation"][
                "review_sha256"
            ] != value_hash(current["review"]):
                raise ToolError("请确认当前故事与核验结果后再导出")
            if current["missing_images"]:
                raise ToolError(
                    "故事中有缺失或已改变的图片，素材包未导出",
                    detail="module=comic.story export=images",
                )
        stream, files = io.BytesIO(), {}
        for image in current["manifest"]["images"]:
            ref = ImageVersionRef(image_id=image["image_id"], version=image["version"])
            frozen = next(item for item in story["fixed_images"] if _key(item) == _key(ref))
            self._require_image(ref, anchor, request.conversation_id, allow_related=True,
                                snapshot=frozen)
            artifact = self.runtime.get_artifact(image["artifact_id"])
            content = validate_runtime_path(Path(artifact.location)).read_bytes()
            if hashlib.sha256(content).hexdigest() != image["sha256"]:
                raise ToolError("图片在导出过程中发生变化，已停止导出")
            files.setdefault(image["filename"], content)
        with ZipFile(stream, "w", compression=ZIP_STORED) as archive:
            archive.writestr("story.md", current["export_text"].encode("utf-8"))
            archive.writestr(
                "manifest.json",
                json.dumps(current["manifest"], ensure_ascii=False, indent=2).encode("utf-8"),
            )
            for filename, content in files.items():
                archive.writestr(filename, content)
        content = stream.getvalue()
        manager = get_runtime_paths()
        path = validate_runtime_path(
            Path(output_dir) if output_dir else manager.artifacts / "comic-stories"
        ) / (f"story-v{story['version']}-{uuid4().hex[:12]}.zip")
        committed = False
        try:
            atomic_write_bytes(
                path, content, file_type="export", owner="comic.story", replace=False
            )
            with self.projects._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if saved := self.media.request_result(
                    anchor, operation, request, connection=connection
                ):
                    self._export_record(saved["export_artifact_id"])
                    return saved
                self._prepare(connection, request)
                refreshed = self.view(request.conversation_id, connection=connection)
                if (
                    refreshed["confirmation"] != current["confirmation"]
                    or refreshed["review"] != current["review"]
                ):
                    raise ToolError("故事确认或核验已改变，请核对后重新导出")
                self._images(
                    self._draft(story),
                    anchor,
                    request.conversation_id,
                    previous=story["fixed_images"],
                    connection=connection,
                )
                artifact_id, now = "artifact-" + uuid4().hex, utc_now()
                self.runtime.insert_artifact(
                    connection,
                    ArtifactRecord(
                        id=artifact_id,
                        type=ArtifactType.DOCUMENT,
                        run_id=run_id,
                        conversation_id=request.conversation_id,
                        node_id=operation,
                        source="comic.story.export",
                        status="ready",
                        created_at=now,
                        location=str(path),
                        metadata={
                            "project_id": anchor,
                            "story_id": story["story_id"],
                            "story_version": story["version"],
                            "content_sha256": story["content_sha256"],
                            "sha256": hashlib.sha256(content).hexdigest(),
                            "origin": "real",
                            "filename": path.name,
                            "mime_type": "application/zip",
                            "request_id": request.request_id,
                            "trace_id": current_trace_id(),
                            "manifest": current["manifest"],
                        },
                    ),
                )
                identity = f"{story['story_id']}:{story['version']}"
                version, _ = self.media._latest(connection, anchor, "story_export", identity)
                self.projects._insert_version(
                    connection,
                    project_id=anchor,
                    entity_type="story_export",
                    entity_id=identity,
                    version=version + 1,
                    payload_json=json.dumps(
                        {
                            "artifact_id": artifact_id,
                            "filename": path.name,
                            "story_version": story["version"],
                        }
                    ),
                    created_at=now,
                )
                result = self._finish(
                    connection, request, operation, run_id=run_id, export_artifact_id=artifact_id
                )
            committed = True
            return result
        finally:
            if not committed:
                manager.remove_file(path, file_type="export", if_registered=True)
