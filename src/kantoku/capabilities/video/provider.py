"""视频 Provider 接口与显式 Mock 实现。"""

from __future__ import annotations

import hashlib
from typing import Protocol

from kantoku.config import ToolError

from .models import VideoGenerationRequest, VideoGenerationResult


class VideoProvider(Protocol):
    """未来真实视频供应商必须实现的端口。"""

    provider_id: str

    def preflight(self, request: VideoGenerationRequest) -> None: ...

    def generate(self, request: VideoGenerationRequest) -> VideoGenerationResult: ...


class MockVideoProvider:
    """不联网、不伪装真实成功的视频 Provider。"""

    provider_id = "mock"

    def preflight(self, request: VideoGenerationRequest) -> None:
        """执行零网络输入检查。"""
        if not request.prompt.strip():
            raise ToolError("视频 Prompt 不能为空")

    def generate(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        """返回带 mock=true 的逻辑产物位置。"""
        self.preflight(request)
        digest = hashlib.sha256(request.request_id.encode("utf-8")).hexdigest()[:16]
        return VideoGenerationResult(
            status="succeeded",
            location=f"mock://video/{digest}.mp4",
            provider_job_id=f"mock-video-{digest}",
            actual_fen=0,
            mock=True,
        )
