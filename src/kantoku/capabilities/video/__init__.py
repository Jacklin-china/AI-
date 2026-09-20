"""通用视频 Capability。"""

from .models import VideoGenerationRequest, VideoGenerationResult
from .provider import MockVideoProvider, VideoProvider
from .service import VideoService

__all__ = [
    "MockVideoProvider", "VideoGenerationRequest", "VideoGenerationResult",
    "VideoProvider", "VideoService",
]
