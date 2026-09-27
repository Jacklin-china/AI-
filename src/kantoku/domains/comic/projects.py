"""作品级创作上下文；使用现有 SQLite，不复制 Core Runtime。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import ValidationError

from kantoku.config import ToolError
from kantoku.core.runtime.models import utc_now

from .models import (
    ComicAsset,
    ComicContext,
    ComicProjectInput,
    ComicProjectSnapshot,
    CreativeBrief,
    CreativeBriefInput,
    CreativeBriefUpdate,
    CreativeProject,
    DirectorSpec,
    DirectorSpecDraft,
    ProjectStatus,
)

_SCHEMA_VERSION = 3


class ComicProjectStore:
    """在 Core 所用数据库中保存 Comic 专属作品与不可变修订。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
            connection.commit()
        except (sqlite3.Error, OSError) as error:
            if connection is not None:
                connection.rollback()
            raise ToolError("漫剧作品数据库操作失败", detail=type(error).__name__) from error
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def migrate(self) -> None:
        """只增表，不读取、覆盖或删除旧 Studio/shot/persona/recipe 记录。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS comic_schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {int(row["version"]) for row in connection.execute(
                "SELECT version FROM comic_schema_migrations"
            )}
            if 1 not in applied:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS comic_projects ("
                    "project_id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL, "
                    "status TEXT NOT NULL CHECK(status IN "
                    "('draft','planning','production','completed','archived')), "
                    "current_version INTEGER NOT NULL CHECK(current_version > 0), "
                    "brief_id TEXT NOT NULL, brief_version INTEGER NOT NULL "
                    "CHECK(brief_version > 0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS comic_entity_versions ("
                    "project_id TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, "
                    "version INTEGER NOT NULL CHECK(version > 0), payload_json TEXT NOT NULL, "
                    "created_at TEXT NOT NULL, "
                    "PRIMARY KEY(project_id, entity_type, entity_id, version), "
                    "FOREIGN KEY(project_id) REFERENCES comic_projects(project_id))"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_comic_entity_latest ON comic_entity_versions "
                    "(project_id, entity_type, entity_id, version DESC)"
                )
                connection.execute(
                    "INSERT INTO comic_schema_migrations(version,applied_at) VALUES (?,?)",
                    (1, utc_now().isoformat()),
                )
            if 2 not in applied:
                connection.execute("ALTER TABLE comic_projects ADD COLUMN director_id TEXT")
                connection.execute("ALTER TABLE comic_projects ADD COLUMN director_version INTEGER")
                connection.execute(
                    "INSERT INTO comic_schema_migrations(version,applied_at) VALUES (?,?)",
                    (2, utc_now().isoformat()),
                )
            if _SCHEMA_VERSION not in applied:
                connection.execute(
                    "CREATE TABLE comic_assets ("
                    "asset_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
                    "kind TEXT NOT NULL CHECK(kind IN ('character','scene','style')), "
                    "name TEXT NOT NULL, current_version INTEGER NOT NULL, "
                    "state TEXT NOT NULL CHECK(state IN ('active','deleted')), "
                    "pinned_version INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                    "FOREIGN KEY(project_id) REFERENCES comic_projects(project_id))"
                )
                connection.execute(
                    "CREATE INDEX idx_comic_assets_project ON comic_assets "
                    "(project_id,kind,state)"
                )
                connection.execute(
                    "INSERT INTO comic_schema_migrations(version,applied_at) VALUES (?,?)",
                    (_SCHEMA_VERSION, utc_now().isoformat()),
                )

    @staticmethod
    def _insert_version(
        connection: sqlite3.Connection, *, project_id: str, entity_type: str,
        entity_id: str, version: int, payload_json: str, created_at: datetime,
    ) -> None:
        connection.execute(
            "INSERT INTO comic_entity_versions "
            "(project_id,entity_type,entity_id,version,payload_json,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (project_id, entity_type, entity_id, version, payload_json, created_at.isoformat()),
        )

    @staticmethod
    def _version_payload(
        connection: sqlite3.Connection, project_id: str, entity_type: str,
        entity_id: str, version: int,
    ) -> str:
        row = connection.execute(
            "SELECT payload_json FROM comic_entity_versions "
            "WHERE project_id=? AND entity_type=? AND entity_id=? AND version=?",
            (project_id, entity_type, entity_id, version),
        ).fetchone()
        if row is None:
            raise ToolError("找不到指定作品版本")
        return str(row["payload_json"])

    def _snapshot(
        self, connection: sqlite3.Connection, project_id: str, version: int,
    ) -> ComicProjectSnapshot:
        try:
            project = CreativeProject.model_validate_json(self._version_payload(
                connection, project_id, "project", project_id, version,
            ))
            brief = CreativeBrief.model_validate_json(self._version_payload(
                connection, project_id, "creative_brief", project.brief_id,
                project.brief_version,
            ))
        except (ValueError, ValidationError) as error:
            raise ToolError("漫剧作品历史版本损坏", detail=type(error).__name__) from error
        if brief.project_id != project.project_id:
            raise ToolError("漫剧作品与创作理解的关联不一致")
        return ComicProjectSnapshot(project=project, creative_brief=brief)

    def create(self, data: ComicProjectInput) -> ComicProjectSnapshot:
        """一次事务创建作品和 Brief v1；未提供的偏好保持为空，不伪造 AI 理解。"""
        now = utc_now()
        project_id = f"comic-project-{uuid4().hex}"
        brief_id = f"comic-brief-{uuid4().hex}"
        brief_input = data.brief or CreativeBriefInput(original_request=data.title)
        brief = CreativeBrief(
            **brief_input.model_dump(), brief_id=brief_id, project_id=project_id,
            version=1, created_at=now,
        )
        project = CreativeProject(
            project_id=project_id, title=data.title, description=data.description,
            status=ProjectStatus.DRAFT, created_at=now, updated_at=now,
            current_version=1, brief_id=brief_id, brief_version=1,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO comic_projects "
                "(project_id,title,description,status,current_version,brief_id,brief_version,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (project_id, project.title, project.description, project.status.value,
                 1, brief_id, 1, now.isoformat(), now.isoformat()),
            )
            self._insert_version(
                connection, project_id=project_id, entity_type="project",
                entity_id=project_id, version=1, payload_json=project.model_dump_json(),
                created_at=now,
            )
            self._insert_version(
                connection, project_id=project_id, entity_type="creative_brief",
                entity_id=brief_id, version=1, payload_json=brief.model_dump_json(),
                created_at=now,
            )
        return ComicProjectSnapshot(project=project, creative_brief=brief)

    def get(self, project_id: str, *, version: int | None = None) -> ComicProjectSnapshot:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT current_version FROM comic_projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if row is None:
                raise ToolError("找不到指定漫剧作品")
            requested_version = int(row["current_version"]) if version is None else version
            return self._snapshot(connection, project_id, requested_version)

    def replace_brief(
        self, project_id: str, data: CreativeBriefUpdate,
    ) -> ComicProjectSnapshot:
        """完整替换 Brief，使用作品版本做 CAS；重复相同 PUT 不新增修订。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT current_version FROM comic_projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if row is None:
                raise ToolError("找不到指定漫剧作品")
            current_version = int(row["current_version"])
            current = self._snapshot(connection, project_id, current_version)
            content = data.model_dump(exclude={"expected_version"})
            previous_content = current.creative_brief.model_dump(exclude={
                "brief_id", "project_id", "version", "created_at",
            })
            if content == previous_content:
                return current
            if data.expected_version != current_version:
                raise ToolError(
                    "作品已由其他操作更新，请刷新后重试",
                    detail=f"expected={data.expected_version}; current={current_version}",
                )
            now = utc_now()
            brief = CreativeBrief(
                **content, brief_id=current.project.brief_id, project_id=project_id,
                version=current.creative_brief.version + 1, created_at=now,
            )
            project = current.project.model_copy(update={
                "updated_at": now, "current_version": current_version + 1,
                "brief_version": brief.version,
                "director_id": None, "director_version": None,
            })
            connection.execute(
                "UPDATE comic_projects SET current_version=?,brief_version=?,updated_at=?,"
                "director_id=NULL,director_version=NULL "
                "WHERE project_id=? AND current_version=?",
                (project.current_version, brief.version, now.isoformat(),
                 project_id, current_version),
            )
            self._insert_version(
                connection, project_id=project_id, entity_type="creative_brief",
                entity_id=brief.brief_id, version=brief.version,
                payload_json=brief.model_dump_json(), created_at=now,
            )
            self._insert_version(
                connection, project_id=project_id, entity_type="project",
                entity_id=project_id, version=project.current_version,
                payload_json=project.model_dump_json(), created_at=now,
            )
        return ComicProjectSnapshot(project=project, creative_brief=brief)

    def get_director(
        self, project_id: str, *, project_version: int | None = None,
    ) -> DirectorSpec:
        snapshot = self.get(project_id, version=project_version)
        project = snapshot.project
        if project.director_id is None or project.director_version is None:
            raise ToolError("当前作品尚无导演方案")
        with self._connect() as connection:
            return DirectorSpec.model_validate_json(self._version_payload(
                connection, project_id, "director_spec", project.director_id,
                project.director_version,
            ))

    def director_versions(self, project_id: str) -> list[DirectorSpec]:
        snapshot = self.get(project_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM comic_entity_versions "
                "WHERE project_id=? AND entity_type='director_spec' ORDER BY version DESC",
                (snapshot.project.project_id,),
            ).fetchall()
        return [DirectorSpec.model_validate_json(row["payload_json"]) for row in rows]

    def save_director(
        self, project_id: str, draft: DirectorSpecDraft, *,
        expected_project_version: int,
        source: Literal["model", "manual", "restored"] = "manual",
        restored_from_version: int | None = None,
    ) -> DirectorSpec:
        """CAS 追加导演修订；Brief 改动会清除当前指针但保留历史。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT current_version FROM comic_projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if row is None:
                raise ToolError("找不到指定漫剧作品")
            current_version = int(row["current_version"])
            if current_version != expected_project_version:
                raise ToolError("作品已由其他操作更新，请刷新后重试")
            snapshot = self._snapshot(connection, project_id, current_version)
            project = snapshot.project
            missing = [
                item for item in snapshot.creative_brief.hard_constraints
                if item not in draft.constraints
            ]
            if missing:
                raise ToolError("导演方案不得遗漏创作理解中的硬约束")
            version_row = connection.execute(
                "SELECT entity_id,version FROM comic_entity_versions "
                "WHERE project_id=? AND entity_type='director_spec' "
                "ORDER BY version DESC LIMIT 1", (project_id,),
            ).fetchone()
            spec_id = (
                str(version_row["entity_id"])
                if version_row else f"comic-director-{uuid4().hex}"
            )
            next_version = int(version_row["version"]) + 1 if version_row else 1
            now = utc_now()
            spec = DirectorSpec(
                **draft.model_dump(), spec_id=spec_id, project_id=project_id,
                creative_brief_version=snapshot.creative_brief.version,
                version=next_version, created_at=now, source=source,
                restored_from_version=restored_from_version,
            )
            revised = project.model_copy(update={
                "director_id": spec_id, "director_version": next_version,
                "current_version": current_version + 1, "updated_at": now,
            })
            connection.execute(
                "UPDATE comic_projects SET director_id=?,director_version=?,"
                "current_version=?,updated_at=? WHERE project_id=?",
                (spec_id, next_version, revised.current_version, now.isoformat(), project_id),
            )
            self._insert_version(
                connection, project_id=project_id, entity_type="director_spec",
                entity_id=spec_id, version=next_version,
                payload_json=spec.model_dump_json(), created_at=now,
            )
            self._insert_version(
                connection, project_id=project_id, entity_type="project",
                entity_id=project_id, version=revised.current_version,
                payload_json=revised.model_dump_json(), created_at=now,
            )
        return spec

    def restore_director(
        self, project_id: str, *, version: int, expected_project_version: int,
    ) -> DirectorSpec:
        snapshot = self.get(project_id)
        if snapshot.project.current_version != expected_project_version:
            raise ToolError("作品已由其他操作更新，请刷新后重试")
        matches = [item for item in self.director_versions(project_id) if item.version == version]
        if not matches:
            raise ToolError("找不到指定导演方案版本")
        historical = matches[0]
        if historical.creative_brief_version != snapshot.creative_brief.version:
            raise ToolError("导演方案关联旧版创作理解，请重新生成或编辑")
        draft = DirectorSpecDraft.model_validate(
            historical.model_dump(include=set(DirectorSpecDraft.model_fields))
        )
        return self.save_director(
            project_id, draft, expected_project_version=expected_project_version,
            source="restored", restored_from_version=version,
        )


class ComicContextBuilder:
    """只组合调用方确实选中的导演方案与资产；不读聊天全文。"""

    @staticmethod
    def build(
        snapshot: ComicProjectSnapshot, *, task: str | None = None,
        director: DirectorSpec | None = None,
        assets: list[ComicAsset] | None = None,
    ) -> ComicContext:
        description = task.strip() if task is not None else None
        if description is not None and len(description) > 1000:
            raise ToolError("当前任务描述过长")
        project = snapshot.project
        brief = snapshot.creative_brief
        stable_context = {
            "project": {
                "project_id": project.project_id,
                "title": project.title,
                "description": project.description,
            },
            "creative_brief": {
                "original_request": brief.original_request,
                "hard_constraints": brief.hard_constraints,
                "soft_preferences": brief.soft_preferences,
                "creative_freedom": brief.creative_freedom,
            },
        }
        source_versions = {
            "project": project.current_version,
            "creative_brief": brief.version,
        }
        if director is not None:
            if (director.project_id != project.project_id
                    or director.creative_brief_version != brief.version):
                raise ToolError("导演方案与当前作品或创作理解版本不一致")
            stable_context["director_spec"] = director.model_dump(include={
                "visual_direction", "storytelling_goal", "camera_language", "composition",
                "lighting", "color_language", "emotion", "character_focus", "constraints",
                "creative_choices",
            })
            source_versions["director_spec"] = director.version
        if len(assets or []) > 8:
            raise ToolError("单次上下文引用的资产过多")
        relevant_memory: list[dict[str, object]] = []
        seen_assets: set[str] = set()
        for asset in assets or []:
            if asset.asset_id in seen_assets:
                raise ToolError("上下文中存在重复资产引用")
            seen_assets.add(asset.asset_id)
            if (asset.project_id != project.project_id
                    or asset.project_version > project.current_version):
                raise ToolError("资产与当前作品或版本不一致")
            if asset.state != "active":
                raise ToolError("已删除资产不能进入创作上下文")
            relevant_memory.append(asset.model_dump(include={
                "asset_id", "name", "aliases", "details", "fixed_constraints",
                "reference_artifact_ids", "tags", "version",
            }))
            source_versions[f"asset:{asset.asset_id}"] = asset.version
        return ComicContext(
            stable_context=stable_context,
            relevant_memory=relevant_memory,
            current_task=description or None,
            source_versions=source_versions,
        )
