"""作品范围的角色、场景与风格资产；复用 Comic 修订表和 Core Artifact。"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Literal
from uuid import uuid4

from kantoku.config import ToolError
from kantoku.core.runtime.models import ArtifactType, utc_now
from kantoku.core.runtime.store import RuntimeStore

from .models import ComicAsset, ComicAssetDraft, ComicAssetRef
from .projects import ComicProjectStore


class ComicAssetStore:
    """资产是领域内容；参考图仍只存在于共享 Artifact Store。"""

    def __init__(self, projects: ComicProjectStore, runtime: RuntimeStore) -> None:
        self.projects = projects
        self.runtime = runtime

    def _validate_references(self, draft: ComicAssetDraft) -> None:
        for artifact_id in draft.reference_artifact_ids:
            artifact = self.runtime.get_artifact(artifact_id)
            if (artifact.type != ArtifactType.IMAGE or artifact.status != "ready"
                    or not artifact.location):
                raise ToolError("参考素材必须是已就绪的图片 Artifact")

    def _current(
        self, connection: sqlite3.Connection, project_id: str, asset_id: str,
    ) -> ComicAsset:
        row = connection.execute(
            "SELECT current_version FROM comic_assets WHERE project_id=? AND asset_id=?",
            (project_id, asset_id),
        ).fetchone()
        if row is None:
            raise ToolError("找不到指定作品资产")
        return ComicAsset.model_validate_json(self.projects._version_payload(
            connection, project_id, "comic_asset", asset_id, int(row["current_version"]),
        ))

    def _write_project(
        self, connection: sqlite3.Connection, project_id: str, current_version: int,
    ) -> int:
        snapshot = self.projects._snapshot(connection, project_id, current_version)
        now = utc_now()
        revised = snapshot.project.model_copy(update={
            "current_version": current_version + 1, "updated_at": now,
        })
        result = connection.execute(
            "UPDATE comic_projects SET current_version=?,updated_at=? "
            "WHERE project_id=? AND current_version=?",
            (revised.current_version, now.isoformat(), project_id, current_version),
        )
        if result.rowcount != 1:
            raise ToolError("作品已由其他操作更新，请刷新后重试")
        self.projects._insert_version(
            connection, project_id=project_id, entity_type="project", entity_id=project_id,
            version=revised.current_version, payload_json=revised.model_dump_json(),
            created_at=now,
        )
        return revised.current_version

    @staticmethod
    def _draft(asset: ComicAsset) -> ComicAssetDraft:
        return ComicAssetDraft.model_validate(
            asset.model_dump(include=set(ComicAssetDraft.model_fields))
        )

    def create(
        self, project_id: str, draft: ComicAssetDraft, *, expected_project_version: int,
    ) -> ComicAsset:
        self._validate_references(draft)
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT current_version FROM comic_projects WHERE project_id=?", (project_id,),
            ).fetchone()
            if row is None:
                raise ToolError("找不到指定漫剧作品")
            if int(row["current_version"]) != expected_project_version:
                raise ToolError("作品已由其他操作更新，请刷新后重试")
            project_version = self._write_project(
                connection, project_id, expected_project_version,
            )
            now = utc_now()
            asset = ComicAsset(
                **draft.model_dump(), asset_id=f"comic-asset-{uuid4().hex}",
                project_id=project_id, version=1, project_version=project_version,
                created_at=now, source="created",
            )
            connection.execute(
                "INSERT INTO comic_assets "
                "(asset_id,project_id,kind,name,current_version,state,pinned_version,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (asset.asset_id, project_id, asset.details.kind, asset.name, 1,
                 asset.state, None, now.isoformat(), now.isoformat()),
            )
            self.projects._insert_version(
                connection, project_id=project_id, entity_type="comic_asset",
                entity_id=asset.asset_id, version=1, payload_json=asset.model_dump_json(),
                created_at=now,
            )
        return asset

    def get(
        self, project_id: str, asset_id: str, *, version: int | None = None,
    ) -> ComicAsset:
        with self.projects._connect() as connection:
            current = self._current(connection, project_id, asset_id)
            if version is None:
                return current
            return ComicAsset.model_validate_json(self.projects._version_payload(
                connection, project_id, "comic_asset", asset_id, version,
            ))

    def versions(self, project_id: str, asset_id: str) -> list[ComicAsset]:
        with self.projects._connect() as connection:
            self._current(connection, project_id, asset_id)
            rows = connection.execute(
                "SELECT payload_json FROM comic_entity_versions WHERE project_id=? "
                "AND entity_type='comic_asset' AND entity_id=? ORDER BY version DESC",
                (project_id, asset_id),
            ).fetchall()
        return [ComicAsset.model_validate_json(row["payload_json"]) for row in rows]

    def list(
        self, project_id: str, *, project_version: int | None = None,
        include_deleted: bool = False,
    ) -> list[ComicAsset]:
        snapshot = self.projects.get(project_id, version=project_version)
        with self.projects._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM comic_entity_versions WHERE project_id=? "
                "AND entity_type='comic_asset' ORDER BY entity_id,version DESC",
                (project_id,),
            ).fetchall()
        chosen: dict[str, ComicAsset] = {}
        for row in rows:
            asset = ComicAsset.model_validate_json(row["payload_json"])
            if (asset.asset_id not in chosen
                    and asset.project_version <= snapshot.project.current_version):
                chosen[asset.asset_id] = asset
        return sorted(
            (asset for asset in chosen.values() if include_deleted or asset.state == "active"),
            key=lambda asset: (asset.details.kind, asset.name, asset.asset_id),
        )

    def change(
        self, project_id: str, asset_id: str, *, expected_project_version: int,
        expected_asset_version: int, action: Literal["edit", "restore", "lock", "delete"],
        draft: ComicAssetDraft | None = None, target_version: int | None = None,
    ) -> ComicAsset:
        """所有编辑、锁定、删除及恢复都追加资产与作品修订。"""
        with self.projects._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._current(connection, project_id, asset_id)
            row = connection.execute(
                "SELECT current_version FROM comic_projects WHERE project_id=?", (project_id,),
            ).fetchone()
            if (row is None or int(row["current_version"]) != expected_project_version
                    or current.version != expected_asset_version):
                raise ToolError("作品或资产已更新，请刷新后重试")
            if action == "edit":
                if draft is None or current.state != "active":
                    raise ToolError("只有有效资产可以编辑")
                if draft.details.kind != current.details.kind:
                    raise ToolError("资产类型不可修改")
                next_draft = draft
                state = "active"
                pin = current.pinned_version
                restored_from = None
                if next_draft == self._draft(current):
                    return current
            elif action == "restore":
                if target_version is None:
                    raise ToolError("需要指定恢复版本")
                historical = ComicAsset.model_validate_json(self.projects._version_payload(
                    connection, project_id, "comic_asset", asset_id, target_version,
                ))
                if historical.state != "active":
                    raise ToolError("不能恢复已删除的资产版本")
                next_draft = self._draft(historical)
                state, pin, restored_from = "active", None, target_version
            elif action == "lock":
                if current.state != "active":
                    raise ToolError("已删除资产不能锁定")
                if target_version is not None:
                    historical = ComicAsset.model_validate_json(self.projects._version_payload(
                        connection, project_id, "comic_asset", asset_id, target_version,
                    ))
                    if historical.state != "active":
                        raise ToolError("只能锁定有效资产版本")
                if current.pinned_version == target_version:
                    return current
                next_draft = self._draft(current)
                state, pin, restored_from = "active", target_version, None
            elif action == "delete":
                if current.state == "deleted":
                    return current
                next_draft = self._draft(current)
                state, pin, restored_from = "deleted", None, None
            else:
                raise ToolError("资产操作不受支持")
            self._validate_references(next_draft)
            project_version = self._write_project(
                connection, project_id, expected_project_version,
            )
            now = utc_now()
            source = {
                "edit": "edited", "restore": "restored", "lock": "locked", "delete": "deleted",
            }[action]
            revised = ComicAsset(
                **next_draft.model_dump(), asset_id=asset_id, project_id=project_id,
                version=current.version + 1, project_version=project_version,
                created_at=now, state=state, pinned_version=pin, source=source,
                restored_from_version=restored_from,
            )
            result = connection.execute(
                "UPDATE comic_assets SET name=?,current_version=?,state=?,pinned_version=?,"
                "updated_at=? WHERE project_id=? AND asset_id=? AND current_version=?",
                (revised.name, revised.version, state, pin, now.isoformat(), project_id,
                 asset_id, current.version),
            )
            if result.rowcount != 1:
                raise ToolError("资产已由其他操作更新，请刷新后重试")
            self.projects._insert_version(
                connection, project_id=project_id, entity_type="comic_asset",
                entity_id=asset_id, version=revised.version,
                payload_json=revised.model_dump_json(), created_at=now,
            )
        return revised

    def select_relevant(
        self, project_id: str, *, task: str | None = None,
        refs: Sequence[ComicAssetRef] = (), project_version: int | None = None,
        limit: int = 8,
    ) -> list[ComicAsset]:
        """显式引用优先；其余仅按真实名称/别名召回，不伪称语义检索。"""
        if task is not None and len(task) > 1000:
            raise ToolError("当前任务描述过长")
        if len(refs) > limit:
            raise ToolError("单次上下文引用的资产过多")
        if len({ref.asset_id for ref in refs}) != len(refs):
            raise ToolError("当前任务存在重复资产引用")
        snapshot = self.projects.get(project_id, version=project_version)
        available = self.list(project_id, project_version=snapshot.project.current_version)
        by_id = {asset.asset_id: asset for asset in available}
        selected: dict[str, ComicAsset] = {}

        def add(asset: ComicAsset, *, version: int | None = None) -> None:
            if asset.asset_id in selected:
                return
            resolved_version = version if version is not None else asset.pinned_version
            resolved = self.get(project_id, asset.asset_id, version=resolved_version)
            if (resolved.state != "active"
                    or resolved.project_version > snapshot.project.current_version):
                raise ToolError("引用的资产版本不可用于当前作品上下文")
            selected[asset.asset_id] = resolved

        for ref in refs:
            asset = by_id.get(ref.asset_id)
            if asset is None:
                raise ToolError("引用的资产不属于当前作品或已删除")
            add(asset, version=ref.version)
        if task:
            lowered = task.casefold()
            for asset in available:
                if len(selected) >= limit:
                    break
                names = [asset.name, *asset.aliases]
                if any(len(name) >= 2 and name.casefold() in lowered for name in names):
                    add(asset)
        styles = [asset for asset in available if asset.details.kind == "style"]
        if len(styles) == 1 and len(selected) < limit:
            add(styles[0])
        return list(selected.values())
