"""作品级创作上下文；使用现有 SQLite，不复制 Core Runtime。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from kantoku.config import ToolError
from kantoku.core.runtime.models import utc_now

from .models import (
    ComicContext,
    ComicProjectInput,
    ComicProjectSnapshot,
    CreativeBrief,
    CreativeBriefInput,
    CreativeBriefUpdate,
    CreativeProject,
    ProjectStatus,
)

_SCHEMA_VERSION = 1


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
            applied = connection.execute(
                "SELECT 1 FROM comic_schema_migrations WHERE version=?", (_SCHEMA_VERSION,)
            ).fetchone()
            if applied is not None:
                return
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
            })
            connection.execute(
                "UPDATE comic_projects SET current_version=?,brief_version=?,updated_at=? "
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


class ComicContextBuilder:
    """从指定作品修订选择稳定上下文；不读取聊天全文或旧 Prompt。"""

    @staticmethod
    def build(snapshot: ComicProjectSnapshot, *, task: str | None = None) -> ComicContext:
        description = task.strip() if task is not None else None
        if description is not None and len(description) > 1000:
            raise ToolError("当前任务描述过长")
        project = snapshot.project
        brief = snapshot.creative_brief
        return ComicContext(
            stable_context={
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
            },
            relevant_memory=[],  # Phase 4/5 才有可验证的资产与镜头引用。
            current_task=description or None,
            source_versions={
                "project": project.current_version,
                "creative_brief": brief.version,
            },
        )
