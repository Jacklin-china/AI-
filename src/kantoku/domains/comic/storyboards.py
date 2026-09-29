"""作品级分镜与镜头修订；内容属于 Comic，执行记录仍属于 Core。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from uuid import uuid4

from pydantic import ValidationError

from kantoku.config import ToolError
from kantoku.core.runtime.models import utc_now

from .assets import ComicAssetStore
from .models import (
    ComicAsset,
    ComicProjectSnapshot,
    ComicShot,
    ComicShotDraft,
    ComicStoryboard,
    ComicStoryboardDraft,
    ComicStoryboardPlan,
    DirectorSpec,
    ShotStatus,
    StoryboardStatus,
)
from .projects import ComicContextBuilder, ComicProjectStore


def plan_storyboard(
    snapshot: ComicProjectSnapshot, director: DirectorSpec, *, task: str | None,
    assets: list[ComicAsset], model_call: Callable[[list[dict[str, str]]], str],
) -> ComicStoryboardPlan:
    """用当前 Brief/Director/相关资产动态规划，不使用固定镜数或镜头模板。"""
    context = ComicContextBuilder.build(
        snapshot, task=task, director=director, assets=assets,
    )
    messages = [
        {"role": "system", "content": (
            "你是漫剧分镜规划师。根据当前故事目标、用户硬约束、导演方案与相关资产，"
            "动态决定镜头数量和节奏，不套用固定远中近模板。不生成图片 Prompt。"
            "只返回 JSON 对象：title、description、shots。shots 为非空数组，最多100镜。"
            "每镜包含 purpose、subject、action、environment、emotion、shot_size、"
            "camera_angle、camera_movement、character_asset_versions、"
            "scene_asset_versions、style_version。资产引用仅可使用上下文中存在的"
            "asset_id 与 version；没有依据时使用空列表或 null，禁止捏造资产。"
        )},
        {"role": "user", "content": json.dumps(context.model_dump(), ensure_ascii=False)},
    ]
    raw = model_call(messages)
    try:
        plan = ComicStoryboardPlan.model_validate_json(raw)
    except (ValueError, ValidationError) as error:
        raise ToolError("分镜模型未返回有效结构化规划", detail=type(error).__name__) from error
    allowed = {(asset.asset_id, asset.version) for asset in assets}
    for shot in plan.shots:
        refs = [*shot.character_asset_versions, *shot.scene_asset_versions]
        if shot.style_version is not None:
            refs.append(shot.style_version)
        if any((ref.asset_id, ref.version) not in allowed for ref in refs):
            raise ToolError("分镜模型引用了上下文之外的资产")
    return plan


class ComicStoryboardStore:
    """复用作品修订表；指针表只标识当前版本，旧记录永久保留。"""

    def __init__(self, projects: ComicProjectStore, assets: ComicAssetStore) -> None:
        self.projects = projects
        self.assets = assets

    def _project(self, connection: sqlite3.Connection, project_id: str, expected: int):
        row = connection.execute(
            "SELECT current_version FROM comic_projects WHERE project_id=?", (project_id,),
        ).fetchone()
        if row is None:
            raise ToolError("找不到指定漫剧作品", detail=project_id)
        if int(row["current_version"]) != expected:
            raise ToolError("作品已由其他操作更新，请刷新后重试")
        return self.projects._snapshot(connection, project_id, expected)

    def _storyboard(self, connection: sqlite3.Connection, storyboard_id: str) -> ComicStoryboard:
        row = connection.execute(
            "SELECT project_id,current_version FROM comic_storyboards WHERE storyboard_id=?",
            (storyboard_id,),
        ).fetchone()
        if row is None:
            raise ToolError("找不到指定分镜", detail=storyboard_id)
        return ComicStoryboard.model_validate_json(self.projects._version_payload(
            connection, row["project_id"], "storyboard", storyboard_id,
            int(row["current_version"]),
        ))

    def _shot(self, connection: sqlite3.Connection, shot_id: str) -> ComicShot:
        row = connection.execute(
            "SELECT project_id,current_version FROM comic_shots WHERE shot_id=?", (shot_id,),
        ).fetchone()
        if row is None:
            raise ToolError("找不到指定镜头", detail=shot_id)
        return ComicShot.model_validate_json(self.projects._version_payload(
            connection, row["project_id"], "shot", shot_id, int(row["current_version"]),
        ))

    def _validate_refs(self, project_id: str, shots: list[ComicShotDraft]) -> None:
        for shot in shots:
            groups = (
                (shot.character_asset_versions, "character"),
                (shot.scene_asset_versions, "scene"),
                ([shot.style_version] if shot.style_version else [], "style"),
            )
            seen: set[str] = set()
            for refs, kind in groups:
                for ref in refs:
                    if ref.asset_id in seen:
                        raise ToolError("镜头资产引用重复", detail=ref.asset_id)
                    seen.add(ref.asset_id)
                    current = self.assets.get(project_id, ref.asset_id)
                    asset = self.assets.get(project_id, ref.asset_id, version=ref.version)
                    if (current.state != "active" or asset.state != "active"
                            or asset.details.kind != kind):
                        raise ToolError("镜头资产引用类型或状态无效", detail=ref.asset_id)

    def _require_current_director(self, snapshot, storyboard: ComicStoryboard) -> None:
        if (snapshot.project.director_id is None
                or snapshot.project.director_version != storyboard.director_spec_version):
            raise ToolError("当前导演方案已变化，请基于新版方案新建分镜")
        director = self.projects.get_director(storyboard.project_id)
        self.projects.require_confirmed_director(director)
        self.assets.director_assets(director)

    def _insert_storyboard(
        self, connection: sqlite3.Connection, storyboard: ComicStoryboard,
    ) -> None:
        self.projects._insert_version(
            connection, project_id=storyboard.project_id, entity_type="storyboard",
            entity_id=storyboard.storyboard_id, version=storyboard.version,
            payload_json=storyboard.model_dump_json(), created_at=storyboard.created_at,
        )
        connection.execute(
            "UPDATE comic_storyboards SET current_version=?,status=?,updated_at=? "
            "WHERE storyboard_id=?",
            (storyboard.version, storyboard.status.value, storyboard.created_at.isoformat(),
             storyboard.storyboard_id),
        )

    def _insert_shot(self, connection: sqlite3.Connection, shot: ComicShot) -> None:
        self.projects._insert_version(
            connection, project_id=shot.project_id, entity_type="shot",
            entity_id=shot.shot_id, version=shot.version,
            payload_json=shot.model_dump_json(), created_at=shot.created_at,
        )
        connection.execute(
            "UPDATE comic_shots SET current_version=?,status=?,updated_at=? WHERE shot_id=?",
            (shot.version, shot.status.value, shot.created_at.isoformat(), shot.shot_id),
        )

    def create(
        self, project_id: str, draft: ComicStoryboardDraft, *,
        expected_project_version: int, director_spec_version: int,
        shots: list[ComicShotDraft] | None = None, source: str = "created",
    ) -> ComicStoryboard:
        planned = shots or []
        if len(planned) > 100:
            raise ToolError("单个分镜最多100个镜头")
        self._validate_refs(project_id, planned)
        director = self.projects.get_director(project_id)
        self.projects.require_confirmed_director(director)
        self.assets.director_assets(director)
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            snapshot = self._project(connection, project_id, expected_project_version)
            if (snapshot.project.director_id is None
                    or snapshot.project.director_version != director_spec_version):
                raise ToolError("分镜必须基于当前导演方案")
            new_project_version = self.projects._advance_project(
                connection, project_id, expected_project_version,
            )
            now = utc_now()
            storyboard_id = f"comic-storyboard-{uuid4().hex}"
            shot_ids = [f"comic-shot-{uuid4().hex}" for _ in planned]
            storyboard = ComicStoryboard(
                **draft.model_dump(), storyboard_id=storyboard_id, project_id=project_id,
                director_spec_version=director_spec_version, version=1,
                project_version=new_project_version,
                status=StoryboardStatus.PLANNING if planned else StoryboardStatus.DRAFT,
                shot_ids=shot_ids, created_at=now, source=source,
            )
            connection.execute(
                "INSERT INTO comic_storyboards "
                "(storyboard_id,project_id,current_version,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (storyboard_id, project_id, 1, storyboard.status.value,
                 now.isoformat(), now.isoformat()),
            )
            self._insert_storyboard(connection, storyboard)
            for sequence, (shot_id, item) in enumerate(
                zip(shot_ids, planned, strict=True), start=1,
            ):
                shot = ComicShot(
                    **item.model_dump(), shot_id=shot_id, storyboard_id=storyboard_id,
                    project_id=project_id, sequence_number=sequence, version=1,
                    project_version=new_project_version, status=ShotStatus.PLANNED,
                    created_at=now, source="model" if source == "model" else "created",
                )
                connection.execute(
                    "INSERT INTO comic_shots "
                    "(shot_id,storyboard_id,project_id,current_version,status,"
                    "created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (shot_id, storyboard_id, project_id, 1, shot.status.value,
                     now.isoformat(), now.isoformat()),
                )
                self._insert_shot(connection, shot)
        return storyboard

    def get(self, storyboard_id: str) -> ComicStoryboard:
        with self.projects._connect() as connection:
            return self._storyboard(connection, storyboard_id)

    def list(self, project_id: str) -> list[ComicStoryboard]:
        self.projects.get(project_id)
        with self.projects._connect() as connection:
            rows = connection.execute(
                "SELECT storyboard_id FROM comic_storyboards WHERE project_id=? "
                "ORDER BY created_at", (project_id,),
            ).fetchall()
            return [self._storyboard(connection, row["storyboard_id"]) for row in rows]

    def versions(self, storyboard_id: str) -> list[ComicStoryboard]:
        current = self.get(storyboard_id)
        with self.projects._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM comic_entity_versions WHERE project_id=? "
                "AND entity_type='storyboard' AND entity_id=? ORDER BY version DESC",
                (current.project_id, storyboard_id),
            ).fetchall()
        return [ComicStoryboard.model_validate_json(row["payload_json"]) for row in rows]

    def list_shots(self, storyboard_id: str) -> list[ComicShot]:
        with self.projects._connect() as connection:
            storyboard = self._storyboard(connection, storyboard_id)
            return [self._shot(connection, shot_id) for shot_id in storyboard.shot_ids]

    def get_shot(self, shot_id: str) -> ComicShot:
        with self.projects._connect() as connection:
            return self._shot(connection, shot_id)

    def shot_versions(self, shot_id: str) -> list[ComicShot]:
        current = self.get_shot(shot_id)
        with self.projects._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM comic_entity_versions WHERE project_id=? "
                "AND entity_type='shot' AND entity_id=? ORDER BY version DESC",
                (current.project_id, shot_id),
            ).fetchall()
        return [ComicShot.model_validate_json(row["payload_json"]) for row in rows]

    def _resequence(
        self, connection: sqlite3.Connection, shot_ids: list[str], project_version: int,
        *, skip_id: str | None = None,
    ) -> None:
        for number, shot_id in enumerate(shot_ids, start=1):
            if shot_id == skip_id:
                continue
            current = self._shot(connection, shot_id)
            if current.sequence_number == number:
                continue
            revised = current.model_copy(update={
                "sequence_number": number, "version": current.version + 1,
                "project_version": project_version, "created_at": utc_now(),
                "source": "reordered",
            })
            self._insert_shot(connection, revised)

    def edit(
        self, storyboard_id: str, *, expected_project_version: int,
        expected_version: int, draft: ComicStoryboardDraft, status: StoryboardStatus,
        shot_ids: list[str] | None = None,
    ) -> ComicStoryboard:
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._storyboard(connection, storyboard_id)
            snapshot = self._project(connection, current.project_id, expected_project_version)
            self._require_current_director(snapshot, current)
            if current.version != expected_version:
                raise ToolError("分镜已由其他操作更新，请刷新后重试")
            order = current.shot_ids if shot_ids is None else shot_ids
            if len(order) != len(set(order)) or set(order) != set(current.shot_ids):
                raise ToolError("重排必须包含当前分镜的每个镜头且不得重复")
            if current.status == StoryboardStatus.ARCHIVED and status != StoryboardStatus.ARCHIVED:
                raise ToolError("已归档分镜须通过版本恢复后再编辑")
            project_version = self.projects._advance_project(
                connection, current.project_id, expected_project_version,
            )
            self._resequence(connection, order, project_version)
            revised = current.model_copy(update={
                **draft.model_dump(), "shot_ids": order, "status": status,
                "version": current.version + 1, "project_version": project_version,
                "created_at": utc_now(),
                "source": "reordered" if order != current.shot_ids else "edited",
            })
            self._insert_storyboard(connection, revised)
        return revised

    def restore(
        self, storyboard_id: str, *, expected_project_version: int,
        expected_version: int, version: int,
    ) -> ComicStoryboard:
        historical = next((item for item in self.versions(storyboard_id)
                           if item.version == version), None)
        if historical is None:
            raise ToolError("找不到指定分镜版本")
        current = self.get(storyboard_id)
        if set(historical.shot_ids) != set(current.shot_ids):
            raise ToolError("历史分镜的镜头集合已变化，请先恢复镜头")
        return self._restore_storyboard(current, historical, expected_project_version,
                                        expected_version)

    def _restore_storyboard(
        self, current: ComicStoryboard, historical: ComicStoryboard,
        expected_project_version: int, expected_version: int,
    ) -> ComicStoryboard:
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            fresh = self._storyboard(connection, current.storyboard_id)
            snapshot = self._project(connection, current.project_id, expected_project_version)
            self._require_current_director(snapshot, current)
            if fresh.version != expected_version:
                raise ToolError("分镜已由其他操作更新，请刷新后重试")
            project_version = self.projects._advance_project(
                connection, current.project_id, expected_project_version,
            )
            self._resequence(connection, historical.shot_ids, project_version)
            revised = fresh.model_copy(update={
                "title": historical.title, "description": historical.description,
                "status": historical.status, "shot_ids": historical.shot_ids,
                "version": fresh.version + 1, "project_version": project_version,
                "created_at": utc_now(), "source": "restored",
                "restored_from_version": historical.version,
            })
            self._insert_storyboard(connection, revised)
        return revised

    def add_shot(
        self, storyboard_id: str, draft: ComicShotDraft, *,
        expected_project_version: int, expected_storyboard_version: int,
    ) -> ComicShot:
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            storyboard = self._storyboard(connection, storyboard_id)
            snapshot = self._project(
                connection, storyboard.project_id, expected_project_version,
            )
            self._require_current_director(snapshot, storyboard)
            if storyboard.version != expected_storyboard_version:
                raise ToolError("分镜已由其他操作更新，请刷新后重试")
            if len(storyboard.shot_ids) >= 100:
                raise ToolError("单个分镜最多100个镜头")
            if storyboard.status == StoryboardStatus.ARCHIVED:
                raise ToolError("已归档分镜不能新增镜头")
            self._validate_refs(storyboard.project_id, [draft])
            project_version = self.projects._advance_project(
                connection, storyboard.project_id, expected_project_version,
            )
            now = utc_now()
            shot_id = f"comic-shot-{uuid4().hex}"
            shot = ComicShot(
                **draft.model_dump(), shot_id=shot_id, storyboard_id=storyboard_id,
                project_id=storyboard.project_id,
                sequence_number=len(storyboard.shot_ids) + 1, version=1,
                project_version=project_version, status=ShotStatus.DRAFT,
                created_at=now, source="created",
            )
            connection.execute(
                "INSERT INTO comic_shots "
                "(shot_id,storyboard_id,project_id,current_version,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (shot_id, storyboard_id, storyboard.project_id, 1,
                 shot.status.value, now.isoformat(), now.isoformat()),
            )
            self._insert_shot(connection, shot)
            revised = storyboard.model_copy(update={
                "shot_ids": [*storyboard.shot_ids, shot_id],
                "version": storyboard.version + 1, "project_version": project_version,
                "created_at": now, "source": "edited",
            })
            self._insert_storyboard(connection, revised)
        return shot

    def edit_shot(
        self, shot_id: str, draft: ComicShotDraft, *, expected_project_version: int,
        expected_version: int, status: ShotStatus,
    ) -> ComicShot:
        if status not in {ShotStatus.DRAFT, ShotStatus.PLANNED}:
            raise ToolError("生产状态由后续执行阶段管理，本阶段只能编辑草稿或已规划镜头")
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._shot(connection, shot_id)
            storyboard = self._storyboard(connection, current.storyboard_id)
            snapshot = self._project(connection, current.project_id, expected_project_version)
            self._require_current_director(snapshot, storyboard)
            if current.version != expected_version:
                raise ToolError("镜头已由其他操作更新，请刷新后重试")
            if current.status == ShotStatus.DELETED:
                raise ToolError("已删除镜头须先恢复")
            if storyboard.status == StoryboardStatus.ARCHIVED:
                raise ToolError("已归档分镜不能编辑镜头")
            self._validate_refs(current.project_id, [draft])
            project_version = self.projects._advance_project(
                connection, current.project_id, expected_project_version,
            )
            revised = current.model_copy(update={
                **{name: getattr(draft, name) for name in ComicShotDraft.model_fields},
                "status": status, "version": current.version + 1,
                "project_version": project_version, "created_at": utc_now(),
                "source": "edited",
            })
            self._insert_shot(connection, revised)
        return revised

    def delete_shot(
        self, shot_id: str, *, expected_project_version: int, expected_version: int,
    ) -> ComicShot:
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._shot(connection, shot_id)
            storyboard = self._storyboard(connection, current.storyboard_id)
            snapshot = self._project(connection, current.project_id, expected_project_version)
            self._require_current_director(snapshot, storyboard)
            if current.version != expected_version or shot_id not in storyboard.shot_ids:
                raise ToolError("镜头已由其他操作更新，请刷新后重试")
            project_version = self.projects._advance_project(
                connection, current.project_id, expected_project_version,
            )
            now = utc_now()
            revised = current.model_copy(update={
                "status": ShotStatus.DELETED, "version": current.version + 1,
                "project_version": project_version, "created_at": now, "source": "deleted",
            })
            self._insert_shot(connection, revised)
            order = [item for item in storyboard.shot_ids if item != shot_id]
            self._resequence(connection, order, project_version)
            self._insert_storyboard(connection, storyboard.model_copy(update={
                "shot_ids": order, "version": storyboard.version + 1,
                "project_version": project_version, "created_at": now, "source": "edited",
            }))
        return revised

    def restore_shot(
        self, shot_id: str, *, expected_project_version: int, expected_version: int,
        version: int,
    ) -> ComicShot:
        historical = next((item for item in self.shot_versions(shot_id)
                           if item.version == version), None)
        if historical is None or historical.status == ShotStatus.DELETED:
            raise ToolError("找不到可恢复的镜头版本")
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._shot(connection, shot_id)
            storyboard = self._storyboard(connection, current.storyboard_id)
            snapshot = self._project(connection, current.project_id, expected_project_version)
            self._require_current_director(snapshot, storyboard)
            if current.version != expected_version:
                raise ToolError("镜头已由其他操作更新，请刷新后重试")
            self._validate_refs(current.project_id, [ComicShotDraft.model_validate(
                historical.model_dump(include=set(ComicShotDraft.model_fields))
            )])
            project_version = self.projects._advance_project(
                connection, current.project_id, expected_project_version,
            )
            now = utc_now()
            order = storyboard.shot_ids
            if shot_id not in order:
                index = min(historical.sequence_number - 1, len(order))
                order = [*order[:index], shot_id, *order[index:]]
            self._resequence(connection, order, project_version, skip_id=shot_id)
            revised = historical.model_copy(update={
                "sequence_number": order.index(shot_id) + 1,
                "version": current.version + 1, "project_version": project_version,
                "created_at": now, "source": "restored",
                "restored_from_version": historical.version,
            })
            self._insert_shot(connection, revised)
            self._insert_storyboard(connection, storyboard.model_copy(update={
                "shot_ids": order, "version": storyboard.version + 1,
                "project_version": project_version, "created_at": now, "source": "edited",
            }))
        return revised
