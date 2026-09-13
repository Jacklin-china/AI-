"""角色卡契约与 SQLite 持久化，为跨镜头一致性提供稳定锚点。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from kantoku.config import SchemaError, ToolError, get_settings
from kantoku.config.settings import ROOT

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SCHEMA_PATH = ROOT / "db" / "schema.sql"


class Persona(BaseModel):
    """一名角色不可随镜头变化的视觉锚点。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: NonBlank = Field(description="角色在分镜 characters 中使用的唯一原名")
    appearance: NonBlank = Field(description="年龄、脸型、发型等稳定外貌特征")
    outfit: NonBlank = Field(description="本集需要保持一致的服装与配饰")
    style_tokens: list[NonBlank] = Field(
        min_length=1,
        description="跨镜头复用的画风关键词，按稳定顺序保存",
    )


def parse_persona(raw: str) -> Persona:
    """解析严格角色卡；原始正文仅保留在异常属性中。"""
    try:
        return Persona.model_validate_json(raw)
    except ValidationError as error:
        detail = "; ".join(
            f"{'.'.join(map(str, item['loc'])) or 'root'}: {item['type']}"
            for item in error.errors(include_input=False, include_context=False, include_url=False)
        )
        raise SchemaError("角色卡 JSON 校验失败", detail=detail, raw=raw) from None


def _database_path() -> Path:
    """把配置中的相对 SQLite 路径固定到项目根目录。"""
    configured = get_settings().storage.sqlite_path
    return configured if configured.is_absolute() else ROOT / configured


def _read_schema() -> str:
    """读取数据库结构；不在 Python 中维护第二份建表语句。"""
    try:
        return SCHEMA_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ToolError(
            "无法读取角色卡数据库结构",
            detail=f"path={SCHEMA_PATH}；error={type(error).__name__}",
        ) from error


def _connect() -> sqlite3.Connection:
    """建立已初始化的数据库连接，并统一包装打开失败。"""
    path = _database_path()
    connection: sqlite3.Connection | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.executescript(_read_schema())
        return connection
    except (OSError, sqlite3.Error):
        if connection is not None:
            connection.close()
        raise ToolError(
            "无法打开角色卡数据库",
            detail=f"path={path}",
        ) from None


def _persona_from_row(row: sqlite3.Row) -> Persona:
    """把数据库行恢复成严格角色卡，拒绝损坏的历史数据。"""
    try:
        return Persona.model_validate(
            {
                "name": row["name"],
                "appearance": row["appearance"],
                "outfit": row["outfit"],
                "style_tokens": json.loads(row["style_tokens_json"]),
            }
        )
    except (json.JSONDecodeError, ValidationError, TypeError) as error:
        raise ToolError("角色卡数据库内容无效", detail=type(error).__name__) from error


def save_persona(persona: Persona) -> None:
    """按角色原名幂等保存；重复录入时整体更新视觉锚点。"""
    connection = _connect()
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO persona (name, appearance, outfit, style_tokens_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    appearance = excluded.appearance,
                    outfit = excluded.outfit,
                    style_tokens_json = excluded.style_tokens_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    persona.name,
                    persona.appearance,
                    persona.outfit,
                    json.dumps(persona.style_tokens, ensure_ascii=False),
                ),
            )
    except sqlite3.Error as error:
        raise ToolError("角色卡写入数据库失败", detail=type(error).__name__) from error
    finally:
        connection.close()


def load_persona(name: str) -> Persona | None:
    """按角色原名读取；未录入时返回 None。"""
    if not isinstance(name, str) or not name.strip():
        raise ToolError("角色名称不能为空")
    connection = _connect()
    try:
        row = connection.execute(
            """
            SELECT name, appearance, outfit, style_tokens_json
            FROM persona WHERE name = ?
            """,
            (name.strip(),),
        ).fetchone()
        return None if row is None else _persona_from_row(row)
    except sqlite3.Error as error:
        raise ToolError("角色卡读取失败", detail=type(error).__name__) from error
    finally:
        connection.close()


def list_personas(names: Sequence[str] | None = None) -> list[Persona]:
    """读取全部或指定角色卡；指定模式保持输入顺序并去重。"""
    requested: list[str] | None = None
    if names is not None:
        if any(not isinstance(name, str) or not name.strip() for name in names):
            raise ToolError("角色名称不能为空")
        requested = list(dict.fromkeys(name.strip() for name in names))
        if not requested:
            return []

    connection = _connect()
    try:
        if requested is None:
            rows = connection.execute(
                """
                SELECT name, appearance, outfit, style_tokens_json
                FROM persona ORDER BY name
                """
            ).fetchall()
        else:
            placeholders = ",".join("?" for _ in requested)
            rows = connection.execute(
                f"""
                SELECT name, appearance, outfit, style_tokens_json
                FROM persona WHERE name IN ({placeholders})
                """,
                requested,
            ).fetchall()
        personas = [_persona_from_row(row) for row in rows]
        if requested is None:
            return personas
        by_name = {persona.name: persona for persona in personas}
        return [by_name[name] for name in requested if name in by_name]
    except sqlite3.Error as error:
        raise ToolError("角色卡读取失败", detail=type(error).__name__) from error
    finally:
        connection.close()
