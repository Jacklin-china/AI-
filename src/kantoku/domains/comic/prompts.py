"""作品级镜头 Prompt 编译与不可变版本；不调用图片 Provider。"""

from __future__ import annotations

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
from kantoku.core.conversation_context import SourceRef
from kantoku.core.runtime.models import ArtifactRecord, ArtifactType, utc_now
from kantoku.core.runtime.store import RuntimeStore

from .assets import ComicAssetStore
from .constraints import (
    CONSTRAINT_CONTRACT,
    image_edit_constraint_basis,
    verify_prompt_constraints,
)
from .context_declaration import BIBLE_SOURCE_KEYS
from .critic import require_approved_director
from .director_provenance import provenance_context, value_hash
from .media import ComicMediaStore, decode_image
from .models import (
    ComicAsset,
    ComicProjectSnapshot,
    ComicPromptArtifact,
    ComicPromptContext,
    ComicPromptContexts,
    ComicPromptDraft,
    ComicShot,
    ComicStoryboard,
    CompleteImagePromptOutput,
    CreativeConstraint,
    DirectorSpec,
    ImageEditDraft,
    ImageVersionRef,
    ShotStatus,
)
from .projects import ComicContextBuilder, ComicProjectStore
from .storyboards import ComicStoryboardStore

COMPILER_VERSION = "comic-prompt-5"
CONTEXT_VERSION = "image-bibles-1"
BIBLE_CONTRACT = "separated-contexts-1"
SEMANTIC_BIBLE_CONTRACT = "semantic-contexts-2"
PROMPT_SECTIONS = {
    "character_context": "Character Bible / 人物设定",
    "world_context": "World Bible / 世界观设定",
    "style_context": "Style Bible / 视觉风格",
    "shot_context": "Shot Direction / 当前镜头要求",
}


def image_prompt(draft: ComicPromptDraft) -> str:
    """复制和 Provider 使用同一份内容，负向约束不能在提交时丢失。"""
    continuity = draft.context_sources.get("image_references", {}).get("continuity")
    reference = ""
    if continuity:
        image = continuity["image"]
        changes = "；".join(dict.fromkeys(item["quote"] for item in continuity["changes"]))
        reference = (
            f"请附上已采用的参考图片：{image['filename']}（v{image['version']}，"
            f"SHA-256 {image['sha256']}）。文件名、版本和指纹仅用于 Kantoku 来源追踪，"
            "不是图片地址或检索条件。外部应用可能重命名附件或转为 PNG，"
            "请以本条消息实际附带的参考图为视觉基准，保留未被本轮明确修改的"
            "主体身份与数量、外貌、服装、道具、场景结构、风格、构图和摄影。"
            "下文是完整目标画面；不得将未改元素重新设计。"
            + (f"本轮允许的变化：{changes}。" if changes else "本轮未授权改变既有视觉元素。")
            + "没有附上这张参考图时，先要求提供参考图，不要仅凭文字重新猜画面。\n\n")
    return reference + draft.positive_prompt + (
        "\n\nNegative Constraint / 禁止内容\n" + draft.negative_prompt
        if draft.negative_prompt else "")


def prompt_fingerprint(draft: ComicPromptDraft) -> str:
    return value_hash({"positive": draft.positive_prompt, "negative": draft.negative_prompt})


def prompt_sections(text: str, *, allow_empty: bool = False) -> dict[str, str]:
    """编辑的正文解析回已有 Prompt 修订，不保留与编辑内容不一致的 Bible。"""
    pattern = (r"(?m)^(" + "|".join(re.escape(value)
               for value in PROMPT_SECTIONS.values()) + r")\r?$")
    headings = list(re.finditer(pattern, text))
    if [item[1] for item in headings] != list(PROMPT_SECTIONS.values()):
        raise ToolError("完整 Prompt 标题缺失、重复或顺序不正确",
                        detail="module=prompt.sections field=headings")
    if text[:headings[0].start()].strip():
        raise ToolError("完整 Prompt 标题前存在未归属内容", detail="module=prompt.sections")
    result = {}
    for index, name in enumerate(PROMPT_SECTIONS):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[headings[index].end():end].strip()
        if not body and not allow_empty:
            raise ToolError("完整 Prompt 缺少内容", detail=f"module=prompt.sections field={name}")
        result[name] = body
    return result


def _asset_context(contexts: ComicPromptContexts, assets: list[ComicAsset], *,
                   semantic_scope: bool = False) -> None:
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
                shot_field = "camera" if semantic_scope else ComicPromptContext.shot_field(value)
                if shot_field:
                    section, field = "shot_context", shot_field
            target = getattr(contexts, section)
            for fact in value if isinstance(value, list) else [value]:
                if fact and fact not in getattr(target, field):
                    setattr(target, field, "；".join(filter(None, [getattr(target, field), fact])))
        # 自由文本约束由模型按语义归属，末尾仍核验原文；不能随意塞入构图。


def prompt_constraints(snapshot: ComicProjectSnapshot,
                       assets: list[ComicAsset]) -> list[CreativeConstraint]:
    result = list(snapshot.creative_brief.semantic_constraints)
    for asset in assets:
        for requirement in asset.fixed_constraints:
            result.append(CreativeConstraint(
                constraint_id="constraint-" + value_hash({"asset": asset.asset_id,
                    "version": asset.version, "requirement": requirement})[:24],
                requirement=requirement, target=asset.name,
                scope={"character": "identity", "scene": "world", "style": "style"}[
                    asset.details.kind],
                source_ref=SourceRef(id=f"asset:{asset.project_id}:{asset.asset_id}:{asset.version}",
                                     kind="resource", version=asset.version),
                source_quote=requirement, origin="asset_fact", trust="source_verified"))
    return result


# 用户要求按 scope 落到既有段落字段；这里只做定位，不新增合约字段。
CONSTRAINT_SECTION_FIELDS: dict[str, tuple[str, tuple[tuple[str, str, int], ...]]] = {
    "identity": ("character_context", (
        ("appearance", "外貌", 600), ("clothing", "服装", 600),
        ("fixed_features", "固定特征", 600), ("identity", "身份", 300))),
    "world": ("world_context", (
        ("environment", "环境", 1000), ("location", "地点", 600),
        ("world_setting", "世界观", 600), ("time", "时间", 300))),
    "style": ("style_context", (
        ("art_style", "艺术风格", 600), ("photographic_style", "摄影风格", 600),
        ("materials", "材质", 600), ("color", "色彩", 600))),
    "shot": ("shot_context", (
        ("pose", "姿态", 600), ("action", "动作", 600), ("camera", "镜头", 600),
        ("composition", "构图", 600), ("lighting_changes", "光影变化", 1000))),
    "output": ("shot_context", (
        ("action", "动作", 600), ("composition", "构图", 600), ("camera", "镜头", 600))),
}
# 表情与神态在既有约定中由 Shot 的“姿态”承载，与 execution_emotion 投影保持一致。
EXPRESSION_TERMS = re.compile(r"表情|神态|目光|眼神|情绪|神情|爱意")
SECTION_MAX_CHARS = 2000
# 正文未承载用户要求：语义覆盖缺失或原样文字缺失，都属于可补齐的编译遗漏。
CONSTRAINT_GAP_PREFIXES = ("module=comic.constraints stage=coverage",
                           "module=comic.constraints stage=exact")


def constraint_gap_ids(error: ToolError) -> list[str]:
    """从公开核验结论取回未被正文承载的要求编号，不解析自由文本。"""
    ids = list(getattr(error, "review_uncovered", None) or [])
    for issue in getattr(error, "review_issues", None) or []:
        for constraint_id in issue.get("requirement_ids") or []:
            if constraint_id not in ids:
                ids.append(constraint_id)
    return ids


def _append_section_field(text: str, label: str, addition: str, limit: int) -> str | None:
    """在既有字段行追加一句要求；字段已满或超长时返回 None 交给下一个字段。"""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        name, separator, value = line.partition("：")
        if not separator or name != label:
            continue
        room = limit - len(value.strip()) - 1
        if room <= 0:
            return None
        merged = "；".join(filter(None, [value.strip(), addition[:room]]))
        lines[index] = f"{name}：{merged}"
        return "\n".join(lines)
    candidate = f"{label}：{addition[:limit]}"
    return "\n".join([*lines, candidate]) if len(candidate) <= limit else None


def project_constraint_requirements(
    draft: ComicPromptDraft, constraints: list[CreativeConstraint],
    constraint_ids: list[str], *, complete_prompt: bool,
) -> list[dict[str, Any]]:
    """把核验确认未被正文承载的用户要求写入对应字段。

    只增补缺项，不改写已通过的句子，也不重新设计角色；写入位置沿用既有字段合约。
    """
    targets = {item.constraint_id: item for item in constraints}
    projected: list[dict[str, Any]] = []
    for constraint_id in constraint_ids:
        item = targets.get(constraint_id)
        if item is None or item.status != "active" or item.origin == "model_choice":
            continue
        text = (item.literal_value if item.verification == "exact" and item.literal_value
                else item.requirement)
        text = (text or "").strip()
        if not text:
            continue
        if not complete_prompt:
            if text in draft.positive_prompt:
                continue
            merged = "；".join(filter(None, [draft.positive_prompt.strip(), text]))
            if len(merged) > 4000:
                continue
            draft.positive_prompt = merged
            projected.append({"constraint_id": constraint_id, "field": "positive_prompt"})
            continue
        if any(text in getattr(draft, name) for name in PROMPT_SECTIONS):
            continue
        section, fields = CONSTRAINT_SECTION_FIELDS.get(
            item.scope, CONSTRAINT_SECTION_FIELDS["shot"])
        addition = (f"神态与情绪：{text}" if EXPRESSION_TERMS.search(f"{item.target}{text}")
                    else text)
        current = getattr(draft, section)
        for _name, label, limit in fields:
            updated = _append_section_field(current, label, addition, limit)
            if updated is None or len(updated) > SECTION_MAX_CHARS:
                continue
            setattr(draft, section, updated)
            projected.append({"constraint_id": constraint_id, "field": f"{section}.{label}"})
            break
    if projected and complete_prompt:
        # 保存时以 positive_prompt 为准重建四段，补齐正文后必须同步渲染结果。
        draft.positive_prompt = "\n\n".join(
            heading + "\n" + getattr(draft, name) for name, heading in PROMPT_SECTIONS.items())
    return projected


class BasePromptCompiler(Protocol):
    """模型格式适配边界；编译器不提交图片请求。"""

    version: str

    def compile_edit(self, *, scope: dict, constraints: list[CreativeConstraint],
                     model_call: Callable[[list[dict]], str],
                     on_diagnostics: Callable[[dict], None] | None = None
                     ) -> tuple[ImageEditDraft, dict]: ...

    def compile(
        self, *, snapshot: ComicProjectSnapshot, director: DirectorSpec,
        storyboard: ComicStoryboard, shot: ComicShot, assets: list[ComicAsset],
        model_target: str, model_call: Callable[[list[dict[str, str]]], str],
        allow_advisory: bool = False,
        allow_unavailable: bool = False,
        complete_prompt: bool = False,
        reused_context: dict[str, str] | None = None,
        on_constraint_diagnostics: Callable[[dict], None] | None = None,
    ) -> ComicPromptDraft: ...


class StructuredImagePromptCompiler:
    """让共享文本模型根据镜头语义组织提示词，不使用固定摄影词映射。"""

    version = COMPILER_VERSION

    def __init__(self, model_guidance: str) -> None:
        self.model_guidance = model_guidance

    def compile_edit(self, *, scope: dict, constraints: list[CreativeConstraint],
                     model_call: Callable[[list[dict]], str],
                     on_diagnostics: Callable[[dict], None] | None = None
                     ) -> tuple[ImageEditDraft, dict]:
        """原编译器的编辑模式；不伪造镜头，不接入内部生图。"""
        if len(constraints) > 50:
            raise ToolError("改图必需约束超过核验容量，请缩小范围")
        messages = [{"role": "system", "content":
            "为外部图像编辑模型编写可执行修改指令。只应用选中的发现项和用户本次修改原文；"
            "观察是待核对资料，不是用户事实。保留有效约束和没有授权修改的内容；"
            "不引入无依据人物、风格、摄影或情节，不声称图片已修改。"
            "edit_scope 为 regions 时，只在列出的归一化左上/右下坐标内修改，"
            "使用各区域 label 标注的编号及各区要求，保留区域外内容；whole 明确写整图调整。"
            "mixed 同时有局部和整图授权：只有 whole_finding_ids 对应建议可以整图修改，"
            "其余建议和各区要求仍严格绑定区域；整图调整不扩大局部要求的范围。"
            "user_instruction 的范围由 instruction_target 指定：whole 为整图，"
            "selected_regions 为本轮全部区域，region 只作用于指定 region_id；"
            "缺少该字段的历史指令沿用原整图或局部模式。不要添加未授权的改变。"
            "指定文字必须原样。输出正向修改指令、负向约束和公开摘要。"
            "authorized_requirements 是唯一的本轮改变清单，逐项写出改变和作用位置，"
            "不能将要求相近的不同目标合并丢失。constraints 是冻结的有效保护规则，"
            "enforcement=scoped_replacement 时，effective_requirement 是相应对象上的新要求；"
            "original_requirement 仅说明旧来源，不是继续生成旧对象的要求。"
            "不要在正向或负向无条件要求保留这个被明确替换的旧对象/属性。"
            "enforcement=preserve 时仍保护原要求；current_requirement 按 authorized_targets "
            "的范围执行。原对象其他属性及范围外内容仍保持。"
            "不能因旧方案另有目标就改动未授权区域，也不能宣称原图已符合未核实的目标。"
            "historical_constraint_ids 是原方案的约束；明确的本轮人工修改可在授权对象和"
            "范围内替代同一属性的旧用户要求，不同时要求生成新旧两种互斥对象。"
            "局部修改不撤回其他区域、其他对象或未变属性的保护，资产固定事实仍不可更改。"
            "current_constraint_targets 将本轮提取要求绑定到人工原文所在授权清单；"
            "这些要求的范围以关联 requirement_id 的 target 为准，不扩展到整图。"
            "inherited_constraint_replacements 是当前图片版本已确认的目标替代记录，"
            "未被本轮人工再次修改时保护相应新要求，不重新生成旧对象。历史坐标只是来源，"
            "不能当作当前图片的新选区；记录也不证明外部图片已经达成要求。"
            "AI 报告的保留建议不属于本轮冻结硬约束，不能自行加入负向禁止。"
            "只勾选 AI 建议不足以解除旧用户硬要求；语义不明确或冲突不能静默扩大范围。"
            + self.model_guidance + "只返回 JSON："
            + json.dumps(ImageEditDraft.model_json_schema(), ensure_ascii=False)},
            {"role": "user", "content": json.dumps({"mode": "edit",
                **{key: value for key, value in scope.items() if key not in {"protected_features",
                                                                           "task"}},
                "image_task_context": {"task": scope["task"], "use": "context_only"},
                "constraints": image_edit_constraint_basis(scope, constraints)},
                ensure_ascii=False)}]
        try:
            draft = ImageEditDraft.model_validate_json(model_call(messages))
        except (ValueError, ValidationError):
            raise ToolError("改图编译未返回有效结构",
                            detail="module=prompt.compiler mode=edit") from None
        receipt = verify_prompt_constraints(constraints,
            sections={"positive_prompt": draft.positive_prompt},
            negative_prompt=draft.negative_prompt, model_call=model_call, edit_scope=scope,
            on_diagnostics=on_diagnostics)
        return draft, receipt

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

    @staticmethod
    def _constraint_sections(draft: ComicPromptDraft, complete_prompt: bool) -> dict[str, str]:
        return ({name: getattr(draft, name) for name in PROMPT_SECTIONS}
                if complete_prompt else {"positive_prompt": draft.positive_prompt})

    def _verify_constraints(
        self, draft: ComicPromptDraft, constraints: list[CreativeConstraint], *,
        complete_prompt: bool, model_call: Callable[[list[dict[str, str]]], str],
        on_diagnostics: Callable[[dict], None] | None, final_attempt: bool,
    ) -> dict:
        """核验最终正文；末次仍缺项时先补齐正文，再独立复核，不把创作判为失败。"""
        sections = self._constraint_sections(draft, complete_prompt)
        try:
            return verify_prompt_constraints(
                constraints, sections=sections, negative_prompt=draft.negative_prompt,
                model_call=model_call, on_diagnostics=on_diagnostics)
        except ToolError as error:
            uncovered = constraint_gap_ids(error)
            if (not final_attempt or not uncovered
                    or not (error.detail or "").startswith(CONSTRAINT_GAP_PREFIXES)):
                raise
            # 模型已重编一次仍遗漏：服务端把要求写进对应字段，正文必须真实承载它。
            projected = project_constraint_requirements(
                draft, constraints, uncovered, complete_prompt=complete_prompt)
            logger.warning(
                "prompt_constraint_projection trace_id={} uncovered={} projected={}",
                current_trace_id(), uncovered, [item["field"] for item in projected])
            if on_diagnostics:
                on_diagnostics({
                    "contract": CONSTRAINT_CONTRACT, "stage": "requirement_projection",
                    "projected": projected, "uncovered": uncovered,
                    "sections": self._constraint_sections(draft, complete_prompt),
                    "negative_prompt": draft.negative_prompt,
                })
            # 正文已按 scope 补齐；独立复核仍存疑时以 advisory 如实交付，不中断创作。
            return verify_prompt_constraints(
                constraints, sections=self._constraint_sections(draft, complete_prompt),
                negative_prompt=draft.negative_prompt, model_call=model_call,
                on_diagnostics=on_diagnostics, allow_unverified=True)

    def compile(
        self, *, snapshot: ComicProjectSnapshot, director: DirectorSpec,
        storyboard: ComicStoryboard, shot: ComicShot, assets: list[ComicAsset],
        model_target: str, model_call: Callable[[list[dict[str, str]]], str],
        allow_advisory: bool = False,
        allow_unavailable: bool = False,
        complete_prompt: bool = False,
        reused_context: dict[str, str] | None = None,
        on_constraint_diagnostics: Callable[[dict], None] | None = None,
        repair: dict[str, Any] | None = None,
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
        if director.reference_context:
            context["image_references"] = director.reference_context
        required = list(dict.fromkeys([
            *snapshot.creative_brief.hard_constraints, *director.constraints,
            *(item for asset in assets for item in asset.fixed_constraints),
        ]))
        semantic_scope = snapshot.creative_brief.constraint_contract_version == 2
        constraints = prompt_constraints(snapshot, assets) if semantic_scope else []
        literal_constraints = [item for item in constraints if item.status == "active"
                               and item.verification == "exact"]
        literal_text = "；".join(f"{item.target}的文字为“{item.literal_value}”"
                                for item in literal_constraints)
        if literal_constraints:
            context["literal_text_requirements"] = [item.model_dump(mode="json", include={
                "target", "literal_value", "source_ref"}) for item in literal_constraints]
        context["required_constraints"] = ([item.model_dump(mode="json") for item in constraints
                                           if item.status == "active"]
                                          if semantic_scope else required)
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
        # 本镜头的表现优先于导演总体表现；表情属于当前 Shot，不能混入固定人物设定。
        emotion_source = ("shot.emotion" if shot.emotion else
                          "director.director_plan.character_expression")
        execution_emotion = shot.emotion or (
            director.director_plan.character_expression if director.director_plan else None)
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
            if execution_emotion:
                context["execution_emotion"] = execution_emotion
        if repair is not None:
            # 重编只针对已确认的缺项；失败候选不是已通过内容，不能直接沿用。
            context["content_repair"] = {
                "candidate": repair.get("candidate"), "issues": repair.get("issues") or [],
                "sources": repair.get("sources") or {}}
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
            "literal_text_requirements 的指定文字由服务端原样填入 shot_context.literal_text，"
            "此字段留空，不在其他字段改写这些文字；其可见位置围绕 target 对象安排。"
            "未知细节可作一次具体创作选择，不得列候选、伪装为用户事实或覆盖固定资产。"
            "execution_camera 是本镜头已选定的执行要求，正文必须一致，不能再选相反机位或光源。"
            "execution_emotion 是本镜头已选定的神态与情绪，必须在 shot_context.pose 中落实，"
            "不能只写在摘要或负向正文中；表情与目光不属于固定外貌。"
            "当前镜头的景别、机位、运动优先使用 current_shot；未指定时沿用导演摄影。"
            "image_references.continuity 如存在，表示用户已采用的视觉基准；changes 是本轮"
            "明确授权的叶子变化。locked_context_fields 必须原样保留，包括空值；不能用新"
            "导演、分镜建议或修复覆盖。无文字描述的图像细节沿用参考图，不自行补造。"
            "导演的整体风格、色彩与主光逻辑不得改变。reused_bibles 是已保存的相同身份设定，"
            "必须沿用；镜头动作写在 shot_context。把构图落实为主体在画面的位置、朝向和前后景关系，"
            "把光线落实为光源位置、受光面与阴影，把色彩落实为主体与环境的关系。"
            "字段内容使用可直接交给图片模型的自然语言，不写来源 ID、审核术语或泛化质量标签。"
            "所有正文总长不超过2500字，"
            "negative_prompt 写具体禁止内容，不写泛化质量口号。"
            "负向正文不能禁止用户必需的内容或指定文字。"
            "静态设定与镜头执行必须先分清：stable_context 和固定资产提供事实，"
            "shot_direction 提供导演执行要求，不能原封不动复制到静态三段。"
            "从导演文字提取永久身份、外貌、服装即可，姿态/受光/空间布局必须拆到 shot_context。"
            "逐项检查：当本轮动作或拍摄方式变化时，这个属性是否仍成立？"
            "只有不随镜头变化的属性才能放在 Character、World、Style。"
            "前三段每个字段用简洁名词描述，建议60字内，不堆砌导演叙述。"
            "外貌只描述持续的发型、面容、体型，服装只描述款式和固有颜色，"
            "不混入当轮动态和受光变化；身份不混入姿态。"
            "World 地点和环境描述场景本身，不混入本镜头的画面位置或画外光源。"
            "摄影风格只写质感，空间布局和纵深属于 Shot；未知年龄输出空字符串。"
            + ("没有人物或某段不适用时使用空字段，不要为了填字段虚构人物或场景。"
               if semantic_scope else "人物身份不能为空。")
            + json.dumps(CompleteImagePromptOutput.model_json_schema(), ensure_ascii=False)
        ) if complete_prompt else (
            "只返回 JSON：director_summary、positive_prompt、negative_prompt。")
        messages = [
            {"role": "system", "content": (
                "你是漫剧镜头 Prompt 编译器，不是生图模型。根据当前镜头的叙事目的、"
                "导演方案和固定资产版本组织内容；不要把情绪机械映射到固定机位或光线。"
                + ("required_constraints 是有来源的语义要求。只覆盖 active 要求；"
                   "保持对象、关系、范围和否定，普通视觉要求允许准确改写。"
                   "只有 verification=exact 的 literal_value 必须完整原样写入正向正文。"
                   "scope=shot 且涉及表情、神态、目光或情绪的要求必须写进 "
                   "shot_context.pose 的“神态与情绪：”，不能只写动作、摘要或负向正文。"
                   "不把委托措辞、来源 ID 或模型选择冒充用户硬约束。"
                   if semantic_scope else
                   "required_constraints 是去重后的不可变原文。每项必须完整、逐字保留在"
                   "四段 Context 的相应字段正文中，不能只放在摘要或 negative_prompt，"
                   "也不能拆分成多个字段或换成近义表达；用户原话同样保留。"
                   "系统由这些字段组成最终正向 Prompt，无需返回 positive_prompt 字段。"
                   if complete_prompt else
                   "用户硬约束和资产固定约束必须在 positive_prompt 中逐字保留。") +
                "不得替换主体。negative_prompt 只写需要避免的具体偏差。"
                + section_instruction +
                f"目标模型注意事项：{self.model_guidance}"
                + ("content_repair 是上一次已通过结构校验但遗漏本轮要求的候选正文。"
                   "按 issues 指出的缺项在对应 scope 字段补齐，保留其余已成立的身份、"
                   "世界、风格与镜头设定，不重新设计角色，也不删改已覆盖的内容。"
                   if repair is not None else "")
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
                            value, semantic_scope=semantic_scope).model_dump()
                for path, value in context.get("locked_context_fields", {}).items():
                    section, field = path.split(".", 1)
                    if (section not in PROMPT_SECTIONS or field not in
                            ComicPromptContexts.model_fields[section].annotation.model_fields):
                        raise ValueError("invalid locked context field")
                    if not isinstance(result.get(section), dict):
                        raise ValueError("partial context reuse requires structured fields")
                    result[section][field] = value
                if semantic_scope:
                    contexts = ComicPromptContexts.model_validate(
                        {name: result[name] for name in PROMPT_SECTIONS},
                        context={"semantic_scope": True})
                    scope_relocations = []
                else:
                    contexts, scope_relocations = self._separate_contexts(result)
                _asset_context(contexts, assets, semantic_scope=semantic_scope)
                if literal_constraints:
                    contexts.shot_context.literal_text = literal_text
                if shot.action:
                    contexts.shot_context.action = shot.action
                if execution_emotion:
                    emotion = "；".join(execution_emotion.splitlines())
                    pose = contexts.shot_context.pose
                    # 短表现直接投影，避免模型漏掉已经确认的本轮表情；长说明仍由
                    # 编译器凝练并接受下方语义核验，不截断来源或突破原字段容量。
                    projected = "；".join(filter(None, [pose, f"神态与情绪：{emotion}"]))
                    if emotion not in pose and len(projected) <= 600:
                        contexts.shot_context.pose = projected
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
                # 最后投影锁定字段，防止 Shot/摄影和约束补全前的普通组装覆盖图片基准。
                for path, value in context.get("locked_context_fields", {}).items():
                    section, field = path.split(".", 1)
                    setattr(getattr(contexts, section), field, value)
                contexts = ComicPromptContexts.model_validate(contexts.model_dump(),
                    context={"semantic_scope": semantic_scope})
                result.update(contexts.sections())
                # 复用已校验的稳定内容，模型不能换服装/世界/风格；禁止内容同样保持。
                if (reused_context or {}).get("negative_prompt"):
                    result["negative_prompt"] = reused_context["negative_prompt"]
                result["positive_prompt"] = "\n\n".join(
                    heading + "\n" + result[name] for name, heading in PROMPT_SECTIONS.items())
                result.update(context_version=CONTEXT_VERSION, context_sources={
                    "bible_contract": SEMANTIC_BIBLE_CONTRACT if semantic_scope else BIBLE_CONTRACT,
                    "structured_contexts": contexts.model_dump(exclude={
                        "shot_context": {"literal_text"}}
                        if not semantic_scope and not contexts.shot_context.literal_text else None),
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
                if execution_emotion:
                    lineage["execution_emotion"] = {
                        "value": execution_emotion, "field_path": emotion_source,
                        "target_field": "shot_context.pose",
                    }
                if literal_constraints:
                    lineage["literal_text"] = [{"constraint_id": item.constraint_id,
                        "source_ref": item.source_ref.model_dump(mode="json"),
                        "value_sha256": value_hash(item.literal_value), "target": item.target}
                        for item in literal_constraints]
                result["context_sources"]["input_lineage"] = lineage
                if director.reference_context:
                    result["context_sources"]["image_references"] = director.reference_context
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
        if semantic_scope:
            if literal_constraints and not complete_prompt:
                draft.positive_prompt += "\n指定文字：" + literal_text
                if len(draft.positive_prompt) > 4000:
                    raise ToolError("必需文字超过 Prompt 容量，请缩小范围",
                                    detail="module=comic.constraints stage=capacity")
            try:
                draft.context_sources["constraint_review"] = self._verify_constraints(
                    draft, constraints, complete_prompt=complete_prompt,
                    model_call=model_call, on_diagnostics=on_constraint_diagnostics,
                    # 单段正文没有四段结构可重编，直接走服务端补齐。
                    final_attempt=repair is not None or not complete_prompt,
                )
            except ToolError as error:
                # 只交回已通过 Schema 的公开草稿作一次内容修复；不能带入非法输出。
                error.prompt_candidate = draft.model_dump(mode="json", include={
                    "director_summary", "negative_prompt", *PROMPT_SECTIONS})
                uncovered = constraint_gap_ids(error)
                if (repair is not None or not uncovered or not complete_prompt
                        or not (error.detail or "").startswith(CONSTRAINT_GAP_PREFIXES)):
                    raise
                logger.warning(
                    "prompt_content_repair_started trace_id={} project_id={} shot_id={} "
                    "uncovered={}", current_trace_id(), snapshot.project.project_id,
                    shot.shot_id, uncovered)
                # 首次正文遗漏用户要求：带公开核验结论重编一次，把要求写进对应段落。
                return self.compile(
                    snapshot=snapshot, director=director, storyboard=storyboard, shot=shot,
                    assets=assets, model_target=model_target, model_call=model_call,
                    allow_advisory=allow_advisory, allow_unavailable=allow_unavailable,
                    complete_prompt=complete_prompt, reused_context=reused_context,
                    on_constraint_diagnostics=on_constraint_diagnostics,
                    repair={"reason": error.message, "candidate": error.prompt_candidate,
                            "issues": getattr(error, "review_issues", []),
                            "sources": getattr(error, "review_sources", {})})
        missing = [str(index) for index, item in enumerate(required)
                   if item not in draft.positive_prompt]
        if missing and not semantic_scope:
            raise ToolError("Prompt 编译遗漏不可变约束",
                            detail=f"module=prompt.compiler request_id={request_id} "
                            "field=positive_prompt reason=missing_immutable_constraints "
                            f"missing_count={len(missing)} missing_indices={','.join(missing)}")
        if complete_prompt and director.reference_context.get("continuity"):
            final = ComicPromptContexts.from_sections({key: getattr(draft, key)
                for key in PROMPT_SECTIONS}, semantic_scope=semantic_scope)
            for path, value in context.get("locked_context_fields", {}).items():
                section, field = path.split(".", 1)
                if getattr(getattr(final, section), field) != value:
                    raise ToolError("本轮要求与已采用图片的保留字段冲突，请明确需要修改的元素",
                                    detail=f"module=comic.continuity field={path}")
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
        self.media = ComicMediaStore(projects, runtime)
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
                    or prompt.context_sources.get("bible_contract") != (
                        SEMANTIC_BIBLE_CONTRACT if self.projects.get(
                            shot.project_id).creative_brief.constraint_contract_version == 2
                        else BIBLE_CONTRACT)
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
            "AND status='ready' AND lifecycle_state='active' "
            "AND json_extract(metadata_json, '$.project_id')=? "
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
                "reference_attachments": [
                    *prompt.context_sources.get("image_references", {}).get("bindings", []),
                    *([{"image": {key: continuity["image"][key] for key in ("image_id", "version")},
                        "artifact_id": continuity["image"]["artifact_id"],
                        "source_project_id": continuity["image"]["project_id"],
                        "sha256": continuity["image"]["sha256"],
                        "filename": continuity["image"]["filename"],
                        "roles": ["identity", "style", "composition"],
                        "target": "已采用画面的未修改元素"}]
                      if (continuity := prompt.context_sources.get(
                          "image_references", {}).get("continuity")) else [])],
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
        content, _mime, suffix = decode_image(data_url)
        digest = hashlib.sha256(content).hexdigest()
        duplicate = self._external_image(prompt, content_sha256=digest)
        created = duplicate is None
        if duplicate is not None:
            existing_path = validate_runtime_path(Path(duplicate.location or ""))
            if not existing_path.is_file():
                raise ToolError("已保存图片的原文件缺失，请在图片工作台重传缺失原文件",
                                detail="module=external_image.import source=content_missing")
            if hashlib.sha256(existing_path.read_bytes()).hexdigest() != digest:
                raise ToolError("已保存图片的原文件已改变，不能作为重复上传返回",
                                detail="module=external_image.import source=content_changed")
        if duplicate is None:
            output_dir = validate_runtime_path(output_dir)
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
        return {"artifact_id": duplicate.id, "artifact_type": "image", "created": created,
                "shot_id": shot_id,
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
        with self.projects._connect() as connection:
            row = connection.execute("SELECT version,payload_json FROM comic_entity_versions "
                "WHERE project_id=? AND entity_type='image_references' "
                "ORDER BY version DESC LIMIT 1", (shot.project_id,)).fetchone()
        if row:
            reference_snapshot = json.loads(row["payload_json"])
            bindings = reference_snapshot["bindings"]
            expected = director.reference_context.get("reference_version", 0)
            if (bindings or director.reference_context) and expected != int(row["version"]):
                raise ToolError("参考用途已变化，请重新生成导演方案")
            for binding in director.reference_context.get("bindings", []):
                image = self.media.require_image(ImageVersionRef.model_validate(binding["image"]),
                    shot.project_id, reference_snapshot["conversation_id"], allow_related=True)
                if (binding.get("artifact_id") != image.artifact_id
                        or binding.get("source_project_id", image.project_id) != image.project_id
                        or binding.get("sha256") != image.sha256):
                    raise ToolError("导演参考图片的来源不一致")
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
        constraint_model_call: Callable[[list[dict[str, str]]], str] | None = None,
    ) -> ComicPromptArtifact:
        # 外部模型调用在事务外；事务内再次验证所有来源版本，避免保存过时结果。
        snapshot, director, storyboard, shot, assets = self.source(shot_id)
        semantic_scope = snapshot.creative_brief.constraint_contract_version == 2
        previous = self.get(shot_id) if source in {"edited", "restored"} else None
        if (previous and previous.context_sources.get("bible_contract") in {
                BIBLE_CONTRACT, SEMANTIC_BIBLE_CONTRACT}
                and draft.context_version != CONTEXT_VERSION):
            raise ToolError("不能将结构化 Prompt 降级为未分段正文",
                            detail="module=prompt.sections schema=separated-contexts-1")
        if draft.context_version == CONTEXT_VERSION:
            draft = draft.model_copy(update=prompt_sections(draft.positive_prompt,
                                                          allow_empty=semantic_scope))
            if not draft.negative_prompt.strip():
                raise ToolError("完整 Prompt 缺少禁止内容", detail="module=prompt.sections")
            inherited_sources = (previous.context_sources if source == "edited" and previous
                                 else draft.context_sources)
            contexts = None
            if (inherited_sources.get("bible_contract") in {BIBLE_CONTRACT, SEMANTIC_BIBLE_CONTRACT}
                    or (previous and previous.context_sources.get("bible_contract")
                        in {BIBLE_CONTRACT, SEMANTIC_BIBLE_CONTRACT})):
                try:
                    contexts = ComicPromptContexts.from_sections({
                        name: getattr(draft, name) for name in PROMPT_SECTIONS},
                        semantic_scope=semantic_scope)
                    fixed = contexts.model_copy(deep=True)
                    _asset_context(fixed, assets, semantic_scope=semantic_scope)
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
                                         "bible_contract": (SEMANTIC_BIBLE_CONTRACT
                                                            if semantic_scope else BIBLE_CONTRACT),
                                         "structured_contexts": contexts.model_dump(exclude={
                                             "shot_context": {"literal_text"}} if not semantic_scope
                                             and not contexts.shot_context.literal_text else None)}
        required = [*snapshot.creative_brief.hard_constraints, *director.constraints]
        for asset in assets:
            required.extend(asset.fixed_constraints)
        if semantic_scope:
            # 编辑请求中的凭据不可信；只复用数据库中的上一版，正文变更则重新审核。
            receipt = (previous.context_sources.get("constraint_review") if previous
                       else draft.context_sources.get("constraint_review") if source == "compiled"
                       else None)
            draft.context_sources["constraint_review"] = verify_prompt_constraints(
                prompt_constraints(snapshot, assets),
                sections=({name: getattr(draft, name) for name in PROMPT_SECTIONS}
                          if draft.context_version else {"positive_prompt": draft.positive_prompt}),
                negative_prompt=draft.negative_prompt, model_call=constraint_model_call,
                receipt=receipt,
                # 同一正文已如实标注 advisory 时不再阻断保存；结论随凭据保留。
                allow_unverified=bool((receipt or {}).get("delivery") == "advisory"))
        elif any(item not in draft.positive_prompt for item in required):
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
