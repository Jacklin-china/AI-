"""作品级创作上下文；使用现有 SQLite，不复制 Core Runtime。"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import ValidationError

from kantoku.config import ToolError
from kantoku.config.observability import current_trace_id
from kantoku.config.runtime_paths import resolve_runtime_path
from kantoku.core.conversations import InteractionMode
from kantoku.core.database import database_manager
from kantoku.core.runtime.models import ApprovalDecision, ExecutionStatus, RuntimeEventType, utc_now
from kantoku.core.runtime.store import RuntimeStore

from .cinematography import require_execution_choices
from .context_declaration import (
    BIBLE_GROUP_TO_SLOT,
    BIBLE_SOURCE_KEYS,
    EDIT_SCOPE_KEYS,
)
from .director_provenance import project_decision_fields, project_provenance, record_changes
from .models import (
    ComicAsset,
    ComicContext,
    ComicProjectInput,
    ComicProjectSnapshot,
    ComicPromptContexts,
    CreativeBrief,
    CreativeBriefFork,
    CreativeBriefInput,
    CreativeBriefUpdate,
    CreativeContextLock,
    CreativeIntentBoundary,
    CreativeProject,
    DirectorEvidence,
    DirectorSpec,
    DirectorSpecDraft,
    ProjectStatus,
)

_SCHEMA_VERSION = 5


class ComicProjectStore:
    """在 Core 所用数据库中保存 Comic 专属作品与不可变修订。"""

    def __init__(self, path: Path) -> None:
        self.path = resolve_runtime_path(path)
        self.runtime_store: RuntimeStore | None = None
        self.migrate()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = database_manager.connect(self.path, timeout=30)
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
            if 3 not in applied:
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
                    (3, utc_now().isoformat()),
                )
            if 4 not in applied:
                connection.execute(
                    "CREATE TABLE comic_storyboards ("
                    "storyboard_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
                    "current_version INTEGER NOT NULL CHECK(current_version > 0), "
                    "status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                    "FOREIGN KEY(project_id) REFERENCES comic_projects(project_id))"
                )
                connection.execute(
                    "CREATE INDEX idx_comic_storyboards_project ON comic_storyboards(project_id)"
                )
                connection.execute(
                    "CREATE TABLE comic_shots ("
                    "shot_id TEXT PRIMARY KEY, storyboard_id TEXT NOT NULL, "
                    "project_id TEXT NOT NULL, current_version INTEGER NOT NULL "
                    "CHECK(current_version > 0), status TEXT NOT NULL, "
                    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                    "FOREIGN KEY(storyboard_id) REFERENCES comic_storyboards(storyboard_id), "
                    "FOREIGN KEY(project_id) REFERENCES comic_projects(project_id))"
                )
                connection.execute(
                    "CREATE INDEX idx_comic_shots_storyboard ON comic_shots(storyboard_id)"
                )
                connection.execute(
                    "INSERT INTO comic_schema_migrations(version,applied_at) VALUES (?,?)",
                    (4, utc_now().isoformat()),
                )
            if 5 not in applied:
                connection.execute(
                    "CREATE TABLE comic_prompts ("
                    "prompt_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
                    "storyboard_id TEXT NOT NULL, shot_id TEXT NOT NULL UNIQUE, "
                    "current_version INTEGER NOT NULL CHECK(current_version > 0), "
                    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                    "FOREIGN KEY(project_id) REFERENCES comic_projects(project_id), "
                    "FOREIGN KEY(storyboard_id) REFERENCES comic_storyboards(storyboard_id), "
                    "FOREIGN KEY(shot_id) REFERENCES comic_shots(shot_id))"
                )
                connection.execute(
                    "CREATE INDEX idx_comic_prompts_project ON comic_prompts(project_id)"
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

    def _advance_project(
        self, connection: sqlite3.Connection, project_id: str, current_version: int,
    ) -> int:
        """在调用方事务中追加通用作品修订，供资产与镜头共用。"""
        snapshot = self._snapshot(connection, project_id, current_version)
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
        self._insert_version(
            connection, project_id=project_id, entity_type="project", entity_id=project_id,
            version=revised.current_version, payload_json=revised.model_dump_json(),
            created_at=now,
        )
        return revised.current_version

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
        self, project_id: str, data: CreativeBriefUpdate, *,
        fork: CreativeBriefFork | None = None,
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
            content = data.model_dump(include=set(CreativeBriefInput.model_fields))
            previous_content = current.creative_brief.model_dump(
                include=set(CreativeBriefInput.model_fields),
            )
            if content == previous_content and (fork is None or (
                fork.parent_brief_id == current.creative_brief.parent_brief_id
                and fork.parent_brief_version == current.creative_brief.parent_brief_version
                and fork.reason == current.creative_brief.fork_reason
            )):
                return current
            if data.expected_version != current_version:
                raise ToolError(
                    "作品已由其他操作更新，请刷新后重试",
                    detail=f"expected={data.expected_version}; current={current_version}",
                )
            if fork is not None and (
                fork.parent_brief_id != current.creative_brief.brief_id
                or fork.parent_brief_version != current.creative_brief.version
            ):
                raise ToolError("创意分叉的父 Brief 与当前版本不一致")
            now = utc_now()
            lineage = {key: getattr(current.creative_brief, key) for key in (
                "parent_brief_id", "parent_brief_version", "fork_reason",
            )}
            if fork is not None:
                lineage = {"parent_brief_id": fork.parent_brief_id,
                           "parent_brief_version": fork.parent_brief_version,
                           "fork_reason": fork.reason}
                # 归档是追加生命周期事件，绝不 UPDATE 旧 Brief JSON。
                self._insert_version(
                    connection, project_id=project_id, entity_type="creative_brief_lifecycle",
                    entity_id=current.creative_brief.brief_id,
                    version=current.creative_brief.version,
                    payload_json=json.dumps({
                        "status": "archived", "reason": fork.reason,
                        "successor_version": current.creative_brief.version + 1,
                    }),
                    created_at=now,
                )
            brief = CreativeBrief(
                **content, brief_id=current.project.brief_id, project_id=project_id,
                version=current.creative_brief.version + 1, created_at=now, **lineage,
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

    def fork_brief(self, project_id: str, data: CreativeBriefFork) -> ComicProjectSnapshot:
        """复用同一事务/版本表/CAS，不创建另一套 Brief 存储。"""
        return self.replace_brief(project_id, data, fork=data)

    def brief_versions(self, project_id: str) -> list[CreativeBrief]:
        current = self.get(project_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM comic_entity_versions WHERE project_id=? "
                "AND entity_type='creative_brief' ORDER BY version DESC", (project_id,),
            ).fetchall()
            archived = {int(row["version"]) for row in connection.execute(
                "SELECT version FROM comic_entity_versions WHERE project_id=? "
                "AND entity_type='creative_brief_lifecycle'", (project_id,),
            )}
        result = []
        for row in rows:
            brief = CreativeBrief.model_validate_json(row["payload_json"])
            if brief.brief_id != current.creative_brief.brief_id:
                continue
            if brief.version <= max(archived, default=0):
                brief = brief.model_copy(update={"status": "archived"})
            result.append(brief)
        return result

    def get_director(
        self, project_id: str, *, project_version: int | None = None,
        version: int | None = None, spec_id: str | None = None,
    ) -> DirectorSpec:
        if version is not None:
            if not spec_id or project_version is not None:
                raise ToolError("历史导演来源必须提供方案身份和版本")
            self.get(project_id)
            with self._connect() as connection:
                return DirectorSpec.model_validate_json(self._version_payload(
                    connection, project_id, "director_spec", spec_id, version))
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

    def delete_director_version(
        self, project_id: str, *, version: int, expected_project_version: int,
    ) -> dict[str, Any]:
        """删除单个历史导演方案版本。

        与 restore_director 同源；两条硬约束与 Prompt 版本删除一致：
          ① 当前正在使用的版本不可删（comic_projects.director_version 指向它）；
          ② 删除同样追加一次作品修订，保留可追溯链。
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT current_version,director_id,director_version FROM comic_projects "
                "WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if row is None:
                raise ToolError("找不到指定漫剧作品")
            if int(row["current_version"]) != expected_project_version:
                raise ToolError("作品已由其他操作更新，请刷新后重试")
            spec_id = None if row["director_id"] is None else str(row["director_id"])
            current_director_version = (
                None if row["director_version"] is None else int(row["director_version"])
            )
            if spec_id is None or current_director_version is None:
                raise ToolError("当前作品尚无导演方案")
            if current_director_version == version:
                raise ToolError(
                    "当前正在使用的导演方案版本不能删除",
                    detail="请先把其他版本恢复为新版本，再删除这一版",
                )
            existing = connection.execute(
                "SELECT 1 FROM comic_entity_versions WHERE project_id=? "
                "AND entity_type='director_spec' AND entity_id=? AND version=?",
                (project_id, spec_id, version),
            ).fetchone()
            if existing is None:
                raise ToolError("找不到指定导演方案版本")
            connection.execute(
                "DELETE FROM comic_entity_versions WHERE project_id=? "
                "AND entity_type='director_spec' AND entity_id=? AND version=?",
                (project_id, spec_id, version),
            )
            project_version = self._advance_project(
                connection, project_id, expected_project_version,
            )
        return {
            "project_id": project_id, "spec_id": spec_id,
            "deleted_version": version, "project_version": project_version,
        }

    def save_director(
        self, project_id: str, draft: DirectorSpecDraft, *,
        expected_project_version: int,
        source: Literal["model", "manual", "restored"] = "manual",
        restored_from_version: int | None = None,
        preserve_provenance: bool = False,
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
            if source == "manual" and not preserve_provenance:
                previous = DirectorSpec.model_validate_json(self._version_payload(
                    connection, project_id, "director_spec", project.director_id,
                    project.director_version,
                )) if project.director_id else None
                if previous:
                    draft = draft.model_copy(update={"execution_policy": previous.execution_policy})
                if draft.execution_policy == "single_image":
                    draft = project_decision_fields(draft, previous)
                draft = record_changes(
                    previous, draft, source="manual_edit", evidence=DirectorEvidence(
                        source_type="manual_edit", reference=f"project:{project_id}",
                        version=current_version,
                    ),
                )
            else:
                draft = project_provenance(draft)
            if draft.schema_version == 2 and draft.execution_policy == "single_image":
                draft = draft.model_copy(update={
                    "cinematography": require_execution_choices(draft.cinematography),
                })
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

    def creative_context_lock(self, project_id: str, conversation_id: str) -> dict | None:
        """会话锁仅存不可变修订引用；Bible 内容仍由原 Prompt Artifact 保存。"""
        snapshot = self.get(project_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM comic_entity_versions WHERE project_id=? "
                "AND entity_type='creative_context_lock' AND entity_id=? "
                "ORDER BY version DESC LIMIT 1", (project_id, conversation_id),
            ).fetchone()
        lock = CreativeContextLock.model_validate_json(row[0]).model_dump() if row else None
        if lock and (lock["director_version"] == snapshot.project.director_version
                     and lock["brief_version"] == snapshot.creative_brief.version):
            return lock
        return None

    def save_context_lock(self, spec: DirectorSpec, conversation_id: str,
                          sources: dict | None = None) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT current_version FROM comic_projects WHERE project_id=?",
                (spec.project_id,),
            ).fetchone()
            if row is None:
                raise ToolError("找不到指定漫剧作品")
            snapshot = self._snapshot(connection, spec.project_id, int(row[0]))
            if (snapshot.project.director_version != spec.version
                    or snapshot.creative_brief.version != spec.creative_brief_version):
                raise ToolError("导演方案已变化，请刷新后继承创作设定")
            row = connection.execute(
                "SELECT version,payload_json FROM comic_entity_versions WHERE project_id=? "
                "AND entity_type='creative_context_lock' AND entity_id=? "
                "ORDER BY version DESC LIMIT 1", (spec.project_id, conversation_id),
            ).fetchone()
            previous = json.loads(row["payload_json"]) if row else {}
            lock = {"director_version": spec.version,
                    "brief_version": spec.creative_brief_version,
                    "bible_sources": sources if sources is not None else
                    previous.get("bible_sources", {}) if
                    previous.get("brief_version") == spec.creative_brief_version else {}}
            plan = spec.director_plan
            lock.update(
                character=(plan.character_presence or spec.character_focus)
                if plan else spec.character_focus,
                world=spec.creative_decision.narrative_context
                if spec.creative_decision else "",
                visual_style=(plan.style_boundary or "") if plan else spec.visual_direction,
                camera_style=spec.camera_language,
                environment=plan.subject_environment_relation if plan else "",
                fixed_attributes=list(dict.fromkeys([
                    *spec.constraints, *(plan.continuity_rules if plan else []),
                    *([plan.character_presence] if plan and plan.character_presence else []),
                ])),
            )
            # Bible content remains canonical in the original Prompt revision/Artifact.
            # The lock stores its stable projection, never a second Prompt or asset.
            for field, source in lock["bible_sources"].items():
                prompt = json.loads(self._version_payload(
                    connection, source.get("project_id", spec.project_id),
                    "prompt", source["prompt_id"], source["version"]))
                if prompt["artifact_id"] != source["artifact_id"]:
                    raise ToolError("创作设定锁与 Prompt Artifact 来源不一致")
                target = BIBLE_GROUP_TO_SLOT.get(field)
                if target is None:
                    raise ToolError("创作设定锁包含不支持的字段")
                lock[target] = prompt[field]
            lock = CreativeContextLock.model_validate(lock).model_dump()
            if lock != previous:
                self._insert_version(
                    connection, project_id=spec.project_id, entity_type="creative_context_lock",
                    entity_id=conversation_id, version=int(row["version"]) + 1 if row else 1,
                    payload_json=json.dumps(lock, ensure_ascii=False), created_at=utc_now(),
                )
        return lock

    def locked_bibles(self, project_id: str, lock: dict) -> dict[str, str]:
        result = {}
        with self._connect() as connection:
            for field, source in lock.get("bible_sources", {}).items():
                if field not in BIBLE_SOURCE_KEYS:
                    raise ToolError("创作设定锁包含不支持的字段")
                prompt = json.loads(self._version_payload(
                    connection, source.get("project_id", project_id),
                    "prompt", source["prompt_id"], source["version"]))
                if prompt["artifact_id"] != source["artifact_id"]:
                    raise ToolError("创作设定锁与 Prompt Artifact 来源不一致")
                result[field] = prompt[field]
                result[f"source:{field}"] = prompt["artifact_id"]
                result[f"provenance:{field}"] = json.dumps(
                    prompt.get("context_sources", {}).get("field_provenance", {}).get(field, {}),
                    ensure_ascii=False)
        return result

    def director_confirmed(self, spec: DirectorSpec) -> bool:
        """确认只对应一个不可变修订；不继承旧版本的审批。"""
        if spec.schema_version == 1:
            return True  # 旧生产契约保持兼容；v2 需要绑定版本的授权。
        if self.runtime_store is None:
            return False
        return any(
            approval.decision is ApprovalDecision.APPROVE
            and approval.request == self._confirmation_request(spec)
            for approval in self.runtime_store.list_approvals()
        )

    @staticmethod
    def _confirmation_request(spec: DirectorSpec) -> dict:
        return {"kind": "comic.director.confirmation", "project_id": spec.project_id,
                "spec_id": spec.spec_id, "version": spec.version,
                "brief_version": spec.creative_brief_version}

    def human_director_confirmed(self, spec: DirectorSpec) -> bool:
        if self.runtime_store is None:
            return False
        return any(
            approval.decision is ApprovalDecision.APPROVE
            and approval.request == self._confirmation_request(spec)
            and approval.response.get("human_review") is True
            for approval in self.runtime_store.list_approvals()
        )

    def require_confirmed_director(self, spec: DirectorSpec, *, human_review: bool = False) -> None:
        from .critic import require_approved_director

        require_approved_director(spec, allow_advisory=self.advisory_authorized(spec),
                                  allow_unavailable=self.human_director_confirmed(spec))
        confirmed = self.human_director_confirmed(spec) if human_review \
            else self.director_confirmed(spec)
        if not confirmed:
            raise ToolError("请先确认当前导演方案，才能进入下一步")

    def advisory_authorized(self, spec: DirectorSpec) -> bool:
        """Advice requires a durable version-bound human or production authorization."""
        if self.runtime_store is None:
            return False
        return any(
            approval.decision is ApprovalDecision.APPROVE
            and approval.request == self._confirmation_request(spec)
            and approval.response.get("allow_advisory") is True
            and (approval.response.get("human_review") is True or (
                approval.response.get("human_review") is False
                and bool(approval.response.get("production_run_id"))))
            for approval in self.runtime_store.list_approvals()
        )

    def confirm_director(self, project_id: str, *, version: int,
                         expected_project_version: int,
                         automatic_run_id: str | None = None,
                         allow_advisory: bool = False) -> DirectorSpec:
        from .critic import require_approved_director

        # 与保存/分叉共用写锁，不能在确认过程中将旧方案标为当前。
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            snapshot = self._snapshot(connection, project_id, expected_project_version)
            current = connection.execute(
                "SELECT current_version FROM comic_projects WHERE project_id=?", (project_id,),
            ).fetchone()
            if current is None or current[0] != expected_project_version:
                raise ToolError("作品已更新，请刷新后确认")
            spec = self.get_director(project_id)
            if (spec.version != version
                    or spec.creative_brief_version != snapshot.creative_brief.version):
                raise ToolError("只能确认当前 Brief 下的当前导演版本")
            if spec.critic_status == "unavailable":
                decision = spec.creative_decision
                hard = snapshot.creative_brief.hard_constraints
                if (decision is None or decision.hard_constraints != hard
                        or any(item not in spec.constraints for item in hard)
                        or spec.director_plan is None or not spec.director_plan.creative_choices):
                    raise ToolError("导演方案的不可变约束或公开创作理由不完整")
            require_approved_director(spec, allow_advisory=allow_advisory,
                                      allow_unavailable=not bool(automatic_run_id))
            if self.runtime_store is None:
                raise ToolError("导演确认尚未绑定现有 Runtime")
            # 审批库可能与作品库是同一 SQLite，先持有写锁验证，再提交后写审批。
        confirmed = self.director_confirmed(spec) if automatic_run_id \
            else self.human_director_confirmed(spec)
        if not confirmed:
            run = self.runtime_store.create_run(
                "comic", "comic.director.confirmation", {"project_id": project_id,
                  "director_spec_version": version,
                  "trace_id": current_trace_id() or f"trace-{uuid4().hex[:12]}"},
                "director_confirmation",
                interaction_mode=InteractionMode.AUTONOMOUS if automatic_run_id
                else InteractionMode.GUIDED,
            )
            approval = self.runtime_store.create_approval(
                run.id, "director_confirmation", self._confirmation_request(spec),
            )
            self.runtime_store.decide_approval(approval.id, ApprovalDecision.APPROVE, {
                "authorization": ("fast_creation_policy" if self.runtime_store.get_run(
                    automatic_run_id).state.get("execution_mode") == "fast"
                    else "production_policy") if automatic_run_id
                else "user_confirmation", "production_run_id": automatic_run_id,
                "human_review": not bool(automatic_run_id),
                "allow_advisory": allow_advisory,
                "critic_status": spec.critic_status,
                "status": "approved",
            })
            if not automatic_run_id:
                transition = {"from": "director_review", "to": "storyboard_generation",
                              "project_id": project_id, "director_version": version,
                              "approval_status": "approved",
                              "trace_id": run.state["trace_id"]}
                self.runtime_store.append_event(
                    run.id, RuntimeEventType.NODE_PROGRESS, node_id="director_confirmation",
                    payload={"kind": "workflow_transition", **transition},
                )
                run = run.model_copy(update={"state": {
                    **run.state, "approval": {"status": "approved", "id": approval.id},
                    "workflow_transition": transition,
                }})
            self.runtime_store.update_run(run.id, status=ExecutionStatus.COMPLETED,
                                          state=run.state, current_node="director_confirmation")
        return spec


class ComicContextBuilder:
    """只组合调用方确实选中的导演方案与资产；不读聊天全文。"""

    @staticmethod
    def explicit_context_changes(text: str) -> set[str]:
        """只认可明确换设定的命令，动作补充和否定命令不会释放锁。"""
        changes = set()
        for clause in re.split(r"[，。；！!\n]|但是|但要|不过", text):
            if re.search(r"(?:不要|不想|不需要|无需|别|不)(?:再)?\s*"
                         r"(?:换|更换|替换|改变|修改|重新设计)", clause):
                continue
            if re.search(r"(?:换|更换|另换)(?:一个|个)?(?:主题|题材)|"
                         r"(?:新|另一个)(?:主题|作品)|开始新创作", clause):
                changes.update(BIBLE_SOURCE_KEYS)
            for field, nouns in (
                ("character_context", "角色|人物|主角"),
                ("world_context", "世界|世界观|场景|地点|时间"),
                ("style_context", "风格|画风|艺术风格|摄影风格"),
            ):
                if re.search(rf"(?:换|更换|替换|改变|修改|重新设计)\s*(?:一个|一下)?(?:{nouns})"
                             r"(?!的?(?:姿态|动作|表情|视线|朝向|镜头|构图|光照))|"
                             rf"(?:{nouns})\s*(?:换成|改成|改为)", clause):
                    changes.add(field)
        return changes

    @staticmethod
    def check_intent_boundary(
        current_user_request: str, brief: CreativeBrief, *,
        model_call: Callable[[list[dict[str, str]]], str],
    ) -> CreativeIntentBoundary:
        """用现有文本能力判断语义边界；无关键词模板，无默认继承失败兜底。"""
        request = current_user_request.strip()
        if not request or len(request) > 1000:
            raise ToolError("创意请求不能为空或超过长度限制")
        if request == brief.original_request:
            return CreativeIntentBoundary(new_creative_direction=False, reason="same request")
        raw = model_call([
            {"role": "system", "content": (
                "判断当前用户输入是在修改现有创意，还是创建独立的新创作方向。"
                "综合主题、角色、世界观、叙事目标和视觉方向，不使用关键词固定映射。"
                "调整颜色/构图、补充细节或语义一致的重述沿用现有 Brief；"
                "角色、题材或世界观根本改变且不是对当前作品的明确修改时创建新 Brief。"
                "历史失败不能成为继承理由。新 Brief 只根据当前用户输入，"
                "不得复制旧角色、旧场景、旧风格、旧偏好或约束。"
                "新 Brief original_request 保留当前用户原文；hard_constraints 仅提取"
                "用户明示的逐字短片段，不添加推断要求。只返回符合 Schema 的 JSON，"
                "reason 是简短公开边界说明，不输出思维链、Prompt 或导演方案："
                + json.dumps(CreativeIntentBoundary.model_json_schema(), ensure_ascii=False)
            )},
            {"role": "user", "content": json.dumps({
                "current_user_request": request,
                "current_brief": brief.model_dump(include=set(CreativeBriefInput.model_fields)),
            }, ensure_ascii=False)},
        ])
        try:
            boundary = CreativeIntentBoundary.model_validate_json(raw)
        except (ValueError, ValidationError):
            raise ToolError("创意边界判断未返回有效公开结构，未继承旧 Context") from None
        if boundary.new_brief is not None:
            if any(value.casefold() not in request.casefold()
                   for value in boundary.new_brief.hard_constraints):
                raise ToolError("新 Brief 硬约束不是当前用户原文，未继承旧 Context")
            boundary.new_brief.original_request = request
        return boundary

    @staticmethod
    def build(
        snapshot: ComicProjectSnapshot, *, task: str | None = None,
        director: DirectorSpec | None = None,
        assets: list[ComicAsset] | None = None,
        creative_lock: dict | None = None,
    ) -> ComicContext:
        description = task.strip() if task is not None else None
        if description is not None and len(description) > 1000:
            raise ToolError("当前任务描述过长")
        project = snapshot.project
        brief = snapshot.creative_brief
        project_context = {"project_id": project.project_id}
        # 所有入口均排除导航标题/描述；旧标题不能成为当前故事事实。
        stable_context = {
            "project": project_context,
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
                "schema_version", "visual_direction", "storytelling_goal", "camera_language",
                "composition", "lighting", "color_language", "emotion", "character_focus",
                "constraints", "creative_choices", "creative_decision", "director_plan",
                "cinematography", "critic_result", "knowledge_refs",
            }, exclude_none=True)
            source_versions["director_spec"] = director.version
        if creative_lock:
            stable_context["creative_context_lock"] = creative_lock
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

    @staticmethod
    def starts_new_context(text: str) -> bool:
        """Only explicit reset commands end the current creative context."""
        for clause in re.split(r"[，。；！!\n]|但是|不过", text):
            if re.search(r"(?:不要|不想|无需|别|不)(?:再)?\s*(?:新建|重新设计|换|开始)", clause):
                continue
            if re.search(r"新建(?:对话|项目)|新项目|重新设计|开始新创作|"
                         r"(?:换|更换|另换)(?:一个|个)?(?:完全不同的?|全新的?)?(?:主题|题材)|"
                         r"(?:新|另一个)(?:主题|作品)", clause):
                return True
        return False

    @staticmethod
    def action_semantic_cluster(text: str) -> set[str]:
        """识别动作/状态指令涉及的同语义字段组。

        用户要求改变角色状态（如“被黑暗力量吞噬并怒吼黑化”）时，只更新
        character_pose 会让情绪目标与表情仍停留在旧状态，方案内部不自洽，
        审核必然判为冲突。这里返回需要一起交还给模型的字段路径，
        具体取值仍由导演决定，服务器不代填创作内容。
        """
        if not re.search(r"黑化|吞噬|失控|暴走|怒吼|咆哮|爆发|变异|崩溃|疯狂|"
                         r"变化|改变状态|状态改变|变回|恢复", text):
            return set()
        return {
            "creative_decision.emotional_target",
            "director_plan.character_expression",
            "director_plan.character_pose",
        }

    @staticmethod
    def resolve_edit_scopes(text: str, director: DirectorSpec, *,
                            model_call: Callable[[list[dict[str, str]]], str]) -> set[str]:
        """Resolve restated requests into writable fields, never a new context."""
        allowed = set(EDIT_SCOPE_KEYS)
        raw = model_call([
            {"role": "system", "content": (
                "比较用户本轮输入与当前导演方案，只识别用户明确要求修改的字段。"
                "根据当前显式选中的方案判断修改与保留范围，不通过固定关键词判断。"
                "重述相同角色不算修改角色；用户否定或撤回的要求不能继续保留。"
                "若本轮输入引入当前方案中还没有的新角色或新对手，"
                "必须把 character_context 列入 edit_scopes；"
                "引入新的地点、时间或天气时列入 world_context。"
                "只有输入纯粹改变已有主体的动作或姿态，才允许只返回 action。"
                "不判断新主题，不生成创作内容。返回 JSON 对象，唯一字段 edit_scopes，"
                "值为字符串数组，只能包含：" + ",".join(sorted(allowed)))},
            {"role": "user", "content": json.dumps({
                "instruction": text, "current_director": director.model_dump(
                    mode="json", include={"creative_decision", "director_plan", "cinematography"}),
            }, ensure_ascii=False)},
        ])
        try:
            result = json.loads(raw)["edit_scopes"]
            if not isinstance(result, list) or any(not isinstance(item, str) or item not in allowed
                                                   for item in result):
                raise ValueError("invalid edit scopes")
        except (ValueError, KeyError, TypeError) as error:
            raise ToolError("修改范围分析未返回有效字段，请重试；原方案已保留") from error
        return set(result)

    @staticmethod
    def edit_scopes(text: str) -> set[str]:
        """Declare writable anchors; this never chooses creative values for the model."""
        scopes = ComicContextBuilder.explicit_context_changes(text)
        text = "，".join(clause for clause in re.split(r"[，。；！!\n]|但是|不过", text)
                        if not re.search(r"(?:不要|不想|不需要|无需|别|不)(?:再)?\s*"
                                         r"(?:换|更换|修改|改变|调整|改)", clause))
        if re.search(r"天空|夜景|夜晚|白天|黄昏|黎明|天气|环境|背景", text):
            scopes.add("world_context")
        if re.search(r"(?:改变|修改|换|调整|改成|改为|使用|采用).*(?:镜头|机位|景别|视角|焦距)|"
                     r"(?:镜头|机位|景别|视角|焦距).*(?:改|换|调整)", text):
            scopes.add("camera_style")
        if re.search(r"颜色|色彩|色调|血红|红色|蓝色|绿色", text):
            scopes.add("color")
        if re.search(r"光线|光照|灯光|夜景|夜晚|白天|黄昏|黎明", text):
            scopes.add("lighting")
        return scopes

    @staticmethod
    def retained_prompt_fields(bibles: dict[str, str], instruction: str,
                               scopes: set[str]) -> dict[str, str]:
        """Preserve unedited leaves when an edit releases only part of a Bible."""
        unlocked = {}
        explicit = ComicContextBuilder.explicit_context_changes(instruction)
        if "world_context" in scopes and "world_context" not in explicit:
            unlocked["world_context"] = {"environment"}
            if re.search(r"夜景|夜晚|白天|黄昏|黎明|时间", instruction):
                unlocked["world_context"].add("time")
        if "color" in scopes and "style_context" not in explicit:
            unlocked["style_context"] = {"color"}
        retained = {}
        for section, fields in unlocked.items():
            if section not in bibles:
                continue
            context = ComicPromptContexts.model_fields[section].annotation.from_text(
                bibles[section])
            for field, value in context.model_dump().items():
                if field not in fields:
                    retained[f"lock:{section}.{field}"] = value
                    retained[f"source:{section}.{field}"] = bibles.get(f"source:{section}", "")
        return retained

    @staticmethod
    def context_diff(before: DirectorSpecDraft, after: DirectorSpecDraft,
                     instruction: str | None) -> dict[str, Any]:
        changes = []
        for section in ("creative_decision", "director_plan", "cinematography"):
            previous = getattr(before, section)
            current = getattr(after, section)
            if previous is None or current is None:
                continue
            for field, value in current.model_dump(mode="json").items():
                old = getattr(previous, field)
                if old != value:
                    changes.append({"field_path": f"{section}.{field}",
                                    "before": old, "after": value})
        return {"instruction": instruction, "changes": changes,
                "affected_fields": [item["field_path"] for item in changes],
                "impact_scope": sorted({item["field_path"].split(".")[0] for item in changes}),
                "potential_conflicts": [], "recommended_sync_changes": []}
