"""Resolve the existing Comic Skills' knowledge references; no model or runtime."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from kantoku.config import ToolError
from kantoku.config.settings import ROOT
from kantoku.core.skills.registry import SkillMetadata

KNOWLEDGE_FILES = {
    "director.intent-first": "storytelling.md",
    "director.constraint-priority": "storytelling.md",
    "director.contextual-expression": "composition.md",
    "director.continuity": "shot_design.md",
    "cinematography.language": "cinematography.md",
    "cinematography.spatial-continuity": "camera_language.md",
    "cinematography.lighting": "lighting.md",
}
MODEL_STAGES = frozenset({
    "comic.creative_understanding", "comic.visual_direction", "comic.cinematography",
})
MAX_CONTEXT_CHARS = 16000


def load_director_skill_context(
    metadata: SkillMetadata, *, project_root: Path = ROOT,
) -> dict[str, Any]:
    """Read only the knowledge declared by this discovered Skill's manifest."""
    if metadata.id not in MODEL_STAGES or not metadata.handler_ref or not metadata.knowledge_refs:
        raise ToolError("导演知识加载失败：Skill 未声明可用知识", detail=metadata.id)
    skills_root = (project_root / "skills" / "comic").resolve()
    handler = (project_root / metadata.handler_ref.partition(":")[0]).resolve()
    if not handler.is_relative_to(skills_root):
        raise ToolError("导演知识加载失败：Skill 来源不在 Comic 目录")
    documents = []
    texts = []
    seen = set()
    for reference in metadata.knowledge_refs:
        filename = KNOWLEDGE_FILES.get(reference)
        if filename is None:
            raise ToolError("导演知识加载失败：知识引用未解析", detail=reference)
        if filename in seen:
            continue
        path = (handler.parent / "knowledge" / filename).resolve()
        if not path.is_relative_to(skills_root):
            raise ToolError("导演知识加载失败：知识文件越出 Comic 目录")
        try:
            content = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as error:
            raise ToolError(f"导演知识加载失败：无法读取 {filename}") from error
        if not content:
            raise ToolError(f"导演知识加载失败：{filename} 内容为空")
        texts.append(content)
        documents.append({
            "path": path.relative_to(project_root.resolve()).as_posix(),
            "sha256": hashlib.sha256(content.encode()).hexdigest(), "chars": len(content),
        })
        seen.add(filename)
    content = "\n\n".join(texts)
    if len(content) > MAX_CONTEXT_CHARS:
        raise ToolError("导演知识加载失败：知识上下文超过长度限制")
    return {
        "skill_id": metadata.id, "skill_version": metadata.version,
        "knowledge_refs": list(metadata.knowledge_refs), "documents": documents,
        "sha256": hashlib.sha256(content.encode()).hexdigest(), "content": content,
    }
