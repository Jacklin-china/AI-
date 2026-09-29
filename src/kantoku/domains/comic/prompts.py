"""作品级镜头 Prompt 编译与不可变版本；不调用图片 Provider。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from typing import Protocol
from uuid import uuid4

from loguru import logger
from pydantic import ValidationError

from kantoku.config import ToolError
from kantoku.config.observability import current_trace_id
from kantoku.core.runtime.models import ArtifactRecord, ArtifactType, utc_now
from kantoku.core.runtime.store import RuntimeStore

from .assets import ComicAssetStore
from .critic import require_approved_director
from .models import (
    ComicAsset,
    ComicProjectSnapshot,
    ComicPromptArtifact,
    ComicPromptDraft,
    ComicShot,
    ComicStoryboard,
    DirectorSpec,
    ShotStatus,
)
from .projects import ComicContextBuilder, ComicProjectStore
from .storyboards import ComicStoryboardStore

COMPILER_VERSION = "comic-prompt-1"


class BasePromptCompiler(Protocol):
    """模型格式适配边界；编译器不提交图片请求。"""

    version: str

    def compile(
        self, *, snapshot: ComicProjectSnapshot, director: DirectorSpec,
        storyboard: ComicStoryboard, shot: ComicShot, assets: list[ComicAsset],
        model_target: str, model_call: Callable[[list[dict[str, str]]], str],
    ) -> ComicPromptDraft: ...


class StructuredImagePromptCompiler:
    """让共享文本模型根据镜头语义组织提示词，不使用固定摄影词映射。"""

    version = COMPILER_VERSION

    def __init__(self, model_guidance: str) -> None:
        self.model_guidance = model_guidance

    def compile(
        self, *, snapshot: ComicProjectSnapshot, director: DirectorSpec,
        storyboard: ComicStoryboard, shot: ComicShot, assets: list[ComicAsset],
        model_target: str, model_call: Callable[[list[dict[str, str]]], str],
    ) -> ComicPromptDraft:
        require_approved_director(director)
        if director.schema_version == 2 and any(
            director.asset_versions.get(f"asset:{asset.asset_id}") != asset.version
            for asset in assets
        ):
            raise ToolError("Prompt 资产版本与已审核导演方案不一致")
        # 复用作品 Context Builder；仅把当前分镜/镜头加入有界视图。
        selected = ComicContextBuilder.build(
            snapshot, task=shot.purpose, director=director, assets=assets,
        )
        context = selected.model_dump()
        context["stable_context"]["storyboard"] = storyboard.model_dump(include={
            "title", "description",
        })
        context["current_shot"] = shot.model_dump(include={
                "purpose", "subject", "action", "environment", "emotion",
                "shot_size", "camera_angle", "camera_movement",
        })
        context["source_versions"].update({
            "storyboard": storyboard.version, "shot": shot.version,
        })
        context["model_target"] = model_target
        messages = [
            {"role": "system", "content": (
                "你是漫剧镜头 Prompt 编译器，不是生图模型。根据当前镜头的叙事目的、"
                "导演方案和固定资产版本组织内容；不要把情绪机械映射到固定机位或光线。"
                "用户硬约束和资产固定约束必须在 positive_prompt 中逐字保留，"
                "不得替换主体。negative_prompt 只写需要避免的具体偏差。"
                "只返回 JSON：director_summary、positive_prompt、negative_prompt。"
                f"目标模型注意事项：{self.model_guidance}"
            )},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ]
        logger.info(
            "comic prompt compile context project_id={} shot_id={} brief_version={} "
            "director_version={} asset_versions={} compiler={} model={} input_chars={}",
            snapshot.project.project_id, shot.shot_id, snapshot.creative_brief.version,
            director.version, [(item.asset_id, item.version) for item in assets],
            self.version, model_target, len(messages[1]["content"]),
        )
        raw = model_call(messages)
        try:
            draft = ComicPromptDraft.model_validate_json(raw)
        except (ValueError, ValidationError) as error:
            raise ToolError("Prompt 编译模型未返回有效结构", detail=type(error).__name__) from error
        required = [*snapshot.creative_brief.hard_constraints, *director.constraints]
        for asset in assets:
            required.extend(asset.fixed_constraints)
        missing = [item for item in required if item not in draft.positive_prompt]
        if missing:
            raise ToolError("Prompt 编译遗漏不可变约束", detail=f"missing_count={len(missing)}")
        logger.info(
            "comic prompt compiled project_id={} shot_id={} output_chars={}",
            snapshot.project.project_id, shot.shot_id, len(raw),
        )
        return draft


def compiler_for_model(model_target: str) -> BasePromptCompiler:
    """仅决定文本组织策略；模型名称由 settings.yaml 传入。"""
    normalized = model_target.lower()
    if normalized.startswith("qwen-image"):
        return StructuredImagePromptCompiler(
            "以明确的视觉主体、动作、环境与可见细节表达；保留中文专名。"
        )
    if normalized.startswith("seedream"):
        return StructuredImagePromptCompiler(
            "清晰描述主体关系和视觉目标，避免不必要的冗长质量词。"
        )
    raise ToolError("当前图片模型尚无 Prompt 编译适配", detail=model_target)


class ComicPromptStore:
    def __init__(
        self, projects: ComicProjectStore, storyboards: ComicStoryboardStore,
        assets: ComicAssetStore, runtime: RuntimeStore,
    ) -> None:
        self.projects = projects
        self.storyboards = storyboards
        self.assets = assets
        self.runtime = runtime
        self.projects.runtime_store = runtime

    def source(self, shot_id: str) -> tuple[
        ComicProjectSnapshot, DirectorSpec, ComicStoryboard, ComicShot, list[ComicAsset]
    ]:
        shot = self.storyboards.get_shot(shot_id)
        storyboard = self.storyboards.get(shot.storyboard_id)
        snapshot = self.projects.get(shot.project_id)
        if shot.status == ShotStatus.DELETED or shot_id not in storyboard.shot_ids:
            raise ToolError("镜头已删除或不在当前分镜中")
        if (snapshot.project.director_version != storyboard.director_spec_version
                or snapshot.project.director_id is None):
            raise ToolError("导演方案已变化，请先创建当前方案的分镜")
        director = self.projects.get_director(shot.project_id)
        require_approved_director(director)
        self.projects.require_confirmed_director(director)
        refs = [*shot.character_asset_versions, *shot.scene_asset_versions]
        if shot.style_version is not None:
            refs.append(shot.style_version)
        assets = []
        for ref in refs:
            current = self.assets.get(shot.project_id, ref.asset_id)
            pinned = self.assets.get(shot.project_id, ref.asset_id, version=ref.version)
            if current.state != "active" or pinned.state != "active":
                raise ToolError("镜头引用的资产已删除", detail=ref.asset_id)
            assets.append(pinned)
        if director.schema_version == 2 and any(
            director.asset_versions.get(f"asset:{asset.asset_id}") != asset.version
            for asset in assets
        ):
            raise ToolError("Prompt 资产版本与已审核导演方案不一致")
        return snapshot, director, storyboard, shot, assets

    def _current(self, connection: sqlite3.Connection, shot_id: str) -> ComicPromptArtifact:
        row = connection.execute(
            "SELECT prompt_id,project_id,current_version FROM comic_prompts WHERE shot_id=?",
            (shot_id,),
        ).fetchone()
        if row is None:
            raise ToolError("当前镜头尚无 Prompt")
        return ComicPromptArtifact.model_validate_json(self.projects._version_payload(
            connection, row["project_id"], "prompt", row["prompt_id"],
            int(row["current_version"]),
        ))

    def get(self, shot_id: str, *, version: int | None = None) -> ComicPromptArtifact:
        with self.projects._connect() as connection:
            current = self._current(connection, shot_id)
            if version is None:
                return current
            return ComicPromptArtifact.model_validate_json(self.projects._version_payload(
                connection, current.project_id, "prompt", current.prompt_id, version,
            ))

    def versions(self, shot_id: str) -> list[ComicPromptArtifact]:
        current = self.get(shot_id)
        with self.projects._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM comic_entity_versions WHERE project_id=? "
                "AND entity_type='prompt' AND entity_id=? ORDER BY version DESC",
                (current.project_id, current.prompt_id),
            ).fetchall()
        return [ComicPromptArtifact.model_validate_json(row["payload_json"]) for row in rows]

    def save(
        self, shot_id: str, draft: ComicPromptDraft, *, expected_project_version: int,
        expected_shot_version: int, model_target: str, compiler_version: str,
        run_id: str, source: str = "compiled", expected_version: int | None = None,
        restored_from_version: int | None = None,
    ) -> ComicPromptArtifact:
        # 外部模型调用在事务外；事务内再次验证所有来源版本，避免保存过时结果。
        snapshot, director, storyboard, shot, assets = self.source(shot_id)
        required = [*snapshot.creative_brief.hard_constraints, *director.constraints]
        for asset in assets:
            required.extend(asset.fixed_constraints)
        if any(item not in draft.positive_prompt for item in required):
            raise ToolError("Prompt 缺少当前作品的不可变约束")
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT current_version FROM comic_projects WHERE project_id=?",
                (shot.project_id,),
            ).fetchone()
            if row is None or int(row["current_version"]) != expected_project_version:
                raise ToolError("作品已由其他操作更新，请刷新后重试")
            live_shot = self.storyboards._shot(connection, shot_id)
            if live_shot.version != expected_shot_version or shot.version != live_shot.version:
                raise ToolError("镜头版本已变化，请重新编译 Prompt")
            existing = connection.execute(
                "SELECT prompt_id,current_version FROM comic_prompts WHERE shot_id=?",
                (shot_id,),
            ).fetchone()
            if existing is None:
                if expected_version is not None:
                    raise ToolError("当前镜头尚无可编辑 Prompt")
                prompt_id, version = f"comic-prompt-{uuid4().hex}", 1
            else:
                prompt_id = str(existing["prompt_id"])
                version = int(existing["current_version"]) + 1
                if expected_version is not None and expected_version != version - 1:
                    raise ToolError("Prompt 已由其他操作更新，请刷新后重试")
            now = utc_now()
            artifact_id = f"artifact-{uuid4().hex}"
            prompt = ComicPromptArtifact(
                **draft.model_dump(), prompt_id=prompt_id, artifact_id=artifact_id,
                project_id=shot.project_id, storyboard_id=shot.storyboard_id, shot_id=shot_id,
                version=version, project_version=expected_project_version + 1,
                creative_brief_version=snapshot.creative_brief.version,
                director_spec_version=director.version, storyboard_version=storyboard.version,
                shot_version=shot.version,
                character_asset_versions=shot.character_asset_versions,
                scene_asset_versions=shot.scene_asset_versions, style_version=shot.style_version,
                original_intent=snapshot.creative_brief.original_request,
                model_target=model_target, compiler_version=compiler_version,
                created_at=now, source=source, restored_from_version=restored_from_version,
            )
            digest = hashlib.sha256(
                json.dumps(draft.model_dump(), ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            artifact = ArtifactRecord(
                id=artifact_id, type=ArtifactType.PROMPT, run_id=run_id,
                node_id="prompt.compile", source="comic.prompt.compiler", status="ready",
                created_at=now, version=version,
                metadata={
                    "prompt_id": prompt_id, "project_id": shot.project_id,
                    "trace_id": current_trace_id(),
                    "storyboard_id": shot.storyboard_id, "shot_id": shot_id,
                    "positive_prompt": draft.positive_prompt,
                    "negative_prompt": draft.negative_prompt,
                    "director_summary": draft.director_summary,
                    "sha256": digest, "model_target": model_target,
                    "compiler_version": compiler_version,
                    "creative_brief_version": snapshot.creative_brief.version,
                    "director_spec_version": director.version,
                    "storyboard_version": storyboard.version, "shot_version": shot.version,
                    "character_asset_versions": [
                        r.model_dump() for r in shot.character_asset_versions
                    ],
                    "scene_asset_versions": [r.model_dump() for r in shot.scene_asset_versions],
                    "style_version": (
                        shot.style_version.model_dump() if shot.style_version else None
                    ),
                },
            )
            self.projects._advance_project(connection, shot.project_id, expected_project_version)
            if existing is None:
                connection.execute(
                    "INSERT INTO comic_prompts "
                    "(prompt_id,project_id,storyboard_id,shot_id,current_version,"
                    "created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (prompt_id, shot.project_id, shot.storyboard_id, shot_id, version,
                     now.isoformat(), now.isoformat()),
                )
            else:
                connection.execute(
                    "UPDATE comic_prompts SET current_version=?,updated_at=? WHERE prompt_id=?",
                    (version, now.isoformat(), prompt_id),
                )
            self.projects._insert_version(
                connection, project_id=shot.project_id, entity_type="prompt",
                entity_id=prompt_id, version=version, payload_json=prompt.model_dump_json(),
                created_at=now,
            )
            self.runtime.insert_artifact(connection, artifact)
        logger.info(
            "comic prompt saved project_id={} shot_id={} prompt_id={} prompt_version={} "
            "artifact_id={} compiler={} model={} asset_versions={} input_chars={} "
            "output_chars={}",
            shot.project_id, shot_id, prompt_id, version, artifact_id, compiler_version,
            model_target, [(a.asset_id, a.version) for a in assets],
            len(prompt.original_intent), len(draft.positive_prompt) + len(draft.negative_prompt),
        )
        return prompt
