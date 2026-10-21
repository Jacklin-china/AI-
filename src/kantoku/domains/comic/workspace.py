"""Work containers and stable image-production contexts in the existing Project revisions."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from kantoku.config import ToolError
from kantoku.config.observability import public_error
from kantoku.config.runtime_paths import get_runtime_paths, validate_runtime_path
from kantoku.core.conversation_context import token_size
from kantoku.core.conversations import InteractionMode, MessageRole, MessageType
from kantoku.core.database import database_manager
from kantoku.core.runtime.models import ExecutionStatus, utc_now

from .director_provenance import value_hash
from .models import (
    ComicImageImportRequest,
    ComicImageSelectionRequest,
    ComicImageTaskCreate,
    ComicLegacyImportRequest,
    ComicPlanRecovery,
    ComicProjectInput,
    ComicPromptContexts,
    ComicStorySaveRequest,
    ComicTaskSettingsSync,
    ComicWorkCreate,
    ComicWorkSettingWrite,
    ComicWorkWrite,
    CreativeBriefInput,
    DirectorSpecDraft,
    ImageContinuityChange,
    ImageContinuityDecision,
    ImageVersionRef,
)
from .projects import ComicContextBuilder


class ComicWorkspaceService:
    """Organization only; production continues through the original domain services."""

    def __init__(self, media, assets, prompts=None):
        self.media, self.assets = media, assets
        self.projects, self.runtime = media.projects, media.runtime
        self.prompts = prompts

    def _latest(self, connection, project_id, kind, entity_id=None):
        return self.media._latest(connection, project_id, kind, entity_id or project_id)

    def _append(self, connection, project_id, kind, payload, entity_id=None):
        entity_id = entity_id or project_id
        version = self._latest(connection, project_id, kind, entity_id)[0] + 1
        self.projects._insert_version(
            connection,
            project_id=project_id,
            entity_type=kind,
            entity_id=entity_id,
            version=version,
            payload_json=json.dumps(payload, ensure_ascii=False),
            created_at=utc_now(),
        )
        return version

    @staticmethod
    def _cas(connection, project_id, expected):
        row = connection.execute(
            "SELECT current_version FROM comic_projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if row is None or int(row[0]) != expected:
            raise ToolError("作品或画面已更新，修改已保留，请重新读取后保存")

    def _replay(self, connection, project_id, operation, request):
        rows = connection.execute(
            "SELECT payload_json FROM comic_entity_versions "
            "WHERE entity_type='workspace_request' AND entity_id=? "
            "AND (? IS NULL OR project_id=?) ORDER BY version DESC",
            (value_hash(request.request_id), project_id, project_id),
        ).fetchall()
        if not rows:
            return None
        saved = json.loads(rows[0][0])
        if saved["fingerprint"] != value_hash(
            {"operation": operation, "request": request.model_dump(mode="json")}
        ):
            raise ToolError("请求 ID 已用于不同内容，请使用新请求")
        return saved["result"]

    def _finish(self, connection, project_id, operation, request, result):
        self._append(
            connection,
            project_id,
            "workspace_request",
            {
                "fingerprint": value_hash(
                    {"operation": operation, "request": request.model_dump(mode="json")}
                ),
                "result": result,
            },
            value_hash(request.request_id),
        )
        return result

    def _work(self, connection, work_id):
        record = self._latest(connection, work_id, "workspace_work")[1]
        if not record or record.get("deleted_at"):
            raise ToolError("找不到作品容器；历史记录请先预览归入作品")
        return record

    def work_binding(self, project_id):
        with self.projects._connect() as connection:
            return self._latest(connection, project_id, "workspace_work")[1] or None

    def work_for_story(self, conversation_id, *, connection=None):
        if connection is None:
            with self.projects._connect() as opened:
                return self.work_for_story(conversation_id, connection=opened)
        row = connection.execute(
            "SELECT project_id FROM comic_entity_versions "
            "WHERE entity_type='workspace_work' "
            "AND json_extract(payload_json,'$.story_conversation_id')=? LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return row[0] if row else None

    def task_binding(self, task_id, *, connection=None):
        if connection is None:
            with self.projects._connect() as opened:
                return self.task_binding(task_id, connection=opened)
        return self._latest(connection, task_id, "workspace_task")[1] or None

    def require_task(self, work_id, task_id, conversation_id=None, *, connection=None):
        if connection is None:
            with self.projects._connect() as opened:
                return self.require_task(work_id, task_id, conversation_id, connection=opened)
        self._work(connection, work_id)
        task = self.task_binding(task_id, connection=connection)
        if (
            not task
            or task["work_id"] != work_id
            or (conversation_id and task["conversation_id"] != conversation_id)
        ):
            raise ToolError("画面、作品或对话归属不一致，不能切换到其他作品执行")
        return task

    def create(self, data):
        request = ComicWorkCreate.model_validate(data)
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if saved := self._replay(connection, None, "work.create", request):
                return saved
            project = self.projects.create(
                ComicProjectInput(title=request.title), connection=connection
            )
            work_id = project.project.project_id
            # Story and dialogue have separate, durable identities.
            conversation = self.runtime.create_conversation(
                request.title,
                interaction_mode=InteractionMode.GUIDED,
                domain="comic",
                connection=connection,
            )
            dialogue = self.runtime.create_conversation(
                request.title, interaction_mode=InteractionMode.GUIDED,
                domain="comic", connection=connection,
            )
            self._append(
                connection,
                work_id,
                "workspace_work",
                {
                    "work_id": work_id,
                    "title": request.title,
                    "auto_title": request.title == "未命名作品",
                    "story_conversation_id": conversation.id,
                    "conversation_id": dialogue.id,
                },
            )
            self._append(connection, work_id, "media_owner", {"conversation_id": conversation.id})
            return self._finish(
                connection,
                work_id,
                "work.create",
                request,
                self.get(work_id, connection=connection),
            )

    def get(self, work_id, *, connection=None):
        if connection is None:
            with self.projects._connect() as opened:
                return self.get(work_id, connection=opened)
        record = self._work(connection, work_id)
        project_row = connection.execute(
            "SELECT current_version,updated_at FROM comic_projects WHERE project_id=?", (work_id,)
        ).fetchone()
        version = int(project_row[0])
        rows = connection.execute(
            "SELECT project_id FROM comic_entity_versions v WHERE entity_type='workspace_task' "
            "AND version=(SELECT MAX(version) FROM comic_entity_versions "
            "WHERE project_id=v.project_id "
            "AND entity_type=v.entity_type AND entity_id=v.entity_id) "
            "AND json_extract(payload_json,'$.work_id')=? ORDER BY created_at,project_id",
            (work_id,),
        ).fetchall()
        migration = connection.execute(
            "SELECT payload_json FROM comic_entity_versions WHERE project_id=? "
            "AND entity_type='legacy_import' ORDER BY version DESC LIMIT 1",
            (work_id,),
        ).fetchone()
        progress = json.loads(migration[0]) if migration else None
        recoveries = connection.execute(
            "SELECT payload_json FROM comic_entity_versions WHERE project_id=? "
            "AND entity_type='workspace_plan_source'", (work_id,),
        ).fetchall()
        return {
            **record,
            "conversation_id": record.get("conversation_id") or (
                self.task_binding(rows[0][0], connection=connection)["conversation_id"]
                if rows else record["story_conversation_id"]),
            "version": version,
            "updated_at": project_row[1],
            "tasks": [self.get_task(work_id, row[0], connection=connection) for row in rows],
            "plan_recoveries": [json.loads(row[0]) for row in recoveries],
            "migration": {"request": progress["request"], "completed": progress["completed"]}
            if progress
            else None,
        }

    def list(self):
        with self.projects._connect() as connection:
            rows = connection.execute(
                "SELECT project_id FROM comic_entity_versions v "
                "WHERE entity_type='workspace_work' AND version=(SELECT MAX(version) "
                "FROM comic_entity_versions WHERE project_id=v.project_id "
                "AND entity_type=v.entity_type AND entity_id=v.entity_id) "
                "AND json_extract(payload_json,'$.deleted_at') IS NULL ORDER BY created_at DESC"
            ).fetchall()
            return {"works": [self.get(row[0], connection=connection) for row in rows]}

    def conversation_ids(self):
        """Workspace conversations are entered through their fixed work/task binding."""
        with self.projects._connect() as connection:
            rows = connection.execute(
                "SELECT json_extract(payload_json,'$.conversation_id'), "
                "json_extract(payload_json,'$.story_conversation_id') "
                "FROM comic_entity_versions WHERE entity_type IN "
                "('workspace_work','workspace_task')"
            ).fetchall()
            return {value for row in rows for value in row if value}

    def delete(self, work_id, data):
        request = ComicWorkWrite.model_validate(data)
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if saved := self._replay(connection, work_id, "work.delete", request):
                return saved
            record = self._work(connection, work_id)
            self._cas(connection, work_id, request.expected_version)
            self._append(connection, work_id, "workspace_work",
                         {**record, "deleted_at": utc_now().isoformat()})
            self.projects._advance_project(connection, work_id, request.expected_version)
            return self._finish(connection, work_id, "work.delete", request, {"deleted": True})

    def conversation(self, work_id):
        work = self.get(work_id)
        mappings = {item["source_run_id"]: item["run_id"] for item in work["plan_recoveries"]}
        projects = {task["project_id"] for task in work["tasks"]
                    if not task.get("history_source_only")}
        owners = {work["conversation_id"], *(task["conversation_id"] for task in work["tasks"])}
        messages, run_ids = {}, set()
        for owner in owners:
            for message in self.runtime.list_conversation_messages(owner):
                record = message.model_dump(mode="json")
                if message.run_id in mappings:
                    record.update(source_run_id=message.run_id, run_id=mappings[message.run_id])
                messages[message.id] = record
            # 作品集合过滤下沉到 SQL，避免取出全部 Run 再逐个解析 state_json。
            run_ids.update(run.id for run in self.runtime.list_runs(
                domain="comic", conversation_id=owner, project_ids=projects, limit=100000))
        return {"conversation_id": work["conversation_id"],
                "messages": sorted(messages.values(),
                                   key=lambda item: (item["created_at"], item["id"])),
                "related_run_ids": sorted(run_ids)}

    def recover_plans(self, work_id, data):
        """Append exact source revisions and dialogue mappings; never rewrite original records."""
        request = ComicPlanRecovery.model_validate(data)
        recovery_id = value_hash(request.request_id)
        fingerprint = value_hash(request.model_dump(mode="json"))
        with self.projects._connect() as connection:
            if saved := self._replay(connection, work_id, "work.recover_plans", request):
                return saved
            progress = self._latest(connection, work_id, "workspace_plan_recovery", recovery_id)[1]
            if progress:
                if progress["fingerprint"] != fingerprint:
                    raise ToolError("恢复请求 ID 已用于不同来源")
            else:
                self._cas(connection, work_id, request.expected_version)
        work = self.get(work_id)
        identifiers = [identifier for group in request.source_run_groups for identifier in group]
        if (any(not group for group in request.source_run_groups)
                or len(identifiers) != len(set(identifiers))):
            raise ToolError("恢复分组不能为空或重复使用同一来源")
        sources = {}
        for identifier in identifiers:
            run = self.runtime.get_run(identifier)
            self.require_task(work_id, run.state.get("project_id"),
                              run.state.get("conversation_id"))
            if run.workflow not in {"comic.director", "comic.director-spec"}:
                raise ToolError("恢复来源必须是具有确定版本的导演记录")
            spec = self.projects.get_director(
                run.state["project_id"], version=run.state["director_spec_version"],
                spec_id=run.state["director_spec_id"])
            sources[identifier] = (run, spec)
        for group in request.source_run_groups:
            revisions = [sources[identifier][1] for identifier in group]
            if (len({item.project_id for item in revisions}) != 1
                    or [item.version for item in revisions]
                    != sorted({item.version for item in revisions})):
                raise ToolError("同一恢复分组必须是同一方案按顺序保存的不同修订")
        backup = Path(progress["backup"]) if progress else database_manager.backup(
            self.projects.path, owner="comic.workspace.recover_plans")
        if not progress:
            with self.projects._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._cas(connection, work_id, request.expected_version)
                self._append(connection, work_id, "workspace_plan_recovery",
                             {"fingerprint": fingerprint, "backup": str(backup)}, recovery_id)
        for index, group in enumerate(request.source_run_groups):
            root, first = sources[group[0]]
            briefs = self.projects.brief_versions(first.project_id)
            brief = next(item for item in briefs if item.version == first.creative_brief_version)
            key = value_hash({"request": request.request_id, "group": index})
            child = self._legacy_step(work_id, key + ":task",
                lambda step, key=key, root=root, brief=brief: self.create_task(
                work_id, {"request_id": key, "expected_version": step["expected_version"],
                          "title": str(root.state.get("task") or "恢复的方案")[:28],
                          "requirement": str(root.state.get("task") or brief.original_request)},
                source_brief={**brief.model_dump(mode="json"),
                              "original_request": root.state.get("task") or brief.original_request},
                conversation_id=work["conversation_id"]))
            for identifier in group:
                run, spec = sources[identifier]
                source_key = key + ":" + identifier
                with self.projects._connect() as connection:
                    if self._latest(connection, work_id, "workspace_plan_source", identifier)[0]:
                        continue
                self._import_legacy_director(work_id, source_key, child, {
                    "project_id": spec.project_id, "brief_version": spec.creative_brief_version,
                    "director": spec.model_dump(mode="json"), "director_run_id": run.id,
                    "conversation_id": run.state["conversation_id"],
                    "work_id": f"{spec.project_id}:brief:{spec.creative_brief_version}",
                })
                current = self.projects.get(child["project_id"])
                restored = self.projects.get_director(child["project_id"])
                state = {"project_id": child["project_id"],
                             "conversation_id": work["conversation_id"], "task_system": True,
                             "task": run.state.get("task"), "execution_mode": "professional",
                             "generation_mode": "FIRST_GENERATION" if identifier == group[0]
                             else run.state.get("generation_mode", "USER_EDIT"),
                             "generation_incomplete": True, "director_spec_id": restored.spec_id,
                             "director_spec_version": restored.version,
                             "project_version_after": current.project.current_version,
                             "input_versions": {"creative_brief": current.creative_brief.version},
                             "source_run_id": run.id, "stage_outputs": {}, "completed_stages": [],
                             "creative_context": {"workspace_work_id": work_id,
                                                  "restored_source": run.id},
                             "source_trace_id": run.state.get("trace_id")}
                recovery_run = self._legacy_step(
                    work_id, source_key + ":run",
                    lambda _step, state=state: self._recovery_run(state))
                with self.projects._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._append(connection, work_id, "workspace_plan_source", {
                        "source_run_id": identifier, "source_project_id": spec.project_id,
                        "source_director_id": spec.spec_id, "source_version": spec.version,
                        "task_id": child["project_id"], "run_id": recovery_run["run_id"],
                        "version": restored.version, "backup": str(backup),
                    }, identifier)
            with self.projects._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                binding = self.task_binding(first.project_id, connection=connection)
                self._append(connection, first.project_id, "workspace_task",
                             {**binding, "history_source_only": True})
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._finish(connection, work_id, "work.recover_plans", request,
                                self.get(work_id, connection=connection))

    def _recovery_run(self, state):
        existing = next((run for run in self.runtime.list_runs(
            domain="comic", conversation_id=state["conversation_id"], limit=100000)
            if run.state.get("project_id") == state["project_id"]
            and run.state.get("source_run_id") == state["source_run_id"]), None)
        run = existing or self.runtime.create_run(
            "comic", "comic.director", state, "director_critic",
            interaction_mode=InteractionMode.GUIDED)
        self.runtime.update_run(run.id, status=ExecutionStatus.WAITING,
                                state=state, current_node="director_critic")
        return {"run_id": run.id}

    def image_continuity(self, work_id, request, resolution, *, model_call, cutoff, budget):
        """从已采用图片及明确引用的方案恢复锚点，不受聊天窗口或轮数限制。"""
        work = self.get(work_id)
        tasks = {item["task_id"]: item for item in work["tasks"]}
        candidates = {}
        descriptions = {}
        selected = {}
        for source in resolution.sources:
            if source.kind != "run":
                continue
            run = self.runtime.get_run(source.id)
            project_id = run.state.get("project_id")
            if project_id in tasks and source.version:
                selected[project_id] = self.projects.get_director(
                    project_id, version=source.version, spec_id=run.state.get("director_spec_id"))
        # 先读引用方案自己的采用图；没有新采用图时，继续其不可变图片来源。
        for project_id in selected or tasks:
            adopted = [item for item in self.media.list(project_id)
                       if item.get("selection")]
            anchors = []
            for family in adopted:
                choice = family["selection"]
                with self.projects._connect() as connection:
                    row = connection.execute(
                        "SELECT created_at FROM comic_entity_versions WHERE project_id=? "
                        "AND entity_type='image_selection' AND entity_id=? AND version=?",
                        (project_id, family["image_id"], choice["selection_version"])).fetchone()
                if row and row[0] <= cutoff.isoformat():
                    image = self.media.get(family["image_id"], version=choice["selected_version"])
                    anchors.append({"image": image.model_dump(mode="json"),
                                    "selection_version": choice["selection_version"]})
            spec = selected.get(project_id)
            if spec is None and tasks[project_id].get("director_version"):
                spec = self.projects.get_director(project_id)
            if spec and spec.created_at > cutoff:
                continue
            own_adoption = bool(anchors)
            if not anchors and spec and spec.reference_context.get("continuity"):
                anchors.append(spec.reference_context["continuity"])
            for anchor in anchors:
                image = anchor["image"]
                self.require_task(work_id, image["project_id"], image["conversation_id"])
                # 最新方案的有效 Prompt 承载已明确完成的局部修改，不能回退到首图文本。
                prompt = None
                if spec and not own_adoption:
                    with self.projects._connect() as connection:
                        rows = connection.execute(
                            "SELECT shot_id FROM comic_prompts WHERE project_id=? "
                            "AND json_extract((SELECT payload_json FROM comic_entity_versions v "
                            "WHERE v.project_id=comic_prompts.project_id "
                            "AND v.entity_type='prompt' "
                            "AND v.entity_id=comic_prompts.prompt_id "
                            "AND v.version=comic_prompts.current_version), "
                            "'$.director_spec_version')=? AND updated_at<=?",
                            (project_id, spec.version, cutoff.isoformat())).fetchall()
                    if len(rows) == 1:
                        prompt = self.prompts.get(rows[0][0])
                    elif len(rows) > 1:
                        raise ToolError("引用方案有多个画面，请明确要沿用哪张已采用图片")
                if prompt is None and image.get("instruction_artifact_id"):
                    previous_source = anchor.get("prompt_source") if not own_adoption else None
                    if previous_source:
                        self.require_task(work_id, previous_source["project_id"])
                        prompt = self.prompts.get(previous_source["shot_id"],
                                                  version=previous_source["version"])
                        if (prompt.project_id != previous_source["project_id"]
                                or prompt.artifact_id != previous_source["artifact_id"]):
                            raise ToolError("历史图片延续的 Prompt 来源不一致")
                if prompt is None and image.get("instruction_artifact_id"):
                    artifact = self.runtime.get_artifact(image["instruction_artifact_id"])
                    if artifact.metadata.get("prompt_id"):
                        prompt = self.prompts.get(artifact.metadata["shot_id"],
                                                  version=artifact.version)
                        if prompt.artifact_id != artifact.id:
                            raise ToolError("已采用图片的 Prompt 来源不一致")
                        if prompt.project_id != image["project_id"]:
                            raise ToolError("已采用图片的 Prompt 不属于其来源画面")
                if prompt and prompt.created_at > cutoff:
                    raise ToolError("图片延续不能读取本轮请求之后的 Prompt")
                source = ({"project_id": prompt.project_id, "shot_id": prompt.shot_id,
                           "prompt_id": prompt.prompt_id,
                           "version": prompt.version, "artifact_id": prompt.artifact_id}
                          if prompt and prompt.context_version else None)
                key = project_id + ":" + image["artifact_id"]
                candidates[key] = {"image": image, "selection_version": anchor["selection_version"],
                    "prompt_source": source, "source_task_id": project_id,
                    "source_director_id": spec.spec_id if spec else None,
                    "source_director_version": spec.version if spec and prompt
                        and prompt.director_spec_version == spec.version else None}
                descriptions[key] = {"source_request": tasks[project_id].get("requirement")
                    or tasks[project_id]["title"], "prompt_sections": {
                        section: getattr(prompt, section)
                        for section in ComicPromptContexts.model_fields
                    } if prompt else {}, "description_status": "prompt_basis_not_image_observation"}
        if not candidates:
            return None
        schema = ImageContinuityDecision.model_json_schema()
        schema["properties"]["candidate_id"]["anyOf"][0]["enum"] = list(candidates)
        messages = [{"role": "system", "content": (
            "判断本轮创作对已采用图片的延续及逐字段修改权限，不生成方案。"
            "已采用图是视觉基准；除用户明确提出的变化，其余人物、数量、外貌、服装、"
            "场景结构、道具、画风、构图和摄影都保留。动作/神态变化只释放实际涉及的"
            "shot_context 叶子，不释放人物或世界。局部换服装不能释放外貌、身份或场景。"
            "fields 来自原有 ComicPromptContexts：" + json.dumps({key: list(
                field.annotation.model_fields)
                for key, field in ComicPromptContexts.model_fields.items()})
            + "。根据本轮原话与有来源的 resolution 选择 candidate_id，不能按最新或列表顺序选择。"
            "candidate_id 逐字使用 candidates 中的完整键，不要只返回图片或产物 ID。"
            "changes 每项 quote 必须逐字来自 current_request，并确实授权该字段改变；"
            "未提及的变化不填写，保持站姿/场景等不是修改授权。"
            "field 必须使用 Schema 中枚举的完整叶子路径，不得填写整段、别名或自创字段。"
            "如收到 repair，只纠正指出的结构问题；fixed 与 change_count 不可改写，"
            "不得借纠正更换图片、重做判断或扩大用户修改范围。"
            "若 repair 给出 alternatives，须在原图片和原独立依据两条分支中消除冲突："
            "选择原图片时保留 fixed/条目数量且 new_context_quote=null；"
            "选择独立画面时 candidate_id=null、原独立依据不变、changes=[]。"
            "明确独立新题材或完全重做时 candidate_id=null，new_context_quote 填对应本轮原话。"
            "只改变某元素仍选原图并列出相应变化。无法唯一判断则给 question，不猜测。"
            "只返回符合 Schema 的公开 JSON："
            + json.dumps(schema, ensure_ascii=False))},
            {"role": "user", "content": json.dumps({"stage": "confirmed_image_continuity",
                "current_request": request, "resolution": resolution.model_dump(mode="json"),
                "candidates": {key: {**candidate, **descriptions[key], "candidate_id": key}
                               for key, candidate in candidates.items()}}, ensure_ascii=False)}]
        decision = self._continuity_decision(
            work_id, messages, request, candidates, model_call=model_call, budget=budget)
        if decision.question:
            raise ToolError(decision.question)
        if decision.candidate_id is None:
            if (decision.new_context_quote and decision.new_context_quote in request
                    and not decision.changes):
                return {"new_context": True}
            raise ToolError("请明确这次要沿用哪张图片，或是否开始独立的新画面")
        if decision.candidate_id not in candidates or decision.new_context_quote:
            raise ToolError("图片延续来源无效，不能替换已采用图片")
        if any(item.quote not in request for item in decision.changes):
            raise ToolError("图片修改范围缺少本轮用户原话依据")
        chosen = candidates[decision.candidate_id]
        self.require_continuity(work_id, chosen)
        return {**chosen, "changes": [item.model_dump() for item in decision.changes]}

    @staticmethod
    def _continuity_decision(work_id, messages, request, candidates, *, model_call, budget):
        """Repair received structure once, retaining validated decisions and source checks."""
        aliases = {}
        for key, candidate in candidates.items():
            for name in ("image_id", "artifact_id"):
                value = candidate["image"].get(name)
                if value:
                    aliases.setdefault(value, set()).add(key)
        context = {"candidate_ids": set(candidates),
                   "candidate_aliases": {key: tuple(values) for key, values in aliases.items()}}
        repair = None
        for attempt in range(2):
            if token_size(messages) > budget:
                raise ToolError("已采用图片来源超过上下文容量，请明确引用要沿用的画面")
            # Transport errors have no received decision to correct and must never resubmit.
            raw = model_call(messages)
            try:
                decision = ImageContinuityDecision.model_validate_json(raw, context=context)
            except ValidationError as error:
                known = {"candidate_id", "changes", "field", "quote", "new_context_quote",
                         "question"}
                issues = [{"path": [part if isinstance(part, int) or part in known else
                                     "<unknown>" for part in item["loc"]], "type": item["type"]}
                          for item in error.errors(include_input=False, include_context=False,
                                                   include_url=False)[:20]]
                failure = ToolError("图片延续判断未返回有效结构，已保留原图，请重试",
                                    detail=json.dumps(issues, ensure_ascii=False))
                if attempt:
                    raise failure from None
                public_error(failure, component="comic.image_continuity", project_id=work_id,
                             phase="structure_correction", response_sha256=value_hash(raw))
            else:
                if repair:
                    fixed = repair["fixed"]
                    independent = False
                    if alternatives := repair.get("alternatives"):
                        independent = (decision.candidate_id is None
                            and decision.new_context_quote == alternatives["new_context_quote"]
                            and not decision.changes)
                        if not (independent or (
                            decision.candidate_id == alternatives["candidate_id"]
                            and decision.new_context_quote is None)):
                            raise ToolError("图片延续结构纠正改写了已核验决定，已保留原图")
                    if not independent and (any(
                            getattr(decision, name) != value for name, value in fixed.items()
                            if name != "changes")
                            or ("change_count" in repair
                                and len(decision.changes) != repair["change_count"])
                            or any(index >= len(decision.changes)
                                   or any(getattr(decision.changes[index], name) != value
                                          for name, value in item["change"].items())
                                   for item in fixed.get("changes", [])
                                   for index in [item["index"]])):
                        raise ToolError("图片延续结构纠正改写了已核验决定，已保留原图")
                return decision
            # Only schema-validated public fragments can enter the correction request.
            # Invalid output, unknown keys and private reasoning never leave this scope.
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                data = None
            fixed = {}
            repair = {"issues": issues, "fixed": fixed}
            if isinstance(data, dict):
                candidate = None
                if isinstance(data.get("candidate_id"), str):
                    try:
                        candidate = ImageContinuityDecision.model_validate(
                            {"candidate_id": data["candidate_id"]}, context=context).candidate_id
                    except ValidationError:
                        raise ToolError(
                            "图片延续来源无效或对应多个方案，请明确要沿用的画面") from None
                    fixed["candidate_id"] = candidate
                    fixed["new_context_quote"] = None
                for name in ("question", "new_context_quote"):
                    value = data.get(name)
                    if isinstance(value, str) and value.strip():
                        try:
                            ImageContinuityDecision.model_validate({name: value})
                        except ValidationError:
                            continue
                        if name == "question":
                            raise ToolError(value)
                        if value not in request:
                            raise ToolError("图片修改范围缺少本轮用户原话依据")
                        if candidate is not None:
                            fixed.pop("candidate_id", None)
                            fixed.pop("new_context_quote", None)
                            repair["alternatives"] = {
                                "candidate_id": candidate, "new_context_quote": value}
                        else:
                            fixed.update(candidate_id=None, new_context_quote=value)
                changes = data.get("changes")
                if isinstance(changes, list) and len(changes) <= 30:
                    repair["change_count"] = len(changes)
                    fixed["changes"] = []
                    for index, item in enumerate(changes):
                        if not isinstance(item, dict):
                            continue
                        change = {}
                        allowed = ImageContinuityChange.model_fields["field"].json_schema_extra[
                            "enum"]
                        if item.get("field") in allowed:
                            change["field"] = item["field"]
                        quote = item.get("quote")
                        if isinstance(quote, str) and quote.strip() and len(quote) <= 1000:
                            if quote not in request:
                                raise ToolError("图片修改范围缺少本轮用户原话依据")
                            change["quote"] = quote
                        if change:
                            fixed["changes"].append({"index": index, "change": change})
                if fixed.get("new_context_quote"):
                    # An independent idea cannot apply any change permission to the old image.
                    repair["change_count"] = 0
                    fixed["changes"] = []
            payload = json.loads(messages[1]["content"])
            payload["repair"] = repair
            messages = [messages[0], {"role": "user", "content": json.dumps(
                payload, ensure_ascii=False)}]

    def require_continuity(self, work_id, context):
        image = context["image"]
        self.require_task(work_id, image["project_id"], image["conversation_id"])
        current = self.media.require_image(ImageVersionRef.model_validate({
            "image_id": image["image_id"], "version": image["version"]}),
            image["project_id"], image["conversation_id"])
        if current.artifact_id != image["artifact_id"] or current.sha256 != image["sha256"]:
            raise ToolError("已采用图片内容已变化，请重新选择图片")
        with self.projects._connect() as connection:
            choice = self.media._latest(connection, image["project_id"], "image_selection",
                                        image["image_id"])[1]
        if (choice.get("selected_version") != image["version"]
                or choice.get("selection_version") != context["selection_version"]):
            raise ToolError("已采用图片版本已变化，请重新提交本轮创作")

    def continuity_fields(self, work_id, context):
        self.require_continuity(work_id, context)
        source = context.get("prompt_source")
        if not source:
            return {}
        self.require_task(work_id, source["project_id"])
        prompt = self.prompts.get(source["shot_id"], version=source["version"])
        if prompt.project_id != source["project_id"] or prompt.artifact_id != source["artifact_id"]:
            raise ToolError("图片延续的 Prompt 版本来源不一致")
        sections = ComicPromptContexts.from_sections({key: getattr(prompt, key)
            for key in ComicPromptContexts.model_fields}, semantic_scope=True)
        changes = {item["field"] for item in context["changes"]}
        return {f"{section}.{field}": value
                for section, body in sections.model_dump().items()
                for field, value in body.items() if f"{section}.{field}" not in changes}

    def continuity_director_fields(self, context):
        """保持已绑定画面的执行决定；只有明确授权的对应变化才释放。"""
        version = context.get("source_director_version")
        if not version:
            return {}
        spec = self.projects.get_director(context["source_task_id"], version=version,
                                         spec_id=context["source_director_id"])
        changes = {item["field"] for item in context["changes"]}
        groups = {"character_context": ["character_presence"],
                  "world_context": ["subject_environment_relation"],
                  "style_context": ["style_boundary", "color_strategy"],
                  "shot_context.pose": ["character_pose", "character_expression"],
                  "shot_context.composition": ["composition_strategy"]}
        result = {}
        if spec.director_plan:
            for group, leaves in groups.items():
                if not any(path == group or path.startswith(group + ".") for path in changes):
                    result.update({"director_plan." + leaf: getattr(spec.director_plan, leaf)
                                   for leaf in leaves})
        if spec.cinematography:
            for leaf in type(spec.cinematography).model_fields:
                path = ("style_context.color" if leaf == "color_relationship" else
                        "shot_context.lighting_changes" if leaf in {
                            "lighting", "light_source", "light_direction"} else
                        "shot_context.camera")
                if path not in changes:
                    result["cinematography." + leaf] = getattr(spec.cinematography, leaf)
        return result

    def turn_instructions_for_work(self, work_id, target_task_id=None):
        work = self.get(work_id)
        return (
            ComicContextBuilder.conversation_instructions()
            + "\n当前是一个作品的连续创作对话。每条新的创作消息（包括动作续写、回头、"
            "表情变化）都用 create 创建独立方案，引用旧设定不表示修改旧方案。"
            "只有用户明确要求修改某份已有方案才用 director.revise，并在 sources 中"
            "给出目标方案的准确 Run 和版本；目标不明确用 clarify。"
            "讨论用 chat；明确分析或修改选中图片使用 image.analyze/image.edit。"
            "不同方案的动作、姿态、表情、构图和摄影按本轮重新设计，"
            "仅沿用本轮有来源且仍有效的人物、场景与风格。"
            "公共设定不能自动发布或修改。作品与当前选择仅用于定位，不是创作事实："
            + json.dumps({"work_id": work_id, "selected_task_id": target_task_id,
                          "plans": [{"task_id": item["task_id"], "title": item["title"],
                                     "director_version": item["director_version"]}
                                    for item in work["tasks"]]}, ensure_ascii=False)
        )

    def rename(self, work_id, data):
        request = ComicWorkWrite.model_validate(data)
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if saved := self._replay(connection, work_id, "work.rename", request):
                return saved
            record = self._work(connection, work_id)
            self._cas(connection, work_id, request.expected_version)
            self._append(
                connection,
                work_id,
                "workspace_work",
                {**record, "title": request.title, "auto_title": False},
            )
            self.projects._advance_project(connection, work_id, request.expected_version)
            return self._finish(
                connection,
                work_id,
                "work.rename",
                request,
                self.get(work_id, connection=connection),
            )

    def create_task(self, work_id, data, *, source_brief=None, conversation_id=None):
        request = ComicImageTaskCreate.model_validate(data)
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            work = self._work(connection, work_id)
            if saved := self._replay(connection, work_id, "task.create", request):
                return saved
            self._cas(connection, work_id, request.expected_version)
            project = self.projects.create(
                ComicProjectInput(
                    title=request.title,
                    brief=CreativeBriefInput.model_validate(
                        {
                            name: source_brief[name]
                            for name in CreativeBriefInput.model_fields
                            if name in source_brief
                        }
                    )
                    if source_brief
                    else {"original_request": request.requirement or request.title},
                ),
                connection=connection,
            )
            task_id = project.project.project_id
            if conversation_id:
                if conversation_id != self.get(work_id, connection=connection)["conversation_id"]:
                    raise ToolError("新方案必须属于当前作品对话")
                conversation = self.runtime.get_conversation(conversation_id)
            else:
                conversation = self.runtime.create_conversation(
                    request.title, interaction_mode=InteractionMode.GUIDED,
                    domain="comic", connection=connection,
                )
            self._append(
                connection,
                task_id,
                "workspace_task",
                {
                    "work_id": work_id,
                    "task_id": task_id,
                    "project_id": task_id,
                    "title": request.title,
                    "conversation_id": conversation.id,
                    "has_initial_requirement": bool(request.requirement),
                    "auto_title": request.title == "新画面",
                    "settings_refs": [],
                },
            )
            if work.get("auto_title") and request.requirement:
                self._append(
                    connection,
                    work_id,
                    "workspace_work",
                    {**work, "title": request.requirement[:28], "auto_title": False},
                )
            self._append(connection, task_id, "media_owner", {"conversation_id": conversation.id})
            public = [] if conversation_id else self.assets.list(work_id)
            if len(public) > 8:
                raise ToolError("公共设定超过单画面容量，请先选择需要引用的设定")
            self._copy_settings(
                connection,
                work_id,
                task_id,
                [(asset.asset_id, asset.pinned_version or asset.version) for asset in public],
            )
            self.projects._advance_project(connection, work_id, request.expected_version)
            return self._finish(
                connection,
                work_id,
                "task.create",
                request,
                self.get_task(work_id, task_id, connection=connection),
            )

    def get_task(self, work_id, task_id, *, connection=None):
        if connection is None:
            with self.projects._connect() as opened:
                return self.get_task(work_id, task_id, connection=opened)
        task = self.require_task(work_id, task_id, connection=connection)
        row = connection.execute(
            "SELECT current_version,director_version FROM comic_projects WHERE project_id=?",
            (task_id,),
        ).fetchone()
        latest = {
            asset.asset_id: asset.pinned_version or asset.version
            for asset in self.assets.list(work_id)
        }
        referenced = {
            ref["source_asset_id"]: ref["source_version"] for ref in task["settings_refs"]
        }
        images = self.media.list(task_id)
        return {
            **task,
            "project_version": int(row[0]),
            "director_version": row[1],
            "image_count": len(images),
            "adopted_image_count": sum(
                bool((image.get("selection") or {}).get("selected_version"))
                and (image.get("selection") or {}).get("available", False)
                for image in images
            ),
            "settings_updates": [
                {"asset_id": asset_id, "version": version}
                for asset_id, version in latest.items()
                if referenced.get(asset_id) != version
            ],
        }

    def initialize_task_name(self, work_id, task_id, title):
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self.require_task(work_id, task_id, connection=connection)
            if task.get("auto_title"):
                self._append(
                    connection,
                    task_id,
                    "workspace_task",
                    {
                        **task,
                        "title": title[:28],
                        "auto_title": False,
                        "has_initial_requirement": True,
                    },
                )
            work = self._work(connection, work_id)
            if work.get("auto_title"):
                version = int(
                    connection.execute(
                        "SELECT current_version FROM comic_projects WHERE project_id=?", (work_id,)
                    ).fetchone()[0]
                )
                self._append(
                    connection,
                    work_id,
                    "workspace_work",
                    {**work, "title": title[:28], "auto_title": False},
                )
                self.projects._advance_project(connection, work_id, version)

    def _copy_settings(self, connection, work_id, task_id, refs):
        task = self.require_task(work_id, task_id, connection=connection)
        copies = []
        for asset_id, version in refs:
            source = self.assets.get(work_id, asset_id, version=version)
            if source.state != "active":
                raise ToolError("公共设定版本不可用")
            current = int(
                connection.execute(
                    "SELECT current_version FROM comic_projects WHERE project_id=?", (task_id,)
                ).fetchone()[0]
            )
            local = self.assets.create(
                task_id,
                self.assets._draft(source),
                expected_project_version=current,
                connection=connection,
            )
            copies.append(
                {
                    "source_asset_id": asset_id,
                    "source_version": version,
                    "local_asset_id": local.asset_id,
                    "local_version": local.version,
                }
            )
        self._append(connection, task_id, "workspace_task", {**task, "settings_refs": copies})
        return copies

    def publish_setting(self, work_id, data):
        request = ComicWorkSettingWrite.model_validate(data)
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._work(connection, work_id)
            if saved := self._replay(connection, work_id, "settings.publish", request):
                return saved
            self._cas(connection, work_id, request.expected_version)
            source = None
            for artifact_id in request.asset.reference_artifact_ids:
                artifact = self.runtime.get_artifact(artifact_id)
                self.require_task(
                    work_id,
                    artifact.metadata.get("project_id"),
                    artifact.conversation_id,
                    connection=connection,
                )
            if any((request.source_task_id, request.source_asset_id, request.source_version)):
                self.require_task(work_id, request.source_task_id, connection=connection)
                source = self.assets.get(
                    request.source_task_id, request.source_asset_id, version=request.source_version
                )
                if self.assets._draft(source) != request.asset:
                    raise ToolError("提升公共设定必须与选中的画面资产版本一致")
            if request.asset_id:
                if not request.expected_asset_version:
                    raise ToolError("更新公共设定需要预期资产版本")
                asset = self.assets.change(
                    work_id,
                    request.asset_id,
                    expected_project_version=request.expected_version,
                    expected_asset_version=request.expected_asset_version,
                    action="edit",
                    draft=request.asset,
                    connection=connection,
                )
            else:
                asset = self.assets.create(
                    work_id,
                    request.asset,
                    expected_project_version=request.expected_version,
                    connection=connection,
                )
            self._append(
                connection,
                work_id,
                "work_setting_source",
                {
                    "asset_id": asset.asset_id,
                    "version": asset.version,
                    "source": "explicit_user_adoption" if source else "user_authored",
                    "source_task_id": request.source_task_id,
                    "source_asset_id": request.source_asset_id,
                    "source_version": request.source_version,
                },
                f"{asset.asset_id}:{asset.version}",
            )
            return self._finish(
                connection, work_id, "settings.publish", request, asset.model_dump(mode="json")
            )

    def sync_settings(self, work_id, task_id, data):
        request = ComicTaskSettingsSync.model_validate(data)
        if any(ref.version is None for ref in request.refs) or len(
            {ref.asset_id for ref in request.refs}
        ) != len(request.refs):
            raise ToolError("公共设定必须使用不重复的固定版本")
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.require_task(work_id, task_id, connection=connection)
            if saved := self._replay(connection, task_id, "settings.sync", request):
                return saved
            self._cas(connection, task_id, request.expected_project_version)
            self._copy_settings(
                connection, work_id, task_id, [(ref.asset_id, ref.version) for ref in request.refs]
            )
            return self._finish(
                connection,
                task_id,
                "settings.sync",
                request,
                self.get_task(work_id, task_id, connection=connection),
            )

    def turn_status(self, work_id, task_id, request_id):
        with self.projects._connect() as connection:
            self.require_task(work_id, task_id, connection=connection)
            record = self._latest(connection, task_id, "media_request", value_hash(request_id))[1]
            return {
                "phase": record.get("phase"),
                "run_id": record.get("run_id"),
                "result": record.get("result"),
                "error_type": record.get("error_type"),
            }

    def candidates(self, conversation_id, story_service):
        work_id = self.work_for_story(conversation_id)
        if not work_id:
            return None
        images, unavailable = [], []
        for task in self.get(work_id)["tasks"]:
            candidates = story_service.conversation_candidates(task["conversation_id"])
            unavailable.extend(candidates.get("unavailable_images", []))
            for image in candidates["images"]:
                if image["project_id"] == task["task_id"]:
                    images.append({**image, "source_project_title": task["title"]})
        return {"images": images, "unavailable_images": unavailable}

    def require_story_image(self, ref, work_id, *, connection=None, snapshot=None):
        image = snapshot or self.media.get(ref.image_id, version=ref.version, connection=connection)
        task = self.require_task(
            work_id, image.project_id, image.conversation_id, connection=connection
        )
        return self.media.require_image(
            ref, task["task_id"], task["conversation_id"], connection=connection, snapshot=snapshot
        )

    def legacy_preview(self, conversation_id, stories):
        """Resolve actual immutable sources. Titles are display-only, never grouping keys."""
        groups = []
        messages = self.runtime.list_conversation_messages(conversation_id)
        assigned = set()
        for group in self.media.conversation_works(conversation_id)["works"]:
            if self.task_binding(group["project_id"]):
                continue
            brief = next(
                (
                    item
                    for item in self.projects.brief_versions(group["project_id"])
                    if item.version == group["brief_version"]
                ),
                None,
            )
            director = None
            if group["has_director"]:
                run = self.runtime.get_run(group["director_run_id"])
                director = self.projects.get_director(
                    group["project_id"],
                    version=run.state["director_spec_version"],
                    spec_id=run.state["director_spec_id"],
                )
            images = []
            for family in self.media.list(
                group["project_id"], brief_version=group["brief_version"] or 0
            ):
                for image in self.media.versions(family["image_id"]):
                    images.append(
                        {
                            **image.model_dump(mode="json"),
                            "selected": (family.get("selection") or {}).get("selected_version")
                            == image.version,
                        }
                    )
            bound_runs = {
                run.id
                for run in self.runtime.list_runs(
                    domain="comic", conversation_id=conversation_id, limit=100000
                )
                if run.state.get("project_id") == group["project_id"]
                and (
                    run.state.get("input_brief_version") == group["brief_version"]
                    or run.id == group["director_run_id"]
                )
            }
            chosen_messages = [
                item.model_dump(mode="json") for item in messages if item.run_id in bound_runs
            ]
            assigned.update(item["id"] for item in chosen_messages)
            groups.append(
                {
                    **group,
                    "brief": brief.model_dump(mode="json") if brief else None,
                    "director": director.model_dump(mode="json") if director else None,
                    "images": sorted(
                        images, key=lambda image: (image["image_id"], image["version"])
                    ),
                    "messages": chosen_messages,
                    "conversation_id": conversation_id,
                }
            )
        story = stories.view(conversation_id)["story"]
        preview = {
            "conversation_id": conversation_id,
            "groups": groups,
            "story": story,
            "unassigned_message_ids": [item.id for item in messages if item.id not in assigned],
        }
        return {**preview, "preview_sha256": value_hash(preview)}

    def _legacy_step(self, work_id, key, action, *, task_id=None):
        """Commit deterministic local steps with their result; replay never makes a model call."""
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            version, step = self._latest(connection, work_id, "legacy_step", key)
            if step.get("result") is not None:
                return step["result"]
            if not step:
                target = task_id or work_id
                step = {
                    "expected_version": int(
                        connection.execute(
                            "SELECT current_version FROM comic_projects WHERE project_id=?",
                            (target,),
                        ).fetchone()[0]
                    )
                }
                self._append(connection, work_id, "legacy_step", step, key)
        result = action(step)
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._append(connection, work_id, "legacy_step", {**step, "result": result}, key)
        return result

    def import_legacy(self, data, stories):
        request = ComicLegacyImportRequest.model_validate(data)
        if len(set(request.source_ids)) != len(request.source_ids):
            raise ToolError("历史来源不能重复选择")
        # Work creation itself is transactional and idempotent. The import journal lives there.
        import_key = "legacy-" + value_hash(request.request_id)[:40]
        with self.projects._connect() as connection:
            rows = connection.execute(
                "SELECT project_id,payload_json FROM comic_entity_versions "
                "WHERE entity_type='legacy_import' AND entity_id=? ORDER BY version DESC LIMIT 1",
                (import_key,),
            ).fetchone()
        if rows:
            work_id, progress = rows[0], json.loads(rows[1])
            if progress["request"] != request.model_dump(mode="json"):
                raise ToolError("归入请求 ID 已用于不同选择")
            if progress.get("completed"):
                return self.get(work_id)
            preview = progress["preview"]
        else:
            preview = self.legacy_preview(request.conversation_id, stories)
            if preview["preview_sha256"] != request.preview_sha256:
                raise ToolError("历史来源已变化，请重新预览后确认归入")
            groups = {group["work_id"]: group for group in preview["groups"]}
            if any(identifier not in groups for identifier in request.source_ids):
                raise ToolError("选择包含其他对话或无法确定的历史来源")
            if request.include_story and preview.get("story"):
                available = {
                    (image["image_id"], image["version"])
                    for identifier in request.source_ids
                    for image in groups[identifier]["images"]
                }
                if any(
                    (shot["image"]["image_id"], shot["image"]["version"]) not in available
                    for shot in preview["story"]["shots"]
                ):
                    raise ToolError("请选入故事全部固定图片的来源后再归入故事")
            backup = database_manager.backup(self.projects.path, owner="comic.workspace.import")
            work_id = self.create({"request_id": import_key, "title": request.title})["work_id"]
            progress = {
                "request": request.model_dump(mode="json"),
                "preview": preview,
                "backup": str(backup),
                "completed": False,
            }
            with self.projects._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._append(connection, work_id, "legacy_import", progress, import_key)
        groups = {group["work_id"]: group for group in preview["groups"]}
        image_map = {}
        for identifier in request.source_ids:
            group = groups[identifier]
            step_key = import_key + ":task:" + value_hash(identifier)[:20]
            child = self._legacy_step(
                work_id,
                step_key,
                lambda step, step_key=step_key, group=group: self.create_task(
                    work_id,
                    {
                        "request_id": step_key,
                        "expected_version": step["expected_version"],
                        "title": group["title"],
                        "requirement": group["original_request"] or "历史图片画面",
                    },
                    source_brief=group["brief"],
                ),
            )
            task_id, owner = child["task_id"], child["conversation_id"]
            self._import_legacy_director(work_id, step_key, child, group)
            for message in group["messages"]:
                self.runtime.add_conversation_message(
                    owner,
                    role=MessageRole(message["role"]),
                    type=MessageType(message["type"]),
                    content=message["content"],
                    event_id="legacy-message:" + message["id"],
                )
            if not group["messages"]:
                self.runtime.add_conversation_message(
                    owner,
                    role=MessageRole.USER,
                    type=MessageType.TEXT,
                    content=group["original_request"] or "历史图片导入",
                    event_id="legacy-source:" + identifier,
                )
            for original in group["images"]:
                old_key = f"{original['image_id']}:{original['version']}"
                image_step = import_key + ":image:" + value_hash(old_key)[:24]
                previous_image = image_map.get((original["image_id"], original["version"] - 1))

                def copy_image(
                    step,
                    original=original,
                    image_step=image_step,
                    task_id=task_id,
                    owner=owner,
                    previous_image=previous_image,
                ):
                    ref = ImageVersionRef(
                        image_id=original["image_id"], version=original["version"]
                    )
                    image = self.media.require_image(
                        ref, original["project_id"], request.conversation_id
                    )
                    artifact = self.runtime.get_artifact(image.artifact_id)
                    content = validate_runtime_path(Path(artifact.location)).read_bytes()
                    if hashlib.sha256(content).hexdigest() != image.sha256:
                        raise ToolError("历史图片在归入过程中发生变化，请恢复原文件")
                    return self.media.import_image(
                        task_id,
                        ComicImageImportRequest(
                            request_id=image_step,
                            conversation_id=owner,
                            expected_project_version=step["expected_version"],
                            filename=image.filename,
                            **(
                                {
                                    "image_id": previous_image["image_id"],
                                    "expected_image_version": previous_image["version"],
                                    "parent": {
                                        "image_id": previous_image["image_id"],
                                        "version": previous_image["version"],
                                    },
                                }
                                if previous_image
                                else {}
                            ),
                            data_url=f"data:{image.mime_type};base64,"
                            + base64.b64encode(content).decode(),
                        ),
                        output_dir=get_runtime_paths().artifacts / "comic-images",
                    )

                imported = self._legacy_step(work_id, image_step, copy_image, task_id=task_id)
                image_map[(original["image_id"], original["version"])] = imported
                if original["selected"]:
                    self._legacy_step(
                        work_id,
                        image_step + ":select",
                        lambda step, imported=imported, image_step=image_step, owner=owner: (
                            self.media.select(
                                imported["image_id"],
                                ComicImageSelectionRequest(
                                    request_id=image_step + "-select",
                                    conversation_id=owner,
                                    expected_project_version=step["expected_version"],
                                    version=imported["version"],
                                    expected_selection_version=0,
                                ),
                            )
                        ),
                        task_id=task_id,
                    )
                with self.projects._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    identity = f"{imported['image_id']}:{imported['version']}"
                    if not self._latest(connection, task_id, "legacy_image", identity)[0]:
                        source = stories._source_contexts([original])[0]
                        self._append(
                            connection,
                            task_id,
                            "legacy_image",
                            {"original": original, "source_context": source, "image": imported},
                            identity,
                        )
        if request.include_story and preview.get("story"):
            original = preview["story"]
            missing = [
                shot["shot_id"]
                for shot in original["shots"]
                if (shot["image"]["image_id"], shot["image"]["version"]) not in image_map
            ]
            if missing:
                raise ToolError(
                    "所选历史来源未包含故事全部固定图片，请补选对应来源", detail=",".join(missing)
                )
            draft = stories._draft(original).model_dump(mode="json")
            for shot in draft["shots"]:
                image = image_map[(shot["image"]["image_id"], shot["image"]["version"])]
                shot["image"] = {"image_id": image["image_id"], "version": image["version"]}
            owner = self.get(work_id)["story_conversation_id"]
            self._legacy_step(
                work_id,
                import_key + ":story",
                lambda step: stories.save(
                    ComicStorySaveRequest(
                        request_id=import_key + "-story",
                        conversation_id=owner,
                        anchor_project_id=work_id,
                        expected_project_version=step["expected_version"],
                        expected_story_version=0,
                        draft=draft,
                    ),
                    imported_images=[
                        self.media.get(image["image_id"], version=image["version"]).model_dump(
                            mode="json"
                        )
                        for image in image_map.values()
                    ],
                ),
            )
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._append(
                connection, work_id, "legacy_import", {**progress, "completed": True}, import_key
            )
        return self.get(work_id)

    def _import_legacy_director(self, work_id, key, child, group):
        """Copy the plan and its map atomically, without approval or paid validation."""
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._latest(connection, work_id, "legacy_director", key)[0]:
                return
            task_id = child["task_id"]
            source = group.get("director")
            if source:
                asset_map = {}
                for identity, version in source.get("asset_versions", {}).items():
                    original = self.assets.get(
                        group["project_id"], identity.removeprefix("asset:"), version=version
                    )
                    current_version = int(
                        connection.execute(
                            "SELECT current_version FROM comic_projects WHERE project_id=?",
                            (task_id,),
                        ).fetchone()[0]
                    )
                    asset = self.assets.create(
                        task_id,
                        self.assets._draft(original),
                        expected_project_version=current_version,
                        connection=connection,
                    )
                    asset_map["asset:" + asset.asset_id] = asset.version
                draft = DirectorSpecDraft.model_validate(
                    {
                        **{
                            name: source[name]
                            for name in DirectorSpecDraft.model_fields
                            if name in source
                        },
                        "critic_result": None,
                        "critic_status": None,
                        "asset_versions": asset_map,
                        "storyboard_version": None,
                        "shot_version": None,
                        "reference_context": {},
                    }
                )
                current_version = int(
                    connection.execute(
                        "SELECT current_version FROM comic_projects WHERE project_id=?", (task_id,)
                    ).fetchone()[0]
                )
                self.projects.save_director(
                    task_id,
                    draft,
                    expected_project_version=current_version,
                    source="restored",
                    restored_from_version=source["version"],
                    connection=connection,
                )
            self._append(
                connection,
                work_id,
                "legacy_director",
                {
                    "task_id": task_id,
                    "source_project_id": group["project_id"],
                    "source_brief_version": group["brief_version"],
                    "source_director": source,
                    "source_conversation_id": group.get("conversation_id"),
                },
                key,
            )
            binding = self.task_binding(task_id, connection=connection)
            self._append(
                connection,
                task_id,
                "workspace_task",
                {
                    **binding,
                    "legacy_source": {
                        "project_id": group["project_id"],
                        "brief_version": group["brief_version"],
                        "director_run_id": group["director_run_id"],
                        "conversation_id": group["conversation_id"],
                        "source_id": group["work_id"],
                    },
                },
            )

    @staticmethod
    def turn_instructions(task, snapshot, image):
        return (
            "当前是作品内一个固定画面的连续对话。作品、画面与对话不能由模型切换，"
            "本轮默认继续当前画面，不创建新项目。当前绑定及版本："
            + json.dumps(
                {
                    "work_id": task["work_id"],
                    "task_id": task["task_id"],
                    "brief": snapshot.creative_brief.model_dump(mode="json"),
                    "director_version": snapshot.project.director_version,
                    "image": image.model_dump() if image else None,
                },
                ensure_ascii=False,
            )
            + "。讨论、解释、比较使用 chat；首次形成画面方案使用 create；"
            "已有方案调整使用 director.revise；明确分析当前图片使用 image.analyze；"
            "已有图片的修改要求使用 image.edit（整理外部改图指令，不生成图片）。"
            "缺少目标图片、要求新画面或新作品、目标不明确使用 clarify 并简短询问。"
            "画面已有方案时 create 也只表示修订当前方案，不能重置画面。"
            "公共设定仅由显式更新作品设定操作发布；局部创作选择不能升级为公共事实。"
        )
