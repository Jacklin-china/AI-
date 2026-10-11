"""作品级镜头 Prompt 编译与不可变版本；不调用图片 Provider。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from loguru import logger
from pydantic import ValidationError

from kantoku.config import ToolError
from kantoku.config.observability import current_trace_id
from kantoku.config.runtime_paths import (
    atomic_write_bytes,
    get_runtime_paths,
    validate_runtime_path,
)
from kantoku.core.runtime.models import ArtifactRecord, ArtifactType, utc_now
from kantoku.core.runtime.store import RuntimeStore

from .assets import ComicAssetStore
from .context_declaration import BIBLE_SOURCE_KEYS
from .critic import require_approved_director
from .director_provenance import provenance_context, value_hash
from .models import (
    ComicAsset,
    ComicProjectSnapshot,
    ComicPromptArtifact,
    ComicPromptContext,
    ComicPromptContexts,
    ComicPromptDraft,
    ComicShot,
    ComicStoryboard,
    DirectorSpec,
    ShotStatus,
)
from .projects import ComicContextBuilder, ComicProjectStore
from .storyboards import ComicStoryboardStore

COMPILER_VERSION = "comic-prompt-3"
CONTEXT_VERSION = "image-bibles-1"
BIBLE_CONTRACT = "separated-contexts-1"
PROMPT_SECTIONS = {
    "character_context": "Character Bible / 人物设定",
    "world_context": "World Bible / 世界观设定",
    "style_context": "Style Bible / 视觉风格",
    "shot_context": "Shot Direction / 当前镜头要求",
}


def image_prompt(draft: ComicPromptDraft) -> str:
    """复制和 Provider 使用同一份内容，负向约束不能在提交时丢失。"""
    return draft.positive_prompt + ("\n\nNegative Constraint / 禁止内容\n" + draft.negative_prompt
                                    if draft.negative_prompt else "")


def prompt_fingerprint(draft: ComicPromptDraft) -> str:
    return value_hash({"positive": draft.positive_prompt, "negative": draft.negative_prompt})


def prompt_sections(text: str) -> dict[str, str]:
    """编辑的正文解析回已有 Prompt 修订，不保留与编辑内容不一致的 Bible。"""
    result = {}
    remaining = text
    for name, heading in reversed(PROMPT_SECTIONS.items()):
        if text.count(heading + "\n") != 1:
            raise ToolError("完整 Prompt 标题缺失或重复",
                            detail=f"module=prompt.sections field={name}")
        prefix, separator, body = remaining.rpartition(heading + "\n")
        if not separator or not body.strip():
            raise ToolError("完整 Prompt 缺少内容", detail=f"module=prompt.sections field={name}")
        result[name] = body.strip()
        remaining = prefix.rstrip()
    if remaining.strip():
        raise ToolError("完整 Prompt 标题前存在未归属内容", detail="module=prompt.sections")
    return result


def _asset_context(contexts: ComicPromptContexts, assets: list[ComicAsset]) -> None:
    """固定事实按已有资产字段归属写入；场景/风格光线只进入 Shot。"""
    routes = {
        "character": {"appearance": ("character_context", "appearance"),
                      "outfit": ("character_context", "clothing"),
                      "features": ("character_context", "fixed_features")},
        "scene": {"location": ("world_context", "location"),
                  "time_of_day": ("world_context", "time"),
                  "weather": ("world_context", "environment"),
                  "atmosphere": ("world_context", "environment"),
                  "environment_features": ("world_context", "environment"),
                  "lighting": ("shot_context", "lighting_changes")},
        "style": {"art_direction": ("style_context", "art_style"),
                  "color_language": ("style_context", "color"),
                  "materials": ("style_context", "materials"),
                  "camera_language": ("style_context", "photographic_style"),
                  "lighting": ("shot_context", "lighting_changes")},
    }
    for asset in assets:
        if (asset.details.kind == "character"
                and asset.name not in contexts.character_context.identity):
            contexts.character_context.identity += "；" + asset.name
        for name, value in asset.details.model_dump(exclude={"kind"}).items():
            section, field = routes[asset.details.kind][name]
            if asset.details.kind == "style" and name == "camera_language" and value:
                shot_field = ComicPromptContext.shot_field(value)
                if shot_field:
                    section, field = "shot_context", shot_field
            target = getattr(contexts, section)
            for fact in value if isinstance(value, list) else [value]:
                if fact and fact not in getattr(target, field):
                    setattr(target, field, "；".join(filter(None, [getattr(target, field), fact])))
        # 自由文本约束由模型按语义归属，末尾仍核验原文；不能随意塞入构图。


class BasePromptCompiler(Protocol):
    """模型格式适配边界；编译器不提交图片请求。"""

    version: str

    def compile(
        self, *, snapshot: ComicProjectSnapshot, director: DirectorSpec,
        storyboard: ComicStoryboard, shot: ComicShot, assets: list[ComicAsset],
        model_target: str, model_call: Callable[[list[dict[str, str]]], str],
        allow_advisory: bool = False,
        allow_unavailable: bool = False,
        complete_prompt: bool = False,
        reused_context: dict[str, str] | None = None,
    ) -> ComicPromptDraft: ...


class StructuredImagePromptCompiler:
    """让共享文本模型根据镜头语义组织提示词，不使用固定摄影词映射。"""

    version = COMPILER_VERSION

    def __init__(self, model_guidance: str) -> None:
        self.model_guidance = model_guidance

    @staticmethod
    def _separate_contexts(result: dict) -> tuple[ComicPromptContexts, list[dict]]:
        """仅归位模型已有的完整分句，不创造、删减或猜测固定事实。"""
        parts = {name: dict(result[name]) if isinstance(result[name], dict) else result[name]
                 for name in PROMPT_SECTIONS}
        moves = []
        shot = parts["shot_context"]
        for name in BIBLE_SOURCE_KEYS:
            if not isinstance(parts[name], dict) or not isinstance(shot, dict):
                continue  # 交给下面的严格 Schema 报告类型错误。
            allowed = ComicPromptContexts.model_fields[name].annotation.model_fields
            for field, value in parts[name].items():
                if field not in allowed or not isinstance(value, str) or "\n" in value:
                    continue
                fixed = []
                for clause in filter(None, re.split(r"[，；。]", value)):
                    target = ComicPromptContext.shot_field(clause)
                    if not target:
                        fixed.append(clause)
                        continue
                    existing = shot.get(target, "")
                    if not isinstance(existing, str):
                        continue  # 错误类型不能被归位过程覆盖。
                    if clause not in existing:
                        shot[target] = "；".join(filter(None, [existing, clause]))
                    moves.append({"from": f"{name}.{field}", "to": f"shot_context.{target}",
                                  "value_sha256": value_hash(clause)})
                if any(move["from"] == f"{name}.{field}" for move in moves):
                    parts[name][field] = "，".join(fixed)
        return ComicPromptContexts.model_validate(parts), moves

    def compile(
        self, *, snapshot: ComicProjectSnapshot, director: DirectorSpec,
        storyboard: ComicStoryboard, shot: ComicShot, assets: list[ComicAsset],
        model_target: str, model_call: Callable[[list[dict[str, str]]], str],
        allow_advisory: bool = False,
        allow_unavailable: bool = False,
        complete_prompt: bool = False,
        reused_context: dict[str, str] | None = None,
    ) -> ComicPromptDraft:
        require_approved_director(director, allow_advisory=allow_advisory,
                                  allow_unavailable=allow_unavailable)
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
        director_fields = {
            "visual_direction", "storytelling_goal", "camera_language", "composition",
            "lighting", "color_language", "emotion", "character_focus", "constraints",
            "creative_choices",
        } if director.schema_version == 1 else {
            "creative_decision", "director_plan", "cinematography", "constraints",
        }
        camera_labels = {
            "shot_size": "景别", "camera_angle": "机位", "camera_movement": "摄影机运动",
            "light_source": "主光源", "light_direction": "主光方向",
            "color_relationship": "色彩关系",
        }
        execution_camera = {
            name: getattr(shot, name, "") or getattr(director.cinematography, name, "")
            for name in camera_labels
        }
        if complete_prompt:
            # 读取已有可信字段，不能让 Compiler 修改 DirectorSpec 或重复整份审核 JSON。
            context["stable_context"].pop("director_spec", None)
            context["shot_direction"] = {
                "director_spec": director.model_dump(include=director_fields),
                "storyboard": context["stable_context"].pop("storyboard"),
            }
            context["director_sources"] = provenance_context(director)
            context["reused_bibles"] = {name: text for name, text in (reused_context or {}).items()
                                        if name in PROMPT_SECTIONS}
            context["locked_context_fields"] = {
                name.removeprefix("lock:"): value for name, value in (reused_context or {}).items()
                if name.startswith("lock:")}
            context["execution_camera"] = {name: value for name, value in execution_camera.items()
                                           if value}
        section_instruction = (
            "完整图片模式：只输出 JSON 对象，符合以下 JSON Schema。"
            "只返回 director_summary、character_context、world_context、"
            "style_context、shot_context、negative_prompt 六个字段。四个 Context 必须为以下 Schema"
            "规定的对象，禁止使用自由文本段落，禁止新增字段；另两字段为字符串。"
            "character_context 只包含身份、固定外貌、年龄、服装、固定特征；未知年龄留空。"
            "当前动作、姿态、视线、朝向、景别、机位、镜头/光线方向全部属于 shot_context，"
            "不能混进角色外貌或固定特征。不写本次定稿、无绑定资产等内部说明。"
            "world_context 只包含地点、时间、世界观、环境，不包含主体动作与前中后景构图；"
            "style_context 只包含艺术风格、摄影质感、色彩、材质，不包含本镜头机位或光源方向。"
            "shot_context 只包含动作、姿态、构图、镜头、光影变化。所有字段是一行描述。"
            "未知细节可作一次具体创作选择，不得列候选、伪装为用户事实或覆盖固定资产。"
            "execution_camera 是本镜头已选定的执行要求，正文必须一致，不能再选相反机位或光源。"
            "当前镜头的景别、机位、运动优先使用 current_shot；未指定时沿用导演摄影。"
            "导演的整体风格、色彩与主光逻辑不得改变。reused_bibles 是已保存的相同身份设定，"
            "必须沿用；镜头动作写在 shot_context。把构图落实为主体在画面的位置、朝向和前后景关系，"
            "把光线落实为光源位置、受光面与阴影，把色彩落实为主体与环境的关系。"
            "字段内容使用可直接交给图片模型的自然语言，不写来源 ID、审核术语或泛化质量标签。"
            "所有正文总长不超过2500字，"
            "negative_prompt 写具体禁止内容，不写泛化质量口号。"
            "静态设定与镜头执行必须先分清：stable_context 和固定资产提供事实，"
            "shot_direction 提供导演执行要求，不能原封不动复制到静态三段。"
            "从导演文字提取永久身份、外貌、服装即可，姿态/受光/空间布局必须拆到 shot_context。"
            "逐项检查：从站立远望切换到战斗姿态，这个属性是否还完全成立？"
            "只有不随镜头变化的属性才能放在 Character、World、Style。"
            "前三段每个字段用简洁名词描述，建议60字内，不堆砌导演叙述。"
            "例如外貌只描述发型、面容、体型；服装只描述款式和固有颜色，"
            "不描述衣带飘动、背光边缘或受光肤色；身份不写夜立山巅。"
            "World 的地点只写山巅岩台，环境只写松枝、云海、远山，"
            "不写左下方、右侧中景或画外光源。摄影风格只写质感，"
            "如真实摄影或写实插画，近远景纵深属于 Shot 镜头；未知年龄输出空字符串。"
            + json.dumps(ComicPromptContexts.model_json_schema(), ensure_ascii=False)
        ) if complete_prompt else (
            "只返回 JSON：director_summary、positive_prompt、negative_prompt。")
        messages = [
            {"role": "system", "content": (
                "你是漫剧镜头 Prompt 编译器，不是生图模型。根据当前镜头的叙事目的、"
                "导演方案和固定资产版本组织内容；不要把情绪机械映射到固定机位或光线。"
                "用户硬约束和资产固定约束必须在 positive_prompt 中逐字保留，"
                "不得替换主体。negative_prompt 只写需要避免的具体偏差。"
                + section_instruction +
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
        request_id = f"prompt-request-{uuid4().hex}"
        skill_loaded = any(e.source_type == "skill_method"
                           for item in director.field_provenance.values() for e in item.evidence)
        logger.info("prompt_compiler_started trace_id={} project_id={} shot_id={} request_id={} "
                    "director_skill_loaded={} complete_prompt={}", current_trace_id(),
                    shot.project_id, shot.shot_id, request_id,
                    skill_loaded, complete_prompt)
        raw = model_call(messages)
        try:
            if complete_prompt:
                result = json.loads(raw)
                allowed = {"director_summary", "negative_prompt", *PROMPT_SECTIONS}
                if not isinstance(result, dict) or set(result) - allowed:
                    raise ValueError("unexpected prompt section fields")
                missing = [name for name in PROMPT_SECTIONS
                           if name not in result and name not in (reused_context or {})]
                if not isinstance(result.get("director_summary"), str) \
                        or not result["director_summary"].strip():
                    missing.append("director_summary")
                if missing:
                    raise ToolError("完整 Prompt 编译缺少内容",
                                    detail=f"module=prompt.compiler fields={','.join(missing)}")
                if not isinstance(result.get("negative_prompt"), str) \
                        or not result["negative_prompt"].strip():
                    raise ToolError("完整 Prompt 缺少禁止内容",
                                    detail="module=prompt.compiler field=negative_prompt")
                for name, value in (reused_context or {}).items():
                    if name in PROMPT_SECTIONS:
                        result[name] = ComicPromptContexts.model_fields[name].annotation.from_text(
                            value).model_dump()
                for path, value in context.get("locked_context_fields", {}).items():
                    section, field = path.split(".", 1)
                    if (section not in PROMPT_SECTIONS or field not in
                            ComicPromptContexts.model_fields[section].annotation.model_fields):
                        raise ValueError("invalid locked context field")
                    if not isinstance(result.get(section), dict):
                        raise ValueError("partial context reuse requires structured fields")
                    result[section][field] = value
                contexts, scope_relocations = self._separate_contexts(result)
                _asset_context(contexts, assets)
                if shot.action:
                    contexts.shot_context.action = shot.action
                for name, value in execution_camera.items():
                    if not value:
                        continue
                    if name == "color_relationship":
                        target, field = contexts.style_context, "color"
                    elif name in {"light_source", "light_direction"}:
                        target, field = contexts.shot_context, "lighting_changes"
                    else:
                        target, field = contexts.shot_context, "camera"
                    fact = f"{camera_labels[name]}：{value}"
                    if fact not in getattr(target, field):
                        setattr(target, field, "；".join(
                            filter(None, [getattr(target, field), fact])))
                contexts = ComicPromptContexts.model_validate(contexts.model_dump())
                result.update(contexts.sections())
                # 复用已校验的稳定内容，模型不能换服装/世界/风格；禁止内容同样保持。
                if (reused_context or {}).get("negative_prompt"):
                    result["negative_prompt"] = reused_context["negative_prompt"]
                result["positive_prompt"] = "\n\n".join(
                    heading + "\n" + result[name] for name, heading in PROMPT_SECTIONS.items())
                result.update(context_version=CONTEXT_VERSION, context_sources={
                    "bible_contract": BIBLE_CONTRACT,
                    "structured_contexts": contexts.model_dump(),
                    "scope_relocations": scope_relocations,
                    "source_type": "model_choice", "trust_status": "creative_choice",
                    "model_request_id": request_id,
                    "source_versions": context["source_versions"],
                    "user_facts": {"brief_id": snapshot.creative_brief.brief_id,
                                   "version": snapshot.creative_brief.version,
                                   "sha256": value_hash(snapshot.creative_brief.original_request)},
                    "director_sources": context["director_sources"],
                    "asset_facts": [{"asset_id": asset.asset_id, "version": asset.version,
                                     "sha256": value_hash(asset.details.model_dump())}
                                    for asset in assets],
                    "reused_context": {name: {"sha256": value_hash(text),
                                              "artifact_id": (reused_context or {}).get(
                                                  f"source:{name}")}
                                       for name, text in (reused_context or {}).items()
                                       if name in PROMPT_SECTIONS},
                    "retained_fields": {
                        path: {"sha256": value_hash(value),
                               "source_artifact_id": (reused_context or {}).get(f"source:{path}")}
                        for path, value in context.get("locked_context_fields", {}).items()},
                })
                result["context_sources"]["field_provenance"] = {
                    name: {"source_type": "model_choice", "trust_status": "creative_choice",
                           "value_sha256": value_hash(result[name]),
                           "model_request_id": (
                               None if name in (reused_context or {}) else request_id),
                           "source_artifact_id": (reused_context or {}).get(f"source:{name}")}
                    for name in (*PROMPT_SECTIONS, "negative_prompt", "director_summary")
                }
                for name in (*PROMPT_SECTIONS, "negative_prompt"):
                    if name in (reused_context or {}):
                        previous = json.loads((reused_context or {}).get(
                            f"provenance:{name}", "{}"))
                        if (isinstance(previous, dict)
                                and previous.get("value_sha256") == value_hash(result[name])
                                and previous.get("trust_status") == "creative_choice"
                                and previous.get("source_type") in {"model_choice", "manual_edit"}):
                            result["context_sources"]["field_provenance"][name] = {
                                **previous,
                                "source_artifact_id": (reused_context or {}).get(f"source:{name}"),
                            }
                # 记录可核验的输入依据；知识输入不能证明某句话的因果来源。
                skill_methods = {}
                for provenance in director.field_provenance.values():
                    for evidence in provenance.evidence:
                        if evidence.source_type == "skill_method":
                            method = evidence.model_dump(exclude_none=True)
                            skill_methods[value_hash(method)] = method
                lineage = {
                    "brief": {"id": snapshot.creative_brief.brief_id,
                              "version": snapshot.creative_brief.version,
                              "sha256": value_hash(snapshot.creative_brief.model_dump(
                                  mode="json"))},
                    "assets": [{"id": asset.asset_id, "version": asset.version,
                                "kind": asset.details.kind,
                                "sha256": value_hash(asset.model_dump(mode="json", include={
                                    "name", "details", "fixed_constraints",
                                    "reference_artifact_ids",
                                }))} for asset in assets],
                    "director": {"id": director.spec_id, "version": director.version,
                                 "sha256": value_hash(director.model_dump(
                                     mode="json", include=director_fields))},
                    "shot": {"id": shot.shot_id, "version": shot.version,
                             "storyboard_id": storyboard.storyboard_id,
                             "storyboard_version": storyboard.version,
                             "sha256": value_hash(shot.model_dump(mode="json"))},
                    "skill_methods": list(skill_methods.values()),
                    "skill_evidence_status": "recorded" if skill_methods else "not_recorded",
                    "evidence_semantics": "input_basis_not_causal_attribution",
                    "execution_camera": {
                        name: {"value": value,
                               "field_path": (f"shot.{name}" if getattr(shot, name, "")
                                              else f"director.cinematography.{name}")}
                        for name, value in execution_camera.items() if value
                    },
                }
                result["context_sources"]["input_lineage"] = lineage
                asset_kinds = {"character_context": "character", "world_context": "scene",
                               "style_context": "style"}
                for name, provenance in result["context_sources"]["field_provenance"].items():
                    if name in (reused_context or {}):
                        provenance["input_basis"] = {
                            "source_prompt_artifact_id": (reused_context or {}).get(
                                f"source:{name}"),
                            "sha256": value_hash(result[name]),
                        }
                        continue
                    provenance["input_basis"] = {
                        "brief": lineage["brief"], "director": lineage["director"],
                        "assets": [item for item in lineage["assets"]
                                   if name not in asset_kinds or item["kind"] == asset_kinds[name]],
                        "skill_methods": lineage["skill_methods"],
                        **({"shot": lineage["shot"]} if name in {
                            "shot_context", "negative_prompt", "director_summary"} else {}),
                    }
                draft = ComicPromptDraft.model_validate(result)
            else:
                draft = ComicPromptDraft.model_validate_json(raw)
        except (ValueError, ValidationError) as error:
            field = "context"
            reason = type(error).__name__
            if isinstance(error, ValidationError):
                issue = error.errors(include_input=False, include_context=False)[0]
                field = ".".join(str(part) for part in issue["loc"]) or field
                reason = issue["msg"]
            raise ToolError("Prompt 编译模型未返回有效结构",
                            detail=f"module=prompt.compiler request_id={request_id} "
                            f"field={field} reason={reason}") from error
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
    if normalized == "external":
        return StructuredImagePromptCompiler(
            "为外部图片创作准备完整、可见的主体关系、动作、场景和镜头描述；"
            "不要加入供应商参数，不声明已经生成图片。"
        )
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

    def list(self, project_id: str) -> list[ComicPromptArtifact]:
        self.projects.get(project_id)
        with self.projects._connect() as connection:
            rows = connection.execute("SELECT shot_id FROM comic_prompts WHERE project_id=? "
                                      "ORDER BY created_at", (project_id,)).fetchall()
            return [self._current(connection, row["shot_id"]) for row in rows]

    def reused_context(self, shot: ComicShot, director: DirectorSpec) -> dict[str, str]:
        """相同身份/场景复用现有 Prompt 修订；不从其他项目或角色继承设定。"""
        result = {}
        for prompt in sorted(self.list(shot.project_id),
                             key=lambda item: item.created_at, reverse=True):
            if (prompt.context_version != CONTEXT_VERSION
                    or prompt.context_sources.get("bible_contract") != BIBLE_CONTRACT
                    or prompt.director_spec_version != director.version
                    or prompt.creative_brief_version != director.creative_brief_version):
                continue
            previous_shot = self._prompt_shot(prompt)
            if ("character_context" not in result
                    and prompt.character_asset_versions == shot.character_asset_versions
                    and (bool(shot.character_asset_versions)
                         or previous_shot.subject == shot.subject)):
                result["character_context"] = prompt.character_context
                result["source:character_context"] = prompt.artifact_id
            if ("world_context" not in result and previous_shot.environment == shot.environment
                    and prompt.scene_asset_versions == shot.scene_asset_versions):
                result["world_context"] = prompt.world_context
                result["source:world_context"] = prompt.artifact_id
            if "style_context" not in result and prompt.style_version == shot.style_version:
                result["style_context"] = prompt.style_context
                result["source:style_context"] = prompt.artifact_id
            for name in PROMPT_SECTIONS:
                if result.get(f"source:{name}") == prompt.artifact_id:
                    result[f"provenance:{name}"] = json.dumps(
                        prompt.context_sources.get("field_provenance", {}).get(name, {}),
                        ensure_ascii=False)
            if all(result.get(f"source:{name}") == prompt.artifact_id for name in
                   BIBLE_SOURCE_KEYS):
                result["negative_prompt"] = prompt.negative_prompt
                result["source:negative_prompt"] = prompt.artifact_id
                result["provenance:negative_prompt"] = json.dumps(
                    prompt.context_sources.get("field_provenance", {}).get("negative_prompt", {}),
                    ensure_ascii=False)
            if all(name in result for name in
                   BIBLE_SOURCE_KEYS):
                break
        return result

    def confirmed(self, prompt: ComicPromptArtifact) -> bool:
        # 现有 Core 没有按业务关联查询的公开接口。此兼容投影只读，不建第二套确认状态。
        with self.runtime._connect() as connection:
            rows = connection.execute(
                "SELECT state_json FROM runs WHERE domain='comic' "
                "AND workflow='comic.prompt.confirm' AND status='completed' "
                "AND json_extract(state_json, '$.project_id')=? "
                "AND json_extract(state_json, '$.shot_id')=? "
                "AND json_extract(state_json, '$.prompt_confirmation.artifact_id')=?",
                (prompt.project_id, prompt.shot_id, prompt.artifact_id),
            )
            expected = {"artifact_id": prompt.artifact_id, "version": prompt.version,
                        "sha256": prompt_fingerprint(prompt)}
            return any(json.loads(row["state_json"]).get("prompt_confirmation") == expected
                       for row in rows)

    def _prompt_shot(self, prompt: ComicPromptArtifact) -> ComicShot:
        with self.projects._connect() as connection:
            return ComicShot.model_validate_json(self.projects._version_payload(
                connection, prompt.project_id, "shot", prompt.shot_id, prompt.shot_version))

    def _external_image(
        self, prompt: ComicPromptArtifact, *, content_sha256: str | None = None,
    ) -> ArtifactRecord | None:
        # 在原 Artifact 表按 Prompt 归属筛选；不读取其他项目的图片内容，不写 Core 表。
        query = (
            "SELECT id FROM artifacts WHERE type=? AND source='comic.external_image' "
            "AND status='ready' AND json_extract(metadata_json, '$.project_id')=? "
            "AND json_extract(metadata_json, '$.shot_id')=? "
            "AND json_extract(metadata_json, '$.prompt_artifact_id')=?"
        )
        parameters = [ArtifactType.IMAGE.value, prompt.project_id,
                      prompt.shot_id, prompt.artifact_id]
        if content_sha256 is not None:
            query += " AND json_extract(metadata_json, '$.sha256')=?"
            parameters.append(content_sha256)
        with self.runtime._connect() as connection:
            row = connection.execute(query + " ORDER BY created_at DESC,id DESC LIMIT 1",
                                     parameters).fetchone()
        return self.runtime.get_artifact(row["id"]) if row else None

    def payload(self, prompt: ComicPromptArtifact) -> dict:
        shot = self._prompt_shot(prompt)
        uploaded = self._external_image(prompt)
        return {**prompt.model_dump(mode="json"), "final_prompt": image_prompt(prompt),
                "shot_sequence_number": shot.sequence_number, "shot_subject": shot.subject,
                "user_confirmed": self.confirmed(prompt),
                "external_image": {"artifact_id": uploaded.id, "status": "image_uploaded",
                                   "filename": uploaded.metadata.get("filename"),
                                   "trace_id": uploaded.metadata.get("trace_id")}
                if uploaded else None}

    def import_image(
        self, shot_id: str, *, data_url: str, filename: str, run_id: str,
        conversation_id: str, expected_project_version: int, expected_shot_version: int,
        expected_prompt_version: int, output_dir: Path,
    ) -> dict:
        """只登记用户提供的文件与现有 Artifact，不经过 Provider/预算/图片工作流。"""
        snapshot, director, storyboard, shot, _assets = self.source(shot_id)
        self.projects.require_confirmed_director(director, human_review=True)
        prompt = self.get(shot_id)
        if (snapshot.project.current_version != expected_project_version
                or shot.version != expected_shot_version
                or prompt.version != expected_prompt_version
                or prompt.shot_version != shot.version
                or prompt.storyboard_version != storyboard.version
                or prompt.creative_brief_version != snapshot.creative_brief.version
                or prompt.director_spec_version != director.version
                or prompt.context_version != CONTEXT_VERSION or prompt.model_target != "external"):
            raise ToolError("外部图片关联的 Prompt 或镜头版本已变化")
        try:
            header, encoded = data_url.split(",", 1)
            if header not in {"data:image/png;base64", "data:image/jpeg;base64",
                              "data:image/webp;base64"}:
                raise ValueError("unsupported content type")
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ToolError("外部图片格式不正确", detail="module=external_image.decode") from error
        if not content or len(content) > 5 * 1024 * 1024:
            raise ToolError("外部图片必须在 5 MB 以内", detail="module=external_image.validate")
        signatures = {
            "data:image/png;base64": content.startswith(b"\x89PNG\r\n\x1a\n")
            and len(content) >= 45 and content[-12:] == b"\x00\x00\x00\x00IEND\xaeB`\x82",
            "data:image/jpeg;base64": content.startswith(b"\xff\xd8\xff")
            and content.endswith(b"\xff\xd9") and len(content) >= 32,
            "data:image/webp;base64": content.startswith(b"RIFF") and content[8:12] == b"WEBP"
            and len(content) >= 32 and int.from_bytes(content[4:8], "little") + 8 == len(content),
        }
        if not signatures[header]:
            raise ToolError("文件内容不是有效的 PNG、JPEG 或 WebP 图片",
                            detail="module=external_image.validate")
        digest = hashlib.sha256(content).hexdigest()
        duplicate = self._external_image(prompt, content_sha256=digest)
        if duplicate is None:
            output_dir = validate_runtime_path(output_dir)
            suffix = {"data:image/png;base64": ".png", "data:image/jpeg;base64": ".jpg",
                      "data:image/webp;base64": ".webp"}[header]
            path = output_dir / (uuid4().hex + suffix)
            atomic_write_bytes(path, content)
            duplicate = ArtifactRecord(
                id=f"artifact-{uuid4().hex}", type=ArtifactType.IMAGE,
                run_id=run_id, conversation_id=conversation_id,
                created_at=utc_now(), status="ready",
                node_id="external_image.import", source="comic.external_image", location=str(path),
                metadata={"origin": "real", "creation_mode": "external_upload",
                          "project_id": shot.project_id, "shot_id": shot_id,
                          "shot_version": shot.version, "storyboard_id": shot.storyboard_id,
                          "prompt_artifact_id": prompt.artifact_id,
                          "prompt_version": prompt.version,
                          "filename": Path(filename).name, "sha256": digest,
                          "trace_id": current_trace_id()},
            )
            try:
                with self.projects._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    live = connection.execute(
                        "SELECT current_version FROM comic_projects WHERE project_id=?",
                        (shot.project_id,),
                    ).fetchone()
                    if (live is None or int(live["current_version"]) != expected_project_version
                            or self._current(connection, shot_id).artifact_id != prompt.artifact_id
                            or self.storyboards._shot(connection, shot_id).version != shot.version):
                        raise ToolError("上传期间 Prompt 或镜头已变化，请刷新后重新上传")
                    self.runtime.insert_artifact(connection, duplicate)
            except Exception:
                # 仅清理本次创建的随机文件；不触碰用户原图或任何历史 Artifact。
                get_runtime_paths().remove_file(path, file_type="artifact")
                raise
        logger.info("COMIC_EXTERNAL_IMAGE_IMPORTED trace_id={} project_id={} shot_id={} "
                    "prompt_artifact_id={} artifact_id={} status=image_uploaded",
                    current_trace_id(), shot.project_id, shot_id, prompt.artifact_id, duplicate.id)
        return {"artifact_id": duplicate.id, "artifact_type": "image", "shot_id": shot_id,
                "storyboard_id": shot.storyboard_id, "version": prompt.version,
                "media_preparation": {"image_mode": "external", "status": "image_uploaded",
                                      "shot_id": shot_id, "prompt_artifact_id": prompt.artifact_id,
                                      "image_artifact_id": duplicate.id}}

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
        self.storyboards.get_shot(shot_id)
        with self.projects._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM comic_prompts WHERE shot_id=?", (shot_id,),
            ).fetchone() is None:
                return []
        current = self.get(shot_id)
        with self.projects._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM comic_entity_versions WHERE project_id=? "
                "AND entity_type='prompt' AND entity_id=? ORDER BY version DESC",
                (current.project_id, current.prompt_id),
            ).fetchall()
        return [ComicPromptArtifact.model_validate_json(row["payload_json"]) for row in rows]

    def delete_version(
        self, shot_id: str, *, version: int, expected_project_version: int,
    ) -> dict[str, Any]:
        """删除单个历史 Prompt 版本。

        与 restore 同源：都只操作 comic_entity_versions 中 prompt 实体的行。
        两条硬约束：
          ① 当前正在使用的版本不可删（删了会让 comic_prompts.current_version 悬空）；
          ② 删除也追加一次作品修订，历史链上不会出现"无记录的空档"。
        """
        shot = self.storyboards.get_shot(shot_id)
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT current_version FROM comic_projects WHERE project_id=?",
                (shot.project_id,),
            ).fetchone()
            if row is None or int(row["current_version"]) != expected_project_version:
                raise ToolError("作品已由其他操作更新，请刷新后重试")
            current = connection.execute(
                "SELECT prompt_id,current_version FROM comic_prompts WHERE shot_id=?",
                (shot_id,),
            ).fetchone()
            if current is None:
                raise ToolError("当前镜头尚无 Prompt 资产")
            prompt_id = str(current["prompt_id"])
            if int(current["current_version"]) == version:
                raise ToolError(
                    "当前正在使用的 Prompt 版本不能删除",
                    detail="请先把其他版本恢复为新版本，再删除这一版",
                )
            existing = connection.execute(
                "SELECT 1 FROM comic_entity_versions WHERE project_id=? "
                "AND entity_type='prompt' AND entity_id=? AND version=?",
                (shot.project_id, prompt_id, version),
            ).fetchone()
            if existing is None:
                raise ToolError("找不到指定 Prompt 版本")
            connection.execute(
                "DELETE FROM comic_entity_versions WHERE project_id=? "
                "AND entity_type='prompt' AND entity_id=? AND version=?",
                (shot.project_id, prompt_id, version),
            )
            project_version = self.projects._advance_project(
                connection, shot.project_id, expected_project_version,
            )
        logger.info(
            "comic prompt version deleted project_id={} shot_id={} prompt_id={} "
            "deleted_version={} project_version={}",
            shot.project_id, shot_id, prompt_id, version, project_version,
        )
        return {
            "shot_id": shot_id, "prompt_id": prompt_id,
            "deleted_version": version, "project_version": project_version,
        }

    def save(
        self, shot_id: str, draft: ComicPromptDraft, *, expected_project_version: int,
        expected_shot_version: int, model_target: str, compiler_version: str,
        run_id: str, source: str = "compiled", expected_version: int | None = None,
        restored_from_version: int | None = None,
    ) -> ComicPromptArtifact:
        # 外部模型调用在事务外；事务内再次验证所有来源版本，避免保存过时结果。
        snapshot, director, storyboard, shot, assets = self.source(shot_id)
        previous = self.get(shot_id) if source in {"edited", "restored"} else None
        if (previous and previous.context_sources.get("bible_contract") == BIBLE_CONTRACT
                and draft.context_version != CONTEXT_VERSION):
            raise ToolError("不能将结构化 Prompt 降级为未分段正文",
                            detail="module=prompt.sections schema=separated-contexts-1")
        if draft.context_version == CONTEXT_VERSION:
            draft = draft.model_copy(update=prompt_sections(draft.positive_prompt))
            if not draft.negative_prompt.strip():
                raise ToolError("完整 Prompt 缺少禁止内容", detail="module=prompt.sections")
            inherited_sources = (previous.context_sources if source == "edited" and previous
                                 else draft.context_sources)
            contexts = None
            if (inherited_sources.get("bible_contract") == BIBLE_CONTRACT
                    or (previous and previous.context_sources.get("bible_contract")
                        == BIBLE_CONTRACT)):
                try:
                    contexts = ComicPromptContexts.from_sections({
                        name: getattr(draft, name) for name in PROMPT_SECTIONS})
                    fixed = contexts.model_copy(deep=True)
                    _asset_context(fixed, assets)
                    if fixed != contexts:
                        raise ValueError("固定资产字段必须保留在对应 Context 字段")
                except (ValueError, ValidationError) as error:
                    raise ToolError(
                        "Prompt Context 字段不合法，请将动作和摄影信息放入 Shot Context",
                        detail="module=prompt.sections schema=separated-contexts-1",
                    ) from error
                sections = contexts.sections()
                draft = draft.model_copy(update={**sections, "positive_prompt": "\n\n".join(
                    heading + "\n" + sections[name] for name, heading in PROMPT_SECTIONS.items())})
            # 固定资产不会因编辑自由文本而变成另一个角色/世界/风格。
            facts = [value for asset in assets
                     for value in asset.details.model_dump(exclude={"kind"}).values() if value]
            if any(item not in draft.positive_prompt for value in facts
                   for item in (value if isinstance(value, list) else [value])):
                raise ToolError("完整 Prompt 遗漏固定资产信息", detail="module=prompt.sections")
            if source in {"edited", "restored"}:
                draft.context_sources = {
                    **(previous.context_sources if source == "edited" else draft.context_sources),
                    "source_type": "manual_edit", "run_id": run_id,
                    "sha256": prompt_fingerprint(draft),
                    "parent_artifact_id": previous.artifact_id,
                    "restored_from_version": restored_from_version,
                }
                fields = dict(draft.context_sources.get("field_provenance", {}))
                for name in (*PROMPT_SECTIONS, "negative_prompt", "director_summary"):
                    if source == "restored" or getattr(previous, name) != getattr(draft, name):
                        fields[name] = {
                            **fields.get(name, {}),
                            "source_type": "manual_edit", "trust_status": "creative_choice",
                            "value_sha256": value_hash(getattr(draft, name)), "run_id": run_id,
                            "model_request_id": None,
                            "source_artifact_id": previous.artifact_id,
                        }
                draft.context_sources["field_provenance"] = fields
            if contexts is not None:
                draft.context_sources = {**draft.context_sources,
                                         "bible_contract": BIBLE_CONTRACT,
                                         "structured_contexts": contexts.model_dump()}
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
                    "final_prompt": image_prompt(draft),
                    "context_version": draft.context_version,
                    "context_sources": draft.context_sources,
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
        logger.info("prompt_artifact_created trace_id={} project_id={} shot_id={} "
                    "artifact_id={} prompt_version={}", current_trace_id(), shot.project_id,
                    shot_id, artifact_id, version)
        return prompt
