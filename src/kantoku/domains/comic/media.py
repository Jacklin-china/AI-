"""直接导入图片、内容版本与参考用途；复用 Core Artifact 和作品修订表。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from uuid import uuid4

from kantoku.config import ToolError
from kantoku.config.observability import current_trace_id
from kantoku.config.runtime_paths import (
    atomic_write_bytes,
    get_runtime_paths,
    validate_runtime_path,
)
from kantoku.core.conversations import InteractionMode
from kantoku.core.runtime.models import ArtifactLifecycle, ArtifactRecord, ArtifactType, utc_now
from kantoku.core.runtime.store import RuntimeStore

from .director_provenance import value_hash
from .models import (
    ComicImageImportRequest,
    ComicImageSelectionRequest,
    ComicMediaMutation,
    ComicReferenceRequest,
    ImageVersion,
    ImageVersionRef,
)
from .projects import ComicProjectStore

logger = logging.getLogger(__name__)


def decode_image(data_url: str) -> tuple[bytes, str, str]:
    """两种图片上传入口共用已有的大小、编码、文件签名契约。"""
    try:
        header, encoded = data_url.split(",", 1)
        formats = {
            "data:image/png;base64": ("image/png", ".png"),
            "data:image/jpeg;base64": ("image/jpeg", ".jpg"),
            "data:image/webp;base64": ("image/webp", ".webp"),
        }
        mime, suffix = formats[header]
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, KeyError, binascii.Error) as error:
        raise ToolError("外部图片格式不正确", detail="module=external_image.decode") from error
    if not content or len(content) > 5 * 1024 * 1024:
        raise ToolError("外部图片必须在 5 MB 以内", detail="module=external_image.validate")
    signatures = {
        "image/png": content.startswith(b"\x89PNG\r\n\x1a\n")
        and len(content) >= 45
        and content[-12:] == b"\x00\x00\x00\x00IEND\xaeB`\x82",
        "image/jpeg": content.startswith(b"\xff\xd8\xff")
        and content.endswith(b"\xff\xd9")
        and len(content) >= 32,
        "image/webp": content.startswith(b"RIFF")
        and content[8:12] == b"WEBP"
        and len(content) >= 32
        and int.from_bytes(content[4:8], "little") + 8 == len(content),
    }
    if not signatures[mime]:
        raise ToolError(
            "文件内容不是有效的 PNG、JPEG 或 WebP 图片", detail="module=external_image.validate"
        )
    return content, mime, suffix


class ComicMediaStore:
    """领域版本关系，无媒体副本库、独立 Runtime 或自动模型调用。"""

    def __init__(self, projects: ComicProjectStore, runtime: RuntimeStore) -> None:
        self.projects, self.runtime = projects, runtime

    @staticmethod
    def _latest(
        connection: sqlite3.Connection, project_id: str, kind: str, entity_id: str
    ) -> tuple[int, dict]:
        row = connection.execute(
            "SELECT version,payload_json FROM comic_entity_versions "
            "WHERE project_id=? AND entity_type=? AND entity_id=? ORDER BY version DESC LIMIT 1",
            (project_id, kind, entity_id),
        ).fetchone()
        return (int(row["version"]), json.loads(row["payload_json"])) if row else (0, {})

    def require_owner(self, project_id: str, conversation_id: str, *, connection=None) -> None:
        conversation = self.runtime.get_conversation(conversation_id)
        if (
            conversation.domain != "comic"
            or conversation.interaction_mode != InteractionMode.GUIDED
        ):
            raise ToolError(
                "图片必须绑定当前 Comic 创作会话", detail="module=comic.media owner=mode"
            )
        if connection is None:
            with self.projects._connect() as opened:
                return self.require_owner(project_id, conversation_id, connection=opened)
        _, task = self._latest(connection, project_id, "workspace_task", project_id)
        if task and task["conversation_id"] != conversation_id:
            raise ToolError("画面已固定绑定另一段对话，不能读取或修改")
        _, owner = self._latest(connection, project_id, "media_owner", project_id)
        if owner and owner["conversation_id"] != conversation_id:
            raise ToolError(
                "不能读取或修改其他会话的图片", detail="module=comic.media owner=conversation"
            )
        # 兼容已有导演作品的会话归属；没有导演的新项目在第一次写入时原子绑定。
        rows = connection.execute(
            "SELECT DISTINCT json_extract(state_json, '$.conversation_id') "
            "AS owner FROM runs WHERE domain='comic' "
            "AND json_extract(state_json, '$.project_id')=? "
            "AND workflow IN ('comic.director-spec','comic.director')",
            (project_id,),
        ).fetchall()
        if any(row["owner"] and row["owner"] != conversation_id for row in rows):
            raise ToolError("作品已有其他会话的导演任务", detail="module=comic.media owner=project")

    def _write_guard(self, connection, project_id: str, request: ComicMediaMutation) -> None:
        self.require_owner(project_id, request.conversation_id, connection=connection)
        row = connection.execute(
            "SELECT current_version FROM comic_projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if row is None or int(row["current_version"]) != request.expected_project_version:
            raise ToolError(
                "作品已由其他操作更新，请刷新后重试", detail="module=comic.media cas=project"
            )
        if not self._latest(connection, project_id, "media_owner", project_id)[0]:
            self.projects._insert_version(
                connection,
                project_id=project_id,
                entity_type="media_owner",
                entity_id=project_id,
                version=1,
                payload_json=json.dumps({"conversation_id": request.conversation_id}),
                created_at=utc_now(),
            )

    def request_result(
        self, project_id: str, operation: str, request: ComicMediaMutation, *, connection=None
    ) -> dict | None:
        if connection is None:
            with self.projects._connect() as opened:
                return self.request_result(project_id, operation, request, connection=opened)
        self.require_owner(project_id, request.conversation_id, connection=connection)
        _, saved = self._latest(
            connection, project_id, "media_request", value_hash(request.request_id)
        )
        if not saved:
            return None
        if saved["sha256"] != value_hash(
            {"operation": operation, "request": request.model_dump(mode="json")}
        ):
            raise ToolError(
                "请求 ID 已用于不同内容，请使用新请求 ID",
                detail="module=comic.media idempotency=conflict",
            )
        if saved.get("phase") not in (None, "completed"):
            raise ToolError(
                "该请求已提交但没有可复用结果，请查看原运行记录；不会自动重发模型调用",
                detail=f"module=comic.media phase={saved['phase']} run_id={saved.get('run_id')}",
            )
        return saved["result"]

    def claim_request(self, project_id, operation, request, *, run_id):
        """沿用媒体请求记录，调用前原子认领；异常或重启不释放后重发。"""
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if saved := self.request_result(project_id, operation, request, connection=connection):
                return saved
            self._write_guard(connection, project_id, request)
            self.projects._insert_version(connection, project_id=project_id,
                entity_type="media_request", entity_id=value_hash(request.request_id),
                version=1, created_at=utc_now(), payload_json=json.dumps({
                    "sha256": value_hash({"operation": operation,
                        "request": request.model_dump(mode="json")}),
                    "phase": "submitted", "run_id": run_id, "result": None}))
        return None

    def fail_request(self, project_id, request, error):
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            version, saved = self._latest(connection, project_id, "media_request",
                                           value_hash(request.request_id))
            if saved.get("phase") != "submitted":
                return
            self.projects._insert_version(connection, project_id=project_id,
                entity_type="media_request", entity_id=value_hash(request.request_id),
                version=version + 1, created_at=utc_now(), payload_json=json.dumps({
                    **saved, "phase": "unavailable", "error_type": type(error).__name__,
                    "trace_id": current_trace_id()}))

    def _finish(self, connection, project_id, operation, request, result, *, advance_project=True):
        version, claimed = self._latest(connection, project_id, "media_request",
                                        value_hash(request.request_id))
        if claimed and (claimed.get("phase") != "submitted" or claimed["sha256"] != value_hash(
            {"operation": operation, "request": request.model_dump(mode="json")}
        )):
            raise ToolError("请求认领已变化，拒绝保存", detail="module=comic.media claim=conflict")
        result["project_version"] = (self.projects._advance_project(
            connection, project_id, request.expected_project_version
        ) if advance_project else int(connection.execute(
            "SELECT current_version FROM comic_projects WHERE project_id=?", (project_id,)
        ).fetchone()[0]))
        self.projects._insert_version(
            connection,
            project_id=project_id,
            entity_type="media_request",
            entity_id=value_hash(request.request_id),
            version=version + 1,
            created_at=utc_now(),
            payload_json=json.dumps(
                {
                    "sha256": value_hash(
                        {"operation": operation, "request": request.model_dump(mode="json")}
                    ),
                    "result": result,
                    "phase": "completed",
                    "run_id": claimed.get("run_id") if claimed else result.get("run_id"),
                },
                ensure_ascii=False,
            ),
        )
        return result

    def workspace_state(self, project_id, conversation_id, *, image=None):
        self.require_owner(project_id, conversation_id)
        kind = "image_review_draft" if image else "image_workbench"
        identity = f"{image.image_id}:{image.version}" if image else project_id
        if image:
            self.historical_identity(image, project_id, conversation_id)
        with self.projects._connect() as connection:
            version, state = self._latest(connection, project_id, kind, identity)
        refs = [image.model_dump(mode="json")] if image else []
        refs.extend(value for key in ("image", "edit_image")
                    if isinstance(value := state.get(key), dict))
        unavailable, seen = [], set()
        for value in refs:
            ref = ImageVersionRef.model_validate(value)
            key = (ref.image_id, ref.version)
            if key in seen:
                continue
            seen.add(key)
            selected = self.selection_view(project_id, {
                "image_id": ref.image_id, "selected_version": ref.version,
            }, conversation_id=conversation_id)
            if not selected["available"]:
                unavailable.append({**ref.model_dump(mode="json"),
                    "reason": selected["unavailable_reason"],
                    "lifecycle_state": selected.get("lifecycle_state")})
        return {"version": version, "state": state or None, "unavailable_images": unavailable}

    def image_view(self, image: ImageVersion) -> dict:
        """列表只核对身份、生命周期及文件存在/容量；实际读取与操作仍须校验字节哈希。"""
        lifecycle, reason = None, None
        try:
            artifact = self.runtime.get_artifact(image.artifact_id)
            lifecycle = artifact.lifecycle_state.value
            self.require_identity(ImageVersionRef(image_id=image.image_id, version=image.version),
                                  image.project_id, image.conversation_id, _snapshot=image)
            path = validate_runtime_path(Path(artifact.location or ""))
            if not path.is_file() or path.stat().st_size > 5 * 1024 * 1024:
                raise ToolError("图片原文件缺失或超出容量")
        except (ToolError, OSError) as error:
            reason = error.message if isinstance(error, ToolError) else "图片原文件不可读取"
        return {**image.model_dump(mode="json"), "available": reason is None,
                "unavailable_reason": reason, "lifecycle_state": lifecycle}

    def selection_view(self, project_id, selection, *, conversation_id=None, connection=None):
        """保留原采用指针，单独核对其真实版本，不自动改用其他图片。"""
        if not selection:
            return None
        result = {**selection, "available": False, "unavailable_reason": None,
                  "lifecycle_state": None}
        try:
            image = self.get(selection["image_id"], version=selection["selected_version"],
                             connection=connection, _legacy_history=True)
            if image.project_id != project_id or (conversation_id is not None
                    and image.conversation_id != conversation_id):
                raise ToolError("采用图片不属于当前作品或会话")
            view = self.image_view(image)
            result.update({key: view[key] for key in (
                "available", "unavailable_reason", "lifecycle_state")})
        except (ToolError, OSError) as error:
            result["unavailable_reason"] = (error.message if isinstance(error, ToolError)
                                            else "图片原文件不可读取")
        return result

    def require_identity(self, ref, project_id, conversation_id, *, _snapshot=None):
        """Reading/restoring drafts does not require the missing file to exist."""
        image = _snapshot or self.get(ref.image_id, version=ref.version)
        if image.image_id != ref.image_id or image.version != ref.version:
            raise ToolError("图片快照与请求版本不一致")
        if image.project_id != project_id or image.conversation_id != conversation_id:
            raise ToolError("图片不属于当前作品或会话")
        artifact = self.runtime.get_artifact(image.artifact_id)
        if (artifact.lifecycle_state != ArtifactLifecycle.ACTIVE
                or artifact.type != ArtifactType.IMAGE or artifact.status != "ready"
                or artifact.conversation_id != conversation_id
                or artifact.metadata.get("project_id") != project_id
                or artifact.metadata.get("sha256") != image.sha256):
            raise ToolError("图片已失效或来源不一致")
        return image

    def historical_identity(self, ref, project_id, conversation_id, *, allow_related=False):
        """仅读取已有来源关系；停用文件不能因此用于分析、恢复、采用或导出。"""
        image = self.get(ref.image_id, version=ref.version, _legacy_history=True)
        if image.conversation_id != conversation_id or (
            image.project_id != project_id and not allow_related
        ):
            raise ToolError("图片不属于当前作品或会话")
        self.require_owner(image.project_id, conversation_id)
        artifact = self.runtime.get_artifact(image.artifact_id)
        if (artifact.type != ArtifactType.IMAGE or artifact.status != "ready"
                or artifact.conversation_id != conversation_id
                or artifact.metadata.get("project_id") != image.project_id
                or artifact.metadata.get("sha256") != image.sha256):
            raise ToolError("图片历史来源不一致")
        return image

    def save_workspace_state(self, project_id, request, *, draft=False):
        image = request.image
        kind = "image_review_draft" if draft else "image_workbench"
        identity = f"{image.image_id}:{image.version}" if draft else project_id
        operation = kind + ":" + identity
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if saved := self.request_result(project_id, operation, request, connection=connection):
                return saved
            self.require_owner(project_id, request.conversation_id, connection=connection)
            if image:
                self.require_identity(image, project_id, request.conversation_id)
            if not draft and request.edit_image:
                self.require_identity(request.edit_image, project_id, request.conversation_id)
            version, previous = self._latest(connection, project_id, kind, identity)
            if version != request.expected_version:
                raise ToolError("草稿或工作台已在其他页面更新，请重新读取后核对")
            state = request.model_dump(mode="json", exclude={"request_id", "expected_version"})
            if draft:
                state["edit_scope"] = (request.edit_scope.frozen_scope()
                                       if request.edit_scope else None)
                if "dismissed_finding_region_ids" not in request.model_fields_set:
                    state.pop("dismissed_finding_region_ids", None)
            if state != previous:
                version += 1
                self.projects._insert_version(connection, project_id=project_id,
                    entity_type=kind, entity_id=identity, version=version,
                    payload_json=json.dumps(state, ensure_ascii=False), created_at=utc_now())
            return self._finish(connection, project_id, operation, request,
                {"version": version, "state": state}, advance_project=False)

    def restore_file(self, project_id, request):
        content, mime, _ = decode_image(request.data_url)
        image = self.require_identity(request.image, project_id, request.conversation_id)
        if hashlib.sha256(content).hexdigest() != image.sha256 or mime != image.mime_type:
            raise ToolError("所选文件与原图内容不同，请作为新图片或新版本导入")
        artifact = self.runtime.get_artifact(image.artifact_id)
        path = validate_runtime_path(Path(artifact.location or ""))
        manager = get_runtime_paths()
        if not path.is_relative_to(manager.root):
            raise ToolError("原图路径不在当前运行目录内，无法直接恢复")
        operation = f"image.restore:{image.image_id}:{image.version}"
        if saved := self.request_result(project_id, operation, request):
            return saved
        if (self.projects.get(project_id).project.current_version
                != request.expected_project_version):
            raise ToolError("作品已更新，请刷新后重新恢复")
        if path.exists():
            if (not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != image.sha256):
                raise ToolError("原位置已有其他内容，已停止恢复，不会覆盖文件")
        else:
            atomic_write_bytes(path, content, owner="comic.media", replace=False)
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if saved := self.request_result(project_id, operation, request, connection=connection):
                return saved
            self._write_guard(connection, project_id, request)
            return self._finish(connection, project_id, operation, request,
                {"image": request.image.model_dump(), "restored": True})

    def get(self, image_id: str, *, version: int | None = None, connection=None,
            _legacy_history: bool = False) -> ImageVersion:
        if connection is None:
            with self.projects._connect() as opened:
                return self.get(image_id, version=version, connection=opened,
                                _legacy_history=_legacy_history)
        query = (
            "SELECT payload_json FROM comic_entity_versions "
            "WHERE entity_type='image' AND entity_id=?"
        )
        parameters = [image_id]
        if version is not None:
            query += " AND version=?"
            parameters.append(version)
        row = connection.execute(query + " ORDER BY version DESC LIMIT 1", parameters).fetchone()
        if row is None:
            # 旧分镜上传仅有 Core Artifact：以原 Artifact ID 投影不可变 v1，
            # 只读兼容，不复制文件、不在 GET 时创建版本或改写历史。
            if version not in (None, 1):
                raise ToolError("找不到指定图片版本", detail="module=comic.media source=image")
            try:
                artifact = self.runtime.get_artifact(image_id)
            except ToolError as error:
                # 图片已被删除、或本来就没有这个 id：统一按"找不到图片版本"回答，
                # 不把内部产物 id 透露成"找不到指定 Artifact"。
                raise ToolError(
                    "找不到指定图片版本", detail="module=comic.media source=image"
                ) from error
            metadata = artifact.metadata
            # 图片库可删除旧文件，但仍保留作品与后续版本的来源。仅既有版本链的
            # 名称/来源/历史投影允许读取停用 v1 的元数据，不能据此读取或采用文件。
            retained_history = (
                _legacy_history and connection.execute(
                    "SELECT 1 FROM comic_entity_versions WHERE entity_id=? AND project_id=? "
                    "AND ((entity_type='image' AND version>1 "
                    "AND json_extract(payload_json,'$.conversation_id')=?) "
                    "OR entity_type='image_selection') LIMIT 1",
                    (image_id, metadata.get("project_id"), artifact.conversation_id),
                ).fetchone() is not None
            )
            mime = {".png": "image/png", ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(
                        Path(artifact.location or "").suffix.lower())
            if (artifact.source != "comic.external_image" or artifact.type != ArtifactType.IMAGE
                    or artifact.status != "ready"
                    or (artifact.lifecycle_state != ArtifactLifecycle.ACTIVE
                        and not retained_history)
                    or metadata.get("origin") != "real" or not artifact.conversation_id
                    or not metadata.get("project_id") or not metadata.get("sha256") or not mime):
                raise ToolError("找不到指定图片版本", detail="module=comic.media source=image")
            return ImageVersion(image_id=artifact.id, version=1, artifact_id=artifact.id,
                project_id=metadata["project_id"], conversation_id=artifact.conversation_id,
                sha256=metadata["sha256"], filename=metadata.get("filename") or "external-image",
                mime_type=mime, instruction_artifact_id=metadata.get("prompt_artifact_id"),
                provenance="prompt_output", created_at=artifact.created_at)
        return ImageVersion.model_validate_json(row["payload_json"])

    def require_image(
        self,
        ref: ImageVersionRef,
        project_id: str,
        conversation_id: str,
        *,
        connection=None,
        allow_related: bool = False,
        snapshot: ImageVersion | None = None,
    ) -> ImageVersion:
        # 已持久化的不可变引用使用其原 Artifact，不让后来的版本记录替换历史内容。
        image = snapshot or self.get(ref.image_id, version=ref.version, connection=connection)
        if image.image_id != ref.image_id or image.version != ref.version:
            raise ToolError("图片快照与请求版本不一致", detail="module=comic.media source=identity")
        if image.conversation_id != conversation_id or (
            image.project_id != project_id and not allow_related
        ):
            raise ToolError("图片来源越过作品或会话范围", detail="module=comic.media source=scope")
        if image.project_id != project_id:
            self.require_owner(image.project_id, conversation_id, connection=connection)
        artifact = self.runtime.get_artifact(image.artifact_id)
        if (
            artifact.type != ArtifactType.IMAGE
            or artifact.status != "ready"
            or artifact.lifecycle_state != ArtifactLifecycle.ACTIVE
            or artifact.conversation_id != conversation_id
            or artifact.metadata.get("project_id") != image.project_id
            or artifact.metadata.get("sha256") != image.sha256
        ):
            raise ToolError(
                "图片产物已失效或来源不一致", detail="module=comic.media source=artifact"
            )
        path = validate_runtime_path(Path(artifact.location or ""))
        if (
            not path.is_file()
            or path.stat().st_size > 5 * 1024 * 1024
            or hashlib.sha256(path.read_bytes()).hexdigest() != image.sha256
        ):
            raise ToolError("图片原文件缺失或已改变", detail="module=comic.media source=content")
        return image

    def import_image(
        self,
        project_id: str,
        request: ComicImageImportRequest,
        *,
        output_dir: Path,
        run_id: str | None = None,
    ) -> dict:
        content, mime, suffix = decode_image(request.data_url)
        if saved := self.request_result(project_id, "image.import", request):
            return saved
        # File registration uses the same database; publish before its write lock.
        path = validate_runtime_path(output_dir) / (uuid4().hex + suffix)
        atomic_write_bytes(path, content, owner="comic.media")
        committed = False
        try:
            with self.projects._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if saved := self.request_result(
                    project_id, "image.import", request, connection=connection
                ):
                    return saved
                self._write_guard(connection, project_id, request)
                parent = (
                    self.require_image(
                        request.parent,
                        project_id,
                        request.conversation_id,
                        connection=connection,
                        allow_related=request.image_id is None,
                    )
                    if request.parent
                    else None
                )
                image_id, version = request.image_id or f"comic-image-{uuid4().hex}", 1
                if request.image_id:
                    current = self.get(image_id, connection=connection)
                    if current.version != request.expected_image_version:
                        raise ToolError(
                            "图片版本已变化，请刷新后重试", detail="module=comic.media cas=image"
                        )
                    version = current.version + 1
                if request.instruction_artifact_id:
                    instruction = self.runtime.get_artifact(request.instruction_artifact_id)
                    owner = instruction.conversation_id
                    if not owner and instruction.run_id:
                        owner = self.runtime.get_run(instruction.run_id).state.get(
                            "conversation_id"
                        )
                    if (
                        instruction.type != ArtifactType.PROMPT
                        or owner != request.conversation_id
                        or instruction.metadata.get("project_id") != project_id
                        or instruction.lifecycle_state != ArtifactLifecycle.ACTIVE
                        or instruction.status != "ready"
                    ):
                        raise ToolError(
                            "改图指令或 Prompt 的来源不合法",
                            detail="module=comic.media source=instruction",
                        )
                    if instruction.metadata.get("image_operation") == "edit":
                        if request.parent is None or request.parent.model_dump() != (
                            instruction.metadata.get("source_image")
                        ):
                            raise ToolError("改图结果必须绑定指令的原图片版本")
                        self.require_edit_confirmation(instruction.id, connection=connection)
                now = utc_now()
                image = ImageVersion(
                    image_id=image_id,
                    version=version,
                    project_id=project_id,
                    conversation_id=request.conversation_id,
                    artifact_id=f"artifact-{uuid4().hex}",
                    sha256=hashlib.sha256(content).hexdigest(),
                    filename=Path(request.filename).name,
                    mime_type=mime,
                    parent=request.parent,
                    instruction_artifact_id=request.instruction_artifact_id,
                    provenance="external_edit"
                    if parent
                    else "prompt_output"
                    if request.instruction_artifact_id
                    else "direct_upload",
                    created_at=now,
                )
                artifact = ArtifactRecord(
                    id=image.artifact_id,
                    type=ArtifactType.IMAGE,
                    conversation_id=request.conversation_id,
                    run_id=run_id,
                    node_id="image.import",
                    source="comic.media",
                    status="ready",
                    created_at=now,
                    location=str(path),
                    version=version,
                    metadata={
                        **image.model_dump(mode="json"),
                        "source_brief_version": self.image_brief_version(
                            request.image_id or parent.image_id, connection=connection)
                        if request.image_id or (parent and parent.project_id == project_id)
                        else self.projects._snapshot(connection, project_id,
                            request.expected_project_version).creative_brief.version,
                        "request_id": request.request_id,
                        "trace_id": current_trace_id(),
                        "parent_artifact_id": parent.artifact_id if parent else None,
                        "origin": "real",
                        "creation_mode": "external_upload",
                    },
                )
                self.runtime.insert_artifact(connection, artifact)
                self.projects._insert_version(
                    connection,
                    project_id=project_id,
                    entity_type="image",
                    entity_id=image_id,
                    version=version,
                    payload_json=image.model_dump_json(),
                    created_at=now,
                )
                result = self._finish(
                    connection,
                    project_id,
                    "image.import",
                    request,
                    {**image.model_dump(mode="json"), "artifact_type": "image", "run_id": run_id},
                )
            committed = True
            return result
        finally:
            if not committed:
                get_runtime_paths().remove_file(path, file_type="artifact")

    def require_edit_confirmation(self, artifact_id, *, connection=None):
        if connection is None:
            with self.projects._connect() as opened:
                return self.require_edit_confirmation(artifact_id, connection=opened)
        artifact = self.runtime.get_artifact(artifact_id)
        _, confirmation = self._latest(connection, artifact.metadata["project_id"],
                                        "image_edit_confirmation", artifact_id)
        if not confirmation or confirmation["sha256"] != value_hash(artifact.metadata):
            raise ToolError("请先确认当前改图指令", detail="module=comic.media edit=unconfirmed")
        return confirmation

    def versions(self, image_id: str, *, before=None, limit=None) -> list[ImageVersion]:
        image = self.get(image_id, _legacy_history=True)
        with self.projects._connect() as connection:
            query = ("SELECT payload_json FROM comic_entity_versions "
                     "WHERE project_id=? AND entity_type='image' AND entity_id=?")
            parameters = [image.project_id, image_id]
            if before is not None:
                query += " AND version<?"
                parameters.append(before)
            query += " ORDER BY version DESC"
            if limit is not None:
                query += " LIMIT ?"
                parameters.append(limit)
            rows = connection.execute(query, parameters).fetchall()
        versions = [ImageVersion.model_validate_json(row["payload_json"]) for row in rows]
        if (not any(item.version == 1 for item in versions)
                and (before is None or before > 1) and (limit is None or len(versions) < limit)):
            versions.append(self.get(image_id, version=1, _legacy_history=True))
        return versions

    def has_work_content(self, project_id: str) -> bool:
        """Imported/story works are occupied even before their first director exists."""
        with self.projects._connect() as connection:
            return bool(connection.execute(
                "SELECT 1 FROM comic_entity_versions WHERE project_id=? "
                "AND entity_type IN ('image','story','story_anchor') LIMIT 1",
                (project_id,),
            ).fetchone() or connection.execute(
                "SELECT 1 FROM artifacts WHERE source='comic.external_image' "
                "AND json_extract(metadata_json,'$.project_id')=? LIMIT 1",
                (project_id,),
            ).fetchone())

    def image_brief_version(self, image_id: str, *, connection=None) -> int | None:
        """Resolve original recorded input, never assign old media to today's Brief."""
        if connection is None:
            with self.projects._connect() as opened:
                return self.image_brief_version(image_id, connection=opened)
        image = self.get(image_id, version=1, connection=connection, _legacy_history=True)
        artifact = self.runtime.get_artifact(image.artifact_id)
        recorded = artifact.metadata.get("source_brief_version")
        if isinstance(recorded, int) and recorded > 0:
            return recorded
        if artifact.run_id:
            run = self.runtime.get_run(artifact.run_id)
            expected = (run.state.get("media_input") or {}).get("expected_project_version")
            if (run.state.get("project_id") == image.project_id
                    and isinstance(expected, int) and expected > 0):
                return self.projects._snapshot(
                    connection, image.project_id, expected).creative_brief.version
        if image.instruction_artifact_id:
            prompt = self.runtime.get_artifact(image.instruction_artifact_id)
            recorded = prompt.metadata.get("creative_brief_version")
            if isinstance(recorded, int) and recorded > 0:
                return recorded
        # Unbound older media stays separately visible, rather than borrowing a plan.
        return None

    def conversation_works(self, conversation_id: str) -> dict:
        conversation = self.runtime.get_conversation(conversation_id)
        if (conversation.domain != "comic"
                or conversation.interaction_mode is not InteractionMode.GUIDED):
            raise ToolError("作品必须属于当前漫剧创作会话")
        runs = self.runtime.list_runs(domain="comic", conversation_id=conversation_id,
                                      limit=100000)
        project_ids = dict.fromkeys(run.state.get("project_id") for run in runs
                                   if run.state.get("project_id"))
        works = {}
        for project_id in project_ids:
            self.require_owner(project_id, conversation_id)
            snapshot = self.projects.get(project_id)
            briefs = {item.version: item for item in self.projects.brief_versions(project_id)}

            def entry(version, project_id=project_id, briefs=briefs, snapshot=snapshot):
                identity = f"{project_id}:brief:{version or 0}"
                if identity not in works:
                    brief = briefs.get(version)
                    works[identity] = {
                        "work_id": identity, "project_id": project_id,
                        "brief_version": version, "title": snapshot.project.title,
                        "original_request": brief.original_request if brief else "",
                        "current": version == snapshot.creative_brief.version,
                        "director_run_id": None, "has_director": False, "image_count": 0,
                    }
                return works[identity]

            for image in self.list(project_id):
                if image["conversation_id"] == conversation_id:
                    entry(image["source_brief_version"])["image_count"] += 1
            for run in sorted(runs, key=lambda item: item.started_at, reverse=True):
                if (run.state.get("project_id") != project_id
                        or run.workflow not in {"comic.director", "comic.director-spec"}):
                    continue
                version = run.state.get("director_spec_version")
                director = self.projects.get_director(project_id, version=version,
                    spec_id=run.state.get("director_spec_id")) if version else None
                brief_version = director.creative_brief_version if director else (
                    run.state.get("input_brief_version") or snapshot.creative_brief.version)
                item = entry(brief_version)
                if not item["director_run_id"]:
                    item.update(director_run_id=run.id, has_director=director is not None,
                                title=(item["original_request"] or snapshot.project.title)[:100])
        return {"works": list(works.values())}

    def list(self, project_id: str, *, brief_version: int | None = None) -> list[dict]:
        with self.projects._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM comic_entity_versions v "
                "WHERE project_id=? AND entity_type='image' AND version=(SELECT MAX(version) "
                "FROM comic_entity_versions WHERE project_id=v.project_id "
                "AND entity_type='image' AND entity_id=v.entity_id) ORDER BY created_at,entity_id",
                (project_id,),
            ).fetchall()
            result = [
                {
                    **ImageVersion.model_validate_json(row["payload_json"]).model_dump(mode="json"),
                    "selection": self._latest(
                        connection,
                        project_id,
                        "image_selection",
                        json.loads(row["payload_json"])["image_id"],
                    )[1]
                    or None,
                }
                for row in rows
            ]
            known = {item["image_id"] for item in result}
            legacy = connection.execute(
                "SELECT id FROM artifacts WHERE type='image' AND source='comic.external_image' "
                "AND status='ready' AND (lifecycle_state='active' OR EXISTS ("
                "SELECT 1 FROM comic_entity_versions s WHERE s.project_id=? "
                "AND s.entity_type='image_selection' AND s.entity_id=artifacts.id)) "
                "AND json_extract(metadata_json,'$.origin')='real' "
                "AND json_extract(metadata_json,'$.project_id')=? "
                "AND length(json_extract(metadata_json,'$.sha256'))=64 ORDER BY created_at,id",
                (project_id, project_id)).fetchall()
            for row in legacy:
                if row["id"] in known:
                    continue
                image = self.get(row["id"], version=1, connection=connection, _legacy_history=True)
                result.append({**image.model_dump(mode="json"),
                    "selection": self._latest(connection, project_id,
                                              "image_selection", image.image_id)[1] or None})
            for item in result:
                item["label"] = self.get(item["image_id"], version=1,
                                         connection=connection, _legacy_history=True).filename
                item["source_brief_version"] = self.image_brief_version(
                    item["image_id"], connection=connection)
                image = ImageVersion.model_validate_json(json.dumps({
                    key: item[key] for key in ImageVersion.model_fields}, ensure_ascii=False))
                view = self.image_view(image)
                item.update({key: view[key] for key in (
                    "available", "unavailable_reason", "lifecycle_state")})
                item["selection"] = self.selection_view(project_id, item["selection"],
                    conversation_id=item["conversation_id"], connection=connection)
            if brief_version is not None:
                result = [item for item in result
                          if (item["source_brief_version"] or 0) == brief_version]
            return sorted(result, key=lambda item: (item["created_at"], item["image_id"]))

    def select(self, image_id: str, request: ComicImageSelectionRequest) -> dict:
        image = self.get(image_id)
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            project_id = image.project_id
            operation = "image.select:" + image_id
            if saved := self.request_result(project_id, operation, request, connection=connection):
                return saved
            self._write_guard(connection, project_id, request)
            self.require_image(
                ImageVersionRef(image_id=image_id, version=request.version),
                project_id,
                request.conversation_id,
                connection=connection,
            )
            version, _ = self._latest(connection, project_id, "image_selection", image_id)
            if version != request.expected_selection_version:
                raise ToolError(
                    "所选图片已变化，请刷新后重试", detail="module=comic.media cas=selection"
                )
            result = {
                "image_id": image_id,
                "selected_version": request.version,
                "selection_version": version + 1,
            }
            self.projects._insert_version(
                connection,
                project_id=project_id,
                entity_type="image_selection",
                entity_id=image_id,
                version=version + 1,
                payload_json=json.dumps(result),
                created_at=utc_now(),
            )
            return self._finish(connection, project_id, operation, request, result)

    def delete_image(self, project_id: str, image_id: str, request: ComicMediaMutation) -> dict:
        """删除一张导入图片及其全部版本。

        与 Prompt、导演方案的版本删除同源：都要过作品级 CAS，也都要让作品修订 +1，
        历史链上不会出现"无记录的空档"。图片多两条硬引用，必须先拒绝：
          ① 故事编排已固定的图片（含已排进镜头的），删了会让故事材料失去来源；
          ② 正作为参考资产（identity/style/composition）使用的图片。
        删除只移除作品内的版本记录与该图片的草稿指针，图片产物转入回收站后仍可
        在资产库恢复或彻底清理，文件不在这里直接删除。
        """
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation = "image.delete:" + image_id
            if saved := self.request_result(project_id, operation, request, connection=connection):
                return saved
            self._write_guard(connection, project_id, request)
            image = self.get(image_id, connection=connection, _legacy_history=True)
            if image.project_id != project_id:
                raise ToolError("图片不属于该作品", detail="module=comic.media source=project")
            rows = connection.execute(
                "SELECT version,payload_json FROM comic_entity_versions "
                "WHERE project_id=? AND entity_type='image' AND entity_id=? ORDER BY version",
                (project_id, image_id),
            ).fetchall()
            versions = [int(row["version"]) for row in rows]
            self._require_deletable(connection, project_id, image_id)
            artifacts = list(dict.fromkeys(
                ImageVersion.model_validate_json(row["payload_json"]).artifact_id for row in rows
            ))
            # 旧分镜上传的图片以 Artifact ID 充当 image_id，v1 只是它的只读投影：
            # 不把它一起回收，删掉的图片下次读取会"复活"成 v1，新导入的图会被当成它的 v2。
            legacy = connection.execute(
                "SELECT metadata_json FROM artifacts WHERE id=?", (image_id,),
            ).fetchone()
            legacy_family = legacy is not None and json.loads(
                legacy["metadata_json"]).get("project_id") == project_id
            if legacy_family and image_id not in artifacts:
                artifacts.append(image_id)
            if not versions:
                if not legacy_family:
                    raise ToolError("找不到指定图片", detail="module=comic.media source=image")
                versions = [1]
            # 版本、采用指针与逐版本改图草稿同时消失，避免留下指向已删图片的悬空记录。
            connection.execute(
                "DELETE FROM comic_entity_versions WHERE project_id=? "
                "AND entity_type='image' AND entity_id=?",
                (project_id, image_id),
            )
            connection.execute(
                "DELETE FROM comic_entity_versions WHERE project_id=? "
                "AND entity_type='image_selection' AND entity_id=?",
                (project_id, image_id),
            )
            connection.execute(
                "DELETE FROM comic_entity_versions WHERE project_id=? "
                "AND entity_type='image_review_draft' AND entity_id LIKE ?",
                (project_id, f"{image_id}:%"),
            )
            result = self._finish(connection, project_id, operation, request, {
                "image_id": image_id, "filename": image.filename,
                "deleted_versions": versions,
            })
        # 事务提交后再改产物生命周期：Core 生命周期切换自带事务，不能嵌套在本事务里。
        # 版本记录已删除，媒体库靠产物过滤，这里失败只会让图片继续留在资产库，不影响工作台。
        for artifact_id in artifacts:
            self._trash_artifact(artifact_id)
        return result

    def _require_deletable(self, connection, project_id: str, image_id: str) -> None:
        image = self.get(image_id, connection=connection, _legacy_history=True)
        task = self._latest(connection, project_id, "workspace_task", project_id)[1]
        parent_id = task.get("work_id") or project_id
        rows = connection.execute(
            "SELECT entity_id,payload_json FROM comic_entity_versions v "
            "WHERE entity_type='story' AND (project_id IN (?,?) "
            "OR json_extract(payload_json,'$.conversation_id')=?) "
            "AND version=(SELECT MAX(version) "
            "FROM comic_entity_versions WHERE project_id=v.project_id "
            "AND entity_type='story' AND entity_id=v.entity_id)",
            (project_id, parent_id, image.conversation_id),
        ).fetchall()
        for row in rows:
            story = json.loads(row["payload_json"])
            fixed = [image for image in story.get("fixed_images") or []
                     if isinstance(image, dict) and image.get("image_id")]
            # 旧故事快照可能没有会话字段；图片身份全局唯一，不能因此漏掉硬引用。
            if any(item["image_id"] == image_id for item in fixed):
                raise ToolError(
                    "这张图片已在故事编排里固定，不能删除",
                    detail="module=comic.media reference=story.images",
                )
            if any(image_id == ((shot.get("image") or {}).get("image_id"))
                   for shot in story.get("shots") or [] if isinstance(shot, dict)):
                raise ToolError(
                    "这张图片已排进故事镜头，不能删除",
                    detail="module=comic.media reference=story.shots",
                )
        bindings = self.references(project_id, connection=connection).get("bindings") or []
        if any((binding.get("image") or {}).get("image_id") == image_id for binding in bindings):
            raise ToolError(
                "这张图片正作为参考资产使用，不能删除",
                detail="module=comic.media reference=bindings",
            )

    def _trash_artifact(self, artifact_id: str) -> None:
        """把已删除图片的产物移入回收站；不在正常状态时保持原样。"""
        try:
            artifact = self.runtime.get_artifact(artifact_id)
            if artifact.lifecycle_state != ArtifactLifecycle.ACTIVE:
                return
            self.runtime.change_artifact_lifecycle(
                artifact_id, ArtifactLifecycle.TRASHED, artifact.lifecycle_version)
        except ToolError as error:
            logger.warning("comic image artifact trash skipped artifact_id=%s error=%s",
                           artifact_id, error)

    def references(self, project_id: str, *, connection=None) -> dict:
        if connection is None:
            with self.projects._connect() as opened:
                return self.references(project_id, connection=opened)
        version, payload = self._latest(connection, project_id, "image_references", project_id)
        return payload or {"reference_version": version, "bindings": []}

    def bind_references(self, project_id: str, request: ComicReferenceRequest) -> dict:
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if saved := self.request_result(
                project_id, "references.bind", request, connection=connection
            ):
                return saved
            self._write_guard(connection, project_id, request)
            current = self.references(project_id, connection=connection)
            if current["reference_version"] != request.expected_reference_version:
                raise ToolError(
                    "参考用途已变化，请刷新后重试", detail="module=comic.media cas=reference"
                )
            for binding in request.bindings:
                self.require_image(
                    binding.image,
                    project_id,
                    request.conversation_id,
                    connection=connection,
                    allow_related=True,
                )
            result = {
                "reference_version": current["reference_version"] + 1,
                "conversation_id": request.conversation_id,
                "bindings": [item.model_dump(mode="json") for item in request.bindings],
            }
            self.projects._insert_version(
                connection,
                project_id=project_id,
                entity_type="image_references",
                entity_id=project_id,
                version=result["reference_version"],
                payload_json=json.dumps(result, ensure_ascii=False),
                created_at=utc_now(),
            )
            return self._finish(connection, project_id, "references.bind", request, result)

    def reference_context(self, project_id: str, conversation_id: str) -> dict:
        self.require_owner(project_id, conversation_id)
        snapshot = self.references(project_id)
        resolved = []
        for binding in snapshot["bindings"]:
            image = self.require_image(
                ImageVersionRef.model_validate(binding["image"]),
                project_id,
                conversation_id,
                allow_related=True,
            )
            resolved.append(
                {
                    **binding,
                    "artifact_id": image.artifact_id,
                    "source_project_id": image.project_id,
                    "sha256": image.sha256,
                    "filename": image.filename,
                    "observation_status": "not_analyzed",
                }
            )
        return {"reference_version": snapshot["reference_version"], "bindings": resolved}
