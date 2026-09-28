"""Comic 导演流程协调层。

该模块只协调现有 Core Run、Runtime Event、SkillRegistry 和作品版本存储。
真实模型调用由调用方注入的共享文本 Capability 提供；本模块不导入 Provider。
"""

from __future__ import annotations

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


def director_execution_summary(run: Any) -> dict[str, Any]:
    """用户可见投影；Fast 不传内部节点、输出或审核细节。"""
    mode = run.state["execution_mode"]
    status = run.status.value
    current = (run.current_node or "creative_understanding").removeprefix("comic.")
    label = FAST_LABELS.get(current, "正在设计视觉方案")
    if status == "waiting":
        label = "创作方案需要调整"
    elif status == "failed":
        label = "导演方案生成失败"
    actions = ["view"]
    if status in {"failed", "waiting"}:
        actions.append("resume")
    if mode == "professional" and status in {"completed", "failed", "waiting"}:
        actions.extend(["edit_stage", "rerun_stage"])
    summary: dict[str, Any] = {
        "mode": mode, "current_stage": current if mode == "professional" else label,
        "status_label": label, "available_actions": actions,
        "stages": [], "error_id": run.state.get("error_id"),
    }
    if mode == "professional":
        for stage in DIRECTOR_STAGES:
            key = f"comic.{stage}"
            output = run.state.get("stage_outputs", {}).get(key)
            node_status = "completed" if output is not None else "pending"
            if run.state.get("failed_skill_id") == key:
                node_status = "failed"
            elif current == stage and status in {"running", "waiting"}:
                node_status = status
            public_output = output or {}
            candidate_key = {
                "creative_understanding": "creative_decision",
                "visual_direction": "director_plan", "cinematography": "cinematography",
            }.get(stage)
            candidate = run.state.get("director_candidate", {})
            if candidate_key and candidate_key in candidate:
                public_output = {candidate_key: candidate[candidate_key]}
            summary["stages"].append({
                "stage": stage, "status": node_status,
                "input_versions": run.state["input_versions"],
                "output": public_output,
                "output_summary": next((
                    value.get("public_summary") or value.get("intent_summary")
                    or value.get("visual_strategy") or value.get("lighting")
                    or value.get("visual_direction")
                    for value in public_output.values() if isinstance(value, dict)
                ), None),
            })
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
    status: Literal["completed"] = "completed"
    output_keys: list[str] = Field(default_factory=list)


class DirectorCoordinatorResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: str
    trace_id: str
    project_id: str
    execution_mode: Literal["fast", "professional"]
    director_spec: DirectorSpec | None
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
            "completed_stages": list(reused_outputs),
            "stage_outputs": reused_outputs,
            "last_completed_step": None,
            "rerun_from": request.rerun_from,
            "previous_run_id": request.previous_run_id,
            "storyboard_id": request.storyboard.storyboard_id if request.storyboard else None,
            "shot_id": request.shot.shot_id if request.shot else None,
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

        with request_trace(trace_id), logger.contextualize(
            component="comic.director", project_id=project.project_id,
            run_id=run.id, task_id=run.id, execution_mode=request.execution_mode,
            conversation_id=request.conversation_id or "-",
        ):
            logger.info(
                "director task received versions={} user_input={}", input_versions,
                redact_secrets(
                    request.task or request.snapshot.creative_brief.original_request,
                )[:1000],
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
                self._run_stage(
                    run.id, "comic.director_critic", {
                        "director_spec": provisional.model_dump(mode="json"),
                    }, context_payload, request, state, outputs, stages,
                )
                if state.get("needs_review"):
                    self.runtime_store.update_run(
                        run.id, status=ExecutionStatus.WAITING, state=state,
                        current_node="director_critic",
                    )
                    return DirectorCoordinatorResult(
                        run_id=run.id, trace_id=trace_id, project_id=project.project_id,
                        execution_mode=request.execution_mode, director_spec=None,
                        critic_result=DirectorCriticResult.model_validate(outputs["critic_result"]),
                        needs_review=True, stages=stages,
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
                spec = self.project_store.save_director(
                    project.project_id, draft,
                    expected_project_version=project.current_version,
                    source="model",
                )
                stale = self._stale_dependents(request, input_versions)
                state["director_spec_version"] = spec.version
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
            except Exception as error:
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
                error._kantoku_public_failure = failure
                self.runtime_store.update_run(
                    run.id, status=ExecutionStatus.FAILED, state=state,
                    current_node=current_skill, error=failure["error_id"],
                )
                self._event(
                    run.id, RuntimeEventType.NODE_FAILED, "director_failed",
                    trace_id=trace_id, project_id=project.project_id,
                    execution_mode=request.execution_mode, skill_id=current_skill,
                    error_id=failure["error_id"], error_type=type(error).__name__,
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
        node_id = skill_id.removeprefix("comic.")
        event_name = node_id
        trace_id = str(state["trace_id"])
        state["active_skill_id"] = skill_id
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
                    state["critic_activity"] = name
                    if "critic_result" in payload:
                        state["critic_result"] = payload["critic_result"]
                    self.runtime_store.update_run(
                        run_id, status=ExecutionStatus.RUNNING,
                        state=state, current_node=node_id,
                    )
                    self._event(
                        run_id, RuntimeEventType.NODE_PROGRESS, name,
                        trace_id=trace_id, project_id=request.snapshot.project.project_id,
                        execution_mode=request.execution_mode, skill_id=skill_id, **payload,
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
                state["task_status"] = "needs_review" if outcome.needs_review else "planning"
                outputs.update({key: candidate[key] for key in (
                    "creative_decision", "director_plan", "cinematography",
                )})
                raw_output = {"critic_result": outcome.critic_result.model_dump()}
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
                raw_output = self.stage_executor(skill_id, inputs, stage_context)
            if not isinstance(raw_output, Mapping):
                raise ToolError("导演 Skill 未返回结构化对象", detail=skill_id)
            validated = self.registry.execute(
                skill_id, {"_contract_output": raw_output}, stage_context,
                store=self.runtime_store, run_id=run_id, node_id=node_id,
            )
        outputs.update(validated)
        state["stage_outputs"][skill_id] = validated
        state["completed_stages"].append(skill_id)
        state["last_completed_step"] = node_id
        state["task_status"] = "planning" if not state.get("needs_review") else "needs_review"
        stages.append(DirectorStageRecord(
            skill_id=skill_id, output_keys=sorted(validated),
        ))
        self.runtime_store.update_run(
            run_id, status=ExecutionStatus.RUNNING, state=state, current_node=node_id,
        )
        if skill_id != "comic.director_critic":
            self._event(
                run_id, RuntimeEventType.NODE_COMPLETED, f"{event_name}_completed",
                trace_id=trace_id, project_id=request.snapshot.project.project_id,
                execution_mode=request.execution_mode, skill_id=skill_id,
                output_keys=sorted(validated),
            )
        self._event(
            run_id, RuntimeEventType.NODE_PROGRESS, "director_stage_completed",
            trace_id=trace_id, project_id=request.snapshot.project.project_id,
            mode=request.execution_mode,
            visible_stage=node_id if request.execution_mode == "professional"
            else FAST_LABELS[node_id],
        )

    def _reused_outputs(
        self, request: DirectorCoordinatorRequest, input_versions: Mapping[str, int],
    ) -> dict[str, dict[str, Any]]:
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
        payload["project"] = payload["stable_context"].pop("project")
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
            "visual_direction": plan["visual_strategy"],
            "storytelling_goal": decision["intent_summary"],
            "camera_language": (
                f"{camera['shot_size']}；{camera['camera_angle']}；"
                f"{camera['spatial_feel']}"
            ),
            "composition": plan["composition_strategy"],
            "lighting": camera["lighting"],
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
            prior_versions != current_assets
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
