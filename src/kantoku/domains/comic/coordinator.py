"""Comic 导演流程协调层。

该模块只协调现有 Core Run、Runtime Event、SkillRegistry 和作品版本存储。
真实模型调用由调用方注入的共享文本 Capability 提供；本模块不导入 Provider。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any, Literal
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, model_validator

from kantoku.config import ToolError
from kantoku.config.logging_setup import redact_secrets
from kantoku.config.observability import (
    current_trace_id,
    public_error,
    request_trace,
    run_trace,
)
from kantoku.core.conversations import InteractionMode
from kantoku.core.runtime.models import ExecutionStatus, RuntimeEventType
from kantoku.core.runtime.store import RuntimeStore
from kantoku.core.skills import SkillRegistry

from .cinematography import parse_cinematography
from .critic import DirectorCriticEngine, require_approved_director
from .models import (
    CinematographyPlan,
    ComicAsset,
    ComicContext,
    ComicProjectSnapshot,
    ComicShot,
    ComicStoryboard,
    CreativeDecision,
    DirectorCriticResult,
    DirectorPlan,
    DirectorSpec,
    DirectorSpecDraft,
)
from .projects import ComicContextBuilder, ComicProjectStore

StageExecutor = Callable[
    [str, Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]
]
DIRECTOR_STAGES = (
    "creative_understanding", "visual_direction", "cinematography",
    "director_critic", "director_assemble",
)
FAST_LABELS = {
    "creative_understanding": "正在理解创意",
    "visual_direction": "正在设计视觉方案",
    "cinematography": "正在设计视觉方案",
    "director_critic": "正在设计视觉方案",
    "director_assemble": "正在生成导演方案",
    "director_spec": "导演方案已完成",
}


def _fingerprint(payload: Mapping[str, Any]) -> dict[str, Any]:
    """有界公开输入的指纹；不把完整模型响应或私有推理写入日志。"""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return {"sha256": hashlib.sha256(encoded.encode()).hexdigest(),
            "chars": len(encoded), "keys": sorted(payload)}


class _DirectorCancelled(Exception):
    """阶段边界观察原 Core Run 的取消状态。"""


def director_execution_summary(run: Any) -> dict[str, Any]:
    """公开状态投影；Fast 隐藏内部输出，但保留真实阶段状态和故障定位。"""
    mode = run.state["execution_mode"]
    status = run.status.value
    current = (run.current_node or "creative_understanding").removeprefix("comic.")
    label = FAST_LABELS.get(current, "正在设计视觉方案")
    failure = run.state.get("stage_failures", {}).get(f"comic.{current}")
    review = run.state.get("critic_result") or {}
    if status == "waiting":
        findings = review.get("findings", [])
        issue = next((item for item in findings if item["severity"] in {"warning", "error"}), {})
        reason = (
            (failure or {}).get("safe_message")
            or issue.get("suggested_action")
            or review.get("public_summary")
            or "请查看审核结果"
        )
        label = f"导演草案已生成，待审核或修订：{reason[:150]}"
    elif status == "failed":
        reason = (failure or {}).get("safe_message", "请按错误 ID 查看详情")
        label = f"导演阶段 {current} 失败：{reason}"
    elif status == "cancelled":
        label = "导演任务已取消"
    actions = ["view"]
    if status in {"failed", "waiting"}:
        actions.append("resume")
    if mode == "professional" and status in {"completed", "failed", "waiting"}:
        actions.extend(["edit_stage", "rerun_stage"])
    summary: dict[str, Any] = {
        "mode": mode,
        "current_stage": current if mode == "professional" else label,
        "status_label": label,
        "available_actions": actions,
        "stages": [],
        "error_id": run.state.get("error_id"),
        "trace_id": run.state.get("trace_id"),
        "failure": failure,
        "stage_statuses": [],
    }
    for stage in DIRECTOR_STAGES:
        key = f"comic.{stage}"
        output = run.state.get("stage_outputs", {}).get(key)
        node_status = "completed" if output is not None else "pending"
        if run.state.get("failed_skill_id") == key:
            node_status = "failed"
        elif current == stage and status in {"running", "waiting"}:
            node_status = status
        node_status = run.state.get("stage_statuses", {}).get(key, node_status)
        if (stage == "director_critic" and status == "waiting"
                and node_status not in {"failed", "unavailable"}):
            node_status = "needs_revision"
        if node_status == "pending" and status in {"waiting", "failed"}:
            node_status = "waiting"
        summary["stage_statuses"].append(
            {
                "stage_name": stage,
                "status": node_status,
                "trace_id": run.state.get("trace_id"),
                "input_version": run.state.get("input_versions", {}),
                "failure": run.state.get("stage_failures", {}).get(key),
            }
        )
        public_output = output or {}
        candidate_key = {
            "creative_understanding": "creative_decision",
            "visual_direction": "director_plan",
            "cinematography": "cinematography",
        }.get(stage)
        candidate = run.state.get("director_candidate", {})
        if candidate_key and candidate_key in candidate:
            public_output = {candidate_key: candidate[candidate_key]}
        if mode == "professional":
            summary["stages"].append(
                {
                    "stage": stage,
                    "status": node_status,
                    "input_versions": run.state["input_versions"],
                    "output": public_output,
                    "output_summary": next(
                        (
                            value.get("public_summary")
                            or value.get("intent_summary")
                            or value.get("visual_strategy")
                            or value.get("public_decision")
                            or value.get("lighting")
                            or value.get("visual_direction")
                            for value in public_output.values()
                            if isinstance(value, dict)
                        ),
                        None,
                    ),
                    "failure": run.state.get("stage_failures", {}).get(key),
                }
            )
    if mode == "professional":
        summary["critic_result"] = run.state.get("critic_result")
    return summary


class DirectorCoordinatorRequest(BaseModel):
    """导演协调所需的有界输入。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    snapshot: ComicProjectSnapshot
    assets: list[ComicAsset] = Field(default_factory=list, max_length=8)
    task: str | None = Field(default=None, max_length=1000)
    execution_mode: Literal["fast", "professional"] = "professional"
    storyboard: ComicStoryboard | None = None
    shot: ComicShot | None = None
    prior_spec: DirectorSpec | None = None
    rerun_from: Literal[
        "creative_understanding", "visual_direction", "cinematography",
        "director_critic", "director_assemble",
    ] | None = None
    previous_run_id: str | None = None
    trace_id: str | None = Field(default=None, max_length=100)
    conversation_id: str | None = Field(default=None, max_length=100)
    worker_instance_id: str | None = Field(default=None, max_length=100)
    stage_edits: dict[str, Any] = Field(default_factory=dict, max_length=1)
    creative_context: dict[str, Any] = Field(default_factory=dict)
    review_draft: DirectorSpec | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> DirectorCoordinatorRequest:
        project_id = self.snapshot.project.project_id
        for asset in self.assets:
            if asset.project_id != project_id:
                raise ValueError("导演上下文不能引用其他作品的资产")
        if self.storyboard is not None and self.storyboard.project_id != project_id:
            raise ValueError("导演上下文不能引用其他作品的分镜")
        if self.shot is not None:
            if self.shot.project_id != project_id:
                raise ValueError("导演上下文不能引用其他作品的镜头")
            if self.storyboard is None or self.shot.storyboard_id != self.storyboard.storyboard_id:
                raise ValueError("镜头必须属于传入的分镜")
        if self.prior_spec is not None and self.prior_spec.project_id != project_id:
            raise ValueError("导演方案必须属于当前作品")
        if self.review_draft is not None and (
            self.review_draft.project_id != project_id
            or self.review_draft.creative_brief_version != self.snapshot.creative_brief.version
            or self.review_draft.version != self.snapshot.project.director_version
            or self.previous_run_id or self.stage_edits
        ):
            raise ValueError("只能重新审核当前 Brief 下的当前导演草稿")
        if (self.rerun_from is None) != (self.previous_run_id is None):
            raise ValueError("重新执行阶段必须指定来源 Run 和起始阶段")
        if self.stage_edits:
            models = {
                "creative_understanding": CreativeDecision,
                "visual_direction": DirectorPlan,
                "cinematography": CinematographyPlan,
            }
            if (self.execution_mode != "professional" or self.rerun_from not in models
                    or set(self.stage_edits) != {self.rerun_from}):
                raise ValueError("仅专业模式允许修改选定的公开节点")
            models[self.rerun_from].model_validate(self.stage_edits[self.rerun_from])
        return self


class DirectorStageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    skill_id: str
    status: Literal["completed", "failed", "needs_revision", "unavailable"] = "completed"
    output_keys: list[str] = Field(default_factory=list)


class DirectorCoordinatorResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: str
    trace_id: str
    project_id: str
    execution_mode: Literal["fast", "professional"]
    director_spec: DirectorSpec | None
    director_draft: DirectorSpecDraft | None = None
    critic_result: DirectorCriticResult | None = None
    needs_review: bool = False
    stages: list[DirectorStageRecord]
    stale_dependents: list[str] = Field(default_factory=list)


class ComicDirectorCoordinator:
    """用一个 Core Run 协调所有导演 Skill。"""

    def __init__(
        self,
        *,
        registry: SkillRegistry,
        runtime_store: RuntimeStore,
        project_store: ComicProjectStore,
        stage_executor: StageExecutor,
        critic_engine: DirectorCriticEngine | None = None,
    ) -> None:
        self.registry = registry
        self.runtime_store = runtime_store
        self.project_store = project_store
        self.project_store.runtime_store = runtime_store
        self.stage_executor = stage_executor
        self.critic_engine = critic_engine or DirectorCriticEngine()

    def execute(self, request: DirectorCoordinatorRequest) -> DirectorCoordinatorResult:
        trace_id = request.trace_id or current_trace_id() or f"trace-{uuid4().hex}"
        project = request.snapshot.project
        input_versions = self._input_versions(request)
        context = ComicContextBuilder.build(
            request.snapshot, task=request.task, assets=request.assets,
        )
        context_payload = self._context_payload(request, context)
        reused_outputs = self._reused_outputs(request, input_versions)
        state: dict[str, Any] = {
            "task_type": "director",
            "project_id": project.project_id,
            "execution_mode": request.execution_mode,
            "conversation_id": request.conversation_id,
            "worker_instance_id": request.worker_instance_id,
            "trace_id": trace_id,
            "task": request.task,
            "input_versions": input_versions,
            "input_brief_id": request.snapshot.creative_brief.brief_id,
            "input_brief_version": request.snapshot.creative_brief.version,
            "creative_context": request.creative_context
            or {
                "brief_used": f"{request.snapshot.creative_brief.brief_id}"
                f"@v{request.snapshot.creative_brief.version}",
                "previous_brief_detected": False,
                "fork_created": False,
                "reason": "explicit snapshot",
            },
            "completed_stages": list(reused_outputs),
            "stage_outputs": reused_outputs,
            "stage_statuses": {
                f"comic.{stage}": "completed" if f"comic.{stage}" in reused_outputs else "pending"
                for stage in DIRECTOR_STAGES
            },
            "stage_failures": {},
            "last_completed_step": list(reused_outputs)[-1].removeprefix("comic.")
            if reused_outputs else None,
            "rerun_from": request.rerun_from,
            "previous_run_id": request.previous_run_id,
            "storyboard_id": request.storyboard.storyboard_id if request.storyboard else None,
            "shot_id": request.shot.shot_id if request.shot else None,
            "director_debug": {
                "context": _fingerprint(context_payload),
                "brief_request": redact_secrets(request.snapshot.creative_brief.original_request),
                "current_task": redact_secrets(request.task or ""),
                "source_versions": context.source_versions,
                "input_brief_id": request.snapshot.creative_brief.brief_id,
                "input_brief_version": request.snapshot.creative_brief.version,
                "memory_used": [
                    {"asset_id": asset.asset_id, "version": asset.version}
                    for asset in request.assets
                ],
                "reused_stages": sorted(reused_outputs),
                "stages": {},
            },
        }
        interaction_mode = (
            InteractionMode.AUTONOMOUS
            if request.execution_mode == "fast"
            else InteractionMode.GUIDED
        )
        run = self.runtime_store.create_run(
            "comic", "comic.director", state, "creative_understanding",
            interaction_mode=interaction_mode,
        )
        state["run_id"] = run.id
        state["task_id"] = run.id
        stages: list[DirectorStageRecord] = []
        outputs: dict[str, Any] = {
            key: value for stage in reused_outputs.values() for key, value in stage.items()
        }
        current_skill = "comic.director"

        with (
            request_trace(trace_id),
            logger.contextualize(
                component="comic.director",
                project_id=project.project_id,
                run_id=run.id,
                task_id=run.id,
                execution_mode=request.execution_mode,
                conversation_id=request.conversation_id or "-",
            ),
        ):
            logger.info(
                "Director Input Snapshot project_id={} conversation_id={} user_request={} "
                "brief_version={} memory_used={} asset_used={} previous_run={} trace_id={}",
                project.project_id, request.conversation_id or "-",
                redact_secrets(
                    request.task or request.snapshot.creative_brief.original_request,
                )[:1000],
                request.snapshot.creative_brief.version,
                [{"asset_id": item["asset_id"], "version": item["version"]}
                 for item in context.relevant_memory],
                {key: value for key, value in input_versions.items() if key.startswith("asset:")},
                request.previous_run_id or "-", trace_id,
            )
            self.runtime_store.update_run(
                run.id, status=ExecutionStatus.RUNNING, state=state,
                current_node="creative_understanding",
            )
            self._event(
                run.id, RuntimeEventType.RUN_STARTED, "director_run_started",
                trace_id=trace_id, project_id=project.project_id,
                execution_mode=request.execution_mode,
            )
            self._event(
                run.id, RuntimeEventType.NODE_PROGRESS, "director_mode_selected",
                trace_id=trace_id, project_id=project.project_id,
                mode=request.execution_mode,
            )
            self._event(
                run.id, RuntimeEventType.NODE_PROGRESS, "director_input_snapshot",
                trace_id=trace_id, project_id=project.project_id,
                conversation_id=request.conversation_id, previous_run=request.previous_run_id,
                debug=state["director_debug"],
                creative_context=state["creative_context"],
            )
            logger.info("Director Debug Context brief_request={} current_task={} "
                        "context={} source_versions={} reused_stages={} creative_context={}",
                        state["director_debug"]["brief_request"],
                        state["director_debug"]["current_task"],
                        state["director_debug"]["context"], context.source_versions,
                        sorted(reused_outputs), redact_secrets(str(state["creative_context"])))
            try:
                if "comic.creative_understanding" not in reused_outputs:
                    self._run_stage(
                        run.id, "comic.creative_understanding", {
                            "creative_brief": context_payload["creative_brief"],
                            "project_context": context_payload["project"],
                            "relevant_assets": context_payload["relevant_assets"],
                            "current_task": context.current_task,
                        }, context_payload, request, state, outputs, stages,
                    )
                if "comic.visual_direction" not in reused_outputs:
                    self._run_stage(
                        run.id, "comic.visual_direction", {
                            "creative_decision": outputs["creative_decision"],
                            "character_assets": self._assets_by_kind(request.assets, "character"),
                            "scene_assets": self._assets_by_kind(request.assets, "scene"),
                            "style_bible": self._style_asset(request.assets),
                        }, context_payload, request, state, outputs, stages,
                    )
                if "comic.cinematography" not in reused_outputs:
                    self._run_stage(
                        run.id, "comic.cinematography", {
                            "director_plan": outputs["director_plan"],
                            "shot_context": self._shot_context(request),
                        }, context_payload, request, state, outputs, stages,
                    )
                # Fast 隐藏审核细节，不绕过真实审核和受限修订。
                provisional = self._provisional_spec(outputs, request, critic_result=None)
                state["director_candidate"] = provisional.model_dump(mode="json")
                state["draft_status"] = "generated"
                self.runtime_store.update_run(
                    run.id,
                    status=ExecutionStatus.RUNNING,
                    state=state,
                    current_node="cinematography",
                )
                self._event(
                    run.id,
                    RuntimeEventType.NODE_PROGRESS,
                    "director_draft_created",
                    trace_id=trace_id,
                    project_id=project.project_id,
                    input_versions=input_versions,
                    draft_status="generated",
                )
                self._run_stage(
                    run.id, "comic.director_critic", {
                        "director_spec": provisional.model_dump(mode="json"),
                    }, context_payload, request, state, outputs, stages,
                )
                if state.get("needs_review"):
                    candidate = DirectorSpecDraft.model_validate(state["director_candidate"])
                    hard = request.snapshot.creative_brief.hard_constraints
                    # 违反不可变输入的候选仍保留 Run；不能提升为当前作品的合法版本。
                    if (candidate.creative_decision.hard_constraints == hard
                            and all(item in candidate.constraints for item in hard)):
                        saved_draft = self.project_store.save_director(
                            project.project_id, candidate,
                            expected_project_version=project.current_version, source="model",
                        )
                        state.update(director_spec_version=saved_draft.version,
                                     director_spec_id=saved_draft.spec_id,
                                     project_version_after=self.project_store.get(
                                         project.project_id).project.current_version)
                    self.runtime_store.update_run(
                        run.id, status=ExecutionStatus.WAITING, state=state,
                        current_node="director_critic",
                    )
                    return DirectorCoordinatorResult(
                        run_id=run.id,
                        trace_id=trace_id,
                        project_id=project.project_id,
                        execution_mode=request.execution_mode,
                        director_spec=None,
                        critic_result=DirectorCriticResult.model_validate(outputs["critic_result"])
                        if outputs.get("critic_result") is not None else None,
                        director_draft=DirectorSpecDraft.model_validate(
                            state["director_candidate"]
                        ),
                        needs_review=True,
                        stages=stages,
                    )
                assemble_input = {
                    "creative_decision": outputs["creative_decision"],
                    "director_plan": outputs["director_plan"],
                    "cinematography": outputs["cinematography"],
                    "critic_result": outputs.get("critic_result"),
                }
                self._run_stage(
                    run.id, "comic.director_assemble", assemble_input,
                    context_payload, request, state, outputs, stages,
                )
                current_skill = "director.persist"
                state["active_skill_id"] = current_skill
                draft = DirectorSpecDraft.model_validate(outputs["director_spec"])
                if draft.schema_version != 2:
                    raise ToolError("导演协调必须输出 DirectorSpec v2")
                draft_data = draft.model_dump()
                draft_data.update({
                    "asset_versions": {
                        key: value for key, value in input_versions.items()
                        if key.startswith("asset:")
                    },
                    "storyboard_version": input_versions.get("storyboard"),
                    "shot_version": input_versions.get("shot"),
                    "critic_result": outputs.get("critic_result"),
                })
                draft = DirectorSpecDraft.model_validate(draft_data)
                require_approved_director(draft)
                self._check_cancelled(run.id)
                spec = self.project_store.save_director(
                    project.project_id, draft,
                    expected_project_version=project.current_version,
                    source="model",
                )
                stale = self._stale_dependents(request, input_versions)
                state["director_spec_version"] = spec.version
                state["director_spec_id"] = spec.spec_id
                state["project_version_after"] = self.project_store.get(
                    project.project_id,
                ).project.current_version
                state["stale_dependents"] = stale
                state["last_completed_step"] = "director_spec_created"
                state["task_status"] = "completed"
                self.runtime_store.update_run(
                    run.id, status=ExecutionStatus.RUNNING, state=state,
                    current_node="director_spec",
                )
                self._event(
                    run.id, RuntimeEventType.NODE_COMPLETED, "director_spec_created",
                    trace_id=trace_id, project_id=project.project_id,
                    execution_mode=request.execution_mode,
                    director_spec_version=spec.version, stale_dependents=stale,
                    director_spec_id=spec.spec_id,
                    input_brief_id=request.snapshot.creative_brief.brief_id,
                    input_brief_version=request.snapshot.creative_brief.version,
                )
                self.runtime_store.update_run(
                    run.id, status=ExecutionStatus.COMPLETED, state=state,
                    current_node="director_spec",
                )
                self._event(
                    run.id, RuntimeEventType.RUN_COMPLETED, "director_run_completed",
                    trace_id=trace_id, project_id=project.project_id,
                    execution_mode=request.execution_mode,
                )
                return DirectorCoordinatorResult(
                    run_id=run.id, trace_id=trace_id, project_id=project.project_id,
                    execution_mode=request.execution_mode, director_spec=spec,
                    critic_result=spec.critic_result,
                    stages=stages, stale_dependents=stale,
                )
            except _DirectorCancelled:
                logger.info("director cancelled at stage={} trace_id={}",
                            state.get("active_skill_id"), trace_id)
                return DirectorCoordinatorResult(
                    run_id=run.id, trace_id=trace_id, project_id=project.project_id,
                    execution_mode=request.execution_mode, director_spec=None, stages=stages,
                )
            except Exception as error:
                if self.runtime_store.get_run(run.id).status is ExecutionStatus.CANCELLED:
                    return DirectorCoordinatorResult(
                        run_id=run.id, trace_id=trace_id, project_id=project.project_id,
                        execution_mode=request.execution_mode, director_spec=None, stages=stages,
                    )
                current_skill = str(state.get("active_skill_id", current_skill))
                failure = public_error(
                    error, trace_id=trace_id, project_id=project.project_id,
                    run_id=run.id, task_id=run.id, skill_id=current_skill,
                    execution_mode=request.execution_mode,
                )
                state.update({
                    "task_status": "failed",
                    "error_id": failure["error_id"],
                    "error_type": type(error).__name__,
                    "failed_skill_id": current_skill,
                    "last_completed_step": state.get("last_completed_step"),
                })
                stage_failure = {
                    **failure,
                    "stage_name": current_skill.removeprefix("comic."),
                    "input_version": input_versions,
                    "output_before_failure": dict(state["stage_outputs"]),
                    "exception": {
                        "type": type(error).__name__,
                        "module": type(error).__module__,
                        "message": failure["safe_message"],
                    },
                }
                state["stage_failures"][current_skill] = stage_failure
                state["stage_statuses"][current_skill] = "failed"
                error._kantoku_public_failure = failure
                self.runtime_store.update_run(
                    run.id, status=ExecutionStatus.FAILED, state=state,
                    current_node=current_skill, error=failure["error_id"],
                )
                self._event(
                    run.id,
                    RuntimeEventType.NODE_FAILED,
                    "director_failed",
                    trace_id=trace_id,
                    project_id=project.project_id,
                    execution_mode=request.execution_mode,
                    skill_id=current_skill,
                    error_id=failure["error_id"],
                    error_type=type(error).__name__,
                    failure=stage_failure,
                )
                raise

    def _run_stage(
        self,
        run_id: str,
        skill_id: str,
        inputs: Mapping[str, Any],
        context_payload: Mapping[str, Any],
        request: DirectorCoordinatorRequest,
        state: dict[str, Any],
        outputs: dict[str, Any],
        stages: list[DirectorStageRecord],
    ) -> None:
        self._check_cancelled(run_id)
        node_id = skill_id.removeprefix("comic.")
        event_name = node_id
        trace_id = str(state["trace_id"])
        state["active_skill_id"] = skill_id
        state["stage_statuses"][skill_id] = "running"
        debug = {"input": _fingerprint(inputs), "context": _fingerprint(context_payload),
                 "input_versions": state["input_versions"],
                 "source": "user_edit" if node_id in request.stage_edits else "execution"}
        state["director_debug"]["stages"][skill_id] = debug
        logger.info("Director Debug Stage skill_id={} input={} context={} source={}",
                    skill_id, debug["input"], debug["context"], debug["source"])
        self.runtime_store.update_run(
            run_id, status=ExecutionStatus.RUNNING, state=state, current_node=node_id,
        )
        self._event(
            run_id, RuntimeEventType.NODE_PROGRESS, "director_stage_visible",
            trace_id=trace_id, project_id=request.snapshot.project.project_id,
            mode=request.execution_mode,
            visible_stage=node_id if request.execution_mode == "professional"
            else FAST_LABELS[node_id],
        )
        if skill_id != "comic.director_critic":
            self._event(
                run_id, RuntimeEventType.NODE_STARTED, f"{event_name}_started",
                trace_id=trace_id, project_id=request.snapshot.project.project_id,
                execution_mode=request.execution_mode, skill_id=skill_id,
            )
        stage_context = {
            "trace_id": trace_id,
            "run_id": run_id,
            "project_id": request.snapshot.project.project_id,
            "execution_mode": request.execution_mode,
            "input_versions": state["input_versions"],
            "context": context_payload,
        }
        with run_trace(run_id, node_id):
            if skill_id == "comic.director_critic":

                def emit(name: str, payload: dict[str, Any]) -> None:
                    self._check_cancelled(run_id)
                    state["critic_activity"] = name
                    if "critic_result" in payload:
                        state["critic_result"] = payload["critic_result"]
                    if "director_candidate" in payload:
                        state["director_candidate"] = payload["director_candidate"]
                        state["revision_count"] = payload["revision_count"]
                    if name in {"director_critic_failed", "director_patch_invalid"}:
                        state["error_id"] = payload["error_id"]
                        state["failed_skill_id"] = skill_id
                        state["stage_failures"][skill_id] = {
                            **payload,
                            "stage_name": node_id,
                            "input_version": state["input_versions"],
                            "output_before_failure": dict(state["stage_outputs"]),
                            "exception": {
                                "type": payload["error_type"],
                                "module": payload["exception_module"],
                                "message": payload["safe_message"],
                            },
                        }
                    self.runtime_store.update_run(
                        run_id, status=ExecutionStatus.RUNNING,
                        state=state, current_node=node_id,
                    )
                    self._event(
                        run_id,
                        RuntimeEventType.NODE_PROGRESS,
                        name,
                        **{
                            "trace_id": trace_id,
                            "project_id": request.snapshot.project.project_id,
                            "execution_mode": request.execution_mode,
                            "skill_id": skill_id,
                            **payload,
                        },
                    )

                outcome = self.critic_engine.review_and_revise(
                    DirectorSpecDraft.model_validate(inputs["director_spec"]),
                    request.snapshot.creative_brief,
                    assets=request.assets, storyboard=request.storyboard, shot=request.shot,
                    emit=emit,
                )
                candidate = outcome.director_spec.model_dump()
                state["director_candidate"] = candidate
                state["revision_count"] = outcome.revision_count
                state["needs_review"] = outcome.needs_review
                state["critic_status"] = candidate.get("critic_status")
                state["task_status"] = "needs_review" if outcome.needs_review else "planning"
                state["draft_status"] = "needs_revision" if outcome.needs_review else "reviewed"
                outputs.update({key: candidate[key] for key in (
                    "creative_decision", "director_plan", "cinematography",
                )})
                raw_output = {"critic_result": outcome.critic_result.model_dump()
                              if outcome.critic_result is not None else None}
                if outcome.critic_result is None:
                    raw_output["critic_status"] = "unavailable"
            elif skill_id == "comic.director_assemble":
                raw_output = {"director_spec": self._provisional_spec(
                    outputs, request, critic_result=outputs.get("critic_result"),
                ).model_dump()}
            elif node_id in request.stage_edits:
                output_key = {
                    "creative_understanding": "creative_decision",
                    "visual_direction": "director_plan", "cinematography": "cinematography",
                }[node_id]
                raw_output = {output_key: request.stage_edits[node_id]}
            else:
                try:
                    raw_output = self.stage_executor(skill_id, inputs, stage_context)
                except _DirectorCancelled:
                    raise
                except Exception as error:
                    if skill_id != "comic.cinematography":
                        raise
                    self._check_cancelled(run_id)
                    failure = public_error(
                        error, trace_id=trace_id, project_id=request.snapshot.project.project_id,
                        run_id=run_id, skill_id=skill_id, component="comic.cinematography",
                    )
                    raw_output = {"cinematography": CinematographyPlan(
                        status="missing").model_dump(), "parse_diagnostics": {
                            "failure": failure, "errors": [type(error).__name__],
                            "raw_output": "", "missing_fields": [],
                        }}
            self._check_cancelled(run_id)
            if skill_id == "comic.cinematography":
                diagnostics = raw_output.get("parse_diagnostics", {}) \
                    if isinstance(raw_output, Mapping) else {}
                payload = {key: value for key, value in raw_output.items()
                           if key != "parse_diagnostics"} \
                    if isinstance(raw_output, Mapping) else raw_output
                camera, normalized = parse_cinematography(payload)
                diagnostics = {**normalized, **diagnostics,
                               "missing_fields": camera.missing_fields, "status": camera.status}
                raw_output = {"cinematography": camera.model_dump()}
                self._camera_diagnostics(run_id, diagnostics, request, state)
            if not isinstance(raw_output, Mapping):
                raise ToolError("导演 Skill 未返回结构化对象", detail=skill_id)
            validated = self.registry.execute(
                skill_id, {"_contract_output": raw_output}, stage_context,
                store=self.runtime_store, run_id=run_id, node_id=node_id,
            )
        outputs.update(validated)
        debug["output"] = _fingerprint(validated)
        self._event(
            run_id, RuntimeEventType.NODE_PROGRESS, "director_stage_debug",
            trace_id=trace_id, project_id=request.snapshot.project.project_id,
            skill_id=skill_id, debug=debug,
        )
        logger.info("Director Debug Result skill_id={} output={}", skill_id, debug["output"])
        state["stage_outputs"][skill_id] = validated
        camera_incomplete = skill_id == "comic.cinematography" and (
            validated["cinematography"]["status"] != "complete")
        review_failed = skill_id in state["stage_failures"] and not camera_incomplete
        if camera_incomplete:
            state["stage_statuses"][skill_id] = "needs_revision"
        elif skill_id == "comic.director_critic" and state.get("critic_status") == "unavailable":
            state["stage_statuses"][skill_id] = "unavailable"
        elif review_failed:
            state["stage_statuses"][skill_id] = "failed"
        else:
            state["stage_statuses"][skill_id] = (
                "needs_revision"
                if skill_id == "comic.director_critic" and state.get("needs_review")
                else "completed")
        if not review_failed and not camera_incomplete:
            state["completed_stages"].append(skill_id)
            state["last_completed_step"] = node_id
        state["task_status"] = "planning" if not state.get("needs_review") else "needs_review"
        stages.append(
            DirectorStageRecord(
                skill_id=skill_id,
                status=state["stage_statuses"][skill_id],
                output_keys=sorted(validated),
            )
        )
        self.runtime_store.update_run(
            run_id, status=ExecutionStatus.RUNNING, state=state, current_node=node_id,
        )
        if skill_id != "comic.director_critic" and not camera_incomplete:
            self._event(
                run_id, RuntimeEventType.NODE_COMPLETED, f"{event_name}_completed",
                trace_id=trace_id, project_id=request.snapshot.project.project_id,
                execution_mode=request.execution_mode, skill_id=skill_id,
                output_keys=sorted(validated),
            )
        self._event(
            run_id,
            RuntimeEventType.NODE_PROGRESS,
            "node_warning" if camera_incomplete else (
                "node_warning" if state.get("critic_status") == "unavailable" and review_failed
                else "director_stage_failed" if review_failed else "director_stage_completed"),
            trace_id=trace_id,
            project_id=request.snapshot.project.project_id,
            mode=request.execution_mode,
            stage_status=state["stage_statuses"][skill_id],
            visible_stage=node_id
            if request.execution_mode == "professional"
            else FAST_LABELS[node_id],
        )

    def _camera_diagnostics(
        self, run_id: str, diagnostics: dict[str, Any], request: DirectorCoordinatorRequest,
        state: dict[str, Any],
    ) -> None:
        """摄影缺失可保留草稿，但不能成为已完成检查点或绕过审核。"""
        skill_id = "comic.cinematography"
        metadata = {"skill_id": skill_id, "trace_id": state["trace_id"],
                    "project_id": request.snapshot.project.project_id,
                    "input_versions": state["input_versions"], **diagnostics}
        state["director_debug"]["stages"][skill_id]["parsing"] = metadata
        if diagnostics.get("errors") or diagnostics.get("failure"):
            self._event(run_id, RuntimeEventType.NODE_PROGRESS,
                        "schema_validation_failed", **metadata)
        if diagnostics["status"] != "complete":
            failure = diagnostics.get("failure")
            if not failure:
                try:
                    raise ToolError("摄影方案不完整，已保留公开输出，需要修订")
                except ToolError as error:
                    failure = public_error(
                        error, trace_id=state["trace_id"], run_id=run_id, skill_id=skill_id,
                        project_id=request.snapshot.project.project_id,
                    )
            state["stage_failures"][skill_id] = {
                **failure, "stage_name": "cinematography", "status": "needs_revision",
                "input_version": state["input_versions"],
                "output_before_failure": dict(state["stage_outputs"]),
                "missing_fields": diagnostics["missing_fields"],
                "raw_output": diagnostics.get("raw_output", ""),
                "exception": {"type": "CinematographyOutputError",
                              "message": failure["safe_message"]},
            }
            state["error_id"] = failure["error_id"]
            metadata["error_id"] = failure["error_id"]
            self._event(run_id, RuntimeEventType.NODE_PROGRESS, "node_warning", **metadata)
        self._event(run_id, RuntimeEventType.NODE_PROGRESS, "stage_output_saved", **metadata)
        logger.info("Cinematography parse status={} missing_fields={} trace_id={}",
                    diagnostics["status"], diagnostics["missing_fields"], state["trace_id"])
        self.runtime_store.update_run(run_id, status=ExecutionStatus.RUNNING,
                                      state=state, current_node="cinematography")

    def _check_cancelled(self, run_id: str) -> None:
        if self.runtime_store.get_run(run_id).status is ExecutionStatus.CANCELLED:
            raise _DirectorCancelled

    def _reused_outputs(
        self, request: DirectorCoordinatorRequest, input_versions: Mapping[str, int],
    ) -> dict[str, dict[str, Any]]:
        if request.review_draft is not None:
            draft = request.review_draft
            if (draft.schema_version != 2 or draft.asset_versions != {
                key: value for key, value in input_versions.items() if key.startswith("asset:")
            } or draft.storyboard_version != input_versions.get("storyboard")
                    or draft.shot_version != input_versions.get("shot")):
                raise ToolError("待审核草稿的资产或镜头版本已变化")
            return {
                "comic.creative_understanding": {
                    "creative_decision": draft.creative_decision.model_dump()},
                "comic.visual_direction": {"director_plan": draft.director_plan.model_dump()},
                "comic.cinematography": {"cinematography": draft.cinematography.model_dump()},
            }
        if request.previous_run_id is None or request.rerun_from is None:
            return {}
        previous = self.runtime_store.get_run(request.previous_run_id)
        interrupted = (
            previous.status is ExecutionStatus.RUNNING and request.worker_instance_id
            and previous.state.get("worker_instance_id") != request.worker_instance_id
        )
        if (
            previous.domain != "comic"
            or previous.workflow != "comic.director"
            or (not interrupted and previous.status not in {
                ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.WAITING,
            })
            or previous.state.get("project_id") != request.snapshot.project.project_id
            or previous.state.get("execution_mode") != request.execution_mode
            or previous.state.get("task") != request.task
            or previous.state.get("input_versions") != dict(input_versions)
            or previous.state.get("input_brief_id", request.snapshot.creative_brief.brief_id)
            != request.snapshot.creative_brief.brief_id
            or previous.state.get("input_brief_version", input_versions["creative_brief"])
            != input_versions["creative_brief"]
            or previous.state.get("storyboard_id") != (
                request.storyboard.storyboard_id if request.storyboard else None
            )
            or previous.state.get("shot_id") != (request.shot.shot_id if request.shot else None)
        ):
            raise ToolError("来源导演 Run 与当前作品或输入版本不一致")
        if request.execution_mode == "fast":
            completed = previous.state.get("completed_stages", [])
            first_missing = next((stage for stage in DIRECTOR_STAGES
                                  if f"comic.{stage}" not in completed), "director_critic")
            if (previous.status is ExecutionStatus.COMPLETED
                    or request.rerun_from != first_missing):
                raise ToolError("快速模式仅允许恢复未完成步骤")
        order = [
            "comic.creative_understanding", "comic.visual_direction",
            "comic.cinematography", "comic.director_critic",
            "comic.director_assemble",
        ]
        start = order.index(f"comic.{request.rerun_from}")
        prior_outputs = previous.state.get("stage_outputs")
        if not isinstance(prior_outputs, dict):
            raise ToolError("来源导演 Run 缺少阶段结果")
        try:
            reused = {
                skill_id: prior_outputs[skill_id] for skill_id in order[:start]
                if skill_id != "comic.director_critic"
            }
        except KeyError as error:
            raise ToolError("来源导演 Run 缺少阶段结果", detail=str(error)) from error
        if any(not isinstance(value, dict) for value in reused.values()):
            raise ToolError("来源导演 Run 阶段结果无效")
        if start >= 3 and previous.state.get("director_candidate") is not None:
            # 同一任务恢复审核时使用最近真实修订，不能退回修订前的阶段输出。
            candidate = DirectorSpecDraft.model_validate(previous.state["director_candidate"])
            for skill_id, field in zip(order[:3], (
                "creative_decision", "director_plan", "cinematography",
            ), strict=True):
                reused[skill_id] = {field: getattr(candidate, field).model_dump(mode="json")}
        return reused

    @staticmethod
    def _input_versions(request: DirectorCoordinatorRequest) -> dict[str, int]:
        versions = {"creative_brief": request.snapshot.creative_brief.version}
        versions.update({f"asset:{asset.asset_id}": asset.version for asset in request.assets})
        if request.storyboard is not None:
            versions["storyboard"] = request.storyboard.version
        if request.shot is not None:
            versions["shot"] = request.shot.version
        return versions

    @staticmethod
    def _context_payload(
        request: DirectorCoordinatorRequest, context: ComicContext,
    ) -> dict[str, Any]:
        payload = context.model_dump(mode="json")
        project = payload["stable_context"].pop("project")
        # 项目标题是可保留的导航元数据，不是当前创意。Brief 更新后旧标题不能污染导演。
        payload["project"] = {"project_id": project["project_id"]}
        payload["creative_brief"] = payload["stable_context"].pop("creative_brief")
        payload["relevant_assets"] = payload.pop("relevant_memory")
        payload.pop("stable_context", None)
        if request.storyboard is not None:
            payload["storyboard"] = request.storyboard.model_dump(mode="json")
        if request.shot is not None:
            payload["shot"] = request.shot.model_dump(mode="json")
        return payload

    @staticmethod
    def _assets_by_kind(assets: list[ComicAsset], kind: str) -> list[dict[str, Any]]:
        return [
            asset.model_dump(mode="json")
            for asset in assets if asset.details.kind == kind
        ]

    @staticmethod
    def _style_asset(assets: list[ComicAsset]) -> dict[str, Any] | None:
        for asset in assets:
            if asset.details.kind == "style":
                return asset.model_dump(mode="json")
        return None

    @staticmethod
    def _shot_context(request: DirectorCoordinatorRequest) -> dict[str, Any]:
        if request.shot is not None:
            return request.shot.model_dump(mode="json")
        return {"task": request.task}

    @staticmethod
    def _provisional_spec(
        outputs: Mapping[str, Any], request: DirectorCoordinatorRequest,
        *, critic_result: Mapping[str, Any] | None,
    ) -> DirectorSpecDraft:
        decision = outputs["creative_decision"]
        plan = outputs["director_plan"]
        camera = outputs["cinematography"]
        return DirectorSpecDraft.model_validate({
            "schema_version": 2,
            # v2 的兼容字段与 Patch Alias 指向同一公开视觉焦点，避免双份值漂移。
            "visual_direction": plan["visual_focus"],
            "storytelling_goal": decision["intent_summary"],
            "camera_language": (
                camera.get("camera_language") or "；".join(
                    camera[key] for key in ("shot_size", "camera_angle", "spatial_feel")
                    if camera.get(key)) or camera.get("public_decision") or "摄影方案待修订"
            ),
            "composition": plan["composition_strategy"],
            "lighting": camera.get("lighting") or "光影方案待修订",
            "color_language": plan["color_strategy"],
            "emotion": decision["emotional_target"],
            "character_focus": plan["visual_focus"],
            "constraints": decision.get("hard_constraints", []),
            "creative_choices": plan.get("creative_choices") or [
                decision["narrative_focus"]
            ],
            "creative_decision": decision,
            "director_plan": plan,
            "cinematography": camera,
            "critic_result": critic_result,
            "asset_versions": {
                f"asset:{asset.asset_id}": asset.version for asset in request.assets
            },
            "storyboard_version": request.storyboard.version if request.storyboard else None,
            "shot_version": request.shot.version if request.shot else None,
        })

    @staticmethod
    def _stale_dependents(
        request: DirectorCoordinatorRequest, input_versions: Mapping[str, int],
    ) -> list[str]:
        prior = request.prior_spec
        if prior is None:
            return []
        prior_versions = dict(prior.asset_versions)
        current_assets = {
            key: value for key, value in input_versions.items() if key.startswith("asset:")
        }
        changed = (
            prior.creative_brief_version != request.snapshot.creative_brief.version
            or prior_versions != current_assets
            or prior.storyboard_version != input_versions.get("storyboard")
            or prior.shot_version != input_versions.get("shot")
        )
        return ["storyboard", "shot", "prompt_artifact"] if changed else []

    def _event(
        self, run_id: str, event_type: RuntimeEventType, director_event: str,
        **payload: Any,
    ) -> None:
        event_payload = {"director_event": director_event, **payload}
        node_id = str(payload.get("skill_id") or director_event)
        self.runtime_store.append_event(
            run_id, event_type, node_id=node_id, payload=event_payload,
        )
