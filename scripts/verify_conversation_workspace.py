"""Browser QA: real HTTP/SQLite conversations, isolated data, offline text model only.

Run with uv run python scripts/verify_conversation_workspace.py; Ctrl+C stops.
No production database or external model is accessed. Reuses existing test fixtures.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402
from test_comic_director_coordinator import SKILLS, _parts  # noqa: E402
from test_web_studio import app as app_fixture  # noqa: E402

from kantoku.config import ToolError  # noqa: E402
from kantoku.shells import web_studio  # noqa: E402


def main() -> None:
    directory = ROOT / "data" / "qa" / f"conversation-workspace-{datetime.now():%Y%m%d-%H%M%S}"
    directory.mkdir(parents=True, exist_ok=True)
    patch = pytest.MonkeyPatch()
    application = app_fixture.__wrapped__(patch, directory)

    def model(messages: list[dict[str, str]]) -> str:
        body = json.loads(messages[1]["content"])
        if "context" not in body:
            return json.dumps({"public_summary": "离线替身：公开方案契约检查", "confidence": 0.9,
                               "findings": [], "suggested_patches": []})
        brief = body["context"]["creative_brief"]
        request = brief["original_request"]
        parts = _parts()
        decision = parts[SKILLS[0]]["creative_decision"]
        decision.update(intent_summary=request, narrative_context=request,
                        hard_constraints=brief["hard_constraints"],
                        narrative_focus="离线验证：当前人物与环境关系")
        plan = parts[SKILLS[1]]["director_plan"]
        plan.update(visual_strategy=f"离线导演方案：{request}", visual_focus=request)
        for skill, schema in zip(SKILLS[:3], (
            "CreativeDecision", "DirectorPlan", "CinematographyPlan",
        ), strict=True):
            if f'"title": "{schema}"' in messages[0]["content"]:
                return json.dumps(next(iter(parts[skill].values())), ensure_ascii=False)
        return json.dumps({"public_summary": "离线替身：公开方案契约检查", "confidence": 0.9,
                           "findings": [], "suggested_patches": []})

    def prohibited(*_args, **_kwargs):
        raise ToolError("本验收禁止调用图片或外部 Provider")

    patch.setattr(application, "_comic_director_model", model)
    patch.setattr(application.image_service.provider, "submit", prohibited)
    patch.setattr(web_studio, "stream_chat", lambda *_args, **_kwargs: iter([
        "离线聊天验收：这条回复只属于当前对话。",
    ]))
    if not application.list_conversations(domain="comic"):
        conversation = application.create_conversation({
            "domain": "comic", "interaction_mode": "guided",
        })
        application.rename_conversation(conversation["id"], "穷奇悬崖场景")
        project = application.create_comic_project({
            "title": "旧作品", "brief": {"original_request": "山海经穷奇站在悬崖看村庄"},
        })
        application.create_comic_director(project["project"]["project_id"], {
            "expected_project_version": 1, "creation_mode": "fast",
            "conversation_id": conversation["id"], "creative_operation": "new",
            "task": "山海经穷奇站在悬崖看村庄",
        })
    server = web_studio.make_server(application, port=8766)
    print("Isolated real HTTP/SQLite browser QA: http://127.0.0.1:8766/workspace/comic", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        patch.undo()


if __name__ == "__main__":
    main()
