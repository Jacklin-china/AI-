"""Browser QA: real HTTP/SQLite conversations, isolated data, offline text model only.

Run with uv run python scripts/verify_conversation_workspace.py; Ctrl+C stops.
No production database or external model is accessed. Reuses existing test fixtures.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402
from test_comic_director_quality import SKILLS, _responses  # noqa: E402
from test_web_studio import app as app_fixture  # noqa: E402

from kantoku.config import ToolError  # noqa: E402
from kantoku.config.runtime_paths import get_runtime_paths, isolated_runtime  # noqa: E402
from kantoku.core import llm  # noqa: E402
from kantoku.core.runtime.models import ArtifactType, ExecutionStatus  # noqa: E402
from kantoku.shells import web_studio  # noqa: E402


def seed_workspaces(application, patch, paths) -> str:
    """Real scoped records, with explicitly supplied image bytes and offline model adapters."""
    from uuid import uuid4

    from test_comic_stories import _png

    seed_story(application, patch)
    first = application.comic_workspace.create(
        {"request_id": uuid4().hex, "title": "离线验收 · 作品甲"}
    )
    second = application.comic_workspace.create(
        {"request_id": uuid4().hex, "title": "离线验收 · 作品乙"}
    )
    sources = [
        (
            path.name,
            path.read_bytes(),
            "image/jpeg"
            if path.suffix.lower() in {".jpg", ".jpeg"}
            else "image/webp"
            if path.suffix.lower() == ".webp"
            else "image/png",
        )
        for path in paths
    ]
    if not sources:
        sources = [("离线甲.png", _png(20), "image/png"), ("离线乙.png", _png(200), "image/png")]
    selected = []
    for index, parent in enumerate((first, first, second)):
        current = application.comic_workspace.get(parent["work_id"])
        child = application.comic_workspace.create_task(
            parent["work_id"],
            {
                "request_id": uuid4().hex,
                "expected_version": current["version"],
                "title": f"离线画面 {index + 1}",
                "requirement": f"仅用于验证作品归属、图片查看和导出，画面 {index + 1}",
            },
        )
        filename, content, mime = sources[index % len(sources)]

        def body(child=child):
            return {
                "request_id": uuid4().hex,
                "conversation_id": child["conversation_id"],
                "expected_project_version": application.comic_projects.get(
                    child["task_id"]
                ).project.current_version,
            }

        image = application.import_comic_image(
            child["task_id"],
            {
                **body(),
                "filename": filename,
                "data_url": f"data:{mime};base64," + base64.b64encode(content).decode(),
            },
        )
        application.select_comic_image(
            image["image_id"], {**body(), "version": 1, "expected_selection_version": 0}
        )
        if index < 2:
            selected.append(image)

    def request():
        view = application.comic_work_story(first["work_id"])
        return {
            "request_id": uuid4().hex,
            "expected_project_version": application.comic_workspace.get(first["work_id"])[
                "version"
            ],
            "expected_story_version": view["story_version"],
        }

    application.comic_work_story(
        first["work_id"],
        "save",
        {
            **request(),
            "draft": {
                "title": "离线验收故事",
                "summary": "仅包含作品甲的两个画面，不含作品乙。",
                "instruction": "验证固定图片与同作品选图。",
                "shots": [
                    {
                        "shot_id": f"offline-work-shot-{index}",
                        "image": {"image_id": image["image_id"], "version": image["version"]},
                        "action": "画面保持原有构成",
                        "camera": "缓慢推进",
                        "duration_seconds": 5.0,
                        "transition": "自然切换",
                    }
                    for index, image in enumerate(selected)
                ],
            },
        },
    )

    def vision(messages):
        if '"title": "ImageAnalysisOutput"' in messages[0]["content"]:
            return json.dumps(
                {
                    "summary": "离线契约验收：图片分析已完成，未发现明确偏差。",
                    "findings": [],
                    "protected_features": ["保留原图主体"],
                    "limitations": ["离线替身，不证明视觉质量"],
                },
                ensure_ascii=False,
            )
        raise ToolError("离线工作区验收未配置该视觉动作，禁止真实调用")

    patch.setattr(application, "_comic_image_vision_model", vision)
    patch.setattr(
        application,
        "_comic_constraint_model",
        lambda messages: (
            "离线讨论回复：当前画面的方案、图片和归属均保持不变。"
            if "讨论不创建导演方案" in messages[0]["content"]
            else json.dumps({"status": "pass", "findings": []})
        ),
    )

    def understand(messages):
        payload = json.loads(messages[1]["content"])
        text = payload["current_request"]
        action = "chat" if text == "离线验收：只讨论当前构图" else "create"
        return json.dumps(
            {
                "action": action,
                "relation": "continue",
                "goal": text,
                "sources": [],
                "tasks": [],
                "reads": [],
            },
            ensure_ascii=False,
        )

    patch.setattr(application, "_conversation_context_model", understand)
    application.comic_work_story(first["work_id"], "review", request())
    # Leave confirmation for the browser so both actions and their visible states are exercised.
    current = application.comic_workspace.get(first["work_id"])
    return f"/workspace/comic/images?work={first['work_id']}&task={current['tasks'][0]['task_id']}"


def seed_story(application, patch) -> str:
    """Seed real source records and an editable, reviewed story; model answers are offline."""
    from test_comic_stories import _jpeg, _png, _webp

    def prohibited(*_args, **_kwargs):
        raise ToolError("故事浏览器验收禁止任何真实文本、视觉或媒体供应商调用")

    for module in (llm, web_studio):
        patch.setattr(module, "chat", prohibited)
        patch.setattr(module, "vision_chat", prohibited)
    conversation_id = application.create_conversation(
        {
            "domain": "comic",
            "interaction_mode": "guided",
        }
    )["id"]
    application.rename_conversation(conversation_id, "故事编排与导出（离线验收）")
    projects = [
        application.create_comic_project(
            {
                "title": title,
                "brief": {"original_request": "明确离线的图片素材夹具"},
            }
        )["project"]["project_id"]
        for title in ("离线素材作品一", "离线素材作品二")
    ]

    def media_fields(project_id, key):
        return {
            "conversation_id": conversation_id,
            "request_id": key,
            "expected_project_version": application.comic_projects.get(
                project_id
            ).project.current_version,
        }

    samples = [
        (projects[0], "场景.png", "image/png", _png(30)),
        (projects[1], "主体.jpg", "image/jpeg", _jpeg()),
        (projects[1], "结尾.webp", "image/webp", _webp()),
        (projects[0], "未入故事候选.png", "image/png", _png(170)),
    ]
    images = []
    for index, (project_id, filename, mime, content) in enumerate(samples):
        image = application.import_comic_image(
            project_id,
            {
                **media_fields(project_id, f"offline-story-import-{index}"),
                "filename": filename,
                "data_url": f"data:{mime};base64," + base64.b64encode(content).decode("ascii"),
            },
        )
        application.select_comic_image(
            image["image_id"],
            {
                **media_fields(project_id, f"offline-story-select-{index}"),
                "version": 1,
                "expected_selection_version": 0,
            },
        )
        images.append({"image_id": image["image_id"], "version": 1})

    def story_vision(messages):
        payload = json.loads(messages[-1]["content"][0]["text"])
        return json.dumps(
            {
                "title": "离线 AI 故事草稿",
                "summary": "仅验证数据与交互，不评价艺术质量。",
                "instruction": payload["instruction"],
                "shots": [
                    {
                        "shot_id": f"offline-generated-{index}",
                        "image": {"image_id": image["image_id"], "version": image["version"]},
                        "action": "保持当前画面构成，小幅自然动作。",
                        "camera": "固定机位。",
                        "duration_seconds": 4.0,
                        "transition": "自然切换到下一镜。",
                    }
                    for index, image in enumerate(payload["images"], 1)
                ],
            },
            ensure_ascii=False,
        )

    patch.setattr(application, "_comic_image_vision_model", story_vision)
    patch.setattr(
        application,
        "_comic_constraint_model",
        lambda _messages: json.dumps(
            {
                "status": "pass",
                "findings": [],
            }
        ),
    )
    anchor = projects[0]

    def story_fields(key):
        current = application.get_comic_story(conversation_id)["story"]
        return {
            **media_fields(anchor, key),
            "anchor_project_id": anchor,
            "expected_story_version": current["version"] if current else 0,
        }

    application.comic_story_operation(
        conversation_id,
        "save",
        {
            **story_fields("offline-story-save"),
            "draft": {
                "title": "离线示例：三幅画面的连续故事",
                "summary": "已保存且已核验；先编辑并刷新，确认后测试文字与图片材料分别导出。",
                "instruction": "使用已选的三张图片编排连续片段，不将不同主体自动当成同一角色。",
                "shots": [
                    {
                        "shot_id": f"offline-shot-{index}",
                        "image": image,
                        "action": "保持原图构成，画面内小幅自然动作。",
                        "camera": "固定机位。",
                        "duration_seconds": 4.0,
                        "transition": "切到下一张图片。",
                    }
                    for index, image in enumerate(images[:3], 1)
                ],
            },
        },
    )
    application.comic_story_operation(
        conversation_id, "review", story_fields("offline-story-review")
    )
    print(
        "Offline story fixture: 2 projects, 4 adopted images, 3 story shots, unconfirmed.",
        flush=True,
    )
    return conversation_id


def main() -> None:
    parser = argparse.ArgumentParser(description="隔离的真实 HTTP/SQLite 浏览器验收")
    parser.add_argument("--assets", action="store_true", help="只创建离线资产夹具")
    parser.add_argument("--story", action="store_true", help="创建故事编排与导出的离线夹具")
    parser.add_argument("--workspaces", action="store_true", help="验证作品与固定画面工作区")
    parser.add_argument(
        "--workspace-image",
        action="append",
        type=Path,
        default=[],
        help="显式指定浏览器夹具图片，只复制到隔离环境",
    )
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--data-dir", type=Path, help="复用 data/qa 内已有的隔离验收目录")
    options = parser.parse_args()
    if options.data_dir:
        parser.error("本验收脚本只使用自动隔离目录；旧 QA 数据保留供人工审查")
    if sum((options.assets, options.story, options.workspaces)) > 1:
        parser.error("--assets、--story 与 --workspaces 分别用于不同的浏览器验收")
    with isolated_runtime(prefix="kantoku-browser-"), ExitStack() as cleanup:
        _run(options, get_runtime_paths().data, cleanup)


def _run(options, directory: Path, cleanup: ExitStack) -> None:
    print(f"QA temporary data: {directory}", flush=True)
    patch = pytest.MonkeyPatch()
    application = app_fixture.__wrapped__(
        patch,
        directory,
        SimpleNamespace(addfinalizer=cleanup.callback),
    )
    # 原路由测试夹具没有上传配置；媒体只保存到本次隔离目录。
    web_studio.get_settings().image.output_dir = directory / "output"

    def model(messages: list[dict[str, str]]) -> str:
        body = json.loads(messages[1]["content"])
        if "revision_instruction" in body:
            instruction = body["revision_instruction"]
            if "无效回复" in instruction:
                return '{"changes": {"director_plan.visual_strategy": "truncated'
            return json.dumps(
                {
                    "changes": {
                        "director_plan.character_expression": "满怀爱意，直视镜头",
                        "director_plan.composition_strategy": "人物位于右三分线，左侧保留窗边环境",
                        "cinematography.camera_language": "平视，视线交于镜头",
                    }
                },
                ensure_ascii=False,
            )
        if "context" not in body:
            spec = body.get("director_spec", {})
            composition = spec.get("director_plan", {}).get("composition_strategy", "")
            findings = (
                [
                    {
                        "severity": "warning",
                        "code": "COMPOSITION_CAUSALITY",
                        "field_path": "director_plan.composition_strategy",
                        "evidence": composition,
                        "expected": "保留人物视线方向与窗边环境空间",
                        "suggested_action": "人物位于右三分线，左侧保留窗边环境与视线空间",
                    }
                ]
                if body.get("context_diff") and "拥挤" in composition
                else []
            )
            return json.dumps(
                {
                    "public_summary": "离线替身：公开方案契约检查",
                    "confidence": 0.9,
                    "findings": findings,
                    "suggested_patches": [],
                }
            )
        brief = body["context"]["creative_brief"]
        request = body["context"].get("current_task") or brief["original_request"]
        parts = _responses()
        decision = parts[SKILLS[0]]["creative_decision"]
        decision.update(
            intent_summary=request,
            narrative_context=request,
            hard_constraints=brief["hard_constraints"],
            narrative_focus="离线验证：当前人物与环境关系",
            emotional_target="爱意"
            if "爱意" in request
            else "烦恼考试"
            if "考试" in request
            else "平静自然",
        )
        plan = parts[SKILLS[1]]["director_plan"]
        plan.update(
            visual_strategy=f"离线导演方案：{request}",
            visual_focus=request,
            character_presence="持野蓬，成年女性，黑色及肩发，深色上衣、浅色内搭",
            subject_environment_relation="室内摄影棚休息区，窗边椅子",
            style_boundary="超写实电影摄影",
            composition_strategy="人物居中，保留窗边休息区的空间层次",
            color_strategy="低饱和灰蓝背景与自然肤色",
            character_pose="坐在窗边椅子，身体朝向镜头",
            character_expression="含情脉脉，直视镜头"
            if "爱意" in request
            else "平静，目光看向画外",
            continuity_rules=["人物身份、头发与服装保持一致"],
            creative_choices=["离线验收：以人物表情为视觉焦点"],
        )
        parts[SKILLS[2]]["cinematography"].update(
            public_decision="窗边室内电影人像",
            creative_reason="以自然光呈现人物表情",
            spatial_feel="室内休息区",
            shot_size="中近景",
            camera_angle="平视",
            lighting="窗边散射日光",
            light_source="窗户",
            light_direction="左侧",
            color_relationship="低饱和背景与自然肤色",
            material_language="自然皮肤与衣物质感",
        )
        parts[SKILLS[2]]["cinematography"]["camera_language"] = (
            "平视，视线交于镜头" if "爱意" in request else "平视，视线落在画外"
        )
        for skill, schema in zip(
            SKILLS[:3],
            (
                "CreativeDecision",
                "DirectorPlan",
                "CinematographyPlan",
            ),
            strict=True,
        ):
            if f'"title": "{schema}"' in messages[0]["content"]:
                return json.dumps(next(iter(parts[skill].values())), ensure_ascii=False)
        return json.dumps(
            {
                "public_summary": "离线替身：公开方案契约检查",
                "confidence": 0.9,
                "findings": [],
                "suggested_patches": [],
            }
        )

    def prohibited(*_args, **_kwargs):
        raise ToolError("本验收禁止调用图片或外部 Provider")

    def production_model(messages: list[dict[str, str]]) -> str:
        """仅替换模型响应，分镜/Compiler/API/存储继续运行真实实现。"""
        if "character_context" not in messages[0]["content"]:
            return json.dumps(
                {
                    "title": "离线验收分镜",
                    "description": "当前导演画面",
                    "shots": [
                        {
                            "purpose": "电影人像",
                            "subject": "持野蓬",
                            "action": "坐在窗边椅子",
                            "environment": "室内摄影棚休息区",
                        }
                    ],
                },
                ensure_ascii=False,
            )
        context = json.loads(messages[1]["content"])
        director = context["shot_direction"]["director_spec"]
        plan = director["director_plan"]
        camera = director["cinematography"]
        shot = context["current_shot"]
        return json.dumps(
            {
                "director_summary": "离线验收夹具：保持已确认导演方案",
                "character_context": {
                    "identity": "持野蓬",
                    "appearance": "黑色及肩发，成年女性",
                    "age": "",
                    "clothing": "深色上衣、浅色内搭",
                    "fixed_features": "",
                },
                "world_context": {
                    "world_setting": "现实世界",
                    "location": "室内摄影棚休息区",
                    "time": "白天",
                    "environment": "窗户、椅子",
                },
                "style_context": {
                    "art_style": plan["style_boundary"],
                    "photographic_style": "真实摄影质感",
                    "color": plan["color_strategy"],
                    "materials": "皮肤与布料",
                },
                "shot_context": {
                    "action": shot["action"],
                    "pose": plan["character_pose"],
                    "composition": plan["composition_strategy"],
                    "camera": f"{camera['shot_size']}，{camera['camera_angle']}",
                    "lighting_changes": f"{camera['light_source']}，{camera['light_direction']}"
                    f"，{camera['lighting']}",
                },
                "negative_prompt": "不要文字、水印、额外人物或多余肢体",
            },
            ensure_ascii=False,
        )

    patch.setattr(application, "_comic_director_model", model)
    patch.setattr(application, "_comic_storyboard_model", production_model)
    patch.setattr(
        application,
        "_comic_intent_model",
        lambda _: json.dumps(
            {
                "edit_scopes": [],
                "new_creative_direction": False,
                "reason": "offline QA only",
            }
        ),
    )
    patch.setattr(application.image_service.provider, "submit", prohibited)
    patch.setattr(llm, "_build_client", prohibited)
    patch.setattr(web_studio, "chat", prohibited)
    patch.setattr(
        web_studio,
        "stream_chat",
        lambda *_args, **_kwargs: iter(
            [
                "离线聊天验收：这条回复只属于当前对话。",
            ]
        ),
    )
    story_conversation = seed_story(application, patch) if options.story else None
    work_route = (
        seed_workspaces(application, patch, options.workspace_image) if options.workspaces else None
    )
    if options.assets:
        run = application.runtime_store.create_run("studio", "offline.asset-qa", {}, "END")
        application.runtime_store.update_run(
            run.id,
            status=ExecutionStatus.COMPLETED,
            state={"offline_fixture": True},
            current_node="END",
        )
        image = directory / "offline-image.png"
        from kantoku.config.runtime_paths import atomic_write_bytes

        atomic_write_bytes(
            image,
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
                "/x8AAwMCAO+aVk8AAAAASUVORK5CYII=",
            ),
        )
        fixtures = [
            (
                ArtifactType.IMAGE,
                "studio.image",
                {
                    "origin": "real",
                    "filename": "离线测试图片.png",
                },
                str(image),
            ),
            (
                ArtifactType.DOCUMENT,
                "studio.document",
                {
                    "payload": {
                        "title": "离线测试文档",
                        "content": "删除与恢复应保留这段正文。",
                    }
                },
                None,
            ),
            (
                ArtifactType.PROMPT,
                "comic.prompt.compiler",
                {
                    "payload": {
                        "positive_prompt": "离线提示词：雨后城市的窗边橘猫。",
                        "negative_prompt": "不要文字、水印或多余肢体。",
                    }
                },
                None,
            ),
            (
                ArtifactType.REPORT,
                "studio.report",
                {
                    "payload": {
                        "summary": "离线测试报告",
                        "passed": True,
                    }
                },
                None,
            ),
            (
                ArtifactType.IMAGE,
                "commerce.image.mock",
                {
                    "origin": "mock",
                    "schema_name": "commerce.product_image",
                    "payload": {"model": "offline-mock"},
                },
                None,
            ),
        ]
        for kind, source, metadata, location in fixtures:
            application.runtime_store.create_artifact(
                type=kind,
                run_id=run.id,
                node_id="offline-fixture",
                source=source,
                metadata={**metadata, "offline_fixture": True},
                location=location,
            )
    elif (
        not options.story
        and not options.workspaces
        and not application.list_conversations(domain="comic")
    ):
        conversation = application.create_conversation(
            {
                "domain": "comic",
                "interaction_mode": "guided",
            }
        )
        application.rename_conversation(conversation["id"], "人物电影感照片（离线验收）")
        project = application.create_comic_project(
            {
                "title": "离线验收",
                "brief": {"original_request": "帮我生成日本女优持野蓬的超写实电影感照片"},
            }
        )
        application.create_comic_director(
            project["project"]["project_id"],
            {
                "expected_project_version": 1,
                "creation_mode": "professional",
                "conversation_id": conversation["id"],
                "creative_operation": "new",
                "task": "帮我生成日本女优持野蓬的超写实电影感照片",
            },
        )
    server = web_studio.make_server(application, port=options.port)
    route = work_route or (
        f"/workspace/comic/story?conversation={story_conversation}"
        if story_conversation
        else "/assets"
        if options.assets
        else "/workspace/comic"
    )
    print(
        f"Isolated real HTTP/SQLite browser QA: http://127.0.0.1:{server.server_port}{route}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        application.runner.close()
        patch.undo()


if __name__ == "__main__":
    main()
