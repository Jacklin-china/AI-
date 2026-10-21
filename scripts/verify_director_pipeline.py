"""显式付费的导演或动态上下文验收：复用共享 LLM，不生成媒体。"""

from __future__ import annotations

import argparse
import json
import re
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from kantoku.config import ToolError, get_settings
from kantoku.config.logging_setup import setup_logging
from kantoku.config.runtime_paths import (
    atomic_write_text,
    exclusive_write_text,
    get_runtime_paths,
    isolated_runtime,
)
from kantoku.config.settings import ROOT
from kantoku.core.conversations import MessageRole, MessageType
from kantoku.core.llm import chat
from kantoku.domains.comic.critic import require_approved_director
from kantoku.domains.comic.models import DirectorSpecDraft
from kantoku.shells.web_studio import StudioApplication


def verify_semantic_media(app: StudioApplication, report: dict, *,
                          selected_cases: list[str] | None = None, natural: bool = False) -> None:
    """四个不同主题的真实文本链路；样例只存在于验收数据，不进入生产判断。"""
    from kantoku.core.conversation_context import SourceRef
    from kantoku.domains.comic.constraints import ConstraintSource, normalize_constraints
    from kantoku.domains.comic.models import ComicProjectInput, ComicShotDraft, ComicStoryboardDraft
    from kantoku.domains.comic.prompts import compiler_for_model

    cases = [
        ("portrait", "做一张成年女性的写实肖像，短卷发，安静的神情，衣服不要红色"),
        ("mascot", "画一只圆滚滚的机械水獭吉祥物，用扁平剪纸风格，拿着透明的玻璃杯"),
        ("architecture", "制作一张沙漠里废弃天文台的画面，黄昏，地上只有一辆自行车，不出现人物"),
        ("lettering", "画一个雨后的咖啡馆门口，招牌上的文字必须原样写 OPEN 24/7，不出现人物"),
    ]
    cases = [(key, value) for key, value in cases if not selected_cases or key in selected_cases]
    report["expected_cases"] = len(cases)
    for identifier, request in cases:
        conversation = app.create_conversation({"interaction_mode": "guided", "domain": "comic"})
        cid = conversation["id"]
        message = None
        if not natural:
            message = app.runtime_store.add_conversation_message(cid, role=MessageRole.USER,
                type=MessageType.TEXT, content=request)
        item = {"id": identifier, "request": request, "conversation_id": cid}
        report["cases"].append(item)
        if natural:
            created = app.create_comic_project({"title": request,
                                                "brief": {"original_request": request}})
            pid = created["project"]["project_id"]
            item["project_id"] = pid
            result = app.create_comic_director(pid, {"expected_project_version": 1,
                "creation_mode": "professional", "conversation_id": cid,
                "task": request, "independent_plan": True, "request_id": f"qa-{uuid4().hex}"})
            item.update(run_id=result["run_id"], status=result["status"],
                        context=result.get("context"))
            if result["status"] != "completed":
                raise ToolError("自然入口未通过导演审核，保留原结果，不继续编译")
            pid = result["director_spec"]["project_id"]
            snapshot = app.comic_projects.get(pid)
            constraints = snapshot.creative_brief.semantic_constraints
            item.update(project_id=pid,
                        constraints=[c.model_dump(mode="json") for c in constraints])
            run_record = app.runtime_store.get_run(result["run_id"])
            item["trace_id"] = run_record.state.get("trace_id")
            source_id = run_record.state["creative_context"]["input_message_id"]
            message = app.runtime_store.list_conversation_messages(cid, message_ids=[source_id])[0]
        else:
            constraints, evidence = normalize_constraints([ConstraintSource(value=request,
            source=SourceRef(id=message.id, kind="message"))], previous=[],
            model_call=app._comic_constraint_model,
            on_diagnostics=lambda audit, entry=item: entry.update(normalization=audit))
            snapshot = app.comic_projects.create(ComicProjectInput.model_validate({"title": request,
            "brief": {"original_request": request,
                "hard_constraints": list(dict.fromkeys(c.requirement for c in constraints)),
                "constraint_contract_version": 2,
                "semantic_constraints": constraints, "constraint_evidence": evidence}}))
            pid = snapshot.project.project_id
            item.update(project_id=pid,
                        constraints=[c.model_dump(mode="json") for c in constraints])
            app.create_comic_director(pid, {"expected_project_version": 1, "task": request,
                                       "conversation_id": cid})
        director = app.comic_projects.get_director(pid)
        if natural:
            # 仅隔离 QA 数据模拟用户确认步骤；不越过 warning/blocked/unavailable 审核。
            require_approved_director(director)
            app.confirm_comic_director(pid, {"expected_project_version":
                app.comic_projects.get(pid).project.current_version, "version": director.version})
            item["confirmation"] = "qa_fixture_after_actual_critic_pass"
        board = app.comic_storyboards.create(pid, ComicStoryboardDraft(title="单图语义验收"),
            expected_project_version=app.comic_projects.get(pid).project.current_version,
            director_spec_version=director.version)
        shot = app.comic_storyboards.add_shot(board.storyboard_id, ComicShotDraft(purpose=request,
            subject=director.character_focus, environment=director.visual_direction),
            expected_project_version=app.comic_projects.get(pid).project.current_version,
            expected_storyboard_version=board.version)
        snapshot, director, board, shot, assets = app.comic_prompts.source(shot.shot_id)
        compiler = compiler_for_model("external")
        draft = compiler.compile(snapshot=snapshot, director=director, storyboard=board, shot=shot,
            assets=assets, model_target="external", model_call=app._comic_constraint_model,
            complete_prompt=True,
            on_constraint_diagnostics=lambda audit, entry=item: entry.update(prompt_review=audit))
        run = app.runtime_store.create_run("comic", "comic.prompt.compile",
            {"conversation_id": cid, "project_id": pid}, "prompt.compile")
        saved = app.comic_prompts.save(shot.shot_id, draft,
            expected_project_version=snapshot.project.current_version,
            expected_shot_version=1, model_target="external", compiler_version=compiler.version,
            run_id=run.id)
        restored = app.comic_prompts.get(shot.shot_id, version=saved.version)
        if restored != saved or not all(c.source_ref.id == message.id
            and c.source_quote in message.content for c in constraints):
            raise ToolError("真实语义来源或持久化回读不一致")
        item.update(prompt_artifact_id=saved.artifact_id, prompt_version=saved.version,
                    prompt=restored.model_dump(mode="json"), status="verified", passed=True)


def verify_context(app: StudioApplication, report: dict) -> None:
    """Real semantic checks over synthetic QA history; never fake a delivered asset."""
    from kantoku.shells.conversation_router import conversation_memory

    settings = get_settings().runtime
    conversation = app.create_conversation({"interaction_mode": "guided", "domain": "comic"})
    cid = conversation["id"]
    other = app.create_conversation({"interaction_mode": "guided", "domain": "comic"})
    app.runtime_store.add_conversation_message(other["id"], role=MessageRole.USER,
        type=MessageType.TEXT, content="另一聊天的专属角色：隔离紫龙")
    initial = []
    for text in ("最早的角色名叫阿青，衣服原定红色。",
                 "纠正阿青的服装：蓝色衣服；红色要求已经撤回。"):
        initial.append(app.runtime_store.add_conversation_message(cid, role=MessageRole.USER,
            type=MessageType.TEXT, content=text).id)
    for index in range(110):
        app.runtime_store.add_conversation_message(cid, role=MessageRole.USER,
            type=MessageType.TEXT, content=f"历史话题 {index % 5} 第 {index} 次记录：" +
            "讨论本话题的目标、进度和待完成事项；这些是独立的历史讨论。" * 5)
    report.update(seeded_messages=112, real_context_turns=0,
                  context_budget=settings.context_token_budget,
                  acceptance_note="历史为人工构造 QA 数据；本轮理解、压缩和召回使用真实模型。")
    suite = [
        ("subject", "给我生成一个jojo风格的大耳朵图图里面的牛爷爷",
         {"new"}, {"create", "image.generate"}, "牛爷爷", ()),
        ("pronoun", "他拿起手机打电话", {"continue", "revise"},
         {"create", "image.generate", "image.edit"}, "牛爷爷", ()),
        ("switch", "我想要一张欧美清纯少女的图片", {"new"},
         {"create", "image.generate"}, "少女", ("牛爷爷", "jojo")),
        ("correction", "她的裙子改为蓝色，其他先保留", {"continue", "revise"},
         {"create", "image.generate", "image.edit"}, "蓝色", ("牛爷爷",)),
        ("comparison", "比较牛爷爷和少女两个方案的风格", {"compare", "recall"},
         {"chat"}, "", ()),
        ("partial", "回到牛爷爷方案，仅沿用人物身份，风格改成写实",
         {"recall", "revise", "continue"}, {"create", "image.generate"}, "写实", ("少女",)),
        ("early", "最早讨论的阿青现在衣服是什么颜色？请核对纠正记录。",
         {"recall", "continue"}, {"chat"}, "", ()),
        ("ambiguity", "把刚才比较的两个方案中的一个人物换掉",
         {"revise", "continue", "compare"}, {"clarify"}, "", ()),
    ]
    report["expected_cases"] = len(suite)
    for identifier, request, relations, actions, required, forbidden in suite:
        item = {"id": identifier, "request": request, "conversation_id": cid}
        report["cases"].append(item)
        incoming = app.runtime_store.add_conversation_message(cid, role=MessageRole.USER,
            type=MessageType.TEXT, content=request)
        prepared = app._prepare_conversation_context(cid, request,
                                                     through_message_id=incoming.id)
        resolution = prepared.understanding
        report["real_context_turns"] += 1
        item.update(context=prepared.public(), status="resolved",
                    context_input=json.loads(prepared.messages[0]["content"].split("\n", 1)[1]))
        actual = json.dumps(prepared.messages, ensure_ascii=False)
        if "隔离紫龙" in actual:
            raise ToolError("真实上下文发生跨聊天污染")
        if (resolution.action not in actions or resolution.relation not in relations
                or required not in resolution.goal
                or any(term.casefold() in resolution.goal.casefold() for term in forbidden)):
            raise ToolError("真实上下文语义判定未通过，需按公开来源分析")
        if identifier == "early":
            relevant = json.dumps({"summary": prepared.summary_id,
                                  "recalled": prepared.evidence,
                                  "resolution": resolution.model_dump()}, ensure_ascii=False)
            if "蓝色衣服" not in relevant or not any(key in prepared.evidence for key in initial):
                raise ToolError("早期用户纠正未重新读取核验")
        conversation_memory(app.runtime_store, settings).remember(cid, prepared, incoming.id)
        item["passed"] = True


def acceptance_image_input(path):
    """两个图片相关验收套件使用同一真实文件边界。"""
    import base64

    suffix = path.suffix.lower()
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp"}.get(suffix)
    if mime is None or not path.is_file() or not 0 < path.stat().st_size <= 5 * 1024 * 1024:
        raise ToolError("验收图片必须为 5 MB 内的 PNG/JPEG/WebP")
    return {"filename": path.name, "data_url": f"data:{mime};base64,"
            + base64.b64encode(path.read_bytes()).decode()}


def verify_image_review(app, report, args):
    """显式输入真实图片；只检查后端闭环，艺术判断仍由用户完成。"""

    cid = app.create_conversation({"interaction_mode": "guided", "domain": "comic"})["id"]
    pid = app.create_comic_project({"title": "图片闭环工程验收",
        "brief": {"original_request": args.task}})["project"]["project_id"]
    def common(request_id):
        return {"conversation_id": cid, "request_id": request_id,
                "expected_project_version": app.comic_projects.get(pid).project.current_version}
    item = {"id": "image-review", "project_id": pid, "conversation_id": cid}
    report["cases"].append(item)
    imported = app.import_comic_image(pid, {**common("import"),
        **acceptance_image_input(args.image)})
    image = {"image_id": imported["image_id"], "version": 1}
    analysis = app.comic_image_operation(pid, "analysis", {**common("analysis"),
        "image": image, "task": args.task})
    item["analysis"] = analysis
    edited = app.comic_image_operation(pid, "edit", {**common("edit"), "image": image,
        "task": args.task, "analysis_artifact_id": analysis["artifact_id"],
        "selected_finding_ids": args.finding_id or [], "instruction": args.edit_request})
    item["edit_instructions"] = edited
    app.comic_image_operation(pid, "confirm", {**common("confirm"),
        "instruction_artifact_id": edited["artifact_id"]})
    item["confirmation"] = "explicit_paid_acceptance_fixture_not_production_approval"
    if args.edited_image:
        after = app.import_comic_image(pid, {**common("import-after"),
            **acceptance_image_input(args.edited_image), "image_id": image["image_id"],
            "expected_image_version": 1, "parent": image,
            "instruction_artifact_id": edited["artifact_id"]})
        item["comparison"] = app.comic_image_operation(pid, "comparison", {**common("compare"),
            "before": image, "after": {"image_id": after["image_id"], "version": after["version"]},
            "instruction_artifact_id": edited["artifact_id"]})
    item.update(passed=True, status="completed", run_id=edited["run_id"],
                visual_acceptance="pending_human", comparison_exercised=bool(args.edited_image))


def verify_story_export(app, report, args):
    """真实图像生成故事及语义核验；确认只代表隔离验收夹具。"""
    import hashlib
    import zipfile

    cid = app.create_conversation({"interaction_mode": "guided", "domain": "comic"})["id"]
    images, originals, projects = [], {}, []
    for index, path in enumerate([args.image, *(args.additional_image or [])]):
        pid = app.create_comic_project({"title": f"故事验收素材 {index + 1}",
            "brief": {"original_request": "隔离工程验收素材，不属于生产作品"}})[
                "project"]["project_id"]
        projects.append(pid)
        def common(request_id, pid=pid):
            return {"conversation_id": cid, "request_id": request_id,
                "expected_project_version": app.comic_projects.get(pid).project.current_version}
        imported = app.import_comic_image(pid, {**common(f"import-{index}"),
            **acceptance_image_input(path)})
        image = {"image_id": imported["image_id"], "version": imported["version"]}
        app.select_comic_image(image["image_id"], {**common(f"select-{index}"),
            "version": image["version"], "expected_selection_version": 0})
        images.append(image)
        originals[imported["artifact_id"]] = hashlib.sha256(path.read_bytes()).hexdigest()
    item = {"id": "story-export", "conversation_id": cid, "source_projects": projects}
    report["cases"].append(item)
    def story_request(request_id, version):
        return {"conversation_id": cid, "request_id": request_id,
            "anchor_project_id": projects[0], "expected_story_version": version,
            "expected_project_version": app.comic_projects.get(projects[0]).project.current_version}
    generated = app.comic_story_operation(cid, "generate", {**story_request("generate", 0),
        "instruction": args.task, "images": images})
    item["generated"] = generated
    if generated["review"]["status"] != "pass":
        raise ToolError("真实故事仍有待处理核验意见，未模拟确认或继续导出")
    version = generated["story"]["version"]
    confirmed = app.comic_story_operation(cid, "confirm", story_request("confirm", version))
    item["confirmation"] = "qa_fixture_after_actual_story_review_not_production_approval"
    exported = app.comic_story_operation(cid, "exports", story_request("export", version))
    item["export"] = exported
    artifact = app.runtime_store.get_artifact(exported["export_artifact_id"])
    with zipfile.ZipFile(artifact.location) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        if bundle.read("story.md").decode("utf-8") != confirmed["export_text"]:
            raise ToolError("导出文字与确认的故事版本不一致")
        for image in manifest["images"]:
            if hashlib.sha256(bundle.read(image["filename"])).hexdigest() != originals[
                    image["artifact_id"]]:
                raise ToolError("真实导出图片与用户输入字节不一致")
    item.update(passed=True, status="completed", visual_acceptance="pending_human")


def _run(persistent_manager, cleanup) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--max-cny", type=Decimal, default=Decimal("2"))
    parser.add_argument("--suite",
                        choices=("director", "context", "semantic-media", "image-review",
                                 "story-export"),
                        default="director")
    parser.add_argument("--budget-id", help="多模块共用累计额度身份；不得换 ID 绕过额度")
    parser.add_argument("--case", action="append",
                        choices=("portrait", "mascot", "architecture", "lettering"))
    parser.add_argument("--natural", action="store_true", help="验证真实专业自然入口与记忆解析")
    parser.add_argument("--image", type=Path, help="图片闭环验收的真实原图")
    parser.add_argument("--additional-image", type=Path, action="append",
                        help="故事验收追加的明确图片；连同 --image 最多八张")
    parser.add_argument("--edited-image", type=Path, help="可选的真实外部修改结果")
    parser.add_argument("--task", help="这张图片的实际任务")
    parser.add_argument("--edit-request", help="本次改图原文；不默选模型发现项")
    parser.add_argument("--finding-id", action="append", help="显式选中的报告发现项")
    args = parser.parse_args()
    if (args.suite == "story-export"
            and args.budget_id != "comic-backend-three-modules-20261012"):
        parser.error("故事模块实测必须复用原三模块累计预算身份")
    if not args.allow_paid or not Decimal("0") < args.max_cny <= Decimal("2"):
        parser.error("必须显式允许付费，且额度在 (0, 2] 元内")
    settings = get_settings()
    price = settings.llm.pricing_cny_per_million_by_model.get(settings.llm.model_chat)
    if price is None:
        parser.error("当前文本模型缺少配置的保守单价，禁止未知费用实测")
    vision_price = settings.llm.pricing_cny_per_million_by_model.get(settings.llm.model_vision)
    if args.suite in {"image-review", "story-export"}:
        if not args.image or not args.image.is_file() or not args.task:
            parser.error("图片相关验收必须指定 --image、--task")
        if args.suite == "image-review" and not args.edit_request:
            parser.error("图片验收必须指定 --image、--task、--edit-request")
        if len(args.additional_image or []) > 7:
            parser.error("故事验收最多八张图片")
        if vision_price is None:
            parser.error("视觉模型未定价，禁止未知费用实测")
    # 仅修改本进程的存储位置；模型/端点/密钥/输出上限仍读取 settings.yaml。
    manager = persistent_manager
    budget_catalog = manager.catalog  # 保留锁登记时的句柄，后续切库仍由它释放锁。
    ledger_path, budget_lock, prior_spend = None, None, Decimal("0")
    if args.suite in {"semantic-media", "image-review", "story-export"} or args.budget_id:
        if not args.budget_id or not re.fullmatch(r"[a-z0-9-]{1,64}", args.budget_id):
            parser.error("模块验收必须指定稳定的 --budget-id（小写字母、数字、短横线）")
        ledger_path = manager.data / "paid-qa" / f"budget-{args.budget_id}.json"
        if args.suite in {"image-review", "story-export"} and not ledger_path.is_file():
            parser.error("累计费用账本缺失，禁止把已进行的多模块验收重新从零计费")
        budget_lock = ledger_path.with_suffix(".lock")
        try:
            exclusive_write_text(budget_lock, "paid-acceptance", file_type="audit_lock")
        except FileExistsError:
            parser.error("相同验收额度已有任务运行或未释放锁；核对原任务状态，禁止并行付费")
        def release_lock():
            if budget_lock.exists():
                budget_catalog.remove_file(budget_lock, "audit_lock")
        cleanup.callback(release_lock)
        if ledger_path.exists():
            try:
                ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                if (ledger["budget_id"] != args.budget_id
                        or Decimal(ledger["limit_cny"]) != args.max_cny):
                    raise ValueError("budget identity mismatch")
                prior_spend = Decimal(ledger["used_cny"])
                if not prior_spend.is_finite() or prior_spend < 0:
                    raise ValueError("invalid spend")
            except (ValueError, KeyError) as error:
                budget_catalog.remove_file(budget_lock, "audit_lock")
                raise ToolError("累计验收费用记录无效，禁止覆盖或重新付费") from error
    folder = get_runtime_paths().root
    settings.storage.sqlite_path = folder / "runtime.db"
    setup_logging("INFO")
    app = StudioApplication()
    spend = Decimal("0")
    calls: list[dict] = []

    def persist_budget() -> None:
        if ledger_path:
            atomic_write_text(ledger_path, json.dumps({"budget_id": args.budget_id,
                "limit_cny": str(args.max_cny), "used_cny": str(prior_spend + spend),
                "last_report": None, "last_suite": args.suite, "call_count": len(calls),
                "billing_note": "未知用量保留调用上界；按配置单价保守估算"}, ensure_ascii=False),
                file_type="audit", owner="paid-acceptance")

    def model(messages: list[dict], *, vision=False) -> str:
        nonlocal spend
        selected_price = vision_price if vision else price
        model_name = settings.llm.model_vision if vision else settings.llm.model_chat
        maximum = settings.llm.vision_max_tokens if vision else settings.llm.max_tokens
        # UTF-8 字节数 + 消息协议余量作为输入 token 保守上界；不假定缓存命中。
        upper_input = len(json.dumps(messages, ensure_ascii=False).encode()) + 2048
        upper_cost = (upper_input * selected_price.input_cny
                      + maximum * selected_price.output_cny) / 1_000_000
        if prior_spend + spend + upper_cost > args.max_cny:
            raise ToolError("真实导演验收剩余额度不足，未提交下一调用")
        record = {"model": model_name, "upper_cny": str(upper_cost),
                  "usage_reported": False}
        if args.suite == "context":
            from kantoku.core.conversation_context import token_size

            record.update(input_messages=json.loads(json.dumps(messages)),
                          estimated_input_tokens=token_size(messages))
        calls.append(record)
        spend += upper_cost  # 结果未知保留整个上界，不自动重试或降级。
        persist_budget()

        def usage(incoming: int, outgoing: int, reported: bool) -> None:
            nonlocal spend
            if not reported:
                raise ToolError("供应商用量未知，停止后续实测并保留预估额度")
            actual = (incoming * selected_price.input_cny
                      + outgoing * selected_price.output_cny) / 1_000_000
            spend += actual - upper_cost
            persist_budget()
            record.update(input_tokens=incoming, output_tokens=outgoing,
                          estimated_cny=str(actual), usage_reported=True)

        from kantoku.core.llm import vision_chat

        response = (vision_chat if vision else chat)(messages,
                        response_format={"type": "json_object"},
                        single_attempt=True, on_usage=usage)
        if args.suite == "context":
            from kantoku.core.conversation_context import (
                ConversationContext,
                ConversationSummary,
                TurnUnderstanding,
            )

            payload = json.loads(messages[1]["content"])
            schema = ConversationSummary if "previous_summary" in payload else TurnUnderstanding
            record["public_output"] = ConversationContext._parse(
                schema, response.content or "").model_dump(mode="json")
        return response.content or ""

    app._comic_director_model = model
    app._comic_image_vision_model = lambda messages: model(messages, vision=True)
    report: dict = {"model": settings.llm.model_chat, "limit_cny": str(args.max_cny),
                    "pricing": price.model_dump(mode="json"), "cases": [], "calls": calls,
                    "suite": args.suite, "expected_cases": 4,
                    "billing_note": "按配置高峰未缓存单价估算；供应商账单为最终事实"}
    suite = [json.loads(line) for line in (ROOT / "evals" / "director.jsonl").read_text(
        encoding="utf-8",
    ).splitlines() if line.strip()]
    strategies: set[str] = set()
    try:
        if args.suite == "context":
            verify_context(app, report)
        elif args.suite == "semantic-media":
            verify_semantic_media(app, report, selected_cases=args.case, natural=args.natural)
        elif args.suite == "image-review":
            report["expected_cases"] = 1
            verify_image_review(app, report, args)
        elif args.suite == "story-export":
            report["expected_cases"] = 1
            verify_story_export(app, report, args)
        for case in suite if args.suite == "director" else []:
            project = app.create_comic_project({"title": case["input"], "brief": {
                "original_request": case["input"], "hard_constraints": case["constraints"],
            }})
            project_id = project["project"]["project_id"]
            conversation = app.create_conversation({"interaction_mode": "guided",
                                                    "domain": "comic"})
            item = {"id": case["id"], "request": case["input"], "project_id": project_id,
                    "conversation_id": conversation["id"]}
            report["cases"].append(item)
            result = app.create_comic_director(project_id, {
                "expected_project_version": 1, "creation_mode": case["mode"],
                "task": case["input"], "conversation_id": conversation["id"],
            })
            run_record = app.runtime_store.get_run(result["run_id"])
            item.update(run_id=run_record.id, trace_id=run_record.state["trace_id"],
                        status=result["status"], debug=run_record.state["director_debug"],
                        critic=run_record.state.get("critic_result"))
            if result["status"] != "completed":
                raise ToolError("真实导演未通过审核，保留候选，不继续后续流程")
            raw = result["director_spec"]
            spec = DirectorSpecDraft.model_validate({key: raw[key] for key
                                                   in DirectorSpecDraft.model_fields if key in raw})
            require_approved_director(spec)
            item["director_spec"] = spec.model_dump(mode="json")
            text = json.dumps(spec.model_dump(), ensure_ascii=False)
            if any(term in text for term in case["forbidden"]):
                raise ToolError("真实导演出现其他案例主体污染")
            strategy = spec.director_plan.visual_strategy
            if strategy in strategies:
                raise ToolError("不同创意返回相同视觉策略，需人工分析模板化")
            strategies.add(strategy)
            item["passed"] = True
    except Exception as error:
        from kantoku.config.observability import public_error

        report["failure"] = getattr(error, "_kantoku_public_failure", None) or public_error(
            error, component="paid_acceptance")
        report["error_type"] = type(error).__name__
        report["error"] = "实测未全部通过，请按 Run/Trace 检查；未自动重试"
        if report["cases"]:
            item = report["cases"][-1]
            failed = next((record for record in app.runtime_store.list_runs(domain="comic")
                           if record.state.get("project_id") == item.get("project_id")), None)
            if failed is not None:
                item.update(run_id=failed.id, trace_id=failed.state.get("trace_id"),
                            status=failed.status.value, error_id=failed.state.get("error_id"),
                            debug=failed.state.get("director_debug"),
                            critic=failed.state.get("critic_result"))
        # Provider 原始异常不进入报告；安全 error_id/traceback 已由 Coordinator 记录。
    finally:
        report["estimated_total_cny"] = str(spend)
        report["cumulative_cny"] = str(prior_spend + spend)
        report["budget_id"] = args.budget_id
        report["passed"] = len(report["cases"]) == report["expected_cases"] and all(
            item.get("passed") for item in report["cases"]
        )
        app.runner.close()
        get_runtime_paths().close_staging()
        get_runtime_paths().catalog.close_databases(folder)
        from loguru import logger

        logger.remove()
        if budget_lock:
            budget_catalog.remove_file(budget_lock, "audit_lock")
        print(json.dumps({"passed": report["passed"], "estimated_cny": str(spend),
                          "cumulative_cny": str(prior_spend + spend),
                          "cases": [{key: item.get(key) for key in
                                     ("id", "status", "passed", "run_id", "trace_id")}
                                    for item in report["cases"]],
                          "report": report, "test_files_retained": False}, ensure_ascii=False))
    return 0 if report["passed"] else 1


def run() -> int:
    persistent_manager = get_runtime_paths()
    with isolated_runtime(prefix="kantoku-paid-acceptance-"), ExitStack() as cleanup:
        return _run(persistent_manager, cleanup)


if __name__ == "__main__":
    raise SystemExit(run())
