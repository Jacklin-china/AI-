"""把已人工终审的图片和标签原子归档，原始生成文件保持不动。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from kantoku.config import ToolError, get_settings
from kantoku.config.settings import ROOT
from kantoku.core.budget import get_reservation, load_generation_result
from kantoku.perception.review import load_human_review
from kantoku.tools.storyboard import load_storyboard

_SUPPORTED_SUFFIXES = {".jpeg", ".jpg", ".png", ".webp"}
SCHEMA_PATH = ROOT / "db" / "schema.sql"


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    """一次可重复归档的输出位置。"""

    image_path: Path
    metadata_path: Path
    approved: bool
    source_request_id: str
    project: str
    episode: str
    shot_no: int
    characters: tuple[str, ...]
    image_sha256: str


def _database_path() -> Path:
    configured = get_settings().storage.sqlite_path
    return configured if configured.is_absolute() else ROOT / configured


def _archive_root() -> Path:
    return _database_path().parent / "archive"


def _safe_component(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", value.strip())
    normalized = normalized.strip(" ._")[:48] or "unnamed"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{normalized}-{digest}"


def _characters_for(episode: str, shot_no: int) -> tuple[str, ...]:
    storyboard = load_storyboard(episode)
    if storyboard is None:
        return ()
    for shot in storyboard.shots:
        if shot.shot_no == shot_no:
            return tuple(shot.characters)
    return ()


def _write_once(path: Path, data: bytes) -> None:
    """同内容重复执行直接复用；不同内容绝不覆盖。"""
    if path.exists():
        try:
            if path.read_bytes() == data:
                return
        except OSError as error:
            raise ToolError("无法核对已有归档文件", detail=type(error).__name__) from error
        raise ToolError("归档目标已存在且内容不同，禁止覆盖")
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(data)
        temporary.replace(path)
    except OSError as error:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise ToolError("归档文件写入失败", detail=type(error).__name__) from error


def _row_to_archive(row: sqlite3.Row) -> ArchiveResult:
    try:
        characters = json.loads(row["characters_json"])
    except (json.JSONDecodeError, TypeError) as error:
        raise ToolError("归档索引内容无效", detail=type(error).__name__) from error
    if not isinstance(characters, list) or any(not isinstance(item, str) for item in characters):
        raise ToolError("归档索引角色列表无效")
    return ArchiveResult(
        image_path=Path(row["image_path"]),
        metadata_path=Path(row["metadata_path"]),
        approved=bool(row["approved"]),
        source_request_id=row["source_request_id"],
        project=row["project"],
        episode=row["episode"],
        shot_no=row["shot_no"],
        characters=tuple(characters),
        image_sha256=row["image_sha256"],
    )


def _index_archive(result: ArchiveResult) -> None:
    database_path = _database_path()
    connection: sqlite3.Connection | None = None
    try:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        with connection:
            existing = connection.execute(
                "SELECT * FROM archive_asset WHERE source_request_id = ?",
                (result.source_request_id,),
            ).fetchone()
            if existing is not None:
                if _row_to_archive(existing) == result:
                    return
                raise ToolError("归档索引已存在且内容不同，禁止覆盖")
            connection.execute(
                """
                INSERT INTO archive_asset (
                    source_request_id, project, episode, shot_no, characters_json,
                    image_path, metadata_path, image_sha256, approved
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.source_request_id,
                    result.project,
                    result.episode,
                    result.shot_no,
                    json.dumps(result.characters, ensure_ascii=False),
                    str(result.image_path),
                    str(result.metadata_path),
                    result.image_sha256,
                    int(result.approved),
                ),
            )
    except ToolError:
        raise
    except (OSError, UnicodeError, sqlite3.Error) as error:
        raise ToolError("归档索引写入失败", detail=type(error).__name__) from error
    finally:
        if connection is not None:
            connection.close()


def search_archived_images(
    *,
    project: str | None = None,
    episode: str | None = None,
    shot_no: int | None = None,
    character: str | None = None,
    approved: bool | None = None,
) -> list[ArchiveResult]:
    """按项目、集、镜号、角色和终审结果查询已归档素材。"""
    text_filters = {"project": project, "episode": episode, "character": character}
    if any(value is not None and not value.strip() for value in text_filters.values()):
        raise ToolError("归档查询条件不能为空字符串")
    if shot_no is not None and (type(shot_no) is not int or shot_no <= 0):
        raise ToolError("归档查询镜号必须是正整数")
    if approved is not None and type(approved) is not bool:
        raise ToolError("归档查询终审结果必须是布尔值")

    clauses: list[str] = []
    parameters: list[object] = []
    for column, value in (("project", project), ("episode", episode)):
        if value is not None:
            clauses.append(f"{column} = ?")
            parameters.append(value.strip())
    if shot_no is not None:
        clauses.append("shot_no = ?")
        parameters.append(shot_no)
    if approved is not None:
        clauses.append("approved = ?")
        parameters.append(int(approved))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""

    connection: sqlite3.Connection | None = None
    try:
        database_path = _database_path()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        rows = connection.execute(
            "SELECT * FROM archive_asset"
            + where
            + " ORDER BY project, episode, shot_no, created_at, source_request_id",
            parameters,
        ).fetchall()
        results = [_row_to_archive(row) for row in rows]
        if character is not None:
            expected = character.strip()
            results = [item for item in results if expected in item.characters]
        return results
    except ToolError:
        raise
    except (OSError, UnicodeError, sqlite3.Error) as error:
        raise ToolError("归档索引读取失败", detail=type(error).__name__) from error
    finally:
        if connection is not None:
            connection.close()


def archive_reviewed_image(source_request_id: str) -> ArchiveResult:
    """归档成功图片及不可变人工标签；没有终审时拒绝归档。"""
    if not isinstance(source_request_id, str) or not source_request_id.strip():
        raise ToolError("原生图请求 ID 不能为空")
    request_id = source_request_id.strip()
    generation = load_generation_result(request_id)
    if generation is None or generation.status != "succeeded" or generation.path is None:
        raise ToolError("只有成功生成的图片才能归档")
    label = load_human_review(request_id)
    if label is None:
        raise ToolError("图片尚未完成人工终审，不能归档")
    reservation = get_reservation(request_id)
    if reservation is None:
        raise ToolError("找不到生图请求对应的预算台账")
    source = generation.path
    if source.resolve() != label.image_path.resolve():
        raise ToolError("人工终审图片与原生图请求不一致")
    suffix = source.suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        raise ToolError("归档图片格式不支持")
    try:
        image_bytes = source.read_bytes()
    except OSError as error:
        raise ToolError("无法读取待归档图片", detail=type(error).__name__) from error
    if not image_bytes:
        raise ToolError("待归档图片为空")

    image_sha256 = hashlib.sha256(image_bytes).hexdigest()
    stable_name = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:20]
    decision = "approved" if label.approved else "rejected"
    characters = _characters_for(reservation.episode, reservation.shot_no)
    directory = (
        _archive_root()
        / _safe_component(reservation.project)
        / _safe_component(reservation.episode)
        / decision
    )
    file_stem = f"shot-{reservation.shot_no:03d}-{stable_name}"
    image_path = directory / f"{file_stem}{suffix}"
    metadata_path = directory / f"{file_stem}.json"
    metadata = {
        "source_request_id": request_id,
        "provider_job_id": generation.provider_job_id,
        "project": reservation.project,
        "episode": reservation.episode,
        "shot_no": reservation.shot_no,
        "characters": characters,
        "image_sha256": image_sha256,
        "label": label.model_dump(mode="json"),
    }
    metadata_bytes = (
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _write_once(image_path, image_bytes)
    _write_once(metadata_path, metadata_bytes)
    result = ArchiveResult(
        image_path=image_path,
        metadata_path=metadata_path,
        approved=label.approved,
        source_request_id=request_id,
        project=reservation.project,
        episode=reservation.episode,
        shot_no=reservation.shot_no,
        characters=characters,
        image_sha256=image_sha256,
    )
    _index_archive(result)
    return result
