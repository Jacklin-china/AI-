"""剧本分镜工具：结构化生成、严格校验，并原子写入 SQLite。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from kantoku.agent.tools_registry import register
from kantoku.config import SchemaError, ToolError, get_settings
from kantoku.config.settings import ROOT
from kantoku.core.llm import chat
from kantoku.schemas.storyboard import Storyboard, parse_storyboard

SHOT_COUNT = 20
SCHEMA_PATH = ROOT / "db" / "schema.sql"

_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "script": {
            "type": "string",
            "minLength": 1,
            "description": "需要拆成分镜的完整中文剧本",
        }
    },
    "required": ["script"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = """你是 AI 漫剧分镜师。请把用户剧本转换成严格 JSON，不要输出 Markdown 或解释。
顶层必须只有 episode 和 shots。shots 必须恰好 20 条，shot_no 从 1 连续到 20。
每镜必须包含 desc、dialogue、camera、duration_s、characters；无台词用空字符串，无人物用空数组。
只要角色在画面中出现或发声，就必须用剧本原名列入 characters；动物、AI 等非人角色也算。
不要给角色名增加“AI”“成年”等前缀；年龄或形态写进 desc，以便人物身份保持可追踪。
镜头描述必须可直接指导后续生图，人物名称在相邻镜头保持一致，时长使用正整数秒。
必须满足下面的 JSON Schema：
{schema}
"""


def _database_path() -> Path:
    """把配置的相对数据库路径稳定解析到项目根目录。"""
    configured = get_settings().storage.sqlite_path
    return configured if configured.is_absolute() else ROOT / configured


def _read_schema() -> str:
    """以 UTF-8 读取唯一数据库结构定义。"""
    try:
        return SCHEMA_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ToolError(
            "无法读取分镜数据库结构",
            detail=f"path={SCHEMA_PATH}；error={type(error).__name__}",
        ) from error


def save_storyboard(storyboard: Storyboard) -> None:
    """在单个事务内替换同名剧集，失败时保留旧版本。"""
    database_path = _database_path()
    connection: sqlite3.Connection | None = None
    try:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path)
        connection.executescript(_read_schema())
        with connection:
            connection.execute("DELETE FROM shot WHERE episode = ?", (storyboard.episode,))
            connection.executemany(
                """
                INSERT INTO shot (
                    episode, shot_no, desc, dialogue, camera, duration_s, characters_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        storyboard.episode,
                        shot.shot_no,
                        shot.desc,
                        shot.dialogue,
                        shot.camera,
                        shot.duration_s,
                        json.dumps(shot.characters, ensure_ascii=False),
                    )
                    for shot in storyboard.shots
                ],
            )
    except (OSError, sqlite3.Error) as error:
        raise ToolError(
            "分镜写入数据库失败",
            detail=f"path={database_path}；error={type(error).__name__}",
        ) from error
    finally:
        if connection is not None:
            connection.close()


def load_storyboard(episode: str) -> Storyboard | None:
    """按剧集读取分镜；不存在时返回 None，坏数据转为工具异常。"""
    if not episode.strip():
        raise ToolError("剧集名称不能为空")
    database_path = _database_path()
    connection: sqlite3.Connection | None = None
    try:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        connection.executescript(_read_schema())
        rows = connection.execute(
            """
            SELECT shot_no, desc, dialogue, camera, duration_s, characters_json
            FROM shot WHERE episode = ? ORDER BY shot_no
            """,
            (episode.strip(),),
        ).fetchall()
        if not rows:
            return None
        return Storyboard.model_validate(
            {
                "episode": episode.strip(),
                "shots": [
                    {
                        "shot_no": row["shot_no"],
                        "desc": row["desc"],
                        "dialogue": row["dialogue"],
                        "camera": row["camera"],
                        "duration_s": row["duration_s"],
                        "characters": json.loads(row["characters_json"]),
                    }
                    for row in rows
                ],
            }
        )
    except (OSError, sqlite3.Error, json.JSONDecodeError, ValueError) as error:
        raise ToolError(
            "分镜数据库内容无效",
            detail=f"path={database_path}；error={type(error).__name__}",
        ) from error
    finally:
        if connection is not None:
            connection.close()


@register(
    "generate_storyboard",
    description="把完整剧本拆成恰好 20 个连续、可供后续生图的分镜并保存",
    parameters=_PARAMETERS,
)
def generate_storyboard(script: str) -> Storyboard:
    """生成并保存严格的 20 镜分镜。"""
    if not isinstance(script, str) or not script.strip():
        raise ToolError("剧本不能为空")

    settings = get_settings()
    schema = json.dumps(Storyboard.model_json_schema(), ensure_ascii=False)
    message = chat(
        [
            {"role": "system", "content": _SYSTEM_PROMPT.format(schema=schema)},
            {"role": "user", "content": script.strip()},
        ],
        temperature=settings.llm.structured_temperature,
        response_format={"type": "json_object"},
    )
    if not message.content:
        raise SchemaError("分镜模型未返回 JSON")

    storyboard = parse_storyboard(message.content)
    if len(storyboard.shots) != SHOT_COUNT:
        raise SchemaError(
            "分镜数量不符合要求",
            detail=f"expected={SHOT_COUNT}；actual={len(storyboard.shots)}",
            raw=message.content,
        )
    save_storyboard(storyboard)
    return storyboard
