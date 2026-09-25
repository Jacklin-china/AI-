"""Shared image capability for lightweight Conversation requests.

Provider submission, budget reservation, and reconciliation remain in the
existing image_gen implementation; this layer only attaches its result to a
Conversation and the common Artifact store.
"""

from __future__ import annotations

import base64
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from loguru import logger

from kantoku.config import ToolError
from kantoku.core import budget
from kantoku.core.conversations import (
    ConversationMessageRecord,
    MessageRole,
    MessageType,
)
from kantoku.core.runtime.models import ArtifactRecord, ArtifactType, utc_now
from kantoku.core.runtime.store import RuntimeStore
from kantoku.tools.image_gen import ImageProvider, gen_image


class ConversationImageService:
    """Deliver one paid image without creating a Graph Run or StudioTask."""

    def __init__(self, store: RuntimeStore, provider: ImageProvider) -> None:
        self.store = store
        self.provider = provider

    def generate(
        self, *, conversation_id: str, user_message_id: str,
        generation_request_id: str, prompt: str, estimate_fen: int,
        reference_artifact_id: str | None = None,
    ) -> ConversationMessageRecord:
        saved_prompt = self.store.save_conversation_generation(
            generation_request_id, conversation_id, user_message_id, prompt,
        )
        event = logger.bind(
            component="conversation-image", conversation_id=conversation_id,
            generation_request_id=generation_request_id,
            provider=str(self.provider.generation_identity().get("provider", "unknown")),
        )
        event.info("generation start")
        reference_urls: tuple[str, ...] = ()
        if reference_artifact_id is not None:
            artifact = self.store.get_artifact(reference_artifact_id)
            if (artifact.conversation_id != conversation_id or artifact.type != ArtifactType.IMAGE
                    or artifact.status != "ready" or not artifact.location):
                raise ToolError("参考图片不是当前聊天的已完成图片")
            source = Path(artifact.location)
            if not source.is_file() or source.stat().st_size > 10_000_000:
                raise ToolError("参考图片不存在或超过供应商大小限制")
            image_bytes = source.read_bytes()
            if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                mime = "image/png"
            elif image_bytes.startswith(b"\xff\xd8\xff"):
                mime = "image/jpeg"
            elif image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
                mime = "image/webp"
            else:
                raise ToolError("当前参考图片格式不受支持")
            reference_urls = (
                f"data:{mime};base64," + base64.b64encode(image_bytes).decode("ascii"),
            )
        result = gen_image(
            saved_prompt, 1, conversation_id=conversation_id,
            client_request_id=generation_request_id, provider=self.provider,
            est_fen=estimate_fen, reference_urls=reference_urls,
        )
        if result.path is None:
            unknown = result.status == "unknown"
            event.warning("generation not ready status={}", result.status)
            return self.store.add_conversation_message(
                conversation_id, role=MessageRole.ASSISTANT,
                type=MessageType.STATUS if unknown else MessageType.ERROR,
                content=(
                    "原生成请求仍待查询或对账；不会重复提交或扣费。"
                    if unknown else "这次生图未完成；不会自动重新付费提交。"
                ),
                event_id=f"generation-status:{generation_request_id}:{result.status}",
            )
        if not result.path.is_file():
            event.error("provider reported missing image file path={}", result.path)
            raise ToolError("生图结果文件不存在；请按错误编号检查日志，不会自动重复提交")

        artifact_id = f"artifact-{uuid5(NAMESPACE_URL, generation_request_id).hex}"
        reservation = budget.get_reservation(generation_request_id)
        if reservation is None:
            raise ToolError("生图结果缺少预算台账")
        if reservation.artifact_id:
            artifact = self.store.get_artifact(reservation.artifact_id)
        else:
            record = ArtifactRecord(
                id=artifact_id, type=ArtifactType.IMAGE, run_id=None,
                conversation_id=conversation_id, node_id="image.generate",
                source="conversation.image.generate", status="ready",
                created_at=utc_now(), location=str(result.path),
                metadata={
                    "origin": "real", "generation_request_id": generation_request_id,
                    "provider_job_id": result.provider_job_id,
                    "parent_artifact_id": reference_artifact_id,
                },
            )
            try:
                artifact = self.store.save_artifact(record)
            except ToolError:
                # A concurrent replay may have inserted the deterministic artifact.
                # A different write error still fails when this lookup finds nothing.
                artifact = self.store.get_artifact(artifact_id)
                if artifact.conversation_id != conversation_id:
                    raise ToolError("生图 Artifact 已属于其他对话") from None
            budget.attach_image_artifact(generation_request_id, artifact.id)
        event.info("artifact ready id={}", artifact.id)
        return self.store.add_conversation_message(
            conversation_id, role=MessageRole.ASSISTANT,
            type=MessageType.ARTIFACT, content="图片已生成",
            artifact_id=artifact.id, event_id=f"generation-artifact:{generation_request_id}",
        )
