"""DirectorSpec 字段依据；复用现有版本表和 Run，不另建来源系统。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .cinematography import PLAN_EXECUTION_FIELDS, unresolved_execution
from .models import (
    ComicAsset,
    ComicProjectSnapshot,
    DirectorEvidence,
    DirectorFieldProvenance,
    DirectorSource,
    DirectorSpecDraft,
)

PROJECTIONS = {
    "visual_direction": "director_plan.visual_focus",
    "storytelling_goal": "creative_decision.intent_summary",
    "composition": "director_plan.composition_strategy",
    "lighting": "cinematography.lighting",
    "color_language": "director_plan.color_strategy",
    "emotion": "creative_decision.emotional_target",
    "character_focus": "director_plan.visual_focus",
    "constraints": "creative_decision.hard_constraints",
    "creative_choices": "director_plan.creative_choices",
    "camera_language": "cinematography.camera_language",
}
SECTIONS = ("creative_decision", "director_plan", "cinematography")


def value_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def field_values(spec: DirectorSpecDraft) -> dict[str, Any]:
    data = spec.model_dump(mode="json")
    if spec.schema_version == 1:
        return {name: data[name] for name in PROJECTIONS}
    return {f"{section}.{name}": value
            for section in SECTIONS for name, value in (data[section] or {}).items()
            if name not in {"status", "unresolved_decisions"}}


def project_decision_fields(
    spec: DirectorSpecDraft, previous: DirectorSpecDraft | None = None,
) -> DirectorSpecDraft:
    """编辑分层决策后同步原有兼容字段；只投影，不生成新决定。"""
    if spec.schema_version != 2:
        return spec
    data = spec.model_dump(mode="json")
    values = field_values(spec)
    for alias, path in PROJECTIONS.items():
        value = values[path]
        if value is not None and (alias != "creative_choices" or value):
            data[alias] = value
    data["character_focus"] = (
        spec.director_plan.character_presence or spec.director_plan.visual_focus
    )
    prior_summary = previous.field_provenance.get("cinematography.camera_language") \
        if previous else None
    if (not spec.cinematography.camera_language or prior_summary and prior_summary.derived_from
            and spec.cinematography.camera_language == previous.cinematography.camera_language):
        data["camera_language"] = "；".join(filter(None, (
            spec.cinematography.shot_size, spec.cinematography.camera_angle,
            spec.cinematography.spatial_feel,
        ))) or "摄影方案待修订"
    return DirectorSpecDraft.model_validate(data)


def project_provenance(spec: DirectorSpecDraft) -> DirectorSpecDraft:
    """验证值绑定；兼容投影指向同一来源，不要求模型再次声明来源。"""
    values = field_values(spec)
    provenance = {path: item for path, item in spec.field_provenance.items()
                  if path in values and item.value_sha256 == value_hash(values[path])}
    # 原有摄影摘要可由三个真实参数投影；记录派生关系，不能丢失来源或假报模型原文。
    summary_path = "cinematography.camera_language"
    components = ("shot_size", "camera_angle", "spatial_feel")
    if spec.cinematography:
        summary = "；".join(filter(None, (
            getattr(spec.cinematography, name) for name in components
        )))
        paths = [f"cinematography.{name}" for name in components]
        if values.get(summary_path) == summary and all(path in provenance for path in paths):
            evidence = {item.model_dump_json(): item for path in paths
                        for item in provenance[path].evidence}
            prior = provenance.get(summary_path)
            if prior:
                evidence.update({item.model_dump_json(): item for item in prior.evidence})
                provenance[summary_path] = prior.model_copy(update={
                    "derived_from": ",".join(paths), "evidence": list(evidence.values()),
                })
            else:
                provenance[summary_path] = DirectorFieldProvenance(
                    source_type="model_choice", value_sha256=value_hash(summary),
                    trust_status="creative_choice" if all(
                        provenance[path].trust_status != "unresolved" for path in paths
                    ) else "unresolved",
                    evidence=list(evidence.values()), derived_from=",".join(paths),
                )
    if spec.cinematography:
        for name in spec.cinematography.unresolved_decisions:
            path = f"cinematography.{name}"
            if path in provenance:
                provenance[path] = provenance[path].model_copy(
                    update={"trust_status": "unresolved"},
                )
    if spec.execution_policy == "single_image" and spec.director_plan:
        for name in unresolved_execution(spec.director_plan.model_dump(), PLAN_EXECUTION_FIELDS):
            path = f"director_plan.{name}"
            if path in provenance:
                provenance[path] = provenance[path].model_copy(
                    update={"trust_status": "unresolved"},
                )
    if spec.schema_version == 2:
        data = spec.model_dump(mode="json")
        projections = dict(PROJECTIONS)
        if spec.director_plan.character_presence:
            projections["character_focus"] = "director_plan.character_presence"
        for alias, path in projections.items():
            if path in provenance and data[alias] == values[path]:
                provenance[alias] = provenance[path].model_copy(update={"derived_from": path})
    return spec.model_copy(update={"field_provenance": provenance})


def provenance_context(spec: DirectorSpecDraft) -> dict[str, Any]:
    """字段映射到去重后的只读依据，避免模型输入重复整份来源记录。"""
    sources: dict[str, Any] = {}
    fields = {}
    for path, item in project_provenance(spec).field_provenance.items():
        if spec.schema_version == 2 and "." not in path:
            continue
        source = item.model_dump(mode="json", include={"source_type", "trust_status", "evidence"})
        if item.derived_from:
            source["derived_from"] = item.derived_from
        encoded = json.dumps(source, sort_keys=True, ensure_ascii=False)
        source_id = hashlib.sha256(encoded.encode()).hexdigest()[:16]
        sources[source_id] = source
        fields[path] = source_id
    return {"field_provenance": fields, "provenance_sources": sources}


def stage_provenance(
    section: str, values: Mapping[str, Any], *, snapshot: ComicProjectSnapshot,
    assets: list[ComicAsset], knowledge: Mapping[str, Any] | None,
    model_requests: list[str], run_id: str, manual: bool = False,
) -> dict[str, DirectorFieldProvenance]:
    brief = snapshot.creative_brief
    context_evidence = [DirectorEvidence(
        source_type="user_fact", reference=brief.brief_id, version=brief.version,
        field_path="original_request", sha256=value_hash(brief.original_request),
    )]
    # 输入关联不是因果归属；固定资产摘要被模型解释后仍是 model_choice。
    context_evidence.extend(DirectorEvidence(
        source_type="asset_fact", reference=asset.asset_id, version=asset.version,
        sha256=value_hash(asset.details.model_dump(mode="json")),
    ) for asset in assets)
    if knowledge:
        context_evidence.append(DirectorEvidence(
            source_type="skill_method", reference=knowledge["skill_id"],
            sha256=knowledge["sha256"],
        ))
    request_evidence = [DirectorEvidence(source_type="model_choice", reference=item)
                        for item in dict.fromkeys(model_requests)]
    if manual:
        request_evidence = [DirectorEvidence(source_type="manual_edit", reference=run_id)]
    result = {}
    for name, value in values.items():
        if name in {"status", "unresolved_decisions"}:
            continue
        path = f"{section}.{name}"
        source: DirectorSource = "manual_edit" if manual else "model_choice"
        trust = "creative_choice" if request_evidence else "unresolved"
        evidence = [*context_evidence, *request_evidence]
        if (not manual and section == "creative_decision"
                and name in {"hard_constraints", "soft_preferences", "creative_freedom"}
                and value == getattr(brief, name)):
            source, trust = "user_fact", "confirmed_fact"
            evidence = [DirectorEvidence(source_type=source, reference=brief.brief_id,
                                         field_path=name, version=brief.version)]
        elif (not manual and path == "creative_decision.intent_summary"
              and value == brief.original_request):
            source, trust, evidence = "user_fact", "confirmed_fact", context_evidence[:1]
        # 仅明确的同义字段逐字复制才能提升为资产事实，不能通过关键词猜事实。
        asset_fields = {
            "director_plan.color_strategy": ("style", "color_language"),
            "cinematography.color_relationship": ("style", "color_language"),
            "cinematography.camera_language": ("style", "camera_language"),
            "cinematography.lighting": ("scene", "lighting"),
        }
        for asset in assets if not manual else []:
            match = asset_fields.get(path)
            if (match and asset.details.kind == match[0]
                    and value == getattr(asset.details, match[1])):
                source, trust = "asset_fact", "confirmed_fact"
                evidence = [DirectorEvidence(
                    source_type=source, reference=asset.asset_id, version=asset.version,
                    field_path=f"details.{match[1]}", sha256=value_hash(value),
                )]
                break
        if value is None or name in values.get("unresolved_decisions", []):
            trust = "unresolved"
        result[path] = DirectorFieldProvenance(
            source_type=source, trust_status=trust, value_sha256=value_hash(value),
            evidence=evidence,
        )
    return result


def record_changes(
    previous: DirectorSpecDraft | None, current: DirectorSpecDraft, *,
    source: DirectorSource, evidence: DirectorEvidence,
) -> DirectorSpecDraft:
    """服务端比较值；忽略客户端/模型自报来源，保留未变字段的真实依据。"""
    old_values = field_values(previous) if previous else {}
    old = project_provenance(previous).field_provenance if previous else {}
    result = {}
    for path, value in field_values(current).items():
        prior = old.get(path)
        if path in old_values and old_values[path] == value:
            if prior:
                result[path] = prior
            # 历史字段没有依据时不能事后补造来源。
            continue
        result[path] = DirectorFieldProvenance(
            source_type=source, trust_status="unresolved" if value is None else "creative_choice",
            value_sha256=value_hash(value), evidence=[evidence],
            previous_source=prior.source_type if prior else None,
            previous_value_sha256=value_hash(old_values[path]) if path in old_values else None,
        )
    return project_provenance(current.model_copy(update={"field_provenance": result}))
