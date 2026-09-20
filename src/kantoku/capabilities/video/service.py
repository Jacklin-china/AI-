"""带预算、幂等和 Artifact 持久化的视频能力服务。"""

from __future__ import annotations

from kantoku.config import BudgetError, ToolError
from kantoku.config.settings import VideoSettings
from kantoku.core import budget
from kantoku.core.runtime.models import ArtifactRecord, ArtifactType
from kantoku.core.runtime.store import RuntimeStore

from .models import VideoGenerationRequest
from .provider import VideoProvider


class VideoService:
    """Provider 外层的付费安全边界。"""

    def __init__(
        self, store: RuntimeStore, provider: VideoProvider, settings: VideoSettings
    ) -> None:
        self.store = store
        self.provider = provider
        self.settings = settings

    def generate(self, request: VideoGenerationRequest) -> ArtifactRecord:
        """通过 preflight → reserve → provider → settle 创建 VIDEO Artifact。"""
        if not self.settings.enabled:
            raise ToolError("视频能力未启用", detail="video.enabled=false")
        if self.settings.estimated_fen > self.settings.max_fen:
            raise BudgetError(
                "视频预算不足",
                detail=(f"本次需 {self.settings.estimated_fen} 分，"
                        f"上限 {self.settings.max_fen} 分"),
            )
        image = self.store.get_artifact(request.image_artifact_id)
        if image.type is not ArtifactType.IMAGE or image.run_id != request.run_id:
            raise ToolError("视频输入必须是同一 Run 的图片 Artifact")
        for artifact in self.store.list_artifacts(request.run_id, type=ArtifactType.VIDEO):
            if artifact.metadata.get("request_id") == request.request_id:
                return artifact
        self.provider.preflight(request)
        budget.reserve(
            reservation_id=request.request_id,
            job="video_generation",
            project=request.project,
            episode="video",
            shot_no=request.shot_no,
            kind="video",
            est_fen=self.settings.estimated_fen,
            model=self.settings.model,
        )
        budget.mark_submitted(request.request_id)
        try:
            result = self.provider.generate(request)
        except Exception:
            budget.mark_outcome(request.request_id, "unknown")
            raise
        if result.status != "succeeded" or result.location is None:
            outcome = "unknown" if result.status == "unknown" else "failed"
            budget.mark_outcome(
                request.request_id, outcome, provider_job_id=result.provider_job_id
            )
            if outcome == "failed":
                budget.release(request.request_id)
            raise ToolError("视频生成未成功", detail=result.error or result.status)
        budget.mark_outcome(
            request.request_id, "succeeded", provider_job_id=result.provider_job_id
        )
        budget.settle(request.request_id, result.actual_fen or 0)
        return self.store.create_artifact(
            type=ArtifactType.VIDEO,
            run_id=request.run_id,
            node_id=request.node_id,
            source=f"video.{self.provider.provider_id}",
            location=result.location,
            metadata={
                "request_id": request.request_id,
                "image_artifact_id": request.image_artifact_id,
                "provider_job_id": result.provider_job_id,
                "mock": result.mock,
                "config": request.config,
            },
        )
