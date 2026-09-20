"""从项目 skills 目录发现并加载 Skill。"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml
from pydantic import ValidationError

from kantoku.config import ToolError

from .registry import Skill, SkillExecutor, SkillMetadata, SkillRegistry


class SkillLoader:
    """manifest.yaml + handler.py 文件化 Skill Loader。"""

    def __init__(self, root: Path, *, project_root: Path | None = None) -> None:
        self.root = root.resolve()
        self.project_root = (project_root or root.parent).resolve()

    def discover(self) -> list[Skill]:
        """递归发现、校验并导入所有 manifest。"""
        if not self.root.exists():
            return []
        return [self._load_manifest(path) for path in sorted(self.root.rglob("manifest.yaml"))]

    def load(self, registry: SkillRegistry) -> list[SkillMetadata]:
        """将发现结果注册到现有 Registry。"""
        skills = self.discover()
        registry.load(skills)
        return [skill.metadata for skill in skills]

    def _load_manifest(self, manifest_path: Path) -> Skill:
        try:
            raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ToolError("Skill manifest 顶层必须是对象")
            handler_name = str(raw.pop("handler", "execute"))
            handler_file = str(raw.pop("handler_file", "handler.py"))
            path = (manifest_path.parent / handler_file).resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                raise ToolError("Skill handler 必须位于 skills 目录", detail=handler_file)
            relative = path.relative_to(self.project_root).as_posix()
            raw["handler_ref"] = f"{relative}:{handler_name}"
            metadata = SkillMetadata.model_validate(raw)
        except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as error:
            raise ToolError(
                "Skill manifest 无效", detail=f"{manifest_path}: {type(error).__name__}"
            ) from error
        module = self._module(path, metadata.id)
        executor = getattr(module, handler_name, None)
        if not callable(executor):
            raise ToolError("Skill handler 不可调用", detail=metadata.handler_ref)
        return Skill(metadata=metadata, executor=self._typed_executor(executor))

    @staticmethod
    def _module(path: Path, skill_id: str) -> ModuleType:
        name = "kantoku_file_skill_" + skill_id.replace(".", "_").replace("-", "_")
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ToolError("无法加载 Skill handler", detail=path.name)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as error:
            raise ToolError(
                "Skill handler 导入失败", detail=f"{path.name}: {type(error).__name__}"
            ) from error
        return module

    @staticmethod
    def _typed_executor(executor: Callable[..., Any]) -> SkillExecutor:
        return executor
