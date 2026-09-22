"""实际运行 Core v0.2 与 Commerce v0.1 的端到端验收。"""

from __future__ import annotations

import argparse
import json
import tempfile
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from kantoku.adapters.commerce import (
    MockMarketplaceAdapter,
    MockSourceAdapter,
    MockTranslationAdapter,
)
from kantoku.capabilities.video import (
    MockVideoProvider,
    VideoGenerationRequest,
    VideoService,
)
from kantoku.config import BudgetError
from kantoku.config.settings import ROOT, VideoSettings
from kantoku.core import budget
from kantoku.core.approval import ApprovalService
from kantoku.core.runtime import BatchService
from kantoku.core.runtime.graph import END, GraphRuntime
from kantoku.core.runtime.models import (
    ApprovalDecision,
    ArtifactType,
    BatchStatus,
    ExecutionStatus,
)
from kantoku.core.runtime.store import RuntimeStore
from kantoku.core.skills import SkillLoader, SkillRegistry
from kantoku.domains.commerce import CommerceState, build_commerce_workflow
from kantoku.tools.image_gen import LocalFakeImageProvider, gen_image

CheckDetail = dict[str, Any]


def _runtime(
    store: RuntimeStore,
    *,
    qc_checker: Callable[[CommerceState], bool] | None = None,
) -> tuple[GraphRuntime, str]:
    skills = SkillRegistry()
    SkillLoader(ROOT / "skills", project_root=ROOT).load(skills)
    runtime = GraphRuntime(store)
    workflow = build_commerce_workflow(
        MockSourceAdapter(),
        MockMarketplaceAdapter(),
        skills,
        MockTranslationAdapter(),
        qc_checker=qc_checker,
    )
    runtime.register(workflow)
    return runtime, workflow.id


def _budget_settings(path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        budget=SimpleNamespace(
            accounting_utc_offset_hours=8,
            image_credit_cny=Decimal("0.10"),
            image_estimated_credits_per_call=1,
            image_daily_cny=Decimal("20.00"),
            image_project_cny=Decimal("20.00"),
            image_episode_cny=Decimal("20.00"),
            image_shot_cny=Decimal("1.00"),
            image_max_concurrency=1,
            token_daily_limit=200000,
        ),
        storage=SimpleNamespace(sqlite_path=path),
    )


class Verifier:
    """收集可机器读取的实际验收证据，并在首个失败点停止。"""

    def __init__(self) -> None:
        self.checks: list[CheckDetail] = []

    def confirm(self, name: str, condition: bool, **detail: Any) -> None:
        if not condition:
            raise AssertionError(f"{name}: {detail}")
        self.checks.append({"name": name, "status": "passed", **detail})

    def commerce(self, directory: Path) -> dict[str, Any]:
        database = directory / "commerce.db"
        first_store = RuntimeStore(database)
        first_runtime, workflow_id = _runtime(first_store)
        waiting = first_runtime.start(
            workflow_id,
            CommerceState(requirement="便携阅读灯", locale="ru-RU"),
        )
        self.confirm(
            "candidate approval pauses",
            waiting.status is ExecutionStatus.WAITING
            and waiting.current_node == "candidate_approval",
            run_id=waiting.id,
        )

        restarted_store = RuntimeStore(database)
        restarted_runtime, _ = _runtime(restarted_store)
        candidate = restarted_store.approval_for_node(waiting.id, "candidate_approval")
        assert candidate is not None
        selected_id = str(candidate.request["candidates"][1]["id"])
        after_candidate = ApprovalService(
            restarted_store, restarted_runtime
        ).decide_and_resume(
            candidate.id,
            ApprovalDecision.APPROVE,
            {"candidate_id": selected_id},
        )
        middle_nodes = {
            "candidate_approval",
            "sku_selection",
            "pricing",
            "listing_draft",
            "localize",
            "asset_generation",
            "qc",
        }
        completed_nodes = {
            item.node_id
            for item in restarted_store.list_nodes(waiting.id)
            if item.status is ExecutionStatus.COMPLETED
        }
        self.confirm(
            "candidate approve resumes through commerce nodes",
            after_candidate.current_node == "publish_approval"
            and after_candidate.status is ExecutionStatus.WAITING
            and middle_nodes <= completed_nodes
            and after_candidate.state["selected_candidate_id"] == selected_id,
            completed_nodes=sorted(completed_nodes),
            selected_candidate_id=selected_id,
        )
        self.confirm(
            "publish approval pauses",
            after_candidate.current_node == "publish_approval",
            approval_count=len(restarted_store.list_approvals(pending_only=True)),
        )

        final_store = RuntimeStore(database)
        final_runtime, _ = _runtime(final_store)
        publish = final_store.approval_for_node(waiting.id, "publish_approval")
        assert publish is not None
        completed = ApprovalService(final_store, final_runtime).decide_and_resume(
            publish.id, ApprovalDecision.APPROVE
        )
        sources = {artifact.source for artifact in final_store.list_artifacts(waiting.id)}
        required_sources = {
            "commerce.requirement",
            "commerce.source.mock",
            "commerce.analysis",
            "commerce.sku",
            "commerce.pricing",
            "commerce.listing",
            "commerce.localization",
            "commerce.image.mock",
            "commerce.qc.mock",
            "commerce.marketplace.mock",
            "commerce.publish.mock",
        }
        self.confirm(
            "second approve reaches END",
            completed.status is ExecutionStatus.COMPLETED
            and completed.current_node == END
            and completed.state["marketplace_draft"]["status"] == "submitted",
            marketplace_status=completed.state["marketplace_draft"]["status"],
        )
        self.confirm(
            "all important artifacts persist",
            required_sources <= sources,
            artifact_count=len(final_store.list_artifacts(waiting.id)),
            sources=sorted(sources),
        )
        self.confirm(
            "waiting restart resumes from checkpoint",
            final_store.latest_checkpoint(waiting.id) is not None,
            database=str(database),
        )

        return {
            "run_id": waiting.id,
            "status": completed.status.value,
            "current_node": completed.current_node,
            "selected_candidate_id": completed.state["selected_candidate_id"],
            "artifact_count": len(final_store.list_artifacts(waiting.id)),
            "database": str(database),
        }

    def routes_and_limits(self, directory: Path) -> dict[str, Any]:
        store = RuntimeStore(directory / "routes.db")
        runtime, workflow_id = _runtime(store)
        approvals = ApprovalService(store, runtime)

        revised = runtime.start(workflow_id, CommerceState(requirement="候选修改"))
        first = store.approval_for_node(revised.id, "candidate_approval")
        assert first is not None
        revised = approvals.decide_and_resume(first.id, ApprovalDecision.REQUEST_REVISION)
        self.confirm(
            "candidate revise route",
            revised.current_node == "candidate_approval"
            and revised.state["candidate_revision"] == 1,
            run_id=revised.id,
        )
        second = store.approval_for_node(revised.id, "candidate_approval")
        assert second is not None
        revised = approvals.decide_and_resume(second.id, ApprovalDecision.APPROVE)
        publish = store.approval_for_node(revised.id, "publish_approval")
        assert publish is not None
        revised = approvals.decide_and_resume(
            publish.id, ApprovalDecision.REQUEST_REVISION
        )
        self.confirm(
            "publish revise route",
            revised.current_node == "publish_approval"
            and revised.state["publish_revision"] == 1,
            run_id=revised.id,
        )
        publish_again = store.approval_for_node(revised.id, "publish_approval")
        assert publish_again is not None
        revised = approvals.decide_and_resume(
            publish_again.id, ApprovalDecision.APPROVE
        )

        rejected = runtime.start(workflow_id, CommerceState(requirement="候选拒绝"))
        candidate = store.approval_for_node(rejected.id, "candidate_approval")
        assert candidate is not None
        rejected = approvals.decide_and_resume(candidate.id, ApprovalDecision.REJECT)

        publish_rejected = runtime.start(
            workflow_id, CommerceState(requirement="发布拒绝")
        )
        candidate = store.approval_for_node(publish_rejected.id, "candidate_approval")
        assert candidate is not None
        publish_rejected = approvals.decide_and_resume(
            candidate.id, ApprovalDecision.APPROVE
        )
        publish = store.approval_for_node(publish_rejected.id, "publish_approval")
        assert publish is not None
        publish_rejected = approvals.decide_and_resume(
            publish.id, ApprovalDecision.REJECT
        )
        self.confirm(
            "reject routes",
            rejected.status is ExecutionStatus.COMPLETED
            and rejected.state["selected_sku"] is None
            and publish_rejected.status is ExecutionStatus.COMPLETED
            and publish_rejected.state["marketplace_draft"] is None,
            candidate_reject_run=rejected.id,
            publish_reject_run=publish_rejected.id,
        )

        limit_store = RuntimeStore(directory / "rework.db")
        limit_runtime, limit_workflow = _runtime(
            limit_store, qc_checker=lambda _state: False
        )
        limited = limit_runtime.start(
            limit_workflow,
            CommerceState(requirement="返工上限", max_reworks=1),
        )
        candidate = limit_store.approval_for_node(limited.id, "candidate_approval")
        assert candidate is not None
        limited = ApprovalService(limit_store, limit_runtime).decide_and_resume(
            candidate.id, ApprovalDecision.APPROVE
        )
        images = [
            item
            for item in limit_store.list_artifacts(limited.id)
            if item.source == "commerce.image.mock"
        ]
        self.confirm(
            "max reworks stops loop",
            limited.status is ExecutionStatus.COMPLETED
            and limited.state["rework_count"] == 1
            and len(images) == 2,
            run_id=limited.id,
            rework_count=limited.state["rework_count"],
        )
        return {
            "revised_run": revised.id,
            "candidate_reject_run": rejected.id,
            "publish_reject_run": publish_rejected.id,
            "max_reworks_run": limited.id,
        }

    def cancellation_and_batches(self, directory: Path) -> dict[str, Any]:
        store = RuntimeStore(directory / "batches.db")
        runtime, workflow_id = _runtime(store)
        run = runtime.start(workflow_id, CommerceState(requirement="取消单任务"))
        cancelled = runtime.cancel(run.id)
        self.confirm(
            "run cancel",
            cancelled.status is ExecutionStatus.CANCELLED
            and runtime.resume(run.id).status is ExecutionStatus.CANCELLED,
            run_id=run.id,
        )

        service = BatchService(store, runtime)
        batch = service.create(
            name="取消批次",
            workflow_id=workflow_id,
            states=[CommerceState(requirement=f"商品 {index}") for index in range(3)],
            concurrency_limit=2,
        )
        cancelled_batch = service.cancel(batch.id)
        cancelled_runs = [store.get_run(run_id).status for run_id in batch.run_ids]
        self.confirm(
            "batch cancel",
            cancelled_batch.status is BatchStatus.CANCELLED
            and all(status is ExecutionStatus.CANCELLED for status in cancelled_runs),
            batch_id=batch.id,
            run_count=len(batch.run_ids),
        )

        mixed = store.create_batch("部分失败批次", 2)
        mixed_statuses = [
            ExecutionStatus.COMPLETED,
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        ]
        for position, status in enumerate(mixed_statuses):
            item = store.create_run("commerce", workflow_id, {}, END)
            store.update_run(
                item.id, status=status, state=item.state, current_node=item.current_node
            )
            store.add_batch_run(mixed.id, item.id, position)
        mixed = service.refresh(mixed.id)
        self.confirm(
            "batch partial failed summary",
            mixed.status is BatchStatus.PARTIAL_FAILED,
            batch_id=mixed.id,
            statuses=[status.value for status in mixed_statuses],
        )
        return {
            "cancelled_batch_id": batch.id,
            "cancelled_run_count": len(batch.run_ids),
            "partial_failed_batch_id": mixed.id,
        }

    def hidden_capabilities(self, directory: Path) -> dict[str, Any]:
        registry = SkillRegistry()
        discovered = SkillLoader(ROOT / "skills", project_root=ROOT).load(registry)
        self.confirm(
            "skill loader discovers project skills",
            len(discovered) >= 4,
            skill_ids=[item.id for item in discovered],
        )

        extension_root = directory / "extension"
        skill_dir = extension_root / "skills" / "runtime_probe"
        skill_dir.mkdir(parents=True)
        (skill_dir / "manifest.yaml").write_text(
            "\n".join(
                (
                    "id: demo.runtime_probe",
                    "name: Runtime Probe",
                    "domain: demo",
                    "description: Verify directory discovery.",
                    "version: 1.0.0",
                    "required_tools: []",
                    "input_schema: {}",
                    "output_schema: {}",
                    "handler_file: handler.py",
                    "handler: execute",
                )
            ),
            encoding="utf-8",
        )
        (skill_dir / "handler.py").write_text(
            "def execute(inputs, context):\n    return {'discovered': True}\n",
            encoding="utf-8",
        )
        extension_registry = SkillRegistry()
        SkillLoader(
            extension_root / "skills", project_root=extension_root
        ).load(extension_registry)
        extension_result = extension_registry.execute("demo.runtime_probe", {}, {})
        self.confirm(
            "new skill needs no core modification",
            extension_result == {"discovered": True},
            skill_id="demo.runtime_probe",
        )

        video_db = directory / "video.db"
        video_settings = _budget_settings(video_db)
        with patch("kantoku.core.budget.get_settings", return_value=video_settings):
            store = RuntimeStore(video_db)
            run = store.create_run("comic", "video.acceptance", {}, "video")
            image = store.create_artifact(
                type=ArtifactType.IMAGE,
                run_id=run.id,
                node_id="image",
                source="acceptance",
                location="mock://image.png",
            )
            service = VideoService(
                store,
                MockVideoProvider(),
                VideoSettings(enabled=True, estimated_fen=1, max_fen=1),
            )
            request = VideoGenerationRequest(
                request_id="video-acceptance-001",
                run_id=run.id,
                node_id="video",
                image_artifact_id=image.id,
                prompt="轻微镜头推进",
                project="acceptance",
                shot_no=1,
            )
            first = service.generate(request)
            second = service.generate(request)
            self.confirm(
                "video mock full path",
                first == second
                and first.metadata["mock"] is True
                and first.location is not None
                and first.location.startswith("mock://video/"),
                artifact_id=first.id,
                location=first.location,
            )
            rejected_request = request.model_copy(
                update={"request_id": "video-budget-reject-001"}
            )
            rejected = False
            try:
                VideoService(
                    store,
                    MockVideoProvider(),
                    VideoSettings(enabled=True, estimated_fen=2, max_fen=1),
                ).generate(rejected_request)
            except BudgetError:
                rejected = True
            self.confirm(
                "video budget rejects before reservation",
                rejected
                and budget.get_reservation(rejected_request.request_id) is None,
                request_id=rejected_request.request_id,
            )

        image_db = directory / "image.db"
        image_settings = _budget_settings(image_db)
        with patch("kantoku.core.budget.get_settings", return_value=image_settings):
            provider = LocalFakeImageProvider(
                directory / "images", model_id="acceptance-image", actual_fen=0
            )
            generation = {
                "prompt": "验收用本地假图",
                "shot_no": 1,
                "project": "acceptance",
                "episode": "retry-boundary",
                "client_request_id": "image-retry-boundary-001",
                "provider": provider,
                "est_fen": 1,
            }
            first_image = gen_image(**generation)
            second_image = gen_image(**generation)
            self.confirm(
                "provider and workflow retry do not amplify submission",
                first_image == second_image and provider.submit_count == 1,
                provider_submit_count=provider.submit_count,
            )

        artifact_store = RuntimeStore(directory / "commerce.db")
        commerce_artifacts = artifact_store.list_artifacts(domain="commerce")
        looked_up = artifact_store.get_artifact(commerce_artifacts[0].id)
        self.confirm(
            "artifact global query and lookup",
            bool(commerce_artifacts) and looked_up == commerce_artifacts[0],
            count=len(commerce_artifacts),
            artifact_id=looked_up.id,
        )

        retry_store = RuntimeStore(directory / "retry.db")
        retry_runtime, retry_workflow_id = _runtime(retry_store)
        workflow = retry_runtime.workflow(retry_workflow_id)
        self.confirm(
            "commerce workflow does not multiply provider retry",
            all(node.retry_limit == 0 for node in workflow.nodes.values()),
            retry_limits={key: node.retry_limit for key, node in workflow.nodes.items()},
        )
        return {
            "project_skill_count": len(discovered),
            "dynamic_skill": "demo.runtime_probe",
            "video_artifact_id": first.id,
            "global_commerce_artifact_count": len(commerce_artifacts),
            "image_provider_submit_count": provider.submit_count,
        }


def run(output: Path | None = None) -> dict[str, Any]:
    verifier = Verifier()
    with tempfile.TemporaryDirectory(prefix="kantoku-acceptance-") as raw_directory:
        directory = Path(raw_directory)
        report = {
            "suite": "Core v0.2 + Commerce Workspace v0.1",
            "mode": "real runtime with explicit mock external adapters",
            "commerce": verifier.commerce(directory),
            "routes": verifier.routes_and_limits(directory),
            "batches": verifier.cancellation_and_batches(directory),
            "hidden_capabilities": verifier.hidden_capabilities(directory),
            "checks": verifier.checks,
            "passed": len(verifier.checks),
        }
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="保存 JSON 验收报告")
    args = parser.parse_args()
    print(json.dumps(run(args.output), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
