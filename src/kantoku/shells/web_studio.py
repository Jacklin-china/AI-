"""本机浏览器工作台；沿用预算与持久化服务，不向网络暴露密钥。"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

from loguru import logger
from pydantic import ValidationError

from kantoku.adapters.commerce import (
    CommerceLlmAdapter,
    CoreProductImageCapability,
    MockMarketplaceAdapter,
    MockProductImageCapability,
    MockSourceAdapter,
    MockTranslationAdapter,
)
from kantoku.capabilities.video import MockVideoProvider, VideoService
from kantoku.config import KantokuError, ToolError, get_settings
from kantoku.config.observability import current_trace_id, public_error, request_trace
from kantoku.config.settings import ROOT, CommerceSettings, RuntimeSettings, VideoSettings
from kantoku.core import budget
from kantoku.core.approval import ApprovalService
from kantoku.core.conversations import (
    ConversationMessageRecord,
    IntentPlanner,
    InteractionMode,
    MessageRole,
    MessageType,
)
from kantoku.core.llm import chat, stream_chat
from kantoku.core.runtime.batch import BatchService
from kantoku.core.runtime.graph import GraphRuntime
from kantoku.core.runtime.models import ApprovalDecision
from kantoku.core.runtime.runner import TaskRunner
from kantoku.core.runtime.store import RuntimeStore
from kantoku.core.skills import SkillLoader, SkillRegistry
from kantoku.domains.comic import ComicState, build_comic_workflow
from kantoku.domains.comic.services import StudioComicServices
from kantoku.domains.comic.workflow import WORKFLOW_ID as COMIC_WORKFLOW_ID
from kantoku.domains.commerce import CommerceState, build_commerce_workflow
from kantoku.domains.commerce.workflow import WORKFLOW_ID as COMMERCE_WORKFLOW_ID
from kantoku.perception.qc import qc_image
from kantoku.perception.report import calculate_qc_economics
from kantoku.perception.review import (
    build_rework_plan,
    decide_rework,
    get_rework_item,
    load_human_review,
    load_qc_prediction,
    record_human_review,
    record_qc_prediction,
)
from kantoku.schemas.qc import HumanQcLabel, QcResult
from kantoku.shells.image_cli import _money_fen, _provider
from kantoku.tools.archive import archive_reviewed_image, search_archived_images
from kantoku.tools.studio import (
    StudioTask,
    compose_prompt,
    create_task,
    execute_task,
    list_tasks,
    recover_task,
    refine_prompt,
)

STATIC = Path(__file__).with_name("web")
_SAFE_TRACE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


def traced_request(
    method: Callable[[BaseHTTPRequestHandler], None],
) -> Callable[[BaseHTTPRequestHandler], None]:
    """Give every HTTP request one safe correlation ID across API and workers."""
    def wrapped(handler: BaseHTTPRequestHandler) -> None:
        supplied = handler.headers.get("X-Trace-ID", "")
        trace_id = supplied if _SAFE_TRACE.fullmatch(supplied) else f"trace-{uuid4().hex[:12]}"
        with request_trace(trace_id):
            path = urlsplit(handler.path).path
            logger.bind(component="api", request_id=trace_id).info(
                "request start method={} path={}", method.__name__.removeprefix("do_"), path,
            )
            try:
                method(handler)
            except Exception as error:
                failure = public_error(error, component="api")
                logger.bind(component="api", error_id=failure["error_id"]).error(
                    "request unhandled path={}", path,
                )
                with suppress(BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    handler.json_reply(500, {**failure, "error": failure["safe_message"]})
            finally:
                logger.bind(component="api", request_id=trace_id).info(
                    "request end method={} path={}", method.__name__.removeprefix("do_"), path,
                )
    return wrapped


_CONFIRM_WORDS = ("开始", "确认", "确定", "可以", "执行", "提交", "继续", "批准", "没问题", "好的")
_CONFIRM_EXACT = ("好", "行", "ok", "可以", "好的", "确定", "确认", "开始")


def _is_execution_confirmed(content: str, history: list[ConversationMessageRecord]) -> bool:
    """引导模式下只有短确认口令且前文已有 AI 引导时，才视为确认执行。"""
    text = content.strip().rstrip("。！!~～")
    if not text or len(text) > 30:
        return False
    lowered = text.lower()
    matched = lowered in _CONFIRM_EXACT or any(word in lowered for word in _CONFIRM_WORDS)
    if not matched:
        return False
    return any(item.role == MessageRole.ASSISTANT for item in history)


def _guided_requirement(history: list[ConversationMessageRecord]) -> str:
    """把引导过程中收集到的用户需求汇总为工作流输入，不含最后的确认口令。"""
    users = [
        item.content.strip() for item in history
        if item.role == MessageRole.USER and item.content.strip()
    ]
    checklist = next(
        (item.content.strip() for item in reversed(history)
         if item.role == MessageRole.ASSISTANT and item.content.strip()),
        "",
    )
    summary = "；".join(users[:-1][-3:])
    requirement = f"{summary}。已确认参数：{checklist}" if checklist else summary
    return requirement[:800]


_COUNT_PATTERN = re.compile(r"(\d{1,2}|[一二两三四五六七八九十])\s*[张幅条个份只组套]")
_CN_NUMERALS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _image_count(text: str) -> int:
    """从需求文本识别图片数量，用于按量预估费用；识别不到按 1 张。"""
    match = _COUNT_PATTERN.search(text)
    if not match:
        return 1
    token = match.group(1)
    count = int(token) if token.isdigit() else _CN_NUMERALS.get(token, 1)
    return min(max(count, 1), 20)


def _wants_prompt_enhancement(data: dict[str, Any]) -> bool:
    """创作域可勾选「AI 优化提示词」；默认关闭，避免篡改精确需求。"""
    return data.get("enhance_prompt") is True


def _enhance_prompt(requirement: str, trace_id: str) -> str:
    """把用户需求改写为详细生图提示词；改写失败时退回原文，不阻断执行。"""
    try:
        message = chat([
            {
                "role": "system",
                "content": (
                    "你是生图提示词专家。把用户的制作需求改写为一段详细的中文生图提示词，"
                    "涵盖主体、动作、风格、构图、光线与质量词；只输出提示词本身，不超过 200 字。"
                ),
            },
            {"role": "user", "content": requirement},
        ])
    except Exception as error:
        logger.bind(component="studio", trace_id=trace_id).warning(
            "提示词优化失败，使用原始需求：{}", type(error).__name__
        )
        return requirement
    enhanced = (message.content or "").strip()
    return enhanced or requirement


class StudioApplication:
    """串行运行模型任务；HTTP 线程保持可响应。"""

    def __init__(self) -> None:
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.job: dict[str, Any] = {"state": "idle"}
        configured = get_settings().storage.sqlite_path
        database_path = configured if configured.is_absolute() else ROOT / configured
        self.runtime_store = RuntimeStore(database_path)
        self.runtime = GraphRuntime(self.runtime_store)
        self.skills = SkillRegistry()
        SkillLoader(ROOT / "skills", project_root=ROOT).load(self.skills)
        settings = get_settings()
        self.runtime_settings = getattr(settings, "runtime", RuntimeSettings())
        video_settings = getattr(settings, "video", VideoSettings())
        commerce_settings = getattr(settings, "commerce", CommerceSettings(data_mode="demo"))
        self.commerce_settings = commerce_settings
        video = VideoService(self.runtime_store, MockVideoProvider(), video_settings)
        image_provider = _provider()
        self.runtime.register(build_comic_workflow(
            StudioComicServices(image_provider),
            video_service=video,
            video_enabled=video_settings.enabled,
        ))
        commerce_llm = (
            CommerceLlmAdapter() if commerce_settings.text_mode == "real" else None
        )
        translator = commerce_llm or MockTranslationAdapter()
        product_image = (
            CoreProductImageCapability(
                image_provider,
                max_fen=commerce_settings.real_image_acceptance_max_fen,
            )
            if commerce_settings.image_mode == "real"
            else MockProductImageCapability()
        )
        self.runtime.register(build_commerce_workflow(
            MockSourceAdapter(), MockMarketplaceAdapter(), self.skills,
            translator,
            product_image=product_image,
            listing_writer=commerce_llm.draft if commerce_llm else None,
            marketplace_name=commerce_settings.marketplace,
        ))
        self.approvals = ApprovalService(self.runtime_store, self.runtime)
        self.batches = BatchService(self.runtime_store, self.runtime)
        self.runner = TaskRunner(max_workers=2)
        self.intent_planner = IntentPlanner()

    def create_conversation(self, data: dict[str, Any]) -> dict[str, Any]:
        mode = InteractionMode(str(data.get("interaction_mode", "autonomous")))
        record = self.runtime_store.create_conversation(
            str(data.get("title", "新对话")), interaction_mode=mode,
            domain=str(data["domain"]) if data.get("domain") else None,
        )
        return record.model_dump(mode="json")

    def list_conversations(
        self, *, domain: str | None = None, query: str = ""
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self.runtime_store.list_conversations(domain=domain, query=query)
        ]

    def rename_conversation(self, conversation_id: str, title: str) -> dict[str, Any]:
        cleaned = title.strip()[:80]
        if not cleaned:
            raise ToolError("对话标题不能为空")
        return self.runtime_store.update_conversation(
            conversation_id, title=cleaned,
        ).model_dump(mode="json")

    def delete_conversation(self, conversation_id: str) -> dict[str, bool]:
        self.runtime_store.delete_conversation(conversation_id)
        return {"deleted": True}

    def conversation(self, conversation_id: str) -> dict[str, Any]:
        result = self.runtime_store.get_conversation(conversation_id).model_dump(mode="json")
        result["messages"] = [
            item.model_dump(mode="json")
            for item in self.runtime_store.list_conversation_messages(conversation_id)
        ]
        return result

    def stream_conversation(
        self, conversation_id: str, data: dict[str, Any]
    ) -> Any:
        """Yield real model deltas followed by an optional existing Core run."""
        conversation = self.runtime_store.get_conversation(conversation_id)
        trace_id = str(data.get("_trace_id") or f"trace-{uuid4().hex[:12]}")
        content = str(data.get("content", "")).strip()
        if not content:
            raise ToolError("消息内容不能为空")
        self.runtime_store.add_conversation_message(
            conversation_id, role=MessageRole.USER, type=MessageType.TEXT, content=content,
        )
        if conversation.title == "新对话":
            self.runtime_store.update_conversation(
                conversation_id, title=content[:32], active_run_id=conversation.active_run_id,
            )
        hint = str(data.get("domain_hint") or conversation.domain or "") or None
        plan = self.intent_planner.plan(content, domain_hint=hint)
        yield "intent", {**plan.model_dump(mode="json"), "trace_id": trace_id}
        history = self.runtime_store.list_conversation_messages(conversation_id)[-16:]
        # 引导模式（创作域工作区）：AI 先逐步确认需求，用户明确确认后才提交工作流；
        # 自主模式（首页）：保持原有行为，识别到执行意图即创建 Run。
        guided = conversation.interaction_mode == InteractionMode.GUIDED
        confirmed = guided and _is_execution_confirmed(content, history)
        target_domain = (
            (conversation.domain or plan.suggested_domain) if guided else plan.suggested_domain
        )
        execute = bool(target_domain) and (confirmed if guided else plan.needs_execution)
        model_history = history[-1:] if execute else history
        if execute:
            instruction = (
                "你已把本次需求交给已连接的生产工作流，工作流即将开始执行。"
                "用一两句话说明接下来会发生什么：需要付费的步骤会弹出审批卡片，"
                "等待用户在卡片上点击批准。禁止说没有能力、不能执行，"
                "禁止要求补充可由默认值补齐的字段，"
                "禁止要求用户再回复文字确认，禁止输出冗长的执行计划清单。"
            )
        elif guided:
            instruction = (
                "你正在创作工作区中以引导模式协助用户完成高精度制作。"
                "主动确认需求的关键信息（主题与主体、风格、画面比例与数量、参考素材），"
                "一次最多追问两个最关键的缺失项；信息足够后给出简明的参数确认清单，"
                "并明确告知：回复「开始」或「确认」即提交生产工作流执行。"
                "在用户确认前不要声称已开始执行。付费步骤要说明会先等待用户确认。"
            )
        else:
            instruction = ""
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "你是 Kantoku 制作监督。简洁、专业地回答。不要声称尚未发生的执行结果。"
                    + instruction
                ),
            },
            *[{"role": item.role.value, "content": item.content} for item in model_history
              if item.role in {MessageRole.USER, MessageRole.ASSISTANT}],
        ]
        complete = ""
        for delta in stream_chat(messages, trace_id=trace_id):
            complete += delta
            yield "delta", {"content": delta}
        message_type = MessageType.PLAN if execute else MessageType.TEXT
        assistant = self.runtime_store.add_conversation_message(
            conversation_id, role=MessageRole.ASSISTANT, type=message_type, content=complete,
        )
        yield "message", assistant.model_dump(mode="json")
        if not execute or not target_domain:
            yield "done", {"run_id": None}
            return
        requirement = _guided_requirement(history) if guided else content
        if target_domain == "commerce":
            requested_mode = str(data.get("data_mode") or self.commerce_settings.data_mode)
            if requested_mode not in {"demo", "production"}:
                raise ToolError("电商数据模式无效")
            state: dict[str, Any] = {
                "requirement": requirement, "locale": "ru-RU",
                "data_mode": requested_mode,
            }
            domain = "commerce"
        else:
            prompt = requirement
            if _wants_prompt_enhancement(data):
                prompt = _enhance_prompt(requirement, trace_id)
            state = {
                "project": conversation.title if conversation.title != "新对话" else "Kantoku Chat",
                "prompt": prompt, "shot_no": 1,
                "estimate_fen": budget.estimate_image_fen(),
                "image_count": _image_count(requirement),
                "confirmed": False,
            }
            domain = "comic"
        run = self.enqueue_core_run({"domain": domain, "state": state})
        self.runtime_store.update_conversation(
            conversation_id, domain=conversation.domain, active_run_id=str(run["id"]),
        )
        yield "run", run
        yield "done", {"run_id": run["id"]}

    def task(self, request_id: str) -> StudioTask:
        for task in list_tasks():
            if task.request_id == request_id:
                return task
        raise ToolError("找不到任务，请刷新历史记录")

    def state(self) -> dict[str, Any]:
        settings = get_settings()
        tasks = []
        archived_ids = {
            item.source_request_id for item in search_archived_images(episode="studio")
        }
        for task in list_tasks():
            result = budget.load_generation_result(task.request_id)
            record = budget.get_reservation(task.request_id)
            prediction = load_qc_prediction(task.request_id)
            review = load_human_review(task.request_id)
            rework = get_rework_item(task.request_id)
            tasks.append(
                {
                    **task.model_dump(),
                    "status": result.status if result else "draft",
                    "has_image": bool(result and result.path and result.path.is_file()),
                    "actual_fen": record.actual_fen if record else None,
                    "ledger_status": record.status if record else "not_submitted",
                    "error": result.error if result else None,
                    "created_at": record.created_at if record else "",
                    "qc": prediction.model_dump() if prediction else None,
                    "review": review.model_dump(mode="json") if review else None,
                    "rework": rework.model_dump(mode="json") if rework else None,
                    "archived": task.request_id in archived_ids,
                }
            )
        tasks.sort(key=lambda item: item["created_at"], reverse=True)
        budgets = {
            project: budget.summarize_budget(project).model_dump()
            for project in sorted({item["project"] for item in tasks})
        }
        with self.lock:
            job = dict(self.job)
        return {
            "tasks": tasks,
            "budgets": budgets,
            "job": job,
            "config": {
                "image": settings.image.model,
                "chat": settings.llm.model_chat,
                "vision": settings.llm.model_vision,
                "size": f"{settings.image.width} × {settings.image.height}",
                "limit": str(settings.budget.image_project_cny),
                "estimate_fen": budget.estimate_image_fen(),
            },
        }

    def perform(self, action: str, data: dict[str, Any]) -> dict[str, Any]:
        if action == "compose":
            return {
                "prompt": compose_prompt(
                    data.get("subject", ""),
                    data.get("purpose", ""),
                    data.get("audience", ""),
                    data.get("style", ""),
                )
            }
        if action == "budget":
            return budget.summarize_budget(data["project"]).model_dump()
        if action == "report":
            report = calculate_qc_economics(
                data["project"],
                "studio",
                expected_shots=data.get("expected_shots", 10),
            )
            return {"report": report.model_dump(mode="json")}
        if data.get("confirmed") is not True:
            raise ToolError("此操作需要在页面中明确确认")
        if action == "refine":
            return {"prompt": refine_prompt(data["prompt"], confirmed=True)}
        if action == "generate":
            estimate = _money_fen(data["price"])
            provider = _provider()
            provider.validate_request(
                prompt=data["prompt"],
                shot_no=data["shot_no"],
                reference_urls=(),
                seed=None,
            )
            task = create_task(data["project"], data["prompt"], data["shot_no"], estimate)
            result = execute_task(task, provider=provider, confirmed=True)
            return {"request_id": task.request_id, "status": result.status}
        task = self.task(data["request_id"])
        if action == "recover":
            result = recover_task(task, provider=_provider())
            return {"request_id": task.request_id, "status": result.status}
        if action == "resume":
            result = execute_task(task, provider=_provider(), confirmed=True)
            return {"request_id": task.request_id, "status": result.status}
        if action == "settle":
            record = budget.settle(task.request_id, _money_fen(data["price"]))
            return {"message": f"已记录实扣 {record.actual_fen} 分"}
        if action == "qc":
            result = budget.load_generation_result(task.request_id)
            if result is None or result.path is None:
                raise ToolError("任务尚未取得图片")
            report = qc_image(
                result.path,
                target_platform=data["purpose"],
                genre=data["purpose"],
                target_audience=data["audience"],
                cinematography_requirements=data["style"],
                key_message=task.prompt,
                confirm_paid=True,
            )
            record_qc_prediction(task.request_id, report)
            return {"request_id": task.request_id, "qc": report.model_dump()}
        if action == "review":
            generation = budget.load_generation_result(task.request_id)
            if generation is None or generation.path is None:
                raise ToolError("任务尚未取得图片")
            raw_result = data.get("result")
            prediction = load_qc_prediction(task.request_id)
            if raw_result is None and prediction is None:
                raise ToolError("请先完成视觉预筛，或提交人工检查结果")
            decision = data.get(
                "decision",
                "approve" if data.get("approved") else "request_revision",
            )
            if decision not in {"approve", "reject", "request_revision"}:
                raise ToolError("人工审批决定无效")
            if data.get("approved") is not (decision == "approve"):
                raise ToolError("人工审批决定与通过状态不一致")
            try:
                review_result = (
                    QcResult.model_validate(raw_result)
                    if raw_result is not None
                    else prediction
                )
                if review_result is None:
                    raise ToolError("人工检查结果不能为空")
                label = HumanQcLabel.model_validate(
                    {
                        "id": f"review-{task.request_id}",
                        "image_path": generation.path,
                        "deliverable_type": data.get("deliverable_type", "still_image"),
                        "target_platform": data["purpose"],
                        "genre": data.get("genre", data["purpose"]),
                        "target_audience": data["audience"],
                        "business_goal": data.get("business_goal"),
                        "key_message": data.get("key_message", task.prompt),
                        "first_glance_goal": data.get("first_glance_goal"),
                        "visual_style": data.get("style"),
                        "style_reference": data.get("style_reference"),
                        "persona_reference": data.get("persona_reference"),
                        "cinematography_requirements": data["cinematography_requirements"],
                        "cinematography_notes": data["cinematography_notes"],
                        "review_seconds": data.get("review_seconds"),
                        "result": review_result,
                        "approved": data["approved"],
                        "failure_reasons": data.get("failure_reasons", []),
                    }
                )
            except (KeyError, ValidationError) as error:
                raise ToolError("人工终审信息不完整或互相矛盾") from error
            record_human_review(task.request_id, label)
            if decision == "reject":
                decide_rework(task.request_id, "cancelled")
            return {
                "request_id": task.request_id,
                "approved": label.approved,
                "decision": decision,
                "rework_created": decision == "request_revision",
                "message": {
                    "approve": "人工终审已通过，可以归档交付。",
                    "reject": "作品已拒绝，不会进入付费返工。",
                    "request_revision": "已进入定向返工队列，尚未产生新费用。",
                }[decision],
            }
        if action == "archive":
            archived = archive_reviewed_image(task.request_id)
            return {
                "request_id": task.request_id,
                "approved": archived.approved,
                "image_path": str(archived.image_path),
                "metadata_path": str(archived.metadata_path),
                "message": "作品及质检元数据已归档。",
            }
        if action == "prepare_rework":
            item = get_rework_item(task.request_id)
            if item is None:
                raise ToolError("该任务没有待处理的返工项")
            if item.status == "approved" and item.target_request_id is not None:
                target = self.task(item.target_request_id)
                return {
                    "source_request_id": task.request_id,
                    "request_id": target.request_id,
                    "prompt": target.prompt,
                    "status": item.status,
                    "paid": False,
                }
            if item.status != "pending":
                raise ToolError("返工项已经取消，不能再创建任务")
            plan = build_rework_plan(item)
            corrected_prompt = "\n".join(
                [
                    task.prompt,
                    "定向返工（只修改已确认的问题）：",
                    *plan.correction_directives,
                    "必须保留：",
                    *plan.preserve_constraints,
                ]
            )
            target_id = f"studio-{secrets.token_hex(16)}"
            target = create_task(
                task.project,
                corrected_prompt,
                task.shot_no,
                task.estimate_fen,
                request_id=target_id,
            )
            decided = decide_rework(
                task.request_id,
                "approved",
                target_request_id=target.request_id,
            )
            return {
                "source_request_id": task.request_id,
                "request_id": target.request_id,
                "prompt": target.prompt,
                "status": decided.status,
                "paid": False,
            }
        if action == "cancel_rework":
            item = decide_rework(task.request_id, "cancelled")
            return {"request_id": task.request_id, "status": item.status}
        raise ToolError("不支持的操作")

    def list_core_runs(self) -> list[dict[str, Any]]:
        """返回真实 Run 与 NodeExecution，供工作台渲染。"""
        return [self._run_payload(record.id) for record in self.runtime_store.list_runs()]

    def get_core_run(self, run_id: str) -> dict[str, Any]:
        """返回单个真实 Run。"""
        return self._run_payload(run_id)

    def _run_payload(self, run_id: str) -> dict[str, Any]:
        run = self.runtime_store.get_run(run_id)
        payload = run.model_dump(mode="json")
        payload["nodes"] = [
            node.model_dump(mode="json") for node in self.runtime_store.list_nodes(run_id)
        ]
        return payload

    def create_core_run(self, data: dict[str, Any]) -> dict[str, Any]:
        """根据 shell 选择 Domain Pack；Core 本身没有领域分支。"""
        domain = str(data.get("domain", "")).strip().lower()
        if domain == "comic":
            state = ComicState.model_validate(data.get("state", data))
            run = self.runtime.start(COMIC_WORKFLOW_ID, state)
        elif domain == "commerce":
            raw_state = dict(data.get("state", data))
            raw_state.setdefault("max_reworks", self.runtime_settings.max_reworks)
            raw_state.setdefault("data_mode", self.commerce_settings.data_mode)
            state = CommerceState.model_validate(raw_state)
            run = self.runtime.start(COMMERCE_WORKFLOW_ID, state)
        else:
            raise ToolError("不支持的 Domain Pack", detail=domain)
        return self._run_payload(run.id)

    def enqueue_core_run(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create a visible Run immediately and execute it on the bounded worker pool."""
        domain = str(data.get("domain", "")).strip().lower()
        if domain == "comic":
            state = ComicState.model_validate(data.get("state", data))
            run = self.runtime.create(COMIC_WORKFLOW_ID, state)
        elif domain == "commerce":
            raw_state = dict(data.get("state", data))
            raw_state.setdefault("max_reworks", self.runtime_settings.max_reworks)
            raw_state.setdefault("data_mode", self.commerce_settings.data_mode)
            state = CommerceState.model_validate(raw_state)
            run = self.runtime.create(COMMERCE_WORKFLOW_ID, state)
        else:
            raise ToolError("不支持的 Domain Pack", detail=domain)

        def execute() -> None:
            try:
                self.runtime.resume(run.id)
            except Exception as error:
                public_error(error, component="runtime-worker", run_id=run.id)

        self.runner.submit(execute)
        return self._run_payload(run.id)

    def resume_core_run(self, run_id: str) -> dict[str, Any]:
        """从最后 checkpoint 恢复 Run。"""
        run = self.runtime.resume(run_id)
        return self._run_payload(run.id)

    def list_core_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        """读取 Run 产物。"""
        return [
            item.model_dump(mode="json")
            for item in self.runtime_store.list_artifacts(run_id)
        ]

    def query_core_artifacts(
        self,
        *,
        run_id: str | None = None,
        type_name: str | None = None,
        domain: str | None = None,
    ) -> list[dict[str, Any]]:
        """跨 Run 查询产物。"""
        from kantoku.core.runtime.models import ArtifactType

        artifact_type = ArtifactType(type_name) if type_name else None
        return [
            item.model_dump(mode="json")
            for item in self.runtime_store.list_artifacts(
                run_id, type=artifact_type, domain=domain
            )
        ]

    def get_core_artifact(self, artifact_id: str) -> dict[str, Any]:
        """读取单个 Artifact。"""
        return self.runtime_store.get_artifact(artifact_id).model_dump(mode="json")

    def get_core_artifact_content(self, artifact_id: str) -> Path:
        """读取真实 Artifact 文件；Mock/Blocked 永远不会返回占位内容。"""
        artifact = self.runtime_store.get_artifact(artifact_id)
        origin = str(artifact.metadata.get("origin", "mock"))
        if origin != "real" or not artifact.location:
            raise ToolError("该产物没有真实文件")
        path = Path(artifact.location)
        if not path.is_file():
            raise ToolError("产物文件不可用")
        return path

    def list_core_events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        """读取 Runtime 事件流；按 sequence 升序，供刷新与断线续传。"""
        return [
            item.model_dump(mode="json")
            for item in self.runtime_store.list_events(run_id, after=after)
        ]

    def core_run_status(self, run_id: str) -> str:
        """读取 Run 当前状态，供 SSE 判断是否结束。"""
        return self.runtime_store.get_run(run_id).status.value

    def list_core_approvals(self) -> list[dict[str, Any]]:
        """读取全部通用审批，pending 排在调用方所需顺序。"""
        return [
            item.model_dump(mode="json")
            for item in self.runtime_store.list_approvals()
        ]

    def list_core_skills(self) -> list[dict[str, Any]]:
        """返回文件化 Skill metadata，不泄露机器绝对路径。"""
        return [item.model_dump(mode="json") for item in self.skills.list()]

    def decide_core_approval(
        self, approval_id: str, action: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """提交审批决定；Run 由显式 resume 继续。"""
        decisions = {
            "approve": ApprovalDecision.APPROVE,
            "reject": ApprovalDecision.REJECT,
            "revise": ApprovalDecision.REQUEST_REVISION,
        }
        try:
            decision = decisions[action]
        except KeyError:
            raise ToolError("审批操作无效", detail=action) from None
        approval_before = self.runtime_store.get_approval(approval_id)
        run = self.approvals.decide_and_resume(
            approval_id, decision, data.get("response", data)
        )
        if approval_before.decision is ApprovalDecision.PENDING:
            for batch_id in self.runtime_store.batch_ids_for_run(run.id):
                self.batches.refresh(batch_id, force_progress=True)
        payload = self._run_payload(run.id)
        payload["decision"] = decision.value
        return payload

    def cancel_core_run(self, run_id: str) -> dict[str, Any]:
        """取消一个尚未结束的 Run。"""
        run = self.runtime.cancel(run_id)
        for batch_id in self.runtime_store.batch_ids_for_run(run.id):
            self.batches.refresh(batch_id, force_progress=True)
        return self._run_payload(run.id)

    def create_core_batch(self, data: dict[str, Any]) -> dict[str, Any]:
        """创建同一 Workflow 的通用 Batch。"""
        workflow_id = str(data.get("workflow", "")).strip()
        workflow = self.runtime.workflow(workflow_id)
        raw_items = data.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ToolError("Batch items 必须是非空数组")
        states = []
        for item in raw_items:
            raw_state = dict(item)
            if "max_reworks" in workflow.state_type.model_fields:
                raw_state.setdefault("max_reworks", self.runtime_settings.max_reworks)
            states.append(workflow.state_type.model_validate(raw_state))
        concurrency_limit = int(data.get("concurrency_limit", 2))
        maximum = self.runtime_settings.batch_max_concurrency
        if concurrency_limit > maximum:
            raise ToolError(
                "Batch 并发超过配置上限",
                detail=f"requested={concurrency_limit}; max={maximum}",
            )
        batch = self.batches.create(
            name=str(data.get("name", workflow_id)).strip() or workflow_id,
            workflow_id=workflow_id,
            states=states,
            concurrency_limit=concurrency_limit,
        )
        return self._batch_payload(batch.id)

    def _batch_payload(self, batch_id: str) -> dict[str, Any]:
        batch = self.batches.summarize(batch_id)
        payload = batch.model_dump(mode="json")
        payload["runs"] = [self._run_payload(run_id) for run_id in batch.run_ids]
        payload["progress"] = self.batches.progress(batch.id)
        return payload

    def list_core_batches(self) -> list[dict[str, Any]]:
        """查询所有 Batch。"""
        return [self._batch_payload(item.id) for item in self.runtime_store.list_batches()]

    def cancel_core_batch(self, batch_id: str) -> dict[str, Any]:
        """取消 Batch 中的未结束 Run。"""
        self.batches.cancel(batch_id)
        return self._batch_payload(batch_id)

    def start(self, action: str, data: dict[str, Any]) -> None:
        if action not in {
            "compose",
            "budget",
            "refine",
            "generate",
            "recover",
            "resume",
            "settle",
            "qc",
            "review",
            "archive",
            "prepare_rework",
            "cancel_rework",
            "report",
        }:
            raise ToolError("不支持的操作")
        with self.lock:
            if self.job["state"] == "running":
                raise ToolError("已有操作进行中，请等待完成")
            self.job = {"state": "running", "action": action, "id": secrets.token_hex(8)}

        def worker() -> None:
            try:
                result = self.perform(action, data)
                update = {"state": "done", "result": result}
            except Exception as error:
                failure = public_error(error, component="studio-job")
                update = {"state": "error", "error": failure["safe_message"], **failure}
            with self.lock:
                self.job.update(update)

        self.runner.submit(worker)


def make_server(app: StudioApplication, port: int = 0) -> ThreadingHTTPServer:
    """只绑定回环地址；校验 Host 和会话令牌，收费操作只接受同源 JSON。"""

    class Handler(BaseHTTPRequestHandler):
        DEV_ORIGINS = {"http://127.0.0.1:5173", "http://localhost:5173"}

        def log_message(self, format: str, *args: object) -> None:
            pass  # 提示词、会话令牌与个人路径不写访问日志。

        def _cors(self) -> None:
            origin = self.headers.get("Origin")
            if origin in self.DEV_ORIGINS:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header(
                    "Access-Control-Allow-Headers", "Content-Type, X-Studio-Token, X-Trace-ID",
                )
                self.send_header(
                    "Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS",
                )
                self.send_header("Access-Control-Expose-Headers", "X-Trace-ID")

        def error_reply(self, status: int, error: Exception) -> None:
            failure = public_error(error, component="api")
            self.json_reply(status, {**failure, "error": failure["safe_message"]})

        def reply(self, status: int, body: bytes, media_type: str) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", media_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                self.send_header("X-Trace-ID", current_trace_id() or "-")
                self._cors()
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; "
                    "img-src 'self' blob:; style-src 'self'; script-src 'self'; "
                    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
                )
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return

        def stream_events(self, run_id: str, query: str) -> None:
            """以 SSE 推送 Runtime 事件；支持 Last-Event-ID 与 after 续传。"""
            params = parse_qs(query)
            after = int(params.get("after", ["0"])[0] or 0)
            header_id = self.headers.get("Last-Event-ID")
            if header_id:
                after = max(after, int(header_id))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Trace-ID", current_trace_id() or "-")
            self._cors()
            self.end_headers()
            cursor = after
            deadline = time.time() + 600
            while time.time() < deadline:
                try:
                    events = app.list_core_events(run_id, cursor)
                    for event in events:
                        cursor = max(cursor, int(event["sequence"]))
                        body = json.dumps(event, ensure_ascii=False)
                        self.wfile.write(
                            f"id: {event['sequence']}\n"
                            f"event: {event['event_type']}\n"
                            f"data: {body}\n\n".encode()
                        )
                    status = app.core_run_status(run_id)
                    terminal = status in {"completed", "failed", "cancelled"}
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    if terminal and not events:
                        self.wfile.write(b"event: end\ndata: {}\n\n")
                        self.wfile.flush()
                        return
                    time.sleep(1.0)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    return
                except (KantokuError, OSError, ValueError):
                    self.wfile.write(b"event: error\ndata: {}\n\n")
                    self.wfile.flush()
                    return

        def json_reply(self, status: int, body: object) -> None:
            self.reply(
                status,
                json.dumps(body, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )

        def allowed(self, *, session: bool) -> bool:
            host = f"127.0.0.1:{self.server.server_port}"
            request_host = self.headers.get("Host", "")
            if request_host not in {host, f"localhost:{self.server.server_port}"}:
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {f"http://{host}", *self.DEV_ORIGINS}:
                return False
            return not session or secrets.compare_digest(
                self.headers.get("X-Studio-Token", ""),
                app.token,
            )

        @traced_request
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            request_path = parsed.path
            # SSE 由 EventSource 发起，无法携带自定义头，令牌改由查询参数校验。
            needs_session = request_path.startswith(("/api/", "/media/")) and not (
                request_path == "/api/session"
                or (
                    request_path.startswith("/api/runs/")
                    and request_path.endswith("/events/stream")
                )
            )
            if not self.allowed(session=needs_session):
                self.json_reply(403, {"error": "请从本机工作台入口访问"})
                return
            try:
                app_routes = {
                    "/",
                    "/workspace",
                    "/runs",
                    "/approvals",
                    "/batches",
                    "/assets",
                    "/history",
                    "/skills",
                    "/settings",
                    "/tasks",
                }
                is_app_route = (
                    request_path in app_routes
                    or request_path.startswith("/runs/")
                    or request_path.startswith("/domain/")
                    or request_path.startswith("/workspace/")
                    or request_path.startswith("/tasks/")
                )
                if is_app_route:
                    html = (STATIC / "index.html").read_text(encoding="utf-8")
                    self.reply(
                        200,
                        html.replace("__TOKEN__", app.token).encode(),
                        "text/html; charset=utf-8",
                    )
                elif request_path == "/api/session":
                    self.json_reply(200, {"token": app.token})
                elif request_path == "/api/state":
                    self.json_reply(200, app.state())
                elif request_path == "/api/runs":
                    self.json_reply(200, {"runs": app.list_core_runs()})
                elif request_path.startswith("/api/runs/"):
                    parts = request_path.strip("/").split("/")
                    if len(parts) == 4 and parts[3] == "artifacts":
                        self.json_reply(200, {"artifacts": app.list_core_artifacts(parts[2])})
                    elif len(parts) == 4 and parts[3] == "events":
                        query = parse_qs(parsed.query)
                        after = int(query.get("after", ["0"])[0] or 0)
                        self.json_reply(
                            200, {"events": app.list_core_events(parts[2], max(after, 0))}
                        )
                    elif len(parts) == 5 and parts[3] == "events" and parts[4] == "stream":
                        query_token = parse_qs(parsed.query).get("token", [""])[0]
                        if not self.allowed(session=False) or not secrets.compare_digest(
                            query_token, app.token
                        ):
                            self.json_reply(403, {"error": "事件流会话验证失败"})
                            return
                        self.stream_events(parts[2], parsed.query)
                        return
                    elif len(parts) == 3:
                        self.json_reply(200, app.get_core_run(parts[2]))
                    else:
                        self.json_reply(404, {"error": "Core API 路径不存在"})
                elif request_path == "/api/approvals":
                    self.json_reply(200, {"approvals": app.list_core_approvals()})
                elif request_path == "/api/skills":
                    self.json_reply(200, {"skills": app.list_core_skills()})
                elif request_path == "/api/artifacts":
                    query = parse_qs(parsed.query)
                    self.json_reply(200, {"artifacts": app.query_core_artifacts(
                        run_id=query.get("run_id", [None])[0],
                        type_name=query.get("type", [None])[0],
                        domain=query.get("domain", [None])[0],
                    )})
                elif request_path.startswith("/api/artifacts/"):
                    parts = request_path.strip("/").split("/")
                    if len(parts) == 4 and parts[3] == "content":
                        content = app.get_core_artifact_content(parts[2])
                        media_type = (
                            mimetypes.guess_type(content.name)[0]
                            or "application/octet-stream"
                        )
                        self.reply(200, content.read_bytes(), media_type)
                    elif len(parts) == 3:
                        self.json_reply(200, app.get_core_artifact(parts[2]))
                    else:
                        self.json_reply(404, {"error": "Artifact API 路径不存在"})
                elif request_path == "/api/batches":
                    self.json_reply(200, {"batches": app.list_core_batches()})
                elif request_path.startswith("/api/batches/"):
                    batch_id = request_path.removeprefix("/api/batches/")
                    self.json_reply(200, app._batch_payload(batch_id))
                elif request_path == "/api/conversations":
                    query = parse_qs(parsed.query)
                    self.json_reply(200, {"conversations": app.list_conversations(
                        domain=query.get("domain", [None])[0],
                        query=query.get("q", [""])[0],
                    )})
                elif request_path.startswith("/api/conversations/"):
                    parts = request_path.strip("/").split("/")
                    if len(parts) == 3:
                        self.json_reply(200, app.conversation(parts[2]))
                    else:
                        self.json_reply(404, {"error": "Conversation API 路径不存在"})
                elif request_path.startswith("/media/"):
                    task = app.task(request_path.removeprefix("/media/"))
                    result = budget.load_generation_result(task.request_id)
                    if result is None or result.path is None or not result.path.is_file():
                        raise ToolError("原图片不可用")
                    self.reply(200, result.path.read_bytes(), "image/png")
                elif request_path.startswith("/assets/"):
                    static_root = STATIC.resolve()
                    candidate = (STATIC / unquote(request_path).removeprefix("/")).resolve()
                    if not candidate.is_relative_to(static_root) or not candidate.is_file():
                        self.json_reply(404, {"error": "页面资源不存在"})
                        return
                    media_type = (
                        mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
                    )
                    self.reply(200, candidate.read_bytes(), media_type)
                else:
                    self.json_reply(404, {"error": "页面不存在"})
            except (KantokuError, OSError) as error:
                self.error_reply(400, error)

        @traced_request
        def do_OPTIONS(self) -> None:
            if not self.allowed(session=False):
                self.send_response(403)
                self.end_headers()
                return
            self.send_response(204)
            self.send_header("X-Trace-ID", current_trace_id() or "-")
            self._cors()
            self.end_headers()

        @traced_request
        def do_POST(self) -> None:
            if not self.allowed(session=True):
                self.json_reply(403, {"error": "会话验证失败，请刷新页面"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ToolError("请求内容过长或为空")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict) or not self.path.startswith("/api/"):
                    raise ToolError("请求格式不合法")
                request_path = urlsplit(self.path).path
                parts = request_path.strip("/").split("/")
                if request_path == "/api/runs":
                    self.json_reply(201, app.create_core_run(data))
                elif request_path == "/api/conversations":
                    self.json_reply(201, app.create_conversation(data))
                elif len(parts) == 5 and parts[:2] == ["api", "conversations"] \
                        and parts[3:] == ["messages", "stream"]:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Accel-Buffering", "no")
                    self.send_header("X-Trace-ID", current_trace_id() or "-")
                    self._cors()
                    self.end_headers()
                    trace_id = current_trace_id() or f"trace-{uuid4().hex[:12]}"
                    data["_trace_id"] = trace_id
                    try:
                        for event_name, payload in app.stream_conversation(parts[2], data):
                            encoded = json.dumps(payload, ensure_ascii=False)
                            self.wfile.write(
                                f"event: {event_name}\ndata: {encoded}\n\n".encode()
                            )
                            self.wfile.flush()
                    except Exception as error:
                        failure = public_error(
                            error, component="conversation", conversation_id=parts[2],
                            trace_id=trace_id,
                        )
                        encoded = json.dumps(failure, ensure_ascii=False)
                        self.wfile.write(f"event: error\ndata: {encoded}\n\n".encode())
                        self.wfile.flush()
                elif len(parts) == 4 and parts[:2] == ["api", "runs"] \
                        and parts[3] == "resume":
                    self.json_reply(200, app.resume_core_run(parts[2]))
                elif len(parts) == 4 and parts[:2] == ["api", "runs"] \
                        and parts[3] == "cancel":
                    self.json_reply(200, app.cancel_core_run(parts[2]))
                elif len(parts) == 4 and parts[:2] == ["api", "approvals"]:
                    self.json_reply(
                        200, app.decide_core_approval(parts[2], parts[3], data)
                    )
                elif request_path == "/api/batches":
                    self.json_reply(201, app.create_core_batch(data))
                elif len(parts) == 4 and parts[:2] == ["api", "batches"] \
                        and parts[3] == "cancel":
                    self.json_reply(200, app.cancel_core_batch(parts[2]))
                else:
                    app.start(request_path.removeprefix("/api/"), data)
                    self.json_reply(202, {"accepted": True})
            except (ValueError, ValidationError, KantokuError) as error:
                self.error_reply(400, error)

        @traced_request
        def do_PATCH(self) -> None:
            if not self.allowed(session=True):
                self.json_reply(403, {"error": "会话验证失败"})
                return
            try:
                parts = urlsplit(self.path).path.strip("/").split("/")
                length = int(self.headers.get("Content-Length", "0"))
                if len(parts) != 3 or parts[:2] != ["api", "conversations"] \
                        or not 0 < length <= 4096:
                    raise ToolError("请求格式不合法")
                data = json.loads(self.rfile.read(length))
                self.json_reply(200, app.rename_conversation(parts[2], str(data["title"])))
            except (ValueError, KeyError, KantokuError) as error:
                self.error_reply(400, error)

        @traced_request
        def do_DELETE(self) -> None:
            if not self.allowed(session=True):
                self.json_reply(403, {"error": "会话验证失败"})
                return
            try:
                parts = urlsplit(self.path).path.strip("/").split("/")
                if len(parts) != 3 or parts[:2] != ["api", "conversations"]:
                    raise ToolError("请求格式不合法")
                self.json_reply(200, app.delete_conversation(parts[2]))
            except KantokuError as error:
                self.error_reply(400, error)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(*, port: int = 8000, open_browser: bool = True) -> int:
    """Start the local API, SSE endpoint and production frontend."""
    import os

    os.chdir(ROOT)
    settings = get_settings()
    app = StudioApplication()
    server = make_server(app, port)
    url = f"http://127.0.0.1:{server.server_port}"

    def provider_status(*credentials: Callable[[], str]) -> str:
        try:
            for credential in credentials:
                credential()
        except Exception:
            return "BLOCKED"
        return "READY"

    rows = (
        ("Config", "READY", settings.app.name),
        ("Database", "READY", str(app.runtime_store.path)),
        ("Runtime", "READY", "Graph + checkpoint"),
        ("Logging", "READY", str(ROOT / "data" / "logs" / "kantoku.log")),
        ("DeepSeek", provider_status(settings.llm.chat_api_key), settings.llm.model_chat),
        (
            "Ark Vision", provider_status(settings.llm.vision_api_key),
            settings.llm.model_vision,
        ),
        (
            "Jimeng", provider_status(settings.image.access_key, settings.image.secret_key),
            settings.image.model,
        ),
        ("API", "READY", url),
        ("SSE", "READY", f"{url}/api/runs/:id/events/stream"),
    )
    print("\nKantoku Backend")
    for name, status, detail in rows:
        print(f"[{status:<5}] {name:<12} {detail}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.runner.close()
        server.server_close()
    return 0


def main() -> None:
    """Backward-compatible web-studio command."""
    parser = argparse.ArgumentParser(description="监督酱浏览器工作台")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    serve(port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
