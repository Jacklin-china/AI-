"""本机浏览器工作台；沿用预算与持久化服务，不向网络暴露密钥。"""

from __future__ import annotations

import argparse
import json
import mimetypes
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from pydantic import ValidationError

from kantoku.config import KantokuError, ToolError, get_settings
from kantoku.config.settings import ROOT
from kantoku.core import budget
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


class StudioApplication:
    """串行运行模型任务；HTTP 线程保持可响应。"""

    def __init__(self) -> None:
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.job: dict[str, Any] = {"state": "idle"}

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
        with self.lock:
            job = dict(self.job)
        return {
            "tasks": tasks,
            "job": job,
            "config": {
                "image": settings.image.model,
                "chat": settings.llm.model_chat,
                "vision": settings.llm.model_vision,
                "size": f"{settings.image.width} × {settings.image.height}",
                "limit": str(settings.budget.image_project_cny),
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
            return {
                "request_id": task.request_id,
                "approved": label.approved,
                "rework_created": not label.approved,
            }
        if action == "archive":
            archived = archive_reviewed_image(task.request_id)
            return {
                "request_id": task.request_id,
                "approved": archived.approved,
                "image_path": str(archived.image_path),
                "metadata_path": str(archived.metadata_path),
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
                message = (
                    str(error)
                    if isinstance(error, KantokuError)
                    else (f"操作未完成（{type(error).__name__}），请检查填写内容或配置。")
                )
                update = {"state": "error", "error": message}
            with self.lock:
                self.job.update(update)

        threading.Thread(target=worker, daemon=True).start()


def make_server(app: StudioApplication, port: int = 0) -> ThreadingHTTPServer:
    """只绑定回环地址；校验 Host 和会话令牌，收费操作只接受同源 JSON。"""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass  # 提示词、会话令牌与个人路径不写访问日志。

        def reply(self, status: int, body: bytes, media_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; "
                "img-src 'self' blob:; style-src 'self'; script-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def json_reply(self, status: int, body: object) -> None:
            self.reply(
                status,
                json.dumps(body, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )

        def allowed(self, *, session: bool) -> bool:
            host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != host:
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin != f"http://{host}":
                return False
            return not session or secrets.compare_digest(
                self.headers.get("X-Studio-Token", ""),
                app.token,
            )

        def do_GET(self) -> None:
            request_path = urlsplit(self.path).path
            if not self.allowed(session=request_path.startswith(("/api/", "/media/"))):
                self.json_reply(403, {"error": "请从本机工作台入口访问"})
                return
            try:
                if request_path == "/":
                    html = (STATIC / "index.html").read_text(encoding="utf-8")
                    self.reply(
                        200,
                        html.replace("__TOKEN__", app.token).encode(),
                        "text/html; charset=utf-8",
                    )
                elif request_path == "/api/state":
                    self.json_reply(200, app.state())
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
            except (KantokuError, OSError):
                self.json_reply(400, {"error": "读取失败，请检查任务记录和配置"})

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
                app.start(self.path.removeprefix("/api/"), data)
                self.json_reply(202, {"accepted": True})
            except (ValueError, KantokuError) as error:
                self.json_reply(
                    400,
                    {"error": str(error) if isinstance(error, KantokuError) else "请求格式不合法"},
                )

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main() -> None:
    """启动本机服务并在默认浏览器打开；收费需在页面另行确认。"""
    parser = argparse.ArgumentParser(description="监督酱浏览器工作台")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    import os

    os.chdir(ROOT)
    get_settings()
    server = make_server(StudioApplication(), args.port)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"监督酱创作工作台：{url}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
