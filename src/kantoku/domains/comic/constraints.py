"""Comic 语义约束：原文核验留在 Memory，归一化和输出审核留在 Domain。"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kantoku.config import ToolError
from kantoku.config.observability import current_trace_id
from kantoku.core.conversation_context import MemoryFact, SourceRef

from .director_provenance import value_hash
from .models import ComicPromptContexts, CreativeConstraint

ModelCall = Callable[[list[dict[str, str]]], str]
CONSTRAINT_CONTRACT = "creative-constraints-2"
logger = logging.getLogger(__name__)


class ConstraintSource(BaseModel):
    """入口已保存的当前原文；不放宽 Core 摘录事实的长度和核验规则。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    value: str = Field(min_length=1, max_length=4000)
    source: SourceRef
    origin: Literal["user_request"] = "user_request"
    status: Literal["active", "superseded", "withdrawn"] = "active"


class ConstraintProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_index: int = Field(ge=0)
    source_quote: str = Field(min_length=1, max_length=4000)
    requirement: str | None = Field(default=None, min_length=1, max_length=300)
    target: str = Field(min_length=1, max_length=300)
    scope: Literal["identity", "world", "style", "shot", "output"]
    verification: Literal["semantic", "exact"] = "semantic"
    literal_value: str | None = Field(default=None, min_length=1, max_length=300)


class ConstraintProposals(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    constraints: list[ConstraintProposal] = Field(max_length=50)


class CoverageItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    constraint_id: str
    status: Literal["covered", "missing", "conflict", "uncertain"]
    quote: str = Field(default="", max_length=4000)
    evidence_path: str | None = Field(default=None, max_length=200)
    reason: str = Field(default="", max_length=500)


class CoverageReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    complete: bool
    scope_valid: bool
    findings: list[CoverageItem] = Field(max_length=50)
    summary: str = Field(default="", max_length=1000)


class EditScopeIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    requirement_ids: list[str] = Field(default_factory=list, max_length=101)
    reason: str = Field(min_length=1, max_length=500)


class EditConstraintCoverageItem(CoverageItem):
    status: Literal["covered", "missing", "conflict", "uncertain", "superseded"]


class EditConstraintReplacement(BaseModel):
    """本轮局部替代的公开依据；不撤回项目或资产中的原始约束。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    constraint_id: str
    requirement_id: str
    source_quote: str = Field(min_length=1, max_length=4000)
    reason: str = Field(min_length=1, max_length=500)


class ImageEditExtractionReview(CoverageReview):
    constraint_replacements: list[EditConstraintReplacement] = Field(max_length=50)
    scope_issues: list[EditScopeIssue] = Field(default_factory=list, max_length=101)


class ImageEditCoverageReview(CoverageReview):
    """同一次独立审核分别核验保护约束与本轮授权，不增加模型调用。"""

    findings: list[EditConstraintCoverageItem] = Field(max_length=50)
    edit_findings: list[CoverageItem] = Field(max_length=101)
    scope_issues: list[EditScopeIssue] = Field(default_factory=list, max_length=101)
    constraint_replacements: list[EditConstraintReplacement] = Field(default_factory=list,
                                                                    max_length=50)


def _parse(raw: str, schema: type[BaseModel], stage: str) -> Any:
    try:
        return schema.model_validate_json(raw)
    except (ValueError, ValidationError):
        raise ToolError(
            "语义约束校验未返回有效结构", detail=f"module=comic.constraints stage={stage}"
        ) from None  # 不把尚未验证的模型字段或私有推理写进异常链。


def normalize_constraints(
    facts: list[MemoryFact | ConstraintSource],
    *,
    previous: list[CreativeConstraint],
    model_call: ModelCall,
    on_diagnostics: Callable[[dict], None] | None = None,
    previous_evidence: list[dict] | None = None,
    edit_basis: dict | None = None,
    review_model_call: ModelCall | None = None,
) -> tuple[list[CreativeConstraint], dict]:
    """只接受已核验的当前任务用户原文；同来源同原文复用归一化结果。"""
    facts = [fact for fact in facts if fact.origin == "user_request"]
    if len(facts) > 50 or sum(len(fact.value) for fact in facts) > 12000:
        raise ToolError(
            "本轮必需约束超过容量，请缩小范围", detail="module=comic.constraints stage=capacity"
        )
    retained, pending = [], []
    verified = {
        item["sha256"]: item["constraint_ids"]
        for evidence in previous_evidence or []
        for item in evidence.get("verified_sources", [])
    }
    reused_sources = {}
    for fact in facts:
        matches = [
            item
            for item in previous
            if item.source_ref == fact.source and item.source_quote in fact.value
        ]
        source_hash = value_hash(
            {"quote": fact.value, "source_ref": fact.source.model_dump(mode="json")}
        )
        if fact.status != "active" or (
            source_hash in verified
            and set(verified[source_hash]) == {item.constraint_id for item in matches}
        ):
            retained.extend(item.model_copy(update={"status": fact.status}) for item in matches)
            if source_hash in verified:
                reused_sources[source_hash] = verified[source_hash]
        else:
            pending.append(fact)
    if not pending:
        return retained, {
            "contract": CONSTRAINT_CONTRACT,
            "reused": True,
            "verified_sources": [
                {"sha256": key, "constraint_ids": ids} for key, ids in reused_sources.items()
            ],
        }
    payload = [
        {"quote": fact.value, "source_ref": fact.source.model_dump(mode="json")} for fact in pending
    ]
    proposals = _parse(
        model_call(
            [
                {
                    "role": "system",
                    "content": "从已核验的用户原文提取本轮必须满足的视觉要求。"
                    "不要复制委托措辞作为视觉要求；不要添加推断出的服装、年龄、机位或风格。"
                    "每项用 source_index 和连续 source_quote"
                    "指向输入证据，requirement 可忠实改写，target 指明对象，scope 指明作用范围。"
                    "每项只表达一个独立范围的要求，跨范围要求拆开，不把整体委托汇总为一项。"
                    "identity 表示主体身份和固定造型（含外貌、服饰、特征）；"
                    "world 表示地点、时间与环境；"
                    "style 表示艺术风格与视觉质感；shot 表示当轮动作、表情、摄影等可变化状态；"
                    "output 表示输出形态、数量与指定文字。否定要求与对应对象一同保存。"
                    "只有用户明确要求字面文本/原样措辞时 verification=exact。"
                    "requirement 是要求的意思；literal_value 是要在画面上逐字显示的内容。"
                    "exact 必须填写 literal_value，只复制实际指定的文字，不包含委托说明；"
                    "source_quote 可以包含指定说明，literal_value 必须是其中的连续原文。"
                    "semantic 的 literal_value=null，requirement 可忠实改写或留 null 沿用引用。"
                    "普通要求 verification=semantic。纠正后的当前事实优先，不恢复已撤回内容。"
                    "没有视觉要求可返回空列表。只返回 JSON："
                    + json.dumps(ConstraintProposals.model_json_schema(), ensure_ascii=False),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
        ),
        ConstraintProposals,
        "normalize",
    )
    if on_diagnostics:
        on_diagnostics(
            {
                "stage": "proposal",
                "trust_status": "unverified",
                "sources": payload,
                "proposals": proposals.model_dump(mode="json"),
            }
        )
    created = []
    for item in proposals.constraints:
        if item.source_index >= len(pending):
            raise ToolError(
                "语义约束引用了未知来源", detail="module=comic.constraints stage=source"
            )
        fact = pending[item.source_index]
        if item.source_quote not in fact.value:
            raise ToolError(
                "语义约束证据不在已读原文中", detail="module=comic.constraints stage=source"
            )
        requirement = item.requirement or item.source_quote
        if item.verification == "exact" and (not item.literal_value
                or item.literal_value not in item.source_quote):
            raise ToolError("指定文字值必须指向引用的连续原文",
                            detail="module=comic.constraints stage=literal_source")
        identity = value_hash(
            {
                "source": fact.source.model_dump(mode="json"),
                **item.model_dump(exclude={"source_index"}),
            }
        )
        created.append(
            CreativeConstraint(
                constraint_id=f"constraint-{identity[:24]}",
                requirement=requirement,
                target=item.target,
                scope=item.scope,
                source_ref=fact.source,
                source_quote=item.source_quote,
                origin="user_request",
                trust="source_verified",
                verification=item.verification,
                literal_value=item.literal_value,
            )
        )
    # 复用最终 Prompt 核验的证据位置协议，原文来自已核验输入而非模型重新摘抄。
    evidence_fields = {
        f"constraints.{item.constraint_id}.source_quote": item.source_quote for item in created
    }
    review_type = ImageEditExtractionReview if edit_basis is not None else CoverageReview
    review_schema = review_type.model_json_schema()
    review_schema["$defs"]["CoverageItem"]["properties"]["evidence_path"] = {
        "enum": [*evidence_fields, None], "default": None}
    # 归一化模型不能自证：独立调用核对完整输入，阻止漏提要求与虚构硬约束。
    review = _parse(
        (review_model_call or model_call)(
            [
                {
                    "role": "system",
                    "content": "当前是需求提取阶段，尚未创作图像或 Prompt。"
                    "候选输出就是输入中的 constraints 列表。独立核对它与 sources 原文。"
                    "complete 仅在这个候选列表已提取全部必需要求且没有新增要求时为真；"
                    "covered 表示该条提取忠实于原文，不能因没有成品或 Prompt 判为 uncertain。"
                    "scope_valid 核对对象/作用范围/是否确有逐字要求。为每项返回 constraint_id、"
                    "covered/missing/conflict/uncertain；为每项选择 evidence_fields 中属于"
                    "该项 constraint_id 的 evidence_path，quote 留空，服务端读取冻结原文。"
                    "选择位置不代表已通过，仍须独立核验提取忠实、完整和范围有效。"
                    "identity 为主体身份及固定造型，world 为环境时空，style 为视觉风格，"
                    "shot 为当轮动作表情摄影，output 为输出形态数量文字。"
                    "reason 与 summary 只写公开的具体遗漏或范围问题，不输出思维过程。"
                    "exact 项要分别核验 requirement 的意思和 literal_value 的实际文字；"
                    "literal_value 若混入委托说明、对象名或遗漏空格符号，scope_valid=false。"
                    "不要因为近义改写而判缺失，也不要把模型创作选择算成用户约束。"
                    + ("同时在编译前确定 edit_basis 中的新旧要求关系。"
                       "authorized_requirements 是本轮唯一改变授权，"
                       "target/regions 是用户明确的范围。"
                       "区域原文中的‘这个/这里/它’指向该要求 target.region_ids 绑定的选区，"
                       "结合实际原图与归一化坐标识别对象，不因代词没有再次命名就判范围缺失。"
                       "不能将框内主体替换误解为全图主体替换；框内有多个可能对象、图像不足"
                       "或无法核验新旧对象关系时仍返回具体 scope_issues，不能猜测或扩大范围。"
                       "若人工要求明确替换同一对象的同一属性，必须在 constraint_replacements "
                       "逐条关联被替代的历史用户 constraint_id、人工 requirement_id、人工连续"
                       "source_quote 和公开 reason；来源重复也逐个记录，不能留到编译后再猜。"
                       "scope_valid 也核对替代对象与范围有效。只在相应对象和授权范围内替代，"
                       "其他对象、范围和未变属性仍保护。资产事实、本轮新要求、AI 建议不能解除。"
                       "不能因两条要求不同就判替代，语义无法确定或真正冲突时在 scope_issues "
                       "指出对应需求并返回 scope_valid=false；不要默认扩大范围。"
                       "inherited_constraint_replacements 是该版本已确认的目标，不能恢复旧目标。"
                       "scope_issues 只关联已存在的 authorized_requirements 编号。"
                       "没有替代关系必须明确返回 constraint_replacements=[]。"
                       if edit_basis is not None else "") + "只返回 JSON："
                    + json.dumps(review_schema, ensure_ascii=False),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "stage": "constraint_extraction_review",
                            "sources": payload,
                            "constraints": [item.model_dump(mode="json") for item in created],
                            "evidence_fields": evidence_fields,
                            **({"edit_basis": edit_basis} if edit_basis is not None else {}),
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
        ),
        review_type,
        "normalize_review",
    )
    for finding in review.findings:
        if finding.evidence_path is not None:
            expected = f"constraints.{finding.constraint_id}.source_quote"
            if finding.evidence_path != expected or expected not in evidence_fields:
                raise ToolError("需求核验引用了其他要求或无效原文位置",
                                detail="module=comic.constraints stage=source_evidence_path")
            finding.quote = evidence_fields[expected]
    if on_diagnostics:
        on_diagnostics(
            {
                "contract": CONSTRAINT_CONTRACT,
                "sources": payload,
                "constraints": [item.model_dump(mode="json") for item in created],
                "review": review.model_dump(mode="json"),
            }
        )
    if isinstance(review, ImageEditExtractionReview):
        original = [CreativeConstraint.model_validate(item)
                    for item in edit_basis["historical_constraints"]]
        issues = _edit_replacement_issues(review.constraint_replacements, original, edit_basis)
        known = {item["requirement_id"] for item in edit_basis["authorized_requirements"]}
        issues += [{"code": "scope_invalid" if set(item.requirement_ids) <= known
                    else "protocol_invalid", **item.model_dump(mode="json")}
                   for item in review.scope_issues]
        if issues:
            error = ToolError("本轮修改对象或替代范围未能确定，请查看对应要求",
                              detail="module=comic.constraints stage=edit_resolution")
            error.review_issues = issues
            raise error
    _require_review(review, created, {item.constraint_id: item.source_quote for item in created})
    result = {item.constraint_id: item for item in [*retained, *created]}
    if len(result) > 50:
        raise ToolError("本轮约束超过容量", detail="module=comic.constraints stage=capacity")
    return list(result.values()), {
        "contract": CONSTRAINT_CONTRACT,
        "source_sha256": value_hash(payload),
        "constraints_sha256": value_hash([item.model_dump(mode="json") for item in created]),
        "review": review.model_dump(mode="json"),
        "verified_sources": [
            *[{"sha256": key, "constraint_ids": ids} for key, ids in reused_sources.items()],
            *[
                {
                    "sha256": value_hash(source),
                    "constraint_ids": [
                        item.constraint_id
                        for item in created
                        if item.source_ref.model_dump(mode="json") == source["source_ref"]
                        and item.source_quote in source["quote"]
                    ],
                }
                for source in payload
            ],
        ],
    }


def _edit_replacement_issues(replacements, constraints, scope):
    originals = {item.constraint_id: item for item in constraints}
    historical = set(scope.get("historical_constraint_ids", []))
    manual = {item["requirement_id"]: item for item in scope["authorized_requirements"]
              if item["source"]["kind"] in {"manual_region", "manual_supplement"}
              and item["target"]["mode"] in {"whole", "regions"}}
    seen, issues = set(), []
    for item in replacements:
        original, requirement = originals.get(item.constraint_id), manual.get(item.requirement_id)
        if (item.constraint_id in seen or item.constraint_id not in historical or not original
                or original.origin != "user_request" or not requirement
                or item.source_quote not in requirement["requirement"]):
            issues.append({"code": "replacement_invalid",
                           "requirement_ids": [item.constraint_id, item.requirement_id],
                           "reason": "旧要求替代缺少有效的本轮人工来源或对象范围核验"})
        seen.add(item.constraint_id)
    return issues


def image_edit_constraint_basis(scope, constraints):
    """Compiler 与独立审核使用同一冻结保护规则，不能把范围例外丢回扁平旧要求。"""
    requirements = {item["requirement_id"]: item for item in scope["authorized_requirements"]}
    replacements = {item["constraint_id"]: item for item in scope.get(
        "constraint_resolution", {}).get("constraint_replacements", [])}
    inherited = {item["constraint_id"]: item for item in scope.get(
        "inherited_constraint_replacements", [])}
    current = {item["constraint_id"]: item["requirement_ids"] for item in scope.get(
        "current_constraint_targets", [])}
    result = []
    for item in constraints:
        replacement = replacements.get(item.constraint_id)
        prior = inherited.get(item.constraint_id)
        if replacement or prior:
            result.append({"constraint_id": item.constraint_id, "enforcement": "scoped_replacement",
                "original_requirement": item.model_dump(mode="json"),
                "effective_requirement": requirements[replacement["requirement_id"]]
                if replacement else prior, "protect_other_objects_and_unchanged_properties": True})
        else:
            result.append({**item.model_dump(mode="json"),
                "enforcement": "current_requirement" if item.constraint_id in current
                else "preserve",
                **({"authorized_targets": [requirements[key]["target"]
                    for key in current[item.constraint_id]]}
                   if item.constraint_id in current else {})})
    return result


def _require_review(
    review: CoverageReview, constraints: list[CreativeConstraint], evidence: dict[str, str],
    *, edit_mode: bool = False,
) -> None:
    findings = {item.constraint_id: item for item in review.findings}
    if (
        not review.complete
        or not review.scope_valid
        or len(findings) != len(review.findings)
        or set(findings) != {item.constraint_id for item in constraints}
    ):
        error = ToolError(
            "语义要求存在遗漏或作用范围不明确", detail="module=comic.constraints stage=coverage"
        )
        error.review_issues = [
            {"code": "missing" if item.constraint_id not in findings else
             findings[item.constraint_id].status,
             "requirement_ids": [item.constraint_id],
             "reason": findings[item.constraint_id].reason if item.constraint_id in findings
             else "系统整理结果缺少此要求的核验记录"}
            for item in constraints if item.constraint_id not in findings
            or findings[item.constraint_id].status != "covered"
        ] or [{"code": "scope_invalid" if not review.scope_valid else "protocol_invalid",
               "requirement_ids": [], "reason": review.summary or
               "系统未能确认本轮范围或返回完整的核验记录"}]
        error.review_sources = {item.constraint_id: item.source_quote for item in constraints}
        # 结构化回传未覆盖项：调用方需要在不放弃本次创作的前提下补齐正文。
        error.review_uncovered = [
            item.constraint_id for item in constraints
            if item.constraint_id not in findings
            or findings[item.constraint_id].status != "covered"]
        error.review_result = review.model_dump(mode="json")
        raise error
    for item in constraints:
        finding = findings[item.constraint_id]
        if (
            finding.status != "covered"
            or not finding.quote
            or finding.quote not in evidence[item.constraint_id]
        ):
            error = ToolError(
                "系统生成的改图指令未通过需求核验，请查看具体原因" if edit_mode else
                "本轮要求未获得有效证据，尚未生成指令。请明确修改对象、内容和保留要求",
                detail=f"module=comic.constraints stage=coverage id={item.constraint_id} "
                       f"status={finding.status} evidence="
                       + ("not_covered" if finding.status != "covered" else
                          "missing" if not finding.quote else "unmatched"),
            )
            error.review_issues = [{"code": finding.status if finding.status != "covered"
                                   else "evidence_invalid",
                                   "requirement_ids": [item.constraint_id],
                                   "reason": finding.reason or "系统整理结果未提供有效覆盖证据"}]
            error.review_sources = {item.constraint_id: item.source_quote}
            error.review_uncovered = [item.constraint_id]
            error.review_result = review.model_dump(mode="json")
            raise error


def verify_prompt_constraints(
    constraints: list[CreativeConstraint],
    *,
    sections: dict[str, str],
    negative_prompt: str,
    model_call: ModelCall | None,
    receipt: dict | None = None,
    on_diagnostics: Callable[[dict], None] | None = None,
    edit_scope: dict | None = None,
    allow_unverified: bool = False,
) -> dict:
    """审核四段最终正文；凭据绑定全部输入和输出，编辑后自动失效。

    allow_unverified 只用于“正文已按用户要求补齐、独立复核仍存疑”的最终交付：
    核验结论原样保留，另以 delivery=advisory 与 unresolved 显式标注未确认项，
    既不放宽审核结论，也不让一次复核存疑吞掉用户的创作指令。
    """
    active = [
        item for item in constraints if item.status == "active" and item.origin != "model_choice"
    ]
    if len(active) > 50:
        raise ToolError(
            "必需约束超过语义审核容量，请缩小范围", detail="module=comic.constraints stage=capacity"
        )
    payload = {
        "contract": CONSTRAINT_CONTRACT,
        "constraints": [item.model_dump(mode="json") for item in active],
        "sections": sections,
        "negative_prompt": negative_prompt,
    }
    if edit_scope is not None:
        payload["edit_scope"] = edit_scope.get("edit_scope", edit_scope)
        payload["edit_context"] = {key: value for key, value in edit_scope.items()
                                   if key not in {"edit_scope", "authorized_requirements",
                                                  "protected_features"}}
        if "authorized_requirements" in edit_scope:
            payload["authorized_requirements"] = edit_scope["authorized_requirements"]
        if "constraint_resolution" in edit_scope:
            payload["constraints"] = image_edit_constraint_basis(edit_scope, active)
    fingerprint = value_hash(payload)
    evidence_fields = {f"sections.{name}": text for name, text in sections.items() if text}
    for name, text in sections.items():
        if name not in ComicPromptContexts.model_fields:
            continue
        labels = ComicPromptContexts.model_fields[name].annotation.labels
        names = {label: field for field, label in labels.items()}
        for line in text.splitlines():
            label, separator, value = line.partition("：")
            if separator and label in names and value.strip():
                evidence_fields[f"sections.{name}.{names[label]}"] = value.strip()
    edit_requirements = (edit_scope or {}).get("authorized_requirements")
    review_type = ImageEditCoverageReview if edit_requirements is not None else CoverageReview
    review_schema = review_type.model_json_schema()
    for item_type in ("CoverageItem", "EditConstraintCoverageItem"):
        if item_type in review_schema["$defs"]:
            review_schema["$defs"][item_type]["properties"]["evidence_path"] = {
                "enum": [*evidence_fields, None], "default": None}
    historical_ids = set((edit_scope or {}).get("historical_constraint_ids", []))
    inherited = {item["constraint_id"]: item for item in (edit_scope or {}).get(
        "inherited_constraint_replacements", []) if item["constraint_id"] in historical_ids}
    manual_requirements = {item["requirement_id"]: item for item in edit_requirements or []
        if item["source"]["kind"] in {"manual_region", "manual_supplement"}
        and item["target"]["mode"] in {"whole", "regions"}}
    # 新原样文字仍先检查；旧文字只在有明确人工范围时延后，由独立审核核对是否替代。
    deferred_exact_ids = {item.constraint_id for item in active
        if (manual_requirements or item.constraint_id in inherited)
        and item.constraint_id in historical_ids
        and item.origin == "user_request"}
    for item in active:
        if (item.constraint_id not in deferred_exact_ids and item.verification == "exact"
                and item.literal_value not in "\n".join(sections.values())):
            error = ToolError(
                "Prompt 遗漏用户指定的原样文字",
                detail=f"module=comic.constraints stage=exact id={item.constraint_id}",
            )
            error.review_issues = [{"code": "missing", "requirement_ids": [item.constraint_id],
                                   "reason": "系统整理结果遗漏指定的原样文字"}]
            raise error
    if receipt and receipt.get("sha256") == fingerprint:
        review = _parse(json.dumps(receipt.get("review")), review_type, "receipt")
    else:
        if model_call is None:
            raise ToolError(
                "修改后的 Prompt 需要重新核验语义约束",
                detail="module=comic.constraints stage=review_required",
            )
        review = _parse(
            model_call(
                [
                    {
                        "role": "system",
                        "content": (
                            "当前为单段 Prompt，不检查四段归属，仅核对要求和矛盾。"
                            if set(sections) == {"positive_prompt"}
                            else ""
                        )
                        + ("独立审核当前单段正向修改指令和负向约束。"
                           if edit_scope is not None else "独立审核最终图片 Prompt 四段。")
                        + ("当前为改图指令：edit_scope 为实际范围，edit_context 为编辑依据，"
                           "authorized_requirements 是冻结授权清单。scope_valid 核对只修改"
                           "选中发现项和用户本次指令授权的内容，其他观察不能自动变成修改；"
                           "要求保留未授权改变的特征。与有效约束冲突时不能放行。"
                           "若有 edit_scope，核对整图/局部模式、每个区域的坐标和修改要求；"
                           "不能把局部修改扩展到区域外。"
                           "mixed 模式逐条核对：whole_finding_ids 对应建议才有整图授权；"
                           "局部建议、各区要求始终限制在关联区域。user_instruction 必须遵循"
                           "instruction_target 的整图、全部选区或指定 region_id 范围。"
                           "存在整图要求也不能放宽其他局部要求，不能以补充要求静默覆盖矛盾。"
                           "现有 constraints 是必须保护、不得违背的要求，不是自动纠正整张图的授权。"
                           "未授权对象保持原图，不要求本轮主动实现其全部原始创作目标，"
                           "也不能声称原图已符合尚未核实的特征。"
                           "historical_constraint_ids 来自图片修改前的方案。人工本轮要求明确改变"
                           "同一对象的同一属性时，在授权范围内以本轮要求为准。独立判断语义、对象"
                           "和范围，不按关键词猜测替代，不因两条要求不同就解除保护。"
                           "这种旧用户要求返回 superseded，并在 constraint_replacements 中关联"
                           "旧 constraint_id、人工 requirement_id、"
                           "来自该人工要求的连续 source_quote"
                           "和公开 reason。其 evidence_path 必须指向实际执行新要求的正向正文。"
                           "只允许 historical_constraint_ids 中 origin=user_request "
                           "的旧要求被替代；"
                           "资产固定事实、本轮新要求、单纯 AI 建议不能被此方式解除。"
                           "局部替代只影响该对象及其授权区域，其他对象、区域和未变属性仍保护。"
                           "current_constraint_targets 关联本轮提取要求与人工原文授权项；"
                           "以关联 requirement_id 的 target 核对其范围，不将代词或提取的"
                           "宽泛 target 误当整图授权。"
                           "inherited_constraint_replacements 是当前图片版本沿显式父版本读取的"
                           "已确认修改目标，不证明外部结果已经达成。其旧要求仍返回 superseded，"
                           "在原替代对象上保护记录中的新要求，其他对象与未变属性仍保护。"
                           "未被本轮人工再次改变时，不要恢复旧目标或把新要求列为缺失旧目标。"
                           "记录中的 source_image/regions 属于历史定位，不是当前图片的新修改授权，"
                           "不能直接映射为当前选区。新替代才填 constraint_replacements。"
                           "若有 constraint_resolution，它是编译前已经核验的范围关系。"
                           "constraints 中 enforcement=scoped_replacement 需核对正文执行"
                           "effective_requirement 并保护其他对象与未变属性，original_requirement "
                           "只是旧来源，不能要求同时生成已替代的旧对象。"
                           "符合该有效规则返回 covered（或 superseded），但实际正文/负向仍有矛盾"
                           "必须 conflict；关系已确定不代表生成文本已通过。"
                           "仍独立核对规则的人工来源及同一对象/属性关系，错误绑定或越界"
                           "必须 scope_valid=false，不因存在冻结记录就放行。"
                           "constraint_replacements 留空或照抄已冻结关系，不能自行增加或改写。"
                           "未被本轮改变的旧要求按不得违背/保持原图审核，不要求重新生成原目标；"
                           "受影响而没有有效人工替代依据时返回 conflict 或 uncertain。"
                           if edit_scope is not None else "")
                        +
                        "核对每项要求的对象、关系、否定及范围，"
                        "允许准确的近义表达；只有 exact 要求必须原样。"
                        + ("" if edit_scope is not None else
                           "检查人物段仅含持续身份/外貌，世界段仅含环境，风格段仅含风格质感，"
                           "镜头段容纳当轮动作/摄影。") +
                        "scope_valid 表示这些语义归属正确，不凭单个词判断。结合负向正文检查矛盾。"
                        "每项选择 evidence_fields 中的一个 evidence_path 作为覆盖依据，"
                        "quote 留空，服务端从该冻结字段取原文；不要改写或拼接证据。"
                        "有合适的细字段时优先选细字段，无有效覆盖则 missing 或 uncertain。"
                        "findings 必须逐条包含 constraints 的 constraint_id，"
                        "内容相近也不能省略来源编号；"
                        "没有保护约束时 findings=[]。"
                        + ("edit_findings 必须逐条包含 authorized_requirements 的 requirement_id，"
                           "填入 constraint_id；即使内容相近也不能省略编号。核对改变和目标位置，"
                           "按同一 evidence_fields 选择正文证据。scope_issues 只给公开原因和"
                           "相关授权 requirement_ids，不能返回私有推理。"
                           if edit_requirements is not None else "") + "只返回 JSON："
                        + json.dumps(review_schema, ensure_ascii=False),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {**payload, "evidence_fields": evidence_fields}, ensure_ascii=False
                        ),
                    },
                ]
            ),
            review_type,
            "prompt_review",
        )
    all_findings = [*review.findings, *(review.edit_findings
                    if isinstance(review, ImageEditCoverageReview) else [])]
    for finding in all_findings:
        if finding.evidence_path is not None:
            value = evidence_fields.get(finding.evidence_path)
            if not value:
                raise ToolError(
                    "语义审核引用了无效正文位置",
                    detail="module=comic.constraints stage=evidence_path",
                )
            # 此协议选择位置，原文由服务端提供；模型附带的自由 quote 不作为证据。
            finding.quote = value
    if on_diagnostics:
        on_diagnostics({**payload, "review": review.model_dump(mode="json")})
    if isinstance(review, ImageEditCoverageReview):
        authorized_ids = {item["requirement_id"] for item in edit_requirements}
        returned = {item.constraint_id: item for item in review.edit_findings}
        issues = [{"code": "missing", "requirement_ids": [item],
                   "reason": "系统整理结果缺少此项修改的核验记录"}
                  for item in authorized_ids - returned.keys()]
        if len(returned) != len(review.edit_findings) or returned.keys() - authorized_ids:
            issues.append({"code": "protocol_invalid", "requirement_ids": [],
                           "reason": "系统核验返回了重复或未知修改编号"})
        for finding in review.edit_findings:
            if finding.constraint_id in authorized_ids and (
                    finding.status != "covered" or not finding.quote
                    or finding.quote not in "\n".join(sections.values())):
                issues.append({"code": finding.status if finding.status != "covered"
                               else "evidence_invalid",
                               "requirement_ids": [finding.constraint_id],
                               "reason": finding.reason or "系统整理结果未覆盖此项修改及其目标"})
        for issue in review.scope_issues:
            if set(issue.requirement_ids) - authorized_ids:
                issues.append({"code": "protocol_invalid", "requirement_ids": [],
                               "reason": "范围核验关联了未知修改编号"})
            else:
                issues.append({"code": "scope_invalid", **issue.model_dump(mode="json")})
        constraints_by_id = {item.constraint_id: item for item in active}
        constraint_findings = {item.constraint_id: item for item in review.findings}
        has_resolution = "constraint_resolution" in edit_scope
        planned = [EditConstraintReplacement.model_validate(item) for item in edit_scope.get(
            "constraint_resolution", {}).get("constraint_replacements", [])]
        issues += _edit_replacement_issues(planned, active, edit_scope)
        planned_by_id = {item.constraint_id: item for item in planned}
        replaced_ids = set(planned_by_id)
        returned_replacements = set()
        for replacement in review.constraint_replacements:
            identity = replacement.constraint_id
            original = constraints_by_id.get(identity)
            requirement = manual_requirements.get(replacement.requirement_id)
            finding = constraint_findings.get(identity)
            if (identity in returned_replacements or identity not in historical_ids or not original
                    or original.origin != "user_request" or not requirement
                    or replacement.source_quote not in requirement["requirement"]
                    or not finding or finding.status not in (
                        {"covered", "superseded"} if has_resolution else {"superseded"})
                    or (has_resolution and (identity not in planned_by_id
                        or replacement.requirement_id != planned_by_id[identity].requirement_id))):
                issues.append({"code": "replacement_invalid",
                    "requirement_ids": [identity, replacement.requirement_id],
                    "reason": "旧要求替代缺少有效的本轮人工来源或对象范围核验"})
            replaced_ids.add(identity)
            returned_replacements.add(identity)
        for finding in review.findings:
            if (finding.status == "superseded" and finding.constraint_id not in replaced_ids
                    and finding.constraint_id not in inherited):
                issues.append({"code": "replacement_invalid",
                    "requirement_ids": [finding.constraint_id],
                    "reason": "系统核验解除旧要求时没有提供本轮人工修改依据"})
        for item in active:
            if (item.constraint_id in deferred_exact_ids and item.constraint_id not in replaced_ids
                    and item.constraint_id not in inherited
                    and item.verification == "exact"
                    and item.literal_value not in "\n".join(sections.values())):
                issues.append({"code": "missing", "requirement_ids": [item.constraint_id],
                               "reason": "系统整理结果遗漏仍须保留的原样文字"})
        for identity, change in inherited.items():
            original = constraints_by_id.get(identity)
            if not original or original.origin != "user_request":
                issues.append({"code": "replacement_invalid", "requirement_ids": [identity],
                               "reason": "历史替代记录不能解除资产固定事实或未知要求"})
            if identity in replaced_ids:
                continue
            finding = constraint_findings.get(identity)
            if (not finding or finding.status not in (
                    {"covered", "superseded"} if has_resolution else {"superseded"}) or any(
                    value not in "\n".join(sections.values())
                    for value in change["literal_values"])):
                issues.append({"code": "conflict", "requirement_ids": [identity],
                               "reason": "系统整理结果未保留当前图片版本已经确认的新要求"})
        if issues:
            error = ToolError("系统整理结果未通过修改覆盖或范围核验",
                              detail="module=comic.constraints stage=edit_coverage")
            error.review_issues = issues
            raise error
        # 仅在替代关联与独立范围审核均有效时，按新要求的正文证据核验旧要求本轮的处理。
        coverage_review = review.model_copy(update={"findings": [
            item.model_copy(update={"status": "covered"})
            if item.status == "superseded" and (
                item.constraint_id in replaced_ids or item.constraint_id in inherited)
            else item for item in review.findings]})
    else:
        coverage_review = review
    delivery, unresolved = "verified", []
    try:
        _require_review(
            coverage_review, active,
            {item.constraint_id: "\n".join(sections.values()) for item in active},
            edit_mode=edit_scope is not None,
        )
    except ToolError as error:
        if not allow_unverified or not (error.detail or "").startswith(
                "module=comic.constraints stage=coverage"):
            raise
        delivery = "advisory"
        unresolved = list(getattr(error, "review_uncovered", []))
        logger.warning(
            "prompt_constraint_delivery_advisory trace_id=%s unresolved=%s detail=%s",
            current_trace_id(), unresolved, error.detail,
        )
    if isinstance(review, ImageEditCoverageReview) and has_resolution:
        review.constraint_replacements = planned
    return {
        "sha256": fingerprint,
        "contract": CONSTRAINT_CONTRACT,
        "delivery": delivery,
        "unresolved": unresolved,
        "review": review.model_dump(mode="json"),
    }
