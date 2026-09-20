"""Skill 注册、校验与执行。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from kantoku.config import ToolError
from kantoku.core.runtime.models import ExecutionStatus, SkillExecutionRecord, utc_now
from kantoku.core.runtime.store import RuntimeStore


class SkillMetadata(BaseModel):
    """可发现、可版本化的 Skill 描述。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    name: str
    domain: str
    description: str
    version: str
    required_tools: tuple[str, ...] = ()
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


SkillExecutor = Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class Skill:
    """metadata 与真实执行函数的绑定。"""

    metadata: SkillMetadata
    executor: SkillExecutor


class SkillRegistry:
    """领域无关 Skill 容器。"""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        """注册一个可执行 Skill。"""
        if skill.metadata.id in self._skills:
            raise ToolError("Skill 已注册", detail=skill.metadata.id)
        self._skills[skill.metadata.id] = skill

    def load(self, skills: Iterable[Skill]) -> None:
        """批量加载领域包导出的 Skill。"""
        for skill in skills:
            self.register(skill)

    def get(self, skill_id: str) -> Skill:
        """按 ID 获取 Skill。"""
        try:
            return self._skills[skill_id]
        except KeyError:
            raise ToolError("Skill 未注册", detail=skill_id) from None

    def execute(
        self,
        skill_id: str,
        inputs: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        store: RuntimeStore | None = None,
        run_id: str = "standalone",
        node_id: str = "standalone",
    ) -> dict[str, Any]:
        """执行 Skill，并在有 Store 时留下审计记录。"""
        skill = self.get(skill_id)
        execution_id = f"skill-exec-{uuid4().hex}"
        started = utc_now()
        try:
            outputs = dict(skill.executor(inputs, context))
        except Exception as error:
            if store is not None:
                store.save_skill_execution(SkillExecutionRecord(
                    id=execution_id, run_id=run_id, node_id=node_id, skill_id=skill_id,
                    status=ExecutionStatus.FAILED, started_at=started,
                    completed_at=utc_now(), error=f"{type(error).__name__}: {error}",
                ))
            raise
        if store is not None:
            store.save_skill_execution(SkillExecutionRecord(
                id=execution_id, run_id=run_id, node_id=node_id, skill_id=skill_id,
                status=ExecutionStatus.COMPLETED, started_at=started,
                completed_at=utc_now(), outputs=outputs,
            ))
        return outputs
