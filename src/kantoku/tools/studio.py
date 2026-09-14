"""创作工作台服务：保存不可变生成任务，重启后仍沿用原请求恢复。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from kantoku.config import BudgetError, ToolError, get_settings
from kantoku.config.settings import ROOT
from kantoku.core import budget
from kantoku.core.llm import chat
from kantoku.schemas.media import ImageGenerationResult
from kantoku.tools.image_gen import ImageProvider, gen_image, reconcile_image

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class StudioTask(BaseModel):
    """用户确认时保存的输入；任务文件不保存密钥。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    request_id: str = Field(pattern=r"^studio-[0-9a-f]{32}$")
    project: Text
    prompt: Text
    shot_no: int = Field(gt=0)
    estimate_fen: int = Field(gt=0)


def task_directory() -> Path:
    """任务记录跟随配置数据库，避免依赖启动目录。"""
    path = get_settings().storage.sqlite_path
    return (path if path.is_absolute() else ROOT / path).parent / "studio"


def compose_prompt(subject: str, purpose: str, audience: str, style: str) -> str:
    """零模型费用整理创作要求；用户可在提交前完整修改。"""
    if not subject.strip():
        raise ToolError("请先填写画面主体和你想表达的内容")
    return (
        f"创作内容：{subject.strip()}\n用途：{purpose.strip()}\n"
        f"目标受众：{audience.strip()}\n美术方向：{style.strip()}\n"
        "画面要求：一个明确的视觉主体，前中后景层次清楚，光源方向一致；"
        "人物动作必须有明确接触点、支撑关系和注意对象，避免面向镜头摆拍。"
        "情绪通过视线与微小动作表达，不默认微笑，不用统一温柔表情。"
        "环境细节应有因果：湿润程度随遮挡和材质变化，反射对应真实物体和光源。"
        "按所选画风处理质感，不把插画强制写实。"
        + ("\n海报要求：预留排版空间，关键文字交给后期。" if "海报" in purpose else "")
    )


def refine_prompt(brief: str, *, confirmed: bool = False) -> str:
    """通过统一文本模型出口精炼需求，返回可编辑提示词。"""
    if confirmed is not True:
        raise BudgetError("AI 设计会调用文本模型，请先确认")
    maximum = get_settings().image.prompt_max_chars
    if not brief.strip() or len(brief) > maximum:
        raise ToolError("设计需求为空或过长，请精简")
    reply = chat(
        [
            {
                "role": "system",
                "content": (
                    "你是视觉设计与摄影指导。将用户需求改写为一份可直接生图的中文提示词。"
                    "明确主体、构图、色彩、光源、材质、情绪和受众。保留用户的必需信息；"
                    "模糊需求用保守具体的设计选择补齐，不编造产品功效、价格或品牌承诺。"
                    "海报预留排版空间，关键文字交给后期；一次只描述一个静态画面。"
                    "先解决动作的物理关系和人物注意力：谁扶谁、手落在哪里、身体朝向与"
                    "重心、视线目标；不要将人物统一写成温柔微笑或僵硬并排。"
                    "写实需求使用现场抓拍逻辑：合理的非对称动作、不过度美化的皮肤、"
                    "受遮挡影响的雨水与衣物湿润、与天光匹配的曝光和空间反射。"
                    "不要靠堆叠毛孔、锐利细节或胶片颗粒假装真实；艺术风格保留其表现性。"
                    "避免同时塞入众多环境元素、精确色板和情绪形容词造成模板广告感。"
                    "不堆砌夸张分辨率、互相冲突的镜头或情绪词。只输出最终提示词，"
                    f"不要解释，不超过 {maximum} 个字符。"
                ),
            },
            {"role": "user", "content": brief.strip()},
        ]
    )
    result = (reply.content or "").strip()
    if not result or len(result) > maximum:
        raise ToolError("模型提示词为空或过长，保留原需求供手动编辑")
    return result


def create_task(project: str, prompt: str, shot_no: int, estimate_fen: int) -> StudioTask:
    """先落盘再付费；独占创建避免覆盖历史任务。"""
    try:
        task = StudioTask(
            request_id=f"studio-{uuid4().hex}",
            project=project,
            prompt=prompt,
            shot_no=shot_no,
            estimate_fen=estimate_fen,
        )
    except ValidationError as error:
        raise ToolError("作品名、提示词、镜号或费用不合法") from error
    if len(task.prompt) > get_settings().image.prompt_max_chars:
        raise ToolError("提示词超过配置允许的长度，请精简后生成")
    for previous in list_tasks():
        if previous.project == task.project and previous.shot_no == task.shot_no:
            record = budget.get_reservation(previous.request_id)
            if record is not None and record.status in {"unknown", "submitted"}:
                raise BudgetError("同一画面还有未确认结果的任务，请先从历史任务查询，避免重复收费")
    try:
        directory = task_directory()
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / f"{task.request_id}.json").open("x", encoding="utf-8") as handle:
            handle.write(task.model_dump_json(indent=2))
    except OSError as error:
        raise ToolError("创作记录无法保存，尚未提交生图") from error
    return task


def list_tasks() -> list[StudioTask]:
    """坏记录不会静默消失；保留文件供恢复。"""
    try:
        paths = sorted(task_directory().glob("studio-*.json"), reverse=True)
        return [StudioTask.model_validate_json(p.read_text(encoding="utf-8")) for p in paths]
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as error:
        raise ToolError("历史任务读取失败，请保留原文件并检查数据目录") from error


def execute_task(
    task: StudioTask,
    *,
    provider: ImageProvider,
    confirmed: bool = False,
) -> ImageGenerationResult:
    """真实生成始终经过已有预算控制器，重复执行不会重复提交。"""
    if confirmed is not True:
        raise BudgetError("请先确认本次生成费用")
    if not get_settings().image.force_single:
        raise BudgetError("工作台只允许单张生成，请启用 force_single")
    return gen_image(
        task.prompt,
        task.shot_no,
        project=task.project,
        episode="studio",
        client_request_id=task.request_id,
        provider=provider,
        est_fen=task.estimate_fen,
    )


def recover_task(task: StudioTask, *, provider: ImageProvider) -> ImageGenerationResult:
    """恢复按钮只查询；即使尚未提交，也不能隐式创建收费任务。"""
    saved = budget.load_generation_result(task.request_id)
    if saved is not None and saved.status != "unknown":
        return saved
    if budget.get_reservation(task.request_id) is None:
        raise ToolError("任务尚未提交；请选中它并确认费用后继续生成")
    return reconcile_image(task.request_id, provider=provider)
