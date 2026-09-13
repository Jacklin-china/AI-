"""小批量串行生图：整批原子预占，复用已完成结果，失败或未知状态即停。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from kantoku.config import SchemaError, get_settings
from kantoku.core.budget import reserve_many
from kantoku.perception.review import get_rework_item
from kantoku.schemas.media import ImageGenerationResult, NonBlank
from kantoku.tools.image_gen import ImageProvider, gen_image, prepare_image_reservation
from kantoku.tools.prompt_factory import PromptRecipe, list_recipes


class BatchShot(BaseModel):
    """一个候选对应一个稳定请求 ID，返工由人确认后创建新版本。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: NonBlank
    shot_no: int = Field(gt=0)
    prompt: NonBlank
    reference_urls: list[NonBlank] = Field(default_factory=list, max_length=10)
    seed: int | None = Field(default=None, ge=0)
    rework_of: NonBlank | None = None


class ImageBatch(BaseModel):
    """同一视频、同一集的单候选清单；不在任务文件中写模型和价格。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    project: NonBlank
    episode: NonBlank
    shots: list[BatchShot] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_shots(self) -> Self:
        if len({s.request_id for s in self.shots}) != len(self.shots):
            raise ValueError("请求 ID 重复")
        if len({s.shot_no for s in self.shots}) != len(self.shots):
            raise ValueError("同一批每镜只允许一个候选")
        return self


class BatchResult(BaseModel):
    """按清单顺序返回已处理部分；剩余镜头保持预占，恢复不另起任务。"""

    results: dict[str, ImageGenerationResult]
    pending_request_ids: list[str]


def parse_image_batch(raw: str) -> ImageBatch:
    """解析清单，错误信息不输出提示词正文。"""
    try:
        return ImageBatch.model_validate_json(raw)
    except ValidationError as error:
        raise SchemaError("生图清单格式无效，请检查必填字段、镜号和请求 ID") from error


def _normalize_reference_map(values: Mapping[str, str] | None) -> dict[str, str]:
    """清理素材 ID 到 URL 的映射，并拒绝空值和清理后的重复键。"""
    normalized: dict[str, str] = {}
    for asset_id, url in (values or {}).items():
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise SchemaError("参考素材 ID 不能为空")
        if not isinstance(url, str) or not url.strip():
            raise SchemaError("参考素材 URL 不能为空")
        clean_id = asset_id.strip()
        if clean_id in normalized:
            raise SchemaError("参考素材 ID 重复")
        normalized[clean_id] = url.strip()
    return normalized


def _recipe_request_id(project: str, recipe: PromptRecipe, urls: list[str]) -> str:
    """从配方内容生成稳定候选 ID；内容改变必须产生新任务身份。"""
    payload = {
        "project": project,
        "recipe": recipe.model_dump(mode="json"),
        "reference_urls": urls,
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    ).hexdigest()[:20]
    return f"recipe-s{recipe.shot_no:03d}-{digest}"


def build_recipe_batch(
    *,
    project: str,
    episode: str,
    prompt_version: str,
    reference_urls_by_asset_id: Mapping[str, str] | None = None,
) -> ImageBatch:
    """把已保存配方转换为可预览批次；任何缺失或配置漂移都在付费前失败。"""
    if not isinstance(project, str) or not project.strip():
        raise SchemaError("项目 ID 不能为空")
    settings = get_settings()
    recipes = list_recipes(episode, prompt_version)
    if not recipes:
        raise SchemaError("没有找到该剧集和版本的 Prompt 配方")

    expected_size = f"{settings.image.width}x{settings.image.height}"
    reference_map = _normalize_reference_map(reference_urls_by_asset_id)
    shots: list[BatchShot] = []
    for recipe in recipes:
        if recipe.model != settings.image.model or recipe.size != expected_size:
            raise SchemaError(
                "Prompt 配方与当前生图配置不一致，请新建配方版本后再生成",
                raw=(
                    f"shot={recipe.shot_no}; recipe_model={recipe.model}; "
                    f"recipe_size={recipe.size}"
                ),
            )
        if len(recipe.prompt) > settings.image.prompt_max_chars:
            raise SchemaError(f"镜{recipe.shot_no}的 Prompt 超过供应商字符上限")
        missing = [
            asset_id
            for asset_id in recipe.reference_asset_ids
            if asset_id not in reference_map
        ]
        if missing:
            raise SchemaError(
                f"镜{recipe.shot_no}缺少参考素材 URL 映射",
                raw=json.dumps(missing, ensure_ascii=False),
            )
        urls = [reference_map[asset_id] for asset_id in recipe.reference_asset_ids]
        request_id = recipe.target_request_id or _recipe_request_id(
            project.strip(), recipe, urls
        )
        shots.append(
            BatchShot(
                request_id=request_id,
                shot_no=recipe.shot_no,
                prompt=recipe.prompt,
                reference_urls=urls,
                seed=recipe.seed,
                rework_of=recipe.source_request_id,
            )
        )
    return ImageBatch(project=project.strip(), episode=episode.strip(), shots=shots)


def generate_batch(batch: ImageBatch, *, provider: ImageProvider, est_fen: int) -> BatchResult:
    """所有镜先校验和预占；预算不足零提交，重复执行复用原结果。"""
    for shot in batch.shots:
        if shot.rework_of is None:
            continue
        rework = get_rework_item(shot.rework_of)
        if (
            rework is None
            or rework.status != "approved"
            or rework.target_request_id != shot.request_id
        ):
            raise SchemaError(
                f"镜{shot.shot_no}返工尚未人工批准，或新请求 ID 与批准记录不一致"
            )
    requests = [
        prepare_image_reservation(
            shot.prompt,
            shot.shot_no,
            project=batch.project,
            episode=batch.episode,
            client_request_id=shot.request_id,
            provider=provider,
            est_fen=est_fen,
            reference_urls=shot.reference_urls,
            seed=shot.seed,
        )
        for shot in batch.shots
    ]
    reserve_many(requests)
    results: dict[str, ImageGenerationResult] = {}
    for index, shot in enumerate(batch.shots):
        result = gen_image(
            shot.prompt,
            shot.shot_no,
            project=batch.project,
            episode=batch.episode,
            client_request_id=shot.request_id,
            provider=provider,
            est_fen=est_fen,
            reference_urls=shot.reference_urls,
            seed=shot.seed,
        )
        results[shot.request_id] = result
        if result.status != "succeeded":
            return BatchResult(
                results=results,
                pending_request_ids=[s.request_id for s in batch.shots[index + 1 :]],
            )
    return BatchResult(results=results, pending_request_ids=[])
