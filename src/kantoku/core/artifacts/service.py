"""Artifact 应用服务。"""

from __future__ import annotations

from typing import Any

from kantoku.core.runtime.models import ArtifactRecord, ArtifactType
from kantoku.core.runtime.store import RuntimeStore


class ArtifactService:
    """为 Node 提供统一产物持久化入口。"""

    def __init__(self, store: RuntimeStore) -> None:
        self.store = store

    def create(
        self, *, type: ArtifactType, run_id: str, node_id: str, source: str,
        location: str | None = None, metadata: dict[str, Any] | None = None,
        status: str = "ready", version: int = 1,
    ) -> ArtifactRecord:
        """保存产物并返回完整记录。"""
        return self.store.create_artifact(
            type=type, run_id=run_id, node_id=node_id, source=source,
            location=location, metadata=metadata, status=status, version=version,
        )

    def for_run(self, run_id: str) -> list[ArtifactRecord]:
        """查询 Run 的全部产物。"""
        return self.store.list_artifacts(run_id)
