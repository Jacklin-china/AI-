"""把镜头与稳定人设拼成生图 Prompt，并持久化可追溯配方。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from kantoku.agent.context import select_recent_history
from kantoku.config import ToolError, get_settings
from kantoku.config.settings import ROOT
from kantoku.memory import Persona
from kantoku.schemas.qc import ReworkPlan
from kantoku.schemas.storyboard import Shot

PROMPT_VERSION = "prompt-v1"
SCHEMA_PATH = ROOT / "db" / "schema.sql"
NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ImageSize = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[1-9]\d*x[1-9]\d*$"),
]

_QUALITY_CONSTRAINTS = (
    "自然人体结构与肢体数量，自然皮肤、头发和布料材质，避免塑料感与模板化AI感；"
    "无文字、无字幕、无水印；严格保持角色外貌和服装，不擅自增删角色。"
)


class PromptRecipe(BaseModel):
    """一次镜头 Prompt 的可追溯配方；seed 为空表示不能声称确定性复现。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    episode: NonBlank
    shot_no: int = Field(gt=0)
    prompt_version: NonBlank
    prompt: NonBlank
    scene_constraint: NonBlank | None = None
    model: NonBlank
    size: ImageSize
    reference_asset_ids: list[NonBlank] = Field(default_factory=list)
    seed: int | None = Field(default=None, ge=0)
    source_request_id: NonBlank | None = None
    target_request_id: NonBlank | None = None

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        if len(self.reference_asset_ids) != len(set(self.reference_asset_ids)):
            raise ValueError("reference_asset_ids 不允许重复")
        if (self.source_request_id is None) != (self.target_request_id is None):
            raise ValueError("返工配方必须同时记录原请求和新请求 ID")
        if self.source_request_id is not None and self.source_request_id == self.target_request_id:
            raise ValueError("返工配方的新请求 ID 必须不同于原请求")
        return self

    @property
    def reproducible(self) -> bool:
        """只有供应商接受并记录 seed 时才声明可重复。"""
        return self.seed is not None


def _normalize_personas(persona: Persona | Sequence[Persona]) -> list[Persona]:
    """兼容单人和多人镜头，并保持调用方提供的角色顺序。"""
    personas = [persona] if isinstance(persona, Persona) else list(persona)
    if any(not isinstance(item, Persona) for item in personas):
        raise ToolError("Prompt 人设必须全部是 Persona")
    names = [item.name for item in personas]
    if len(names) != len(set(names)):
        raise ToolError("Prompt 人设不允许重复")
    return personas


def build_prompt(
    shot: Shot,
    persona: Persona | Sequence[Persona],
    *,
    history: Sequence[Shot] = (),
    history_window: int = 3,
    scene_constraint: str | None = None,
) -> str:
    """构建确定顺序的生图 Prompt，并只携带最近的连续性历史。"""
    personas = _normalize_personas(persona)
    expected_names = list(dict.fromkeys(shot.characters))
    actual_names = [item.name for item in personas]
    if actual_names != expected_names:
        raise ToolError(
            "Prompt 人设与镜头人物不一致",
            detail=f"expected={expected_names}；actual={actual_names}",
        )

    style_tokens = list(
        dict.fromkeys(token for item in personas for token in item.style_tokens)
    )
    sections = [
        f"【镜头】{shot.desc}",
        f"【摄影】{shot.camera}",
    ]
    if scene_constraint is not None:
        if not isinstance(scene_constraint, str) or not scene_constraint.strip():
            raise ToolError("场景约束不能为空")
        sections.append(f"【场景锁定】{scene_constraint.strip()}")
    if style_tokens:
        sections.append(f"【统一画风】{', '.join(style_tokens)}")
    if personas:
        character_lines = [
            f"- {item.name}：外貌={item.appearance}；服装={item.outfit}" for item in personas
        ]
        sections.append("【角色锁定】\n" + "\n".join(character_lines))
    else:
        sections.append("【角色锁定】本镜为空镜，不添加人物")
    recent = select_recent_history(shot, history, history_window)
    if recent:
        continuity_lines = [
            f"- 镜{item.shot_no}：{item.desc}；摄影={item.camera}；"
            f"人物={','.join(item.characters) or '无'}"
            for item in recent
        ]
        sections.append("【最近连续性】\n" + "\n".join(continuity_lines))
    sections.append(f"【质量约束】{_QUALITY_CONSTRAINTS}")
    return "\n".join(sections)


def create_recipe(
    *,
    episode: str,
    shot: Shot,
    persona: Persona | Sequence[Persona],
    model: str,
    size: str,
    reference_asset_ids: Sequence[str] = (),
    seed: int | None = None,
    history: Sequence[Shot] = (),
    history_window: int = 3,
    scene_constraint: str | None = None,
) -> PromptRecipe:
    """创建当前版本配方；model 和 size 必须由调用方的配置层传入。"""
    return PromptRecipe(
        episode=episode,
        shot_no=shot.shot_no,
        prompt_version=PROMPT_VERSION,
        prompt=build_prompt(
            shot,
            persona,
            history=history,
            history_window=history_window,
            scene_constraint=scene_constraint,
        ),
        scene_constraint=scene_constraint,
        model=model,
        size=size,
        reference_asset_ids=list(reference_asset_ids),
        seed=seed,
    )


def create_rework_recipe(
    base_recipe: PromptRecipe,
    plan: ReworkPlan,
    *,
    target_request_id: str,
) -> PromptRecipe:
    """基于人工证据创建只改失败项的版本化配方，不调用模型或供应商。"""
    if not isinstance(target_request_id, str) or not target_request_id.strip():
        raise ToolError("返工的新请求 ID 不能为空")
    target_id = target_request_id.strip()
    if target_id == plan.source_request_id:
        raise ToolError("返工的新请求 ID 必须不同于原请求")
    provenance = {
        "base": base_recipe.model_dump(mode="json"),
        "plan": plan.model_dump(mode="json"),
        "target_request_id": target_id,
    }
    digest = hashlib.sha256(
        json.dumps(
            provenance,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:12]
    prompt = "\n".join(
        [
            base_recipe.prompt,
            f"【定向返工来源】原请求={plan.source_request_id}；新请求={target_id}",
            "【必须保留】\n- " + "\n- ".join(plan.preserve_constraints),
            "【只修正以下问题】\n- " + "\n- ".join(plan.correction_directives),
            f"【人工质检证据】{plan.evidence}",
        ]
    )
    return base_recipe.model_copy(
        update={
            "prompt_version": f"{base_recipe.prompt_version}-rework-{digest}",
            "prompt": prompt,
            "source_request_id": plan.source_request_id,
            "target_request_id": target_id,
        }
    )


def _database_path() -> Path:
    """把配置中的相对 SQLite 路径固定到项目根目录。"""
    configured = get_settings().storage.sqlite_path
    return configured if configured.is_absolute() else ROOT / configured


def _connect() -> sqlite3.Connection:
    """建立已初始化的配方数据库连接。"""
    path = _database_path()
    connection: sqlite3.Connection | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        _migrate_recipe_schema(connection)
        return connection
    except (OSError, UnicodeError, sqlite3.Error):
        if connection is not None:
            connection.close()
        raise ToolError("无法打开 Prompt 配方数据库", detail=f"path={path}") from None


def _migrate_recipe_schema(connection: sqlite3.Connection) -> None:
    """为早期本地数据库补充新增列，避免用户必须删除已有数据。"""
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(recipe)").fetchall()
    }
    if "scene_constraint" not in columns:
        connection.execute("ALTER TABLE recipe ADD COLUMN scene_constraint TEXT")
    if "source_request_id" not in columns:
        connection.execute("ALTER TABLE recipe ADD COLUMN source_request_id TEXT")
    if "target_request_id" not in columns:
        connection.execute("ALTER TABLE recipe ADD COLUMN target_request_id TEXT")


def _prompt_sha256(prompt: str) -> str:
    """生成稳定指纹，用于发现同版本内容漂移和数据库损坏。"""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _recipe_from_row(row: sqlite3.Row) -> PromptRecipe:
    """恢复配方并校验保存时的 Prompt 指纹。"""
    try:
        recipe = PromptRecipe.model_validate(
            {
                "episode": row["episode"],
                "shot_no": row["shot_no"],
                "prompt_version": row["prompt_version"],
                "prompt": row["prompt"],
                "scene_constraint": row["scene_constraint"],
                "model": row["model"],
                "size": row["size"],
                "reference_asset_ids": json.loads(row["reference_asset_ids_json"]),
                "seed": row["seed"],
                "source_request_id": row["source_request_id"],
                "target_request_id": row["target_request_id"],
            }
        )
    except (json.JSONDecodeError, ValidationError, TypeError) as error:
        raise ToolError("Prompt 配方数据库内容无效", detail=type(error).__name__) from error
    if _prompt_sha256(recipe.prompt) != row["prompt_sha256"]:
        raise ToolError("Prompt 配方指纹不匹配")
    return recipe


def save_recipe(recipe: PromptRecipe) -> None:
    """幂等保存配方；同版本内容改变时拒绝覆盖历史证据。"""
    connection = _connect()
    try:
        with connection:
            existing_row = connection.execute(
                """
                SELECT episode, shot_no, prompt_version, prompt, prompt_sha256, scene_constraint,
                       model, size, reference_asset_ids_json, seed,
                       source_request_id, target_request_id
                FROM recipe
                WHERE episode = ? AND shot_no = ? AND prompt_version = ?
                """,
                (recipe.episode, recipe.shot_no, recipe.prompt_version),
            ).fetchone()
            if existing_row is not None:
                if _recipe_from_row(existing_row) != recipe:
                    raise ToolError("同一 Prompt 版本的配方发生变化，请升级 PROMPT_VERSION")
                return
            connection.execute(
                """
                INSERT INTO recipe (
                    episode, shot_no, prompt_version, prompt, prompt_sha256, scene_constraint,
                    model, size, reference_asset_ids_json, seed
                    , source_request_id, target_request_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recipe.episode,
                    recipe.shot_no,
                    recipe.prompt_version,
                    recipe.prompt,
                    _prompt_sha256(recipe.prompt),
                    recipe.scene_constraint,
                    recipe.model,
                    recipe.size,
                    json.dumps(recipe.reference_asset_ids, ensure_ascii=False),
                    recipe.seed,
                    recipe.source_request_id,
                    recipe.target_request_id,
                ),
            )
    except ToolError:
        raise
    except sqlite3.Error as error:
        raise ToolError("Prompt 配方写入数据库失败", detail=type(error).__name__) from error
    finally:
        connection.close()


def load_recipe(episode: str, shot_no: int, prompt_version: str) -> PromptRecipe | None:
    """按剧集、镜号和版本读取一份配方。"""
    if not isinstance(episode, str) or not episode.strip():
        raise ToolError("剧集名称不能为空")
    if type(shot_no) is not int or shot_no <= 0:
        raise ToolError("镜号必须是正整数")
    if not isinstance(prompt_version, str) or not prompt_version.strip():
        raise ToolError("Prompt 版本不能为空")

    connection = _connect()
    try:
        row = connection.execute(
            """
            SELECT episode, shot_no, prompt_version, prompt, prompt_sha256, scene_constraint,
                   model, size, reference_asset_ids_json, seed,
                   source_request_id, target_request_id
            FROM recipe
            WHERE episode = ? AND shot_no = ? AND prompt_version = ?
            """,
            (episode.strip(), shot_no, prompt_version.strip()),
        ).fetchone()
        return None if row is None else _recipe_from_row(row)
    except sqlite3.Error as error:
        raise ToolError("Prompt 配方读取失败", detail=type(error).__name__) from error
    finally:
        connection.close()


def list_recipes(episode: str, prompt_version: str) -> list[PromptRecipe]:
    """按镜号顺序读取一集同版本配方，供生图批次直接消费。"""
    if not isinstance(episode, str) or not episode.strip():
        raise ToolError("剧集名称不能为空")
    if not isinstance(prompt_version, str) or not prompt_version.strip():
        raise ToolError("Prompt 版本不能为空")

    connection = _connect()
    try:
        rows = connection.execute(
            """
            SELECT episode, shot_no, prompt_version, prompt, prompt_sha256, scene_constraint,
                   model, size, reference_asset_ids_json, seed,
                   source_request_id, target_request_id
            FROM recipe
            WHERE episode = ? AND prompt_version = ?
            ORDER BY shot_no
            """,
            (episode.strip(), prompt_version.strip()),
        ).fetchall()
        return [_recipe_from_row(row) for row in rows]
    except sqlite3.Error as error:
        raise ToolError("Prompt 配方读取失败", detail=type(error).__name__) from error
    finally:
        connection.close()
