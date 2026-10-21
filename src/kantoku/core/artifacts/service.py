"""Artifact 应用服务。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from kantoku.config import ToolError
from kantoku.config.runtime_paths import get_runtime_paths, resolve_runtime_path
from kantoku.core.runtime.models import ArtifactLifecycle, ArtifactRecord, ArtifactType
from kantoku.core.runtime.store import RuntimeStore


def _remove_media_file(location: str | None, *, connection=None) -> bool:
    """删除产物媒体文件；返回本次是否释放了磁盘文件。

    只接受 Runtime 根目录内、未被重定向的真实路径。已登记文件交由运行文件目录统一
    删除并登记 tombstone；登记机制启用前写入的历史产物没有目录记录，删除已校验路径
    以释放空间。文件缺失视为已释放，不报错。
    """
    if not location:
        return False
    manager = get_runtime_paths()
    original = Path(os.path.abspath(manager.root / Path(location)))
    path = resolve_runtime_path(Path(location))
    if (not path.is_relative_to(manager.root) or original != path
            or original.is_symlink() or path.resolve() != path):
        raise ToolError("产物文件路径越界或被重定向，已停止删除", detail=str(path))
    if not path.is_file():
        return False
    try:
        manager.remove_file(path, file_type="artifact", connection=connection)
    except ValueError:
        path.unlink(missing_ok=True)
    return True


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

    def trash(self, artifact_id: str, expected_version: int) -> ArtifactRecord:
        """从正常产物库移入回收站，仍保留原有引用。"""
        return self.store.change_artifact_lifecycle(
            artifact_id, ArtifactLifecycle.TRASHED, expected_version,
        )

    def restore(self, artifact_id: str, expected_version: int) -> ArtifactRecord:
        """恢复到正常产物库，不改变内容版本。"""
        return self.store.change_artifact_lifecycle(
            artifact_id, ArtifactLifecycle.ACTIVE, expected_version,
        )

    def purge(self, artifact_id: str, expected_version: int) -> ArtifactRecord:
        """彻底删除回收站资产；仍被其他产物引用的文件必须保留。"""
        # 版本先于幂等判断校验，避免缺失/非法版本被当成重复请求放过。
        if type(expected_version) is not int or expected_version < 1:
            raise ToolError("请提供有效的资产操作版本，并刷新后重试")
        manager = get_runtime_paths()
        # 文件目录初始化可能迁移同一数据库，必须在取得产物写锁之前完成。
        catalog = manager.catalog
        same_database = catalog.database == self.store.path

        def remove_unshared(current, connection):
            if not current.location:
                return
            path = resolve_runtime_path(Path(current.location))
            retained = self.store.retained_artifact_locations(
                exclude_id=artifact_id, connection=connection)
            shared = any(
                "://" not in location and (manager.root / Path(location)).resolve() == path
                for location in retained
            )
            if not shared:
                _remove_media_file(current.location,
                                   connection=connection if same_database else None)

        return self.store.purge_artifact(artifact_id, expected_version,
                                         before_purge=remove_unshared)
